# TGD Bot — Docker VPS Hosting Bot

A Discord bot that hands out real Linux VPS instances backed by **Docker Engine**.
Users create a VPS from Discord, get a root password and an SSH port, and log in
over real SSH with `apt`/`apk`, cron, SFTP and a persistent disk.

This is the Docker backend (currently **schema v13**). Version 10 ran the same
bot on LXC/LXD; the Docker releases replace the LXC backend while preserving the
Discord commands, panels, animations and database rows. See
[MIGRATION.md](MIGRATION.md) for the upgrade path.

---

## Contents

| Path | What it is |
| --- | --- |
| `bot.py` | The Discord application. UI, commands, permissions, panels, background loops. |
| `installer.sh` | Three-option interactive installer, migration runner and auto-updater menu. |
| `tgdbot/` | The infrastructure package. All Docker, database and service logic. |
| `tgdbot/docker_backend/` | Docker Engine adapters: containers, images, volumes, ports, security, SSH, backups, monitoring, nodes. |
| `tgdbot/database/` | Schema, migrations and the repository. |
| `tgdbot/services/` | Business logic: provisioning, lifecycle, backups, expiration, nodes. |
| `tgdbot/web/` | The web SSH terminal (Flask). |
| `tgdbot/scripts/` | Installer, database migration, healthcheck and acceptance test. |
| `webssh.html` | Terminal front-end. Unchanged design; the backend was rewritten. |
| `tests/` | Unit tests (218 of them). They run without a Docker daemon. |
| `.env.example` | Every supported environment variable, documented. |

`bot.py` never talks to the Docker SDK directly. It goes through
`tgdbot.platform.Platform` (imported as `INFRA`), which is an async facade over
synchronous services that run on a bounded worker pool.

---

## Requirements

- Linux host, x86_64 or arm64
- Python 3.10+
- Docker Engine 24+ with the daemon running
- A Discord bot token
- For enforceable disk quotas: XFS (project quotas) or Btrfs, and the VPS
  directory on that filesystem

---

## Install

```
bash <(curl -fsSL https://raw.githubusercontent.com/ShadowGamerIND37/DockerVps/refs/heads/main/installer.sh)
```


```bash
git clone <your-fork> TGD Bot && cd TGD Bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env          # at minimum: DISCORD_TOKEN, YOUR_SERVER_IP, MAIN_ADMIN_ID

python -m tgdbot.scripts.migrate_database     # creates or upgrades vps.db
python bot.py
```

`migrate_database` takes a **timestamped copy** of any existing database before it
changes it, and writes a report to `migration_reports/`. It is idempotent: running
it twice is a no-op. `bot.py` runs the same migration on startup, so you can skip
the script — but running it first lets you read the report before the bot starts.

A helper installer is available if you prefer one command:

```bash
bash tgdbot/scripts/install.sh          # never runs destructive Docker cleanup
```

Or use the menu-driven installer from the repository root:

```bash
sudo bash installer.sh
```

It provides three options:

1. **Install Files Of Bot And Run** — asks for the Discord bot token, admin ID
   (`MAIN_ADMIN_ID`) and owner ID(s) (`OWNER_IDS`). Press **Enter** at any prompt
   to leave that `.env` value unchanged. The token is entered without echoing;
   existing `.env` settings are preserved, then the regular installer starts or
   restarts the `tgdbot` service.
2. **Migration** — runs the existing backed-up, idempotent database migration.
3. **Auto Updater** — check/apply an update now, enable an hourly systemd timer,
   or disable it. Updates use `git pull --ff-only`; a dirty worktree is skipped,
   never reset or overwritten. A successful update installs requirements, runs
   the migration and restarts the service. Automatic updates require a Git
   checkout with a configured upstream and root/systemd access.

The non-interactive equivalents are `bash installer.sh --install`,
`--migrate`, `--auto-update`, `--enable-auto-update` and
`--disable-auto-update`.

---

## First run

