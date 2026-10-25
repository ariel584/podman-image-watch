<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Linux and AlmaLinux deployment

Use Python 3.11 or newer. Install the matching Python and venv/pip packages for your AlmaLinux
release; avoid replacing the distribution's system Python. A package installation in a
virtual environment is isolated and requires no container runtime or running daemon.
The commands below assume `python3.11` is available and the optional Zabbix agent/group exists.
Substitute your installed 3.11+ executable as needed.

From a checkout:

```sh
sudo python3.11 -m venv /opt/podman-image-watch
sudo /opt/podman-image-watch/bin/python -m pip install .
sudo useradd --system --home-dir /var/lib/podman-image-watch --shell /sbin/nologin podman-image-watch
sudo install -d -m 0750 -o podman-image-watch -g zabbix /var/lib/podman-image-watch
sudo install -d -m 0755 /etc/podman-image-watch
sudo install -m 0644 examples/config.yaml /etc/podman-image-watch/config.yaml
sudo install -m 0644 examples/systemd/podman-image-watch.service /etc/systemd/system/
sudo install -m 0644 examples/systemd/podman-image-watch.timer /etc/systemd/system/
```

The example uses the `zabbix` primary group so newly replaced 0640 reports remain readable
by the agent. If no Zabbix integration is needed, use the service account's own group in
the service and directory ownership commands. No Zabbix Python dependency is installed.
The private state subdirectory is created as 0700, independently of the shared report.
Give the service account read/traverse permissions for the Quadlets and drop-ins it checks.
It never needs access to environment files, container storage, secrets or Podman's socket.

Validate and execute once before enabling the timer:

```sh
sudo -u podman-image-watch /opt/podman-image-watch/bin/podman-image-watch validate-config --config /etc/podman-image-watch/config.yaml
sudo systemd-analyze verify /etc/systemd/system/podman-image-watch.service /etc/systemd/system/podman-image-watch.timer
sudo systemctl daemon-reload
sudo systemctl start podman-image-watch.service
sudo journalctl -u podman-image-watch.service --no-pager
sudo systemctl enable --now podman-image-watch.timer
systemctl list-timers podman-image-watch.timer
```

`ExecStart` explicitly supplies `--output /var/lib/podman-image-watch/report.json` because
the CLI default is stdout. It also sets a private state path. A oneshot service cannot
start overlapping instances of itself; the application lock additionally protects manual
runs that share its state directory. `SuccessExitStatus=1` permits usable partial reports;
monitor `scan.health`, errors, unknown and age rather than relying only on unit status.
The 30-minute service timeout is an operational upper bound; for very large inventories,
review it against registry deadlines, source count and worker count.

The unit has no capabilities and uses NoNewPrivileges, PrivateTmp, PrivateDevices and a
read-only filesystem except its state directory. HTTPS/DNS require network access.
Do not add network isolation that prevents registry lookups. If enabling notifications,
supply environment variables via an administrator-controlled systemd EnvironmentFile;
keep credential files private and outside the repository. The Quadlet environment files
are never sourced by this utility.

For rootless Quadlets, set the source path explicitly. A user-level timer can use the same
oneshot command without User/Group/StateDirectory and without ProtectHome=true. Choose
user-writable report/state paths. A system service reading a home directory needs an
appropriate ProtectHome override and read/traverse permissions. Use a source ID to preserve
monitoring identity when moving a directory.

Keep SELinux enforcing. Inspect `ausearch -m AVC -ts recent`, check path contexts with
`ls -Z`, and use a narrowly scoped approved file-context or policy rule when needed.
Do not relabel unrelated container data or disable SELinux. Network and file access policy
depends on the service and agent domains on the target host.

To uninstall scheduling:

```sh
sudo systemctl disable --now podman-image-watch.timer
sudo systemctl stop podman-image-watch.service
sudo rm /etc/systemd/system/podman-image-watch.timer /etc/systemd/system/podman-image-watch.service
sudo systemctl daemon-reload
```

Remove the dedicated environment, account, configuration and state only after deciding
whether to retain reports/history. Stopping this utility does not alter containers.
