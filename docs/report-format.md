<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Public JSON report v1

The machine-readable contract is [report-v1.schema.json](report-v1.schema.json).
Consumers must tolerate additional fields within schema version 1. Breaking changes require
a new schema version. IDs and field meanings are stable independently of display labels.
All timestamps use UTC, with ISO 8601 `Z` strings and integer epoch seconds for monitoring.
A complete synthetic report is in [tests/fixtures/report.json](../tests/fixtures/report.json).

`tool_version` identifies the implementation. `generated_at` and `scan.timestamp` identify
scan start; `duration_seconds` measures the scan. `scan.status` is `success` when every
non-ignored service is analyzed, or `partial` on errors/unknown/unsupported results or
notification delivery failures. `scan.health` is 0/1 respectively. Fatal failures exit 2
and may leave the previous atomic report intact; monitor file age as well as health.

`last_success_at` and `last_success_timestamp` record the last complete **image metadata scan**,
independent of optional notification delivery. They are null/0 until a successful scan and
persist across partial scans. `scan.diagnostics` captures directory/traversal errors.
An empty readable directory is a successful zero-service inventory; a missing directory
is a partial scan with diagnostics. Registry failures never mean an image is current.

## Service fields

| Field | Meaning |
|---|---|
| id | First 128 bits of SHA-256 over source ID, NUL separator and relative Quadlet path; explicit images use source `explicit` and configured ID |
| source, quadlet, source_file | Source identifier, relative unit name and source file path; explicit inputs have null file fields |
| display_name | YAML override, humanized static ContainerName, static Unit Description, or humanized filename |
| image, repository | Configured reference and normalized fully qualified OCI repository |
| current_version, configured_digest | Configured tag and digest; an omitted tag means latest unless digest-only |
| check_status | `ok`, `error`, `unsupported`, `unknown`, or `skipped` |
| update_status | `up_to_date`, `updates_available`, `pinned`, `unknown`, or `skipped` |
| version_scheme, reason | Selected profile and a diagnostic explaining inconclusive/skipped/failed checks |
| updates | Independent patch/minor/major/digest_changed candidate strings, null when absent |
| events | Category, candidate, priority, acknowledgement reason and suppression for every detected category |
| digest | Current SHA-256, index/manifest scope, platforms; moving targets also have previous digest and baseline flag |
| acknowledged | True when every visible event is acknowledged; false for no events or a mixture |
| snoozed | Whether the configured UTC snooze is active |
| notification_suppressed | All visible events suppressed by acknowledgement, snooze or notify=false; ignored services are also suppressed |
| labels | User-supplied string metadata |
| metrics | Numeric 0/1 fields for simple monitoring preprocessing |

The stable ID changes if a Quadlet is renamed, its relative path changes, or its source ID
changes. It does not depend on the image, tag, digest or presentation name. Explicit source
IDs allow directory relocation. IDs use only hexadecimal characters for safe monitoring keys.

`metrics.attention` and `metrics.planning` count unsuppressed events with those priorities,
per service. `metrics.errors` and `metrics.unknown` remain independent of update suppression.
`metrics.patch/minor/major/digest_changed` represent raw candidate presence.
`metrics.digest_attention` is a suppression-aware digest event flag. A service can have both
attention and planning = 1; consumers should not sum them as a unique-service total.
Priority remains the highest detected priority even if the corresponding event is suppressed.

Partial results can retain version candidates even if an independent digest lookup fails.
Always inspect `check_status`; null candidates in an inconclusive check do not prove absence.
Ignored Quadlets stay visible as skipped. Unsupported references stay visible as unknown.
Pinned references are verified, but tag upgrades are not inferred for them.

## Aggregate counts

`total_services` includes successful, skipped and unresolved services. `unique_images` counts
distinct nonempty configured reference strings, including skipped inputs. Shared images are
queried once per run, while services retain their own policies and results.
`patch_updates`, `minor_updates`, `major_updates`, `digest_changed_updates` count services
with a candidate in that category, including suppressed candidates. They count categories,
not upstream release counts. `update_services` counts each service with any event once.
`attention_services` and `planning_services` each count affected, unsuppressed services once.
`errors` counts failed services plus source discovery diagnostics; `unknown` counts unknown
and unsupported services; `skipped` counts explicitly ignored services. No aggregate count
claims that patch releases are security fixes.

`notifications.status` is disabled/sent/unchanged/error. `notifications.errors` contains
redacted adapter failures and does not replace image results. Diagnostic logs are written
to stderr. The default stdout is always a complete JSON document; `--format table` is an
explicit human-readable view. File mode creates parent directories and replaces reports
atomically. Stdout has no progress banners. Use `output.pretty: false` for compact JSON.

## Display versions and floating-tag evidence

New optional fields in schema v1 preserve compatibility with earlier reports:

- `display.current_version`: the resolved versioned tag when available, otherwise the
  configured tag. `output.hide_variants` removes recognized distribution prefixes/suffixes
  here without changing raw `current_version` or registry policy.
- `display.configured_tag`: the original alias/tag for presentation under the same setting.
- `display.updates`: presentation forms of category candidates; digest values are unchanged.
- `resolved_tag` and `resolved_version`: a versioned alias with the same observed complete
  manifest/index digest, and its normalized numeric version; null without a verified match.
- `resolution`: null when lookup is disabled or inapplicable; otherwise its status is
  resolved/unknown/ambiguous/error, with source `matching_manifest_digest`, matching tag
  names, tag/version, probe count and diagnostic reason. Probe count is null when a request
  failure interrupts the lookup. Evidence covers the bounded set of probed tags.

The table shows a resolved current value with the configured alias in brackets. JSON retains
all original values and includes ignored services even when `output.hide_skipped` hides table
rows. Unknown version resolution is separate from digest-check health: a successful digest
observation can have an unknown numbered version. A failed resolution request is a registry
error and cannot claim a successful check. None of these fields describes the running image.
