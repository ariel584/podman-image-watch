# SPDX-License-Identifier: GPL-3.0-or-later
from copy import deepcopy

import httpx
import pytest

from podman_image_watch.checker import scan
from podman_image_watch.cli import main
from podman_image_watch.quadlet import parse_reference
from podman_image_watch.registry import RegistryClient, RegistryError
from podman_image_watch.report import render
from podman_image_watch.versions import display_tag

DIGEST = "sha256:" + "a" * 64


class AliasRegistry:
    def __init__(self, matching=None):
        self.matching = set(matching if matching is not None else ["1.28.3-alpine"])
        self.calls = []
        self.tag_list = [
            "1.28.3-alpine",
            "1.29.1-alpine",
            "1.29.1",
            "1.28.3",
            "1.28-alpine",
            "stable-alpine",
        ]

    def tags(self, ref):
        return self.tag_list

    def manifest(self, ref, **kwargs):
        self.calls.append(ref.tag)
        same = ref.tag == "stable-alpine" or ref.tag in self.matching
        return {
            "current": DIGEST if same else "sha256:" + "b" * 64,
            "scope": "index",
            "platforms": ["linux/amd64"],
        }


@pytest.fixture
def floating_config(config):
    config["sources"]["images"][0]["image"] = "docker.io/library/nginx:stable-alpine"
    config["defaults"]["updates"]["resolve_floating"] = True
    return config


def test_exact_digest_alias_not_newest_tag(floating_config):
    registry = AliasRegistry()
    service = scan(floating_config, {}, registry)["services"][0]
    assert service["current_version"] == "stable-alpine"
    assert service["resolved_version"] == "1.28.3"
    assert service["resolved_tag"] == "1.28.3-alpine"
    assert service["resolution"]["source"] == "matching_manifest_digest"
    assert not any(service["updates"].values())
    assert "1.28.3-alpine [stable-alpine]" in render({"services": [service]}, "table")
    assert "1.28-alpine" not in registry.calls  # Partial tags do not establish a version.


def test_resolution_opt_in(config):
    config["sources"]["images"][0]["image"] = "docker.io/library/nginx:stable-alpine"
    registry = AliasRegistry()
    service = scan(config, {}, registry)["services"][0]
    assert service["resolved_version"] is None and service["resolution"] is None
    assert registry.calls == ["stable-alpine"]


def test_no_matching_digest_does_not_guess(floating_config):
    service = scan(floating_config, {}, AliasRegistry([]))["services"][0]
    assert service["resolution"]["status"] == "unknown"
    assert service["resolved_version"] is None
    assert service["check_status"] == "ok"  # Digest monitoring succeeded independently.


def test_conflicting_version_aliases_are_ambiguous(floating_config):
    service = scan(floating_config, {}, AliasRegistry(["1.28.3-alpine", "1.29.1-alpine"]))[
        "services"
    ][0]
    assert service["resolution"]["status"] == "ambiguous" and service["resolved_version"] is None


def test_same_version_multiple_variants(floating_config):
    service = scan(floating_config, {}, AliasRegistry(["1.28.3-alpine", "1.28.3"]))["services"][0]
    assert service["resolved_tag"] == "1.28.3-alpine"
    assert service["resolution"]["matching_tags"] == ["1.28.3", "1.28.3-alpine"]


def test_search_limits_and_patterns(floating_config):
    registry = AliasRegistry()
    floating_config["registries"]["max_resolution_candidates"] = 1
    service = scan(floating_config, {}, registry)["services"][0]
    assert service["resolution"]["checked_tags"] == 1
    assert service["resolved_version"] is None  # First candidate is newer, but a different image.
    floating_config["defaults"]["version"]["tag_patterns"] = ["1.28.*-alpine"]
    service = scan(floating_config, {}, AliasRegistry())["services"][0]
    assert service["resolved_version"] == "1.28.3"


def test_resolution_error_preserves_digest(floating_config):
    class Broken(AliasRegistry):
        def tags(self, ref):
            raise RegistryError("Registry HTTP 403")

    service = scan(floating_config, {}, Broken())["services"][0]
    assert service["check_status"] == "error"
    assert service["digest"]["current"] == DIGEST
    assert service["resolution"]["status"] == "error"
    assert service["resolved_version"] is None


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("ubuntu-7.0.32", "7.0.32"),
        ("18.6-bookworm", "18.6"),
        ("1.2.3-rc1-alpine", "1.2.3rc1"),
        ("stable-alpine", "stable"),
        ("unrecognized-hash", "unrecognized-hash"),
    ],
)
def test_display_version_only(tag, expected):
    assert display_tag(tag, {}, True) == expected
    assert display_tag(tag, {}, False) == tag


