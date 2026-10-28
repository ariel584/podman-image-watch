# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional webhook and STARTTLS SMTP delivery with change-only state."""

from __future__ import annotations

import fnmatch
import logging
import os
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from html import escape

import httpx

from .registry import RegistryError, https_url

LOG = logging.getLogger(__name__)


def snapshot(report: dict, config: dict) -> dict:
    current = {}
    for service in report["services"]:
        if not service["notify"] or (
            config["services"]
            and not any(
                fnmatch.fnmatchcase(service["id"], p)
                or fnmatch.fnmatchcase(service["display_name"], p)
                for p in config["services"]
            )
        ):
            continue
        events = [
            e
            for e in service["events"]
            if not e["notification_suppressed"]
            and (not config["categories"] or e["type"] in config["categories"])
            and (not config["priorities"] or e["priority"] in config["priorities"])
        ]
        failed = config["include_errors"] and service["check_status"] in {
            "error",
            "unknown",
            "unsupported",
        }
        if events or failed:
            current[service["id"]] = {
                "display_name": service["display_name"],
                "image": service["image"],
                "updates": {e["type"]: e["version"] for e in events},
                "failed": bool(failed),
                "reason": service["reason"] if failed else None,
            }
    if config["include_errors"] and report["scan"]["diagnostics"]:
        current["scan"] = {
            "display_name": "Scan",
            "updates": {},
            "failed": True,
            "reason": report["scan"]["diagnostics"],
        }
    return current


def changes(previous: dict, current: dict, report: dict) -> list[dict]:
    events = []
    statuses = {s["id"]: s["check_status"] for s in report["services"]}
    services = {s["id"]: s for s in report["services"]}
    for key in sorted(set(previous) | set(current)):
        old, new = previous.get(key, {}), current.get(key, {})
        # Registry outages cannot establish that an update resolved.
        if old.get("updates") and (
            statuses.get(key) in {"error", "unknown", "unsupported"}
            or (key not in statuses and report["scan"]["status"] != "success")
        ):
            new = dict(new, updates={**old["updates"], **new.get("updates", {})})
            current[key] = new
        for category, version in new.get("updates", {}).items():
            if old.get("updates", {}).get(category) != version:
                events.append(
                    {
                        "service_id": key,
                        "display_name": new.get("display_name", key),
                        "event": "candidate_changed"
                        if category in old.get("updates", {})
                        else "new_update",
                        "type": category,
                        "version": version,
                    }
                )
        for category in old.get("updates", {}):
            if category not in new.get("updates", {}):
                # Acknowledgement, snooze and filters do not resolve an upstream update.
                service = services.get(key)
                if service and (
                    service["updates"].get(category) is not None
                    or service["check_status"] == "skipped"
                ):
                    continue
                events.append(
                    {
                        "service_id": key,
                        "display_name": old.get("display_name", key),
                        "event": "resolved_update",
                        "type": category,
                    }
                )
        if new.get("failed") and (not old.get("failed") or old.get("reason") != new.get("reason")):
            events.append(
                {
                    "service_id": key,
                    "display_name": new.get("display_name", key),
                    "event": "scan_failed",
                    "reason": new.get("reason"),
                }
            )
        elif old.get("failed") and not new.get("failed"):
            events.append(
                {
                    "service_id": key,
                    "display_name": old.get("display_name", key),
                    "event": "scan_recovered",
                }
            )
    return events


class MissingNotificationEnvironment(ValueError):
    pass


def required_env(name: str | None) -> str:
    value = os.environ.get(name or "", "")
    if not value:
        raise MissingNotificationEnvironment("Notification environment variable is missing")
    return value


def webhook(config: dict, payload: dict, timeout: float) -> None:
    url = required_env(config["url_env"])
    https_url(url)
    headers = (
        {"Authorization": "Bearer " + required_env(config["token_env"])}
        if config.get("token_env")
        else {}
    )
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        with client.stream("POST", url, json=payload, headers=headers) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError("Webhook delivery failed")


def email_content(payload: dict, hostname: str) -> tuple[str, str]:
    labels = {
        "new_update": "New update",
        "candidate_changed": "Update changed",
        "resolved_update": "Update resolved",
        "scan_failed": "Scan failed",
        "scan_recovered": "Scan recovered",
    }
    lines = [f"Host: {hostname}", f"Checked at: {payload['generated_at']}", "", "Changes:"]
    rows = []
    for event in payload["events"]:
        name = str(event.get("display_name", event.get("service_id", "Unknown")))
        label = labels.get(event["event"], event["event"])
        details = []
        if event.get("type"):
            details.append(str(event["type"]))
        if event.get("version"):
            details.append(str(event["version"]))
        if event.get("reason"):
            reason = event["reason"]
            details.append("; ".join(map(str, reason)) if isinstance(reason, list) else str(reason))
        detail = " · ".join(details)
        lines.append(f"- {name}: {label}" + (f" — {detail}" if detail else ""))
        cells = (name, label, detail or "—")
        rows.append("<tr>" + "".join(f'<td style="padding:10px;border-bottom:1px solid #e2e8f0;vertical-align:top">{escape(cell)}</td>' for cell in cells) + "</tr>")
    html = (
        '<html><body style="margin:0;padding:24px;background:#f1f5f9;font-family:Arial,sans-serif;color:#1e293b">'
        '<div style="max-width:720px;margin:auto;background:#fff;border-radius:8px;padding:24px">'
        '<h1 style="font-size:22px;margin:0 0 16px">Podman Image Watch</h1>'
        f'<p><strong>Host:</strong> {escape(hostname)}<br>'
        f'<strong>Checked at:</strong> {escape(str(payload["generated_at"]))}</p>'
        '<h2 style="font-size:17px;margin-top:24px">Changes</h2>'
        '<table style="width:100%;border-collapse:collapse;text-align:left;font-size:14px">'
        '<thead><tr><th style="padding:10px">Service</th><th style="padding:10px">Event</th>'
        '<th style="padding:10px">Details</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div></body></html>'
    )
    return "\n".join(lines), html


