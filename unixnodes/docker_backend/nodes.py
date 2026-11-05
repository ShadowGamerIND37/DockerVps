"""
Docker node registry: connection, health, capacity and placement.

A *node* is one Docker Engine.  The existing UnixNodes ``nodes`` table is kept
(and extended — see :mod:`unixnodes.database.migrations`) so every existing
Discord node command keeps working; the LXC "remote agent" transport is
replaced by native Docker transports (``local``, ``ssh``, ``tls``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..errors import DockerUnavailable, NodeDisabled, NodeNotFound
from . import containers as ctr
from . import monitoring
from .client import Endpoint, POOL, validate_remote_endpoint

logger = logging.getLogger("unixnodes.docker.nodes")


# ── connection ──────────────────────────────────────────────────────────────

def endpoint_for(node: Dict[str, Any]) -> Endpoint:
    return Endpoint.from_node(node)


def client_for(node: Dict[str, Any]) -> Any:
    """Docker client for a node record (cached process-wide)."""
    return POOL.get(endpoint_for(node))


def forget(node_id: int) -> None:
    """Drop the cached client after a node edit."""
    POOL.drop(int(node_id))


def validate_node_config(transport: str, base_url: str) -> Tuple[bool, str]:
    """Validate a node's Docker connection settings before saving."""
    transport = str(transport or "local").lower()
    if transport == "local":
        return True, "ok"
    return validate_remote_endpoint(transport, base_url)


# ── health ──────────────────────────────────────────────────────────────────

def health(node: Dict[str, Any]) -> Dict[str, Any]:
    """Cheap ping + version probe.  Never raises."""
    name = str(node.get("name") or f"node {node.get('id')}")
    if not int(node.get("enabled", 1) or 0):
        return {"online": False, "state": "disabled", "label": "⚪ Disabled", "detail": "Node disabled"}
    try:
        client = client_for(node)
        report = monitoring.engine_healthy(client)
    except Exception as exc:
        report = {"online": False, "error": str(exc)}
    if report.get("online"):
        report["state"] = "online"
        report["label"] = "🟢 Online (local)" if node.get("is_local") else "🟢 Online"
    else:
        report["state"] = "unreachable"
        report["label"] = "🔴 Unreachable"
    report["node"] = name
    report["transport"] = str(node.get("transport") or ("local" if node.get("is_local") else "tls"))
    report["endpoint"] = endpoint_for(node).describe()
    return report


def status(node: Dict[str, Any]) -> Dict[str, Any]:
    """Deep node status: engine, capacity, containers, disk, security policy."""
    probe = health(node)
    if not probe.get("online"):
        return probe
    try:
        client = client_for(node)
        report = monitoring.engine_status(client)
    except Exception as exc:
        return {**probe, "online": False, "error": str(exc)}
    info = report.get("info") or {}
    report.update({
        "node": str(node.get("name")),
        "node_id": int(node.get("id") or 0),
        "location": str(node.get("location") or "Unknown"),
        "transport": probe.get("transport"),
        "endpoint": probe.get("endpoint"),
        "max_vps": int(node.get("total_vps") or 0),
        "max_cpu": int(node.get("max_cpu") or 0),
        "max_ram_gb": int(node.get("max_ram_gb") or 0),
        "tags": node.get("tags") or [],
        "docker_version": info.get("server_version"),
        "operating_system": info.get("os"),
        "kernel": info.get("kernel"),
        "cpu_count": info.get("cpus"),
        "ram_total": info.get("ram_total"),
        "storage_driver": info.get("storage_driver"),
        "security_options": info.get("security_options"),
    })
    return report


# ── capacity + placement ────────────────────────────────────────────────────

