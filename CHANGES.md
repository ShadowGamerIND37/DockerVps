# v10 → v11 Change Record

What was preserved, what changed, and what could not be verified.

Verified by diffing this tree against commit `49bfff0` (v10) and by running the
test suite. Command counts below come from the live prefix and application-command
inventories, not from memory.

---

## Preserved — unchanged

**Commands.** All **75** v10 prefix commands still exist under the same names.
Zero removed. `bot.py` registers 88 in total (75 preserved + 13 new).

The only alias edit: `lxc-list` kept its `containers` alias and gained
`docker-list` and `docker-ps`. The command name itself is unchanged so existing
muscle memory and any automation keeps working.

**Interface.** Everything the user sees is intact:

- Every embed, field layout, colour and emoji set
- Buttons, dropdowns, modals and pagination views
- The animation engine, including the shimmer/shake effects and speed setting
- `PanelView`, `LivePanelView`, `LivePlatformView`, `LiveFleetView`,
  `LiveContainerGrid`
- `build_vps_record` and `CreateVpsModal`
- The `Responder` and the six background loops
- The admin panel, permission system, audit log and rate limiting
- `webssh.html` — same design, same JS, same xterm.js loader chain

**Database.** Schema migrates from v10 to v11 in place. Users, VPS rows, port
allocations, legacy port forwards, backups, snapshots, audit history, settings,
API keys and web SSH tokens all survive untouched. Legacy LXC rows are tagged
`backend='lxc-legacy'`; they are never converted or deleted.

**Reused verbatim from v10.** `_env*` helpers, `_SecretRedactor`,
Argon2id/PBKDF2 `hash_password`/`verify_password`, `validate_ssh_target`,
`_is_private_ip`, `check_rate_limit`, `audit()`, `db_conn`/`query_all`/
`query_one`/`execute`, host metric readers, `clamp_int`, `truncate_text`,
`sanitize_reason`.

---

## Changed — same feature, new implementation

| Feature | v10 | v11 |
| --- | --- | --- |
| Container engine | `lxc` CLI, shelled out over SSH | Docker Engine API via the `docker` SDK |
| Create workflow | ad-hoc sequence | 18-step transaction with per-step rollback |
| Persistent storage | LXD storage pool | Named volume `tgdbot_<name>` at `/var/lib/tgdbot` plus writable-root image snapshots |
| Ports | LXD proxy devices | Docker port publishing, tracked in `docker_ports` |
| Disk quota | `limits.disk` | XFS project quota or Btrfs qgroup, reported honestly |
| Snapshots / backup | LXD snapshots | Volume tar + rootfs Docker-image archive + JSON side-car + sha256 |
| Resize | `lxc config set` (live) | Container recreate, volume preserved |
| Reinstall | delete + recreate | Recreate with `wipe_volume=True` |
| Clone | `lxc copy` | Volume duplicated via helper container |
| Node transport | SSH to the LXD host | `local`, `ssh://`, or mutual TLS |
| Container listing | `lxc list --format=csv` | Docker API, filtered by managed label |
| Web SSH server | 737-line Flask/Paramiko impl in `bot.py` | `tgdbot/web/terminal.py` |
| Progress reporting | direct `await` from the loop | `PanelPump` bridging worker threads |

**`bot.py` shrank from 12,259 to 11,642 lines** while gaining 13 commands, because
the infrastructure moved into `tgdbot/` (9,441 lines across 33 modules).

---

## Added

**13 admin commands**

`images` · `image-add` · `image-remove` · `docker-quota` · `docker-policy` ·
`docker-orphans` · `docker-orphan-remove` · `port-history` · `vps-backup` ·
`vps-backups` · `vps-backup-verify` · `vps-restore` · `vps-backup-prune`

**Modules**

| Path | Purpose |
| --- | --- |
| `tgdbot/platform.py` | Async facade; the only entry point `bot.py` uses |
| `tgdbot/docker_backend/` | 12 Docker adapters |
| `tgdbot/database/` | Schema, migrations, repository |
| `tgdbot/services/` | Provisioning, lifecycle, backups, expiration, nodes |
| `tgdbot/web/terminal.py` | Web SSH terminal |
| `tgdbot/scripts/` | Installer, migration, healthcheck, acceptance test |
| `tests/` | 156 tests + a fake Docker client |

