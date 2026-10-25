<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Podman Image Watch

A lightweight, read-only CLI that checks OCI images **configured in Podman Quadlets** for
available updates. Designed for Linux and AlmaLinux hosts, scheduled systemd jobs and
external monitoring. It checks configured references, not necessarily the images currently
running in containers.

It reads `.container` and `.image` units, applies drop-ins, queries registry metadata, and
reports independent patch, minor and major candidates. It preserves image variants, supports
PostgreSQL, Zabbix and calendar version policies, monitors moving-tag digests, and retains
clear diagnostics for images it cannot safely analyze. YAML rules provide display names,
ignores, exact update acknowledgements, snoozes, metadata and notification policy.

The process requires no Docker, Podman daemon/socket, root privileges, database or server.
It never pulls images, downloads layers, changes Quadlets, or restarts containers/services.
Runtime dependencies are PyYAML, httpx and packaging. Source is licensed under
[GPL-3.0-or-later](LICENSE), with SPDX identifiers in source and examples.

## Install and run

Requires Python 3.11+. From this checkout:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/podman-image-watch --quadlet-dir /etc/containers/systemd
```

Alternatively, `pipx install .` installs an isolated CLI from the checkout. There is no
assumption that a release has already been published to PyPI.

With no output path, the complete pretty-printed JSON report goes to stdout. Diagnostics
always go to stderr. Updates alone leave exit status 0.

```sh
podman-image-watch --quadlet-dir ./examples/quadlets
podman-image-watch --quadlet-dir /etc/containers/systemd --quadlet-dir /home/alice/.config/containers/systemd
podman-image-watch --quadlet-dir /etc/containers/systemd --format table
podman-image-watch --quadlet-dir /etc/containers/systemd --format table --hide-variants --resolve-floating
podman-image-watch --help
podman-image-watch --version
```

Advanced YAML configuration:

```sh
cp examples/config.yaml config.yaml
# Edit source directories and optional rules.
podman-image-watch validate-config --config config.yaml
podman-image-watch --config config.yaml
podman-image-watch --config config.yaml --output /var/lib/podman-image-watch/report.json
podman-image-watch --config config.yaml --output -
podman-image-watch --config config.yaml --no-notify --verbose
```

Explicit file output creates parent directories and replaces the report atomically, without
printing the report to stdout. `--output -` forces stdout even when YAML supplies a path.
Defaults are overridden by YAML, then explicit CLI arguments. State defaults to
`~/.local/state/podman-image-watch`; change it with `--state-dir` or YAML.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Successful scan, including available updates; or valid configuration |
| 1 | Usable partial report: registry/discovery error, unknown/unsupported result, or failed notification |
| 2 | Fatal configuration, state-lock, output or runtime error |

A report contains these fields (excerpt; synthetic candidates):

```json
{
  "schema_version": 1,
  "tool": "podman-image-watch",
  "tool_version": "0.1.0",
  "generated_at": "2026-10-25T10:00:00Z",
  "scan": {"status": "success", "health": 0},
  "services": [
    {
      "id": "4fdca03afc754811a34bfaec98eae1a6",
      "display_name": "Grafana",
      "image": "docker.io/grafana/grafana:13.2.4",
      "check_status": "ok",
      "updates": {"patch": "13.2.5", "minor": "13.3.1", "major": "14.0.2"},
      "priority": "attention",
      "acknowledged": false,
      "snoozed": false
    }
  ]
}
```

The complete [fixture report](tests/fixtures/report.json) and
[JSON Schema](docs/report-v1.schema.json) define the integration contract. Stable IDs derive
from source identity and Quadlet path, so changing image versions or names in the UI does
not recreate monitoring items.

## Classification and operational policy

For `13.2.4`, patch `13.2.5`, minor `13.3.1` and major `14.0.2` are reported independently.
Patch/minor default to `attention`; major defaults to `planning`, not urgent maintenance.
No patch is labeled a security fix without independent evidence. PostgreSQL maintenance
and CalVer use profile-specific rules; a new calendar year is not a SemVer major.

`18.6-bookworm` keeps its distribution suffix by default. Floating tags such as
`stable-alpine` use digest changes; their first observation establishes a baseline.
Multi-platform indexes are compared consistently across scanning hosts. Digest-pinned
references are verified without inventing tag recommendations. Registry failures never
become up-to-date results. Ambiguous short names, local images and unresolved dynamic
references stay visible with reasons; short names require explicit opt-in to Docker Hub.

`--hide-variants` displays `ubuntu-7.0.32` as `7.0.32` and `18.6-bookworm` as `18.6`;
raw JSON fields and variant matching remain unchanged. `--resolve-floating` optionally
matches a floating tag's exact manifest digest to numbered tags, showing a value such as
`1.28.3 [stable]`. This identifies the registry's current target, not a running container's
version. Missing or conflicting evidence remains unknown. Resolution makes additional
bounded metadata requests and is disabled by default.

To skip a local image, add `ignore: true` to a YAML rule matching its Quadlet:

```yaml
rules:
  - match: {quadlet: dnsmasq-acme.container}
    ignore: true
