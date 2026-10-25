# SPDX-License-Identifier: GPL-3.0-or-later
from copy import deepcopy
from datetime import UTC, datetime

import httpx

from podman_image_watch.checker import scan
from podman_image_watch.registry import RegistryClient


class FakeRegistry:
    def __init__(self):
        self.digest = "sha256:" + "a" * 64
        self.tags_result = ["1.2.3", "1.2.4", "1.3.0", "2.0.0"]

    def tags(self, _):
        return self.tags_result

    def manifest(self, _):
        return {"current": self.digest, "scope": "index", "platforms": ["linux/amd64"]}


def test_report_independent_candidates_and_suppression(config):
    config["rules"] = [
        {
            "match": {"image": "docker.io/example/app"},
            "display_name": "Application",
            "updates": {
                "acknowledge": [{"type": "major", "version": "2.0.0", "reason": "Planned"}]
            },
        }
    ]
    report = scan(config, {}, FakeRegistry())
    service = report["services"][0]
    assert service["display_name"] == "Application"
    assert service["updates"] == {
        "patch": "1.2.4",
        "minor": "1.3.0",
        "major": "2.0.0",
        "digest_changed": None,
    }
    assert service["metrics"]["attention"] == 1 and service["metrics"]["planning"] == 0
    assert not service["acknowledged"]
    assert report["summary"]["update_services"] == 1
    assert (
        report["summary"]["patch_updates"]
        == report["summary"]["minor_updates"]
        == report["summary"]["major_updates"]
        == 1
    )


def test_snoozed_visible_and_ack_scope(config):
    config["defaults"]["updates"].update(
        snooze_until="2026-12-01", acknowledge=[{"type": "major", "version": "1.9.0"}]
    )
    report = scan(config, {}, FakeRegistry(), datetime(2026, 11, 1, tzinfo=UTC))
    service = report["services"][0]
    assert service["snoozed"] and service["notification_suppressed"]
    assert not service["acknowledged"] and len(service["events"]) == 3
    assert report["summary"]["attention_services"] == 0


def test_digest_baseline_and_change(config):
    config["sources"]["images"][0]["image"] = "docker.io/example/app:stable-alpine"
    registry, state = FakeRegistry(), {}
    first = scan(config, state, registry)["services"][0]
    assert first["digest"]["baseline_established"] and not first["events"]
    registry.digest = "sha256:" + "b" * 64
    second = scan(config, state, registry)["services"][0]
    assert second["updates"]["digest_changed"] == registry.digest
    assert second["events"][0]["type"] == "digest_changed"
    assert not scan(config, state, registry)["services"][0]["events"]


def test_error_is_partial_and_retains_other_services(config):
    config["sources"]["images"].append({"id": "bad", "image": "ghcr.io/private/missing:1.0.0"})

    def handler(request):
        return (
            httpx.Response(403)
            if request.url.host == "ghcr.io"
            else httpx.Response(200, json={"tags": ["1.2.3", "1.2.4"]})
        )

    client = RegistryClient(config["registries"], transport=httpx.MockTransport(handler))
    state = {"last_success_at": "2026-01-01T00:00:00Z"}
    report = scan(config, state, client)
    assert report["scan"]["status"] == "partial" and report["summary"]["errors"] == 1
    assert report["scan"]["last_success_at"] == "2026-01-01T00:00:00Z"
    assert {s["check_status"] for s in report["services"]} == {"ok", "error"}
    client.close()


def test_missing_tag_unknown(config):
    registry = FakeRegistry()
    registry.tags_result = []
    service = scan(config, {}, registry)["services"][0]
    assert service["check_status"] == "unsupported" and service["update_status"] == "unknown"


def test_ignore_and_local_preserved(config):
    config["sources"]["images"] += [
        {"id": "local", "image": "localhost/built:1.0"},
        {"id": "ignored", "image": "ghcr.io/test/ignored:1.0.0"},
    ]
    config["rules"] = [{"match": {"image": "ghcr.io/test/ignored"}, "ignore": True}]
    report = scan(config, {}, FakeRegistry())
    assert report["summary"]["total_services"] == 3
    assert report["summary"]["skipped"] == report["summary"]["unknown"] == 1


def test_synthetic_installation_integration(config, synthetic_quadlets):
    root, images = synthetic_quadlets
    config["sources"].update(images=[], quadlet_dirs=[str(root)])
    config["rules"] = [
        {"match": {"quadlet": "local.container"}, "ignore": True},
        {"match": {"quadlet": "database.container"}, "version": {"scheme": "postgres"}},
    ]
    tags = {
        "/v2/demo/app/tags/list": ["1.2.3", "1.2.4", "1.3.0", "2.0.0"],
        "/v2/demo/database/tags/list": ["18.6-bookworm", "18.7-bookworm", "19.0-alpine"],
        "/v2/demo/calendar/tags/list": ["2026.8.5", "2026.8.6", "2027.1.0"],
        "/v2/demo/metrics/tags/list": ["ubuntu-7.0.32", "ubuntu-7.0.33", "alpine-7.0.99"],
    }
    calls = []

    def handler(request):
        assert request.url.host == "registry.example.test"
        calls.append(request.url.path)
        if request.url.path == "/v2/demo/gateway/manifests/stable-alpine":
            return httpx.Response(200, json={"schemaVersion": 2, "manifests": []})
        return httpx.Response(200, json={"tags": tags[request.url.path]})

    client = RegistryClient(config["registries"], transport=httpx.MockTransport(handler))
    try:
        report = scan(config, {}, client)
    finally:
        client.close()
    assert report["scan"]["status"] == "success"
    assert report["summary"]["total_services"] == len(images)
    assert report["summary"]["unique_images"] == len(set(images.values()))
    assert report["summary"]["skipped"] == 1
    assert calls.count("/v2/demo/app/tags/list") == 1
    assert calls.count("/v2/demo/gateway/manifests/stable-alpine") == 1
    by_quadlet = {service["quadlet"]: service for service in report["services"]}
    assert set(by_quadlet) == set(images)
    for name in ("frontend.container", "worker.container"):
        assert by_quadlet[name]["updates"] == {
            "patch": "1.2.4",
            "minor": "1.3.0",
            "major": "2.0.0",
            "digest_changed": None,
        }
    assert by_quadlet["database.container"]["updates"]["patch"] == "18.7-bookworm"
    assert by_quadlet["database.container"]["updates"]["major"] is None
    assert by_quadlet["calendar.container"]["updates"]["patch"] == "2026.8.6"
    assert by_quadlet["calendar.container"]["updates"]["minor"] == "2027.1.0"
    assert by_quadlet["calendar.container"]["updates"]["major"] is None
    assert by_quadlet["prefixed.container"]["updates"]["patch"] == "ubuntu-7.0.33"
    assert by_quadlet["floating.container"]["digest"]["baseline_established"]
    assert not by_quadlet["floating.container"]["events"]
    assert by_quadlet["local.container"]["check_status"] == "skipped"


def test_digest_scope_change_resets_baseline(config):
    config["sources"]["images"][0]["image"] = "docker.io/example/app:latest"
    state = {"digests": {"docker.io/example/app:latest": {"current": "old", "scope": "manifest"}}}
    service = scan(config, state, FakeRegistry())["services"][0]
    assert service["digest"]["baseline_established"] and not service["events"]


def test_scan_does_not_mutate_config(config):
    before = deepcopy(config)
    scan(config, {}, FakeRegistry())
    assert config == before
