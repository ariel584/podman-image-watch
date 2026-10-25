# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, HTTPS-only OCI metadata client. No layer endpoints are used."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .config import duration
from .quadlet import ImageReference

ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
DIGEST = re.compile(r"sha256:[a-f0-9]{64}$")
TAG = re.compile(r"[\w][\w.-]{0,127}$", re.ASCII)


class RegistryError(RuntimeError):
    """Messages are deliberately generic; never include response bodies or URLs."""


def https_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise RegistryError("Rejected malformed metadata URL") from None
    if (
        len(url) > 4096
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise RegistryError("Rejected unsafe metadata URL")
    if port is not None and not 1 <= port <= 65535:
        raise RegistryError("Rejected metadata URL port")


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return min(30, max(0, float(value)))
        except ValueError:
            try:
                return min(
                    30, max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
                )
            except (ValueError, TypeError, OverflowError):
                pass
    return min(8, 2**attempt)


class RegistryClient:
    def __init__(
        self,
        config: dict,
        cache: dict | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.cache = cache if cache is not None else {}
        self.sleep = sleep
        self.http = httpx.Client(
            timeout=config["timeout"],
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=config["max_parallel_requests"],
                max_keepalive_connections=config["max_parallel_requests"],
            ),
            transport=transport,
            headers={"User-Agent": "podman-image-watch/0.1.0"},
        )
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._memo: dict[str, object] = {}

    def close(self) -> None:
        self.http.close()

    def _deduplicated(self, key: str, operation: Callable):
        with self._lock:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key not in self._memo:
                try:
                    self._memo[key] = operation()
                except RegistryError as exc:
                    self._memo[key] = exc
            value = self._memo[key]
            if isinstance(value, RegistryError):
                raise value
            return value

    def _request(self, url: str, headers: dict, deadline: float) -> httpx.Response:
        https_url(url)
        for attempt in range(self.config["retries"] + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RegistryError("Registry metadata deadline exceeded")
            try:
                with self.http.stream(
                    "GET", url, headers=headers, timeout=min(self.config["timeout"], remaining)
                ) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > self.config["max_response_bytes"]:
                            raise RegistryError("Registry response exceeds configured size limit")
                        if time.monotonic() > deadline:
                            raise RegistryError("Registry metadata deadline exceeded")
                        data.extend(chunk)
                    decoded_headers = dict(response.headers)
                    decoded_headers.pop("content-encoding", None)
                    decoded_headers.pop("content-length", None)
                    result = httpx.Response(
                        response.status_code,
                        headers=decoded_headers,
                        content=bytes(data),
                        request=response.request,
                    )
            except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
                if attempt == self.config["retries"]:
                    raise RegistryError("Registry connection or TLS request failed") from None
                result = None
            if result is not None and result.status_code != 429 and result.status_code < 500:
                return result
            if attempt == self.config["retries"]:
                raise RegistryError(
                    f"Registry HTTP {result.status_code}"
                    if result
                    else "Registry connection failed"
                )
            delay = retry_delay(result.headers.get("Retry-After") if result else None, attempt)
            if time.monotonic() + delay >= deadline:
                raise RegistryError("Registry retry would exceed metadata deadline")
            self.sleep(delay)
        raise RegistryError("Registry request failed")

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            value = response.json()
        except (ValueError, UnicodeError, RecursionError):
            raise RegistryError("Malformed registry JSON") from None
        if not isinstance(value, dict):
            raise RegistryError("Registry response must be a JSON object")
        return value

    def _credential(self, registry: str) -> tuple[dict, dict]:
        credential = self.config["credentials"].get(registry, {})
        values = {}
        for key in ("username_env", "password_env", "token_env"):
            name = credential.get(key)
            if name:
                value = os.environ.get(name)
                if not value:
                    raise RegistryError(
                        "Configured registry credential environment variable is missing"
                    )
                if key == "token_env" and (
                    not value.isascii() or any(c.isspace() for c in value) or len(value) > 16384
                ):
                    raise RegistryError("Configured registry token has an invalid format")
                values[key] = value
        return credential, values

    def _auth(self, ref: ImageReference, response: httpx.Response, deadline: float) -> str:
        challenge = response.headers.get("WWW-Authenticate", "")
        if len(challenge) > 4096 or not challenge.lower().startswith("bearer "):
            raise RegistryError("Registry requires unsupported authentication")
        params = dict(re.findall(r'(\w+)="([^"\r\n]*)"', challenge[7:]))
        realm = params.get("realm", "")
        https_url(realm)
        target = urlsplit(realm)
        credential, values = self._credential(ref.registry)
        host = "registry-1.docker.io" if ref.registry == "docker.io" else ref.registry
        allowed = {host, ref.registry, *credential.get("auth_hosts", [])}
        if ref.registry == "docker.io":
            allowed.add("auth.docker.io")
        if target.netloc not in allowed:
            raise RegistryError(
                "Bearer realm host is untrusted; configure credentials.auth_hosts explicitly"
            )
        query = dict(parse_qsl(target.query))
        query.update({"scope": f"repository:{ref.name}:pull"})
        if params.get("service"):
            query["service"] = params["service"]
        url = urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), ""))
        headers = {}
        if values.get("username_env") or values.get("password_env"):
            auth = f"{values.get('username_env', '')}:{values.get('password_env', '')}"
            headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        token_response = self._request(url, headers, deadline)
        if token_response.status_code != 200:
            raise RegistryError(f"Token endpoint HTTP {token_response.status_code}")
        payload = self._json(token_response)
        token = payload.get("token") or payload.get("access_token")
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 16384
            or any(c.isspace() for c in token)
        ):
            raise RegistryError("Invalid bearer token response")
        return "Bearer " + token

    def _get(self, ref: ImageReference, url: str, deadline: float) -> httpx.Response:
        _, values = self._credential(ref.registry)
        headers = {"Accept": ACCEPT}
        if values.get("token_env"):
            headers["Authorization"] = "Bearer " + values["token_env"]
        else:
            cached_auth = self._memo.get("auth:" + ref.repository)
            if isinstance(cached_auth, str):
                headers["Authorization"] = cached_auth
        result = self._request(url, headers, deadline)
        if result.status_code == 401:
            headers["Authorization"] = self._deduplicated(
                "auth:" + ref.repository, lambda: self._auth(ref, result, deadline)
            )
            result = self._request(url, headers, deadline)
        if result.status_code != 200:
            raise RegistryError(f"Registry HTTP {result.status_code}")
        return result

    @staticmethod
    def _base(ref: ImageReference) -> str:
        host = "registry-1.docker.io" if ref.registry == "docker.io" else ref.registry
        return f"https://{host}/v2/{ref.name}"

    def tags(self, ref: ImageReference) -> list[str]:
        return self._deduplicated("tags:" + ref.repository, lambda: self._tags(ref))

    def _tags(self, ref: ImageReference) -> list[str]:
        key = ref.repository
        cached = self.cache.get(key, {})
        ttl = duration(self.config["cache_ttl"])
        now = time.time()
        if (
            isinstance(cached, dict)
            and type(cached.get("at")) in (float, int)
            and 0 <= now - cached["at"] < ttl
        ):
            tags = cached.get("tags")
            if (
                isinstance(tags, list)
                and len(tags) <= self.config["max_tags"]
                and all(isinstance(t, str) and TAG.fullmatch(t) for t in tags)
            ):
                return tags
        deadline = time.monotonic() + self.config["deadline"]
        base = self._base(ref) + "/tags/list"
        url = base + "?n=1000"
        seen: set[str] = set()
        tags_set: set[str] = set()
        for _ in range(self.config["max_pages"]):
            if url in seen:
                raise RegistryError("Registry pagination loop detected")
            seen.add(url)
            response = self._get(ref, url, deadline)
            payload = self._json(response)
            tags = payload.get("tags")
            if tags is None and "tags" in payload:
                tags = []
            if not isinstance(tags, list) or any(
                not isinstance(t, str) or not TAG.fullmatch(t) for t in tags
            ):
                raise RegistryError("Invalid registry tag list")
            tags_set.update(tags)
            if len(tags_set) > self.config["max_tags"]:
                raise RegistryError("Registry tag count exceeds configured limit")
            link = response.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
            if match:
                next_url = urljoin(url, match[1])
                https_url(next_url)
                parts, expected = urlsplit(next_url), urlsplit(base)
                if (parts.scheme, parts.netloc, parts.path) != (
                    expected.scheme,
                    expected.netloc,
                    expected.path,
                ):
                    raise RegistryError("Rejected registry pagination URL outside repository")
                if set(dict(parse_qsl(parts.query))) - {"n", "last"}:
                    raise RegistryError("Rejected unexpected registry pagination parameters")
                url = next_url
            elif len(tags) >= 1000:
                url = base + "?" + urlencode({"n": 1000, "last": tags[-1]})
            else:
                result = sorted(tags_set)
                if ttl:
                    self.cache[key] = {"at": now, "tags": result}
                return result
        raise RegistryError("Registry pagination limit exceeded; results are incomplete")

    def manifest(self, ref: ImageReference, *, deadline: float | None = None) -> dict:
        target = ref.digest or ref.tag or "latest"
        return self._deduplicated(
            f"manifest:{ref.repository}@{target}", lambda: self._manifest(ref, target, deadline)
        )

    def _manifest(self, ref: ImageReference, target: str, deadline: float | None = None) -> dict:
        response = self._get(
            ref,
            self._base(ref) + "/manifests/" + quote(target, safe=":"),
            deadline if deadline is not None else time.monotonic() + self.config["deadline"],
        )
        payload = self._json(response)
        if payload.get("schemaVersion") != 2 or not (
            isinstance(payload.get("manifests"), list) or isinstance(payload.get("config"), dict)
        ):
            raise RegistryError("Unsupported or malformed OCI manifest")
        # Hash exact manifest bytes, including the index for multi-platform images.
        digest = "sha256:" + hashlib.sha256(response.content).hexdigest()
        header = response.headers.get("Docker-Content-Digest")
        if header and (not DIGEST.fullmatch(header) or header != digest):
            raise RegistryError("Registry manifest digest does not match content")
        if ref.digest and ref.digest.lower() != digest:
            raise RegistryError("Pinned manifest digest does not match content")
        platforms = []
        for manifest in payload.get("manifests", []):
            if not isinstance(manifest, dict):
                raise RegistryError("Invalid manifest index descriptor")
            platform = manifest.get("platform", {})
            if (
                isinstance(platform, dict)
                and isinstance(platform.get("os"), str)
                and isinstance(platform.get("architecture"), str)
            ):
                if "variant" in platform and not isinstance(platform["variant"], str):
                    raise RegistryError("Invalid manifest platform variant")
                platforms.append(
                    "/".join(
                        platform[k] for k in ("os", "architecture", "variant") if platform.get(k)
                    )
                )
        return {
            "current": digest,
            "scope": "index" if "manifests" in payload else "manifest",
            "platforms": sorted(set(platforms)),
        }