1. The bot registers a local Docker node (id 1) automatically.
2. `!node-check` shows the engine version, storage driver and available resources.
3. `!docker-quota` tells you **honestly** whether your host can enforce disk
   quotas. If it cannot, the bot says so instead of pretending.
4. `!docker-policy` prints the security policy applied to every customer container.
5. `!vps-create` creates the first VPS.

---

## Configuration

Everything is environment-driven; see `.env.example` for all variables.
The ones that matter most:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token. Required. |
| `YOUR_SERVER_IP` | — | Public IP advertised for SSH connections. |
| `MAIN_ADMIN_ID` | — | Owner Discord ID. |
| `DOCKER_HOST_URL` | `unix:///var/run/docker.sock` | Local engine endpoint (falls back to `DOCKER_HOST`). |
| `DOCKER_PUBLISH_IP` | `0.0.0.0` | Host address to bind published ports to. |
| `DOCKER_SSH_PORT_START` / `_END` | `22000` / `22999` | Pool for VPS SSH ports. |
| `DOCKER_PORT_RANGE_START` / `_END` | `25000` / `26999` | Pool for user-published ports. |
| `DOCKER_RESERVED_PORTS` | — | Host ports the allocator must never hand out. |
| `DOCKER_DEFAULT_RAM` / `_CPU` / `_STORAGE` | `1024` / `1` / `10` | Default VPS size. |
| `WEBSSH_ENABLED` / `_HOST` / `_PORT` | `true` / `0.0.0.0` / `5000` | Text WebSSH terminal for ordinary VPSes. |
| `WEBSSH_ALLOW_ADHOC` | `false` | Allow connecting to hosts the bot didn't issue. |
| `PROXMOX_CONSOLE_ENABLED` / `_HOST` / `_PORT` | `true` / `0.0.0.0` / `5001` | Native Proxmox UI/noVNC gateway, separate from WebSSH. |
| `PROXMOX_CONSOLE_PUBLIC_HOST` | `YOUR_SERVER_IP` | Public IP/DNS host written into Proxmox console links. |
| `PROXMOX_CONSOLE_UPSTREAM_HOST` | `127.0.0.1` | Server-side address of the published Proxmox HTTPS port; never advertised. |
| `SECRET_KEY` | generated | Encrypts stored root passwords. **Back this up.** |

The WebSSH HTTP listener starts directly from `main()` before Discord gateway
login; it does not wait for the gateway's `on_ready` event. The Proxmox console
listener starts at the same point, independently, on port `5001` by default.

If `SECRET_KEY` is unset the bot generates `.secret_key` next to `bot.py` and
warns. Losing it makes every stored root password unreadable — the VPS keeps
running, but the bot can no longer hand out the password or open a web terminal.

---

## What a customer gets

A container running a real init process and `sshd`, with:

- **Root SSH login** on a dedicated host port, password authentication
- **Bash or ash**, `apt` / `apk`, cron, SFTP
- **A persistent named volume** (`tgdbot_<name>`) mounted at `/var/lib/tgdbot`,
  plus a private writable-root image snapshot used during recreation, so data
  survives restart, resize and port changes (reinstall intentionally wipes it)
- **RAM, CPU and PID limits** enforced by the kernel
- **Storage quota** where the host filesystem supports it; the host quota covers
  `/var/lib/tgdbot`, while Docker's writable root layer is separate

Docker rejects a named volume whose destination is `/`. The data volume therefore
lives at `/var/lib/tgdbot`; before a container is recreated, the lifecycle commits
its writable root layer to a private `tgdbot-rootfs/<name>:latest` image and
creates the replacement from that image. This preserves packages, `/etc`, home
directories and SSH host keys without privileged mode or `CAP_SYS_ADMIN`.

The image is initialised reproducibly on first boot: OpenSSH is installed once,
host keys are generated, `sshd_config` is written deterministically, cron starts,
and `sshd` runs in the foreground as PID 1's child.

---

## Security

Every customer container is created with:

