"""
Schema migration v10 → v11.

The existing SQLite database must survive: every user, VPS, port, backup,
snapshot, audit row and setting has to still be there after the migration, the
migration has to be idempotent, and legacy LXC records must be *tagged* — never
silently converted into Docker records.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tgdbot import config
from tgdbot.database import migrations
from tgdbot.database import models
from tgdbot.database.repository import Repository

#: The v10 schema, recreated verbatim so the tests start from real data.
V10_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins (user_id TEXT PRIMARY KEY, added_at TEXT);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, location TEXT,
    total_vps INTEGER DEFAULT 50, max_cpu INTEGER DEFAULT 0, max_ram_gb INTEGER DEFAULT 0,
    tags TEXT, api_key TEXT, url TEXT, is_local INTEGER DEFAULT 0,
    created_at TEXT, last_updated TEXT);
CREATE TABLE IF NOT EXISTS vps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, node_id INTEGER DEFAULT 1,
    container_name TEXT UNIQUE, ram TEXT, cpu TEXT, storage TEXT, config TEXT,
    os_version TEXT, status TEXT, suspended INTEGER DEFAULT 0, whitelisted INTEGER DEFAULT 0,
    is_public INTEGER DEFAULT 0, created_at TEXT, shared_with TEXT, suspension_history TEXT,
    expiration_date TEXT, root_password TEXT, last_modified TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS port_allocations (
    user_id TEXT PRIMARY KEY, allocated_ports INTEGER DEFAULT 0, last_modified TEXT);
CREATE TABLE IF NOT EXISTS port_forwards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, vps_container TEXT,
    vps_port INTEGER, host_port INTEGER UNIQUE, protocol TEXT DEFAULT 'both',
    created_at TEXT, last_modified TEXT);
CREATE TABLE IF NOT EXISTS device_tracking (
    user_id TEXT PRIMARY KEY, vps_created INTEGER DEFAULT 0, last_created TEXT);
CREATE TABLE IF NOT EXISTS cooldowns (user_id TEXT PRIMARY KEY, until TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, actor_id TEXT, target TEXT,
    detail TEXT, success INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS usage_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, container TEXT, cpu_pct REAL, ram_pct REAL,
    ram_used_mb INTEGER, ram_total_mb INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS host_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cpu_pct REAL, ram_pct REAL, disk_pct REAL,
    vps_count INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS webssh_tokens (
    token TEXT PRIMARY KEY, user_id TEXT, vps_id INTEGER, host TEXT, port INTEGER,
    username TEXT, password TEXT, label TEXT, pin_hash TEXT, unlock_keys TEXT,
    max_uses INTEGER DEFAULT 3, uses INTEGER DEFAULT 0, expires_at TEXT, created_at TEXT);
"""

LEGACY_ROWS = [
    # (id, user_id, node_id, container_name, ram, cpu, storage, os_version, status)
    (1, "111", 1, "alice-vps-1", "2GB", "2", "20GB", "ubuntu:22.04", "running"),
    (2, "222", 1, "bob-vps-2", "4GB", "4", "40GB", "debian:12", "stopped"),
]


@pytest.fixture()
def v10_database(tmp_path):
    """A populated v10 database — the exact shape a live install has."""
    path = tmp_path / "vps_data.db"
    conn = sqlite3.connect(path)
    conn.executescript(V10_SCHEMA)
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '10')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('next_vps_id', '3')")
    conn.execute(
        "INSERT INTO nodes (id, name, location, total_vps, is_local, created_at) "
        "VALUES (1, 'Local Node', 'DC1', 50, 1, '2025-01-01')")
    for row in LEGACY_ROWS:
        conn.execute(
            "INSERT INTO vps (id, user_id, node_id, container_name, ram, cpu, storage, config,"
            " os_version, status, suspended, whitelisted, is_public, created_at, shared_with,"
            " suspension_history, expiration_date, root_password)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,'2025-06-01','[]','[]',NULL,'f1:encrypted')",
            (row[0], row[1], row[2], row[3], row[4], row[5], row[6],
             f"{row[4]} RAM / {row[5]} vCPU / {row[6]} Disk", row[7], row[8]))
    conn.execute(
        "INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, protocol,"
        " created_at) VALUES ('111','alice-vps-1',22,22001,'tcp','2025-06-01')")
    conn.execute(
        "INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, protocol,"
        " created_at) VALUES ('111','alice-vps-1',8080,25001,'both','2025-06-01')")
    conn.execute("INSERT INTO port_allocations (user_id, allocated_ports) VALUES ('111', 2)")
    conn.execute(
        "INSERT INTO audit_log (action, actor_id, target, detail, created_at) "
        "VALUES ('vps_create','111','alice-vps-1','test','2025-06-01')")
    conn.execute(
        "INSERT INTO usage_snapshots (container, cpu_pct, ram_pct, ram_used_mb, ram_total_mb,"
        " created_at) VALUES ('alice-vps-1', 5.0, 25.0, 512, 2048, '2025-06-01')")
    conn.execute(
        "INSERT INTO host_snapshots (cpu_pct, ram_pct, disk_pct, vps_count, created_at) "
        "VALUES (10.0, 20.0, 30.0, 2, '2025-06-01')")
    conn.execute(
        "INSERT INTO webssh_tokens (token, user_id, host, port, username, password, label,"
        " expires_at, created_at) VALUES ('tok-1','111','127.0.0.1',22001,'root','f1:x',"
        " 'alice-vps-1','2030-01-01','2025-06-01')")
    conn.execute("INSERT INTO settings (key, value) VALUES ('theme','dark')")
    conn.execute("INSERT INTO device_tracking (user_id, vps_created) VALUES ('111', 1)")
    conn.execute("INSERT INTO cooldowns (user_id, until) VALUES ('111','2030-01-01')")
    conn.commit()
    conn.close()
    return path


