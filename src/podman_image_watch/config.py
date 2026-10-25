# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned configuration, strict validation and deterministic rule resolution."""

from __future__ import annotations

import copy
import fnmatch
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


DEFAULTS: dict[str, Any] = {
    "version": 1,
    "sources": {"quadlet_dirs": [], "images": [], "allow_short_names": False},
    "output": {
        "format": "json",
        "path": None,
        "pretty": True,
        "hide_variants": False,
        "hide_skipped": False,
    },
    "registries": {
        "timeout": 10,
        "retries": 3,
        "cache_ttl": "12h",
        "max_parallel_requests": 4,
        "max_pages": 100,
        "max_tags": 100000,
        "max_response_bytes": 8388608,
        "deadline": 120,
        "max_resolution_candidates": 20,
        "credentials": {},
    },
    "defaults": {
        "version_scheme": "auto",
        "include_prerelease": False,
        "version": {"preserve_variant": True},
        "updates": {
            "check_patch": True,
            "check_minor": True,
            "check_major": True,
            "check_digest": True,
            "monitor_immutable": False,
            "resolve_floating": False,
            "ignore_versions": [],
            "acknowledge": [],
        },
        "severity": {
            "patch": "attention",
            "minor": "attention",
            "major": "planning",
            "digest_changed": "information",
        },
    },
    "state": {"directory": "~/.local/state/podman-image-watch"},
    "rules": [],
    "notifications": {
        "enabled": False,
        "on_change_only": True,
        "timeout": 10,
        "retries": 2,
        "categories": [],
        "priorities": [],
        "services": [],
        "include_errors": True,
        "webhook": {"enabled": False, "url_env": "PIW_WEBHOOK_URL", "token_env": None},
        "smtp": {
            "enabled": False,
            "host": "",
            "port": 587,
            "starttls": True,
            "username_env": None,
            "password_env": None,
            "from": "",
            "to": [],
        },
    },
}
VERSION_KEYS = {
    "scheme",
    "prefix",
    "suffix",
    "preserve_variant",
    "include_prerelease",
    "allowed_majors",
    "tag_patterns",
    "channels",
    "major_components",
    "minor_components",
}
UPDATE_KEYS = set(DEFAULTS["defaults"]["updates"]) | {"snooze_until", "reason"}
POLICY_KEYS = {
    "version_scheme",
    "include_prerelease",
    "version",
    "updates",
    "severity",
    "display_name",
    "ignore",
    "notify",
    "labels",
}
PRIORITIES = {"information", "planning", "attention", "warning"}


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def mapping(value: Any, allowed: set[str], path: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ConfigError(f"{path} must be a mapping with string keys")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    return value


def strings(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{path} must be a list of strings")


def duration(value: Any) -> float:
    if isinstance(value, (float, int)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    if isinstance(value, str) and (match := re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d)", value)):
        return float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    raise ConfigError("registries.cache_ttl must be nonnegative seconds or a duration like 12h")


def validate_policy(policy: Any, path: str) -> None:
    mapping(policy, POLICY_KEYS, path)
    for key in ("ignore", "notify", "include_prerelease"):
        if key in policy and type(policy[key]) is not bool:
            raise ConfigError(f"{path}.{key} must be boolean")
    if "display_name" in policy and (
        not isinstance(policy["display_name"], str) or not policy["display_name"]
    ):
        raise ConfigError(f"{path}.display_name must be a nonempty string")
    if "labels" in policy:
        mapping(
            policy["labels"],
            set(policy["labels"]) if isinstance(policy["labels"], dict) else set(),
            f"{path}.labels",
        )
        if any(not isinstance(v, str) for v in policy["labels"].values()):
            raise ConfigError(f"{path}.labels values must be strings")
    version = policy.get("version", {})
    mapping(version, VERSION_KEYS, f"{path}.version")
    scheme = version.get("scheme", policy.get("version_scheme", "auto"))
    if not isinstance(scheme, str) or scheme not in {
        "auto",
        "semver",
        "calver",
        "postgres",
        "zabbix",
        "maintenance",
        "custom",
    }:
        raise ConfigError(f"{path}: unsupported version scheme")
    for key in ("prefix", "suffix"):
        if key in version and not isinstance(version[key], str):
            raise ConfigError(f"{path}.version.{key} must be a string")
    for key in ("preserve_variant", "include_prerelease"):
        if key in version and type(version[key]) is not bool:
            raise ConfigError(f"{path}.version.{key} must be boolean")
    for key in ("tag_patterns", "channels"):
        if key in version:
            strings(version[key], f"{path}.version.{key}")
    if set(version.get("channels", [])) - {"stable", "prerelease"}:
        raise ConfigError(f"{path}.version.channels must contain stable or prerelease")
    if "allowed_majors" in version and (
        not isinstance(version["allowed_majors"], list)
        or any(type(v) is not int or v < 0 for v in version["allowed_majors"])
    ):
        raise ConfigError(f"{path}.version.allowed_majors must be nonnegative integers")
    for key in ("major_components", "minor_components"):
        if key in version and (type(version[key]) is not int or not 0 <= version[key] <= 3):
            raise ConfigError(f"{path}.version.{key} must be an integer from 0 to 3")
    updates = policy.get("updates", {})
    mapping(updates, UPDATE_KEYS, f"{path}.updates")
    for key in (
        "check_patch",
        "check_minor",
        "check_major",
        "check_digest",
        "monitor_immutable",
        "resolve_floating",
    ):
        if key in updates and type(updates[key]) is not bool:
            raise ConfigError(f"{path}.updates.{key} must be boolean")
    if "ignore_versions" in updates:
        strings(updates["ignore_versions"], f"{path}.updates.ignore_versions")
    if "reason" in updates and not isinstance(updates["reason"], str):
        raise ConfigError(f"{path}.updates.reason must be a string")
    if "snooze_until" in updates:
        try:
            date.fromisoformat(str(updates["snooze_until"]))
        except ValueError as exc:
            raise ConfigError(f"{path}.updates.snooze_until must be YYYY-MM-DD") from exc
    acknowledgements = updates.get("acknowledge", [])
    if not isinstance(acknowledgements, list):
        raise ConfigError(f"{path}.updates.acknowledge must be a list")
    for ack in acknowledgements:
        mapping(ack, {"type", "version", "reason"}, f"{path}.updates.acknowledge")
        if (
            not isinstance(ack.get("type"), str)
            or ack["type"] not in {"patch", "minor", "major", "digest_changed"}
            or not isinstance(ack.get("version"), str)
            or not isinstance(ack.get("reason", ""), str)
        ):
            raise ConfigError(f"{path}: acknowledgement requires type and exact version/digest")
    severities = policy.get("severity", {})
    mapping(severities, {"patch", "minor", "major", "digest_changed"}, f"{path}.severity")
    if any(not isinstance(v, str) or v not in PRIORITIES for v in severities.values()):
        raise ConfigError(f"{path}.severity: expected information, planning, attention or warning")


def validate(config: dict) -> None:
    mapping(config, set(DEFAULTS), "config")
    if type(config["version"]) is not int or config["version"] != 1:
        raise ConfigError("version must be 1")
    for section in ("sources", "output", "registries", "state", "notifications"):
        mapping(config[section], set(DEFAULTS[section]), section)
    sources = config["sources"]
    if not isinstance(sources["quadlet_dirs"], list):
        raise ConfigError("sources.quadlet_dirs must be a list")
    ids = set()
    for root in sources["quadlet_dirs"]:
        if isinstance(root, str):
            if not root:
                raise ConfigError("Quadlet directory must not be empty")
            identity = str(Path(root).expanduser().absolute())
        else:
            mapping(root, {"id", "path"}, "sources.quadlet_dirs[]")
            if any(not isinstance(root.get(k), str) or not root[k] for k in ("id", "path")):
                raise ConfigError("Quadlet directory mapping requires nonempty id and path")
            identity = root["id"]
        if identity in ids:
            raise ConfigError("Source directory identifiers must be unique")
        ids.add(identity)
    if type(sources["allow_short_names"]) is not bool:
        raise ConfigError("sources.allow_short_names must be boolean")
    if not isinstance(sources["images"], list):
        raise ConfigError("sources.images must be a list")
    image_ids = set()
    for item in sources["images"]:
        mapping(item, {"id", "image", "display_name"}, "sources.images[]")
        if any(not isinstance(item.get(k), str) or not item[k] for k in ("id", "image")):
            raise ConfigError("Explicit images require nonempty id and image")
        if "display_name" in item and not isinstance(item["display_name"], str):
            raise ConfigError("Explicit display_name must be a string")
        if item["id"] in image_ids:
            raise ConfigError("Explicit image IDs must be unique")
        image_ids.add(item["id"])
    output = config["output"]
    for key in ("hide_variants", "hide_skipped"):
        if type(output[key]) is not bool:
            raise ConfigError(f"output.{key} must be boolean")
    if (
        not isinstance(output["format"], str)
        or output["format"] not in {"json", "table"}
        or type(output["pretty"]) is not bool
        or (output["path"] is not None and not isinstance(output["path"], str))
    ):
        raise ConfigError("output requires format json/table, boolean pretty and string/null path")
    if not isinstance(config["state"]["directory"], str) or not config["state"]["directory"]:
        raise ConfigError("state.directory must be a nonempty path")
    registry = config["registries"]
    duration(registry["cache_ttl"])
    for key, low, high in (
        ("timeout", 0.1, 120),
        ("deadline", 1, 600),
        ("retries", 0, 5),
        ("max_parallel_requests", 1, 16),
        ("max_pages", 1, 1000),
        ("max_tags", 1, 1000000),
        ("max_response_bytes", 1024, 33554432),
        ("max_resolution_candidates", 1, 100),
    ):
        if (
            type(registry[key]) not in (int, float)
            or not low <= registry[key] <= high
            or (key not in {"timeout", "deadline"} and type(registry[key]) is not int)
        ):
            raise ConfigError(f"registries.{key} must be between {low} and {high}")
    credentials = registry["credentials"]
    if not isinstance(credentials, dict):
        raise ConfigError("registries.credentials must be a mapping")
    for host, credential in credentials.items():
        if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", host):
            raise ConfigError("Credential key must be a registry host")
        mapping(
            credential, {"username_env", "password_env", "token_env", "auth_hosts"}, "credentials"
        )
        for key in ("username_env", "password_env", "token_env"):
            if key in credential and not isinstance(credential[key], str):
                raise ConfigError(f"credentials.{key} must name an environment variable")
        strings(credential.get("auth_hosts", []), "credentials.auth_hosts")
        if any(
            not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", h)
            for h in credential.get("auth_hosts", [])
        ):
            raise ConfigError("credentials.auth_hosts must contain host names only")
    validate_policy(config["defaults"], "defaults")
    if not isinstance(config["rules"], list):
        raise ConfigError("rules must be a list")
    for index, rule in enumerate(config["rules"]):
        mapping(rule, POLICY_KEYS | {"match"}, f"rules[{index}]")
        match = rule.get("match")
        mapping(match, {"quadlet", "image", "source"}, f"rules[{index}].match")
        if not match or any(not isinstance(v, str) or not v for v in match.values()):
            raise ConfigError("Each rule needs at least one nonempty match pattern")
        validate_policy({k: v for k, v in rule.items() if k != "match"}, f"rules[{index}]")
    notifications = config["notifications"]
    for key in ("enabled", "on_change_only", "include_errors"):
        if type(notifications[key]) is not bool:
            raise ConfigError(f"notifications.{key} must be boolean")
    for key in ("categories", "priorities", "services"):
        strings(notifications[key], f"notifications.{key}")
    if (
        set(notifications["categories"]) - {"patch", "minor", "major", "digest_changed"}
        or set(notifications["priorities"]) - PRIORITIES
    ):
        raise ConfigError("Unknown notification category or priority")
    for key, low, high in (("timeout", 0.1, 120), ("retries", 0, 5)):
        if (
            type(notifications[key]) not in (int, float)
            or not low <= notifications[key] <= high
            or (key == "retries" and type(notifications[key]) is not int)
        ):
            raise ConfigError(f"notifications.{key} must be between {low} and {high}")
    for adapter in ("webhook", "smtp"):
        data = notifications[adapter]
        mapping(data, set(DEFAULTS["notifications"][adapter]), f"notifications.{adapter}")
        if type(data["enabled"]) is not bool:
            raise ConfigError(f"notifications.{adapter}.enabled must be boolean")
        for key in set(data) - {"enabled", "starttls", "port", "to"}:
            if data[key] is not None and not isinstance(data[key], str):
                raise ConfigError(f"notifications.{adapter}.{key} must be a string")
    smtp = notifications["smtp"]
    strings(smtp["to"], "notifications.smtp.to")
    if (
        type(smtp["port"]) is not int
        or not 1 <= smtp["port"] <= 65535
        or type(smtp["starttls"]) is not bool
    ):
        raise ConfigError("SMTP requires a valid port and boolean starttls")
    if smtp["enabled"] and (not smtp["host"] or not smtp["from"] or not smtp["to"]):
        raise ConfigError("Enabled SMTP requires host, from and to")
    if smtp["enabled"] and smtp["password_env"] and not smtp["starttls"]:
        raise ConfigError("SMTP credentials require starttls")
    if bool(smtp["username_env"]) != bool(smtp["password_env"]):
        raise ConfigError("SMTP username_env and password_env must be configured together")
    if notifications["webhook"]["enabled"] and not notifications["webhook"]["url_env"]:
        raise ConfigError("Enabled webhook requires url_env")
    if notifications["enabled"] and not any(
        notifications[k]["enabled"] for k in ("webhook", "smtp")
    ):
        raise ConfigError("Enabled notifications require a webhook or SMTP adapter")


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently losing an override."""

    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ConfigError("YAML aliases are not supported; declare settings explicitly")
        self.depth += 1
        try:
            if self.depth > 32:
                raise ConfigError("YAML nesting exceeds 32 levels")
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def _construct_mapping(loader: UniqueLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigError("YAML mapping keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load(path: str | None = None, overrides: dict | None = None) -> dict:
    raw: dict = {}
    if path:
        try:
            content = Path(path).expanduser().read_bytes()
            if len(content) > 1048576:
                raise ConfigError("Configuration exceeds 1 MiB")
            raw = yaml.load(content.decode("utf-8"), Loader=UniqueLoader)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigError(
                "Cannot read configuration as UTF-8 YAML; check path and syntax"
            ) from exc
        mapping(raw, set(DEFAULTS), "config")
        if "version" not in raw:
            raise ConfigError("YAML configuration must declare version: 1")
    result = merge(merge(DEFAULTS, raw), overrides or {})
    validate(result)
    if not result["sources"]["quadlet_dirs"] and not result["sources"]["images"]:
        raise ConfigError(
            "Provide --quadlet-dir or configure sources.quadlet_dirs / sources.images"
        )
    return result


def policy_for(config: dict, *, quadlet: str | None, repository: str, source: str) -> dict:
    policy = copy.deepcopy(config["defaults"])
    matching = []
    for index, rule in enumerate(config["rules"]):
        match = rule["match"]
        values = {"quadlet": quadlet or "", "image": repository, "source": source}
        if all(
            fnmatch.fnmatchcase(values[k], pattern)
            or (k == "quadlet" and fnmatch.fnmatchcase(Path(values[k]).name, pattern))
            for k, pattern in match.items()
        ):
            matching.append(
                (2 if "quadlet" in match else 1 if "image" in match else 0, index, rule)
            )
    for _, _, rule in sorted(matching, key=lambda v: (v[0], v[1])):
        policy = merge(policy, {k: v for k, v in rule.items() if k != "match"})
    return merge(policy, config.get("_cli_policy", {}))


def snoozed(until: Any, now: datetime) -> bool:
    """A date suppresses through that entire UTC date."""
    return until is not None and now.date() <= date.fromisoformat(str(until))