| Control | Setting |
| --- | --- |
| Privileged mode | off by default (`DOCKER_PRIVILEGED=false`); enabling it logs a loud warning |
| Capabilities | dropped (`DEFAULT_CAP_DROP`), keeping the 10 required by package management, PAM and OpenSSH |
| Docker socket | **never** mounted into a customer container |
| Host devices / namespaces | never shared implicitly; the sole device exception is an admin-selected `/dev/kvm` mapping |
| KVM | admin choice at creation; disabled means no `/dev/kvm` mapping, enabled maps `/dev/kvm:/dev/kvm` |
| AppArmor | admin choice at creation; enabled uses the configured profile or `docker-default`, disabled uses `apparmor=unconfined` |
| Memory swap | disabled — `memswap_limit == mem_limit` |
| PID limit | enforced, to stop fork bombs taking the node down |
| `no-new-privileges` | **off, deliberately** — it breaks `su`/`sudo` and PAM root login |
| Root password | encrypted at rest, consumed by `chpasswd`, then truncated on disk |

The guided admin `/create` flow asks for KVM and AppArmor after OS selection.
Regular users never receive these controls and cannot submit overrides through
another creation path; the provisioning service rejects such requests. Ordinary
VPS defaults are KVM disabled and AppArmor enabled. The resolved booleans are
stored in schema v13 and reused by recreate, reinstall, clone and migration.
KVM-disabled creation is refused if privileged Docker mode would expose host
devices implicitly.

The root password is **never** written to an environment variable, container
label, `docker inspect` metadata or a log line. It is handed to the container
through a read-only init mount plus a per-VPS secret directory that `chpasswd`
consumes and truncates.

Remote nodes are reachable over **SSH transport** or **mutual TLS** only. An
unauthenticated `tcp://…:2375` endpoint is rejected outright.

The web terminal requires a token the bot issued. The browser is never given the
root password: it receives `{host, port, username, label}` and the server resolves
the credential itself. Responses carry `Content-Security-Policy`, `no-store`,
`nosniff` and `X-Frame-Options`; links are rate-limited per IP, PIN-protected
links lock after repeated wrong PINs, and sessions expire.

---

## Proxmox VE (separately gated workload)

Proxmox is available through the community **`dockurr/proxmox`** image, pinned by
default to `dockurr/proxmox:9.2.20`. This is not an official Proxmox-maintained
Docker image. It runs Proxmox's native systemd stack and is deliberately **not**
handled as another SSH-oriented Linux rootfs.

### Access grants

Administrators can always select and create Proxmox VPS. A non-admin needs an
explicit Discord-user grant:

```text
/proxmox give userid:123456789012345678
/proxmox remove userid:123456789012345678
/proxmox list
```

Prefix equivalents are `!proxmox give 123456789012345678` and
`!proxmox remove 123456789012345678`. Grants are checked in the selectors and
again by the provisioning service, so a crafted interaction or unapproved image
reference cannot bypass them. Revocation blocks new selection/creation; it does
not delete or lock an existing VPS.

**Security boundary:** the Proxmox image requires privileged Docker mode. A grant
therefore authorizes that user to create this privileged workload on the selected
node, but does **not** authorize changing KVM or AppArmor. Granted non-admins use
the fixed Proxmox policy (KVM enabled, AppArmor disabled); only admins can select
security overrides. Use Proxmox only on a trusted, preferably dedicated node.
Ordinary and public non-Proxmox VPS retain the existing unprivileged capability
policy; the Proxmox profile never applies to them.

### Host and runtime requirements

- Linux Docker node with hardware virtualization and `/dev/kvm`, plus `/dev/fuse`.
  Proxmox receives an explicit `/dev/kvm:/dev/kvm` mapping. KVM-disabled Proxmox
  creation is rejected because the supported privileged runtime cannot provide
  a safe no-KVM guarantee.
- At least **2 GB RAM and 32 GB storage** per Proxmox VPS. The public creation
  flow raises its normal disk minimum to 32 GB only after an authorized Proxmox
  selection.
