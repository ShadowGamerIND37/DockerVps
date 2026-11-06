"""
VPS lifecycle: power actions, deletion, reinstall/rebuild, resource changes,
port changes, suspension, cloning and cross-node migration.

The important rule for every operation here: **the persistent volume is never
deleted unless the caller explicitly asks for it.**  Docker has to recreate a
container to change published ports, resource limits or the security policy,
and that recreation is data-preserving by construction — the new container
mounts the same named volume and uses a committed writable-root snapshot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config
from ..docker_backend import containers as ctr
from ..docker_backend import images as images_mod
from ..docker_backend import monitoring, nodes as nodes_mod, proxmox, resources, security, ssh, volumes
from ..docker_backend.ports import PURPOSE_USER, PortAllocator
from ..errors import ContainerError, ContainerNotFound, TgdBotError
from ..database import models
from .provisioning import generate_password, verification_host

logger = logging.getLogger("tgdbot.services.lifecycle")

Progress = Callable[[int, str, str], None]


def _client_and_allocator(repo: Any, vps: Dict[str, Any]) -> Tuple[Any, PortAllocator]:
    node = repo.get_node(int(vps["node_id"]))
    if not node:
        raise ContainerError(f"Node {vps['node_id']} for {vps['container_name']} does not exist")
    client = nodes_mod.require_client(node)
    allocator = PortAllocator(repo, int(node["id"]),
                              str(node.get("publish_ip") or config.DOCKER_PUBLISH_IP))
    return client, allocator


def _node(repo: Any, vps: Dict[str, Any]) -> Dict[str, Any]:
    node = repo.get_node(int(vps["node_id"]))
    if not node:
        raise ContainerError(f"Node {vps['node_id']} does not exist")
    return node


def _ram_mb(vps: Dict[str, Any]) -> int:
    raw = str(vps.get("ram") or "").upper().strip()
    try:
        if raw.endswith("GB"):
            return int(float(raw[:-2]) * 1024)
        if raw.endswith("MB"):
            return int(float(raw[:-2]))
        return int(float(raw) * 1024)
    except ValueError:
        return int(config.DOCKER_DEFAULT_RAM)


def _cpu(vps: Dict[str, Any]) -> float:
    try:
        return float(str(vps.get("cpu") or config.DOCKER_DEFAULT_CPU))
    except ValueError:
        return float(config.DOCKER_DEFAULT_CPU)


def _storage_gb(vps: Dict[str, Any]) -> int:
    raw = str(vps.get("storage") or "").upper().strip()
    try:
        return int(float(raw.replace("GB", "")))
    except ValueError:
        return int(config.DOCKER_DEFAULT_STORAGE)


# ── state ───────────────────────────────────────────────────────────────────

def state(repo: Any, vps: Dict[str, Any]) -> str:
    client, _ = _client_and_allocator(repo, vps)
    return ctr.simple_state(client, vps["container_name"])


def stats(repo: Any, vps: Dict[str, Any], *, include_disk: bool = True) -> Dict[str, Any]:
    client, _ = _client_and_allocator(repo, vps)
    report = monitoring.container_stats(client, vps["container_name"], include_disk=include_disk)
    if report.get("status") not in ("unknown", "missing"):
        repo.patch_vps(vps["container_name"], status=str(report["status"]),
                       restart_count=int(report.get("restart_count") or 0),
                       last_health_ts=datetime.now(timezone.utc).isoformat())
        try:
            repo.save_usage_snapshot(
                vps["container_name"], float(report.get("cpu") or 0),
                float((report.get("ram") or {}).get("pct") or 0),
                int((report.get("ram") or {}).get("used_mb") or 0),
                int((report.get("ram") or {}).get("total_mb") or 0))
        except Exception as exc:
            logger.debug("usage snapshot failed: %s", exc)
    return report


def logs(repo: Any, vps: Dict[str, Any], tail: int = 60) -> str:
    client, _ = _client_and_allocator(repo, vps)
    return ctr.logs(client, vps["container_name"], tail=tail)


def exec_script(repo: Any, vps: Dict[str, Any], script: str, *,
                timeout: Optional[int] = None) -> str:
    client, _ = _client_and_allocator(repo, vps)
    return ctr.exec_script(client, vps["container_name"], script,
                           timeout=timeout or config.DOCKER_EXEC_TIMEOUT)


def exec_command(repo: Any, vps: Dict[str, Any], command: str, *,
                 timeout: Optional[int] = None) -> Tuple[int, str, str]:
    client, _ = _client_and_allocator(repo, vps)
    return ctr.exec_argv(client, vps["container_name"], ["sh", "-c", command],
                         timeout=timeout or config.DOCKER_EXEC_TIMEOUT)


# ── power ───────────────────────────────────────────────────────────────────

def start(repo: Any, vps: Dict[str, Any]) -> str:
    client, _ = _client_and_allocator(repo, vps)
    name = vps["container_name"]
    current = ctr.simple_state(client, name)
    if current == "missing":
        raise ContainerNotFound(f"Container {name} does not exist on its node")
    if current != "running":
        ctr.start(client, name)
    repo.patch_vps(name, status="running", suspended=False)
    return "running"


def stop(repo: Any, vps: Dict[str, Any], *, timeout: Optional[int] = None) -> str:
    client, _ = _client_and_allocator(repo, vps)
    ctr.stop(client, vps["container_name"], timeout=timeout)
    repo.patch_vps(vps["container_name"], status="stopped")
    return "stopped"


def restart(repo: Any, vps: Dict[str, Any]) -> str:
    client, _ = _client_and_allocator(repo, vps)
    ctr.restart(client, vps["container_name"])
    repo.patch_vps(vps["container_name"], status="running")
    return "running"


def suspend(repo: Any, vps: Dict[str, Any], *, reason: str, actor: str) -> Dict[str, Any]:
    client, _ = _client_and_allocator(repo, vps)
    ctr.stop(client, vps["container_name"], timeout=120, force=True)
    history = list(vps.get("suspension_history") or [])
    history.append({"time": datetime.now(timezone.utc).isoformat(),
                    "reason": reason, "by": actor})
    repo.patch_vps(vps["container_name"], suspended=True, status="stopped",
                   suspension_history=history)
    return {"suspended": True, "status": "stopped"}


def unsuspend(repo: Any, vps: Dict[str, Any]) -> Dict[str, Any]:
    client, _ = _client_and_allocator(repo, vps)
    ctr.start(client, vps["container_name"])
    repo.patch_vps(vps["container_name"], suspended=False, status="running")
    return {"suspended": False, "status": "running"}


# ── deletion ────────────────────────────────────────────────────────────────

def delete(repo: Any, vps: Dict[str, Any], *, delete_volume: bool = True,
           delete_backups: bool = False, progress: Optional[Progress] = None) -> Dict[str, Any]:
    """
    Delete a VPS.

    Order matters: the container goes first (so nothing is writing), then the
    port ledger, then the volume, then the database row.  Every step is
    attempted even if an earlier one fails, and the result reports what
    happened so nothing is silently left behind.
    """
    container = vps["container_name"]
    is_proxmox = proxmox.is_image(vps.get("docker_image") or vps.get("os_version"))
    result = {"container": container, "steps": [], "errors": []}

    def step(index: int, message: str) -> None:
        result["steps"].append(message)
        if progress:
            try:
                progress(index, "done", message)
            except Exception:
                pass

    node = repo.get_node(int(vps["node_id"]))
    client = nodes_mod.require_client(node) if node else None

    if client is not None:
        try:
            ctr.stop(client, container, timeout=90, force=True)
            step(0, "container stopped")
        except Exception as exc:
            result["errors"].append(f"stop: {exc}")
        try:
            ctr.remove(client, container, volumes=False, force=True)
            step(1, "container removed")
        except Exception as exc:
            result["errors"].append(f"remove: {exc}")
        # The database name is authoritative, including legacy unixnodes_ volumes.
        # Keep the recovery image if container removal failed.
        if not ctr.exists(client, container):
            ctr.remove_snapshot(client, container)
        if delete_volume:
            if volumes.remove(client, container, force=True, stored=vps.get("volume_name")):
                step(2, "volume removed")
            else:
                result["errors"].append("volume could not be removed")
            if is_proxmox:
                try:
                    if proxmox.remove_cluster_volume(client, container):
                        step(2, "Proxmox cluster volume removed")
                except Exception as exc:
                    result["errors"].append(f"Proxmox cluster volume: {exc}")
            quota = repo.get_quota(container)
            if quota:
                mountpoint = ""
                resources.release_quota(mountpoint, str(quota.get("method") or ""),
                                        int(quota.get("project_id") or 0))
                repo.delete_quota(container)
                step(2, "quota released")
    else:
        result["errors"].append("node unreachable — only database records were removed")

    try:
        repo.delete_vps_row(container)
        step(3, "database row removed (ports released)")
    except Exception as exc:
        result["errors"].append(f"database: {exc}")

    if not is_proxmox:
        ssh.clear_credential(container)

    if delete_backups:
        for backup in repo.list_backups(container):
            try:
                from ..docker_backend import backups as backups_mod

                backups_mod.delete_backup(client, None, str(backup["name"]),
                                          node_is_local=bool(node and node.get("is_local")))
                repo.delete_backup_row(str(backup["name"]))
            except Exception as exc:
                result["errors"].append(f"backup {backup.get('name')}: {exc}")
        step(4, "backups deleted")

    logger.info("deleted VPS %s: %s", container, result["steps"])
    return result


# ── recreation (the data-preserving core) ───────────────────────────────────

def recreate(repo: Any, vps: Dict[str, Any], *, image: Optional[str] = None,
             ram_mb: Optional[int] = None, cpu: Optional[float] = None,
             pids: Optional[int] = None, wipe_volume: bool = False,
             new_password: Optional[str] = None, target_node_id: Optional[int] = None,
             rootfs_snapshot: Optional[str] = None,
             progress: Optional[Progress] = None) -> Dict[str, Any]:
    """
    Rebuild a container.  The persistent volume survives unless
    ``wipe_volume=True`` (reinstall).
    """
    container = vps["container_name"]
    node = _node(repo, vps)
    if target_node_id and int(target_node_id) != int(node["id"]):
        raise ContainerError("Use migrate() to move a VPS to another node")
    if rootfs_snapshot and wipe_volume:
        raise ContainerError("A rootfs snapshot cannot be used with a volume wipe")

    client, allocator = _client_and_allocator(repo, vps)
    image_ref = image or str(vps.get("docker_image") or vps.get("os_version")
                             or config.DOCKER_DEFAULT_IMAGE)
    if image is not None:
        # Reinstall is another provisioning boundary, not a way to smuggle an
        # unapproved image past /image-add or the Proxmox grant check.
        image_ref = images_mod.resolve(str(image), repo.list_approved_images())["value"]
    proxmox.assert_supported_reference(image_ref)
    current_is_proxmox = proxmox.is_image(vps.get("docker_image") or vps.get("os_version"))
    requested_is_proxmox = proxmox.is_image(image_ref)
    if current_is_proxmox != requested_is_proxmox:
        raise ContainerError(
            "Changing between a regular VPS and Proxmox requires a new VPS; "
            "their privilege and storage contracts are incompatible.",
            user_message="Create a new VPS to change between a regular OS and Proxmox.",
        )
    workload = config.WORKLOAD_PROXMOX if requested_is_proxmox else config.WORKLOAD_VPS
    kvm_enabled, apparmor_enabled = security.security_from_record(vps, workload)
    if requested_is_proxmox and not kvm_enabled:
        raise ContainerError(
            "The supported Proxmox runtime requires KVM; refusing to recreate "
            "a KVM-disabled privileged container.")
    if not kvm_enabled and config.DOCKER_PRIVILEGED:
        raise ContainerError(
            "KVM-disabled VPS recreation is incompatible with privileged Docker mode.")
    # Validate an enabled profile before stopping or removing the old guest.
    security.apparmor_security_opt(apparmor_enabled)
    ram = int(ram_mb or _ram_mb(vps))
    cores = float(cpu if cpu is not None else _cpu(vps))
    limit_pids = int(pids or vps.get("pid_limit") or config.DOCKER_DEFAULT_PID_LIMIT)
    if requested_is_proxmox:
        proxmox.validate_resources(ram, _storage_gb(vps))
        limit_pids = proxmox.effective_pids(limit_pids)
    was_running = ctr.simple_state(client, container) == "running"
    # The volume name recorded in the database wins over a recomputed one, so a
    # VPS created before the rebrand keeps its existing data volume.
    stored_volume = volumes.resolve_volume_name(client, container, vps.get("volume_name"))
    volume_labels = {
        config.LABEL_MANAGED: "true", config.LABEL_CONTAINER: container,
        config.LABEL_OWNER: str(vps.get("user_id")),
        config.LABEL_VPS_ID: str(vps.get("id")), config.LABEL_NODE: str(node["id"]),
    }
    if requested_is_proxmox and not wipe_volume:
        proxmox.assert_volumes_exist(client, container, data_volume=stored_volume)
    if (not wipe_volume and vps.get("volume_name")
            and not volumes.exists(client, container, stored=stored_volume)):
        raise ContainerError(
            f"Recorded persistent volume {stored_volume} is unavailable; "
            "refusing to recreate the VPS with a different volume")

    def step(index: int, message: str) -> None:
        if progress:
            try:
                progress(index, "running", message)
            except Exception:
                pass

    step(0, "stopping container")
    ctr.stop(client, container, timeout=120, force=True)

    # Docker cannot mount a volume at '/', and recreating from the base image
    # would discard packages and configuration in the writable layer. Commit
    # that layer while the old container is stopped, then create from the
    # private snapshot. This needs no privileged mode or CAP_SYS_ADMIN.
    snapshot = None
    if rootfs_snapshot:
        # A restore has already loaded this image; do not overwrite it by
        # committing the container that is about to be replaced.
        snapshot = {"reference": str(rootfs_snapshot), "id": None, "previous_id": None}
    elif not wipe_volume:
        step(1, "preserving the writable root filesystem")
        snapshot = ctr.commit_snapshot(client, container)

    if wipe_volume:
        step(1, "wiping the persistent volume")
        volumes.remove(client, container, force=True, stored=stored_volume)
        if requested_is_proxmox:
            proxmox.remove_cluster_volume(client, container)
        volumes.create(client, container, labels=volume_labels)
        if requested_is_proxmox:
            client.volumes.create(name=proxmox.cluster_volume_name(container),
                                  driver="local", labels=dict(volume_labels))
        repo.delete_quota(container)
        # The volume was just recreated under the current prefix, so the name
        # resolved before the wipe is now stale.
        stored_volume = volumes.resolve_volume_name(client, container, None)
    else:
        if not volumes.exists(client, container, stored=stored_volume):
            volumes.create(client, container, labels=volume_labels)
        step(2, "Proxmox data and cluster volumes kept" if requested_is_proxmox
             else "persistent volume kept")

    step(3, "removing the old container")
    ctr.remove(client, container, volumes=False, force=True)

    step(4, "creating the new container")
    password = new_password
    create_image = str(snapshot.get("reference")) if snapshot else image_ref
    container_labels = {config.LABEL_VPS_ID: str(vps.get("id")),
                        config.LABEL_OWNER: str(vps.get("user_id")),
                        config.LABEL_NODE: str(node["id"]), config.LABEL_IMAGE: image_ref}
    if requested_is_proxmox:
        spec = proxmox.build_container_spec(
            container_name=container, image_ref=create_image,
            password_hash_value=(proxmox.password_hash(password)
                                 if wipe_volume and password else None),
            ram_mb=ram, cpu=cores, pids=limit_pids, publish_ip=allocator.publish_ip,
            published_ports=allocator.publish_spec(container), labels=container_labels,
            kvm_enabled=kvm_enabled, apparmor_enabled=apparmor_enabled)
    else:
        if wipe_volume and password:
            ssh.write_credential(container, password)
        binds = volumes.mounts_for(container, extra_binds=ssh.bind_mounts(container),
                                   stored=stored_volume)
        spec = ctr.build_container_spec(
            container_name=container, image=create_image, ram_mb=ram, cpu=cores, pids=limit_pids,
            publish_ip=allocator.publish_ip, published_ports=allocator.publish_spec(container),
            volumes=binds, labels=container_labels,
            command=ssh.container_command(), hostname=container[:63],
            kvm_enabled=kvm_enabled, apparmor_enabled=apparmor_enabled)
    container_obj = ctr.create(client, spec)

    step(5, "starting container")
    ctr.start(client, container)
    ctr.wait_running(client, container, timeout=90)

    # Remove only superseded private snapshots, after the replacement is
    # running. Never remove the recorded base image or a snapshot still in use.
    if snapshot and snapshot.get("previous_id") and snapshot.get("previous_id") != snapshot.get("id"):
        ctr.remove_snapshot(client, container, image_id=str(snapshot["previous_id"]))
    if wipe_volume:
        ctr.remove_snapshot(client, container)

    problems = allocator.verify_bindings(container_obj, container)
    if problems:
        logger.warning("port bindings missing after recreate of %s: %s", container, problems)

    updates: Dict[str, Any] = {
        "docker_image": image_ref, "os_version": image_ref,
        "docker_container_id": str(container_obj.short_id),
        "ram": f"{ram}MB", "cpu": f"{cores:g}",
        "config": f"{ram}MB RAM / {cores:g} vCPU / {_storage_gb(vps)}GB Disk",
        "pid_limit": limit_pids,
        "kvm_enabled": kvm_enabled,
        "apparmor_enabled": apparmor_enabled,
        "volume_name": stored_volume,
        "status": "running" if was_running or wipe_volume else "stopped",
    }
    if password:
        updates["root_password"] = None  # caller stores the encrypted value
    repo.patch_vps(container, **updates)

    return {"container": container, "was_running": was_running,
            "running": ctr.simple_state(client, container) == "running",
            "port_problems": problems, "volume_kept": not wipe_volume}


def resize(repo: Any, vps: Dict[str, Any], *, ram_mb: Optional[int] = None,
           cpu: Optional[float] = None, storage_gb: Optional[int] = None,
           pids: Optional[int] = None, progress: Optional[Progress] = None) -> Dict[str, Any]:
    """
    Change CPU / RAM / PID / storage limits.

    Docker cannot change published ports or all limits on a live container, so
    the container is recreated — with the same volume, so no customer data is
    touched.  The storage quota is re-applied on the host afterwards.
    """
    container = vps["container_name"]
    node = _node(repo, vps)
    client, _allocator = _client_and_allocator(repo, vps)
    if proxmox.is_image(vps.get("docker_image") or vps.get("os_version")) and storage_gb is not None:
        proxmox.validate_resources(int(ram_mb or _ram_mb(vps)), int(storage_gb))

    result = recreate(repo, vps, ram_mb=ram_mb, cpu=cpu, pids=pids, progress=progress)

    new_storage = int(storage_gb or _storage_gb(vps))
    quota_report = {"enforced": bool(vps.get("quota_enforced")),
                    "method": str(vps.get("quota_method") or ""), "detail": ""}
    if storage_gb:
        mountpoint = volumes.mountpoint(client, container, stored=vps.get("volume_name")) or ""
        support = resources.detect_quota_support(mountpoint, node_is_local=bool(node.get("is_local")))
        project_id = resources.suggested_project_id(
            volumes.resolve_volume_name(client, container, vps.get("volume_name")))
        enforced = False
        detail = support.detail
        if support.supported:
            enforced, detail = resources.apply_quota(mountpoint, new_storage, support.method,
                                                     project_id)
        quota_note = ("Quota covers /var/lib/vz; the cluster-configuration volume is separate."
                      if proxmox.is_image(vps.get("docker_image") or vps.get("os_version"))
                      else resources.DATA_VOLUME_QUOTA_NOTE)
        detail = f"{detail} {quota_note}".strip()
        repo.set_quota(container, node_id=int(node["id"]), method=support.method,
                       project_id=project_id if support.method == "xfs-project-quota" else None,
                       size_gb=new_storage, enforced=enforced, detail=detail)
        repo.patch_vps(container, storage=f"{new_storage}GB", quota_method=support.method,
                       quota_enforced=enforced)
        quota_report = {"enforced": enforced, "method": support.method, "detail": detail}

    result["quota"] = quota_report
    result["storage_gb"] = new_storage
    return result


def reinstall(repo: Any, vps: Dict[str, Any], *, image: str,
              new_password: Optional[str] = None,
              progress: Optional[Progress] = None) -> Dict[str, Any]:
    """Destroy the volume and install a fresh operating system."""
    password = new_password or generate_password()
    result = recreate(repo, vps, image=image, wipe_volume=True, new_password=password,
                      progress=progress)
    result["password"] = password
    return result


def rebuild(repo: Any, vps: Dict[str, Any], *, progress: Optional[Progress] = None) -> Dict[str, Any]:
    """Recreate the container from the same image and volume (data preserved)."""
    return recreate(repo, vps, progress=progress)


# ── passwords ───────────────────────────────────────────────────────────────

def change_password(repo: Any, vps: Dict[str, Any], password: Optional[str] = None,
                    *, restart_ssh: bool = False) -> Dict[str, Any]:
    client, _ = _client_and_allocator(repo, vps)
    new_password = password or generate_password()
    running = ctr.simple_state(client, vps["container_name"]) == "running"
    is_proxmox = proxmox.is_image(vps.get("docker_image") or vps.get("os_version"))
    if running:
        ok, note = ssh.set_root_password(client, vps["container_name"], new_password)
    elif is_proxmox:
        # There is no tgdbot init script to consume a staged credential on the
        # next boot. Never report a password as changed when it was only staged.
        ok, note = False, "Start the Proxmox VPS before changing its root password"
    else:
        # Stopped container: stage the credential for the next boot.
        ssh.write_credential(vps["container_name"], new_password)
        ok, note = True, "Credential staged — it is applied on the next start"
    return {"ok": ok, "note": note, "password": new_password}


# ── ports ───────────────────────────────────────────────────────────────────

def add_port(repo: Any, vps: Dict[str, Any], container_port: int, protocol: str = "both",
             host_port: Optional[int] = None) -> Dict[str, Any]:
    client, allocator = _client_and_allocator(repo, vps)
    reservations = allocator.reserve(vps["container_name"], int(container_port),
                                     purpose=PURPOSE_USER, protocol=protocol,
                                     host_port=host_port, user_id=vps.get("user_id"))
    problems = republish(repo, vps)
    return {"reservations": [item.as_dict() for item in reservations],
            "host_port": reservations[0].host_port if reservations else None,
            "port_problems": problems}


def remove_port(repo: Any, vps: Dict[str, Any], reservation_id: int) -> Dict[str, Any]:
    client, allocator = _client_and_allocator(repo, vps)
    released = allocator.release_by_id(int(reservation_id))
    problems = republish(repo, vps) if released else []
    return {"released": released, "port_problems": problems}


def republish(repo: Any, vps: Dict[str, Any]) -> List[str]:
    """
    Re-apply the current port ledger to the container.

    Docker fixes published ports at creation time, so a change means a
    container recreate — with the volume intact.  If the container is stopped
    the change is simply recorded and applied on the next start.
    """
    client, allocator = _client_and_allocator(repo, vps)
    container = vps["container_name"]
    if not ctr.exists(client, container):
        return []
    if ctr.simple_state(client, container) != "running":
        return []
    result = recreate(repo, vps)
    allocator.mark_bound(container)
    return list(result.get("port_problems") or [])


def port_summary(repo: Any, vps: Dict[str, Any]) -> Dict[str, Any]:
    _client, allocator = _client_and_allocator(repo, vps)
    return allocator.summary(vps["container_name"])


def port_history(repo: Any, vps: Dict[str, Any], limit: int = 40) -> List[Dict[str, Any]]:
    _client, allocator = _client_and_allocator(repo, vps)
    return allocator.history(vps["container_name"], limit=limit)


# ── clone / migrate ─────────────────────────────────────────────────────────

def clone(repo: Any, vps: Dict[str, Any], new_name: str, *, owner_id: Any,
          progress: Optional[Progress] = None) -> Dict[str, Any]:
    """
    Duplicate a VPS: copy the volume, then create a second container.

    Docker has no ``lxc copy``; the volume is duplicated with a short-lived
    helper container and the new container gets its own ports and identity.
    """
    from ..docker_backend.ports import PURPOSE_SSH

    container = vps["container_name"]
    if proxmox.is_image(vps.get("docker_image") or vps.get("os_version")):
        raise ContainerError(
            "Proxmox cloning is disabled because both /var/lib/vz and "
            "/var/lib/pve-cluster must be copied atomically.",
            user_message="Proxmox cloning is not supported; create a new Proxmox VPS instead.")
    node = _node(repo, vps)
    client, allocator = _client_and_allocator(repo, vps)
    if repo.get_vps_by_container(new_name):
        raise ContainerError(f"A VPS named {new_name} already exists")

    def step(index: int, message: str) -> None:
        if progress:
            try:
                progress(index, "running", message)
            except Exception:
                pass

    step(0, "preserving the writable root filesystem")
    snapshot = ctr.commit_snapshot(client, container)

    step(1, "copying the persistent volume")
    volumes.rename(client, container, new_name, stored=vps.get("volume_name"))
    clone_snapshot = (ctr.tag_snapshot(client, container, new_name)
                      if snapshot else None)

    step(2, "reserving an SSH port")
    allocator.reserve(new_name, config.SSH_CONTAINER_PORT, purpose=PURPOSE_SSH,
                      protocol="tcp", user_id=owner_id)
    ssh_port = allocator.ssh_port(new_name) or 0

    step(3, "creating the clone container")
    password = generate_password()
    ssh.write_credential(new_name, password)
    binds = volumes.mounts_for(new_name, extra_binds=ssh.bind_mounts(new_name))
    image_ref = str(vps.get("docker_image") or vps.get("os_version") or config.DOCKER_DEFAULT_IMAGE)
    create_image = clone_snapshot or image_ref
    kvm_enabled, apparmor_enabled = security.security_from_record(vps, config.WORKLOAD_VPS)
    spec = ctr.build_container_spec(
        container_name=new_name, image=create_image, ram_mb=_ram_mb(vps), cpu=_cpu(vps),
        pids=int(vps.get("pid_limit") or config.DOCKER_DEFAULT_PID_LIMIT),
        publish_ip=allocator.publish_ip, published_ports=allocator.publish_spec(new_name),
        volumes=binds,
        labels={config.LABEL_OWNER: str(owner_id), config.LABEL_NODE: str(node["id"]),
                config.LABEL_IMAGE: image_ref},
        command=ssh.container_command(), hostname=new_name[:63],
        kvm_enabled=kvm_enabled, apparmor_enabled=apparmor_enabled)
    container_obj = ctr.create(client, spec)
    ctr.start(client, new_name)

    step(4, "registering the clone")
    new_id = repo.next_vps_id()
    repo.insert_vps({
        "id": new_id, "user_id": str(owner_id), "node_id": int(node["id"]),
        "container_name": new_name, "ram": vps.get("ram"), "cpu": vps.get("cpu"),
        "storage": vps.get("storage"), "config": vps.get("config"), "os_version": image_ref,
        "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
        "backend": models.DOCKER_BACKEND, "docker_container_id": str(container_obj.short_id),
        "docker_image": image_ref, "volume_name": volumes.volume_name(new_name),
        "ssh_host_port": ssh_port, "pid_limit": vps.get("pid_limit"),
        "kvm_enabled": kvm_enabled, "apparmor_enabled": apparmor_enabled,
        "quota_method": None, "quota_enforced": False, "provision_state": "active",
    })
    return {"container": new_name, "id": new_id, "ssh_host_port": ssh_port,
            "password": password}


def migrate(repo: Any, vps: Dict[str, Any], target_node_id: int,
            progress: Optional[Progress] = None) -> Dict[str, Any]:
    """
    Move a VPS to another Docker node.

    Docker cannot copy a container between engines, so the volume and writable
    rootfs are streamed through the backup machinery: archive on the source
    node, copy both archives across, restore on the target, then delete the
    source container.
    """
    from ..docker_backend import backups as backups_mod

    container = vps["container_name"]
    if proxmox.is_image(vps.get("docker_image") or vps.get("os_version")):
        raise ContainerError(
            "Proxmox migration is disabled until both persistent volumes can be transferred.",
            user_message="Proxmox migration between nodes is not supported.")
    source_node = _node(repo, vps)
    target_node = repo.get_node(int(target_node_id))
    if not target_node:
        raise ContainerError(f"Target node {target_node_id} does not exist")
    if int(target_node["id"]) == int(source_node["id"]):
        raise ContainerError("The VPS already lives on that node")

    source_client = nodes_mod.require_client(source_node)
    target_client = nodes_mod.require_client(target_node)

    def step(index: int, message: str) -> None:
        if progress:
            try:
                progress(index, "running", message)
            except Exception:
                pass

    step(0, "backing up the source volume and writable root filesystem")
    backup = backups_mod.create_backup(source_client, container,
                                       node_is_local=bool(source_node.get("is_local")),
                                       volume_name=vps.get("volume_name"), progress=None)

    if not backup.get("relative_rootfs_archive"):
        raise ContainerError("The source backup did not include its required rootfs archive")

    step(1, "transferring the archives")
    archive_bytes = _read_archive(source_client, backup,
                                  node_is_local=bool(source_node.get("is_local")))
    _write_archive(target_client, archive_bytes, backup["relative_archive"])
    if backup.get("relative_rootfs_archive"):
        rootfs_bytes = _read_archive(source_client, backup,
                                     node_is_local=bool(source_node.get("is_local")),
                                     artifact="rootfs")
        _write_archive(target_client, rootfs_bytes, backup["relative_rootfs_archive"])

    step(2, "creating the volume on the target node")
    ssh.ensure_runtime_assets()
    volumes.create(target_client, container, labels={
        config.LABEL_MANAGED: "true", config.LABEL_CONTAINER: container,
        config.LABEL_NODE: str(target_node["id"])})
    restored = backups_mod.restore_backup(
        target_client, container, archive=backup["relative_archive"],
        node_is_local=bool(target_node.get("is_local")), wipe=False,
        volume_name=volumes.volume_name(container),
        rootfs_required=True)

    step(3, "creating the container on the target node")
    allocator = PortAllocator(repo, int(target_node["id"]),
                              str(target_node.get("publish_ip") or config.DOCKER_PUBLISH_IP))
    for row in allocator.active(container):
        allocator.release_by_id(int(row["id"]))
    for row in repo.active_ports_for_container(container):
        allocator.reserve(container, int(row["container_port"]), protocol=str(row["protocol"]),
                          purpose=str(row["purpose"] or "user"), host_port=None,
                          user_id=vps.get("user_id"))
    password = generate_password()
    ssh.write_credential(container, password)
    binds = volumes.mounts_for(container, extra_binds=ssh.bind_mounts(container))
    image_ref = str(vps.get("docker_image") or vps.get("os_version") or config.DOCKER_DEFAULT_IMAGE)
    create_image = str(restored.get("rootfs_snapshot") or image_ref)
    if not restored.get("rootfs_snapshot"):
        images_mod.ensure_image(target_client, image_ref)
    kvm_enabled, apparmor_enabled = security.security_from_record(vps, config.WORKLOAD_VPS)
    spec = ctr.build_container_spec(
        container_name=container, image=create_image, ram_mb=_ram_mb(vps), cpu=_cpu(vps),
        pids=int(vps.get("pid_limit") or config.DOCKER_DEFAULT_PID_LIMIT),
        publish_ip=allocator.publish_ip, published_ports=allocator.publish_spec(container),
        volumes=binds, labels={config.LABEL_NODE: str(target_node["id"]),
                               config.LABEL_IMAGE: image_ref,
                               config.LABEL_OWNER: str(vps.get("user_id")),
                               config.LABEL_VPS_ID: str(vps.get("id"))},
        command=ssh.container_command(), hostname=container[:63],
        kvm_enabled=kvm_enabled, apparmor_enabled=apparmor_enabled)
    ctr.create(target_client, spec)
    ctr.start(target_client, container)

    step(4, "removing the source container")
    ctr.stop(source_client, container, timeout=90, force=True)
    ctr.remove(source_client, container, volumes=False, force=True)
    if not ctr.exists(source_client, container):
        ctr.remove_snapshot(source_client, container)
    # Remove whatever the source really had, including a pre-rebrand volume.
    volumes.remove(source_client, container, force=True,
                   stored=vps.get("volume_name"))

    step(5, "updating the database")
    # The target volume was just created under the current prefix, so the
    # recorded name must follow it or the row points at a deleted volume.
    repo.patch_vps(container, node_id=int(target_node["id"]), status="running",
                   volume_name=volumes.resolve_volume_name(target_client, container, None))
    return {"container": container, "node_id": int(target_node["id"]), "password": password}


def _read_archive(client: Any, backup: Dict[str, Any], *, node_is_local: bool,
                  artifact: str = "volume") -> bytes:
    from pathlib import Path

    rootfs = artifact == "rootfs"
    path_key = "rootfs_archive" if rootfs else "archive"
    relative_key = "relative_rootfs_archive" if rootfs else "relative_archive"
    if node_is_local:
        return Path(str(backup[path_key])).read_bytes()
    import base64

    code, out, _err = _helper_output(
        client, f"base64 /backup/{backup[relative_key]}")
    if code != 0:
        raise ContainerError("Could not read the backup archive from the source node")
    return base64.b64decode("".join(out.split()))


def _write_archive(client: Any, data: bytes, filename: str) -> None:
    import base64
    import tempfile
    from pathlib import Path

    encoded = base64.b64encode(data).decode("ascii")
    staging = Path(config.DOCKER_RUNTIME_DIR) / f"transfer-{filename}.b64"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(encoded, encoding="utf-8")
    try:
        code, out, _err = _helper_output(
            client, f"base64 -d /staging/{staging.name} > /backup/{filename} && echo WROTE",
            extra_volumes={str(staging.parent): {"bind": "/staging", "mode": "ro"}})
        if code != 0 or "WROTE" not in out:
            raise ContainerError("Could not write the archive on the target node")
    finally:
        try:
            staging.unlink()
        except OSError:
            pass


def _helper_output(client: Any, command: str,
                   extra_volumes: Optional[Dict[str, Dict[str, str]]] = None) -> Tuple[int, str, str]:
    from ..docker_backend.backups import run_helper

    volumes_map = {str(config.DOCKER_BACKUP_DIR): {"bind": "/backup", "mode": "rw"}}
    volumes_map.update(extra_volumes or {})
    return run_helper(client, ["sh", "-c", command], volumes=volumes_map, name_hint="transfer")


# ── reconciliation ──────────────────────────────────────────────────────────

def reconcile(repo: Any, vps: Dict[str, Any]) -> str:
    """Refresh the stored status from the real container state."""
    current = state(repo, vps)
    repo.patch_vps(vps["container_name"], status=current)
    return current


def orphans(repo: Any, node_id: Optional[int] = None) -> Dict[int, List[Dict[str, Any]]]:
    """Containers that exist on a node but not in the database."""
    tracked = set(repo.container_names())
    found: Dict[int, List[Dict[str, Any]]] = {}
    for node in repo.list_enabled_nodes():
        if node_id and int(node["id"]) != int(node_id):
            continue
        try:
            items = nodes_mod.orphaned_containers(node, list(tracked))
        except Exception as exc:
            logger.warning("orphan scan failed on node %s: %s", node.get("name"), exc)
            continue
        if items:
            found[int(node["id"])] = items
    return found


def remove_orphan(repo: Any, node_id: int, container_name: str) -> bool:
    node = repo.get_node(int(node_id))
    if not node:
        return False
    client = nodes_mod.require_client(node)
    ctr.remove(client, container_name, volumes=True, force=True)
    if not ctr.exists(client, container_name):
        ctr.remove_snapshot(client, container_name)
    try:
        volumes.remove(client, container_name, force=True)
    except Exception:
        pass
    return True
