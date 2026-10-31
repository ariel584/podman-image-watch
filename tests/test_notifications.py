# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import smtplib
import ssl
from copy import deepcopy
from datetime import UTC, datetime

import httpx
import pytest
from test_checker import FakeRegistry

from podman_image_watch.checker import scan
from podman_image_watch.notifications import delivery_error, notify, smtp, webhook


@pytest.fixture
def notification_setup(config, monkeypatch):
    settings = config["notifications"]
    settings.update(enabled=True, retries=0)
    settings["webhook"]["enabled"] = True
    delivered = []
    monkeypatch.setattr(
        "podman_image_watch.notifications.webhook",
        lambda cfg, payload, timeout: delivered.append(payload),
    )
    return settings, delivered


def test_changes_new_unchanged_candidate_resolved(config, notification_setup):
    settings, delivered = notification_setup
    state, registry = {}, FakeRegistry()
    report = scan(config, state, registry)
    notify(report, state, settings)
    assert [e["event"] for e in delivered[-1]["events"]] == ["new_update"] * 3
    notify(scan(config, state, registry), state, settings)
    assert len(delivered) == 1
    registry.tags_result.append("1.2.5")
    notify(scan(config, state, registry), state, settings)
    assert delivered[-1]["events"][0]["event"] == "candidate_changed"
    config["sources"]["images"][0]["image"] = "docker.io/example/app:2.0.0"
    notify(scan(config, state, registry), state, settings)
    assert {e["event"] for e in delivered[-1]["events"]} == {"resolved_update"}


def test_delivery_debug_log_distinguishes_sent_from_unchanged(config, notification_setup, caplog):
    settings, delivered = notification_setup
    state = {}
    report = scan(config, state, FakeRegistry())
    with caplog.at_level(logging.DEBUG, logger="podman_image_watch.notifications"):
        notify(report, state, settings)
        notify(report, state, settings)
    assert len(delivered) == 1
    assert "webhook notification accepted for delivery" in caplog.text
    assert "webhook notification skipped: no eligible changes" in caplog.text


def test_snooze_expiration_and_ack_scope(config, notification_setup):
    settings, delivered = notification_setup
    config["defaults"]["updates"].update(snooze_until="2026-12-01")
    state, registry = {}, FakeRegistry()
    notify(scan(config, state, registry, datetime(2026, 12, 1, tzinfo=UTC)), state, settings)
    assert not delivered
    notify(scan(config, state, registry, datetime(2026, 12, 2, tzinfo=UTC)), state, settings)
    assert len(delivered) == 1
    config["defaults"]["updates"]["acknowledge"] = [{"type": "major", "version": "2.0.0"}]
    notify(scan(config, state, registry, datetime(2026, 12, 2, tzinfo=UTC)), state, settings)
    assert len(delivered) == 1  # Acknowledging an existing event is not a resolution.
    registry.tags_result.append("3.0.0")
    notify(scan(config, state, registry, datetime(2026, 12, 2, tzinfo=UTC)), state, settings)
    assert delivered[-1]["events"][0]["version"] == "3.0.0"


def test_failure_and_recovery_do_not_resolve_updates(config, notification_setup):
    settings, delivered = notification_setup
    state, registry = {}, FakeRegistry()
    report = scan(config, state, registry)
    notify(report, state, settings)
    failed = deepcopy(report)
    service = failed["services"][0]
    service.update(
        check_status="error", update_status="unknown", events=[], reason="Registry HTTP 403"
    )
    service["updates"] = {k: None for k in service["updates"]}
    failed["scan"]["status"] = "partial"
    notify(failed, state, settings)
    assert [e["event"] for e in delivered[-1]["events"]] == ["scan_failed"]
    notify(report, state, settings)
    assert [e["event"] for e in delivered[-1]["events"]] == ["scan_recovered"]


def test_notification_failures_retry_next_run(config, notification_setup, monkeypatch):
    settings, delivered = notification_setup

    def broken(*args):
        raise httpx.ConnectError("secret-token")

    monkeypatch.setattr("podman_image_watch.notifications.webhook", broken)
    state = {}
    report = scan(config, state, FakeRegistry())
    notify(report, state, settings)
    assert report["notifications"]["status"] == "error"
    assert "secret" not in str(report)
    assert not state["notifications"].get("webhook")
    monkeypatch.setattr(
        "podman_image_watch.notifications.webhook",
        lambda cfg, payload, timeout: delivered.append(payload),
    )
    notify(report, state, settings)
    assert delivered


def test_no_notify_preserves_dedup_state(config, notification_setup):
    settings, delivered = notification_setup
    state = {"notifications": {"webhook": {"old": {"updates": {}}}}}
    before = deepcopy(state["notifications"])
    notify(scan(config, state, FakeRegistry()), state, settings, disabled=True)
    assert state["notifications"] == before and not delivered


