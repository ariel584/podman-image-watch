<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Optional notifications

Notifications are disabled by default. They run once after a scan, with no persistent server.
`--no-notify` disables delivery for a run without consuming notification history.

```yaml
notifications:
  enabled: true
  on_change_only: true
  timeout: 10
  retries: 2
  categories: [patch, minor, major, digest_changed]
  priorities: []
  services: []
  include_errors: true
  webhook:
    enabled: true
    url_env: PIW_WEBHOOK_URL
    token_env: PIW_WEBHOOK_TOKEN
  smtp:
    enabled: false
    host: smtp.example.org
    port: 587
    starttls: true
    username_env: PIW_SMTP_USERNAME
    password_env: PIW_SMTP_PASSWORD
    from: image-watch@example.org
    to: [admin@example.org]
```

Set credentials and the full webhook URL through the named environment variables.
Webhook URLs must use HTTPS, TLS is verified, and redirects are rejected. An optional bearer
token is sent in Authorization. Neither URLs nor credentials are included in diagnostics.
SMTP uses a validated STARTTLS context by default; username/password must be configured
together, and credentials require STARTTLS. Unauthenticated SMTP can explicitly disable
STARTTLS for a trusted local relay. Implicit TLS on port 465 is not implemented.

Empty category/priority/service lists mean no filtering. Services match stable IDs or display
names using globs. Error delivery is controlled independently by `include_errors`.
`notify: false` in a service rule excludes that service from adapter notifications.
Acknowledged and snoozed events remain in reports but are excluded from delivery.

The webhook body and email text contain `schema_version`, `tool`, `generated_at` and an
`events` list. Events contain `service_id`, `display_name` and an event name:

| Event | Meaning |
|---|---|
| new_update | Newly eligible category, including expiration of a snooze |
| candidate_changed | A different version/digest for a known category |
| resolved_update | A category is absent after a conclusive scan or the source was removed |
| scan_failed | Newly inconclusive/failed service or discovery diagnostics; a changed failure reason also produces this event |
| scan_recovered | A previously failed result becomes conclusive |

A registry error cannot establish that a previous update resolved. Suppressing an update
with an acknowledgement, snooze or filter does not emit a resolution. Changes to presentation
names alone do not send an alert. With `on_change_only: false`, all eligible current updates
and failures are sent each scan, together with actual recovery/resolution events.

Each enabled adapter tracks its own last delivered snapshot. Failed adapters retry on later
runs; successful adapters do not resend merely because another adapter failed. Delivery
retries are bounded to 0–5, with exponential waits capped at eight seconds. Timeouts apply
to each delivery attempt. Webhooks and SMTP have at-least-once delivery semantics: a process
crash after remote acceptance but before state persistence can lead to a duplicate.
Do not rely on the utility for exactly-once delivery.

File-mode reports are written before notification delivery, then updated with notification
status. Delivery errors preserve results, appear under `notifications.errors`, and produce
exit 1 and partial health. Notification state is committed only on success. Image scan
success timestamps describe registry/discovery success, independently of delivery.

When Zabbix handles operational alerts, leave built-in notifications disabled unless separate
recipients/channels are intentional. Both systems consuming the same update policy can
otherwise send duplicate alerts. Zabbix triggers use suppression-aware metrics and keep
registry/unknown/stale alarms independent of update acknowledgements.
