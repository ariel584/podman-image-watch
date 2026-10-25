# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative version policies retaining independent update candidates."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

FLOATING = re.compile(r"^(latest|stable|edge|main|master|nightly|rolling)(?:[-.][\w.-]+)?$")
TAG = re.compile(
    r"^(?P<prefix>v|[a-z][a-z0-9]*-)?(?P<numbers>\d+(?:\.\d+){0,2})(?P<pre>[-.]?(?:a|b|alpha|beta|rc|dev)\d*(?:\.\d+)*)?(?P<suffix>[-_][a-zA-Z0-9][\w.-]*)?$"
)


@dataclass(frozen=True)
class Parsed:
    numbers: tuple[int, ...]
    prefix: str
    suffix: str
    version: Version
    prerelease: bool


def parse(tag: str, options: dict) -> Parsed | None:
    original = tag
    prefix, suffix = options.get("prefix"), options.get("suffix")
    if prefix is not None:
        if not tag.startswith(prefix):
            return None
        tag = tag[len(prefix) :]
    if suffix:
        if not tag.endswith(suffix):
            return None
        tag = tag[: -len(suffix)]
    match = TAG.fullmatch(tag)
    if not match:
        return None
    if prefix is None and match["prefix"] not in (None, "v", "ubuntu-", "alpine-", "debian-"):
        # Unknown prefixes require an explicit profile prefix, rather than a guess.
        return None
    numbers = tuple(int(n) for n in match["numbers"].split("."))
    try:
        version = Version(match["numbers"] + (match["pre"] or ""))
    except InvalidVersion:
        return None
    if len(original) > 128:
        return None
    return Parsed(
        numbers,
        prefix if prefix is not None else match["prefix"] or "",
        suffix or match["suffix"] or "",
        version,
        version.is_prerelease or version.is_devrelease,
    )


def compare(
    current: str, tags: list[str], policy: dict, repository: str = ""
) -> tuple[str, dict[str, str | None], str | None]:
    updates: dict[str, str | None] = {"patch": None, "minor": None, "major": None}
    options = policy.get("version", {})
    scheme = options.get("scheme", policy.get("version_scheme", "auto"))
    if FLOATING.fullmatch(current):
        return "floating", updates, None
    parsed = parse(current, options)
    if parsed is None:
        return "unsupported", updates, "Unrecognized tag semantics; configure a version policy"
    if scheme == "auto":
        if repository.endswith("/postgres"):
            scheme = "postgres"
        elif 2000 <= parsed.numbers[0] <= 2199 and len(parsed.numbers) >= 2:
            scheme = "calver"
        elif len(parsed.numbers) == 3:
            scheme = "semver"
        else:
            return (
                "unsupported",
                updates,
                "Partial numeric tag has ambiguous version semantics; select maintenance or postgres",
            )
    if scheme in {"semver", "zabbix"} and len(parsed.numbers) != 3:
        return "unsupported", updates, "This policy requires a three-component version"
    if scheme == "calver" and (len(parsed.numbers) != 3 or not 1 <= parsed.numbers[1] <= 12):
        return "unsupported", updates, "CalVer requires YYYY.month.release"
    if scheme == "postgres" and len(parsed.numbers) != (3 if parsed.numbers[0] < 10 else 2):
        return (
            "unsupported",
            updates,
            "PostgreSQL requires a maintenance tag, not a floating major alias",
        )
    if scheme == "maintenance" and len(parsed.numbers) < 2:
        return "unsupported", updates, "Major-only aliases cannot identify a maintenance release"
    best: dict[str, tuple[Version, str]] = {}
    include_pre = options.get("include_prerelease", policy.get("include_prerelease", False))
    for candidate in tags:
        other = parse(candidate, options)
        if other is None or other.version <= parsed.version:
            continue
        if scheme == "postgres":
            if len(other.numbers) != (3 if other.numbers[0] < 10 else 2):
                continue
        elif len(other.numbers) != len(parsed.numbers):
            continue
        if options.get("preserve_variant", True) and (
            other.prefix != parsed.prefix or other.suffix != parsed.suffix
        ):
            continue
        if other.prerelease and not include_pre:
            continue
        if options.get("allowed_majors") and other.numbers[0] not in options["allowed_majors"]:
            continue
        if options.get("tag_patterns") and not any(
            fnmatch.fnmatchcase(candidate, pattern) for pattern in options["tag_patterns"]
        ):
            continue
        channels = options.get("channels", [])
        channel = "prerelease" if other.prerelease else "stable"
        if channels and channel not in channels:
            continue
        if any(
            fnmatch.fnmatchcase(candidate, pattern)
            for pattern in policy.get("updates", {}).get("ignore_versions", [])
        ):
            continue
        if scheme == "calver":
            if not 1 <= other.numbers[1] <= 12:
                continue
            category = "patch" if other.numbers[:2] == parsed.numbers[:2] else "minor"
        else:
            major_len = options.get(
                "major_components", 2 if scheme == "postgres" and parsed.numbers[0] < 10 else 1
            )
            minor_len = options.get(
                "minor_components", major_len if scheme in {"postgres", "maintenance"} else 2
            )
            if other.numbers[:major_len] != parsed.numbers[:major_len]:
                category = "major"
            elif other.numbers[:minor_len] != parsed.numbers[:minor_len]:
                category = "minor"
            else:
                category = "patch"
        if not policy.get("updates", {}).get("check_" + category, True):
            continue
        rank = (other.version, candidate)
        if rank > best.get(category, (Version("0"), "")):
            best[category] = rank
            updates[category] = candidate
    return scheme, updates, None
