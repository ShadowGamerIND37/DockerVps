"""
Backup and restore.

Docker has no LXC-style atomic container snapshot.  What this module produces
is an explicit, verifiable, documented artefact:

* a gzip-compressed tar of the VPS **persistent volume** (the whole rootfs,
  including ``/home``, ``/etc`` and everything ``apt`` installed);
* a JSON side-car holding the container configuration, image reference,
  published ports, labels and resource limits, so a restore can rebuild an
  identical container;
* a SHA-256 digest and a ``tar -tzf`` verification pass.

The customer workload is *quiesced* first (``docker pause`` by default, or a
full stop when ``mode='stop'``) so the archive is as consistent as a running
Linux system can be.  It is **not** a crash-consistent block snapshot and the
bot says so wherever backups are described.

Archives are written to ``DOCKER_BACKUP_DIR`` on the node that owns the VPS —
never inside the customer container's own storage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..errors import BackupError
from . import containers as ctr

logger = logging.getLogger("unixnodes.docker.backups")

META_SUFFIX = ".json"
ARCHIVE_SUFFIX = ".tar.gz"


def archive_name(container_name: str, when: Optional[datetime] = None) -> str:
    moment = when or datetime.now(timezone.utc)
    return f"{container_name}-{moment.strftime('%Y%m%d-%H%M%S')}"


def archive_path(backup_dir: str, name: str) -> Path:
    return Path(backup_dir) / f"{name}{ARCHIVE_SUFFIX}"


def meta_path(backup_dir: str, name: str) -> Path:
    return Path(backup_dir) / f"{name}{META_SUFFIX}"


# ── helper container runner ─────────────────────────────────────────────────

def _ensure_helper_image(client: Any) -> None:
    image = config.DOCKER_BACKUP_HELPER_IMAGE
    try:
        client.images.get(image)
        return
    except Exception:
        pass
    try:
        client.images.pull(image)
    except Exception as exc:
        raise BackupError(
            f"Backup helper image '{image}' is unavailable",
            detail=str(exc),
            user_message="The node cannot prepare backups (helper image missing).") from exc


def run_helper(client: Any, command: List[str], *, volumes: Dict[str, Dict[str, str]],
               timeout: int = 1800, name_hint: str = "backup") -> Tuple[int, str, str]:
    """Run a short-lived, network-isolated helper container."""
    _ensure_helper_image(client)
    container = None
    try:
        container = client.containers.create(
            config.DOCKER_BACKUP_HELPER_IMAGE,
            command=command,
            volumes=volumes,
            network_disabled=True,
            detach=True,
            labels={config.LABEL_MANAGED: "true", config.LABEL_ROLE: config.ROLE_BACKUP_HELPER,
                    config.LABEL_CONTAINER: name_hint},
            host_config=None,
        )
        container.start()
        result = container.wait(timeout=timeout)
        logs = container.logs(tail=200).decode("utf-8", "replace")
        return int(result.get("StatusCode", 1)), logs, ""
    except Exception as exc:
        raise BackupError("Backup helper container failed", detail=str(exc)) from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass


# ── create ──────────────────────────────────────────────────────────────────

def configuration_snapshot(client: Any, container_name: str, allocator: Any = None) -> Dict[str, Any]:
    """Everything needed to rebuild an identical container."""
    attrs = ctr.inspect(client, container_name)
    config_block = attrs.get("Config") or {}
    host_block = attrs.get("HostConfig") or {}
    ports = allocator.publish_spec(container_name) if allocator is not None else []
    return {
        "image": config_block.get("Image"),
        "hostname": config_block.get("Hostname"),
        "labels": config_block.get("Labels") or {},
        "command": config_block.get("Cmd"),
        "limits": {
            "mem_limit": host_block.get("Memory"),
            "mem_reservation": host_block.get("MemoryReservation"),
            "cpu_quota": host_block.get("CpuQuota"),
            "cpu_period": host_block.get("CpuPeriod"),
            "cpu_shares": host_block.get("CpuShares"),
            "pids_limit": host_block.get("PidsLimit"),
            "cap_drop": host_block.get("CapDrop") or [],
            "security_opt": host_block.get("SecurityOpt") or [],
            "sysctls": host_block.get("Sysctls") or {},
            "privileged": host_block.get("Privileged"),
        },
        "ports": [list(item) for item in ports],
        "restart_policy": host_block.get("RestartPolicy"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "docker_version": None,
    }


def create_backup(client: Any, container_name: str, *, node_is_local: bool,
                  allocator: Any = None, note: str = "", mode: str = "pause",
                  progress: Any = None) -> Dict[str, Any]:
    """
    Create a backup.  Returns the metadata dict (also persisted by the caller).
    """
    if not ctr.exists(client, container_name):
        raise BackupError(f"Container {container_name} does not exist")

    volume = f"{config.DOCKER_VOLUME_PREFIX}{container_name}"
    if _volume_missing(client, volume):
        raise BackupError(f"Persistent volume {volume} does not exist")

    backup_dir = str(config.DOCKER_BACKUP_DIR)
    name = archive_name(container_name)
    relative_archive = f"{name}{ARCHIVE_SUFFIX}"
    created = datetime.now(timezone.utc)

    def step(message: str) -> None:
        logger.info("backup %s: %s", container_name, message)
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    state_before = ctr.simple_state(client, container_name)
    paused = False
    stopped = False

    step(f"quiescing workload (was {state_before}, mode {mode})")
    if state_before == "running":
        if mode == "stop":
            ctr.stop(client, container_name)
            stopped = True
        else:
            paused = ctr.pause(client, container_name)
            if not paused:
                ctr.stop(client, container_name)
                stopped = True

    try:
        step("writing volume archive")
        code, out, _err = run_helper(
            client,
            ["sh", "-c",
             f"set -e; mkdir -p /backup; "
             f"tar -czf /backup/{relative_archive} -C /data . && "
             f"echo ARCHIVE_OK && "
             f"sha256sum /backup/{relative_archive} | awk '{{print $1}}'"],
            volumes={volume: {"bind": "/data", "mode": "ro"},
                     backup_dir: {"bind": "/backup", "mode": "rw"}},
            name_hint=container_name,
        )
        if code != 0 or "ARCHIVE_OK" not in out:
            raise BackupError("Volume archive failed", detail=out[-600:])

        digest = ""
        size = 0
        for line in out.splitlines():
            line = line.strip()
            if len(line) == 64 and all(c in "0123456789abcdef" for c in line):
                digest = line
        if node_is_local:
            path = archive_path(backup_dir, name)
            if path.exists():
                size = path.stat().st_size
                if not digest:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest() if size < 2 * 1024 ** 3 else ""

        step("verifying archive")
        verified, verify_note = verify_archive(client, backup_dir, name)

        metadata = {
            "name": name,
            "container_name": container_name,
            "archive": str(archive_path(backup_dir, name)),
            "relative_archive": relative_archive,
            "meta_file": str(meta_path(backup_dir, name)),
            "node_is_local": node_is_local,
            "size_bytes": size,
            "sha256": digest,
            "created_at": created.isoformat(),
            "mode": mode,
            "quiesced": paused or stopped,
            "previous_state": state_before,
            "verified": verified,
            "verify_note": verify_note,
            "note": note,
            "configuration": configuration_snapshot(client, container_name, allocator),
        }

        step("writing metadata")
        if node_is_local:
            meta_path(backup_dir, name).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        else:
            # Remote node: the archive lives on *that* machine, so the side-car
            # has to be written there too (through a helper container).
            payload = json.dumps(metadata)
            run_helper(
                client,
                ["sh", "-c",
                 "printf '%s' " + _shell_single_quote(payload) + f" > /backup/{name}{META_SUFFIX}"],
                volumes={backup_dir: {"bind": "/backup", "mode": "rw"}},
                name_hint=container_name,
                timeout=300,
            )
        return metadata
    finally:
        if paused:
            ctr.unpause(client, container_name)
        elif stopped:
            try:
                ctr.start(client, container_name)
            except Exception as exc:
                logger.error("could not restart %s after backup: %s", container_name, exc)


def _volume_missing(client: Any, volume: str) -> bool:
    try:
        client.volumes.get(volume)
        return False
    except Exception:
        return True


def _shell_single_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


# ── verification ────────────────────────────────────────────────────────────

def verify_archive(client: Any, backup_dir: str, name: str) -> Tuple[bool, str]:
    """List the archive contents to prove it is a readable tar."""
    try:
        code, out, _err = run_helper(
            client,
            ["sh", "-c",
             f"set -e; test -s /backup/{name}{ARCHIVE_SUFFIX} && "
             f"tar -tzf /backup/{name}{ARCHIVE_SUFFIX} | head -n 5 && "
             f"tar -tzf /backup/{name}{ARCHIVE_SUFFIX} | wc -l && echo VERIFY_OK"],
            volumes={backup_dir: {"bind": "/backup", "mode": "ro"}},
            name_hint="verify",
            timeout=900,
        )
    except BackupError as exc:
        return False, str(exc)
    if code == 0 and "VERIFY_OK" in out:
        entries = [line for line in out.splitlines() if line.strip().isdigit()]
        count = entries[-1] if entries else "?"
        return True, f"archive lists {count} entries"
    return False, (out or "verification failed")[-300:]


# ── restore ─────────────────────────────────────────────────────────────────

def restore_backup(client: Any, container_name: str, *, archive: str,
                   node_is_local: bool, allocator: Any = None,
                   wipe: bool = True, progress: Any = None) -> Dict[str, Any]:
    """
    Replace the contents of a VPS volume with a backup archive.

    ``wipe=True`` empties the volume first (a true restore).  The container is
    stopped for the duration and restarted afterwards.
    """
    volume = f"{config.DOCKER_VOLUME_PREFIX}{container_name}"
    if _volume_missing(client, volume):
        raise BackupError(f"Persistent volume {volume} does not exist — create the VPS first")

    backup_dir = str(config.DOCKER_BACKUP_DIR)
    filename = Path(archive).name
    if not filename.endswith(ARCHIVE_SUFFIX):
        raise BackupError("That does not look like a UnixNodes backup archive")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise BackupError("Invalid archive name")

    def step(message: str) -> None:
        logger.info("restore %s: %s", container_name, message)
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    was_running = ctr.simple_state(client, container_name) == "running"
    step("stopping container")
    ctr.stop(client, container_name, timeout=120, force=True)
    try:
        if wipe:
            step("clearing current data")
            code, out, _err = run_helper(
                client,
                ["sh", "-c",
                 "cd /data && find . -mindepth 1 -maxdepth 1 "
                 "-not -name 'lost+found' -exec rm -rf {} + && echo WIPED"],
                volumes={volume: {"bind": "/data", "mode": "rw"}},
                name_hint=container_name,
            )
            if code != 0 or "WIPED" not in out:
                raise BackupError("Could not clear the volume", detail=out[-400:])

        step("extracting archive")
        code, out, _err = run_helper(
            client,
            ["sh", "-c",
             f"set -e; tar -xzf /backup/{filename} -C /data && echo RESTORE_OK"],
            volumes={volume: {"bind": "/data", "mode": "rw"},
                     backup_dir: {"bind": "/backup", "mode": "ro"}},
            name_hint=container_name,
        )
        if code != 0 or "RESTORE_OK" not in out:
            raise BackupError("Archive extraction failed", detail=out[-600:])
    finally:
        step("restarting container")
        try:
            ctr.start(client, container_name)
        except Exception as exc:
            logger.error("could not start %s after restore: %s", container_name, exc)

    return {"container": container_name, "archive": filename,
            "was_running": was_running, "restored_at": datetime.now(timezone.utc).isoformat()}


# ── inventory / retention ───────────────────────────────────────────────────

def list_local(backup_dir: Optional[str] = None,
               container_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read backup metadata straight from the node filesystem (local nodes)."""
    directory = Path(backup_dir or str(config.DOCKER_BACKUP_DIR))
    if not directory.exists():
        return []
    items: List[Dict[str, Any]] = []
    for meta in sorted(directory.glob(f"*{META_SUFFIX}")):
        if container_name and not meta.name.startswith(f"{container_name}-"):
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        archive = directory / data.get("relative_archive", meta.name.replace(META_SUFFIX, ARCHIVE_SUFFIX))
        data["exists"] = archive.exists()
        if archive.exists():
            data["size_bytes"] = archive.stat().st_size
        items.append(data)
    return items