- OpenSSL on the bot host. The generated root password is converted to a
  SHA-512 `PASSWORD_HASH`; plaintext `PASSWORD` is never placed in container
  metadata.
- A published TCP port mapped to container port **8006**. The DM and management
  panel contain a short-lived **Proxmox console gateway** link (by default
  `http://<YOUR_SERVER_IP>:5001/c/<token>`), not SSH/WebSSH instructions. The gateway
  connects from the bot host to the published port over server-side HTTPS; the
  browser never supplies an upstream target. Proxmox initially uses a
  self-signed certificate on that upstream hop. Before probing, the bot
  checks the node certificate and key as `www-data`; an unusable pair waits
  for the pmxcfs mount, IPC and quorum, then gets a bounded `pvecm updatecerts
  --force` repair followed by a `pveproxy` restart. If a cloud DNS search
  suffix would make Proxmox's certificate CN exceed OpenSSL's 64-character
  limit, the repair temporarily omits that suffix and restores the original
  resolver file before validation and restart. Healthy certificates are not
  rotated, and persistent repair output is returned through the normal
  provisioning rollback error. The readiness probe
  then starts over HTTP to match the image healthcheck, follows Proxmox's native
  HTTP→HTTPS redirect, and requires a final HTTPS 200 before reporting ready.
- Persistent `tgdbot_<name>` at `/var/lib/vz` and
  `tgdbot_<name>-pve-cluster` at `/var/lib/pve-cluster`.

### Proxmox UI and guest boot menus

The pinned image already contains an installed Proxmox VE host; there is no
separate Proxmox host-installer boot menu to complete. Open the gateway link in
the credential DM (or choose **Proxmox UI** in the management panel), then log
in as `root` with the `Linux PAM standard authentication` realm. The link swaps
a short-lived token for an HttpOnly session cookie and redirects to the native
Proxmox application. The proxy preserves its root-relative UI/API paths and
relays binary noVNC WebSockets; it does not render a text-only substitute. On
an HTTP gateway, it adds noVNC's native `encrypt=0` query override to the
console iframe so the browser uses `ws://`; the upstream hop stays HTTPS/WSS.
Proxmox's xterm.js console already follows the page scheme. Use HTTPS for
public deployments so Proxmox's secure authentication cookies are accepted by
remote browsers.

**Remote/public access:** `PROXMOX_CONSOLE_HOST=0.0.0.0` makes the gateway
listen for remote browsers, while `PROXMOX_CONSOLE_PUBLIC_HOST` controls only
the host printed in links. Leave the public host blank to inherit
`YOUR_SERVER_IP`; set it explicitly when the console uses a different DNS name.
For example, with `YOUR_SERVER_IP=console.example.com`, the bot issues
`http://console.example.com:5001/c/<token>`. With an HTTPS reverse proxy on the
standard port, set `PROXMOX_CONSOLE_URL_FORMAT=https://{SERVER_IP}` so links use
`https://console.example.com/c/<token>` instead.

The bot cannot allocate a public IP or create DNS. Configure the address and
network path on the host yourself:

1. Replace `YOUR_SERVER_IP=127.0.0.1` in `.env` with a real public IP or DNS
   name. If `PROXMOX_CONSOLE_PUBLIC_HOST=127.0.0.1` already exists, remove it or
   replace it; an explicit setting takes precedence over `YOUR_SERVER_IP`.
2. For direct public-IP access, keep `PROXMOX_CONSOLE_HOST=0.0.0.0` and allow
   inbound TCP `5001` in the host/cloud firewall. Behind NAT, forward TCP `5001`
   to the bot host. A private, CGNAT, or loopback address is not remotely
   reachable merely because it appears in a link.
3. For a production domain, create A/AAAA records and terminate **HTTPS** at a
   reverse proxy. Proxy HTTP requests and WebSocket upgrades to the gateway,
   then set `PROXMOX_CONSOLE_URL_FORMAT=https://{SERVER_IP}`. Plain public HTTP
   exposes Proxmox credentials and console traffic; HTTPS is strongly required
   for untrusted networks and may be required by browser secure-cookie rules.