**Capability**

- Custom images can be approved at runtime (`!image-add`)
- Port reservation history is queryable, including released ports
- Volume and writable-root backups with checksum verification and retention
- Per-node quota support reporting, with data-volume scope stated explicitly
- Orphan detection without automatic deletion
- 156 unit tests that run with no Docker daemon

---

## Discord UI and application commands

- **Guided VPS creation select menus fixed.** `CreateVpsModal.on_submit()` sent
  `NodeSelectView` before calling `build_options()`, so the embed had no node
  dropdown. It now builds the options first; the node callback was also tested
  through to a populated OS dropdown.
- **All 88 prefix commands have slash equivalents.** 83 use `hybrid_command`;
  the five existing custom slash implementations (`manage`, `myvps`, `ping`,
  `dashboard`, `help`) remain unchanged. Prefix names and aliases are preserved.
- `/vps` and `/ports` expose an optional `args` string for their remaining
  arguments, since Discord rejects variadic slash parameters. Prefix parsing
  still accepts the original command forms.
- `tests/test_creation_views.py` covers the guided node → OS flow with fake
  interactions; `tests/test_slash_commands.py` checks all 88 registrations and
  serializes every payload that would be sent to Discord.

---

## Security fixes

1. **Root password no longer reaches the browser.** v10's `_render_pin_page`
   injected the decrypted password into `__AUTOCONNECT__` *before* PIN
   verification, so anyone with the link could read it from page source. The
   payload is now `{host, port, username, label}`; the server resolves the
   credential from the token on `/api/ssh/connect`.
2. **A token is mandatory.** Ad-hoc "connect me to any host" is refused unless
   `WEBSSH_ALLOW_ADHOC=true` (default false).
3. **`privileged=true` removed.** v10's `apply_lxc_config` set
   `security.privileged=true` with apparmor unconfined. v11 defaults to
   `DOCKER_PRIVILEGED=false`; an administrator can still override it, and doing
   so logs a loud warning rather than passing silently.
4. **Capability drop includes `SYS_ADMIN`**, plus the rest of `DEFAULT_CAP_DROP`.
5. **Docker socket, host devices and host namespaces are never exposed** to a
   customer container.
6. **Passwords are never stored in env vars, container labels, `docker inspect`
   output or logs.** `chpasswd` consumes the credential and truncates the file.
7. **Unauthenticated `tcp://…:2375` is rejected** for remote nodes.
8. **Terminal responses** carry CSP, `no-store`, `nosniff` and
   `X-Frame-Options`; 12 attempts/min per IP; 8 sessions/IP; 200 global; PIN
   lockout after the configured attempts.
9. **`UsePrivilegeSeparation sandbox` removed** from the generated `sshd_config`
   — OpenSSH ≥7.5 rejects the directive, which would have broken SSH entirely.
10. **A credential vault is installed at boot** (`INFRA.set_vault`). Without it
    every generated root password was discarded, so `!vps password` and the web
    terminal would both have failed on a fresh VPS.

---

## Bugs found and fixed while testing

Writing tests surfaced real defects, not just test errors:

1. `SYS_ADMIN` missing from `DEFAULT_CAP_DROP`.
2. `migrate()` could not bootstrap a fresh database (`no such table: meta`)
   despite its docstring claiming it could — a first install would have failed,
   because the installer runs the migrator before `bot.py`'s `init_db()`.
3. **No root password was ever persisted** once provisioning was rewired (see
   security fix 10).
4. `ram_mb=0` was silently coerced to the default instead of rejected.
5. `TerminalBackend.issue()` accepted `max_uses` and never passed it to `Token`,
   so every link silently got the default of 3.
6. `models.BASE_SCHEMA_SQL` had drifted from `bot.py`'s `SCHEMA_SQL` — the copy
   was missing `device_tracking.ip_hash`, so a freshly bootstrapped database made
   `CREATE INDEX idx_device_ip` fail on first start. It is now a verbatim copy
   with a test that fails on drift.
7. **Latent `NameError`s from deleted helpers**: `webssh_tokens`,
   `webssh_sessions`, `_save_webssh_token`, and an invented `_exc_text`. None of
   these are caught by `import bot`, because Python resolves globals when the
   line executes. The webssh names would have broken the admin scan and every
   PIN view.
