# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic files and a nonblocking process lock for short-lived scans."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

LOG = logging.getLogger(__name__)


def valid_state(data: object) -> bool:
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        return False
    if any(not isinstance(data.get(k), dict) for k in ("digests", "cache", "notifications")):
        return False
    for baseline in data["digests"].values():
        if not isinstance(baseline, dict) or not isinstance(baseline.get("current"), str):
            return False
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", baseline["current"]) or baseline.get(
            "scope"
        ) not in ("index", "manifest"):
            return False
    for adapter in data["notifications"].values():
        if not isinstance(adapter, dict):
            return False
        for service in adapter.values():
            if not isinstance(service, dict) or not isinstance(service.get("updates", {}), dict):
                return False
            if any(
                k not in {"patch", "minor", "major", "digest_changed"} or not isinstance(v, str)
                for k, v in service.get("updates", {}).items()
            ):
                return False
    return True


def atomic_write(
    path: Path, content: str, mode: int = 0o600, *, preserve_mode: bool = True
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if preserve_mode and path.exists() and not path.is_symlink():
        mode = stat.S_IMODE(path.stat().st_mode) & 0o666
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, directory: str):
        self.directory = Path(directory).expanduser()
        self.data: dict = {
            "version": 1,
            "digests": {},
            "cache": {},
            "notifications": {},
            "last_success_at": None,
        }
        self.lock_fd: int | None = None

    def __enter__(self) -> State:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_fd = os.open(
            self.directory / "scan.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError("Another scan is using this state directory") from None
        path = self.directory / "state.json"
        if path.exists():
            try:
                if path.stat().st_size > 33554432:
                    raise ValueError("State too large")
                data = json.loads(path.read_text(encoding="utf-8"))
                if not valid_state(data):
                    raise ValueError("Incompatible state")
                self.data = data
            except (OSError, ValueError, UnicodeError, RecursionError):
                LOG.warning(
                    "Damaged or incompatible state ignored; digest baselines will be reestablished"
                )
        return self

    def save(self) -> None:
        # Expired registry cache entries and obsolete baselines are pruned by the checker.
        content = json.dumps(self.data, sort_keys=True, ensure_ascii=False)
        if len(content.encode()) > 33554432:
            raise RuntimeError("State exceeds 32 MiB; reduce cache_ttl or max_tags")
        atomic_write(self.directory / "state.json", content, preserve_mode=False)

    def __exit__(self, *args) -> None:
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
