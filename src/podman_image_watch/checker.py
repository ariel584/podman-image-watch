# SPDX-License-Identifier: GPL-3.0-or-later
"""Discovery, registry metadata and policy evaluation for a single scan."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from . import __version__
from .config import duration, policy_for, snoozed
from .floating import resolve
from .quadlet import Service, discover, parse_reference
from .registry import RegistryClient, RegistryError
from .versions import compare, display_tag

PRIORITY_RANK = {"information": 0, "planning": 1, "attention": 2, "warning": 3}
LOG = logging.getLogger(__name__)


def utc_text(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def check_service(
    service: Service, config: dict, client: RegistryClient, baselines: dict, now: datetime
) -> tuple[dict, tuple[str, dict] | None]:
    result: dict = {
        "id": service.id,
        "source": service.source,
        "quadlet": service.quadlet,
        "source_file": service.source_file,
        "display_name": service.display_name,
        "image": service.image,
        "repository": None,
        "current_version": None,
        "configured_digest": None,
        "check_status": "unknown",
        "update_status": "unknown",
        "priority": "information",
        "version_scheme": None,
        "resolved_tag": None,
        "resolved_version": None,
        "resolution": None,
        "reason": None,
        "updates": {"patch": None, "minor": None, "major": None, "digest_changed": None},
        "events": [],
        "digest": None,
        "acknowledged": False,
        "snoozed": False,
        "notification_suppressed": False,
        "labels": {},
        "notify": True,
        "metrics": {
            "attention": 0,
            "planning": 0,
            "errors": 0,
            "unknown": 0,
            "patch": 0,
            "minor": 0,
            "major": 0,
            "digest_changed": 0,
            "digest_attention": 0,
        },
    }
    reference = None
    reason = service.reason
    if not reason:
        try:
            reference = parse_reference(service.image, config["sources"]["allow_short_names"])
            result.update(
                repository=reference.repository,
                current_version=reference.tag,
                configured_digest=reference.digest,
            )
        except ValueError as exc:
            reason = str(exc)
    policy = policy_for(
        config,
        quadlet=service.quadlet,
        repository=reference.repository if reference else service.image.split(":")[0],
        source=service.source,
    )
    result["display_name"] = policy.get("display_name", service.display_name)
    result["labels"] = policy.get("labels", {})
    result["notify"] = policy.get("notify", True)
    if policy.get("ignore"):
        result.update(
            check_status="skipped",
            update_status="skipped",
            reason="Ignored by configured rule",
            notification_suppressed=True,
        )
        return result, None
    if reason:
        result.update(check_status="unknown", reason=reason)
        result["metrics"]["unknown"] = 1
        return result, None
    assert reference is not None
    updates = policy["updates"]
    baseline = None
    try:
        if reference.digest:
            # Immutable pins have no moving target. Verify metadata, without inventing tag upgrades.
            result["digest"] = client.manifest(reference)
            result.update(version_scheme="digest", check_status="ok", update_status="pinned")
            return result, None
        scheme, candidates, unsupported = compare(
            reference.tag or "latest", [], policy, reference.repository
        )
        result["version_scheme"] = scheme
        if scheme not in {"floating", "unsupported"}:
            tags = client.tags(reference)
            scheme, candidates, unsupported = compare(
                reference.tag or "latest", tags, policy, reference.repository
            )
            # A missing current tag makes an otherwise empty candidate set inconclusive.
            if reference.tag not in tags:
                unsupported = "Configured tag is absent from registry metadata"
        result["updates"].update(candidates)
        resolve_version = scheme == "floating" and updates.get("resolve_floating", False)
        check_digest = updates.get("check_digest", True)
        if resolve_version or (
            check_digest and (scheme == "floating" or updates.get("monitor_immutable", False))
        ):
            digest = client.manifest(reference)
            key = reference.repository + ":" + (reference.tag or "latest")
            previous = baselines.get(key)
            if not isinstance(previous, dict) or previous.get("scope") != digest["scope"]:
                previous = None
            digest["previous"] = previous.get("current") if previous else None
            digest["baseline_established"] = previous is None
            if check_digest and previous and previous.get("current") != digest["current"]:
                result["updates"]["digest_changed"] = digest["current"]
            result["digest"] = digest
            if check_digest:
                baseline = (key, {"current": digest["current"], "scope": digest["scope"]})
            if resolve_version:
                try:
                    result["resolution"] = resolve(
                        reference,
                        digest,
                        client,
                        policy,
                        max_candidates=config["registries"]["max_resolution_candidates"],
                        timeout=config["registries"]["deadline"],
                    )
                except RegistryError as exc:
                    result["resolution"] = {
                        "status": "error",
                        "source": "matching_manifest_digest",
                        "tag": None,
                        "version": None,
                        "matching_tags": [],
                        "checked_tags": None,
                        "reason": str(exc),
                    }
                    raise
                result["resolved_tag"] = result["resolution"]["tag"]
                result["resolved_version"] = result["resolution"]["version"]
        if scheme == "floating" and not check_digest and not resolve_version:
            result.update(
                check_status="unsupported",
                update_status="unknown",
                reason="Floating tag digest monitoring is disabled",
            )
        elif unsupported:
            result.update(check_status="unsupported", update_status="unknown", reason=unsupported)
        else:
            result.update(check_status="ok", update_status="up_to_date")
            if scheme == "floating" and not check_digest:
                result.update(
                    update_status="unknown",
                    reason="Only the registry alias version was checked; digest monitoring is disabled",
                )
    except RegistryError as exc:
        result.update(
            check_status="error", update_status="unknown", priority="warning", reason=str(exc)
        )
    until = updates.get("snooze_until")
    result["snoozed"] = snoozed(until, now)
    result["snooze_until"] = str(until) if until else None
    result["snooze_reason"] = updates.get("reason", "")
    for kind, candidate in result["updates"].items():
        if candidate is None:
            continue
        ack = next(
            (
                a
                for a in updates.get("acknowledge", [])
                if a["type"] == kind and a["version"] == candidate
            ),
            None,
        )
        result["events"].append(
            {
                "type": kind,
                "version": candidate,
                "priority": policy["severity"][kind],
                "acknowledged": ack is not None,
                "acknowledgement_reason": ack.get("reason", "") if ack else None,
                "snoozed": result["snoozed"],
                "notification_suppressed": bool(ack or result["snoozed"] or not result["notify"]),
            }
        )
        result["metrics"][kind] = 1
    events = result["events"]
    if events:
        if result["check_status"] == "ok":
            result["update_status"] = "updates_available"
        if result["check_status"] != "error":
            result["priority"] = max((e["priority"] for e in events), key=PRIORITY_RANK.__getitem__)
        result["acknowledged"] = all(e["acknowledged"] for e in events)
        result["notification_suppressed"] = all(e["notification_suppressed"] for e in events)
    result["metrics"]["attention"] = int(
        any(e["priority"] == "attention" and not e["notification_suppressed"] for e in events)
    )
    result["metrics"]["planning"] = int(
        any(e["priority"] == "planning" and not e["notification_suppressed"] for e in events)
    )
    result["metrics"]["digest_attention"] = int(
        any(e["type"] == "digest_changed" and not e["notification_suppressed"] for e in events)
    )
    result["metrics"]["errors"] = int(result["check_status"] == "error")
    result["metrics"]["unknown"] = int(result["check_status"] in {"unknown", "unsupported"})
    return result, baseline


def scan(
    config: dict, state: dict, client: RegistryClient | None = None, now: datetime | None = None
) -> dict:
    start = time.monotonic()
    now = now or datetime.now(UTC)
    services, diagnostics = discover(config)
    LOG.debug("Discovered %d services; %d source diagnostics", len(services), len(diagnostics))
    own_client = client is None
    # Remove expired cache entries, and never cache credentials or manifest digests.
    ttl = duration(config["registries"]["cache_ttl"])
    state["cache"] = {
        k: v
        for k, v in state.get("cache", {}).items()
        if isinstance(v, dict)
        and type(v.get("at")) in (int, float)
        and 0 <= time.time() - v["at"] < ttl
    }
    client = client or RegistryClient(config["registries"], state["cache"])
    baselines = state.get("digests", {})
    try:
        with ThreadPoolExecutor(max_workers=config["registries"]["max_parallel_requests"]) as pool:
            outcomes = list(
                pool.map(
                    lambda service: check_service(service, config, client, baselines, now), services
                )
            )
    finally:
        if own_client:
            client.close()
    results = [result for result, _ in outcomes]
    for result in results:
        policy = policy_for(
            config,
            quadlet=result["quadlet"],
            repository=result["repository"] or result["image"].split(":")[0],
            source=result["source"],
        )
        options = policy.get("version", {})
        hide = config["output"]["hide_variants"]
        result["display"] = {
            "current_version": display_tag(
                result["resolved_tag"] or result["current_version"], options, hide
            ),
            "configured_tag": display_tag(result["current_version"], options, hide),
            "updates": {
                kind: display_tag(tag, options, hide) if kind != "digest_changed" else tag
                for kind, tag in result["updates"].items()
            },
        }
    # Keep baselines only for currently monitored references; preserve failed observations.
    live_keys = set()
    for service in results:
        if service["repository"] and service["current_version"]:
            live_keys.add(service["repository"] + ":" + service["current_version"])
    state["digests"] = {k: v for k, v in baselines.items() if k in live_keys}
    for _, baseline in outcomes:
        if baseline:
            state["digests"][baseline[0]] = baseline[1]
    errors = sum(s["check_status"] == "error" for s in results) + len(diagnostics)
    unknown = sum(s["check_status"] in {"unknown", "unsupported"} for s in results)
    status = "partial" if errors or unknown else "success"
    LOG.debug("Image metadata scan %s: %d errors, %d unknown results", status, errors, unknown)
    if status == "success":
        state["last_success_at"] = utc_text(now)
    last_success = state.get("last_success_at")
    try:
        last_success_epoch = (
            int(datetime.fromisoformat(last_success).timestamp()) if last_success else 0
        )
    except (ValueError, TypeError):
        last_success, last_success_epoch = None, 0
    summary = {
        "total_services": len(results),
        "unique_images": len({s["image"] for s in results if s["image"]}),
        "errors": errors,
        "unknown": unknown,
        "skipped": sum(s["check_status"] == "skipped" for s in results),
        "attention_services": sum(s["metrics"]["attention"] for s in results),
        "planning_services": sum(s["metrics"]["planning"] for s in results),
        "update_services": sum(bool(s["events"]) for s in results),
    }
    for kind in ("patch", "minor", "major", "digest_changed"):
        summary[kind + "_updates"] = sum(s["updates"][kind] is not None for s in results)
    return {
        "schema_version": 1,
        "tool": "podman-image-watch",
        "tool_version": __version__,
        "generated_at": utc_text(now),
        "scan": {
            "status": status,
            "health": int(status != "success"),
            "timestamp": int(now.timestamp()),
            "last_success_at": last_success,
            "last_success_timestamp": last_success_epoch,
            "duration_seconds": round(time.monotonic() - start, 3),
            "diagnostics": diagnostics,
        },
        "summary": summary,
        "services": results,
        "notifications": {"status": "disabled", "errors": []},
    }
