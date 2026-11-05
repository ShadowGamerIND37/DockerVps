# UnixNodes — Docker VPS Hosting Bot

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
| `unixnodes/` | The infrastructure package. All Docker, database and service logic. |
| `unixnodes/docker_backend/` | Docker Engine adapters: containers, images, volumes, ports, security, SSH, backups, monitoring, nodes. |
| `unixnodes/database/` | Schema, migrations and the repository. |
| `unixnodes/services/` | Business logic: provisioning, lifecycle, backups, expiration, nodes. |
| `unixnodes/web/` | The web SSH terminal (Flask). |
| `unixnodes/scripts/` | Installer, database migration, healthcheck and acceptance test. |
| `webssh.html` | Terminal front-end. Unchanged design; the backend was rewritten. |
| `tests/` | Unit tests (99 of them). They run without a Docker daemon. |
| `.env.example` | Every supported environment variable, documented. |

`bot.py` never talks to the Docker SDK directly. It goes through
`unixnodes.platform.Platform` (imported as `INFRA`), which is an async facade over
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
git clone <your-fork> UnixNodes && cd UnixNodes

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env          # at minimum: DISCORD_TOKEN, YOUR_SERVER_IP, MAIN_ADMIN_ID

python -m unixnodes.scripts.migrate_database     # creates or upgrades vps.db
python bot.py
```

`migrate_database` takes a **timestamped copy** of any existing database before it
changes it, and writes a report to `migration_reports/`. It is idempotent: running
it twice is a no-op. `bot.py` runs the same migration on startup, so you can skip
the script — but running it first lets you read the report before the bot starts.

A helper installer is available if you prefer one command:

```bash
bash unixnodes/scripts/install.sh          # never runs destructive Docker cleanup
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

If `SECRET_KEY` is unset the bot generates `.secret_key` next to `bot.py` and
warns. Losing it makes every stored root password unreadable — the VPS keeps
running, but the bot can no longer hand out the password or open a web terminal.

---

## What a customer gets

A container running a real init process and `sshd`, with:

- **Root SSH login** on a dedicated host port, password authentication
- **Bash or ash**, `apt` / `apk`, cron, SFTP
- **A persistent named volume** (`unixnodes_<name>`) mounted as the container root,
  so data survives restart, resize, reinstall and port changes
- **RAM, CPU and PID limits** enforced by the kernel
- **Storage quota** where the host filesystem supports it

The image is initialised reproducibly on first boot: OpenSSH is installed once,
host keys are generated, `sshd_config` is written deterministically, cron starts,
and `sshd` runs in the foreground as PID 1's child.

---

## Security

Every customer container is created with:

| Control | Setting |
| --- | --- |
| Privileged mode | **never** — `privileged=False`, hardcoded |
| Capabilities | dropped (`DEFAULT_CAP_DROP`), keeping only the 8 a real OS needs |
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

`!vps-backup <name>` archives the persistent volume through a short-lived helper
container, records the config, image, ports and environment alongside it, and
stores a SHA-256 checksum.

```
!vps-backups [name]          # list
!vps-backup-verify <backup>  # re-check checksum and archive integrity
!vps-restore <name> <backup> # recreate the container from the archive
!vps-backup-prune            # apply retention (newest 4, or 30 days)
```

A backup is a **tar archive**, not a filesystem snapshot. It is crash-consistent,
not atomic, and the container is paused while it is taken.

---

## Testing

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

**99 tests, all passing.** They cover the security policy, port allocation,
schema migration, the provisioning transaction and rollback, and the web
terminal's credential handling. They run against a fake Docker client
(`tests/fake_docker.py`) and need **no Docker daemon**.

For a real host, after installation:

```bash
python -m unixnodes.scripts.healthcheck        # read-only environment report
python -m unixnodes.scripts.acceptance_test    # 33 checks across 25 test groups
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
   copy-on-write. A large volume takes a long time to archive.
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

## Support

- `!help` lists every command.
- `!docker-policy`, `!docker-quota` and `!node-check` describe the real state of
  your installation.
- `logs/` holds bot logs; `migration_reports/` holds database migration reports;
  `db_backups/` holds pre-migration database copies.
