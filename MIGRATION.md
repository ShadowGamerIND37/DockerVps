# Migration Guide — v10 (LXC/LXD) → v11 (Docker Engine)

This document covers upgrading an existing TGD Bot v10 installation to v11.

**Read this before you start.** The database migration is automatic, safe and
reversible. The infrastructure migration is **not** automatic — your existing LXC
containers are deliberately left alone, and nothing is deleted for you.

---

## What changes

| | v10 | v11 |
| --- | --- | --- |
| VPS backend | LXC/LXD via `lxc` CLI over SSH | Docker Engine via the Docker SDK |
| Root filesystem | LXD storage pool + container | Named volume `tgdbot_<name>` mounted at `/` |
| Ports | LXD proxy devices | Docker port publishing, tracked in `docker_ports` |
| Disk quota | LXD `limits.disk` | XFS project quota or Btrfs qgroup — **where supported** |
| Snapshots | LXD snapshots | Volume tar archives (not atomic snapshots) |
| Schema version | 10 | 11 |

The Discord interface does not change. Every v10 command still exists under the
same name.

---

## Before you start

1. **Back up the database** (the migrator does this too, but keep your own copy):
   ```bash
   cp vps.db "vps.db.pre-v11.$(date +%Y%m%d-%H%M%S)"
   ```

2. **Back up `.env`**, and note your `SECRET_KEY`. If you have no `SECRET_KEY`
   but you do have a `.secret_key` file, keep that file — it is what decrypts the
   stored root passwords.

3. **Record your LXC inventory**, so you can compare afterwards:
   ```bash
   lxc list --format=csv -c n,s,4 > lxc-inventory.txt
   ```

4. **Stop the bot**:
   ```bash
   systemctl stop tgdbot
   ```

5. **Install Docker Engine** on every node that will host VPS:
   ```bash
   bash tgdbot/scripts/install_docker.sh
   docker version
   ```
   The installer does not run destructive cleanup commands. If you have an old
   Docker install, review before running.

6. **Plan for quota enforcement.** Disk quotas only work on XFS (with `pquota`)
   or Btrfs. Check what `/var/lib/docker/volumes` lives on:
   ```bash
   df -T /var/lib/docker/volumes
   ```
   If it reports `ext4` or `overlay`, quotas **cannot** be enforced. The bot will
   tell you this in `!docker-quota` rather than pretending. To get enforcement,
   move `/var/lib/docker` onto an XFS filesystem mounted with `-o pquota` before
   creating VPS.

---

## One database path

The Discord UI and the Docker services share **`DATABASE_PATH`**. The default
is the existing v10 **`vps.db`** file; an explicit `DATABASE_PATH` in `.env` is
honored. This prevents the UI from migrating one file while provisioning opens
another and fails with `no such table: vps`.

If an earlier v11 build created a separate `vps_data.db`, **back up both files
and check which one contains your users and VPS rows before restarting**. Keep
`DATABASE_PATH` pointed at that file, or set it to `./vps.db` for the original
v10 database. Do not delete either file as part of this fix.

---

## Upgrading

```bash
git pull                                    # or extract the v11 release over the tree
source .venv/bin/activate
pip install -r requirements.txt

python -m tgdbot.scripts.migrate_database
```

Read the output. It tells you exactly what happened. Then:

```bash
python bot.py
```

`bot.py` runs the same migration on startup, so the script is optional — but
running it first lets you inspect the report before the bot touches anything.

### What the migrator does

1. **Copies the database** into `db_backups/` with a timestamp.
2. **Bootstraps base tables** if the file is empty (fresh installs run the script
   before the bot has ever started).
3. **Adds columns**, never dropping any. On `vps`: `backend`,
   `docker_container_id`, `docker_image`, `volume_name`, `ssh_host_port`,
   `pid_limit`, `quota_method`, `quota_enforced`, `quota_project_id`,
   `provision_state`, `restart_count`, `last_health_ts`. On `nodes`: `transport`,
   `docker_host`, `docker_tls_ca`/`_cert`/`_key`/`_verify`, `docker_timeout`,
   `enabled`, `last_check`, `docker_version`, `os_info`, `cpu_info`,
   `ram_total_mb`, `ram_available_mb`, `storage_total_gb`, `storage_free_gb`,
   `containers_running`, `containers_stopped`, `quota_method`, `quota_supported`.
4. **Creates tables**: `docker_ports`, `docker_backups`, `docker_images`,
   `resource_plans`, `storage_quotas`, `migration_reports`.