4. Restart the bot after changing `.env`, then request a fresh console link.

Keep `PROXMOX_CONSOLE_UPSTREAM_HOST=127.0.0.1` for a local node. The browser
never chooses this upstream, and the native Proxmox port does not need to be
exposed just to make the gateway link work. The gateway accepts only Proxmox VE
records and enforces owner, shared-user and administrator authorization when
issuing links.

### Cloudflare quick tunnel (Proxmox only)

Proxmox VPS management panels also show a **Cloudflare** button. It runs the
Cloudflare package setup inside that VPS, installs `cloudflared`, and starts:

```sh
nohup cloudflared tunnel --url https://localhost:8006 --no-tls-verify \
  > /root/cloudflared.log 2>&1 &
```

The button shows live stages for package updates, repository setup, the
`cloudflared` installation, tunnel startup and URL discovery. When the log
contains Cloudflare's **Your quick Tunnel has been created!** message, the bot
returns the resulting `https://…trycloudflare.com` URL in the ephemeral
response. The VPS must be running and have outbound network access.

This URL is public: anyone who has it can reach the Proxmox login page, so keep
it private and use the Proxmox credentials from your credential DM. Quick
tunnels are temporary; after a VPS restart, reinstall or stopped `cloudflared`
process, click **Cloudflare** again to create a fresh tunnel. The setup runs in
the guest container, never on the Discord bot host, and is not offered for
ordinary Linux VPS images.

To install an operating system **inside a guest VM**:

1. Open the Proxmox UI and upload the ISO to a storage's **ISO Images** page.
2. Choose **Create VM**, select the ISO on the **OS** tab, and finish the VM
   wizard.
3. Start the VM and open its **Console**. Proxmox's noVNC console displays the
   graphical ISO installer, BIOS and boot menu. It uses the existing Proxmox
   web connection; no additional WebSSH port is required.

WebSSH remains a text-only terminal for ordinary Linux VPSes. It cannot render
Proxmox's graphical UI or a guest VM's display.

Power actions, resize/recreate, port republication, reinstall and deletion retain
the Proxmox runtime and volume contract. Regular one-volume backup/restore,
clone and cross-node migration are rejected rather than producing an incomplete
Proxmox copy; use Proxmox's own backup tools inside the console. Changing between
Proxmox and a regular OS requires a new VPS.

### Custom Docker images

Only admins can use `/image-add`. The command now pulls and inspects the image,
requires a Linux image, stores the approval, and refreshes both creation menus.
Approved images must still provide a shell and a package manager supported by
the VPS bootstrap (APT, APK, DNF or YUM); distroless, Windows and other
non-Linux images are not VPS rootfs candidates. Custom Proxmox references are
rejected so they cannot bypass the built-in Proxmox grant/runtime checks.

---

## Multi-node

Register remote Docker hosts as nodes:

```
!node                  interactive node manager
!node create           add a node (form) — set the transport to ssh://user@host
                       or tls://host:2376; leave the URL blank for the local node
!node list             inventory
!node status <id>      deep health check
!node edit <id>        change name / location / capacity / endpoint
!node delete <id>      remove a node
!node-check <id>       engine version, storage driver, resources, quota support
!docker-quota <id>     what this node can actually enforce
```

Each node is probed for capacity, and VPS creation picks a node with room.
Containers are tagged so the bot only ever touches the ones it manages.

---

## Backups

`!vps-backup <name>` archives the persistent data volume and exports the
writable root filesystem as a companion Docker-image archive through short-lived
helper operations. It records the config, image, ports and environment alongside
them, and stores SHA-256 checksums. New restores require and reload both parts;
a missing required companion is rejected before the VPS is stopped or its data
is touched. Older backups whose volume archive is a legacy whole-rootfs export
are imported as a rootfs image instead of being redirected into the data mount;
that compatibility path leaves the existing data volume untouched.

