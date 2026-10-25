# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate the Zabbix 7.0 export using stable UUIDs. Development use only."""

import json
from pathlib import Path
from uuid import RFC_4122, UUID, uuid4

import yaml

TEMPLATE = "Podman Image Watch by Zabbix agent"
MASTER = 'vfs.file.contents["{$PIW.REPORT_PATH}"]'
UUIDS_PATH = Path(__file__).with_name("zabbix_uuids.json")
UUIDS = json.loads(UUIDS_PATH.read_text(encoding="utf-8"))["uuids"] if UUIDS_PATH.exists() else {}

for key, value in UUIDS.items():
    parsed = UUID(hex=value)
    if parsed.version != 4 or parsed.variant != RFC_4122 or value != parsed.hex:
        raise ValueError(f"Invalid Zabbix UUIDv4 registry entry: {key}")
if len(set(UUIDS.values())) != len(UUIDS):
    raise ValueError("Duplicate entries in the Zabbix UUID registry")


def uid(key):
    if key not in UUIDS:
        UUIDS[key] = uuid4().hex
    return UUIDS[key]


def dependent(key, name, path, master=MASTER, value_type="UNSIGNED"):
    item = {
        "uuid": uid(key),
        "name": name,
        "type": "DEPENDENT",
        "key": key,
        "delay": "0",
        "history": "7d",
        "value_type": value_type,
        "preprocessing": [{"type": "JSONPATH", "parameters": [path]}],
        "master_item": {"key": master},
    }
    if value_type in {"CHAR", "TEXT"}:
        item["trends"] = "0"
        item["preprocessing"].append(
            {"type": "JAVASCRIPT", "parameters": ["return value === 'null' ? '-' : value;"]}
        )
    return item


def trigger(key, name, expression, priority):
    return {
        "uuid": uid("trigger:" + key),
        "name": name,
        "expression": expression,
        "priority": priority,
        "tags": [{"tag": "component", "value": "image-watch"}],
    }


