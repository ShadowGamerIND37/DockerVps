"""
Monitoring: per-container metrics from the Docker stats API, node level
metrics from ``docker info`` and host level metrics from ``/proc``.

Container numbers come from cgroup counters that Docker exposes — never from
host counters.  Mixing the two is how a panel ends up showing "the VPS is
using 87 % CPU" when it is actually the node.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from .. import config
from . import containers as ctr

logger = logging.getLogger("unixnodes.docker.monitoring")


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def cpu_percent(stats: Dict[str, Any], online_cpus: int = 0) -> float:
    """CPU percentage from two consecutive cgroup samples (Docker's own maths)."""
    cpu_stats = stats.get("cpu_stats") or {}
    pre_stats = stats.get("precpu_stats") or {}
    usage = _int((cpu_stats.get("cpu_usage") or {}).get("total_usage"))
    pre_usage = _int((pre_stats.get("cpu_usage") or {}).get("total_usage"))
    system = _int(cpu_stats.get("system_cpu_usage"))
    pre_system = _int(pre_stats.get("system_cpu_usage"))

    cpu_delta = usage - pre_usage
    system_delta = system - pre_system
    cores = online_cpus or _int(cpu_stats.get("online_cpus")) or len(
        (cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    if system_delta > 0 and cpu_delta >= 0:
        return max(0.0, min(100.0 * cores, (cpu_delta / system_delta) * cores * 100.0))
    return 0.0


def memory_usage(stats: Dict[str, Any]) -> Dict[str, int]:
    memory = stats.get("memory_stats") or {}
    used = _int(memory.get("usage"))
    limit = _int(memory.get("limit"))
    # Page cache is reclaimable; Docker subtracts inactive_file the same way
    # `docker stats` does, so the panel matches the CLI.
    cache = _int((memory.get("stats") or {}).get("inactive_file"))
    used = max(0, used - cache)
    percent = round(used / limit * 100.0, 1) if limit else 0.0
    return {"used": used, "total": limit, "pct": percent,
            "used_mb": used // (1024 * 1024), "total_mb": limit // (1024 * 1024)}


def network_usage(stats: Dict[str, Any]) -> Dict[str, int]:
    networks = (stats.get("networks") or {})
    rx = sum(_int((item or {}).get("rx_bytes")) for item in networks.values())
    tx = sum(_int((item or {}).get("tx_bytes")) for item in networks.values())
    return {"rx_bytes": rx, "tx_bytes": tx, "rx": rx, "tx": tx}


def io_usage(stats: Dict[str, Any]) -> Dict[str, int]:
    io_stats = ((stats.get("blkio_stats") or {}).get("io_service_bytes_recursive")) or []
    read_bytes = sum(_int(item.get("value")) for item in io_stats if item.get("op") == "read")
    write_bytes = sum(_int(item.get("value")) for item in io_stats if item.get("op") == "write")
    return {"read_bytes": read_bytes, "write_bytes": write_bytes}


def container_stats(client: Any, name: str, *, include_disk: bool = True) -> Dict[str, Any]:
    """
    Full metric snapshot for one VPS.

    Shape matches what the Discord panels already consume:
    ``{status, cpu, ram{used,total,pct}, disk, uptime, uptime_seconds, online}``
    plus Docker-specific extras (``net``, ``oom``, ``restart_count``).
    """
    empty = {
        "status": "unknown", "cpu": 0.0,
        "ram": {"used": 0, "total": 0, "pct": 0.0, "used_mb": 0, "total_mb": 0},
        "disk": "Unknown", "uptime": "Unknown", "uptime_seconds": 0.0,
        "online": False, "net": {"rx_bytes": 0, "tx_bytes": 0},
        "oom": False, "restart_count": 0, "ip": None,
    }
    container = ctr.find_container(client, name)
    if container is None:
        return {**empty, "status": "missing"}

    try:
        container.reload()
    except Exception as exc:
        logger.debug("reload failed for %s: %s", name, exc)

    state_block = container.attrs.get("State") or {}
    status = str(state_block.get("Status") or "unknown")
    result = {
        **empty,
        "status": status,
        "online": True,
        "oom": bool(state_block.get("OOMKilled")),
        "restart_count": _int(container.attrs.get("RestartCount")),
        "uptime_seconds": ctr.uptime_seconds(client, name),
    }
    result["uptime"] = format_duration(result["uptime_seconds"]) if result["uptime_seconds"] else "Unknown"

    from .networking import container_ip

    result["ip"] = container_ip(container)

    if status != "running":
        return result

    try:
        stats = container.stats(stream=False)
    except Exception as exc:
        logger.debug("stats failed for %s: %s", name, exc)
        return result

    result["cpu"] = round(cpu_percent(stats), 1)
    memory = memory_usage(stats)
    result["ram"] = memory
    result["net"] = network_usage(stats)
    result["io"] = io_usage(stats)

    if include_disk:
        result["disk"] = disk_usage_text(client, name)
    return result


def disk_usage_text(client: Any, name: str) -> str:
    """``df`` inside the guest — reports the persistent volume, not the image."""
    try:
        code, out, _err = ctr.exec_argv(
            client, name,
            ["sh", "-c", "df -Pk / 2>/dev/null | awk 'NR==2{print $3\" \"$2\" \"$5}'"],
            timeout=25,
        )
        parts = (out or "").split()
        if len(parts) >= 3:
            used_kb, total_kb, pct = parts[0], parts[1], parts[2]
            return f"{human_bytes(_int(used_kb) * 1024)} / {human_bytes(_int(total_kb) * 1024)} ({pct})"
    except Exception as exc:
        logger.debug("disk usage failed for %s: %s", name, exc)
    return "Unknown"


def disk_usage_bytes(client: Any, name: str) -> Dict[str, int]:
    try:
        _code, out, _err = ctr.exec_argv(
            client, name,
            ["sh", "-c", "df -Pk / 2>/dev/null | awk 'NR==2{print $3\" \"$2}'"],
            timeout=25,
        )
        parts = (out or "").split()
        if len(parts) >= 2:
            return {"used": _int(parts[0]) * 1024, "total": _int(parts[1]) * 1024}
    except Exception:
        pass
    return {"used": 0, "total": 0}


# ── node level ──────────────────────────────────────────────────────────────

def engine_info(client: Any) -> Dict[str, Any]:
    """``docker info`` summarised for the node panel."""
    info = dict(client.info() or {})
    driver_status = {
        str(item[0]): str(item[1])
        for item in (info.get("DriverStatus") or [])
        if isinstance(item, (list, tuple)) and len(item) >= 2
    }
    return {
        "server_version": info.get("ServerVersion"),
        "api_version": info.get("APIVersion"),
        "os": f"{info.get('OperatingSystem', '?')} ({info.get('Architecture', '?')})",
        "kernel": info.get("KernelVersion"),
        "cpus": _int(info.get("NCPU")),
        "ram_total": _int(info.get("MemTotal")),
        "containers": _int(info.get("Containers")),
        "containers_running": _int(info.get("ContainersRunning")),
        "containers_paused": _int(info.get("ContainersPaused")),
        "containers_stopped": _int(info.get("ContainersStopped")),
        "images": _int(info.get("Images")),
        "storage_driver": info.get("Driver"),
        "storage_status": driver_status,
        "docker_root_dir": info.get("DockerRootDir"),
        "cgroup_driver": info.get("CgroupDriver"),
        "cgroup_version": info.get("CgroupVersion"),
        "security_options": info.get("SecurityOptions") or [],
        "runtimes": list((info.get("Runtimes") or {}).keys()),
        "logging_driver": info.get("LoggingDriver"),
        "experimental": bool(info.get("ExperimentalBuild")),
        "warnings": info.get("Warnings") or [],
    }


def storage_supports_size_option(info: Dict[str, Any]) -> bool:
    """True when the storage driver can enforce ``--storage-opt size``."""
    driver = str(info.get("storage_driver") or "").lower()
    status = info.get("storage_status") or {}
    if driver == "overlay2":
        backing = str(status.get("Backing Filesystem", "")).lower()
        return "pquota" in str(status.get("Supports d_type", "")) or backing == "xfs"
    return driver in ("btrfs", "zfs", "devicemapper")


def node_disk_usage(client: Any) -> Dict[str, Any]:
    """Free space of the Docker data directory (local node only)."""
    try:
        info = dict(client.info() or {})
    except Exception:
        return {}
    root = str(info.get("DockerRootDir") or "/var/lib/docker")
    try:
        total, used, free = shutil.disk_usage(root)
    except OSError:
        return {}
    return {"path": root, "total": int(total), "used": int(used), "free": int(free),
            "percent": round(used / total * 100.0, 1) if total else 0.0}


def engine_healthy(client: Any) -> Dict[str, Any]:
    """Cheap readiness probe used by the node panel and the health check."""
    started = time.time()
    try:
        client.ping()
        version = client.version()
        return {"online": True, "latency_ms": round((time.time() - started) * 1000, 1),
                "server_version": version.get("Version"),
                "api_version": version.get("ApiVersion")}
    except Exception as exc:
        return {"online": False, "latency_ms": round((time.time() - started) * 1000, 1),
                "error": str(exc)}


def engine_status(client: Any) -> Dict[str, Any]:
    """Full node status: engine + capacity + managed container counts."""
    health = engine_healthy(client)
    if not health.get("online"):
        return {"online": False, **health}
    info = engine_info(client)
    counts = ctr.count_by_state(client)
    disk = node_disk_usage(client)
    return {
        "online": True,
        **health,
        "info": info,
        "managed": counts,
        "disk": disk,
        "storage_quota_size_supported": storage_supports_size_option(info),
    }


# ── host level (the machine running the bot) ────────────────────────────────

_CPU_STATE: Dict[str, Any] = {"idle": None, "total": None}


def _cpu_times() -> Optional[List[int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            return [int(x) for x in handle.readline().split()[1:]]
    except OSError:
        return None


def host_cpu_percent() -> float:
    sample = _cpu_times()
    if not sample:
        try:
            import psutil  # type: ignore

            return float(psutil.cpu_percent(interval=0.2))
        except Exception:
            return 0.0
    idle = sample[3] + (sample[4] if len(sample) > 4 else 0)
    total = sum(sample)
    previous_idle, previous_total = _CPU_STATE["idle"], _CPU_STATE["total"]
    _CPU_STATE["idle"], _CPU_STATE["total"] = idle, total
    if previous_idle is None:
        time.sleep(0.2)
        second = _cpu_times()
        if not second:
            return 0.0
        idle2 = second[3] + (second[4] if len(second) > 4 else 0)
        total2 = sum(second)
        _CPU_STATE["idle"], _CPU_STATE["total"] = idle2, total2
        delta_total, delta_idle = total2 - total, idle2 - idle
    else:
        delta_total, delta_idle = total - previous_total, idle - previous_idle
    if delta_total <= 0:
        return 0.0
    return max(0.0, min(100.0, (delta_total - delta_idle) / delta_total * 100.0))


def host_ram_percent() -> float:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            info: Dict[str, float] = {}
            for line in handle:
                key, _, value = line.partition(":")
                info[key.strip()] = float(value.strip().split()[0])
        total = info.get("MemTotal", 0.0)
        available = info.get("MemAvailable", info.get("MemFree", 0.0))
        if total:
            return max(0.0, min(100.0, (total - available) / total * 100.0))
    except (OSError, ValueError):
        pass
    try:
        import psutil  # type: ignore

        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0


def host_disk(path: str = "/") -> Dict[str, Any]:
    try:
        total, used, free = shutil.disk_usage(path)
        return {"total": int(total), "used": int(used), "free": int(free),
                "percent": round(used / total * 100.0, 1) if total else 0.0,
                "text": f"{human_bytes(used)} / {human_bytes(total)} ({used / total * 100:.0f}%)"}
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0, "text": "Unknown"}


def host_uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.readline().split()[0])
    except OSError:
        return 0.0


def host_load() -> List[float]:
    try:
        return list(os.getloadavg())
    except (OSError, AttributeError):
        return [0.0, 0.0, 0.0]


def host_snapshot(client: Any) -> Dict[str, Any]:
    """Everything the animated platform dashboard shows about the host."""
    disk = host_disk("/")
    docker_disk = node_disk_usage(client) if client is not None else {}
    return {
        "cpu": round(host_cpu_percent(), 1),
        "ram": round(host_ram_percent(), 1),
        "disk_pct": float(disk.get("percent") or 0.0),
        "disk": disk.get("text"),
        "docker_disk": docker_disk,
        "uptime": host_uptime_seconds(),
        "load": host_load(),
        "online": True,
    }


# ── formatting ──────────────────────────────────────────────────────────────

def human_bytes(num_bytes: float) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def format_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "—"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
