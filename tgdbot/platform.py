"""
Async facade over the Docker infrastructure.

``bot.py`` talks to *this* module and nothing else.  Every method is a thin
``await run_in_thread(...)`` wrapper around the synchronous services, so the
Discord event loop is never blocked by a Docker API call and every call can be
timeout-guarded with :func:`asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import config
from .concurrency import run_in_thread
from .database import migrations, models
from .database.repository import Repository
from .docker_backend import (
    backups as backups_mod,
    containers as ctr,
    images as images_mod,
    monitoring,
    nodes as nodes_mod,
    proxmox,
    resources,
    security,
    ssh,
    volumes,
)
from .docker_backend.client import POOL, reset_pool, validate_remote_endpoint
from .docker_backend.ports import PURPOSE_SSH, PURPOSE_USER, PortAllocator
from .errors import TgdBotError
from .services import backup_service, cloudflared, expiration, lifecycle, node_service, provisioning

logger = logging.getLogger("tgdbot.platform")


class Platform:
    """Everything the Discord layer needs, in async form."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None,
                 vault: Optional[Callable[[str], str]] = None) -> None:
        self.repo = Repository(db_path or config.DATABASE_PATH)
        self._vault = vault

    # ── credential vault ────────────────────────────────────────────────────
    #
    # The services generate root passwords but deliberately never see the
    # encryption key, so *something* has to persist them.  Doing it here — in
    # one place — means no caller can forget, and the plaintext never reaches
    # the database, `docker inspect` or a log line.
    def set_vault(self, encrypt: Optional[Callable[[str], str]]) -> None:
        """Install the caller's ``encrypt_secret`` (bot.py wires this at boot)."""
        self._vault = encrypt

    def _store_password(self, container_name: str, password: Optional[str]) -> bool:
        if not password:
            return False
        if self._vault is None:
            logger.warning(
                "no credential vault is installed — the root password for %s was NOT stored",
                container_name)
            return False
        try:
            return bool(self.repo.patch_vps(container_name,
                                            root_password=self._vault(str(password))))
        except Exception:  # pragma: no cover - never lose a deployed VPS over this
            logger.exception("could not store the root password for %s", container_name)
            return False

    # ── bootstrap ───────────────────────────────────────────────────────────
    async def migrate_database(self, *, backup_dir: Optional[Path] = None,
                               report_dir: Optional[Path] = None) -> migrations.MigrationReport:
        """Schema migration (see ``migrate()`` below for VPS node migration)."""
        return await run_in_thread(
            migrations.migrate, Path(config.DATABASE_PATH),
            backup_dir=backup_dir, report_dir=report_dir)

    async def inspect_database(self) -> Dict[str, Any]:
        return await run_in_thread(migrations.inspect_database, Path(config.DATABASE_PATH))

    async def prepare_runtime(self) -> Dict[str, str]:
        return await run_in_thread(ssh.ensure_runtime_assets)

    async def engine_health(self, node_id: Optional[int] = None) -> Dict[str, Any]:
        node = await run_in_thread(self.repo.get_node, int(node_id or 1)) or await run_in_thread(
            self.repo.get_local_node)
        if not node:
            return {"online": False, "error": "no node configured"}
        return await run_in_thread(nodes_mod.health, node)

    async def local_client(self) -> Any:
        node = await run_in_thread(self.repo.get_local_node)
        if not node:
            raise TgdBotError("No local node is configured in the database")
        return nodes_mod.client_for(node)

    # ── images ──────────────────────────────────────────────────────────────
    async def image_options(self, *, user_id: Optional[Any] = None,
                            is_admin: bool = False) -> List[Dict[str, Any]]:
        custom = await run_in_thread(self.repo.list_approved_images)
        items = images_mod.catalogue(custom)
        if is_admin:
            return items
        granted = bool(user_id is not None and await run_in_thread(
            self.repo.has_proxmox_grant, user_id))
        return [item for item in items
                if item.get("kind") != config.WORKLOAD_PROXMOX or granted]

    async def has_proxmox_access(self, user_id: Any, *, is_admin: bool = False) -> bool:
        return bool(is_admin or await run_in_thread(self.repo.has_proxmox_grant, user_id))

    async def grant_proxmox(self, user_id: Any, *, granted_by: Optional[Any] = None) -> bool:
        return await run_in_thread(self.repo.grant_proxmox, user_id, granted_by=granted_by)

    async def revoke_proxmox(self, user_id: Any) -> bool:
        return await run_in_thread(self.repo.revoke_proxmox, user_id)

    async def list_proxmox_grants(self) -> List[Dict[str, Any]]:
        return await run_in_thread(self.repo.list_proxmox_grants)

    async def add_image(self, reference: str, *, label: str = "", emoji: str = "🐳",
                        description: str = "", added_by: Optional[Any] = None) -> Dict[str, Any]:
        reference = reference.strip()
        ok, reason = security.validate_image_reference(reference)
        if not ok:
            raise TgdBotError(reason, user_message=reason)
        proxmox.assert_supported_reference(reference)
        client = await self.local_client()
        # Pull and inspect before approval. A database row alone neither makes
        # an image deployable nor proves it is a supported Linux rootfs.
        attrs = await run_in_thread(images_mod.ensure_image, client, reference)
        images_mod.validate_image_platform(attrs, reference)
        await run_in_thread(self.repo.add_image, reference, label=label, emoji=emoji,
                            description=description, added_by=added_by)
        custom = await run_in_thread(self.repo.list_approved_images)
        return images_mod.resolve(reference, custom)

    async def remove_image(self, reference: str) -> bool:
        return await run_in_thread(self.repo.remove_image, reference)

    async def list_local_images(self, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        client = await self._node_client(node_id)
        return await run_in_thread(images_mod.list_local, client)

    # ── provisioning ────────────────────────────────────────────────────────
    async def provision(self, request: provisioning.ProvisionRequest, *,
                        progress: Optional[Callable[[int, str, str], None]] = None,
                        ) -> provisioning.ProvisionResult:
        result = await run_in_thread(provisioning.provision, self.repo, request,
                                     progress=progress)
        await run_in_thread(self._store_password, result.container_name, result.password)
        return result

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def _vps(self, container_name: str) -> Dict[str, Any]:
        vps = await run_in_thread(self.repo.get_vps_by_container, container_name)
        if not vps:
            raise TgdBotError(f"No VPS named {container_name}",
                                 user_message=f"`{container_name}` is not a registered VPS.")
        return vps

    async def state(self, container_name: str) -> str:
        vps = await self._vps(container_name)
        if str(vps.get("backend") or "docker") != "docker":
            return "legacy"
        return await run_in_thread(lifecycle.state, self.repo, vps)

    async def stats(self, container_name: str, *, include_disk: bool = True) -> Dict[str, Any]:
        vps = await self._vps(container_name)
        if str(vps.get("backend") or "docker") != "docker":
            return {"status": "legacy", "online": False, "cpu": 0.0,
                    "ram": {"used": 0, "total": 0, "pct": 0.0}, "disk": "Legacy record",
                    "uptime": "—", "uptime_seconds": 0.0, "net": {"rx_bytes": 0, "tx_bytes": 0}}
        return await run_in_thread(lifecycle.stats, self.repo, vps, include_disk=include_disk)

    async def logs(self, container_name: str, tail: int = 60) -> str:
        vps = await self._vps(container_name)
        return await run_in_thread(lifecycle.logs, self.repo, vps, tail)

    async def exec(self, container_name: str, command: str) -> Tuple[int, str, str]:
        vps = await self._vps(container_name)
        return await run_in_thread(lifecycle.exec_command, self.repo, vps, command)

    async def cloudflared(self, container_name: str, *,
                          progress: Optional[Callable[[int, str, str], None]] = None,
                          ) -> Dict[str, str]:
        """Set up a quick tunnel inside a freshly reloaded Proxmox VPS record."""
        vps = await self._vps(container_name)
        return await run_in_thread(cloudflared.setup_quick_tunnel, self.repo, vps,
                                   progress=progress)

    async def start(self, container_name: str) -> str:
        return await run_in_thread(lifecycle.start, self.repo, await self._vps(container_name))

    async def stop(self, container_name: str) -> str:
        return await run_in_thread(lifecycle.stop, self.repo, await self._vps(container_name))

    async def restart(self, container_name: str) -> str:
        return await run_in_thread(lifecycle.restart, self.repo, await self._vps(container_name))

    async def delete(self, container_name: str, *, delete_volume: bool = True,
                     delete_backups: bool = False,
                     progress: Optional[Callable[[int, str, str], None]] = None) -> Dict[str, Any]:
        return await run_in_thread(lifecycle.delete, self.repo, await self._vps(container_name),
                                   delete_volume=delete_volume, delete_backups=delete_backups,
                                   progress=progress)

    async def reinstall(self, container_name: str, image: str, *,
                        progress: Optional[Callable[[int, str, str], None]] = None) -> Dict[str, Any]:
        outcome = await run_in_thread(lifecycle.reinstall, self.repo,
                                      await self._vps(container_name),
                                      image=image, progress=progress)
        await run_in_thread(self._store_password, container_name, outcome.get("password"))
        return outcome

    async def rebuild(self, container_name: str, *,
                      progress: Optional[Callable[[int, str, str], None]] = None) -> Dict[str, Any]:
        outcome = await run_in_thread(lifecycle.rebuild, self.repo,
                                      await self._vps(container_name), progress=progress)
        await run_in_thread(self._store_password, container_name, outcome.get("password"))
        return outcome

    async def resize(self, container_name: str, *, ram_mb: Optional[int] = None,
                     cpu: Optional[float] = None, storage_gb: Optional[int] = None,
                     pids: Optional[int] = None,
                     progress: Optional[Callable[[int, str, str], None]] = None) -> Dict[str, Any]:
        return await run_in_thread(lifecycle.resize, self.repo, await self._vps(container_name),
                                   ram_mb=ram_mb, cpu=cpu, storage_gb=storage_gb, pids=pids,
                                   progress=progress)

    async def suspend(self, container_name: str, *, reason: str, actor: str) -> Dict[str, Any]:
        return await run_in_thread(lifecycle.suspend, self.repo, await self._vps(container_name),
                                   reason=reason, actor=actor)

    async def unsuspend(self, container_name: str) -> Dict[str, Any]:
        return await run_in_thread(lifecycle.unsuspend, self.repo, await self._vps(container_name))

    async def change_password(self, container_name: str,
                              password: Optional[str] = None) -> Dict[str, Any]:
        outcome = await run_in_thread(lifecycle.change_password, self.repo,
                                      await self._vps(container_name), password)
        if outcome.get("ok"):
            await run_in_thread(self._store_password, container_name, outcome.get("password"))
        return outcome

    async def clone(self, container_name: str, new_name: str, *, owner_id: Any,
                    progress: Optional[Callable[[int, str, str], None]] = None) -> Dict[str, Any]:
        outcome = await run_in_thread(lifecycle.clone, self.repo,
                                      await self._vps(container_name), new_name,
                                      owner_id=owner_id, progress=progress)
        await run_in_thread(self._store_password, outcome.get("container") or new_name,
                            outcome.get("password"))
        return outcome

    async def migrate(self, container_name: str, target_node_id: int, *,
                      progress: Optional[Callable[[int, str, str], None]] = None) -> Dict[str, Any]:
        outcome = await run_in_thread(lifecycle.migrate, self.repo,
                                      await self._vps(container_name),
                                      int(target_node_id), progress=progress)
        await run_in_thread(self._store_password, container_name, outcome.get("password"))
        return outcome

    async def reconcile(self, container_name: str) -> str:
        return await run_in_thread(lifecycle.reconcile, self.repo, await self._vps(container_name))

    # ── ports ───────────────────────────────────────────────────────────────
    async def ports(self, container_name: str) -> Dict[str, Any]:
        return await run_in_thread(lifecycle.port_summary, self.repo, await self._vps(container_name))

    async def add_port(self, container_name: str, container_port: int,
                       protocol: str = "both") -> Dict[str, Any]:
        return await run_in_thread(lifecycle.add_port, self.repo, await self._vps(container_name),
                                   int(container_port), protocol)

    async def remove_port(self, container_name: str, reservation_id: int) -> Dict[str, Any]:
        return await run_in_thread(lifecycle.remove_port, self.repo, await self._vps(container_name),
                                   int(reservation_id))

    async def port_history(self, container_name: str, limit: int = 40) -> List[Dict[str, Any]]:
        return await run_in_thread(lifecycle.port_history, self.repo, await self._vps(container_name),
                                   limit)

    async def republish_ports(self, container_name: str) -> List[str]:
        return await run_in_thread(lifecycle.republish, self.repo, await self._vps(container_name))

    def active_ports(self, container_name: str) -> List[Dict[str, Any]]:
        """Synchronous read used by the web terminal token issuer."""
        return self.repo.active_ports_for_container(container_name)

    def ssh_host_port(self, container_name: str) -> Optional[int]:
        for row in self.repo.active_ports_for_container(container_name):
            if int(row["container_port"]) == config.SSH_CONTAINER_PORT:
                return int(row["host_port"])
        vps = self.repo.get_vps_by_container(container_name)
        if vps and vps.get("ssh_host_port"):
            return int(vps["ssh_host_port"])
        return None

    # ── backups ─────────────────────────────────────────────────────────────
    async def backup(self, container_name: str, *, note: str = "", mode: str = "pause",
                     progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        return await run_in_thread(backup_service.create, self.repo,
                                   await self._vps(container_name), note=note, mode=mode,
                                   progress=progress)

    async def restore(self, container_name: str, backup_name: str, *,
                      progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        return await run_in_thread(backup_service.restore, self.repo,
                                   await self._vps(container_name), backup_name, progress=progress)

    async def list_backups(self, container_name: Optional[str] = None) -> List[Dict[str, Any]]:
        return await run_in_thread(backup_service.list_backups, self.repo, container_name)

    async def delete_backup(self, backup_name: str) -> bool:
        return await run_in_thread(backup_service.delete, self.repo, backup_name)

    async def verify_backup(self, backup_name: str) -> Dict[str, Any]:
        return await run_in_thread(backup_service.verify, self.repo, backup_name)

    async def backup_retention(self, node_id: Optional[int] = None) -> Dict[str, Any]:
        return await run_in_thread(backup_service.apply_retention, self.repo, node_id=node_id)

    async def scheduled_backups(self) -> Dict[str, Any]:
        return await run_in_thread(backup_service.scheduled_pass, self.repo)

    # ── nodes ───────────────────────────────────────────────────────────────
    async def nodes(self) -> List[Dict[str, Any]]:
        return await run_in_thread(self.repo.list_nodes)

    async def node(self, node_id: int) -> Optional[Dict[str, Any]]:
        return await run_in_thread(self.repo.get_node, int(node_id))

    async def add_node(self, payload: Dict[str, Any]) -> int:
        return await run_in_thread(node_service.add, self.repo, payload)

    async def edit_node(self, node_id: int, payload: Dict[str, Any]) -> bool:
        return await run_in_thread(node_service.edit, self.repo, int(node_id), payload)

    async def remove_node(self, node_id: int, *, delete_vps: bool = False) -> bool:
        return await run_in_thread(node_service.remove, self.repo, int(node_id),
                                   delete_vps=delete_vps)

    async def node_enabled(self, node_id: int, enabled: bool) -> bool:
        return await run_in_thread(node_service.set_enabled, self.repo, int(node_id), enabled)

    async def test_node(self, node_id: int) -> Dict[str, Any]:
        return await run_in_thread(node_service.test_connection, self.repo, int(node_id))

    async def node_capacity(self, node_id: int) -> Dict[str, Any]:
        return await run_in_thread(node_service.capacity, self.repo, int(node_id))

    async def node_status(self, node_id: int) -> Dict[str, Any]:
        node = await self.node(int(node_id))
        if not node:
            return {"online": False, "error": "node not found"}
        return await run_in_thread(nodes_mod.status, node)

    async def node_containers(self, node_id: int) -> List[Dict[str, Any]]:
        node = await self.node(int(node_id))
        if not node:
            return []
        return await run_in_thread(nodes_mod.containers_on_node, node)

    async def node_quota_support(self, node_id: int) -> Dict[str, Any]:
        return await run_in_thread(node_service.quota_support, self.repo, int(node_id))

    async def set_node_capacity(self, node_id: int, **limits: Any) -> bool:
        return await run_in_thread(node_service.set_capacity, self.repo, int(node_id), **limits)

    async def orphans(self, node_id: Optional[int] = None) -> Dict[int, List[Dict[str, Any]]]:
        return await run_in_thread(lifecycle.orphans, self.repo, node_id)

    async def remove_orphan(self, node_id: int, container_name: str) -> bool:
        return await run_in_thread(lifecycle.remove_orphan, self.repo, int(node_id), container_name)

    # ── monitoring ──────────────────────────────────────────────────────────
    async def host_snapshot(self) -> Dict[str, Any]:
        try:
            client = await self.local_client()
        except Exception:
            client = None
        return await run_in_thread(monitoring.host_snapshot, client)

    async def security_policy(self) -> Dict[str, Any]:
        return security.describe_policy()

    # ── internal ────────────────────────────────────────────────────────────
    async def _node_client(self, node_id: Optional[int]) -> Any:
        node = await run_in_thread(self.repo.get_node, int(node_id or 1))
        if not node:
            node = await run_in_thread(self.repo.get_local_node)
        if not node:
            raise TgdBotError("No node is configured")
        return nodes_mod.client_for(node)

    async def close(self) -> None:
        await run_in_thread(POOL.close_all)


#: One platform instance per process, created by ``bot.py``.
platform = Platform()