```
!vps-backups [name]          # list
!vps-backup-verify <backup>  # re-check checksum and archive integrity
!vps-restore <name> <backup> # recreate the container from the archive
!vps-backup-prune            # apply retention (newest 4, or 30 days)
```

A backup is a set of **tar archives**, not an atomic filesystem snapshot. The
workload is paused (or stopped in stop mode) while both parts are captured; the
result is crash-consistent, not a block-level snapshot.

---

## Testing

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

**218 tests: 217 passed, 1 skipped.** They cover the security policy, port
allocation, schema migration, SSH verification retries and diagnostics, the
provisioning transaction and rollback, the web terminal's credential handling,
the Proxmox native HTTP/noVNC gateway, and backward compatibility across the
UnixNodes → TGD Bot rebrand. They run
against a fake Docker client (`tests/fake_docker.py`) and need **no Docker
daemon**.

For a real host, after installation:

```bash
python -m tgdbot.scripts.healthcheck        # read-only environment report
python -m tgdbot.scripts.acceptance_test    # 33 checks across 25 test groups
```

`acceptance_test.py` creates, connects to, resizes, backs up, restores, suspends
and deletes a real container, and deliberately fails provisioning to prove rollback
leaves no orphans. It needs a working Docker daemon.

---

## Known limitations

These are real, and the bot reports them rather than hiding them:

1. **Disk quotas depend on the host filesystem.** XFS project quotas and Btrfs
   qgroups are supported. On ext4 or overlayfs the limit is recorded and shown to
   users but **cannot be enforced**. `!docker-quota` says which case you are in.
2. **Backups are archives, not snapshots.** They are not atomic and not
   copy-on-write. A large volume or writable rootfs takes a long time to archive.
3. **`no-new-privileges` is disabled** so `sudo` and root SSH login work. This is
   a deliberate trade-off, not an oversight.
4. **Containers share the host kernel.** Unlike LXD or a real VM, a kernel
   exploit is a node-wide escape. If you need hard multi-tenant isolation, run
   one node per tenant.
5. **Legacy LXC VPS are not converted.** Rows from v10 are tagged
   `backend='lxc-legacy'` and left alone; their containers are never deleted or
   silently re-labelled as Docker records.
6. **The web terminal is a bridge, not a shell emulator.** It carries a PTY over
   WebSockets to the container's SSH port; it does not run a shell itself.

---

## Discord commands

Every one of the **89 top-level prefix commands** also has a slash-command equivalent.
The Proxmox group adds the `/proxmox give`, `/proxmox remove`, and `/proxmox list`
subcommands.
The bot syncs the application-command tree on startup; prefix commands and their
aliases remain available. The existing `/manage`, `/myvps`, `/ping`,
`/dashboard`, and `/help` interfaces are unchanged.

Use `/create` with no options for the guided form. After submitting it, choose a
node, an OS, and then the admin-only KVM/AppArmor settings from the populated
controls. Admins can still supply the plan options directly; the security step
remains part of admin creation. Public self-service creation uses fixed policy
defaults and never shows these controls.

Most slash options map one-to-one to their prefix arguments. Two commands accept
a single optional string for the remaining arguments because Discord does not
support variadic (`*args`) slash options:

- `/vps action:create` — equivalent to `!vps create`.
- `/ports subcommand:add args:"1 8080"` — equivalent to `!ports add 1 8080`.

The `VPS info` user context menu is also registered.

---

## Support

- `!help` lists every command.
- `!docker-policy`, `!docker-quota` and `!node-check` describe the real state of
  your installation.
- If provisioning reports `Permission denied` for `/var/lib/unixnodes` or
  another configured host directory, run `sudo bash tgdbot/scripts/install.sh`.
  The installer reads the paths from `.env` without executing it and creates or
  chowns those exact directories. It does not move or delete existing data.
- `logs/` holds bot logs; `migration_reports/` holds database migration reports;
  `db_backups/` holds pre-migration database copies.