def generate():
    template = {
        "uuid": uid("template"),
        "template": TEMPLATE,
        "name": TEMPLATE,
        "description": "Read-only Quadlet image updates from an atomic JSON report. GPL-3.0-or-later.",
        "groups": [{"name": "Templates/Applications"}],
        "macros": [
            {"macro": "{$PIW.REPORT_PATH}", "value": "/var/lib/podman-image-watch/report.json"},
            {
                "macro": "{$PIW.MAX_AGE}",
                "value": "172800",
                "description": "Maximum report age in seconds.",
            },
        ],
        "items": [
            {
                "uuid": uid("master"),
                "name": "Image Watch: JSON report",
                "key": MASTER,
                "delay": "5m",
                "history": "0",
                "value_type": "TEXT",
                "trends": "0",
            }
        ],
    }
    aggregates = [
        ("timestamp", "Last scan timestamp", "$.scan.timestamp"),
        ("last_success", "Last successful scan timestamp", "$.scan.last_success_timestamp"),
        ("health", "Scan health", "$.scan.health"),
        ("total", "Total services", "$.summary.total_services"),
        ("attention", "Services requiring attention", "$.summary.attention_services"),
        ("planning", "Services requiring planning", "$.summary.planning_services"),
        ("major", "Services with major updates", "$.summary.major_updates"),
        ("errors", "Scan errors", "$.summary.errors"),
        ("unknown", "Unknown results", "$.summary.unknown"),
    ]
    for key, label, path in aggregates:
        item = dependent("piw." + key, "Image Watch: " + label, path)
        if key in {"timestamp", "last_success"}:
            item["units"] = "unixtime"
        template["items"].append(item)
    discovery = {
        "uuid": uid("discovery"),
        "name": "Image Watch: service discovery",
        "type": "DEPENDENT",
        "key": "piw.discovery",
        "delay": "0",
        "lifetime": "30d",
        "preprocessing": [{"type": "JSONPATH", "parameters": ["$.services"]}],
        "master_item": {"key": MASTER},
        "lld_macro_paths": [
            {"lld_macro": "{#PIW.ID}", "path": "$.id"},
            {"lld_macro": "{#PIW.NAME}", "path": "$.display_name"},
        ],
        "item_prototypes": [],
        "trigger_prototypes": [],
    }
    service_key = 'piw.service["{#PIW.ID}"]'
    raw = dependent(
        service_key,
        "{#PIW.NAME}: service data",
        '$.services[?(@.id == "{#PIW.ID}")].first()',
        value_type="TEXT",
    )
    raw["history"] = "0"
    discovery["item_prototypes"].append(raw)
    text_fields = [
        ("current", "Current version", "$.current_version"),
        ("digest", "Configured digest", "$.configured_digest"),
        ("patch", "Latest patch", "$.updates.patch"),
        ("minor", "Latest minor", "$.updates.minor"),
        ("major", "Latest major", "$.updates.major"),
        ("update_status", "Update availability", "$.update_status"),
        ("priority", "Operational priority", "$.priority"),
        ("check_status", "Registry check status", "$.check_status"),
    ]
    for key, label, path in text_fields:
        discovery["item_prototypes"].append(
            dependent(
                f'piw.service.{key}["{{#PIW.ID}}"]',
                "{#PIW.NAME}: " + label,
                path,
                service_key,
                "CHAR",
            )
        )
    for key in ("acknowledged", "snoozed"):
        item = dependent(
            f'piw.service.{key}["{{#PIW.ID}}"]',
            "{#PIW.NAME}: " + key.capitalize(),
            "$." + key,
            service_key,
        )
        item["preprocessing"].append({"type": "BOOL_TO_DECIMAL", "parameters": [""]})
        discovery["item_prototypes"].append(item)
    for key, label in (
        ("attention", "Maintenance attention"),
        ("planning", "Major update planning"),
        ("errors", "Registry errors"),
        ("unknown", "Unsupported or unknown"),
        ("digest_changed", "Digest changed"),
    ):
        # Digest trigger uses a dedicated suppression-aware count; raw event remains visible.
        path = "$.metrics.digest_attention" if key == "digest_changed" else "$.metrics." + key
        item = dependent(
            f'piw.service.{key}["{{#PIW.ID}}"]', "{#PIW.NAME}: " + label, path, service_key
        )
        discovery["item_prototypes"].append(item)
        if key == "errors":
            expression = f'min(/{TEMPLATE}/piw.service.{key}["{{#PIW.ID}}"],#3)=1'
        else:
            expression = f'last(/{TEMPLATE}/piw.service.{key}["{{#PIW.ID}}"])=1'
        discovery["trigger_prototypes"].append(
            trigger(
                "service:" + key,
                "{#PIW.NAME}: " + label,
                expression,
                "WARNING" if key in {"attention", "errors"} else "INFO",
            )
        )
    template["discovery_rules"] = [discovery]
    return {
        "zabbix_export": {
            "version": "7.0",
            "template_groups": [{"uuid": uid("group"), "name": "Templates/Applications"}],
            "templates": [template],
            "triggers": [
                trigger(
                    "stale",
                    "Image Watch: report is stale",
                    f"now()-last(/{TEMPLATE}/piw.timestamp)>{{$PIW.MAX_AGE}}",
                    "WARNING",
                ),
                trigger(
                    "unreadable",
                    "Image Watch: report cannot be read",
                    f"nodata(/{TEMPLATE}/{MASTER},15m)=1",
                    "WARNING",
                ),
                trigger(
                    "failed",
                    "Image Watch: scans repeatedly fail",
                    f"min(/{TEMPLATE}/piw.health,#3)=1",
                    "WARNING",
                ),
            ],
        }
    }


if __name__ == "__main__":
    target = (
        Path(__file__).resolve().parents[1] / "examples/zabbix/template_podman_image_watch.yaml"
    )
    export = generate()
    UUIDS_PATH.write_text(
        json.dumps(
            {"$comment": "SPDX-License-Identifier: GPL-3.0-or-later", "uuids": UUIDS},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    target.write_text(
        "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        + yaml.safe_dump(export, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
