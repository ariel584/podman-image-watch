<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Zabbix 7.0 LTS integration

Import [template_podman_image_watch.yaml](../examples/zabbix/template_podman_image_watch.yaml)
under Data collection → Templates → Import, then link **Podman Image Watch by Zabbix agent**
to a host with Zabbix Agent 2. No sender, HTTP endpoint or scanner-per-item execution is used.
The agent reads an existing JSON file. Native notification delivery is optional and normally
disabled when Zabbix manages alerting.

If import reports `UUIDv4 is expected`, replace the old template export with the current
file and retry. Early exports incorrectly used UUIDv5. Zabbix requires 32-character hexadecimal
UUIDv4 values for these entities. The generator now uses UUIDv4 and retains each entity's
ID in [tools/zabbix_uuids.json](../tools/zabbix_uuids.json). Commit that registry together with
the generated YAML; do not delete or recreate it when updating the template. Service IDs
inside scan reports are a separate identity and do not change with this correction.

After publishing/pulling the corrected repository, import
`examples/zabbix/template_podman_image_watch.yaml` again. Reinstalling the Python scanner
or restarting containers is not required for this template-only correction. The generator
is for development; normal installations import the included YAML directly.

Follow [deployment.md](deployment.md) to install the scanner, configure its Quadlet sources,
and run the oneshot service. Verify that `/var/lib/podman-image-watch/report.json` exists
and that the agent account can traverse the directory and read the file. The systemd example
uses a 0750 directory owned by `podman-image-watch:podman-image-watch`, a 0640 report, and
a separate private 0700 state directory. Grant the agent account membership in the report
group and restart the agent to apply its new supplementary group (assuming the account and
unit are named `zabbix` and `zabbix-agent2`):

```sh
sudo usermod -aG podman-image-watch zabbix
sudo systemctl restart zabbix-agent2
sudo -u zabbix head -c 200 /var/lib/podman-image-watch/report.json
```

Zabbix is optional; creating its account/group is not required to install the scanner.
Atomic replacements create new inodes, so a one-time file ACL alone is insufficient;
use the shared group or directory default ACLs. Keep SELinux enforcing and inspect AVCs if
Unix permissions are correct but reads fail. Do not grant access to the private state.

Set host macros:

| Macro | Default |
|---|---|
| `{$PIW.REPORT_PATH}` | `/var/lib/podman-image-watch/report.json` |
| `{$PIW.MAX_AGE}` | `172800` seconds |

The master key is `vfs.file.contents["{$PIW.REPORT_PATH}"]`, polled every five minutes.
Aggregate dependent items extract scan timestamps, health, total services, attention,
planning, major candidates, errors and unknown counts. Missing data for 15 minutes triggers
a warning; a report older than MAX_AGE also triggers a warning. Three consecutive unhealthy
observations warn about scan failure. This is three agent polls, not necessarily three
separate daily scanner invocations.

LLD extracts `$.services`, maps `$.id` to `{#PIW.ID}` and `$.display_name` to `{#PIW.NAME}`.
IDs are safe hexadecimal item-key values and do not change when a version changes.
Per-service raw dependent items select:

```text
$.services[?(@.id == "{#PIW.ID}")].first()
```

Further dependent items expose current tag, configured digest, patch/minor/major candidates,
status, priority, acknowledgement, snooze and numeric operational flags. Null text values
become `-`. Boolean fields are converted to decimal. Visible names use display names;
keys use IDs. Display-name overrides update presentation on the next discovery cycle.
Resources lost from discovery have a 30-day deletion grace period.

Trigger prototypes map maintenance attention to Warning, planning to Information,
digest changes to Information, repeated registry errors to Warning and unknown/unsupported
to Information. The operational flags honor event-level acknowledgements and snoozes.
Monitoring errors remain independent. Candidate items remain visible even when suppressed.
A major event whose rule changes priority to information no longer sets the planning flag;
its raw candidate remains visible. Event priority is a tool policy, not a security advisory.

In Latest data, first confirm the master JSON item receives a value, then check aggregate
items and discovered service items. Use the preprocessing test dialog with
[tests/fixtures/report.json](../tests/fixtures/report.json): the fixture has patch, minor
and major candidates for one service, so raw category counts are one each, attention=1 and
planning=1. Test a service selection by replacing the ID macro with its fixture ID. Test
both null candidates and unknown services using your generated report. Review item errors
before troubleshooting triggers. A stale report can still show previous candidates, which
is why the age trigger is separate.

Zabbix text-value and `vfs.file.contents` limits apply before discovery. Keep reports below
the applicable agent/server item limit (commonly 64 KiB for stored text); use
`output.pretty: false` to reduce JSON size and measure the actual generated file. The scanner's
16 MiB output bound does not imply that Zabbix can ingest a report that large. For large
installations split sources across separate scanner installations/host templates or extend
your collection design; this template intentionally has one report per host.

Automated tests validate UUIDs, export field structure, master dependencies, trigger targets,
and every JSONPath against representative report data, using the Zabbix `.first()` projection
semantics. The export is generated with stable UUIDs by
`python tools/generate_zabbix_template.py`. **A live Zabbix 7.0 import and UI verification has
not been performed in this development environment.** Complete that deployment check on your
server before relying on alert delivery.

References: [Zabbix 7.0 template export format](https://www.zabbix.com/documentation/7.0/en/manual/xml_export_import/templates),
[JSONPath](https://www.zabbix.com/documentation/7.0/en/manual/config/items/preprocessing/jsonpath_functionality),
[LLD](https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery),
[agent items](https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/zabbix_agent).