5. **Seeds** the image catalogue (7 images) and resource plans.
6. **Tags legacy rows**: every existing `vps` row gets `backend='lxc-legacy'`.
   Nothing else about them changes.
7. **Verifies** with `PRAGMA integrity_check` and rolls the whole transaction back
   if the result is not `ok`.

Every step is idempotent. Running it twice changes nothing the second time.

### What the migrator does NOT do

- It does **not** delete or modify any LXC container.
- It does **not** convert an `lxc-legacy` row into a Docker record. Doing so would
  mean claiming a container exists on an engine that has never seen it.
- It does **not** touch `port_forwards`, `port_allocations`, `audit_log`,
  `settings`, `admins`, `webssh_tokens`, `usage_snapshots`, `host_snapshots`,
  `cooldowns` or `device_tracking`. Your users, VPS rows, port slots, backups,
  audit history, settings and API keys all survive untouched.

---

## After the upgrade

### Verify

```
!node-check          engine version, storage driver, resources
!docker-quota        what the host can actually enforce
!docker-policy       the security policy applied to every container
!images              the approved image catalogue
```

Then create a test VPS and confirm you can SSH in as root.

### Legacy VPS

Existing v10 VPS appear in the bot with a `legacy LXC/LXD record` notice. They
are visible but cannot be started, stopped, resized or backed up by the Docker
backend, because that backend cannot see them. You have two options:

**A. Leave them.** They keep running under LXD, managed by hand. Nothing breaks.

**B. Rebuild them as Docker VPS.** For each one:

1. Create a new VPS with the same name on the Docker backend.
2. Copy the data across — `rsync` over SSH, or restore from a v10 backup.
3. Verify the new VPS, then release the old LXC container yourself:
   ```bash
   lxc stop <name> && lxc delete <name>
   ```
4. Update the database row, or delete it and let the new VPS take the name.

The bot will not do step 3 for you. Deleting customer data is not something to
automate.

### Port accounting

`get_user_used_ports()` now counts live rows in `docker_ports` **plus** any
pre-v11 `port_forwards` rows, so a user who had LXC-era forwards cannot slip past
their quota. Once you have migrated or deleted the legacy forwards, only the
Docker reservations count.

---

## Rebranding (UnixNodes → TGD Bot)

The project was renamed from **UnixNodes** to **TGD Bot**, and the developer
identity from **Hopingboyz** to **ShadowGamerIND**.

Display strings — bot name, banner, embeds, docs, package path, systemd unit —
were changed freely. **Runtime identifiers could not be**, because of two hard
Docker constraints:

| Identifier | Why it cannot just be renamed |
| --- | --- |
| Container labels (`unixnodes.*`) | Labels are immutable once a container exists. |
| Volume names (`unixnodes_*`) | Docker has **no volume rename**. Only copy-then-delete works. |

So the rebrand matches both old and new identifiers instead of rewriting them.
**No volume is copied, moved or renamed**, and no container is recreated as part
of this change. Existing VPS keep running and keep their data.

### What still resolves

| Thing | Current | Pre-rebrand, still matched |
| --- | --- | --- |
| Managed-container filter | `tgdbot.managed=true` | `unixnodes.managed=true` |
| Container / owner / VPS labels | `tgdbot.container`, `tgdbot.owner`, `tgdbot.vps-id` | the `unixnodes.*` equivalents |
| Volume prefix | `tgdbot_` | `unixnodes_` |

The resolution order for a volume is:

1. the `volume_name` recorded in the database (authoritative),
2. `tgdbot_<container>`,
3. `unixnodes_<container>`, if that volume exists.

New VPS are always created under `tgdbot_`. A pre-rebrand VPS is only rewritten
if you act on it — and even then its existing volume is reused, not recreated:

- **start / stop / restart / resize / suspend** — reuse the existing volume as-is.
- **recreate** (ports, resize, reinstall without wipe) — reattaches the *same*
  volume; the database keeps the name it already had.
- **reinstall with wipe** — deletes the old volume and creates a fresh one under
  `tgdbot_`. The database is updated. This is the only path that changes the name.
- **clone** — copies the source volume to a new `tgdbot_` name.
- **migrate** — archives, transfers, restores; the target volume is `tgdbot_`.

### Orphan detection

`!docker-orphans` compares live containers against the database. Containers
carrying either label generation count as managed, so a pre-rebrand container is
**not** reported as an orphan.

The scan only ever looks at containers carrying one of those two labels
(`orphans()` → `orphaned_containers()` → `list_managed()`). A container with no
managed label is therefore neither managed nor an orphan — it is simply not
ours, and the widened filter never adopts it. `test_unmanaged_container_is_never_claimed`
pins that.