def list_remote(client: Any, backup_dir: Optional[str] = None,
                container_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """List backups that live on a remote node."""
    directory = str(backup_dir or config.DOCKER_BACKUP_DIR)
    pattern = f"{container_name}-*" if container_name else "*"
    try:
        code, out, _err = run_helper(
            client,
            ["sh", "-c",
             f"ls -la --time-style=+%s /backup/{pattern}{ARCHIVE_SUFFIX} 2>/dev/null | tail -n +2 || true"],
            volumes={directory: {"bind": "/backup", "mode": "ro"}},
            name_hint="list",
            timeout=300,
        )
    except BackupError:
        return []
    items: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            size = int(parts[4])
            mtime = int(parts[5])
        except ValueError:
            continue
        path = parts[-1]
        name = Path(path).name[: -len(ARCHIVE_SUFFIX)]
        items.append({
            "name": name,
            "container_name": container_name or name.rsplit("-", 2)[0],
            "archive": path,
            "relative_archive": Path(path).name,
            "size_bytes": size,
            "created_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "node_is_local": False,
            "verified": False,
        })
    return items


def delete_backup(client: Any, backup_dir: Optional[str], name: str, *,
                  node_is_local: bool) -> bool:
    """Remove an archive and its metadata."""
    directory = backup_dir or str(config.DOCKER_BACKUP_DIR)
    if node_is_local:
        removed = False
        for path in (archive_path(directory, name), meta_path(directory, name)):
            try:
                if path.exists():
                    path.unlink()
                    removed = True
            except OSError as exc:
                logger.warning("could not delete %s: %s", path, exc)
        return removed
    code, _out, _err = run_helper(
        client,
        ["sh", "-c",
         f"rm -f /backup/{name}{ARCHIVE_SUFFIX} /backup/{name}{META_SUFFIX} && echo DELETED"],
        volumes={directory: {"bind": "/backup", "mode": "rw"}},
        name_hint="delete",
        timeout=300,
    )
    return code == 0


def prune(client: Any, *, node_is_local: bool, keep: Optional[int] = None,
          retention_days: Optional[int] = None,
          backup_dir: Optional[str] = None) -> List[str]:
    """Apply retention: newest N per container plus an age cutoff."""
    keep = int(keep if keep is not None else config.DOCKER_BACKUP_KEEP)
    days = int(retention_days if retention_days is not None else config.DOCKER_BACKUP_RETENTION_DAYS)
    listings = list_local(backup_dir) if node_is_local else list_remote(client, backup_dir)
    cutoff = time.time() - days * 86400

    by_container: Dict[str, List[Dict[str, Any]]] = {}
    for item in listings:
        by_container.setdefault(str(item.get("container_name") or ""), []).append(item)

    removed: List[str] = []
    for _container, items in by_container.items():
        items.sort(key=lambda entry: str(entry.get("created_at") or ""), reverse=True)
        for position, item in enumerate(items):
            created = _parse_ts(item.get("created_at"))
            too_old = created is not None and created < cutoff
            if position < keep and not too_old:
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            if delete_backup(client, backup_dir, name, node_is_local=node_is_local):
                removed.append(name)
    return removed


def _parse_ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def disk_usage(backup_dir: Optional[str] = None) -> Dict[str, Any]:
    directory = Path(backup_dir or str(config.DOCKER_BACKUP_DIR))
    if not directory.exists():
        return {"path": str(directory), "exists": False, "bytes": 0, "count": 0}
    total = 0
    count = 0
    for item in directory.glob(f"*{ARCHIVE_SUFFIX}"):
        try:
            total += item.stat().st_size
            count += 1
        except OSError:
            continue
    return {"path": str(directory), "exists": True, "bytes": total, "count": count}
