# SPDX-License-Identifier: GPL-3.0-or-later
import json
import stat
from pathlib import Path

import pytest
from test_checker import FakeRegistry

from podman_image_watch.checker import scan
from podman_image_watch.cli import main
from podman_image_watch.report import emit, render
from podman_image_watch.state import State, atomic_write


def test_stdout_and_atomic_file(config, capsys, tmp_path):
    report = scan(config, {}, FakeRegistry())
    emit(report, config["output"])
    assert json.loads(capsys.readouterr().out) == report
    path = tmp_path / "output/report.json"
    config["output"]["path"] = str(path)
    emit(report, config["output"])
    assert not capsys.readouterr().out
    assert json.loads(path.read_text()) == report
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    path.chmod(0o600)
    emit(report, config["output"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_failed_replace_leaves_old_report(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text("old")

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr("podman_image_watch.state.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, "new")
    assert path.read_text() == "old" and list(tmp_path.iterdir()) == [path]


def test_state_lock_persistence_and_damaged_state(tmp_path):
    with State(str(tmp_path)) as state:
        state.data["digests"] = {"test": {"current": "sha256:" + "a" * 64, "scope": "index"}}
        state.save()
        with pytest.raises(RuntimeError, match="Another scan"):
            with State(str(tmp_path)):
                pass
    with State(str(tmp_path)) as state:
        assert state.data["digests"]["test"]["current"] == "sha256:" + "a" * 64
    (tmp_path / "state.json").write_text("{broken")
    with State(str(tmp_path)) as state:
        assert not state.data["digests"]


def test_cli_stdout_table_and_precedence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "podman_image_watch.cli.scan", lambda cfg, state: scan(cfg, state, FakeRegistry())
    )
    quadlets = tmp_path / "quadlets"
    quadlets.mkdir()
    (quadlets / "app.container").write_text("[Container]\nImage=docker.io/example/app:1.2.3\n")
    args = ["--quadlet-dir", str(quadlets), "--state-dir", str(tmp_path / "state")]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["patch_updates"] == 1
    path = tmp_path / "config.yaml"
    path.write_text(
        f"version: 1\nsources:\n  quadlet_dirs: [{quadlets}]\noutput:\n  path: {tmp_path / 'configured.json'}\n"
    )
    assert (
        main(["--config", str(path), "--output", "-", "--state-dir", str(tmp_path / "state")]) == 0
    )
    assert json.loads(capsys.readouterr().out)["services"]
    assert not (tmp_path / "configured.json").exists()
    assert main([*args, "--format", "table"]) == 0
    assert "SERVICE" in capsys.readouterr().out


def test_cli_exit_codes(tmp_path, capsys):
    assert main([]) == 2
    assert not capsys.readouterr().out
    assert (
        main(["--quadlet-dir", str(tmp_path / "missing"), "--state-dir", str(tmp_path / "state")])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["scan"]["status"] == "partial"


def test_validate_config_no_network_or_state(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nsources:\n  quadlet_dirs: [/etc/containers/systemd]\n")
    assert (
        main(["validate-config", "--config", str(path), "--state-dir", str(tmp_path / "state")])
        == 0
    )
    assert not capsys.readouterr().out and not (tmp_path / "state").exists()


def test_no_runtime_access(config, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("Scanner must not execute subprocesses")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert scan(config, {}, FakeRegistry())["scan"]["status"] == "success"


def test_table_control_characters(config):
    report = scan(config, {}, FakeRegistry())
    report["services"][0]["display_name"] = "Danger\x1b[31m"
    assert "\x1b" not in render(report, "table")


def test_all_sources_have_license_identifiers():
    root = Path(__file__).parent.parent
    for folder in ("src", "tests", "examples"):
        for path in (root / folder).rglob("*"):
            if path.is_file() and path.suffix in {
                ".py",
                ".yaml",
                ".container",
                ".service",
                ".timer",
            }:
                assert "SPDX-License-Identifier: GPL-3.0-or-later" in path.read_text(), path


def test_output_cannot_overwrite_inputs(tmp_path):
    quadlets = tmp_path / "quadlets"
    quadlets.mkdir()
    path = quadlets / "app.container"
    original = "[Container]\nImage=docker.io/example/app:1.2.3\n"
    path.write_text(original)
    assert (
        main(
            [
                "--quadlet-dir",
                str(quadlets),
                "--output",
                str(path),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 2
    )
    assert path.read_text() == original
    config = tmp_path / "config.yaml"
    original_config = f"version: 1\nsources:\n  quadlet_dirs: [{quadlets}]\n"
    config.write_text(original_config)
    assert main(["--config", str(config), "--output", str(config)]) == 2
    assert config.read_text() == original_config


def test_state_always_private(tmp_path):
    with State(str(tmp_path)) as state:
        state.save()
    path = tmp_path / "state.json"
    path.chmod(0o644)
    with State(str(tmp_path)) as state:
        state.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_nested_corrupt_state_resets(tmp_path):
    payload = {
        "version": 1,
        "digests": {},
        "cache": {},
        "notifications": {"webhook": {"app": {"updates": []}}},
    }
    (tmp_path / "state.json").write_text(json.dumps(payload))
    with State(str(tmp_path)) as state:
        assert not state.data["notifications"]
