"""
Backup service: manual + scheduled backups, restore, retention.

The service wraps :mod:`unixnodes.docker_backend.backups` with the database
bookkeeping and the node awareness (local filesystem access vs. helper
container on a remote node).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..docker_backend import backups as backups_mod
from ..docker_backend import nodes as nodes_mod
from ..errors import BackupError
from . import lifecycle

logger = logging.getLogger("unixnodes.services.backups")

Progress = Callable[[str], None]


def _client(repo: Any, vps: Dict[str, Any]) -> Any:
    node = repo.get_node(int(vps["node_id"]))
    if not node:
        raise BackupError(f"Node {vps['node_id']} does not exist")
    return nodes_mod.require_client(node), node


def create(repo: Any, vps: Dict[str, Any], *, note: str = "", mode: str = "pause",
           scheduled: bool = False, progress: Optional[Progress] = None) -> Dict[str, Any]:
    """Create a backup and record it."""
    container = vps["container_name"]
    if str(vps.get("backend") or "docker") != "docker":
        raise BackupError(
            f"`{container}` is a legacy {vps.get('backend')} record, not a Docker VPS",
            user_message="That VPS is not managed by the Docker backend.")
    client, node = _client(repo, vps)
    from ..docker_backend.ports import PortAllocator

    allocator = PortAllocator(repo, int(node["id"]),
                              str(node.get("publish_ip") or config.DOCKER_PUBLISH_IP))
    metadata = backups_mod.create_backup(
        client, container, node_is_local=bool(node.get("is_local")),
        allocator=allocator, note=note, mode=mode, progress=progress)
    metadata["node_id"] = int(node["id"])
    metadata["scheduled"] = scheduled
    repo.add_backup(metadata)
    return metadata


def restore(repo: Any, vps: Dict[str, Any], backup_name: str, *,
            progress: Optional[Progress] = None) -> Dict[str, Any]:
    record = repo.get_backup(backup_name)
    if not record:
        raise BackupError(f"No backup named {backup_name}")
    if str(record["container_name"]) != str(vps["container_name"]):
        raise BackupError("That backup belongs to a different VPS")
    client, node = _client(repo, vps)
    archive = str(record.get("relative_archive")
                  or (record.get("archive") or "").split("/")[-1])
    result = backups_mod.restore_backup(
        client, vps["container_name"], archive=archive,
        node_is_local=bool(node.get("is_local")), progress=progress)
    result["backup"] = backup_name
    return result


def list_backups(repo: Any, container_name: Optional[str] = None) -> List[Dict[str, Any]]:
    return repo.list_backups(container_name)


def delete(repo: Any, backup_name: str) -> bool:
    record = repo.get_backup(backup_name)
    if not record:
        return False
    vps = repo.get_vps_by_container(str(record["container_name"]))
    node = repo.get_node(int(record.get("node_id") or 1))
    node_is_local = bool(node and node.get("is_local"))
    if node is not None:
        try:
            client = nodes_mod.require_client(node)
            backups_mod.delete_backup(client, None, backup_name, node_is_local=node_is_local)
        except Exception as exc:
            logger.warning("could not delete backup file %s: %s", backup_name, exc)
    repo.delete_backup_row(backup_name)
    return True


def verify(repo: Any, backup_name: str) -> Dict[str, Any]:
    record = repo.get_backup(backup_name)
    if not record:
        raise BackupError(f"No backup named {backup_name}")
    node = repo.get_node(int(record.get("node_id") or 1))
    if not node:
        raise BackupError("The node that holds this backup no longer exists")
    client = nodes_mod.require_client(node)
    ok, note = backups_mod.verify_archive(client, str(config.DOCKER_BACKUP_DIR), backup_name)
    repo.add_backup({**record, "verified": ok, "verify_note": note})
    return {"name": backup_name, "verified": ok, "note": note}


def apply_retention(repo: Any, *, node_id: Optional[int] = None) -> Dict[str, Any]:
    """Enforce keep-N + age retention for every node (or one)."""
    removed: List[str] = []
    errors: List[str] = []
    nodes = repo.list_enabled_nodes()
    for node in nodes:
        if node_id and int(node["id"]) != int(node_id):
            continue
        try:
            client = nodes_mod.require_client(node)
            names = backups_mod.prune(client, node_is_local=bool(node.get("is_local")))
        except Exception as exc:
            errors.append(f"{node.get('name')}: {exc}")
            continue
        for name in names:
            repo.delete_backup_row(name)
            removed.append(name)
    return {"removed": removed, "errors": errors}


def scheduled_pass(repo: Any) -> Dict[str, Any]:
    """Create a backup for every running, non-suspended VPS (retention applied)."""
    created: List[str] = []
    errors: List[str] = []
    for vps in repo.list_vps():
        if str(vps.get("backend") or "docker") != "docker":
            continue
        if vps.get("suspended") or vps.get("status") != "running":
            continue
        try:
            metadata = create(repo, vps, note="scheduled", scheduled=True)
            created.append(str(metadata["name"]))
        except Exception as exc:
            errors.append(f"{vps['container_name']}: {exc}")
    retention = apply_retention(repo)
    return {"created": created, "errors": errors, **retention}


def disk_usage() -> Dict[str, Any]:
    return backups_mod.disk_usage()