def test_inspection_reports_the_v10_structure(v10_database):
    data = migrations.inspect_database(v10_database)
    assert data["exists"] is True
    assert int(data["schema_version"]) == 10
    assert data["integrity"] == "ok"
    for table in ("vps", "nodes", "port_forwards", "audit_log", "webssh_tokens",
                  "usage_snapshots", "host_snapshots"):
        assert table in data["tables"], f"{table} missing from the inventory"
    assert data["detail"]["vps"]["rows"] == 2


def test_migration_is_additive_and_keeps_every_row(v10_database):
    report = migrations.migrate(v10_database, skip_backup=True)
    assert report.ok, report.summary_text()
    assert report.from_version == 10
    assert report.to_version == models.SCHEMA_VERSION

    repo = Repository(v10_database)
    assert repo.schema_version() == models.SCHEMA_VERSION

    # Every legacy row survived untouched.
    vps_rows = {row["container_name"]: row for row in repo.list_vps()}
    assert set(vps_rows) == {"alice-vps-1", "bob-vps-2"}
    assert vps_rows["alice-vps-1"]["ram"] == "2GB"
    assert vps_rows["alice-vps-1"]["cpu"] == "2"
    assert vps_rows["alice-vps-1"]["root_password"] == "f1:encrypted"
    assert int(vps_rows["alice-vps-1"]["id"]) == 1

    assert len(repo.list_nodes()) == 1
    assert repo.query_one("SELECT COUNT(*) c FROM port_forwards")["c"] == 2
    assert repo.query_one("SELECT COUNT(*) c FROM audit_log")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM usage_snapshots")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM host_snapshots")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM webssh_tokens")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM port_allocations")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM settings")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM device_tracking")["c"] == 1
    assert repo.query_one("SELECT COUNT(*) c FROM cooldowns")["c"] == 1


def test_docker_columns_and_tables_are_added(v10_database):
    migrations.migrate(v10_database, skip_backup=True)
    data = migrations.inspect_database(v10_database)
    vps_columns = set(data["detail"]["vps"]["columns"])
    for column in ("backend", "docker_container_id", "docker_image", "volume_name",
                   "ssh_host_port", "pid_limit", "kvm_enabled", "apparmor_enabled",
                   "quota_method", "quota_enforced", "quota_project_id", "provision_state",
                   "restart_count", "last_health_ts"):
        assert column in vps_columns, f"vps.{column} was not added"

    node_columns = set(data["detail"]["nodes"]["columns"])
    for column in ("transport", "docker_host", "docker_tls_ca", "docker_tls_cert",
                   "docker_tls_key", "enabled", "quota_supported"):
        assert column in node_columns, f"nodes.{column} was not added"

    for table in ("docker_ports", "docker_backups", "docker_images", "resource_plans",
                  "storage_quotas", "migration_reports"):
        assert table in data["tables"], f"table {table} was not created"


def test_legacy_lxc_rows_are_tagged_not_converted(v10_database):
    migrations.migrate(v10_database, skip_backup=True)
    repo = Repository(v10_database)
    for row in repo.list_vps():
        assert row["backend"] == models.LEGACY_BACKEND, row["container_name"]
        # Tagging must not invent Docker state that does not exist.
        assert not row["docker_container_id"]
        assert not row["volume_name"]


def test_migration_is_idempotent(v10_database):
    first = migrations.migrate(v10_database, skip_backup=True)
    second = migrations.migrate(v10_database, skip_backup=True)
    third = migrations.migrate(v10_database, skip_backup=True)
    assert first.ok and second.ok and third.ok
    assert first.columns_added, "the first run should have added columns"
    assert not second.columns_added and not second.tables_created
    assert not third.columns_added and not third.tables_created
    assert Repository(v10_database).schema_version() == models.SCHEMA_VERSION


