"""
Node management service: add / edit / remove / test Docker nodes, publish
their reported capacity, and assign VPS to a specific node.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..docker_backend import monitoring, nodes as nodes_mod, resources
from ..docker_backend.client import POOL, parse_ssh_target, reset_pool, validate_remote_endpoint
from ..errors import NodeDisabled, NodeNotFound, TgdBotError

logger = logging.getLogger("tgdbot.services.nodes")


def normalise(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and canonicalise a node payload before it reaches the database."""
    transport = str(payload.get("transport") or ("local" if payload.get("is_local") else "tls")).lower()
    docker_host = str(payload.get("docker_host") or "").strip()

    if transport == "local":
        docker_host = docker_host or config.DOCKER_HOST_URL
        payload["is_local"] = True
    else:
        payload["is_local"] = False
        ok, reason = validate_remote_endpoint(transport, docker_host)
        if not ok:
            raise TgdBotError(reason, user_message=reason)

    payload["transport"] = transport
    payload["docker_host"] = docker_host
    payload["docker_timeout"] = int(payload.get("docker_timeout") or config.DOCKER_API_TIMEOUT)
    payload["enabled"] = bool(payload.get("enabled", True))
    payload["docker_tls_verify"] = bool(payload.get("docker_tls_verify", True))
    return payload


def add(repo: Any, payload: Dict[str, Any]) -> int:
    node_id = repo.upsert_node(normalise(payload))
    reset_pool(node_id)
    logger.info("node %s saved (id %s, transport %s)", payload.get("name"), node_id,
                payload.get("transport"))
    return int(node_id)


def edit(repo: Any, node_id: int, payload: Dict[str, Any]) -> bool:
    current = repo.get_node(int(node_id))
    if not current:
        raise NodeNotFound(f"Node {node_id} does not exist")
    merged = {**current, **payload, "id": int(node_id)}
    merged = normalise(merged)
    ok = repo.update_node(int(node_id), **{key: value for key, value in merged.items()
                                           if key != "id"})
    reset_pool(int(node_id))
    return ok


def remove(repo: Any, node_id: int, *, delete_vps: bool = False) -> bool:
    ok = repo.delete_node(int(node_id), delete_vps=delete_vps)
    reset_pool(int(node_id))
    return ok


def set_enabled(repo: Any, node_id: int, enabled: bool) -> bool:
    return repo.update_node(int(node_id), enabled=enabled)


def test_connection(repo: Any, node_id: int) -> Dict[str, Any]:
    """Ping the node and refresh everything the panel displays."""
    node = repo.get_node(int(node_id))
    if not node:
        raise NodeNotFound(f"Node {node_id} does not exist")
    report = nodes_mod.status(node)
    repo.update_node(int(node_id), last_check=datetime.now(timezone.utc).isoformat())
    if report.get("online"):
        info = report.get("info") or {}
        disk = report.get("disk") or {}
        repo.update_node(
            int(node_id),
            docker_version=info.get("server_version"),
            os_info=info.get("os"),
            cpu_info=f"{info.get('cpus')} vCPU",
            ram_total_mb=int((info.get("ram_total") or 0) // (1024 * 1024)),
            storage_total_gb=round(float(disk.get("total") or 0) / 1024 ** 3, 2),
            storage_free_gb=round(float(disk.get("free") or 0) / 1024 ** 3, 2),
            containers_running=int((report.get("managed") or {}).get("running") or 0),
            containers_stopped=int((report.get("managed") or {}).get("stopped") or 0),
            quota_supported=bool(report.get("storage_quota_size_supported")),
            quota_method=str(info.get("storage_driver") or ""),
        )
    return report


def refresh_all(repo: Any) -> List[Dict[str, Any]]:
    reports = []
    for node in repo.list_nodes():
        try:
            reports.append(test_connection(repo, int(node["id"])))
        except Exception as exc:
            reports.append({"node": node.get("name"), "online": False, "error": str(exc)})
    return reports


def capacity(repo: Any, node_id: int) -> Dict[str, Any]:
    node = repo.get_node(int(node_id))
    if not node:
        raise NodeNotFound(f"Node {node_id} does not exist")
    return nodes_mod.capacity(node, repo)


def set_capacity(repo: Any, node_id: int, *, max_vps: Optional[int] = None,
                 max_cpu: Optional[int] = None, max_ram_gb: Optional[int] = None) -> bool:
    fields: Dict[str, Any] = {}
    if max_vps is not None:
        fields["total_vps"] = int(max_vps)
    if max_cpu is not None:
        fields["max_cpu"] = int(max_cpu)
    if max_ram_gb is not None:
        fields["max_ram_gb"] = int(max_ram_gb)
    return bool(fields) and repo.update_node(int(node_id), **fields)


def assign(repo: Any, container_name: str, node_id: int) -> bool:
    """Point a VPS record at a different node (used by ``!lxc-assign``)."""
    node = repo.get_node(int(node_id))
    if not node:
        raise NodeNotFound(f"Node {node_id} does not exist")
    return repo.patch_vps(container_name, node_id=int(node_id))


def describe(node: Dict[str, Any], repo: Any) -> Dict[str, Any]:
    """Everything the Discord node panel shows about one node."""
    base = nodes_mod.capacity(node, repo)
    base.update({
        "transport": str(node.get("transport") or "local"),
        "transport_label": nodes_mod.describe_transport(str(node.get("transport") or "local")),
        "endpoint": str(node.get("docker_host") or config.DOCKER_HOST_URL),
        "enabled": bool(node.get("enabled", True)),
        "docker_version": node.get("docker_version"),
        "os_info": node.get("os_info"),
        "cpu_info": node.get("cpu_info"),
        "ram_total_mb": node.get("ram_total_mb"),
        "storage_total_gb": node.get("storage_total_gb"),
        "storage_free_gb": node.get("storage_free_gb"),
        "containers_running": node.get("containers_running"),
        "containers_stopped": node.get("containers_stopped"),
        "quota_method": node.get("quota_method"),
        "last_check": node.get("last_check"),
        "tags": node.get("tags") or [],
        "location": node.get("location"),
    })
    return base


def quota_support(repo: Any, node_id: int) -> Dict[str, Any]:
    """Report the real disk-quota capability of a node."""
    node = repo.get_node(int(node_id))
    if not node:
        raise NodeNotFound(f"Node {node_id} does not exist")
    from ..docker_backend import volumes as volumes_mod

    sample = None
    for vps in repo.list_vps():
        if int(vps.get("node_id") or 0) == int(node_id):
            try:
                client = nodes_mod.require_client(node)
                sample = volumes_mod.mountpoint(client, vps["container_name"])
            except Exception:
                sample = None
            break
    report = resources.detect_quota_support(sample or str(config.DOCKER_VPS_DATA_DIR),
                                            node_is_local=bool(node.get("is_local")))
    return report.as_dict()
