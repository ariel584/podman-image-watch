# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from podman_image_watch.quadlet import parse_reference
from podman_image_watch.registry import RegistryClient, RegistryError, retry_delay

REF = parse_reference("docker.io/example/app:1.2.3")


def make_client(config, handler, cache=None, sleep=lambda _: None):
    return RegistryClient(
        config["registries"], cache, transport=httpx.MockTransport(handler), sleep=sleep
    )


def test_anonymous_pagination_dedup_and_cache(config):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "last=" not in str(request.url):
            return httpx.Response(
                200,
                json={"tags": ["1.2.3"]},
                headers={"Link": '</v2/example/app/tags/list?n=1000&last=1.2.3>; rel="next"'},
            )
        return httpx.Response(200, json={"tags": ["1.2.4"]})

    config["registries"]["cache_ttl"] = "1h"
    cache = {}
    client = make_client(config, handler, cache)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: client.tags(REF), range(8))) == [["1.2.3", "1.2.4"]] * 8
    assert len(calls) == 2
    client.close()
    cached_client = make_client(config, lambda _: pytest.fail("Should use persisted cache"), cache)
    assert cached_client.tags(REF) == ["1.2.3", "1.2.4"]
    cached_client.close()


def test_bearer_challenge_credentials(config, monkeypatch):
    monkeypatch.setenv("REG_USER", "user")
    monkeypatch.setenv("REG_PASSWORD", "secret-password")
    config["registries"]["credentials"] = {
        "docker.io": {"username_env": "REG_USER", "password_env": "REG_PASSWORD"}
    }

    def handler(request):
        if request.url.host == "auth.docker.io":
            assert request.headers["Authorization"].startswith("Basic ")
            assert request.url.params["scope"] == "repository:example/app:pull"
            return httpx.Response(200, json={"token": "secret-token"})
        if request.headers.get("Authorization") == "Bearer secret-token":
            return httpx.Response(200, json={"tags": ["1.2.3"]})
        assert "Authorization" not in request.headers
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:wrong:push"'
            },
        )

    client = make_client(config, handler)
    assert client.tags(REF) == ["1.2.3"]
    assert "secret" not in repr(client.cache)
    client.close()


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_http_failure(config, status):
    client = make_client(config, lambda _: httpx.Response(status, text="secret response body"))
    with pytest.raises(RegistryError) as error:
        client.tags(REF)
    assert "secret" not in str(error.value)
    client.close()


def test_rate_limit_and_connection_retry(config):
    config["registries"]["retries"] = 2
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("token=secret")
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"tags": []})

    client = make_client(config, handler, sleep=delays.append)
    assert client.tags(REF) == []
    assert delays == [1, 0] and len(calls) == 3
    client.close()


@pytest.mark.parametrize(
    "payload", [[], {"tags": "bad"}, {"tags": ["bad tag"]}, {}, {"tags": [123]}]
)
def test_malformed_tags(config, payload):
    client = make_client(config, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(RegistryError):
        client.tags(REF)
    client.close()


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/v2/example/app/tags/list",
        "http://registry-1.docker.io/v2/example/app/tags/list",
        "/v2/other/app/tags/list",
        "/v2/example/app/tags/list?token=secret",
    ],
)
def test_reject_unsafe_pagination(config, target):
    client = make_client(
        config,
        lambda _: httpx.Response(
            200, json={"tags": []}, headers={"Link": f'<{target}>; rel="next"'}
        ),
    )
    with pytest.raises(RegistryError):
        client.tags(REF)
    client.close()


def test_reject_foreign_auth_and_redirect(config):
    for response in [
        httpx.Response(
            401, headers={"WWW-Authenticate": 'Bearer realm="https://evil.example/token"'}
        ),
        httpx.Response(302, headers={"Location": "https://evil.example"}),
    ]:
        calls = []

        def handler(request, response=response, calls=calls):
            calls.append(request)
            return response

        client = make_client(config, handler)
        with pytest.raises(RegistryError):
            client.tags(REF)
        assert len(calls) == 1
        client.close()


def test_limits_and_invalid_json(config):
    config["registries"]["max_response_bytes"] = 20
    for response in [
        httpx.Response(200, content=b"x" * 21),
        httpx.Response(200, content=b"not JSON"),
    ]:
        client = make_client(config, lambda _, response=response: response)
        with pytest.raises(RegistryError):
            client.tags(REF)
        client.close()


def test_pagination_limit(config):
    config["registries"]["max_pages"] = 1
    client = make_client(
        config,
        lambda _: httpx.Response(
            200,
            json={"tags": ["1.2.3"]},
            headers={"Link": '</v2/example/app/tags/list?last=1.2.3>; rel="next"'},
        ),
    )
    with pytest.raises(RegistryError, match="incomplete"):
        client.tags(REF)
    assert not client.cache
    client.close()


def test_manifest_index_and_digest_pin(config):
    body = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": "sha256:" + "a" * 64,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": "sha256:" + "b" * 64,
                    "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
                },
            ],
        }
    ).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    client = make_client(
        config,
        lambda _: httpx.Response(200, content=body, headers={"Docker-Content-Digest": digest}),
    )
    metadata = client.manifest(parse_reference(REF.repository + "@" + digest))
    assert metadata == {
        "current": digest,
        "scope": "index",
        "platforms": ["linux/amd64", "linux/arm64/v8"],
    }
    with pytest.raises(RegistryError, match="Pinned"):
        client.manifest(parse_reference(REF.repository + "@sha256:" + "c" * 64))
    client.close()


def test_manifest_wrong_digest(config):
    client = make_client(
        config,
        lambda _: httpx.Response(
            200,
            json={"schemaVersion": 2, "config": {}},
            headers={"Docker-Content-Digest": "sha256:" + "a" * 64},
        ),
    )
    with pytest.raises(RegistryError, match="digest"):
        client.manifest(REF)
    client.close()


def test_retry_after_bound():
    assert retry_delay("100000", 0) == 30
    assert retry_delay("nonsense", 2) == 4


def test_gzip_metadata(config):
    import gzip

    body = gzip.compress(b'{"tags":["1.2.3"]}')
    client = make_client(
        config, lambda _: httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})
    )
    assert client.tags(REF) == ["1.2.3"]
    client.close()


def test_malformed_manifest_isolated(config):
    client = make_client(
        config,
        lambda _: httpx.Response(
            200,
            json={
                "schemaVersion": 2,
                "manifests": [
                    {"platform": {"os": "linux", "architecture": "arm64", "variant": {"bad": True}}}
                ],
            },
        ),
    )
    with pytest.raises(RegistryError, match="variant"):
        client.manifest(REF)
    client.close()