def test_dry_run_changes_nothing(v10_database):
    report = migrations.migrate(v10_database, dry_run=True, skip_backup=True)
    assert report.ok
    assert report.dry_run is True
    data = migrations.inspect_database(v10_database)
    assert int(data["schema_version"]) == 10
    assert "docker_ports" not in data["tables"]


def test_backup_is_written_and_verified(v10_database, tmp_path):
    backup_dir = tmp_path / "backups"
    report = migrations.migrate(v10_database, backup_dir=backup_dir, skip_backup=False)
    assert report.ok
    assert report.backup_path, "no pre-migration backup was recorded"
    backup = Path(report.backup_path)
    assert backup.exists()
    # The backup must be a usable copy of the *pre-migration* database.
    check = sqlite3.connect(backup)
    try:
        assert check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "10"
        assert check.execute("SELECT COUNT(*) FROM vps").fetchone()[0] == 2
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_report_is_written_to_disk(v10_database, tmp_path):
    report_dir = tmp_path / "reports"
    report = migrations.migrate(v10_database, report_dir=report_dir, skip_backup=True)
    assert report.ok
    reports = sorted(report_dir.glob("*.json"))
    assert reports, "no migration report file was written"
    payload = __import__("json").loads(reports[-1].read_text())
    assert payload["from_version"] == 10
    assert payload["to_version"] == models.SCHEMA_VERSION


def test_v13_backfills_existing_proxmox_security(v10_database):
    """The ordinary-column defaults must not disable KVM on old Proxmox rows."""
    conn = sqlite3.connect(v10_database)
    try:
        # Simulate a v12 row which already has Docker identity columns but not
        # the v13 security columns.
        conn.execute("ALTER TABLE vps ADD COLUMN backend TEXT DEFAULT 'docker'")
        conn.execute("ALTER TABLE vps ADD COLUMN docker_image TEXT")
        conn.execute(
            "INSERT INTO vps (user_id, node_id, container_name, ram, cpu, storage, config,"
            " os_version, status, backend, docker_image) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("333", 1, "pve-vps-3", "4GB", "2", "40GB", "Proxmox",
             config.PROXMOX_IMAGE, "running", "docker", config.PROXMOX_IMAGE))
        conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()

    report = migrations.migrate(v10_database, skip_backup=True)
    assert report.ok, report.summary_text()
    row = Repository(v10_database).get_vps_by_container("pve-vps-3")
    assert row["kvm_enabled"] is True
    assert row["apparmor_enabled"] is False


def test_fresh_database_gets_the_full_v11_schema(tmp_path):
    path = tmp_path / "fresh.db"
    report = migrations.migrate(path, skip_backup=True)
    assert report.ok
    data = migrations.inspect_database(path)
    assert int(data["schema_version"]) == models.SCHEMA_VERSION
    assert "docker_ports" in data["tables"]


def test_rollback_note_explains_how_to_undo(v10_database):
    migrations.migrate(v10_database, skip_backup=True)
    note = migrations.rollback_note()
    # The note has to tell an operator how to get back: restore the backup.
    assert "backup" in note.lower()
    assert "restore" in note.lower()
    assert "backup_path" in note


def test_corrupt_database_is_refused(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a sqlite database at all" * 40)
    report = migrations.migrate(path, skip_backup=True)
    assert report.ok is False
    assert report.error




def test_base_schema_matches_bot_schema():
    """
    `models.BASE_SCHEMA_SQL` must stay a verbatim copy of bot.py's `SCHEMA_SQL`.

    A hand-written approximation drifted once already: the copy was missing
    `device_tracking.ip_hash`, so a freshly bootstrapped database made bot.py's
    `CREATE INDEX idx_device_ip ON device_tracking(ip_hash)` fail on first start.
    """
    import re
    from pathlib import Path

    from tgdbot.database import models

    bot_source = Path(__file__).resolve().parent.parent / "bot.py"
    text = bot_source.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(r'^SCHEMA_SQL = r?"""(.*?)"""', text, re.M | re.S)
    assert match, "could not find SCHEMA_SQL in bot.py"
    assert models.BASE_SCHEMA_SQL.strip("\n") == match.group(1).strip("\n"), (
        "models.BASE_SCHEMA_SQL has drifted from bot.py SCHEMA_SQL — "
        "re-copy it verbatim")


def test_migration_can_bootstrap_a_brand_new_database(tmp_path):
    """
    A fresh install runs `migrate_database.py` before the bot has ever started,
    so `migrate()` must be able to build the schema on its own.
    """
    target = tmp_path / "fresh.db"
    assert not target.exists()

    report = migrations.migrate(target, backup_dir=tmp_path / "b",
                                report_dir=tmp_path / "r", skip_backup=True)
    assert report.ok, report.error
    assert target.exists()
    for table in models.BASE_TABLES:
        assert table in report.tables_before or table in report.tables_created, table
    for table in ("docker_ports", "docker_backups", "docker_images"):
        assert table in report.tables_created