def smtp(config: dict, payload: dict, timeout: float) -> None:
    hostname = socket.gethostname()
    message = EmailMessage()
    message["From"] = config["from"]
    message["To"] = ", ".join(config["to"])
    message["Subject"] = f"Podman Image Watch [{hostname}]: image and scan changes"
    plain, html = email_content(payload, hostname)
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    accepted = False
    try:
        with smtplib.SMTP(config["host"], config["port"], timeout=timeout) as server:
            if config["starttls"]:
                server.starttls(context=ssl.create_default_context())
            if config["username_env"]:
                server.login(required_env(config["username_env"]), required_env(config["password_env"]))
            refused = server.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            accepted = True
    except (smtplib.SMTPException, OSError):
        # A failed QUIT after acceptance must not trigger another delivery.
        if not accepted:
            raise


def delivery_error(adapter: str, error: Exception) -> str:
    """Describe common failures without exposing server responses or credentials."""
    if adapter != "smtp":
        return f"{adapter} notification delivery failed; check configuration and credentials"
    if isinstance(error, smtplib.SMTPAuthenticationError):
        detail = "authentication rejected; check username and password"
    elif isinstance(error, smtplib.SMTPRecipientsRefused):
        detail = "recipient rejected; check recipient addresses and server policy"
    elif isinstance(error, smtplib.SMTPSenderRefused):
        detail = "sender rejected; check the from address and server policy"
    elif isinstance(error, ssl.SSLCertVerificationError):
        detail = "TLS certificate verification failed; check the server hostname and CA certificates"
    elif isinstance(error, ssl.SSLError):
        detail = "TLS connection failed; check the server, port and TLS mode"
    elif isinstance(error, smtplib.SMTPNotSupportedError):
        detail = "server does not support the requested SMTP feature; check STARTTLS and authentication"
    elif isinstance(error, (TimeoutError, socket.timeout)):
        detail = "connection timed out; check the server, port and network access"
    elif isinstance(error, socket.gaierror):
        detail = "server hostname could not be resolved"
    elif isinstance(error, MissingNotificationEnvironment):
        detail = "required notification environment variable is missing"
    elif isinstance(error, OSError):
        detail = "connection failed; check the server, port and network access"
    else:
        detail = "SMTP server rejected or interrupted delivery; check configuration and credentials"
    return f"smtp notification delivery failed: {detail}"


def notify(report: dict, state: dict, config: dict, *, disabled: bool = False) -> None:
    if disabled or not config["enabled"]:
        LOG.debug("Notifications disabled%s", " by --no-notify" if disabled else " in configuration")
        return
    states = state.setdefault("notifications", {})
    errors = []
    sent = False
    for adapter, send in (("webhook", webhook), ("smtp", smtp)):
        if not config[adapter]["enabled"]:
            continue
        current = snapshot(report, config)
        previous = states.get(adapter, {})
        if not isinstance(previous, dict) or any(
            not isinstance(v, dict) for v in previous.values()
        ):
            previous = {}
        events = changes(previous, current, report)
        if not config["on_change_only"]:
            events = changes({}, current, report) + [
                e for e in events if e["event"] in {"resolved_update", "scan_recovered"}
            ]
        if not events:
            LOG.debug("%s notification skipped: no eligible changes", adapter)
            states[adapter] = current.copy()
            continue
        payload = {
            "schema_version": 1,
            "tool": "podman-image-watch",
            "generated_at": report["generated_at"],
            "events": events,
        }
        for attempt in range(config["retries"] + 1):
            try:
                LOG.debug("%s notification: sending %d events (attempt %d)", adapter, len(events), attempt + 1)
                send(config[adapter], payload, config["timeout"])
                states[adapter] = current.copy()
                sent = True
                LOG.debug("%s notification accepted for delivery", adapter)
                break
            except (httpx.HTTPError, smtplib.SMTPException, OSError, ValueError, RegistryError) as exc:
                if attempt == config["retries"]:
                    errors.append(delivery_error(adapter, exc))
                else:
                    LOG.debug("%s; retrying", delivery_error(adapter, exc))
                    time.sleep(min(2**attempt, 8))
    report["notifications"] = {
        "status": "error" if errors else "sent" if sent else "unchanged",
        "errors": errors,
    }