8. **`unix_networking.BRIDGE_NAME` does not exist** (line 9002, `!vps-network`).
   This one shipped to `main` in PR #1. It is worth recording because the first
   attempt to fix it made things worse: `LXC_BRIDGE` (an undefined *name*) was
   replaced with `unix_networking.BRIDGE_NAME` (a valid module, missing
   *attribute*) and reported as fixed. pyflakes reports **zero** undefined names
   for it, and `import bot` is clean, so both existing checks passed. Fixed to
   `unix_config.DOCKER_DEFAULT_NETWORK`, the value the provisioning path
   actually reads.
9. `acceptance_test.py` referenced `exc` after its `except` block ended.
10. Two silent no-ops: `_with_guard()` and a `_recreate()` stub that raised
    `NotImplementedError`. Both passed `compileall` and did nothing.
11. `PanelPump.start()` passed `name=` to `asyncio.ensure_future()`, which does
    not accept that keyword. Every provisioning attempt failed while starting
    its progress panel, before container creation began. It now uses
    `asyncio.create_task()`; `tests/test_panel_pump.py` reproduced the exact
    `TypeError` before the fix and exercises the same start/emit/stop path.
12. **The Discord UI and Docker repository used different SQLite files.**
    `bot.py` migrated `vps.db`, while `INFRA` used `DATABASE_PATH` (defaulting
    to `vps_data.db`), so fresh provisioning failed with `no such table: vps`.
    Both now use the same configured path; the default remains the v10
    `vps.db`, and an explicit `DATABASE_PATH` is honored.
13. **Runtime directories could be unwritable after the rebrand.** Existing
    `.env` files may still name `/var/lib/unixnodes`, while the installer only
    created its new default paths. The installer now reads the four configured
    directory values without sourcing `.env` and chowns exactly those paths.
    Runtime asset setup checks writability and reports the remediation instead
    of silently moving customer data to a fallback directory.
14. **Port verification used creation-time inspect data.** Docker can leave
    `NetworkSettings.Ports` empty in the object returned by `containers.create()`
    and populate it at startup. Verification now reloads that object before
    comparing bindings, preventing a healthy VPS from being rolled back with a
    false "host port is not bound" error.
15. **WebSSH waited for Discord's `on_ready` event.** Gateway delays or login
    failures could leave the HTTP listener completely unstarted. `main()` now
    starts WebSSH before `bot.run()`, restores saved terminal tokens at the same
    time, and only marks the listener started after its thread launches.
16. **SSH login verification failed on the first transient EOF and could hide
    the actual error.** Empty exception messages now include the exception type
    and SSH target. Negotiation, EOF and socket failures get up to three attempts
    with a fresh Paramiko client; authentication failures are never retried.
17. **Quota and database finalization did not roll back host quotas cleanly.**
    Quota-processing exceptions now identify step 17. If database registration
    then fails, rollback releases the enforced quota before removing the volume;
    a failed release retains the volume and quota record for a safe retry.
18. **Public VPS requests lost their visibility flag.** `is_public` from the
    provision request is now persisted in the registered VPS row.

`tests/test_static_integrity.py` now fails the build on either failure mode:

- **undefined global names** (pyflakes-backed), and
- **attributes that do not exist on a valid `tgdbot` module** — every
  `unix_<alias>.<ATTR>` access in `bot.py` is resolved against the imported
  module.

Both guards were validated by re-injecting the exact bug they target and
confirming the suite goes red. The second guard matters precisely because
pyflakes cannot see it: on the broken `BRIDGE_NAME` line pyflakes reports 0
undefined names while the new test names line 9002.

---

## Not verified in this environment

**There is no Docker daemon in the sandbox.** Everything below is implemented and
unit-tested against a fake client, but has **not** been exercised against a real
engine:

- Real container creation, start, stop, restart, delete
- Root SSH login, `apt`/`apk`, cron, SFTP
- Kernel enforcement of RAM, CPU and PID limits
- XFS project quota and Btrfs qgroup enforcement
- Volume backup and restore through the helper container
- Remote node transports (SSH and mutual TLS)
- The live Discord UI (requires a real bot token and gateway)
- Storage-quota behaviour on a host with a quota-capable filesystem

