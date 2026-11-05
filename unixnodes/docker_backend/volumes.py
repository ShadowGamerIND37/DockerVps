"""
Persistent volumes.

Every VPS owns exactly one named volume which is mounted at ``/`` inside the
container.  Mounting at the root gives the guest a *fully persistent* Linux
filesystem: packages installed with ``apt``/``apk``, ``/etc`` configuration,
cron jobs and every home directory all survive a container stop, restart,
resource change or full rebuild.  Docker layers its own ``/proc``, ``/sys``,
``/dev``, ``/etc/hosts``, ``/etc/resolv.conf`` and ``/etc/hostname`` on top of
the volume, so the guest still behaves like a normal Linux system.

Docker volumes have **no built-in size limit**.  Real quotas are applied by
:mod:`unixnodes.docker_backend.resources` on the host filesystem.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from .. import config
from ..errors import VolumeError

logger = logging.getLogger("unixnodes.docker.volumes")

MOUNT_POINT = "/"


def volume_name(container_name: str) -> str:
    """Deterministic volume name for a VPS."""
    return f"{config.DOCKER_VOLUME_PREFIX}{container_name}"


def create(client: Any, container_name: str, *, labels: Optional[Dict[str, str]] = None,
           driver_opts: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Create (idempotently) the persistent volume for a VPS."""
    name = volume_name(container_name)
    existing = get(client, name)
    if existing is not None:
        return existing
    try:
        volume = client.volumes.create(
            name=name,
            driver="local",
            labels={**(labels or {})},
            driver_opts=driver_opts or None,
        )
    except Exception as exc:
        raise VolumeError(f"Could not create volume {name}", detail=str(exc)) from exc
    return dict(volume.attrs)


def get(client: Any, name: str) -> Optional[Dict[str, Any]]:
    try:
        return dict(client.volumes.get(name).attrs)
    except Exception:
        return None


def exists(client: Any, container_name: str) -> bool:
    return get(client, volume_name(container_name)) is not None


def mountpoint(client: Any, container_name: str) -> Optional[str]:
    attrs = get(client, volume_name(container_name))
    if not attrs:
        return None
    return str(attrs.get("Mountpoint") or "") or None


def remove(client: Any, container_name: str, *, force: bool = True) -> bool:
    """Delete the VPS volume.  Only ever called by the delete/rollback paths."""
    name = volume_name(container_name)
    try:
        client.volumes.get(name).remove(force=force)
        logger.info("removed volume %s", name)
        return True
    except Exception as exc:
        logger.warning("could not remove volume %s: %s", name, exc)
        return False


def list_managed(client: Any) -> List[Dict[str, Any]]:
    prefix = config.DOCKER_VOLUME_PREFIX
    out: List[Dict[str, Any]] = []
    for volume in client.volumes.list():
        attrs = dict(volume.attrs or {})
        name = str(attrs.get("Name") or "")
        if not name.startswith(prefix):
            continue
        out.append({
            "name": name,
            "container": name[len(prefix):],
            "mountpoint": attrs.get("Mountpoint"),
            "created": attrs.get("CreatedAt"),
            "labels": attrs.get("Labels") or {},
        })
    return out


def rename(client: Any, container_name: str, new_container_name: str) -> bool:
    """
    Rename a volume by copying it to a new name.

    Docker has no volume rename, and ``docker cp`` between volumes needs a
    helper container; the copy is done with a short-lived busybox/alpine
    container so no customer workload is involved.
    """
    src = volume_name(container_name)
    dst = volume_name(new_container_name)
    if get(client, dst) is not None:
        raise VolumeError(f"Target volume {dst} already exists")
    if get(client, src) is None:
        raise VolumeError(f"Source volume {src} does not exist")
    helper = config.DOCKER_BACKUP_HELPER_IMAGE
    container = None
    try:
        client.images.get(helper)
    except Exception:
        try:
            client.images.pull(helper)
        except Exception as exc:
            raise VolumeError(f"Backup helper image {helper} is unavailable", detail=str(exc)) from exc
    try:
        container = client.containers.create(
            helper,
            command=["sh", "-c",
                     "cp -a /src/. /dst/ && sync && echo VOLUME_COPY_OK"],
            volumes={src: {"bind": "/src", "mode": "ro"},
                     dst: {"bind": "/dst", "mode": "rw"}},
            labels={config.LABEL_MANAGED: "true", config.LABEL_ROLE: config.ROLE_BACKUP_HELPER},
            network_disabled=True,
        )
        container.start()
        result = container.wait(timeout=1800)
        output = container.logs(tail=50).decode("utf-8", "replace")
        if int(result.get("StatusCode", 1)) != 0 or "VOLUME_COPY_OK" not in output:
            raise VolumeError("Volume copy failed", detail=output[-500:])
        return True
    except VolumeError:
        raise
    except Exception as exc:
        raise VolumeError("Volume copy failed", detail=str(exc)) from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass


# ── host level usage ────────────────────────────────────────────────────────

def usage_bytes(path: str) -> int:
    """Sum the bytes used by a directory tree on the *bot* host (local nodes)."""
    if not path:
        return 0
    try:
        completed = subprocess.run(
            ["du", "-sb", "--", path],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if completed.returncode == 0 and completed.stdout:
            return int(completed.stdout.split()[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("du failed for %s: %s", path, exc)
    return 0


def filesystem_stats(path: str) -> Dict[str, Any]:
    """Total/free bytes of the filesystem backing a volume."""
    import shutil

    if not path:
        return {}
    try:
        total, used, free = shutil.disk_usage(path)
        return {"total": int(total), "used": int(used), "free": int(free),
                "percent": round(used / total * 100, 1) if total else 0.0}
    except OSError:
        return {}


def mounts_for(container_name: str, *, labels: Optional[Dict[str, str]] = None,
               extra_binds: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Dict[str, str]]:
    """
    Bind specification for a VPS container.

    The persistent volume is mounted read-write at ``/``; the caller adds the
    read-only init script and (on first boot) the credential file.
    """
    binds: Dict[str, Dict[str, str]] = {
        volume_name(container_name): {"bind": MOUNT_POINT, "mode": "rw"},
    }
    binds.update(extra_binds or {})
    return binds


def safe_quote(path: str) -> str:
    return shlex.quote(str(path))