def test_category_filter(config, notification_setup):
    settings, delivered = notification_setup
    settings["categories"] = ["major"]
    state = {}
    notify(scan(config, state, FakeRegistry()), state, settings)
    assert [e["type"] for e in delivered[-1]["events"]] == ["major"]


def test_webhook_transport_and_redirect_rejection(monkeypatch):
    monkeypatch.setenv("HOOK", "https://hooks.example/secret-path")
    monkeypatch.setenv("TOKEN", "secret-token")
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    monkeypatch.setattr(
        "podman_image_watch.notifications.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ValueError):
        webhook({"url_env": "HOOK", "token_env": "TOKEN"}, {"events": []}, 1)
    assert len(calls) == 1


def test_smtp_starttls_and_login(config, monkeypatch):
    calls = []

    class Server:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, context):
            assert context.check_hostname
            calls.append("tls")

        def login(self, username, password):
            assert (username, password) == ("user", "secret")
            calls.append("login")

        def send_message(self, message):
            assert "secret" not in message.as_string()
            calls.append("send")

    monkeypatch.setattr("podman_image_watch.notifications.smtplib.SMTP", Server)
    monkeypatch.setenv("SMTP_USER", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    settings = config["notifications"]["smtp"]
    settings.update(
        host="smtp.example",
        username_env="SMTP_USER",
        password_env="SMTP_PASSWORD",
        to=["admin@example.org"],
        **{"from": "watch@example.org"},
    )
    smtp(settings, {"generated_at": "2026-10-28T18:17:04Z", "events": []}, 1)
    assert calls[1:] == ["tls", "login", "send"]


def test_smtp_delivery_failure_keeps_report(config, monkeypatch):
    config["notifications"].update(enabled=True, retries=0)
    config["notifications"]["smtp"]["enabled"] = True

    def broken(*args):
        raise smtplib.SMTPException("secret-password")

    monkeypatch.setattr("podman_image_watch.notifications.smtp", broken)
    report, state = scan(config, {}, FakeRegistry()), {}
    notify(report, state, config["notifications"])
    assert report["services"][0]["updates"]["patch"]
    assert report["notifications"]["status"] == "error" and "secret" not in str(report)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (smtplib.SMTPAuthenticationError(535, b"secret"), "authentication rejected"),
        (ssl.SSLCertVerificationError("secret"), "certificate verification failed"),
        (TimeoutError("secret"), "timed out"),
        (smtplib.SMTPRecipientsRefused({"secret": (550, b"secret")}), "recipient rejected"),
    ],
)
def test_smtp_error_diagnostics_do_not_expose_server_response(error, expected):
    message = delivery_error("smtp", error)
    assert expected in message
    assert "secret" not in message


def test_smtp_partial_recipient_refusal_is_failure(config, monkeypatch):
    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, context):
            pass

        def send_message(self, message):
            return {"rejected@example.org": (550, b"rejected")}

    monkeypatch.setattr("podman_image_watch.notifications.smtplib.SMTP", Server)
    settings = config["notifications"]["smtp"]
    settings.update(host="smtp.example", to=["admin@example.org"], **{"from": "watch@example.org"})
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        smtp(settings, {"generated_at": "2026-10-28T18:17:04Z", "events": []}, 1)


def test_smtp_formats_email_and_does_not_retry_after_quit_failure(config, monkeypatch):
    sent = []

    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise smtplib.SMTPServerDisconnected("QUIT failed")

        def starttls(self, context):
            pass

        def send_message(self, message):
            sent.append(message)
            return {}

    monkeypatch.setattr("podman_image_watch.notifications.smtplib.SMTP", Server)
    monkeypatch.setattr("podman_image_watch.notifications.socket.gethostname", lambda: "server-1")
    settings = config["notifications"]
    settings.update(enabled=True, retries=2)
    settings["smtp"].update(
        enabled=True, host="smtp.example", to=["admin@example.org"], **{"from": "watch@example.org"}
    )
    report, state = scan(config, {}, FakeRegistry()), {}
    report["services"][0]["display_name"] = "Grafana <test>"
    notify(report, state, settings)
    assert report["notifications"]["status"] == "sent"
    assert len(sent) == 1
    assert "server-1" in sent[0]["Subject"]
    plain = sent[0].get_body(preferencelist=("plain",)).get_content()
    html = sent[0].get_body(preferencelist=("html",)).get_content()
    assert "Grafana <test>" in plain and "New update" in plain
    assert "Grafana &lt;test&gt;" in html and "server-1" in html
    notify(report, state, settings)
    assert len(sent) == 1