def capacity(node: Dict[str, Any], repo: Any) -> Dict[str, Any]:
    """Compare the configured limits against what the node reports."""
    max_vps = int(node.get("total_vps") or 0)
    max_cpu = float(node.get("max_cpu") or 0)
    max_ram_gb = float(node.get("max_ram_gb") or 0)
    used_vps = repo.count_vps_on_node(int(node.get("id") or 0))
    allocated = repo.allocated_resources_on_node(int(node.get("id") or 0))

    result: Dict[str, Any] = {
        "node_id": int(node.get("id") or 0),
        "name": str(node.get("name")),
        "vps_used": used_vps,
        "vps_max": max_vps,
        "vps_free": (max_vps - used_vps) if max_vps else None,
        "cpu_allocated": allocated["cpu"],
        "cpu_max": max_cpu,
        "ram_allocated_gb": allocated["ram_gb"],
        "ram_max_gb": max_ram_gb,
    }
    if max_vps:
        result["vps_percent"] = round(used_vps / max_vps * 100.0, 1)
    if max_cpu:
        result["cpu_percent"] = round(allocated["cpu"] / max_cpu * 100.0, 1)
    if max_ram_gb:
        result["ram_percent"] = round(allocated["ram_gb"] / max_ram_gb * 100.0, 1)
    return result


def has_room(node: Dict[str, Any], repo: Any, *, ram_gb: float, cpu: float) -> Tuple[bool, str]:
    """Placement check used before provisioning."""
    if not int(node.get("enabled", 1) or 0):
        return False, "Node is disabled"
    cap = capacity(node, repo)
    if cap["vps_max"] and cap["vps_used"] >= cap["vps_max"]:
        return False, f"Node is full ({cap['vps_used']}/{cap['vps_max']} VPS)"
    if cap["cpu_max"] and cap["cpu_allocated"] + cpu > cap["cpu_max"]:
        return False, (f"Not enough CPU on this node "
                       f"({cap['cpu_allocated']:g}/{cap['cpu_max']:g} allocated)")
    if cap["ram_max_gb"] and cap["ram_allocated_gb"] + ram_gb > cap["ram_max_gb"]:
        return False, (f"Not enough RAM on this node "
                       f"({cap['ram_allocated_gb']:g}/{cap['ram_max_gb']:g} GB allocated)")
    return True, "ok"


def pick_node(repo: Any, *, ram_gb: float, cpu: float, preferred_id: Optional[int] = None,
              require_online: bool = False) -> Optional[Dict[str, Any]]:
    """
    Choose the least-loaded node that can host a new VPS.

    Honours an explicit ``preferred_id`` when that node has room.
    """
    nodes = repo.list_enabled_nodes()
    if preferred_id:
        wanted = repo.get_node(int(preferred_id))
        if wanted and int(wanted.get("enabled", 1) or 0):
            ok, _reason = has_room(wanted, repo, ram_gb=ram_gb, cpu=cpu)
            if ok:
                return wanted
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for node in nodes:
        ok, _reason = has_room(node, repo, ram_gb=ram_gb, cpu=cpu)
        if not ok:
            continue
        cap = capacity(node, repo)
        score = float(cap.get("vps_percent") or 0)
        candidates.append((score, node))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (not bool(item[1].get("is_local")), item[0]))
    chosen = candidates[0][1]
    if require_online:
        probe = health(chosen)
        if not probe.get("online"):
            for _score, node in candidates[1:]:
                if health(node).get("online"):
                    return node
            return None
    return chosen


def containers_on_node(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every managed container on a node (the ``!lxc-list`` replacement)."""
    client = client_for(node)
    return ctr.list_managed(client)


def orphaned_containers(node: Dict[str, Any], tracked: List[str]) -> List[Dict[str, Any]]:
    """Containers present on the node but missing from the database."""
    known = {str(name) for name in tracked}
    return [item for item in containers_on_node(node)
            if item["container_name"] not in known and item["name"] not in known]


def require_client(node: Dict[str, Any]) -> Any:
    """Like :func:`client_for` but raises friendly errors."""
    if not node:
        raise NodeNotFound("That node does not exist.")
    if not int(node.get("enabled", 1) or 0):
        raise NodeDisabled(f"Node {node.get('name')} is disabled.")
    try:
        return client_for(node)
    except DockerUnavailable:
        raise
    except Exception as exc:
        raise DockerUnavailable(f"Could not reach Docker on {node.get('name')}: {exc}") from exc


def describe_transport(transport: str) -> str:
    return {
        "local": "Local Docker socket",
        "ssh": "SSH tunnel (no API port exposed)",
        "tls": "TCP with mutual TLS",
    }.get(str(transport or "local").lower(), str(transport))
