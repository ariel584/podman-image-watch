# SPDX-License-Identifier: GPL-3.0-or-later
"""Public report output. Stdout is reserved for the selected format."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .state import atomic_write


def render(
    report: dict, format: str = "json", pretty: bool = True, *, hide_skipped: bool = False
) -> str:
    if format == "json":
        content = (
            json.dumps(report, indent=2 if pretty else None, ensure_ascii=False, sort_keys=True)
            + "\n"
        )
    else:
        rows = [["SERVICE", "CURRENT", "PATCH", "MINOR", "MAJOR", "STATUS"]]
        for service in report["services"]:
            if hide_skipped and service["check_status"] == "skipped":
                continue
            display = service.get("display", service)
            current = display["current_version"] or service["configured_digest"] or "-"
            if service.get("resolved_tag"):
                current += " [" + display["configured_tag"] + "]"
            rows.append(
                [
                    service["display_name"],
                    current,
                    *[display["updates"][k] or "-" for k in ("patch", "minor", "major")],
                    service["check_status"],
                ]
            )
        # Strip control characters from untrusted registry/Quadlet presentation fields.
        rows = [
            ["".join(c if c.isprintable() else " " for c in str(v)) for v in row] for row in rows
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(6)]
        content = (
            "\n".join(
                "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip()
                for row in rows
            )
            + "\n"
        )
    if len(content.encode("utf-8")) > 16777216:
        raise RuntimeError("Report exceeds 16 MiB size limit")
    return content


def emit(report: dict, output: dict) -> None:
    content = render(
        report, output["format"], output["pretty"], hide_skipped=output.get("hide_skipped", False)
    )
    if output["path"] in (None, "-"):
        sys.stdout.write(content)
    else:
        atomic_write(Path(output["path"]).expanduser(), content, mode=0o640)
