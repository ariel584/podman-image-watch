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
