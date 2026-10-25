# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from podman_image_watch.versions import compare


def test_independent_categories(config):
    scheme, updates, reason = compare(
        "13.2.4", ["13.2.5", "13.3.1", "14.0.2", "14.0.3-rc1", "13.2.5-alpine"], config["defaults"]
    )
    assert scheme == "semver" and reason is None
    assert updates == {"patch": "13.2.5", "minor": "13.3.1", "major": "14.0.2"}


@pytest.mark.parametrize(
    "current,tags,scheme,expected",
    [
        (
            "18.6-bookworm",
            ["18.7-bookworm", "19.0-bookworm", "19.0-alpine"],
            "postgres",
            {"patch": "18.7-bookworm", "minor": None, "major": "19.0-bookworm"},
        ),
        (
            "9.6.20",
            ["9.6.24", "10.1", "9.7.1"],
            "postgres",
            {"patch": "9.6.24", "minor": None, "major": "10.1"},
        ),
        (
            "ubuntu-7.0.32",
            ["ubuntu-7.0.33", "ubuntu-7.2.1", "ubuntu-8.0.1", "alpine-8.0.2"],
            "zabbix",
            {"patch": "ubuntu-7.0.33", "minor": "ubuntu-7.2.1", "major": "ubuntu-8.0.1"},
        ),
        (
            "2026.8.5",
            ["2026.8.6", "2026.9.1", "2027.1.0"],
            "calver",
            {"patch": "2026.8.6", "minor": "2027.1.0", "major": None},
        ),
        (
            "v0.107.79",
            ["v0.107.80", "v0.108.0", "1.0.0"],
            "semver",
            {"patch": "v0.107.80", "minor": "v0.108.0", "major": None},
        ),
        (
            "18.6",
            ["18.7", "19.1"],
            "maintenance",
            {"patch": "18.7", "minor": None, "major": "19.1"},
        ),
    ],
)
def test_profiles(config, current, tags, scheme, expected):
    config["defaults"]["version"]["scheme"] = scheme
    assert compare(current, tags, config["defaults"])[1] == expected


@pytest.mark.parametrize("tag", ["hash-abc123", "7", "7.4", "vendor-1.2.3", "RELEASE.2026-01-01"])
def test_conservative_auto(config, tag):
    assert compare(tag, [], config["defaults"])[0] == "unsupported"


@pytest.mark.parametrize("tag", ["latest", "stable", "stable-alpine", "nightly"])
def test_floating(config, tag):
    assert compare(tag, ["999.0.0"], config["defaults"])[0] == "floating"


def test_filtering_and_custom(config):
    policy = config["defaults"]
    policy["version"].update(
        scheme="custom",
        prefix="release-",
        suffix="-linux",
        allowed_majors=[1],
        tag_patterns=["release-*-linux"],
    )
    policy["updates"]["ignore_versions"] = ["release-1.2.5-linux"]
    _, candidates, _ = compare(
        "release-1.2.3-linux",
        [
            "release-1.2.4-linux",
            "release-1.2.5-linux",
            "release-2.0.0-linux",
            "release-1.3.0-rc1-linux",
        ],
        policy,
    )
    assert candidates == {"patch": "release-1.2.4-linux", "minor": None, "major": None}
    policy["version"]["include_prerelease"] = True
    assert compare("release-1.2.3-linux", ["release-1.3.0-rc1-linux"], policy)[1]["minor"]


def test_explicit_variant_migration(config):
    config["defaults"]["version"]["preserve_variant"] = False
    assert (
        compare("1.2.3-bookworm", ["1.2.4-alpine"], config["defaults"])[1]["patch"]
        == "1.2.4-alpine"
    )


def test_explicit_prefix_variant_migration(config):
    config["defaults"]["version"]["preserve_variant"] = False
    assert (
        compare("ubuntu-7.0.32", ["alpine-7.0.33"], config["defaults"])[1]["patch"]
        == "alpine-7.0.33"
    )
