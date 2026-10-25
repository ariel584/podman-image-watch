# SPDX-License-Identifier: GPL-3.0-or-later
"""Identify versioned aliases of a floating reference without guessing from tag order."""

from __future__ import annotations

import fnmatch
import time
from dataclasses import replace

from .quadlet import ImageReference
from .registry import RegistryClient, RegistryError
from .versions import FLOATING, compare, parse


def resolve(
    ref: ImageReference,
    observed: dict,
    client: RegistryClient,
    policy: dict,
    *,
    max_candidates: int,
    timeout: float,
) -> dict:
    result: dict = {
        "status": "unknown",
        "source": "matching_manifest_digest",
        "tag": None,
        "version": None,
        "matching_tags": [],
        "checked_tags": 0,
        "reason": "No matching versioned tag found within the configured search limit",
    }
    deadline = time.monotonic() + timeout
    tags = client.tags(ref)
    options = policy.get("version", {})
    alias = ref.tag or "latest"
    floating = FLOATING.fullmatch(alias)
    suffix = alias[len(floating[1]) :] if floating else ""
    candidates = []
    for tag in tags:
        parsed = parse(tag, options)
        if parsed is None or compare(tag, [], policy, ref.repository)[0] in {
            "floating",
            "unsupported",
        }:
            continue
        if parsed.prerelease and not options.get(
            "include_prerelease", policy.get("include_prerelease", False)
        ):
            continue
        if options.get("allowed_majors") and parsed.numbers[0] not in options["allowed_majors"]:
            continue
        channel = "prerelease" if parsed.prerelease else "stable"
        if options.get("channels") and channel not in options["channels"]:
            continue
        if options.get("tag_patterns") and not any(
            fnmatch.fnmatchcase(tag, p) for p in options["tag_patterns"]
        ):
            continue
        if any(fnmatch.fnmatchcase(tag, p) for p in policy["updates"].get("ignore_versions", [])):
            continue
        # Prefer the explicit floating variant, e.g. stable-alpine -> 1.28.0-alpine.
        # Every result still requires an identical digest; names are never proof of identity.
        candidates.append((parsed.suffix == suffix, parsed.version, tag))
    matches = []
    for _, version, tag in sorted(candidates, reverse=True)[:max_candidates]:
        if time.monotonic() >= deadline:
            raise RegistryError("Floating-tag version resolution deadline exceeded")
        metadata = client.manifest(replace(ref, tag=tag, digest=None), deadline=deadline)
        result["checked_tags"] += 1
        if metadata["scope"] == observed["scope"] and metadata["current"] == observed["current"]:
            matches.append((version, tag))
    if not matches:
        return result
    result["matching_tags"] = sorted(tag for _, tag in matches)
    versions = {str(version) for version, _ in matches}
    if len(versions) != 1:
        result.update(
            status="ambiguous", reason="Matching tags advertise conflicting numeric versions"
        )
        return result
    # Multiple aliases of the same version are fine. Prefer the matching floating variant.
    matching_names = {tag for _, tag in matches}
    selected = next(tag for _, _, tag in sorted(candidates, reverse=True) if tag in matching_names)
    result.update(status="resolved", tag=selected, version=next(iter(versions)), reason=None)
    return result
