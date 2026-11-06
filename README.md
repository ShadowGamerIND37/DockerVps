# TGD Bot — Docker VPS Hosting Bot

A Discord bot that hands out real Linux VPS instances backed by **Docker Engine**.
Users create a VPS from Discord, get a root password and an SSH port, and log in
over real SSH with `apt`/`apk`, cron, SFTP and a persistent disk.

This is **v11**. Version 10 ran the same bot on LXC/LXD; v11 replaces the LXD
backend with Docker Engine and keeps every Discord command, panel, animation and
database row intact. See [MIGRATION.md](MIGRATION.md) for the upgrade path.

---

## Contents

| Path | What it is |
| --- | --- |
| `bot.py` | The Discord application. UI, commands, permissions, panels, background loops. |
| `tgdbot/` | The infrastructure package. All Docker, database and service logic. |
| `tgdbot/docker_backend/` | Docker Engine adapters: containers, images, volumes, ports, security, SSH, backups, monitoring, nodes. |
| `tgdbot/database/` | Schema, migrations and the repository. |
| `tgdbot/services/` | Business logic: provisioning, lifecycle, backups, expiration, nodes. |
| `tgdbot/web/` | The web SSH terminal (Flask). |
| `tgdbot/scripts/` | Installer, database migration, healthcheck and acceptance test. |
| `webssh.html` | Terminal front-end. Unchanged design; the backend was rewritten. |
| `tests/` | Unit tests (158 of them). They run without a Docker daemon. |
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

Everything is environment-driven; see `.env.example` for all 125 variables.
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
| `WEBSSH_ENABLED` / `_HOST` / `_PORT` | `true` / `0.0.0.0` / `5000` | Web terminal. |
| `WEBSSH_ALLOW_ADHOC` | `false` | Allow connecting to hosts the bot didn't issue. |
| `SECRET_KEY` | generated | Encrypts stored root passwords. **Back this up.** |

The WebSSH HTTP listener starts directly from `main()` before Discord gateway
login; it does not wait for the gateway's `on_ready` event.

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
| Host devices / namespaces | **never** shared (no `pid=host`, `net=host`, `/dev` mounts) |
| Memory swap | disabled — `memswap_limit == mem_limit` |
| PID limit | enforced, to stop fork bombs taking the node down |
| `no-new-privileges` | **off, deliberately** — it breaks `su`/`sudo` and PAM root login |
| Root password | encrypted at rest, consumed by `chpasswd`, then truncated on disk |

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
node. Use Proxmox only on a trusted, preferably dedicated node. Ordinary and
public non-Proxmox VPS retain the existing unprivileged capability policy; the
Proxmox profile never applies to them.

### Host and runtime requirements

- Linux Docker node with hardware virtualization and `/dev/kvm`, plus `/dev/fuse`.
- At least **2 GB RAM and 32 GB storage** per Proxmox VPS. The public creation
  flow raises its normal disk minimum to 32 GB only after an authorized Proxmox
  selection.
- OpenSSL on the bot host. The generated root password is converted to a
  SHA-512 `PASSWORD_HASH`; plaintext `PASSWORD` is never placed in container
  metadata.
- A published TCP port mapped to container port **8006**. The DM contains the
  native `https://YOUR_SERVER_IP:port` console, not SSH/WebSSH instructions.
  Proxmox initially uses a self-signed certificate.
- Persistent `tgdbot_<name>` at `/var/lib/vz` and
  `tgdbot_<name>-pve-cluster` at `/var/lib/pve-cluster`.

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

**177 tests: 176 passed, 1 skipped.** They cover the security policy, port
allocation, schema migration, SSH verification retries and diagnostics, the
provisioning transaction and rollback, the web terminal's credential handling,
and backward compatibility across the UnixNodes → TGD Bot rebrand. They run
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
node and then an OS from the populated dropdowns. Admins can still supply the
creation options directly.

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
