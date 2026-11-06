# Cloudflare quick tunnels for Proxmox VPS

- Proxmox management panels now include a **Cloudflare** action. It installs
  Cloudflare's repository and `cloudflared` inside the VPS, then starts a
  background quick tunnel to `https://localhost:8006` with `--no-tls-verify`.
- The Discord response reports live setup stages and returns the generated
  `trycloudflare.com` URL after reading `/root/cloudflared.log`. Package,
  startup and URL-wait failures are shown in the progress message.
- The action is restricted to Proxmox images, delivered ephemerally, and warns
  that anyone with the public URL can reach the Proxmox login page. Ordinary
  Linux management panels are unchanged.

---

# Public Proxmox console addresses

- Proxmox gateway links now inherit `YOUR_SERVER_IP` by default instead of
  advertising `127.0.0.1`. Set that value to a real public IP or DNS name, or
  override only the console address with `PROXMOX_CONSOLE_PUBLIC_HOST`.
- The gateway listener now defaults to `0.0.0.0`, so a remote browser can reach
  it when the host/cloud firewall (and NAT, where applicable) permits TCP 5001.
  The server-side Proxmox upstream remains loopback-only and is never placed in
  the browser URL.
- Documented direct public-IP and HTTPS reverse-proxy deployments, including
  DNS, firewall, WebSocket and restart requirements. The bot does not allocate
  public IPs, create DNS records or provision TLS certificates.
- Ordinary WebSSH example configuration now also inherits `YOUR_SERVER_IP`
  instead of forcing a loopback address. Added public-domain and clean-process
  configuration regressions.

---

# Proxmox native browser console gateway

- Added a separate `aiohttp` service for Proxmox VE only. It reverse-proxies the
  native Proxmox UI, API and binary noVNC WebSockets, so VM ISO installers,
  BIOS screens and boot menus remain graphical instead of being replaced by a
  text terminal.
- Bot-issued `/c/<token>` links are short-lived, exchange for an HttpOnly
  session cookie, and preserve owner/shared-user/admin authorization. The
  browser cannot choose an upstream host or port, and the gateway cookie is not
  forwarded to Proxmox. Large request bodies stream for ISO uploads.
- Defaults bind the gateway and its server-side upstream to `127.0.0.1`; the
  ordinary VPS WebSSH service is unchanged. Remote browsers need an SSH tunnel
  or another explicitly configured secure loopback-access arrangement.
- Added fake-upstream HTTP, redirect, cookie, authorization and binary
  WebSocket regressions, including the HTTP noVNC `encrypt=0` compatibility
  redirect. A real-TCP-listener smoke test also passed; no live Proxmox/browser
  verification was available.

---

# Proxmox console certificate repair

- Added a guarded certificate/key check before Proxmox console readiness. It
  runs as `www-data`, matching the pveproxy workers, and leaves a healthy
  certificate untouched.
- Missing or unusable certificates now wait for the pmxcfs mount, IPC and
  quorum before bounded `pvecm updatecerts --force` repair attempts, a
  post-repair validation, and a `pveproxy` restart. This avoids invoking pvecm
  while the container's systemd stack is still starting. Persistent failures
  retain command output and use the existing transactional provisioning
  rollback instead of waiting indefinitely on a TLS-broken socket.
- Certificate repair now handles cloud-internal DNS search suffixes that would
  make Proxmox's certificate CN exceed OpenSSL's 64-character limit. It removes
  the suffix only during `pvecm` and restores the original resolver file on all
  exit paths; normal DNS configuration is preserved.
- Proxmox recreation/reinstall uses the same guarded preparation; healthy
  rebuilds do not rotate certificates. The HTTP→HTTPS redirect and final HTTPS
  200 readiness checks remain unchanged.
- Added healthy, delayed-pmxcfs repair, long-search-domain, persistently
  failing, and rollback regressions, including the `Connection refused`
  startup path and resolver restoration.

---

# Proxmox native UI guidance

- Clarified that the pinned Proxmox image is already installed and that guest
  ISO installers, BIOS screens and boot menus are available through each VM's
  native noVNC **Console** in the Proxmox web UI.
- The management panel now labels the Proxmox action **Proxmox UI** instead of
  **Web SSH**, and labels its text command action **Host Shell**. Login and
  guest-console instructions are included in the panel response and credential
  DM. WebSSH remains text-only for ordinary Linux VPSes.

---

# Proxmox console readiness protocol fix

- Updated readiness verification for `dockurr/proxmox:9.2.20`: its Docker
  healthcheck starts over HTTP on port 8006 and follows redirects. The bot now
  follows pveproxy's native HTTP→HTTPS redirect and only accepts a final HTTPS
  200 as ready, so a redirect or an unfinished TLS endpoint is not enough.
