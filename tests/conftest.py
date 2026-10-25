# SPDX-License-Identifier: GPL-3.0-or-later
from copy import deepcopy

import pytest

from podman_image_watch.config import DEFAULTS


@pytest.fixture
def config(tmp_path):
    result = deepcopy(DEFAULTS)
    result["state"]["directory"] = str(tmp_path / "state")
    result["sources"]["images"] = [{"id": "app", "image": "docker.io/example/app:1.2.3"}]
    result["registries"].update(retries=0, cache_ttl=0)
    return result


@pytest.fixture
def synthetic_quadlets(tmp_path):
    """A fictional installation covering image policies without a real host inventory."""
    root = tmp_path / "quadlets"
    root.mkdir()
    images = {
        "frontend.container": "registry.example.test/demo/app:1.2.3",
        "worker.container": "registry.example.test/demo/app:1.2.3",
        "database.container": "registry.example.test/demo/database:18.6-bookworm",
        "calendar.container": "registry.example.test/demo/calendar:2026.8.5",
        "prefixed.container": "registry.example.test/demo/metrics:ubuntu-7.0.32",
        "floating.container": "registry.example.test/demo/gateway:stable-alpine",
        "local.container": "localhost/example-local:1.0",
    }
    for name, image in images.items():
        (root / name).write_text(
            f"[Container]\nContainerName={name.removesuffix('.container')}\nImage={image}\n",
            encoding="utf-8",
        )
    return root, images
