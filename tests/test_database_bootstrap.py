"""Startup database-path regression tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fresh_configured_database_is_migrated_before_repository_use(tmp_path):
    """The bot and INFRA must initialize and query the same SQLite file.

    This runs import-time startup in an isolated process with DATABASE_PATH
    pointing at a fresh file. It used to migrate bot.py's hard-coded vps.db,
    then raise ``sqlite3.OperationalError: no such table: vps`` when the
    provisioning repository opened the configured file.
    """
    database = tmp_path / "fresh.db"
    env = os.environ.copy()
    env.update({
        "DATABASE_PATH": str(database),
        "DATABASE_BACKUP_DIR": str(tmp_path / "backups"),
        "MIGRATION_REPORT_DIR": str(tmp_path / "reports"),
        "WEBSSH_ENABLED": "false",
    })
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    probe = r'''
import sqlite3
from pathlib import Path
import bot

bot_path = Path(bot.DB_FILE).expanduser().resolve()
repo_path = Path(bot.INFRA.repo.db_path).expanduser().resolve()
print("bot DB_FILE:", bot_path)
print("INFRA repository:", repo_path)
assert bot_path == repo_path, (bot_path, repo_path)
# This is the same repository read performed at the start of provisioning.
assert bot.INFRA.repo.get_vps_by_container("bootstrap-probe") is None
with sqlite3.connect(repo_path) as conn:
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vps'"
    ).fetchone() == ("vps",)
print("fresh database has the vps table")
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fresh database has the vps table" in completed.stdout
