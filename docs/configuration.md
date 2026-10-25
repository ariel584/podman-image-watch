<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Configuration reference

A directory needs no YAML: `podman-image-watch --quadlet-dir /etc/containers/systemd`.
For advanced use copy [examples/config.yaml](../examples/config.yaml). YAML must declare
`version: 1`. Unknown keys, duplicate YAML keys, wrong types and invalid dates are errors.
`validate-config --config config.yaml` checks syntax and settings without reading Quadlets,
creating state or querying registries. It does not verify directory or credential availability.

Precedence is built-in defaults, YAML, then explicitly supplied CLI arguments. Repeated
`--quadlet-dir` values replace the YAML directory list. `--output -` replaces a YAML output
path. `--format`, `--state-dir` and `--no-notify` provide other CLI overrides.
`--hide-variants` and `--hide-skipped` override their output settings; their `--no-...`
forms disable them. `--resolve-floating` / `--no-resolve-floating` override the corresponding
update policy after all matching rules, including Quadlet rules.
Paths are relative to the working directory, with `~` expanded for filesystem paths.
The parser uses safe YAML with a 1 MiB configuration limit; configuration files are trusted
local administrator input. YAML aliases are rejected to keep resource use bounded.

## Sources

```yaml
version: 1
sources:
  quadlet_dirs:
    - id: system
      path: /etc/containers/systemd
    - id: alice
      path: /home/alice/.config/containers/systemd
  allow_short_names: false
  images:
    - id: custom-service
      image: docker.io/library/redis:7.4.1
      display_name: Redis
```

Directories can also be strings. A mapping's `id` is the stable source identity; a string
uses its absolute path. Use explicit IDs if moving a directory should preserve service IDs.
Each root is an independent installation, rather than an implicit `/run`/`/etc`/`/usr`
systemd search overlay. Supply the effective directory for each installation; do not supply
multiple overlay layers expecting unit-name shadowing. Recursion ignores directory symlinks
and drop-in directories as independent sources. File symlinks are read only inside their
configured root. The root itself may be a symlink. Escaping links receive diagnostics.

Discovery reads `.container`, standalone `.image`, and referenced `.image` units.
A referenced `.image` is represented by its consuming containers, rather than an additional
service. Different consumers retain distinct IDs and share registry queries. Supported
drop-ins include `container.d`, dash prefixes, template and instance directories, with
more-specific directories winning same-filename conflicts, then lexical filename order.
These directories are read beside the base Quadlet. Last scalar setting wins; an empty
setting resets it. Comments, quoted scalar values and systemd continuations are handled.
Other INI settings are ignored; environment files and secrets are never opened.

Instance drop-in directories instantiate matching templates. Static `%i` in explicit
instances is resolved; other specifiers and environment expressions remain unknown.
Uninstantiated templates with dynamic images remain visible as unknown. The scanner cannot
infer instances created solely by a running systemd manager or enumerate active containers.
Build units, local-only images, escaped instance names and ambiguous short names require
explicit policy or remain unknown. `allow_short_names: true` explicitly selects Docker Hub,
including its `library/` namespace, and does not read Podman's short-name aliases.

## Output and state

`output.format`: `json` (default) or `table`. `output.path`: null (stdout), `-` (stdout),
or a file path. `output.pretty`: true by default; false produces compact JSON for monitoring.
`output.hide_variants`: false by default; true removes distribution prefixes/suffixes in
the additional `display` fields and table output. Original tags and matching constraints
are preserved. `output.hide_skipped`: false by default; true hides ignored rows in tables.
The JSON inventory and summary still include skipped services.
Output files must be outside scanned Quadlet directories and differ from the YAML configuration,
private state and lock paths, to prevent accidental overwriting of inputs.
File replacement is atomic and UTF-8; new reports use mode 0640, existing permission bits
are preserved without executable bits. New state files use 0600 in a private directory.

`state.directory` defaults to `~/.local/state/podman-image-watch`. It stores versioned JSON,
digest baselines, a bounded registry cache, per-adapter notification state and successful scan
time. A nonblocking `flock` prevents concurrent scans using the same directory. All writers
of a given report must share that state directory. A corrupt state is logged and reset;
the next digest observation establishes a baseline. State is capped at 32 MiB and reports
at 16 MiB. Deleting state also resets digest and notification history.

## Registry settings

