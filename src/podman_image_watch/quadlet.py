# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative, offline Quadlet discovery. Never invokes Podman or systemd."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImageReference:
    repository: str
    registry: str
    name: str
    tag: str | None
    digest: str | None


def parse_reference(value: str, allow_short_names: bool = False) -> ImageReference:
    if not value or len(value) > 1024 or any(c in value for c in "%$ \\?#\n\r\t") or "://" in value:
        raise ValueError("Invalid or dynamic OCI image reference")
    if value.startswith("sha256:") or re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Local-only image ID has no remote repository")
    image, sep, digest = value.partition("@")
    if sep and not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        raise ValueError("Only valid sha256 manifest digests are supported")
    slash = image.rfind("/")
    colon = image.rfind(":")
    tag = image[colon + 1 :] if colon > slash else None
    if tag is not None:
        image = image[:colon]
        if not re.fullmatch(r"[\w][\w.-]{0,127}", tag, flags=re.ASCII):
            raise ValueError("Invalid OCI tag")
    first, _, remainder = image.partition("/")
    qualified = bool(remainder) and ("." in first or ":" in first or first == "localhost")
    if (
        first == "localhost"
        or image.endswith((".build", ".image"))
        or image.startswith(("sha256:", "/", "."))
    ):
        raise ValueError("Local-only image or unresolved Quadlet reference")
    if not qualified:
        if not allow_short_names:
            raise ValueError(
                "Ambiguous short image name; qualify its registry or enable allow_short_names"
            )
        first, remainder = "docker.io", image
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?", first):
        raise ValueError("Invalid registry host")
    if not re.fullmatch(
        r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*",
        remainder,
    ):
        raise ValueError("Invalid OCI repository")
    if first in {"index.docker.io", "registry-1.docker.io"}:
        first = "docker.io"
    if first == "docker.io" and "/" not in remainder:
        remainder = "library/" + remainder
    return ImageReference(
        f"{first}/{remainder}", first, remainder, tag if sep else tag or "latest", digest or None
    )


@dataclass
class Service:
    id: str
    source: str
    quadlet: str | None
    source_file: str | None
    display_name: str
    image: str
    reason: str | None = None


def stable_id(source: str, relative: str) -> str:
    return hashlib.sha256(f"{source}\0{relative}".encode()).hexdigest()[:32]


def humanize(value: str) -> str:
    abbreviations = {
        "dns": "DNS",
        "dnsmasq": "DNSmasq",
        "acme": "ACME",
        "wg": "WG",
        "api": "API",
        "http": "HTTP",
        "https": "HTTPS",
        "db": "DB",
        "smtp": "SMTP",
        "postgresql": "PostgreSQL",
        "nginx": "Nginx",
    }
    return " ".join(
        abbreviations.get(s.lower(), s[:1].upper() + s[1:])
        for s in re.split(r"[-_@.]+", value)
        if s
    )


def safe_file(path: Path, root: Path) -> str:
    """Allow internal file symlinks, reject escaping links and special files."""
    try:
        resolved = path.resolve()
    except RuntimeError:
        raise ValueError("Quadlet symlink loop") from None
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Symlink escapes its configured source directory")
    if not path.is_file() or path.stat().st_size > 1048576:
        raise ValueError("Quadlet must be a regular file of at most 1 MiB")
    return path.read_text(encoding="utf-8")


def read_settings(text: str, values: dict[tuple[str, str], str]) -> None:
    section = ""
    pending = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        pending += line
        if len(pending) - len(pending.rstrip("\\")) & 1:
            pending = pending[:-1] + " "
            continue
        line, pending = pending, ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line and section:
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if (section, key) in {
                ("Container", "Image"),
                ("Image", "Image"),
                ("Container", "ContainerName"),
                ("Unit", "Description"),
                ("Install", "DefaultInstance"),
            }:
                if value[:1] in {'"', "'"}:
                    if len(value) < 2 or value[-1] != value[0] or "\\" in value:
                        raise ValueError("Unsupported quoted Quadlet setting")
                    value = value[1:-1]
                values[section, key] = value
    if pending:
        raise ValueError("Unterminated Quadlet continuation")


def effective(path: Path, root: Path, unit_name: str | None = None) -> dict[tuple[str, str], str]:
    name = unit_name or path.name
    stem, ext = name.rsplit(".", 1)
    names = [f"{ext}.d"]
    names += [f"{stem[: i + 1]}.{ext}.d" for i, c in enumerate(stem) if c == "-"]
    if "@" in stem:
        names.append(f"{stem.split('@')[0]}@.{ext}.d")
    names.append(f"{name}.d")
    dropins: dict[str, Path] = {}
    for directory in dict.fromkeys(names):
        folder = path.parent / directory
        if folder.exists() and not folder.resolve().is_relative_to(root.resolve()):
            raise ValueError("Drop-in directory symlink escapes source")
        for dropin in sorted(folder.glob("*.conf")):
            dropins[dropin.name] = dropin
    values: dict[tuple[str, str], str] = {}
    read_settings(safe_file(path, root), values)
    for _, dropin in sorted(dropins.items()):
        if dropin.is_symlink() and dropin.resolve() == Path("/dev/null"):
            continue
        read_settings(safe_file(dropin, root), values)
    return values