```

Ignored services stay in JSON as skipped, without registry requests. `--hide-skipped`
omits those rows from the table while retaining the full monitoring report.

Optional HTTPS webhook and STARTTLS email notifications have per-adapter change detection,
filters, bounded retries and environment-based secrets. Acknowledged and snoozed candidates
remain visible. Notification failures preserve the scan report.

## Deploy and monitor

After pulling the repository on an AlmaLinux server, follow the commands in
[the server installation guide](docs/deployment.md). It covers Python packages, an isolated
installation in `/opt/podman-image-watch`, a dedicated service account, configuration,
a manual preview, daily scheduling and updates after future `git pull` operations.

The ready-to-edit [server configuration](examples/config-server.yaml) reads
`/etc/containers/systemd`, writes `/var/lib/podman-image-watch/report.json`, hides variants
in display fields, resolves floating versions, and ignores `dnsmasq-acme.container` as a
local image. Review the source path and ignore rule for your host. It keeps notifications
disabled. The service has its own group; Zabbix is an optional later integration.

The [systemd service and timer](examples/systemd) run a daily oneshot scan with explicit file
output and filesystem hardening. Follow the [Linux/AlmaLinux deployment guide](docs/deployment.md)
for installation, verification and removal. Ordinary scans run as a dedicated unprivileged
user. Keep SELinux enforcing and grant only the required read access.

The optional [Zabbix 7.0 template](examples/zabbix/template_podman_image_watch.yaml) reads the
JSON file through Agent 2, discovers services automatically, and creates dependent items
and suppression-aware triggers. It requires no sender or scanner subprocess for each item.
See the [complete installation-to-monitoring workflow](docs/zabbix.md). Template structure
and JSONPath are tested offline; live Zabbix import/UI verification remains a deployment check.

## Documentation and limitations

- [Configuration and rule precedence](docs/configuration.md)
- [Version policies, variants and digest semantics](docs/version-policies.md)
- [Report fields, counters, exit codes and state](docs/report-format.md)
- [Notifications and deduplication](docs/notifications.md)
- [Deployment](docs/deployment.md) and [Zabbix](docs/zabbix.md)

Version names do not establish upgrade compatibility. Moving tags reveal only a change since
the previous observed digest. Registry tag lists can be incomplete or stale upstream; the
configurable cache can delay seeing new versions. Unsupported/private registries require
suitable API support and credentials. Candidate tags are not verified for a target platform.
The tool does not inspect live systemd/Podman state or merge independent source roots as
search-path overlays. File symlinks are confined to a root and directory symlinks are not
traversed. These conservative limits avoid unintended file access and guessed recommendations.

Registry traffic uses HTTPS with certificate validation. Auth realms and pagination URLs are
validated; redirects and environment proxies are disabled. Secrets are not stored in reports
or cache. Metadata sizes, pagination, retries and deadlines are bounded. Use read-only registry
credentials and keep local YAML/environment configuration private. Only manifest/index and
tag endpoints are queried, never layer endpoints.

## Develop

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests tools
.venv/bin/python -m ruff format --check src tests tools
.venv/bin/python -m mypy src
.venv/bin/python tools/generate_zabbix_template.py
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

Tests use mocked HTTP transports and local fixtures, without live registry access.
Sanitized image settings from the supplied real Quadlets cover Authentik, Grafana,
PostgreSQL, Zabbix, local images and floating tags; no server secrets or volumes are copied.
CI covers Python 3.11–3.14 on Linux. Local execution has also been exercised on macOS;
production service/SELinux verification must take place on the intended Linux host.

Format references: [Podman Quadlets](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html),
[OCI Distribution](https://github.com/opencontainers/distribution-spec/blob/main/spec.md),
[Zabbix 7.0 LLD](https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery).