- Creation, password, management, and reinstall console links use the same HTTPS
  URL helper as the verified console endpoint.

---

# v13 — Admin-only KVM and AppArmor creation settings

- Added an admin-only security step to guided VPS creation. Admins can explicitly
  enable or disable KVM hardware acceleration and AppArmor before deployment;
  public creation does not render or accept these overrides.
- Enforced the same authorization in the provisioning backend. Non-admin requests
  containing either override are rejected before Docker mutation. Ordinary VPS
  defaults are KVM off/AppArmor on; granted non-admin Proxmox uses fixed KVM
  on/AppArmor off policy.
- KVM enabled emits an explicit `/dev/kvm:/dev/kvm` device mapping and disabled
  emits none. AppArmor enabled emits the configured profile or `docker-default`;
  disabled emits `apparmor=unconfined`. Privileged mode cannot silently bypass
  the KVM-disabled guarantee.
- Proxmox preserves its KVM-backed privileged runtime and rejects KVM-disabled
  creation rather than claiming an unsupported configuration. Its AppArmor choice
  is carried into the spec instead of being discarded.
- Schema v13 persists both booleans, backfills existing Proxmox rows, and reuses
  the settings during recreate/reinstall, clone and migration.

The historical v12 record below is retained unchanged.

---

# v12 — Proxmox grants and functional custom images

- Added a separately gated Proxmox VE workload using the pinned community
  `dockurr/proxmox:9.2.20` image. Admins have implicit access; non-admins require
  a persisted grant managed by `/proxmox give`, `/proxmox remove`, and
  `/proxmox list` (with prefix equivalents).
- Enforced authorization in both Discord selectors and the provisioning service.
  Unknown images and alternate Proxmox references cannot be injected through a
  crafted callback or direct service request.
- Proxmox preserves its native systemd entrypoint and stop signal, uses
  privileged mode only for that workload, publishes port 8006, verifies the
  HTTPS console, and persists both `/var/lib/vz` and `/var/lib/pve-cluster`.
  Ordinary VPS remain on the existing unprivileged SSH pipeline.
- Proxmox credentials use a one-way SHA-512 `PASSWORD_HASH`, never plaintext
  `PASSWORD` in Docker metadata. Credential DMs and the management console link
  use the native HTTPS UI rather than SSH/WebSSH.
- Recreate/resize/reinstall/delete understand the Proxmox contract. Incomplete
  one-volume backups/restores, cloning, migration and workload conversions are
  explicitly rejected instead of silently losing data or changing privileges.
- Fixed `/image-add`: it now returns the expected catalogue entry, pulls and
  validates a Linux image, records the approving admin, and refreshes the
  creation selectors. `/image-remove` refreshes them too. Both remain admin-only.
- Schema v12 adds only `proxmox_grants`; existing users, VPS, ports, backups and
  settings are preserved. The live command tree has 89 top-level commands plus
  the three Proxmox subcommands.

The historical v10 → v11 record below is retained unchanged.

---

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
| `tests/` | 158 tests + a fake Docker client |

**Capability**

- Custom images can be approved at runtime (`!image-add`)
- Port reservation history is queryable, including released ports
- Volume and writable-root backups with checksum verification and retention
- Per-node quota support reporting, with data-volume scope stated explicitly
- Orphan detection without automatic deletion
- 158 unit tests that run with no Docker daemon

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
19. **OpenSSH could not enter its mandatory pre-auth chroot.** The container's
    capability drop included `SYS_CHROOT`, so sshd accepted TCP and sent its
    banner, then terminated every Paramiko login with `EOFError` or
    `ConnectionResetError`. `CAP_KEEP` now retains `SYS_CHROOT` even when an
    extra drop requests it; dangerous administration capabilities remain
    dropped. A real OpenSSH run reproduced `chroot("/run/sshd"): Operation not
    permitted`, and the corrected drop list passed real Paramiko logins both
    with and without PAM.
20. **APT could not chmod its package-index files.** Dropping `FOWNER` caused
    `SetupAPTPartialDirectory` and `Item::QueueURI` "Operation not permitted"
    warnings after APT changed index ownership for its sandbox user. `CAP_KEEP`
    now retains `FOWNER`. A disposable local APT repository reproduced the
    warnings with the old drop list and completed without them using the
    corrected one.

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
157 passed, 1 skipped
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_security_policy.py` | 26 | capability drop including OpenSSH/APT requirements, privileged/socket/namespace refusal, name and image validation |
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
