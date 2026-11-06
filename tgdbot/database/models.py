"""
Schema definitions for the Docker metadata added on top of the existing
TGD Bot v10 database.

Nothing here renames or drops an existing column: the original ``vps``,
``nodes``, ``port_forwards``, ``usage_snapshots``, ``host_snapshots``,
``audit_log``, ``webssh_tokens``, ``settings``, ``admins``, ``cooldowns`` and
``device_tracking`` tables are preserved exactly as they were.
"""

from __future__ import annotations

SCHEMA_VERSION = 12

# ── columns added to existing tables ────────────────────────────────────────
# (table, column definition).  Applied with ALTER TABLE … ADD COLUMN, so the
# migration is idempotent and never touches existing rows' data.

ADDED_COLUMNS = [
    # vps — Docker identity + quota/provisioning state
    ("vps", "backend TEXT DEFAULT 'docker'"),
    ("vps", "docker_container_id TEXT"),
    ("vps", "docker_image TEXT"),
    ("vps", "volume_name TEXT"),
    ("vps", "ssh_host_port INTEGER"),
    ("vps", "pid_limit INTEGER"),
    ("vps", "quota_method TEXT"),
    ("vps", "quota_enforced INTEGER DEFAULT 0"),
    ("vps", "quota_project_id INTEGER"),
    ("vps", "provision_state TEXT DEFAULT 'active'"),
    ("vps", "restart_count INTEGER DEFAULT 0"),
    ("vps", "last_health_ts TEXT"),
    # nodes — Docker connectivity + reported capacity
    ("nodes", "transport TEXT DEFAULT 'local'"),
    ("nodes", "docker_host TEXT"),
    ("nodes", "docker_tls_ca TEXT"),
    ("nodes", "docker_tls_cert TEXT"),
    ("nodes", "docker_tls_key TEXT"),
    ("nodes", "docker_tls_verify INTEGER DEFAULT 1"),
    ("nodes", "docker_timeout INTEGER DEFAULT 30"),
    ("nodes", "enabled INTEGER DEFAULT 1"),
    ("nodes", "last_check TEXT"),
    ("nodes", "docker_version TEXT"),
    ("nodes", "os_info TEXT"),
    ("nodes", "cpu_info TEXT"),
    ("nodes", "ram_total_mb INTEGER DEFAULT 0"),
    ("nodes", "ram_available_mb INTEGER DEFAULT 0"),
    ("nodes", "storage_total_gb REAL DEFAULT 0"),
    ("nodes", "storage_free_gb REAL DEFAULT 0"),
    ("nodes", "containers_running INTEGER DEFAULT 0"),
    ("nodes", "containers_stopped INTEGER DEFAULT 0"),
    ("nodes", "quota_method TEXT"),
    ("nodes", "quota_supported INTEGER DEFAULT 0"),
]

# ── base (v10) tables ────────────────────────────────────────────────────────
# bot.py's `init_db()` normally creates these.  They are repeated here so the
# migration can also bootstrap a brand-new database on its own (a fresh install
# runs `migrate_database.py` before the bot has ever started).  Every statement
# is `CREATE TABLE IF NOT EXISTS`, so an existing database is never touched.
#
# This block is a VERBATIM copy of bot.py's `SCHEMA_SQL` — see
# tests/test_migrations.py::test_base_schema_matches_bot_schema, which fails if
# the two ever drift.  Never hand-edit one without the other.

BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    user_id   TEXT PRIMARY KEY,
    added_by  TEXT,
    added_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    location      TEXT DEFAULT 'Unknown',
    total_vps     INTEGER DEFAULT 50,
    max_cpu       INTEGER DEFAULT 0,
    max_ram_gb    INTEGER DEFAULT 0,
    tags          TEXT DEFAULT '[]',
    api_key       TEXT,
    url           TEXT,
    is_local      INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    last_updated  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vps (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT NOT NULL,
    node_id            INTEGER NOT NULL DEFAULT 1,
    container_name     TEXT UNIQUE NOT NULL,
    ram                TEXT NOT NULL DEFAULT '1GB',
    cpu                TEXT NOT NULL DEFAULT '1',
    storage            TEXT NOT NULL DEFAULT '10GB',
    config             TEXT NOT NULL DEFAULT 'Custom',
    os_version         TEXT DEFAULT 'ubuntu:22.04',
    status             TEXT DEFAULT 'stopped',
    suspended          INTEGER DEFAULT 0,
    whitelisted        INTEGER DEFAULT 0,
    is_public          INTEGER DEFAULT 0,
    created_at         TEXT NOT NULL,
    shared_with        TEXT DEFAULT '[]',
    suspension_history TEXT DEFAULT '[]',
    expiration_date    TEXT DEFAULT NULL,
    root_password      TEXT DEFAULT NULL,
    last_modified      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vps_user   ON vps(user_id);
CREATE INDEX IF NOT EXISTS idx_vps_node   ON vps(node_id);
CREATE INDEX IF NOT EXISTS idx_vps_status ON vps(status);

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    last_modified TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS port_allocations (
    user_id         TEXT PRIMARY KEY,
    allocated_ports INTEGER NOT NULL DEFAULT 0,
    last_modified   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS port_forwards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    vps_container TEXT NOT NULL,
    vps_port      INTEGER NOT NULL,
    host_port     INTEGER NOT NULL UNIQUE,
    protocol      TEXT DEFAULT 'both',
    created_at    TEXT NOT NULL,
    last_modified TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_forwards_container ON port_forwards(vps_container);

CREATE TABLE IF NOT EXISTS device_tracking (
    user_id            TEXT PRIMARY KEY,
    username           TEXT,
    avatar_hash        TEXT,
    device_fingerprint TEXT,
    ip_hash            TEXT,
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen          TEXT DEFAULT CURRENT_TIMESTAMP,
    vps_created        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_device_ip ON device_tracking(ip_hash);
CREATE INDEX IF NOT EXISTS idx_device_fp ON device_tracking(device_fingerprint);

CREATE TABLE IF NOT EXISTS cooldowns (
    key        TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    reason     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    action    TEXT NOT NULL,
    actor_id  TEXT,
    target    TEXT,
    detail    TEXT,
    guild_id  TEXT,
    success   INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    container  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    cpu_pct    REAL DEFAULT 0,
    ram_pct    REAL DEFAULT 0,
    ram_used   INTEGER DEFAULT 0,
    ram_total  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_container ON usage_snapshots(container, ts);

-- v10: host history for the animated platform dashboard ---------------------
CREATE TABLE IF NOT EXISTS host_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    cpu_pct   REAL DEFAULT 0,
    ram_pct   REAL DEFAULT 0,
    disk_pct  REAL DEFAULT 0,
    vps_total INTEGER DEFAULT 0,
    vps_run   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_host_ts ON host_snapshots(ts);

CREATE TABLE IF NOT EXISTS webssh_tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    vps_id     INTEGER,
    host       TEXT,
    port       INTEGER,
    username   TEXT NOT NULL,
    password   TEXT,
    label      TEXT,
    pin_hash   TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER DEFAULT 0,
    max_uses   INTEGER DEFAULT 3,
    pin_attempts INTEGER DEFAULT 0,
    unlock_keys TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_webssh_user ON webssh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_webssh_expires ON webssh_tokens(expires_at);
"""

#: Tables that must exist before the Docker additions can be applied.
BASE_TABLES = (
    "meta", "admins", "nodes", "vps", "settings", "port_allocations", "port_forwards",
    "device_tracking", "cooldowns", "audit_log", "usage_snapshots", "host_snapshots",
    "webssh_tokens",
)

# ── new tables ──────────────────────────────────────────────────────────────

NEW_TABLES_SQL = """
-- Host port ledger. One row per (host address, port, protocol).
-- Released rows are kept so the allocation history survives.
CREATE TABLE IF NOT EXISTS docker_ports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id        INTEGER NOT NULL,
    host_ip        TEXT    NOT NULL DEFAULT '0.0.0.0',
    host_port      INTEGER NOT NULL,
    container_name TEXT    NOT NULL,
    container_port INTEGER NOT NULL,
    protocol       TEXT    NOT NULL DEFAULT 'tcp',
    purpose        TEXT    NOT NULL DEFAULT 'user',
    status         TEXT    NOT NULL DEFAULT 'reserved',
    user_id        TEXT,
    created_at     TEXT    NOT NULL,
    released_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_docker_ports_container
    ON docker_ports(container_name);
CREATE INDEX IF NOT EXISTS idx_docker_ports_node
    ON docker_ports(node_id, status);
-- The hard guarantee: two VPS can never own the same address + port + protocol.
CREATE UNIQUE INDEX IF NOT EXISTS ux_docker_ports_active
    ON docker_ports(node_id, host_ip, host_port, protocol)
    WHERE status != 'released';

-- Backups (archive + configuration metadata).
CREATE TABLE IF NOT EXISTS docker_backups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    node_id        INTEGER NOT NULL DEFAULT 1,
    name           TEXT NOT NULL UNIQUE,
    archive        TEXT NOT NULL,
    size_bytes     INTEGER DEFAULT 0,
    sha256         TEXT,
    image          TEXT,
    configuration  TEXT DEFAULT '{}',
    note           TEXT DEFAULT '',
    scheduled      INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'complete',
    verified       INTEGER DEFAULT 0,
    verify_note    TEXT DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docker_backups_container ON docker_backups(container_name);
CREATE INDEX IF NOT EXISTS idx_docker_backups_created   ON docker_backups(created_at);

-- Administrator-approved custom images.
CREATE TABLE IF NOT EXISTS docker_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reference   TEXT NOT NULL UNIQUE,
    label       TEXT,
    emoji       TEXT DEFAULT '🐳',
    description TEXT DEFAULT '',
    approved    INTEGER DEFAULT 1,
    added_by    TEXT,
    created_at  TEXT NOT NULL
);

-- Per-user permission to select and provision the privileged Proxmox workload.
-- Admins are implicitly authorized and do not need a row here.
CREATE TABLE IF NOT EXISTS proxmox_grants (
    user_id    TEXT PRIMARY KEY,
    granted_by TEXT,
    created_at TEXT NOT NULL
);

-- Administrator-configurable resource plans.
CREATE TABLE IF NOT EXISTS resource_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    ram_mb      INTEGER NOT NULL DEFAULT 1024,
    cpu         REAL NOT NULL DEFAULT 1,
    storage_gb  INTEGER NOT NULL DEFAULT 10,
    pids        INTEGER DEFAULT 512,
    description TEXT DEFAULT '',
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- Host level disk quota bookkeeping (XFS project ids / btrfs qgroups).
CREATE TABLE IF NOT EXISTS storage_quotas (
    container_name TEXT PRIMARY KEY,
    node_id        INTEGER NOT NULL DEFAULT 1,
    method         TEXT,
    project_id     INTEGER,
    size_gb        REAL DEFAULT 0,
    enforced       INTEGER DEFAULT 0,
    detail         TEXT DEFAULT '',
    updated_at     TEXT
);

-- Audit trail of every schema migration run.
CREATE TABLE IF NOT EXISTS migration_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    from_version INTEGER,
    to_version   INTEGER,
    backup_path  TEXT,
    summary      TEXT NOT NULL,
    detail       TEXT DEFAULT '[]'
);
"""

DEFAULT_PLANS = [
    ("Starter", 1024, 1.0, 10, 256, "1 GB RAM • 1 vCPU • 10 GB disk"),
    ("Basic", 2048, 1.0, 20, 384, "2 GB RAM • 1 vCPU • 20 GB disk"),
    ("Standard", 4096, 2.0, 40, 512, "4 GB RAM • 2 vCPU • 40 GB disk"),
    ("Performance", 8192, 4.0, 80, 768, "8 GB RAM • 4 vCPU • 80 GB disk"),
]

DEFAULT_IMAGES = [
    ("ubuntu:24.04", "Ubuntu 24.04 LTS (Noble)", "🟠", "Latest LTS Ubuntu — recommended"),
    ("ubuntu:22.04", "Ubuntu 22.04 LTS (Jammy)", "🟠", "Previous LTS with long support"),
    ("ubuntu:20.04", "Ubuntu 20.04 LTS (Focal)", "🟠", "Older LTS for legacy software"),
    ("debian:13", "Debian 13 (Trixie)", "🔴", "Current Debian stable"),
    ("debian:12", "Debian 12 (Bookworm)", "🔴", "Rock solid Debian release"),
    ("debian:11", "Debian 11 (Bullseye)", "🔴", "Proven Debian stable"),
    ("alpine:3.20", "Alpine Linux 3.20", "🏔️", "Ultra lightweight (~8 MB)"),
]

#: Values that identify a record created by the LXC/LXD backend.  Existing rows
#: are *tagged*, never converted: the bot refuses to manage them as Docker VPS
#: until an administrator verifies the real infrastructure.
LEGACY_BACKEND = "lxc-legacy"
DOCKER_BACKEND = "docker"