### Verifying after an upgrade

```bash
# Both label generations appear, nothing is orphaned
!docker-policy
!docker-orphans

# A pre-rebrand VPS still lists, and its volume is the old one
!vps-info <name>
docker volume ls | grep -E 'tgdbot_|unixnodes_'
```

Pinned by `tests/test_rebrand_compat.py` (18 tests), including
`test_label_list_is_ored_not_anded`, which keeps the test fake honest about the
Docker API's OR semantics for repeated filter keys.

### Optional: retiring the legacy names

Nothing requires this. If you want every volume under `tgdbot_`, clone each VPS
to a new name and delete the original during a maintenance window — that copies
the data. There is no in-place rename, and the bot will not attempt one.

---

## Rolling back

The database is a single file, so rollback is a restore:

```bash
systemctl stop tgdbot
cp "db_backups/<timestamp>-vps.db" vps.db
git checkout <v10-tag>
pip install -r requirements.txt
systemctl start tgdbot
```

Note that `ALTER TABLE ... ADD COLUMN` cannot be undone in SQLite, which is why
the backup is the rollback path. A v11 database restored over a v10 install will
simply have extra columns the old code ignores — but the reverse (v10 code
reading a v11 database) is untested, so restore the file rather than downgrading
in place.

Any Docker containers created by v11 are unaffected by a database rollback. Find
and clean them up by label:

```bash
docker ps -a --filter label=tgdbot.managed=1
```

---

## Environment variables

`.env.example` documents all of them. New in v11:

| Variable | Meaning |
| --- | --- |
| `DOCKER_HOST_URL` | Local engine endpoint. Falls back to `DOCKER_HOST`, then the Unix socket. |
| `DOCKER_PUBLISH_IP` | Host address published ports bind to. Default `0.0.0.0`. |
| `DOCKER_SSH_PORT_START` / `DOCKER_SSH_PORT_END` | Pool for VPS SSH ports. Default `22000` / `22999`. |
| `DOCKER_PORT_RANGE_START` / `DOCKER_PORT_RANGE_END` | Pool for user-published ports. Default `25000` / `26999`. |
| `DOCKER_RESERVED_PORTS` | Host ports never to hand out. |
| `DOCKER_VOLUME_PREFIX` | Volume naming prefix. Default `tgdbot_`. |
| `DOCKER_CPU_PERIOD` | CPU quota period in microseconds. Default `100000`. |
| `DOCKER_DEFAULT_PID_LIMIT` | Per-container PID ceiling. Default `512`. |
| `DOCKER_DEFAULT_IMAGE` | Image used when none is specified. Default `ubuntu:24.04`. |
| `DOCKER_ENFORCE_DISK_QUOTA` | Attempt XFS/Btrfs quota enforcement. Default `true`. |
| `DOCKER_BACKUP_DIR` | Where volume archives are written. Default `/var/backups/tgdbot`. |
| `DOCKER_BACKUP_KEEP` / `DOCKER_BACKUP_RETENTION_DAYS` | Retention. Default `4` / `30`. |
| `DOCKER_BACKUP_INTERVAL_HOURS` | How often the scheduled backup pass runs. |
| `SSH_CONTAINER_PORT` | The port sshd listens on inside the container. Default `22`. |
| `WEBSSH_ALLOW_ADHOC` | Allow the terminal to reach hosts the bot didn't issue. Default `false`. |
| `BACKUP_TIMEOUT` | Seconds allowed for a volume archive. Default `1800`. |

Removed: `DEFAULT_STORAGE_POOL`, `LXC_TIMEOUT_DEFAULT` and the LXD-specific
settings. Unknown keys in `.env` are ignored, so you can leave them in place.

---

## Troubleshooting

**`Cannot connect to the Docker daemon`**
The bot's user needs access to `/var/run/docker.sock`. Either add it to the
`docker` group or run the service as root. Then `docker version` as that user.

**`no such table: docker_ports`**
The migration did not run. Check `migration_reports/` for the failure reason, and
confirm the bot's working directory is the project root — the database path is
relative to `bot.py`.

**Root passwords stopped working**
The `SECRET_KEY` changed. Restore the previous `.secret_key` or the previous
`SECRET_KEY` value. Without it, stored passwords are unrecoverable; use
`!vps-password` to issue a fresh one per VPS.

**Quotas show as not enforced**
Expected on ext4/overlayfs. See the quota planning step above.

**A VPS row exists but the container does not**
`!docker-orphans` lists containers without database rows. For the reverse case,
`!vps-rebuild` recreates the container from the existing row, preserving the
volume.
