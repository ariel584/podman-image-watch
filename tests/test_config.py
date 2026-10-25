# SPDX-License-Identifier: GPL-3.0-or-later
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from podman_image_watch.config import ConfigError, load, policy_for, snoozed, validate


def test_load_precedence(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nsources:\n  quadlet_dirs: [/old]\noutput:\n  path: result.json\n")
    result = load(str(path), {"sources": {"quadlet_dirs": ["/new"]}, "output": {"path": "-"}})
    assert result["sources"]["quadlet_dirs"] == ["/new"]
    assert result["output"]["path"] == "-"
    assert result["output"]["pretty"]


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "version: 2",
        "version: 1\nwat: true",
        "version: 1\nversion: 1",
        "sources: {}",
        "version: 1\nrules: [bad]",
        "version: 1\noutput: []",
    ],
)
def test_invalid_yaml(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load(str(path))


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("registries", "retries", -1),
        ("registries", "timeout", True),
        ("registries", "max_pages", 1.5),
        ("sources", "allow_short_names", "yes"),
        ("output", "pretty", "false"),
        ("notifications", "retries", 1.5),
        ("defaults", "include_prerelease", "no"),
        ("defaults", "version", {"scheme": "unknown"}),
        ("defaults", "updates", {"snooze_until": "tomorrow"}),
        ("defaults", "updates", {"acknowledge": [{"type": "major"}]}),
    ],
)
def test_validation(config, section, key, value):
    config[section][key] = value
    with pytest.raises(ConfigError):
        validate(config)


def test_deterministic_conflicts(config):
    config["rules"] = [
        {
            "match": {"quadlet": "app.container"},
            "display_name": "First",
            "severity": {"major": "attention"},
        },
        {
            "match": {"image": "docker.io/example/*"},
            "display_name": "Image",
            "severity": {"major": "information"},
        },
        {"match": {"quadlet": "app.container", "source": "prod"}, "display_name": "Final"},
    ]
    before = deepcopy(config)
    policy = policy_for(
        config, quadlet="nested/app.container", repository="docker.io/example/app", source="prod"
    )
    assert policy["display_name"] == "Final"
    assert policy["severity"]["major"] == "attention"
    assert config == before


def test_snooze_date():
    assert snoozed("2026-12-01", datetime(2026, 12, 1, 23, 59, tzinfo=UTC))
    assert not snoozed("2026-12-01", datetime(2026, 12, 2, tzinfo=UTC))


def test_defaults_only():
    assert (
        load(overrides={"sources": {"quadlet_dirs": ["/tmp/quadlets"]}})["output"]["path"] is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\ndefaults: {version: {scheme: []}}",
        "version: 1\noutput: {format: []}",
        "version: 1\ndefaults: {severity: {major: []}}",
        "version: 1\ndefaults: {version: {channels: [invalid]}}",
        'version: 1\ndefaults: &settings {version_scheme: auto}\nrules: [{match: {quadlet: "*"}, version: *settings}]',
    ],
)
def test_malformed_types_and_aliases(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load(str(path), {"sources": {"quadlet_dirs": ["/tmp/quadlets"]}})
