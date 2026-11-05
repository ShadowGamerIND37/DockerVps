"""
Safe, idempotent schema migration from UnixNodes v10 (LXC) to v11 (Docker).

Rules this module never breaks:

1. The database is **backed up and integrity-checked before anything changes**.
2. Migration is additive: ``ALTER TABLE … ADD COLUMN`` and
   ``CREATE TABLE IF NOT EXISTS`` only.  No column is dropped, renamed or
   rewritten, and no row is deleted.
3. Running it twice is a no-op (``already_present`` entries in the report).
4. Existing LXC-era VPS rows are *tagged* ``backend='lxc-legacy'`` — they are
   never silently re-labelled as Docker containers, because the real
   infrastructure has not been verified.
5. A written report (console + JSON + ``migration_reports`` table) lists every
   change.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import models

logger = logging.getLogger("unixnodes.database.migrations")

EXPECTED_TABLES = (
    "meta", "admins", "nodes", "vps", "settings", "port_allocations",
    "port_forwards", "device_tracking", "cooldowns", "audit_log",
    "usage_snapshots", "host_snapshots", "webssh_tokens",
)


@dataclass
class MigrationReport:
    database: str
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    from_version: Optional[int] = None
    to_version: int = models.SCHEMA_VERSION
    backup_path: Optional[str] = None
    integrity: str = ""
    tables_before: List[str] = field(default_factory=list)
    tables_created: List[str] = field(default_factory=list)
    columns_added: List[str] = field(default_factory=list)
    columns_present: List[str] = field(default_factory=list)
    indexes_created: List[str] = field(default_factory=list)
    legacy_rows_tagged: int = 0
    seed_rows: Dict[str, int] = field(default_factory=dict)
    row_counts: Dict[str, int] = field(default_factory=int)  # type: ignore[arg-type]
    warnings: List[str] = field(default_factory=list)
    dry_run: bool = False
    ok: bool = True
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "database": self.database,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "backup_path": self.backup_path,
            "integrity": self.integrity,
            "tables_before": self.tables_before,
            "tables_created": self.tables_created,
            "columns_added": self.columns_added,
            "columns_present": self.columns_present,
            "indexes_created": self.indexes_created,
            "legacy_rows_tagged": self.legacy_rows_tagged,
            "seed_rows": self.seed_rows,
            "row_counts": self.row_counts,
            "warnings": self.warnings,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "error": self.error,
        }

    def summary_text(self) -> str:
        lines = [
            f"database      : {self.database}",
            f"schema        : v{self.from_version if self.from_version is not None else '?'} → v{self.to_version}",
            f"integrity     : {self.integrity or 'not checked'}",
            f"backup        : {self.backup_path or 'none'}",
            f"tables created: {len(self.tables_created)} {self.tables_created}",
            f"columns added : {len(self.columns_added)}",
            f"columns kept  : {len(self.columns_present)}",
            f"indexes added : {len(self.indexes_created)}",
            f"legacy tagged : {self.legacy_rows_tagged} LXC-era VPS row(s) marked '{models.LEGACY_BACKEND}'",
        ]
        if self.seed_rows:
            lines.append(f"seed rows     : " + ", ".join(f"{k}={v}" for k, v in self.seed_rows.items()))
        if self.row_counts:
            lines.append("row counts    : " + ", ".join(f"{k}={v}" for k, v in sorted(self.row_counts.items())))
        if self.warnings:
            lines.append("warnings      :")
            lines.extend(f"  - {item}" for item in self.warnings)
        if self.error:
            lines.append(f"ERROR         : {self.error}")
        lines.append(f"result        : {'OK' if self.ok else 'FAILED'} in {self.duration_seconds:.2f}s")
        return "\n".join(lines)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def list_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return sorted(str(row[0]) for row in rows)


def list_indexes(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
    return sorted(str(row[0]) for row in rows)


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def foreign_keys(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")]
    except sqlite3.Error:
        return []


def inspect_database(db_path: Path) -> Dict[str, Any]:
    """Full structural inventory — this is step 1 of the migration plan."""
    conn = connect(db_path)
    try:
        tables = list_tables(conn)
        detail: Dict[str, Any] = {}
        for table in tables:
            detail[table] = {
                "columns": table_columns(conn, table),
                "indexes": [str(row["name"]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?", (table,))],
                "foreign_keys": foreign_keys(conn, table),
                "rows": conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
            }
        version_row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return {
            "path": str(db_path),
            "exists": db_path.exists(),
            "schema_version": int(version_row[0]) if version_row else None,
            "tables": tables,
            "indexes": list_indexes(conn),
            "detail": detail,
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        conn.close()


def backup_database(db_path: Path, backup_dir: Path) -> Optional[Path]:
    """Verified SQLite backup taken before any change is made."""
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"pre_migration_{stamp}_{db_path.name}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    source = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        destination = sqlite3.connect(str(tmp))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    check = sqlite3.connect(str(tmp))
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Pre-migration backup failed integrity check: {result}")
    tmp.replace(target)
    # Keep the last ten pre-migration backups.
    for old in sorted(backup_dir.glob("pre_migration_*"))[:-10]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def migrate(db_path: Path, *, backup_dir: Optional[Path] = None,
            report_dir: Optional[Path] = None, dry_run: bool = False,
            skip_backup: bool = False) -> MigrationReport:
    """
    Run the migration.  Safe to call repeatedly.

    ``db_path`` may not exist yet — a fresh database is created from the
    existing v10 schema (which lives in ``bot.py``) plus the Docker additions.
    """
    started = time.time()
    report = MigrationReport(database=str(db_path),
                             started_at=datetime.now(timezone.utc).isoformat(),
                             dry_run=dry_run)
    report.row_counts = {}

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not skip_backup and db_path.exists():
        try:
            backup = backup_database(db_path, backup_dir or db_path.parent / "backups")
            report.backup_path = str(backup) if backup else None
        except Exception as exc:
            report.ok = False
            report.error = f"Backup failed, refusing to migrate: {exc}"
            return report

    conn = connect(db_path)
    try:
        report.tables_before = list_tables(conn)
        version_row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone() if "meta" in report.tables_before else None
        try:
            report.from_version = int(version_row[0]) if version_row else None
        except (TypeError, ValueError):
            report.from_version = None

        if dry_run:
            report.warnings.append("dry run — no changes were written")
            _finish(conn, report, started)
            return report

        conn.execute("PRAGMA journal_mode=WAL")

        # 0. bootstrap the base (v10) tables when they are missing -----------
        # bot.py's init_db() normally creates these, but the installer runs this
        # migration before the bot has ever started, so a brand-new database has
        # to be bootstrapped here.  Every statement is CREATE TABLE IF NOT
        # EXISTS — an existing database is never touched.
        missing_base = [table for table in models.BASE_TABLES if table not in report.tables_before]
        if missing_base:
            conn.executescript(models.BASE_SCHEMA_SQL)
            report.tables_before = list_tables(conn)
            report.warnings.append(
                "created the missing base tables: " + ", ".join(missing_base))
            logger.info("migration: bootstrapped base tables %s", ", ".join(missing_base))

        # 1. additive column changes on existing tables -----------------------
        for table, column_def in models.ADDED_COLUMNS:
            existing = table_columns(conn, table)
            if not existing:
                report.warnings.append(f"table '{table}' is missing — column '{column_def.split()[0]}' skipped")
                continue
            column = column_def.split()[0]
            if column in existing:
                report.columns_present.append(f"{table}.{column}")
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
            report.columns_added.append(f"{table}.{column}")
            logger.info("migration: %s.%s added", table, column)

        # 2. new tables ------------------------------------------------------
        before_indexes = set(list_indexes(conn))
        conn.executescript(models.NEW_TABLES_SQL)
        report.tables_created = [table for table in list_tables(conn)
                                 if table not in report.tables_before]
        report.indexes_created = [index for index in list_indexes(conn)
                                  if index not in before_indexes]

        # 3. seed reference data --------------------------------------------
        now_iso = datetime.now(timezone.utc).isoformat()
        added_plans = 0
        for name, ram, cpu, storage, pids, description in models.DEFAULT_PLANS:
            cursor = conn.execute(
                """INSERT INTO resource_plans (name, ram_mb, cpu, storage_gb, pids, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO NOTHING""",
                (name, ram, cpu, storage, pids, description, now_iso))
            added_plans += cursor.rowcount if cursor.rowcount > 0 else 0
        added_images = 0
        for reference, label, emoji, description in models.DEFAULT_IMAGES:
            cursor = conn.execute(
                """INSERT INTO docker_images (reference, label, emoji, description, approved, added_by, created_at)
                   VALUES (?, ?, ?, ?, 1, 'system', ?)
                   ON CONFLICT(reference) DO NOTHING""",
                (reference, label, emoji, description, now_iso))
            added_images += cursor.rowcount if cursor.rowcount > 0 else 0
        report.seed_rows = {"resource_plans": added_plans, "docker_images": added_images}

        # 4. tag (never convert) LXC-era records ----------------------------
        if "backend" in table_columns(conn, "vps"):
            already = conn.execute(
                "SELECT value FROM meta WHERE key='legacy_vps_tagged'").fetchone()
            if not already:
                cursor = conn.execute(
                    "UPDATE vps SET backend = ? WHERE backend IS NULL OR backend = '' OR backend = 'docker'",
                    (models.LEGACY_BACKEND,))
                # Only rows that pre-date Docker: they have no volume/container id.
                conn.execute(
                    "UPDATE vps SET backend = ? "
                    "WHERE (docker_container_id IS NOT NULL AND docker_container_id != '')",
                    (models.DOCKER_BACKEND,))
                report.legacy_rows_tagged = cursor.rowcount if cursor.rowcount > 0 else 0
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_vps_tagged', ?)",
                             (datetime.now(timezone.utc).isoformat(),))
                if report.legacy_rows_tagged:
                    report.warnings.append(
                        f"{report.legacy_rows_tagged} existing VPS row(s) were created by the LXC backend. "
                        "They are tagged 'lxc-legacy' and are NOT managed as Docker containers until an "
                        "administrator verifies the real infrastructure (see MIGRATION.md).")

        # 5. bookkeeping -----------------------------------------------------
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(models.SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated_at', ?)",
                     (datetime.now(timezone.utc).isoformat(),))

        # 6. row counts + integrity -----------------------------------------
        for table in list_tables(conn):
            try:
                report.row_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                continue
        report.integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if report.integrity != "ok":
            report.ok = False
            report.error = f"integrity_check returned {report.integrity}"
            conn.rollback()
            return report

        conn.execute(
            "INSERT INTO migration_reports (ts, from_version, to_version, backup_path, summary, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), report.from_version, report.to_version,
             report.backup_path, f"{len(report.columns_added)} columns, "
                                 f"{len(report.tables_created)} tables",
             json.dumps({"columns_added": report.columns_added,
                         "tables_created": report.tables_created,
                         "legacy_rows_tagged": report.legacy_rows_tagged})))
        conn.commit()
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        report.ok = False
        report.error = str(exc)
    finally:
        conn.close()

    _finish(None, report, started, report_dir=report_dir)
    return report


def _finish(conn: Optional[sqlite3.Connection], report: MigrationReport, started: float,
            report_dir: Optional[Path] = None) -> None:
    report.finished_at = datetime.now(timezone.utc).isoformat()
    report.duration_seconds = time.time() - started
    if report_dir is not None:
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (report_dir / f"migration_{stamp}.json").write_text(
                json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        except OSError as exc:
            report.warnings.append(f"could not write the JSON report: {exc}")


def rollback_note() -> str:
    return ("The migration is additive: to undo it, restore the pre-migration backup that was "
            "written before the change (see 'backup_path' in the report) and run the previous "
            "version of the bot. No data is destroyed by the migration itself.")
