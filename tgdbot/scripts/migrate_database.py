#!/usr/bin/env python3
"""
TGD Bot v11 database migration runner.

Usage::

    python tgdbot/scripts/migrate_database.py                 # migrate ./vps.db
    python tgdbot/scripts/migrate_database.py --database /path/to/vps.db
    python tgdbot/scripts/migrate_database.py --inspect       # report only, no changes
    python tgdbot/scripts/migrate_database.py --dry-run       # plan without writing
    python tgdbot/scripts/migrate_database.py --import-legacy-ports

The migration is additive and idempotent: it adds Docker metadata columns and
tables, seeds reference data, and tags pre-existing LXC-era VPS rows.  It never
drops a column, renames a table or deletes a row.  A verified backup is written
before anything changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tgdbot import config                      # noqa: E402
from tgdbot.database import migrations          # noqa: E402
from tgdbot.database.repository import Repository  # noqa: E402


def print_inspection(data: dict) -> None:
    print(f"database        : {data['path']}")
    print(f"exists          : {data['exists']}")
    print(f"schema version  : {data['schema_version']}")
    print(f"integrity       : {data['integrity']}")
    print(f"tables ({len(data['tables'])})     : {', '.join(data['tables'])}")
    print(f"indexes ({len(data['indexes'])})    : {', '.join(data['indexes'])}")
    print()
    print(f"{'table':<22} {'rows':>7}  columns")
    print("-" * 100)
    for table, detail in sorted(data["detail"].items()):
        columns = ", ".join(detail["columns"][:8])
        more = f" (+{len(detail['columns']) - 8} more)" if len(detail["columns"]) > 8 else ""
        print(f"{table:<22} {detail['rows']:>7}  {columns}{more}")
        if detail["foreign_keys"]:
            print(f"{'':<22} {'':>7}  FKs: {detail['foreign_keys']}")


def import_legacy_ports(repo: Repository) -> int:
    """
    Optionally copy the v10 ``port_forwards`` rows into the Docker ledger.

    Only do this when the containers have genuinely been re-created on Docker
    with the same host ports — otherwise you would be recording forwards that
    nothing is listening on.
    """
    rows = repo.query_all("SELECT * FROM port_forwards")
    imported = 0
    for row in rows:
        container = str(row["vps_container"])
        vps = repo.get_vps_by_container(container)
        if not vps:
            continue
        for protocol in ("tcp", "udp"):
            if str(row["protocol"] or "both").lower() not in ("both", protocol):
                continue
            try:
                repo.reserve_port(
                    node_id=int(vps["node_id"]), host_ip=config.DOCKER_PUBLISH_IP,
                    host_port=int(row["host_port"]), container_name=container,
                    container_port=int(row["vps_port"]), protocol=protocol,
                    purpose="ssh" if int(row["vps_port"]) == 22 else "user",
                    user_id=row["user_id"])
                imported += 1
            except Exception as exc:
                print(f"  skipped {container} {row['host_port']}/{protocol}: {exc}")
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", default=str(config.DATABASE_PATH),
                        help="path to vps.db")
    parser.add_argument("--backup-dir", default=None, help="where to write the pre-migration backup")
    parser.add_argument("--report-dir", default=str(config.MIGRATION_REPORT_DIR))
    parser.add_argument("--inspect", action="store_true",
                        help="print a full structural inventory and exit")
    parser.add_argument("--dry-run", action="store_true", help="plan without writing")
    parser.add_argument("--skip-backup", action="store_true",
                        help="do not write a pre-migration backup (not recommended)")
    parser.add_argument("--import-legacy-ports", action="store_true",
                        help="copy v10 port_forwards rows into the Docker port ledger")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    db_path = Path(args.database).expanduser()
    backup_dir = Path(args.backup_dir).expanduser() if args.backup_dir else None
    report_dir = Path(args.report_dir).expanduser()

    if args.inspect:
        if not db_path.exists():
            print(f"database {db_path} does not exist yet")
            return 0
        print_inspection(migrations.inspect_database(db_path))
        return 0

    report = migrations.migrate(
        db_path, backup_dir=backup_dir, report_dir=report_dir,
        dry_run=args.dry_run, skip_backup=args.skip_backup)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print("=" * 78)
        print(" TGD Bot v10 → v11 database migration")
        print("=" * 78)
        print(report.summary_text())
        print("=" * 78)
        if report.columns_added:
            print("columns added:")
            for column in report.columns_added:
                print(f"  + {column}")
        if report.tables_created:
            print("tables created:")
            for table in report.tables_created:
                print(f"  + {table}")
        print()
        print(migrations.rollback_note())

    if report.ok and args.import_legacy_ports:
        repo = Repository(db_path)
        count = import_legacy_ports(repo)
        print(f"\nimported {count} legacy port forward(s) into the Docker ledger")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
