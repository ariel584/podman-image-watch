# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from podman_image_watch.quadlet import discover, effective, parse_reference


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_real_sanitized_examples(config):
    config["sources"] = {
        "quadlet_dirs": [str(Path(__file__).parent / "fixtures/quadlets")],
        "images": [],
        "allow_short_names": False,
    }
    services, diagnostics = discover(config)
    assert len(services) == 12 and not diagnostics
    images = {s.quadlet: s.image for s in services}
    assert images["postgresql.container"] == "docker.io/library/postgres:18.6-bookworm"
    assert images["authentik-server.container"] == images["authentik-worker.container"]
    assert len({s.id for s in services}) == 12
    assert (
        next(s for s in services if s.quadlet == "dnsmasq-acme.container").display_name
        == "DNSmasq ACME"
    )


def test_sections_repeated_continuation(tmp_path):
    path = write(
        tmp_path,
        "app.container",
        """# comment
[Container]
Image=docker.io/test/old:1
Environment=Image=ignored
[Service]
Image=irrelevant
[Container]
Image=
Image=docker.io/test/\\
# comment
app:1.2.3
""",
    )
    # systemd continuation inserts a space, so the reference must remain invalid.
    assert effective(path, tmp_path)["Container", "Image"] == "docker.io/test/ app:1.2.3"


def test_dropin_specificity_and_lexical_order(tmp_path):
    path = write(tmp_path, "foo-bar.container", "[Container]\nImage=base\n")
    write(tmp_path, "container.d/10-image.conf", "[Container]\nImage=global\n")
    write(tmp_path, "foo-.container.d/10-image.conf", "[Container]\nImage=prefix\n")
    write(tmp_path, "foo-bar.container.d/10-image.conf", "[Container]\nImage=specific\n")
    assert effective(path, tmp_path)["Container", "Image"] == "specific"
    write(tmp_path, "container.d/99-image.conf", "[Container]\nImage=late\n")
    assert effective(path, tmp_path)["Container", "Image"] == "late"


def test_image_resolution_recursive_and_stable_id(config, tmp_path):
    image = write(tmp_path, "nested/base.image", "[Image]\nImage=quay.io/example/app:1.2.3\n")
    write(tmp_path, "nested/app.container", "[Container]\nImage=base.image\n")
    write(tmp_path, "nested/worker.container", "[Container]\nImage=base.image\n")
    config["sources"].update(quadlet_dirs=[{"id": "prod", "path": str(tmp_path)}], images=[])
    services, diagnostics = discover(config)
    assert len(services) == 2 and not diagnostics
    ids = [s.id for s in services]
    image.write_text("[Image]\nImage=quay.io/example/app:2.0.0\n")
    assert [s.id for s in discover(config)[0]] == ids


def test_template_instance_dropin(config, tmp_path):
    write(tmp_path, "app@.container", "[Container]\nImage=quay.io/example/app:%i\n")
    write(tmp_path, "app@1.2.3.container.d/10-name.conf", "[Container]\nContainerName=app-%i\n")
    config["sources"].update(quadlet_dirs=[str(tmp_path)], images=[])
    services, _ = discover(config)
    assert {s.image for s in services} == {"quay.io/example/app:%i", "quay.io/example/app:1.2.3"}


def test_safe_symlinks(config, tmp_path):
    original = write(tmp_path, "original.container", "[Container]\nImage=quay.io/test/app:1.0.0\n")
    (tmp_path / "alias.container").symlink_to(original)
    (tmp_path / "escape.container").symlink_to("/etc/passwd")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    config["sources"].update(quadlet_dirs=[str(tmp_path)], images=[])
    services, _ = discover(config)
    assert len(services) == 3
    assert next(s for s in services if s.quadlet == "escape.container").reason
    assert (
        next(s for s in services if s.quadlet == "alias.container").image
        == "quay.io/test/app:1.0.0"
    )


def test_cycle_and_missing_image(config, tmp_path):
    write(tmp_path, "app.container", "[Container]\nImage=loop.image\n")
    write(tmp_path, "loop.image", "[Image]\nImage=loop.image\n")
    write(tmp_path, "empty.container", "[Service]\nImage=quay.io/test/app:1\n")
    config["sources"].update(quadlet_dirs=[str(tmp_path)], images=[])
    assert all(s.reason for s in discover(config)[0])


@pytest.mark.parametrize(
    "value",
    [
        "redis:7",
        "localhost/built:1.0",
        "name.build",
        "quay.io/a/b:%i",
        "https://quay.io/a/b",
        "quay.io/a/../b:1",
        "quay.io/a/b@sha256:bad",
        "quay.io/a/b:bad tag",
    ],
)
def test_unsupported_reference(value):
    with pytest.raises(ValueError):
        parse_reference(value)


def test_reference_normalization():
    assert parse_reference("redis:7.4.1", True).repository == "docker.io/library/redis"
    assert parse_reference("docker.io/nginx").tag == "latest"
    assert parse_reference("registry.example:5000/a/b:1.2.3").registry == "registry.example:5000"
    ref = parse_reference("ghcr.io/a/b:1.2.3@sha256:" + "a" * 64)
    assert ref.tag == "1.2.3" and ref.digest


def test_file_symlink_loop_is_visible(config, tmp_path):
    (tmp_path / "loop.container").symlink_to("loop.container")
    config["sources"].update(quadlet_dirs=[str(tmp_path)], images=[])
    services, _ = discover(config)
    assert len(services) == 1 and services[0].reason


def test_local_id_never_assumed_docker_hub():
    with pytest.raises(ValueError, match="Local-only"):
        parse_reference("sha256:" + "a" * 64, allow_short_names=True)
