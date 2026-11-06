"""
CPU / RAM / PID limits and **real** storage quotas.

Docker enforces memory, CPU and PID limits through cgroups, and those are
genuine: a container cannot exceed ``mem_limit`` or its ``cpu_quota``.

Disk is different.  A Docker *volume* is just a directory on the host, so
``docker run --storage-opt size=…`` does not constrain it.  This module
therefore implements host level enforcement:

* **XFS with ``pquota``** — a project id is assigned to the volume directory
  and a hard byte limit is applied with ``xfs_quota``.  This is the
  recommended production configuration.
* **Btrfs with quota groups** — ``btrfs qgroup limit`` on the volume
  subvolume.
* **Anything else** — quota support is reported as unavailable and the bot
  must say so instead of advertising a limit it cannot enforce.

Enforcement runs on the *node host*.  It is available for the local node (the
bot has shell access there) and is reported as unavailable for remote nodes,
because the Docker API has no volume-quota endpoint.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import zlib
from typing import Any, Dict, Optional, Sequence, Tuple

from .. import config
from ..errors import QuotaSupportReport, QuotaUnsupported

logger = logging.getLogger("tgdbot.docker.resources")

#: The host quota is attached to the named data volume. Docker's writable
#: image layer is a separate storage location and is driver-dependent.
DATA_VOLUME_QUOTA_NOTE = (
    "Host quota covers the persistent data volume only; Docker's writable root "
    "filesystem is stored separately."
)

XFS_MAGIC = 0x58465342
BTRFS_MAGIC = 0x9123683E


# ── cgroup limits ───────────────────────────────────────────────────────────

def limits_for(ram_mb: int, cpu: float, pids: int = 0) -> Dict[str, Any]:
    """Translate a plan into Docker HostConfig limit values."""
    ram_mb = max(64, int(ram_mb or config.DOCKER_DEFAULT_RAM))
    cpu = max(0.0, float(cpu or config.DOCKER_DEFAULT_CPU))
    pids = int(pids or config.DOCKER_DEFAULT_PID_LIMIT)
    mem_limit = ram_mb * 1024 * 1024
    return {
        "mem_limit": mem_limit,
        "mem_reservation": int(mem_limit * config.DOCKER_MEMORY_RESERVATION_RATIO),
        "memswap_limit": mem_limit,
        "cpu_period": config.DOCKER_CPU_PERIOD,
        "cpu_quota": int(round(cpu * config.DOCKER_CPU_PERIOD)) if cpu > 0 else 0,
        "cpu_shares": max(2, int(cpu * 1024)) if cpu > 0 else 1024,
        "pids_limit": max(32, pids),
        "nano_cpus": int(cpu * 1_000_000_000) if cpu > 0 else 0,
    }


def describe_limits(ram_mb: int, cpu: float, pids: int = 0) -> str:
    limits = limits_for(ram_mb, cpu, pids)
    cores = limits["cpu_quota"] / limits["cpu_period"] if limits["cpu_period"] else 0
    return (f"{ram_mb} MiB RAM • {cores:g} vCPU (quota {limits['cpu_quota']}/{limits['cpu_period']}) "
            f"• {limits['pids_limit']} PIDs")


def read_effective_limits(container: Any) -> Dict[str, Any]:
    """Read the limits Docker actually applied to a container."""
    attrs = dict(getattr(container, "attrs", {}) or {})
    host = attrs.get("HostConfig") or {}
    return {
        "mem_limit": int(host.get("Memory") or 0),
        "mem_reservation": int(host.get("MemoryReservation") or 0),
        "memswap_limit": int(host.get("MemorySwap") or 0),
        "cpu_quota": int(host.get("CpuQuota") or 0),
        "cpu_period": int(host.get("CpuPeriod") or 0),
        "cpu_shares": int(host.get("CpuShares") or 0),
        "pids_limit": int(host.get("PidsLimit") or 0),
    }


# ── small subprocess helper ─────────────────────────────────────────────────

def _run(argv: Sequence[str], timeout: int = 60) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True,
                                   timeout=timeout, check=False)
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except FileNotFoundError:
        return 127, "", f"executable not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)


# ── filesystem detection ────────────────────────────────────────────────────

def filesystem_type(path: str) -> str:
    """Return a lowercase filesystem name for ``path`` (``xfs``, ``btrfs``, …)."""
    if not path:
        return ""
    code, out, _err = _run(["stat", "-f", "-c", "%T", "--", path])
    if code == 0 and out.strip():
        return out.strip().lower()
    return ""


def mount_options(path: str) -> str:
    """Mount options of the filesystem that contains ``path``."""
    target = str(path or "")
    best = ""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 4:
                    continue
                mount_point = parts[1].replace("\\040", " ")
                if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
                    if len(mount_point) > len(best):
                        best = parts[3]
    except OSError:
        return ""
    return best


def detect_quota_support(volume_path: str, *, node_is_local: bool = True) -> QuotaSupportReport:
    """
    Work out whether a hard disk quota can actually be enforced here.

    The result is stored on the VPS record so the bot can honestly report
    "enforced" versus "monitored only".
    """
    if not config.DOCKER_ENFORCE_DISK_QUOTA:
        return QuotaSupportReport(False, "disabled", "", "DOCKER_ENFORCE_DISK_QUOTA is off.")
    if not node_is_local:
        return QuotaSupportReport(
            False, "remote-node", "",
            "Remote Docker nodes expose no volume quota API; disk usage is monitored, not limited.")
    if not volume_path:
        return QuotaSupportReport(False, "no-volume", "", "Volume path unknown.")

    fstype = filesystem_type(volume_path)
    options = mount_options(volume_path)

    if fstype == "xfs":
        if "pquota" in options or "prjquota" in options:
            code, _out, err = _run(["xfs_quota", "-V"])
            if code == 0:
                return QuotaSupportReport(True, "xfs-project-quota", "xfs",
                                          "XFS project quotas are enabled on the volume filesystem.")
            return QuotaSupportReport(False, "xfs-missing-tools", "xfs",
                                      "XFS has pquota but xfs_quota is not installed on the node.")
        return QuotaSupportReport(
            False, "xfs-no-pquota", "xfs",
            "The XFS volume filesystem was not mounted with 'pquota'. Remount with "
            "-o pquota (or add it to /etc/fstab) to enable hard disk limits.")

    if fstype == "btrfs":
        code, _out, _err = _run(["btrfs", "qgroup", "show", "--", volume_path])
        if code == 0:
            return QuotaSupportReport(True, "btrfs-qgroup", "btrfs",
                                      "Btrfs quota groups are active on the volume filesystem.")
        return QuotaSupportReport(
            False, "btrfs-no-qgroup", "btrfs",
            "Btrfs quota groups are not enabled. Run 'btrfs quota enable' on the Docker "
            "filesystem to enforce hard disk limits.")

    return QuotaSupportReport(
        False, "unsupported-fs", fstype or "unknown",
        f"Filesystem '{fstype or 'unknown'}' does not support enforceable disk quotas. "
        "Use XFS (pquota) or Btrfs (qgroups) for the Docker data directory.")


# ── project id management ───────────────────────────────────────────────────

def suggested_project_id(volume_name: str) -> int:
    """Stable numeric project id in the 100000–899999 range."""
    return 100_000 + (zlib.crc32(volume_name.encode("utf-8")) % 800_000)


# ── enforcement ─────────────────────────────────────────────────────────────

def apply_xfs_quota(volume_path: str, size_gb: float, project_id: int) -> Tuple[bool, str]:
    """Tag the volume directory with a project id and set a hard byte limit."""
    if not volume_path:
        return False, "No volume path"
    pid = int(project_id)
    limit = f"{max(1, int(round(float(size_gb))))}g"

    code, _out, err = _run(["xfs_io", "-c", f"chproj {pid}", "-c", "chattr +P", "--", volume_path])
    if code != 0:
        return False, f"xfs_io chproj failed: {(err or 'unknown error').strip()[:200]}"

    code, _out, err = _run(["xfs_quota", "-x", "-c", f"limit -p bhard={limit} bsoft={limit} {pid}",
                            volume_path])
    if code != 0:
        return False, f"xfs_quota limit failed: {(err or 'unknown error').strip()[:200]}"
    return True, f"XFS project {pid} limited to {limit}"


def apply_btrfs_quota(volume_path: str, size_gb: float) -> Tuple[bool, str]:
    limit = f"{max(1, int(round(float(size_gb))))}G"
    code, _out, err = _run(["btrfs", "qgroup", "limit", limit, "--", volume_path])
    if code != 0:
        return False, f"btrfs qgroup limit failed: {(err or 'unknown error').strip()[:200]}"
    return True, f"Btrfs qgroup limited to {limit}"


def apply_quota(volume_path: str, size_gb: float, method: str, project_id: int) -> Tuple[bool, str]:
    """Dispatch to the right enforcement mechanism."""
    if method == "xfs-project-quota":
        return apply_xfs_quota(volume_path, size_gb, project_id)
    if method == "btrfs-qgroup":
        return apply_btrfs_quota(volume_path, size_gb)
    return False, f"Quota method '{method}' cannot be enforced on this node."


def release_quota(volume_path: str, method: str, project_id: int) -> Tuple[bool, str]:
    """Remove a quota when a VPS is deleted."""
    if method == "xfs-project-quota" and volume_path:
        return _run(["xfs_quota", "-x", "-c", f"limit -p bhard=0 bsoft=0 {int(project_id)}",
                     volume_path])[0] == 0, "xfs quota released"
    if method == "btrfs-qgroup" and volume_path:
        return _run(["btrfs", "qgroup", "limit", "none", "--", volume_path])[0] == 0, "qgroup released"
    return False, "nothing to release"


def quota_usage(volume_path: str, method: str, project_id: int) -> Dict[str, Any]:
    """Report how much of the quota a volume currently uses (host side)."""
    if method == "xfs-project-quota" and volume_path:
        code, out, _err = _run(["xfs_quota", "-x", "-c", f"quota -p -N -h {int(project_id)}",
                                volume_path])
        if code == 0 and out:
            return {"raw": out.strip()}
    return {}


def node_disk_usage(path: str) -> Dict[str, Any]:
    """Free/used space of a directory on the bot host."""
    if not path:
        return {}
    try:
        total, used, free = shutil.disk_usage(path)
        return {"total": int(total), "used": int(used), "free": int(free),
                "percent": round(used / total * 100, 1) if total else 0.0}
    except OSError:
        return {}


def humanize(num_bytes: float) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"
