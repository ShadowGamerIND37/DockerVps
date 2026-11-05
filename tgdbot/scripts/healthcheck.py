#!/usr/bin/env python3
"""
TGD Bot health check.

Verifies the whole stack without touching customer data:

* Python dependencies
* configuration sanity (port ranges, directories, security policy)
* database presence, schema version and integrity
* Docker Engine reachability on every node
* runtime assets (the container supervisor script)
* storage quota capability — reported honestly, including "not enforceable"

Exit code 0 = healthy, 1 = warnings only, 2 = failed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tgdbot import config                                  # noqa: E402
from tgdbot.database import migrations                     # noqa: E402
from tgdbot.database.repository import Repository          # noqa: E402
from tgdbot.docker_backend import monitoring, nodes as nodes_mod, resources, security, ssh  # noqa: E402

OK, WARN, FAIL = "ok", "warn", "fail"


def line(status: str, label: str, detail: str = "") -> None:
    marker = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}[status]
    print(f"  {marker} {label:<34} {detail}")


def check_dependencies(results: dict) -> None:
    print("\n[dependencies]")
    for module in ("docker", "discord", "dotenv", "requests", "paramiko", "flask"):
        try:
            __import__(module)
            line(OK, module)
        except Exception as exc:
            severity = FAIL if module in ("docker", "discord") else WARN
            line(severity, module, str(exc))
            results[severity] += 1


def check_config(results: dict) -> None:
    print("\n[configuration]")
    if config.DOCKER_SSH_PORT_END <= config.DOCKER_SSH_PORT_START:
        line(FAIL, "ssh port range", "end must be greater than start")
        results[FAIL] += 1
    else:
        span = config.DOCKER_SSH_PORT_END - config.DOCKER_SSH_PORT_START
        line(OK, "ssh port range",
             f"{config.DOCKER_SSH_PORT_START}-{config.DOCKER_SSH_PORT_END} ({span} ports)")
    if config.DOCKER_PORT_RANGE_END <= config.DOCKER_PORT_RANGE_START:
        line(FAIL, "user port range", "end must be greater than start")
        results[FAIL] += 1
    else:
        line(OK, "user port range",
             f"{config.DOCKER_PORT_RANGE_START}-{config.DOCKER_PORT_RANGE_END}")
    line(OK, "default image", config.DOCKER_DEFAULT_IMAGE)
    line(OK, "publish ip", config.DOCKER_PUBLISH_IP)

    if config.DOCKER_PRIVILEGED:
        line(FAIL, "privileged containers", "DOCKER_PRIVILEGED=true is host-equivalent access")
        results[FAIL] += 1
    else:
        line(OK, "privileged containers", "disabled")
    line(OK if not config.DOCKER_NO_NEW_PRIVILEGES else WARN, "no-new-privileges",
         "off (breaks su/sudo + PAM sshd)" if not config.DOCKER_NO_NEW_PRIVILEGES else "on")
    line(OK, "capabilities dropped", f"{len(security.cap_drop_list())} capabilities")
    line(OK, "default limits",
         f"{config.DOCKER_DEFAULT_RAM} MiB RAM / {config.DOCKER_DEFAULT_CPU:g} vCPU / "
         f"{config.DOCKER_DEFAULT_PID_LIMIT} PIDs")


def check_directories(results: dict) -> None:
    print("\n[directories]")
    for label, path in (("vps data dir", config.DOCKER_VPS_DATA_DIR),
                        ("backup dir", config.DOCKER_BACKUP_DIR),
                        ("runtime dir", config.DOCKER_RUNTIME_DIR),
                        ("secret dir", config.DOCKER_SECRET_DIR)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            writable = Path(path).exists() and __import__("os").access(path, __import__("os").W_OK)
            line(OK if writable else WARN, label, f"{path}{' (not writable)' if not writable else ''}")
            if not writable:
                results[WARN] += 1
        except OSError as exc:
            line(WARN, label, f"{path}: {exc}")
            results[WARN] += 1


def check_database(db_path: Path, results: dict) -> None:
    print("\n[database]")
    if not db_path.exists():
        line(WARN, "database file", f"{db_path} does not exist yet")
        results[WARN] += 1
        return
    data = migrations.inspect_database(db_path)
    line(OK, "path", str(db_path))
    line(OK if data["integrity"] == "ok" else FAIL, "integrity_check", data["integrity"])
    if data["integrity"] != "ok":
        results[FAIL] += 1
    version = data["schema_version"]
    if version is None or int(version) < migrations.models.SCHEMA_VERSION:
        line(WARN, "schema version", f"v{version} — run migrate_database.py (want v"
                                     f"{migrations.models.SCHEMA_VERSION})")
        results[WARN] += 1
    else:
        line(OK, "schema version", f"v{version}")

    repo = Repository(db_path)
    for table in ("vps", "nodes", "docker_ports", "docker_backups", "docker_images",
                  "resource_plans", "usage_snapshots", "audit_log"):
        if table not in data["tables"]:
            line(WARN, f"table {table}", "missing — run the migration")
            results[WARN] += 1
            continue
        count = data["detail"][table]["rows"]
        line(OK, f"table {table}", f"{count} row(s)")
    legacy = [vps for vps in repo.list_vps() if str(vps.get("backend")) != "docker"]
    if legacy:
        line(WARN, "legacy LXC records",
             f"{len(legacy)} row(s) tagged '{legacy[0].get('backend')}' — not managed by Docker")
        results[WARN] += 1


def check_nodes(db_path: Path, results: dict) -> None:
    print("\n[docker nodes]")
    if not db_path.exists():
        line(WARN, "nodes", "no database")
        results[WARN] += 1
        return
    repo = Repository(db_path)
    nodes = repo.list_nodes()
    if not nodes:
        line(WARN, "nodes", "none registered")
        results[WARN] += 1
        return
    for node in nodes:
        report = nodes_mod.health(node)
        detail = f"{report.get('transport')} · {report.get('endpoint')}"
        if report.get("online"):
            detail += f" · server {report.get('server_version')}"
            line(OK, f"node {node['id']} {node['name']}", detail)
        else:
            line(WARN, f"node {node['id']} {node['name']}",
                 f"{detail} · {report.get('error') or 'unreachable'}")
            results[WARN] += 1
        if not node.get("enabled", True):
            line(WARN, f"node {node['id']} enabled", "disabled")
            results[WARN] += 1


def check_runtime_assets(results: dict) -> None:
    print("\n[runtime assets]")
    try:
        assets = ssh.ensure_runtime_assets()
        line(OK, "supervisor script", assets["init_script"])
        path = Path(assets["init_script"])
        content = path.read_text(encoding="utf-8")
        for marker in ("install_openssh", "ensure_host_keys", "apply_credential", "exec \"$SSHD_BIN\""):
            if marker in content:
                line(OK, f"supervisor step {marker}", "present")
            else:
                line(FAIL, f"supervisor step {marker}", "missing")
                results[FAIL] += 1
    except OSError as exc:
        line(WARN, "supervisor script", str(exc))
        results[WARN] += 1


def check_quota_support(results: dict) -> None:
    print("\n[storage quotas]")
    target = str(config.DOCKER_VPS_DATA_DIR)
    report = resources.detect_quota_support(target, node_is_local=True)
    if report.supported:
        line(OK, "quota enforcement", f"{report.method} on {report.filesystem}")
    else:
        line(WARN, "quota enforcement", f"NOT enforceable ({report.method}): {report.detail}")
        results[WARN] += 1
    try:
        total, used, free = shutil.disk_usage("/")
        line(OK, "root filesystem",
             f"{total / 1024 ** 3:.1f} GiB total, {free / 1024 ** 3:.1f} GiB free "
             f"({used / total * 100:.0f}% used)")
    except OSError:
        pass


def check_engine(results: dict) -> None:
    print("\n[docker engine]")
    try:
        import docker

        client = docker.DockerClient(base_url=config.DOCKER_HOST_URL,
                                     timeout=config.DOCKER_API_TIMEOUT)
        client.ping()
        info = monitoring.engine_info(client)
        line(OK, "ping", f"server {info['server_version']} · api {info['api_version']}")
        line(OK, "host", f"{info['os']} · kernel {info['kernel']}")
        line(OK, "capacity",
             f"{info['cpus']} vCPU · {info['ram_total'] / 1024 ** 3:.1f} GiB RAM")
        line(OK, "storage driver", f"{info['storage_driver']} {info['storage_status']}")
        line(OK, "containers",
             f"{info['containers_running']} running / {info['containers_stopped']} stopped")
        client.close()
    except Exception as exc:
        line(FAIL, "engine", str(exc))
        results[FAIL] += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(config.DATABASE_PATH))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-engine", action="store_true")
    args = parser.parse_args()

    results = {OK: 0, WARN: 0, FAIL: 0}
    db_path = Path(args.database).expanduser()

    print("=" * 78)
    print(f" TGD Bot health check — {config.DOCKER_DEFAULT_IMAGE} · schema v{migrations.models.SCHEMA_VERSION}")
    print("=" * 78)

    check_dependencies(results)
    check_config(results)
    check_directories(results)
    check_database(db_path, results)
    check_nodes(db_path, results)
    check_runtime_assets(results)
    check_quota_support(results)
    if not args.skip_engine:
        check_engine(results)

    print("\n" + "=" * 78)
    status = "HEALTHY" if not results[WARN] and not results[FAIL] else (
        "FAILED" if results[FAIL] else "HEALTHY WITH WARNINGS")
    print(f" {status} — {results[OK]} ok, {results[WARN]} warning(s), {results[FAIL]} failure(s)")
    print("=" * 78)

    if args.json:
        print(json.dumps(results))
    return 2 if results[FAIL] else (1 if results[WARN] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
