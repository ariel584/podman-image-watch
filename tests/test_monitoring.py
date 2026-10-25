# SPDX-License-Identifier: GPL-3.0-or-later
import json
import re
from pathlib import Path
from uuid import RFC_4122, UUID

import jsonschema
import pytest
import yaml
from jsonpath_ng.ext import parse
from test_checker import FakeRegistry

from podman_image_watch.checker import scan

ROOT = Path(__file__).parent.parent


def project(path, value):
    first = path.endswith(".first()")
    expression = path.removesuffix(".first()")
    matches = [match.value for match in parse(expression).find(value)]
    assert matches, f"JSONPath has no match: {path}"
    return matches[0] if first or len(matches) == 1 else matches


def test_public_schema_success_partial_skipped_pinned(config):
    schema = json.loads((ROOT / "docs/report-v1.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.check_schema(schema)
    for image in (
        "docker.io/example/app:1.2.3",
        "localhost/local:1.0",
        "docker.io/example/app@sha256:" + "a" * 64,
        "docker.io/example/app:latest",
    ):
        config["sources"]["images"][0]["image"] = image
        validator.validate(scan(config, {}, FakeRegistry()))
    config["rules"] = [{"match": {"image": "docker.io/example/app"}, "ignore": True}]
    validator.validate(scan(config, {}, FakeRegistry()))
    validator.validate(json.loads((ROOT / "tests/fixtures/report.json").read_text()))


def test_template_structure_uuid_dependencies_and_preprocessing():
    export = yaml.safe_load(
        (ROOT / "examples/zabbix/template_podman_image_watch.yaml").read_text()
    )["zabbix_export"]
    report = json.loads((ROOT / "tests/fixtures/report.json").read_text())
    assert export["version"] == "7.0"
    template = export["templates"][0]
    assert {g["name"] for g in export["template_groups"]} == {g["name"] for g in template["groups"]}
    assert template["items"][0]["key"] == 'vfs.file.contents["{$PIW.REPORT_PATH}"]'
    discovery = template["discovery_rules"][0]
    assert discovery["type"] == "DEPENDENT" and discovery["lifetime"] == "30d"
    services = project(discovery["preprocessing"][0]["parameters"][0], report)
    assert len(services) == 1
    macros = {
        entry["lld_macro"]: project(entry["path"], services[0])
        for entry in discovery["lld_macro_paths"]
    }
    assert re.fullmatch("[a-f0-9]{32}", macros["{#PIW.ID}"])
    all_items = template["items"] + discovery["item_prototypes"]
    items = {item["key"]: item for item in all_items}
    assert len(items) == len(all_items)

    def value_for(item):
        if "master_item" not in item:
            return report
        master = value_for(items[item["master_item"]["key"]])
        path = item["preprocessing"][0]["parameters"][0]
        for name, value in macros.items():
            path = path.replace(name, value)
        return project(path, master)

    for item in all_items:
        value_for(item)
    assert (
        value_for(items["piw.total"])
        == value_for(items["piw.attention"])
        == value_for(items["piw.major"])
        == 1
    )
    assert value_for(items['piw.service.patch["{#PIW.ID}"]']) == "1.2.4"
    assert all("{#PIW.NAME}" in item["name"] for item in discovery["item_prototypes"])
    for trigger in export["triggers"] + discovery["trigger_prototypes"]:
        for item in re.findall(
            r"/" + re.escape(template["template"]) + r"/(.+?)\)(?:[=<>]|,|$)", trigger["expression"]
        ):
            assert item.split(",#")[0].split(",15m")[0] in items
    uuids = []

    def collect(value):
        if isinstance(value, dict):
            if "uuid" in value:
                assert re.fullmatch("[a-f0-9]{32}", value["uuid"])
                parsed_uuid = UUID(hex=value["uuid"])
                assert parsed_uuid.version == 4, f"Zabbix requires UUIDv4: {value['uuid']}"
                assert parsed_uuid.variant == RFC_4122
                uuids.append(value["uuid"])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(export)
    assert len(uuids) == len(set(uuids))


def test_template_generator_matches_committed_artifact(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "template_generator", ROOT / "tools/generate_zabbix_template.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Existing entities must reuse their checked-in UUIDv4 values on every regeneration.
    monkeypatch.setattr(module, "uuid4", lambda: pytest.fail("Missing persisted Zabbix UUID"))
    assert module.generate() == yaml.safe_load(
        (ROOT / "examples/zabbix/template_podman_image_watch.yaml").read_text()
    )