| Setting | Default | Meaning |
|---|---:|---|
| timeout | 10 | Per-operation network timeout, seconds (0.1–120) |
| retries | 3 | Retries after connection errors, 429 or 5xx (0–5) |
| cache_ttl | 12h | Tag cache freshness; seconds or `s`/`m`/`h`/`d`; 0 disables |
| max_parallel_requests | 4 | Worker and connection limit (1–16) |
| max_pages | 100 | Maximum pages per repository (1–1000) |
| max_tags | 100000 | Maximum distinct tags per repository |
| max_response_bytes | 8388608 | Maximum decoded bytes per HTTP response |
| deadline | 120 | Total seconds per tag-list or manifest operation (1–600) |
| max_resolution_candidates | 20 | Maximum numbered-tag manifests per floating-tag lookup (1–100) |

Exponential retries honor `Retry-After` up to 30 seconds within the operation deadline.
Incomplete pagination is an error, never an up-to-date result. Complete tag lists can be
reused until TTL expiration. Manifests are fetched each run when monitoring digests.
No stale-cache fallback occurs after an attempted refresh fails. The report reflects
metadata as fresh as `cache_ttl`, rather than guaranteeing instantaneous upstream state.

```yaml
registries:
  credentials:
    ghcr.io:
      username_env: PIW_GHCR_USER
      password_env: PIW_GHCR_TOKEN
    registry.example.org:
      token_env: PIW_REGISTRY_BEARER_TOKEN
      auth_hosts: [auth.example.org]
```

Use read-only credentials. Username/password are sent only to a validated HTTPS bearer-token
realm. `token_env` sends a pre-issued bearer token to the registry. The client requests only
`repository:<name>:pull` scope. Realm hosts default to the registry host; Docker Hub also
trusts `auth.docker.io`. Other realm hosts require an explicit `auth_hosts` entry.
Pagination must stay on the same repository endpoint. Redirects are rejected. TLS verification
is always enabled; HTTP proxies and environment-based client configuration are disabled.
No Docker/Podman credential store is consulted. Secrets and upstream error bodies are never
copied into reports or diagnostics. Only SHA-256 manifest digests are currently supported.

## Rules and defaults

All matching rules are applied. Priority is defaults, source-only rules, image rules,
then Quadlet rules. At each level later declarations win field by field. A rule matching
both image and Quadlet belongs to the Quadlet level. Match fields are ANDed, using case-sensitive
globs. `quadlet` matches the relative path or filename; `image` matches the full normalized
repository without tag/digest; `source` matches the source ID. Display names affect no IDs
or registry requests. Defaults may contain the same policy fields as rules.

```yaml
rules:
  - match: {quadlet: 'test-*.container'}
    ignore: true
  - match: {quadlet: 'nested/authentik-server.container', source: system}
    display_name: Authentik Server
    labels: {team: infrastructure}
  - match: {image: 'docker.io/library/postgres'}
    version: {scheme: postgres, preserve_variant: true}
  - match: {quadlet: grafana.container}
    updates:
      ignore_versions: ['13.3.0']
      check_major: true
      acknowledge:
        - type: major
          version: '14.0.2'
          reason: Migration planned
      snooze_until: '2026-12-01'
      reason: Maintenance window
    severity: {major: information}
    notify: false
```

`ignore` preserves a skipped service in the report, without registry requests. Ignored
versions are excluded from candidates. `check_patch`, `check_minor`, `check_major` and
`check_digest` default to true. `monitor_immutable` defaults to false and enables digest
observations behind numeric tags. `resolve_floating` defaults to false; enabling it matches
the registry's current floating digest against versioned tags. The alias lookup shares a
single registry deadline across its manifest probes and checks at most
`max_resolution_candidates` tags, preferring the named variant and newer versions. Use
`version.tag_patterns` to narrow the search for long-lived maintenance aliases. No match or
conflicting versions produce an explicit unknown/ambiguous resolution, without guessing.
Registry failures during resolution make the service check an error while retaining digest
results. See [version policies](version-policies.md) for the distinction from running images.
Acknowledgements match exact category and candidate
(or digest for `digest_changed`). A different candidate requires a new acknowledgement.
Snoozes include the whole named UTC date and expire at midnight of the following day.
Neither snoozing nor acknowledging removes candidates. `notify: false` suppresses operational
update notifications; registry errors remain visible in reports. The notification adapter
also excludes services with `notify: false`; Zabbix error triggers remain independent.

`severity` maps patch/minor/major/digest_changed to information, planning, attention or
warning. Defaults are attention/attention/planning/information. Report priority is the
highest observed event priority (warning > attention > planning > information), even when
suppressed. Per-event priorities and suppression are retained so consumers can apply their
own policy. See [version policies](version-policies.md) and [notifications](notifications.md).