def expand_static(value: str, unit_name: str) -> str:
    stem = unit_name.rsplit(".", 1)[0]
    instance = stem.split("@", 1)[1] if "@" in stem else ""
    # Escaped systemd instance names need systemd unescaping; preserve as unsupported.
    if "\\" in instance:
        return value
    token = "\x00"
    value = value.replace("%%", token)
    if instance:
        value = value.replace("%i", instance)
    return value.replace(token, "%")


def discover(config: dict) -> tuple[list[Service], list[str]]:
    services: list[Service] = []
    diagnostics: list[str] = []
    for source in config["sources"]["quadlet_dirs"]:
        root = Path(source if isinstance(source, str) else source["path"]).expanduser().absolute()
        source_id = str(root) if isinstance(source, str) else source["id"]
        if not root.is_dir():
            diagnostics.append(f"Source {source_id}: directory does not exist or is unreadable")
            continue
        paths: dict[str, Path] = {}

        def walk_error(error: OSError, source_id: str = source_id) -> None:
            diagnostics.append(
                f"Source {source_id}: cannot traverse directory ({error.__class__.__name__})"
            )

        for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(
                d for d in dirs if not d.endswith(".d") and not Path(folder, d).is_symlink()
            )
            for filename in sorted(files):
                if filename.endswith((".container", ".image")):
                    path = Path(folder, filename)
                    paths[str(path.relative_to(root))] = path
        # Instantiate templates from instance drop-in directories, including empty ones.
        for _relative, path in list(paths.items()):
            if "@." not in path.name:
                continue
            prefix, ext = path.name.split("@.", 1)
            for instance_folder in sorted(path.parent.glob(f"{prefix}@*.{ext}.d")):
                instance_name = instance_folder.name[:-2]
                if instance_name == path.name:
                    continue
                paths.setdefault(str(path.with_name(instance_name).relative_to(root)), path)
        resolved_images: set[str] = set()

        def resolve(
            path: Path,
            name: str,
            seen: set[str],
            root: Path = root,
            resolved_images: set[str] = resolved_images,
        ) -> tuple[str, dict]:
            key = str(path.relative_to(root))
            if key in seen:
                raise ValueError("Cyclic .image reference")
            if len(seen) >= 32:
                raise ValueError("Quadlet image reference depth exceeds 32")
            values = effective(path, root, name)
            section = "Container" if name.endswith(".container") else "Image"
            image = expand_static(values.get((section, "Image"), ""), name)
            if image.endswith(".image"):
                target = path.parent / image
                if not target.exists():
                    target = root / image
                image, _ = resolve(target, target.name, seen | {key})
                resolved_images.add(str(target.relative_to(root)))
            return image, values

        # Containers first so referenced .image definitions do not become duplicate services.
        for relative, path in sorted(
            paths.items(), key=lambda p: (not p[0].endswith(".container"), p[0])
        ):
            if relative in resolved_images:
                continue
            name = Path(relative).name
            reason = None
            image = ""
            values: dict = {}
            try:
                image, values = resolve(path, name, set())
                if not image:
                    reason = "No effective Image= setting in the relevant section"
            except (OSError, ValueError, UnicodeError) as exc:
                reason = (
                    f"Cannot resolve Quadlet ({exc.__class__.__name__}): {exc}"
                    if isinstance(exc, ValueError)
                    else "Cannot read Quadlet or drop-in"
                )
            container_name = expand_static(values.get(("Container", "ContainerName"), ""), name)
            description = values.get(("Unit", "Description"), "")
            display = (
                humanize(container_name)
                if container_name and not any(c in container_name for c in "%$")
                else description
                if description and "%" not in description
                else humanize(name.rsplit(".", 1)[0])
            )
            services.append(
                Service(
                    stable_id(source_id, relative),
                    source_id,
                    relative,
                    str(path),
                    display,
                    image,
                    reason,
                )
            )
    for item in config["sources"]["images"]:
        services.append(
            Service(
                stable_id("explicit", item["id"]),
                "explicit",
                None,
                None,
                item.get("display_name") or humanize(item["id"]),
                item["image"],
            )
        )
    return sorted(services, key=lambda s: (s.source, s.quadlet or s.id)), diagnostics
