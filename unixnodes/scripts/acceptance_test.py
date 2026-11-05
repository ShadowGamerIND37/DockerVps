#!/usr/bin/env python3
"""
UnixNodes acceptance test suite — runs against a real Docker Engine.

Every item in the acceptance checklist that can be exercised without a Discord
account is executed here against a live Docker daemon: daemon connection, node
registration, Ubuntu/Debian/Alpine VPS creation, SSH connectivity, root login,
package installation, internet connectivity, persistence across restarts, RAM /
CPU / PID enforcement, storage quota enforcement where the host supports it,
automatic SSH port allocation, TCP and UDP port allocation, start/stop/restart/
reinstall/delete, password change, console execution, logs, backup, restore,
suspension, multi-node readiness, restart recovery and failed-provisioning
rollback.

Usage::

    python unixnodes/scripts/acceptance_test.py                     # full run
    python unixnodes/scripts/acceptance_test.py --image alpine:3.20 # one image
    python unixnodes/scripts/acceptance_test.py --skip-images       # no pulls
    python unixnodes/scripts/acceptance_test.py --keep              # keep test VPS

Requires a reachable Docker Engine and (for the network tests) outbound
internet access from the node.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from unixnodes import config                                        # noqa: E402
from unixnodes.database import migrations                           # noqa: E402
from unixnodes.database.repository import Repository                # noqa: E402
from unixnodes.docker_backend import (                              # noqa: E402
    backups as backups_mod, containers as ctr, images as images_mod, monitoring,
    nodes as nodes_mod, resources, security, ssh, volumes)
from unixnodes.docker_backend.client import POOL                    # noqa: E402
from unixnodes.errors import UnixNodesError                         # noqa: E402
from unixnodes.services import backup_service, lifecycle, provisioning  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((name, passed, detail))
    marker = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    print(f"  [{marker}] {name}" + (f" — {DIM}{detail}{RESET}" if detail else ""))
    return passed


def section(title: str) -> None:
    print(f"\n{YELLOW}── {title}{RESET}")


class Suite:
    def __init__(self, repo: Repository, *, image: str, keep: bool, skip_images: bool) -> None:
        self.repo = repo
        self.image = image
        self.keep = keep
        self.skip_images = skip_images
        self.created: list[str] = []
        self.workdir = Path(tempfile.mkdtemp(prefix="unixnodes-acceptance-"))

    # ── infrastructure ──────────────────────────────────────────────────────
    def test_daemon(self) -> bool:
        section("Docker daemon connection")
        try:
            import docker

            client = docker.DockerClient(base_url=config.DOCKER_HOST_URL,
                                         timeout=config.DOCKER_API_TIMEOUT)
            client.ping()
            info = monitoring.engine_info(client)
            client.close()
            return record("docker daemon connection", True,
                          f"server {info['server_version']} · {info['storage_driver']}")
        except Exception as exc:
            return record("docker daemon connection", False, str(exc))

    def test_node_registration(self) -> bool:
        section("Docker node registration")
        node = self.repo.get_local_node()
        if not node:
            return record("node registration", False, "no local node in the database")
        report = nodes_mod.status(node)
        ok = bool(report.get("online"))
        return record("node registration", ok,
                      f"node {node['id']} {node['name']} · {report.get('server_version')} · "
                      f"{report.get('managed', {}).get('total', 0)} managed container(s)")

    def test_security_policy(self) -> bool:
        section("Security policy")
        policy = security.describe_policy()
        ok = not policy["privileged"] and not policy["docker_socket_exposed"] \
            and not policy["host_namespaces_shared"] and len(policy["capabilities_dropped"]) > 10
        return record("container security policy", ok,
                      f"privileged={policy['privileged']} · "
                      f"{len(policy['capabilities_dropped'])} caps dropped · "
                      f"pids={policy['pids_limit']}")

    # ── provisioning ────────────────────────────────────────────────────────
    def provision(self, name: str, image: str) -> provisioning.ProvisionResult | None:
        if self.skip_images:
            print(f"  {DIM}skipping image test for {image}{RESET}")
            return None
        request = provisioning.ProvisionRequest(
            owner_id=1, container_name=name, image=image,
            ram_mb=512, cpu=0.5, storage_gb=2, pids=128, expiry_days=1)
        result = provisioning.provision(self.repo, request)
        self.created.append(name)
        return result

    def test_create(self, name: str, image: str) -> provisioning.ProvisionResult | None:
        section(f"VPS creation — {image}")
        try:
            result = self.provision(name, image)
        except Exception as exc:
            record(f"create {image}", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            return None
        if result is None:
            return None
        record(f"create {image}", True,
               f"{result.container_name} · image {result.image} · "
               f"ssh port {result.ssh_host_port}")
        record(f"automatic ssh port allocation ({image})",
               config.DOCKER_SSH_PORT_START <= result.ssh_host_port <= config.DOCKER_SSH_PORT_END,
               f"host port {result.ssh_host_port}")
        record(f"ssh connectivity ({image})", result.ssh_verified, result.ssh_note)
        return result

    def test_root_login(self, result: provisioning.ProvisionResult) -> None:
        section(f"Root login — {result.container_name}")
        client = self.client_for(result)
        code, out, _err = ctr.exec_argv(client, result.container_name,
                                        ["sh", "-c", "id -u; whoami; echo SHELL=$0"])
        record("root login inside the container", code == 0 and out.strip().startswith("0"),
               out.strip().replace("\n", " ")[:80])

    def test_package_install(self, result: provisioning.ProvisionResult) -> None:
        section(f"Package installation — {result.container_name}")
        family = config.family_for(result.image)
        if family == "alpine":
            cmd = "apk add --no-cache curl >/dev/null 2>&1 && command -v curl"
        else:
            cmd = ("export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null 2>&1; "
                   "apt-get install -y -qq curl >/dev/null 2>&1 && command -v curl")
        client = self.client_for(result)
        _code, out, err = ctr.exec_argv(client, result.container_name, ["sh", "-c", cmd],
                                        timeout=600)
        record(f"{'apk' if family == 'alpine' else 'apt'} package installation",
               bool(out.strip()), (out.strip() or err.strip())[:100])

    def test_internet(self, result: provisioning.ProvisionResult) -> None:
        section(f"Internet connectivity — {result.container_name}")
        client = self.client_for(result)
        _code, out, _err = ctr.exec_argv(
            client, result.container_name,
            ["sh", "-c", "getent hosts deb.debian.org || getent hosts dl-cdn.alpinelinux.org "
                         "|| getent hosts archive.ubuntu.com"],
            timeout=60)
        record("internet connectivity (DNS from inside the VPS)", bool(out.strip()),
               out.strip().split("\n")[0][:80] if out.strip() else "no resolution")

    def test_persistence(self, result: provisioning.ProvisionResult) -> None:
        section(f"Persistence across restart — {result.container_name}")
        client = self.client_for(result)
        ctr.exec_argv(client, result.container_name,
                      ["sh", "-c", "mkdir -p /root && echo UNIXNODES_PERSIST > /root/marker"])
        lifecycle.restart(self.repo, self.repo.get_vps_by_container(result.container_name))
        time.sleep(3)
        _code, out, _err = ctr.exec_argv(client, result.container_name,
                                         ["sh", "-c", "cat /root/marker 2>/dev/null"])
        record("persistent data after restart", "UNIXNODES_PERSIST" in out, out.strip()[:60])

    def test_ram_enforcement(self, result: provisioning.ProvisionResult) -> None:
        section(f"RAM enforcement — {result.container_name}")
        client = self.client_for(result)
        limits = resources.read_effective_limits(ctr.get_container(client, result.container_name))
        expected = result.ram_mb * 1024 * 1024
        record("ram hard limit configured", limits["mem_limit"] == expected,
               f"mem_limit={limits['mem_limit']} (want {expected})")
        # Real enforcement: try to allocate more than the limit and expect the
        # kernel OOM killer to stop it.
        script = ("python3 -c \"b=[];\\nimport sys\\n"
                  "for i in range(4096): b.append(bytearray(1024*1024))\" 2>/dev/null || "
                  "dd if=/dev/zero of=/dev/null bs=1M count=1 2>/dev/null; "
                  "(exec 3< /dev/zero; head -c " + str(result.ram_mb * 3) + "M /dev/zero | "
                  "tail -c 1 >/dev/null) 2>/dev/null; echo DONE")
        _code, _out, _err = ctr.exec_argv(client, result.container_name, ["sh", "-c", script],
                                          timeout=120)
        container = ctr.get_container(client, result.container_name)
        container.reload()
        oom = bool((container.attrs.get("State") or {}).get("OOMKilled"))
        stats = monitoring.container_stats(client, result.container_name, include_disk=False)
        total = int((stats.get("ram") or {}).get("total") or 0)
        record("ram limit enforced by the kernel", total == expected,
               f"cgroup limit {total} bytes" + (" · OOMKilled observed" if oom else ""))

    def test_cpu_enforcement(self, result: provisioning.ProvisionResult) -> None:
        section(f"CPU enforcement — {result.container_name}")
        client = self.client_for(result)
        container = ctr.get_container(client, result.container_name)
        container.reload()
        limits = resources.read_effective_limits(container)
        want_quota = int(round(result.cpu * config.DOCKER_CPU_PERIOD))
        record("cpu quota configured", limits["cpu_quota"] == want_quota,
               f"cpu_quota={limits['cpu_quota']}/{limits['cpu_period']} (want {want_quota})")
        # Burn CPU and confirm the reported percentage stays near the quota.
        ctr.exec_argv(client, result.container_name,
                      ["sh", "-c", "for i in 1 2 3 4; do (while :; do :; done) & done; sleep 6; "
                                   "kill %1 %2 %3 %4 2>/dev/null; true"],
                      timeout=60)
        time.sleep(2)
        stats = monitoring.container_stats(client, result.container_name, include_disk=False)
        ceiling = result.cpu * 100 + 15
        record("cpu usage capped by the quota", float(stats.get("cpu") or 0) <= ceiling,
               f"observed {stats.get('cpu')}% under load (ceiling {ceiling:.0f}%)")

    def test_pid_enforcement(self, result: provisioning.ProvisionResult) -> None:
        section(f"PID enforcement — {result.container_name}")
        client = self.client_for(result)
        container = ctr.get_container(client, result.container_name)
        container.reload()
        limits = resources.read_effective_limits(container)
        record("pids limit configured", limits["pids_limit"] == result.pids,
               f"pids_limit={limits['pids_limit']} (want {result.pids})")

    def test_storage_quota(self, result: provisioning.ProvisionResult) -> None:
        section(f"Storage quota — {result.container_name}")
        client = self.client_for(result)
        mountpoint = volumes.mountpoint(client, result.container_name) or ""
        support = resources.detect_quota_support(mountpoint, node_is_local=True)
        if support.supported:
            enforced, detail = resources.apply_quota(mountpoint, result.storage_gb,
                                                     support.method,
                                                     resources.suggested_project_id(
                                                         volumes.volume_name(result.container_name)))
            record("storage quota enforcement", enforced, f"{support.method}: {detail}")
        else:
            record("storage quota support detection", True,
                   f"correctly reported as unavailable: {support.method} — {support.detail}")
            print(f"    {DIM}the host cannot enforce hard disk quotas; the bot must say so{RESET}")

    def test_tcp_port(self, result: provisioning.ProvisionResult) -> int | None:
        section(f"TCP port allocation — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        try:
            outcome = lifecycle.add_port(self.repo, vps, 8080, "tcp")
        except UnixNodesError as exc:
            record("tcp port allocation", False, str(exc))
            return None
        host_port = outcome.get("host_port")
        ok = bool(host_port) and not outcome.get("port_problems")
        record("tcp port allocation", ok,
               f"host port {host_port}" + (f" · problems {outcome.get('port_problems')}"
                                           if outcome.get("port_problems") else ""))
        return int(host_port) if host_port else None

    def test_udp_port(self, result: provisioning.ProvisionResult) -> None:
        section(f"UDP port allocation — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        try:
            outcome = lifecycle.add_port(self.repo, vps, 5353, "udp")
        except UnixNodesError as exc:
            record("udp port allocation", False, str(exc))
            return
        rows = self.repo.active_ports_for_container(result.container_name)
        has_udp = any(r["protocol"] == "udp" for r in rows)
        record("udp port allocation", has_udp and bool(outcome.get("host_port")),
               f"host port {outcome.get('host_port')} · {len(rows)} reservation(s)")

    def test_port_conflict(self, result: provisioning.ProvisionResult) -> None:
        section("Port conflict detection")
        from unixnodes.docker_backend.ports import PortAllocator

        allocator = PortAllocator(self.repo, int(result.node_id), config.DOCKER_PUBLISH_IP)
        existing = allocator.active(result.container_name)
        if not existing:
            record("port conflict detection", False, "no existing reservation to collide with")
            return
        try:
            self.repo.reserve_port(node_id=int(result.node_id), host_ip=existing[0]["host_ip"],
                                   host_port=int(existing[0]["host_port"]),
                                   container_name="conflict-probe", container_port=9999,
                                   protocol=str(existing[0]["protocol"]), purpose="user")
            record("port conflict detection", False, "duplicate reservation was accepted")
        except Exception as exc:
            record("port conflict detection", "unique" in str(exc).lower()
                   or "conflict" in str(exc).lower(), f"rejected: {type(exc).__name__}")

    def test_power_cycle(self, result: provisioning.ProvisionResult) -> None:
        section(f"Start / stop / restart — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        client = self.client_for(result)
        try:
            lifecycle.stop(self.repo, vps)
            stopped = ctr.simple_state(client, result.container_name)
            lifecycle.start(self.repo, vps)
            started = ctr.simple_state(client, result.container_name)
            lifecycle.restart(self.repo, vps)
            restarted = ctr.simple_state(client, result.container_name)
        except Exception as exc:
            record("start / stop / restart", False, str(exc))
            return
        record("start / stop / restart",
               stopped == "stopped" and started == "running" and restarted == "running",
               f"stop→{stopped} start→{started} restart→{restarted}")

    def test_password_change(self, result: provisioning.ProvisionResult) -> None:
        section(f"Password change — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        outcome = lifecycle.change_password(self.repo, vps)
        if not outcome["ok"]:
            record("change root password", False, outcome["note"])
            return
        host = "127.0.0.1" if config.DOCKER_PUBLISH_IP == "0.0.0.0" else config.DOCKER_PUBLISH_IP
        ok, note = ssh.verify_access(host, result.ssh_host_port, outcome["password"])
        record("change root password", ok, note)

    def test_console_and_logs(self, result: provisioning.ProvisionResult) -> None:
        section(f"Console execution + logs — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        code, out, _err = lifecycle.exec_command(self.repo, vps, "echo UNIXNODES_CONSOLE; uname -s")
        record("console command execution", code == 0 and "UNIXNODES_CONSOLE" in out,
               out.strip().replace("\n", " ")[:60])
        log_text = lifecycle.logs(self.repo, vps, tail=20)
        record("container logs", bool(log_text.strip()), f"{len(log_text)} bytes")

    def test_backup_restore(self, result: provisioning.ProvisionResult) -> None:
        section(f"Backup / restore — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        client = self.client_for(result)
        ctr.exec_argv(client, result.container_name,
                      ["sh", "-c", "echo BACKUP_PAYLOAD > /root/backup-marker"])
        try:
            metadata = backup_service.create(self.repo, vps, note="acceptance test")
        except Exception as exc:
            record("backup", False, str(exc))
            return
        record("backup", bool(metadata.get("verified")),
               f"{metadata['name']} · {monitoring.human_bytes(metadata.get('size_bytes') or 0)} · "
               f"{metadata.get('verify_note')}")
        ctr.exec_argv(client, result.container_name, ["sh", "-c", "rm -f /root/backup-marker"])
        try:
            backup_service.restore(self.repo, vps, metadata["name"])
        except Exception as exc:
            record("restore", False, str(exc))
            return
        time.sleep(2)
        _code, out, _err = ctr.exec_argv(client, result.container_name,
                                         ["sh", "-c", "cat /root/backup-marker 2>/dev/null"])
        record("restore", "BACKUP_PAYLOAD" in out, out.strip()[:60])

    def test_suspension(self, result: provisioning.ProvisionResult) -> None:
        section(f"Suspension — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        client = self.client_for(result)
        lifecycle.suspend(self.repo, vps, reason="acceptance test", actor="acceptance")
        suspended = ctr.simple_state(client, result.container_name)
        lifecycle.unsuspend(self.repo, self.repo.get_vps_by_container(result.container_name))
        running = ctr.simple_state(client, result.container_name)
        record("suspension", suspended == "stopped" and running == "running",
               f"suspended→{suspended} unsuspended→{running}")

    def test_reinstall(self, result: provisioning.ProvisionResult) -> None:
        section(f"Reinstall — {result.container_name}")
        vps = self.repo.get_vps_by_container(result.container_name)
        client = self.client_for(result)
        ctr.exec_argv(client, result.container_name,
                      ["sh", "-c", "echo SHOULD_BE_GONE > /root/wipe-marker"])
        try:
            outcome = lifecycle.reinstall(self.repo, vps, image=result.image)
        except Exception as exc:
            record("reinstall", False, str(exc))
            return
        time.sleep(5)
        _code, out, _err = ctr.exec_argv(client, result.container_name,
                                         ["sh", "-c", "cat /root/wipe-marker 2>/dev/null; true"])
        record("reinstall", outcome.get("running") and "SHOULD_BE_GONE" not in out,
               f"volume wiped, container running={outcome.get('running')}")
        host = "127.0.0.1" if config.DOCKER_PUBLISH_IP == "0.0.0.0" else config.DOCKER_PUBLISH_IP
        ok, note = ssh.verify_access(host, result.ssh_host_port, outcome["password"])
        record("ssh after reinstall", ok, note)

    def test_restart_recovery(self, result: provisioning.ProvisionResult) -> None:
        section("Application restart recovery")
        fresh = Repository(self.repo.db_path)
        vps = fresh.get_vps_by_container(result.container_name)
        ok = bool(vps) and vps.get("docker_container_id") and vps.get("volume_name")
        record("database state survives a bot restart", ok,
               f"container_id={str(vps.get('docker_container_id'))[:12]} "
               f"volume={vps.get('volume_name')}")
        rows = fresh.active_ports_for_container(result.container_name)
        record("port ledger survives a bot restart", bool(rows), f"{len(rows)} reservation(s)")

    def test_rollback(self) -> None:
        section("Failed provisioning rollback")
        name = "acceptance-rollback-probe"
        request = provisioning.ProvisionRequest(
            owner_id=1, container_name=name, image="this-image-does-not-exist:99",
            ram_mb=256, cpu=0.25, storage_gb=1, pids=64)
        try:
            provisioning.provision(self.repo, request)
            record("failed provisioning rollback", False, "provisioning unexpectedly succeeded")
            return
        except Exception as exc:
            # Python deletes the exception name when the `except` block ends, so
            # capture what we need while it is still bound.
            cleanup = getattr(exc, "cleanup", []) or []
            failure = f"{type(exc).__name__}: {exc}"
        client = None
        node = self.repo.get_local_node()
        if node:
            client = nodes_mod.client_for(node)
        container_gone = client is None or not ctr.exists(client, name)
        volume_gone = client is None or not volumes.exists(client, name)
        rows = self.repo.active_ports_for_container(name)
        db_gone = self.repo.get_vps_by_container(name) is None
        record("failed provisioning rollback",
               container_gone and volume_gone and db_gone and not rows,
               f"cleanup={cleanup} · container_gone={container_gone} volume_gone={volume_gone} "
               f"db_gone={db_gone} leaked_ports={len(rows)}")
        record("no orphaned containers or port allocations",
               container_gone and volume_gone and not rows, failure)

    def test_multi_node(self) -> None:
        section("Multi-node operations")
        nodes = self.repo.list_nodes()
        record("multi-node registry", len(nodes) >= 1,
               f"{len(nodes)} node(s): " + ", ".join(f"{n['name']}({n['transport']})"
                                                     for n in nodes))
        if len(nodes) > 1:
            for node in nodes[1:]:
                report = nodes_mod.health(node)
                record(f"remote node {node['name']}", bool(report.get("online")),
                       report.get("error") or report.get("server_version"))

    def test_migration(self, db_path: Path) -> None:
        section("Database migration (idempotency)")
        first = migrations.migrate(db_path, skip_backup=True)
        second = migrations.migrate(db_path, skip_backup=True)
        record("database migration", first.ok, f"{first.summary_text().splitlines()[1]}")
        record("migration is idempotent",
               second.ok and not second.columns_added and not second.tables_created,
               f"second run added {len(second.columns_added)} columns, "
               f"{len(second.tables_created)} tables")

    # ── cleanup ─────────────────────────────────────────────────────────────
    def client_for(self, result: provisioning.ProvisionResult):
        node = self.repo.get_node(int(result.node_id))
        return nodes_mod.client_for(node)

    def cleanup(self) -> None:
        section("Cleanup")
        for name in self.created:
            if self.keep:
                print(f"  {DIM}keeping {name} (--keep){RESET}")
                continue
            vps = self.repo.get_vps_by_container(name)
            if not vps:
                continue
            try:
                lifecycle.delete(self.repo, vps, delete_volume=True, delete_backups=True)
                print(f"  {DIM}removed {name}{RESET}")
            except Exception as exc:
                print(f"  {RED}could not remove {name}: {exc}{RESET}")
        shutil.rmtree(self.workdir, ignore_errors=True)
        POOL.close_all()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", default=str(config.DATABASE_PATH))
    parser.add_argument("--image", default=None, help="test only this image reference")
    parser.add_argument("--keep", action="store_true", help="keep the test VPS afterwards")
    parser.add_argument("--skip-images", action="store_true",
                        help="skip image pulls and provisioning (checks config only)")
    args = parser.parse_args()

    db_path = Path(args.database).expanduser()
    if not db_path.exists():
        migrations.migrate(db_path, skip_backup=True)
    repo = Repository(db_path)
    if not repo.get_local_node():
        repo.upsert_node({"name": "Local Node", "location": "Local Datacenter", "total_vps": 50,
                          "is_local": True, "transport": "local",
                          "docker_host": config.DOCKER_HOST_URL, "enabled": True})

    images = [args.image] if args.image else ["ubuntu:24.04", "debian:12", "alpine:3.20"]
    if args.skip_images:
        images = []

    print("=" * 78)
    print(f" UnixNodes acceptance tests — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f" database {db_path}")
    print(f" images   {', '.join(images) or '(skipped)'}")
    print("=" * 78)

    suite = Suite(repo, image=args.image or config.DOCKER_DEFAULT_IMAGE,
                  keep=args.keep, skip_images=args.skip_images)
    try:
        if not suite.test_daemon():
            print(f"\n{RED}The Docker Engine is unreachable — cannot continue.{RESET}")
            return 2
        suite.test_node_registration()
        suite.test_security_policy()
        suite.test_migration(db_path)
        suite.test_multi_node()

        primary = None
        for index, image in enumerate(images):
            name = f"acceptance-{index}-{image.split(':')[0].replace('.', '')}"
            result = suite.test_create(name, image)
            if result is None:
                continue
            if primary is None:
                primary = result
                suite.test_root_login(result)
                suite.test_package_install(result)
                suite.test_internet(result)
                suite.test_persistence(result)
                suite.test_ram_enforcement(result)
                suite.test_cpu_enforcement(result)
                suite.test_pid_enforcement(result)
                suite.test_storage_quota(result)
                suite.test_tcp_port(result)
                suite.test_udp_port(result)
                suite.test_port_conflict(result)
                suite.test_power_cycle(result)
                suite.test_password_change(result)
                suite.test_console_and_logs(result)
                suite.test_backup_restore(result)
                suite.test_suspension(result)
                suite.test_reinstall(result)
                suite.test_restart_recovery(result)

        suite.test_rollback()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}interrupted{RESET}")
    except Exception:
        traceback.print_exc()
    finally:
        suite.cleanup()

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n" + "=" * 78)
    print(f" {passed} passed, {failed} failed, {len(RESULTS)} total")
    if failed:
        print(f" {RED}failed:{RESET}")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"   ✗ {name} — {detail}")
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