`tgdbot/scripts/acceptance_test.py` covers all of the above with 33 checks
across 25 groups and must be run on a real host. It reports the daemon as
unreachable rather than fabricating a pass.

Nothing in this document is an assumed result. Where a test could not run, it is
listed here instead.

---

## Rebrand — UnixNodes → TGD Bot

The project was renamed to **TGD Bot** (developer: **ShadowGamerIND**). Display
strings, the package path (`unixnodes/` → `tgdbot/`), the systemd unit and the
docs changed freely. **Runtime identifiers could not**, because Docker labels
are immutable after creation and Docker has no volume rename. Both generations
are therefore matched:

| | Current | Pre-rebrand, still matched |
| --- | --- | --- |
| Managed filter | `tgdbot.managed=true` | `unixnodes.managed=true` |
| Volume prefix | `tgdbot_` | `unixnodes_` |

No volume is copied, moved or renamed by the rebrand, and no container is
recreated. Details and the per-operation behaviour are in
[MIGRATION.md](MIGRATION.md#rebranding-unixnodes--tgd-bot).

Writing these tests found four further defects, all of which would have lost or
mis-attached customer data on an existing install:

| Defect | Effect before the fix |
| --- | --- |
| `containers.py` hardcoded `"unixnodes.managed=true"` at three call sites | `!docker-list`, `!dashboard`, `!docker-quota` and the state counts would have reported **zero** containers the moment the label changed |
| `volumes.rename()` recomputed the *source* name | cloning any pre-rebrand VPS would fail with "Source volume does not exist" |
| `lifecycle.recreate()` / `resize()` recomputed the volume name | a resize or port change would attach a **new, empty** volume and the VPS would appear wiped |
| `migrate()` never wrote the target `volume_name` | after a node migration the row pointed at a volume that no longer existed |

Two defects in the test fake were also fixed, both of which had been masking the
above rather than exposing it:

- `volumes.list()` returned a non-iterable wrapper; docker-py returns a plain list.
- `containers.list()` **AND**'d repeated `label` filter values; the real API **OR**'s them.
  With AND semantics the widened filter silently matched nothing, so the broken
  code path looked green.

## Test results

```
155 passed, 1 skipped
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_security_policy.py` | 24 | capability drop, privileged/socket/namespace refusal, name and image validation |
| `test_migrations.py` | 13 | idempotency, backup integrity, rollback note, base-schema drift, fresh bootstrap |
| `test_database_bootstrap.py` | 1 | fresh configured DB is migrated before the provisioning repository reads it |
| `test_env_value.py` | 2 | safe .env path parsing, legacy paths, shell precedence, no shell execution |
| `test_runtime_assets.py` | 1 | unwritable host directory diagnostics and remediation |
| `test_ports.py` | 22 | reservation, conflicts, ranges, TCP/UDP, release, history, binding refresh |
| `test_creation_views.py` | 1 | guided modal node options and node → OS select handoff |
| `test_slash_commands.py` | 4 | 88 prefix/slash names, payload serialization, variadic-argument options, context menu |
| `test_panel_pump.py` | 1 | runtime progress-pump start, named task, event delivery and shutdown |
| `test_provisioning.py` | 26 | happy path, rollback at each injected failure, quota/database finalization, plan validation, credential vault |
| `test_ssh_verification.py` | 3 | transient EOF retry, empty-exception diagnostics, authentication failure short-circuit |
| `test_rebrand_compat.py` | 18 | pre-rebrand containers and volumes still resolve, foreign containers never adopted, filter OR semantics |
| `test_web_terminal.py` | 19 | no password in page source, PIN lockout, ad-hoc refusal, headers, startup order |
| `test_static_integrity.py` | 11 | undefined globals, unresolvable module attributes, deleted-helper revival, progress-callback safety, no privileged mode |
| `test_storage_persistence.py` | 10 | root-mount rejection, snapshot recreation/retry, backup/legacy restore, stored-volume safety, clone and migration |

Static analysis: **0 undefined names** across `bot.py`, `tgdbot/` and
`tests/`, and every `tgdbot` attribute access in `bot.py` resolves.