def test_display_raw_values_and_variant_policy_unchanged(config):
    config["sources"]["images"][0]["image"] = "docker.io/zabbix/server:ubuntu-7.0.32"
    config["output"]["hide_variants"] = True
    registry = AliasRegistry()
    registry.tag_list = ["ubuntu-7.0.32", "ubuntu-7.0.33", "alpine-7.0.99"]
    before = deepcopy(config)
    service = scan(config, {}, registry)["services"][0]
    assert service["display"]["current_version"] == "7.0.32"
    assert service["display"]["updates"]["patch"] == "7.0.33"
    assert service["updates"]["patch"] == "ubuntu-7.0.33"
    assert service["current_version"] == "ubuntu-7.0.32"
    assert "ubuntu" not in render({"services": [service]}, "table")
    assert config == before


def test_hide_skipped_only_hides_table_rows(config):
    config["rules"] = [{"match": {"image": "docker.io/example/app"}, "ignore": True}]
    report = scan(config, {}, AliasRegistry())
    assert len(report["services"]) == 1
    assert "skipped" in render(report, "table")
    assert "skipped" not in render(report, "table", hide_skipped=True)
    assert '"skipped"' in render(report, "json", hide_skipped=True)


def test_cli_policy_precedence_and_display(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("""version: 1
sources:
  images:
    - id: nginx
      image: docker.io/library/nginx:stable-alpine
rules:
  - match: {image: docker.io/library/nginx}
    updates: {resolve_floating: false}
""")
    monkeypatch.setattr(
        "podman_image_watch.cli.scan", lambda config, state: scan(config, state, AliasRegistry())
    )
    assert (
        main(
            [
                "--config",
                str(config_path),
                "--state-dir",
                str(tmp_path / "state"),
                "--resolve-floating",
                "--hide-variants",
                "--format",
                "table",
            ]
        )
        == 0
    )
    assert "1.28.3 [stable]" in capsys.readouterr().out


def test_manifest_resolution_shared_absolute_deadline(config):
    import time

    registry = RegistryClient(
        config["registries"],
        transport=httpx.MockTransport(lambda _: pytest.fail("Expired request should not run")),
    )
    with pytest.raises(RegistryError, match="deadline"):
        registry.manifest(
            parse_reference("docker.io/library/nginx:1.28.3"), deadline=time.monotonic() - 1
        )
    registry.close()


def test_resolution_http_integration_no_layers_and_index_scope(floating_config):
    import json

    body = {
        "schemaVersion": 2,
        "manifests": [
            {"digest": "sha256:" + "c" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
        ],
    }
    different = {"schemaVersion": 2, "config": {"digest": "sha256:" + "c" * 64}}
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/tags/list"):
            return httpx.Response(
                200, json={"tags": ["stable-alpine", "1.28.3-alpine", "1.29.1-alpine"]}
            )
        assert "/manifests/" in request.url.path
        payload = different if request.url.path.endswith("1.29.1-alpine") else body
        return httpx.Response(200, content=json.dumps(payload).encode())

    registry = RegistryClient(floating_config["registries"], transport=httpx.MockTransport(handler))
    report = scan(floating_config, {}, registry)
    assert report["services"][0]["resolved_version"] == "1.28.3"
    assert len(calls) == 4 and not any("/blobs/" in path for path in calls)
    registry.close()


def test_resolution_public_schema(floating_config):
    import json
    from pathlib import Path

    import jsonschema

    schema = json.loads((Path(__file__).parent.parent / "docs/report-v1.schema.json").read_text())
    for matching in ([], ["1.28.3-alpine"], ["1.28.3-alpine", "1.29.1-alpine"]):
        jsonschema.validate(scan(floating_config, {}, AliasRegistry(matching)), schema)


def test_resolution_prerelease_and_ignore_filters(floating_config):
    registry = AliasRegistry(["1.28.3-alpine", "1.30.0rc1-alpine"])
    registry.tag_list += ["1.30.0rc1-alpine"]
    floating_config["defaults"]["updates"]["ignore_versions"] = ["1.28.3-alpine"]
    service = scan(floating_config, {}, registry)["services"][0]
    assert service["resolved_version"] is None
    assert "1.30.0rc1-alpine" not in registry.calls
    floating_config["defaults"]["include_prerelease"] = True
    service = scan(floating_config, {}, registry)["services"][0]
    assert service["resolved_version"] == "1.30.0rc1"


def test_resolve_without_digest_monitoring(floating_config):
    floating_config["defaults"]["updates"]["check_digest"] = False
    state = {}
    service = scan(floating_config, state, AliasRegistry())["services"][0]
    assert service["resolved_version"] == "1.28.3"
    assert service["update_status"] == "unknown" and not state["digests"]
