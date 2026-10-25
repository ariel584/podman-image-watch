# SPDX-License-Identifier: GPL-3.0-or-later
"""Console entry point: exit 0 success, 1 partial, 2 fatal."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .checker import scan
from .config import ConfigError, load
from .notifications import notify
from .report import emit
from .state import State

LOG = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Check OCI images configured in Quadlets without running Podman."
    )
    result.add_argument(
        "command",
        nargs="?",
        choices=["validate-config"],
        help="Validate YAML without querying registries",
    )
    result.add_argument("--version", action="version", version=f"podman-image-watch {__version__}")
    result.add_argument("--config", help="Versioned YAML configuration")
    result.add_argument(
        "--quadlet-dir",
        action="append",
        help="Source directory (repeatable; replaces YAML directories)",
    )
    result.add_argument("--output", help="Atomic report path, or - for stdout")
    result.add_argument("--format", choices=["json", "table"], help="Output format (default: json)")
    result.add_argument("--state-dir", help="State, cache and process lock directory")
    result.add_argument(
        "--hide-variants",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Hide distribution prefixes/suffixes in display versions",
    )
    result.add_argument(
        "--hide-skipped",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Hide ignored services in table output (JSON retains them)",
    )
    result.add_argument(
        "--resolve-floating",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Match floating tags to numbered tags by exact manifest digest",
    )
    result.add_argument(
        "--no-notify", action="store_true", help="Disable notifications for this run"
    )
    result.add_argument("--verbose", action="store_true", help="Additional diagnostics on stderr")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # HTTP libraries log full URLs; keep credential-bearing URLs out of verbose logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    overrides: dict = {}
    if args.quadlet_dir is not None:
        overrides["sources"] = {"quadlet_dirs": args.quadlet_dir}
    if args.output is not None or args.format is not None:
        overrides["output"] = {}
        if args.output is not None:
            overrides["output"]["path"] = args.output
        if args.format is not None:
            overrides["output"]["format"] = args.format
    if args.state_dir:
        overrides["state"] = {"directory": args.state_dir}
    for key in ("hide_variants", "hide_skipped"):
        if getattr(args, key) is not None:
            overrides.setdefault("output", {})[key] = getattr(args, key)
    try:
        if args.command == "validate-config" and not args.config:
            raise ConfigError("validate-config requires --config")
        config = load(args.config, overrides)
        if args.resolve_floating is not None:
            config["_cli_policy"] = {"updates": {"resolve_floating": args.resolve_floating}}
        if args.command == "validate-config":
            print("Configuration is valid", file=sys.stderr)
            return 0
        # Do not overwrite private state or the active lock with a public report.
        output = config["output"]["path"]
        state_dir = Path(config["state"]["directory"]).expanduser().resolve()
        if output not in (None, "-"):
            target = Path(output).expanduser().resolve()
            if args.config and target == Path(args.config).expanduser().resolve():
                raise ConfigError("Output path must not overwrite the YAML configuration")
            for source in config["sources"]["quadlet_dirs"]:
                root = (
                    Path(source if isinstance(source, str) else source["path"])
                    .expanduser()
                    .resolve()
                )
                if target.is_relative_to(root):
                    raise ConfigError("Output path must be outside scanned Quadlet directories")
        if output not in (None, "-") and Path(output).expanduser().resolve() in {
            state_dir / "state.json",
            state_dir / "scan.lock",
        }:
            raise ConfigError("Output path must differ from private state and lock files")
        with State(config["state"]["directory"]) as state:
            report = scan(config, state.data)
            # Make scan results durable before attempting external notification delivery.
            emit(report, config["output"]) if output not in (None, "-") else None
            notify(report, state.data, config["notifications"], disabled=args.no_notify)
            if report["notifications"]["errors"]:
                report["scan"]["status"] = "partial"
                report["scan"]["health"] = 1
                for error in report["notifications"]["errors"]:
                    LOG.warning("%s", error)
            state.save()
            emit(report, config["output"])
        return 0 if report["scan"]["status"] == "success" else 1
    except (ConfigError, OSError, RuntimeError, UnicodeError) as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("Scan interrupted")
        return 2
