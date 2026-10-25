<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Linux and AlmaLinux deployment

Run these commands on the server after cloning/pulling the repository. Start in the checkout
that contains `pyproject.toml` and `examples/`. This guide installs the application in
`/opt/podman-image-watch`, with configuration in `/etc/podman-image-watch`, a report in
`/var/lib/podman-image-watch/report.json`, and private state in its `private/` subdirectory.
The source checkout can remain in your administrator account's directory; the installed
package does not depend on that directory after installation.

Only installation and service management require sudo. Scheduled scans use a dedicated
unprivileged account. Docker, Podman runtime access and Zabbix are not required.

## 1. Install Python

Check the OS and available Python first:

```sh
cat /etc/almalinux-release
python3 --version
```

Python 3.11 or newer is required. On AlmaLinux 9.4 or newer, install the Python 3.12 packages:

```sh
sudo dnf install -y python3.12 python3.12-pip
python3.12 --version
```

On AlmaLinux 10, the default Python is 3.12; install its native packages instead:

```sh
sudo dnf install -y python3 python3-pip
python3 --version
```

The remaining commands use `python3.12`. If your existing 3.11+ interpreter has a different
command, substitute that command when creating the virtual environment. Do not replace
AlmaLinux's system Python or install this application's dependencies into its system environment.
Older AlmaLinux releases may need a different Python package source; these package commands
are intended for current AlmaLinux 9/10 installations.

References: [AlmaLinux 9 Python 3.12 packages](https://errata.almalinux.org/9/ALSA-2024-8447.html),
[AlmaLinux 10 release notes](https://wiki.almalinux.org/release-notes/10.0.html),
[Enterprise Linux 9 Python and pip package commands](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/installing_and_using_dynamic_programming_languages/installing-and-using-dynamic-programming-languages).

## 2. Install the package and create its account

From the repository checkout:

```sh
sudo python3.12 -m venv /opt/podman-image-watch
sudo /opt/podman-image-watch/bin/python -m pip install .
/opt/podman-image-watch/bin/podman-image-watch --version

sudo useradd --system --user-group --no-create-home --home-dir /var/lib/podman-image-watch --shell /sbin/nologin podman-image-watch
sudo install -d -m 0750 -o podman-image-watch -g podman-image-watch /var/lib/podman-image-watch
sudo install -d -m 0755 /etc/podman-image-watch
sudo install -m 0644 examples/config-server.yaml /etc/podman-image-watch/config.yaml
```

These account/configuration commands are for a first installation. An existing installation
should use the update procedure below to preserve its configuration. Dependencies are installed
inside the virtual environment, not into AlmaLinux's system Python. You do not need to activate
the environment or install development/test dependencies on the server.

## 3. Review the configuration

```sh
sudoedit /etc/podman-image-watch/config.yaml
```

The supplied server configuration contains:

```yaml
version: 1
sources:
  quadlet_dirs:
    - id: system
      path: /etc/containers/systemd
output:
  format: json
  path: /var/lib/podman-image-watch/report.json
  pretty: false
  hide_variants: true
  hide_skipped: true
state:
  directory: /var/lib/podman-image-watch/private
defaults:
  updates:
    resolve_floating: true
rules:
  - match:
      quadlet: dnsmasq-acme.container
    ignore: true
notifications:
  enabled: false
```

Set the source path to the actual directory containing your `.container`/`.image` definitions
on this server, rather than the Downloads path used for local development. Keep or remove
the DNSmasq ignore rule according to your installation. Variants are hidden only in display
fields; original tags remain in JSON and variant matching remains enabled. Ignored services
remain in JSON as skipped but are hidden in table output.

Give `podman-image-watch` read/traverse permissions for the source directory, Quadlets and
drop-ins. Ordinary 0755 directories and 0644 files are readable already. If permissions are
more restrictive, grant access to those definitions only. The scanner never needs read access
to referenced environment files, container secrets, container storage or Podman's socket.
File symlinks escaping the source root are rejected; for a directory of such links, configure
the real source directory instead. See [configuration.md](configuration.md) for discovery limits.

## 4. Validate and run a manual scan

Validate configuration as the account that will run the service:

```sh
sudo -u podman-image-watch /opt/podman-image-watch/bin/podman-image-watch validate-config --config /etc/podman-image-watch/config.yaml
```

Preview a readable table in the console:

```sh
sudo -u podman-image-watch /opt/podman-image-watch/bin/podman-image-watch --config /etc/podman-image-watch/config.yaml --format table --output -
```

`--output -` overrides the configured report file, so the table cannot replace its JSON.
The scan still updates digest/cache state. On first observation, floating digests establish
baselines; they do not produce false digest-change events. A resolved alias describes the
registry's current target, rather than necessarily the version running on the host.

Generate the monitoring report using the configured JSON output:

```sh
sudo -u podman-image-watch /opt/podman-image-watch/bin/podman-image-watch --config /etc/podman-image-watch/config.yaml
sudo cat /var/lib/podman-image-watch/report.json
```

Exit 0 means a complete scan, even with available updates. Exit 1 means usable partial results;
inspect service reasons, scan diagnostics and notification errors. Exit 2 means a fatal error.
State is private (0700 directory/0600 files); new reports are 0640. If file creation fails,
check directory ownership and any SELinux denials before enabling scheduling.

## 5. Enable daily scans

Install the provided units from the repository checkout:

```sh
sudo install -m 0644 examples/systemd/podman-image-watch.service /etc/systemd/system/
sudo install -m 0644 examples/systemd/podman-image-watch.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/podman-image-watch.service /etc/systemd/system/podman-image-watch.timer
sudo systemctl daemon-reload
sudo systemctl start podman-image-watch.service
sudo journalctl -u podman-image-watch.service -n 50 --no-pager
sudo systemctl enable --now podman-image-watch.timer
systemctl list-timers podman-image-watch.timer
```

The timer runs daily with up to one hour of randomized delay and catches up missed runs.
The oneshot service exits after each scan. An inactive/dead service after a successful run
is normal; the timer remains active. A oneshot service does not overlap instances of itself;
the application lock also protects manual scans sharing the same state directory.

`ExecStart` explicitly supplies file output and the private state path. `SuccessExitStatus=1`
permits partial reports; monitor `scan.health`, errors, unknown and report age rather than
relying only on unit status. The 30-minute service timeout is an operational bound. For large
inventories, review it against registry deadlines, source count and worker count.

## 6. Optional Zabbix access

The scanner has no dependency on a Zabbix account or group. If Agent 2 is installed and uses
the `zabbix` account, grant it read access through the report group:

```sh
sudo usermod -aG podman-image-watch zabbix
sudo systemctl restart zabbix-agent2
sudo -u zabbix head -c 200 /var/lib/podman-image-watch/report.json
```

New report files inherit the service's primary group, so this access survives atomic
replacements. The private 0700 state directory remains inaccessible to the agent.
Follow [zabbix.md](zabbix.md) to import the template, set the report macro and verify discovery.

## Updating after future pulls

From the existing source checkout:

```sh
git pull --ff-only
sudo systemctl stop podman-image-watch.timer
sudo systemctl stop podman-image-watch.service
sudo /opt/podman-image-watch/bin/python -m pip install --upgrade .
/opt/podman-image-watch/bin/podman-image-watch --version
sudo systemctl start podman-image-watch.service
sudo systemctl start podman-image-watch.timer
```

A `git pull` alone does not update an installed package; the pip command installs the new
code. Preserve `/etc/podman-image-watch/config.yaml` and `/var/lib/podman-image-watch/private`.
If the example unit files changed, review/copy those files and run `systemctl daemon-reload`
before restarting the service. Do not replace customized configuration with an example.
If an update fails, correct it or restore the previous package before restarting scheduling.

## Hardening and rootless sources

The service has no capabilities and uses NoNewPrivileges, PrivateTmp, PrivateDevices and
a read-only filesystem except its state directory. HTTPS/DNS require network access.
If enabling notifications, supply environment variables via an administrator-controlled
systemd EnvironmentFile, kept private and outside the repository. Quadlet environment files
are never sourced by this utility.

For rootless Quadlets, set the source path explicitly. A user-level timer can use the same
oneshot command without User/Group/StateDirectory and without ProtectHome=true. Choose
user-writable report/state paths. A system service reading a home directory needs an
appropriate ProtectHome override and read/traverse permissions. Explicit source IDs preserve
monitoring identity when moving a directory.

Keep SELinux enforcing. Inspect `ausearch -m AVC -ts recent`, check contexts with `ls -Z`,
and use a narrowly scoped approved file-context or policy rule when needed. Do not relabel
unrelated container data or disable SELinux. Policy depends on the service/agent domains
on the target host. A successful CLI scan does not replace verification under the hardened
systemd service; test both before relying on scheduled reports.

## Uninstall scheduling

```sh
sudo systemctl disable --now podman-image-watch.timer
sudo systemctl stop podman-image-watch.service
sudo rm /etc/systemd/system/podman-image-watch.timer /etc/systemd/system/podman-image-watch.service
sudo systemctl daemon-reload
```

Remove the dedicated environment, account, configuration and state only after deciding
whether to retain reports/history. If you added the agent to the report group, remove that
membership when removing the account/group. Stopping this utility does not alter containers.
