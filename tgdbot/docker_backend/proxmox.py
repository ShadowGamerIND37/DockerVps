"""The isolated Proxmox VE workload contract.

Proxmox is not an SSH-oriented Linux rootfs.  The community ``dockurr/proxmox``
image runs systemd and expects privileged mode, KVM/FUSE, its native stop
signal, two persistent volumes and its HTTPS console on port 8006.  These
helpers keep that contract out of the ordinary unprivileged VPS pipeline.

The container receives a one-way ``PASSWORD_HASH`` rather than a plaintext
``PASSWORD`` environment variable.  The generated password is returned only to
the owner by the existing credential-delivery layer.
"""

from __future__ import annotations

import http.client
import logging
import secrets
import ssl
import string
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config
from ..errors import ContainerError, SecurityPolicyError, VolumeError
from . import containers as ctr
from . import volumes

logger = logging.getLogger("tgdbot.docker.proxmox")

VZ_MOUNT = "/var/lib/vz"
CLUSTER_MOUNT = "/var/lib/pve-cluster"
WEB_PORT = config.PROXMOX_CONTAINER_PORT
MIN_RAM_MB = config.PROXMOX_MIN_RAM_MB
MIN_STORAGE_GB = config.PROXMOX_MIN_STORAGE_GB
MIN_PIDS = 1024


def is_image(image_ref: Any) -> bool:
    """Whether a stored/reference image selects the built-in Proxmox workload."""
    return str(image_ref or "").strip() == config.PROXMOX_IMAGE


def is_proxmox_reference(image_ref: Any) -> bool:
    """Conservatively identify Proxmox images, including unapproved variants.

    Custom Proxmox images are deliberately not accepted through ``image-add``:
    they would otherwise bypass the grant check and might have a different
    privileged runtime contract.
    """
    ref = str(image_ref or "").lower()
    return "proxmox" in ref or "dockermox" in ref


def assert_supported_reference(image_ref: Any) -> None:
    if is_proxmox_reference(image_ref) and not is_image(image_ref):
        raise SecurityPolicyError(
            "Only the built-in, reviewed Proxmox image is supported; custom Proxmox "
            "images cannot be approved as ordinary VPS images.",
            user_message="Use the built-in Proxmox option. Custom Proxmox images are not allowed.",
        )


def validate_resources(ram_mb: int, storage_gb: int) -> None:
    if int(ram_mb) < MIN_RAM_MB:
        raise ContainerError(
            f"Proxmox requires at least {MIN_RAM_MB // 1024} GB RAM (requested {ram_mb} MB).",
            user_message=f"Proxmox needs at least {MIN_RAM_MB // 1024} GB RAM.",
        )
    if int(storage_gb) < MIN_STORAGE_GB:
        raise ContainerError(
            f"Proxmox requires at least {MIN_STORAGE_GB} GB storage (requested {storage_gb} GB).",
            user_message=f"Proxmox needs at least {MIN_STORAGE_GB} GB storage.",
        )


def effective_pids(requested: int) -> int:
    """systemd plus the Proxmox service stack needs more than the SSH VPS default."""
    return max(int(requested), MIN_PIDS)


def preflight(*, node_is_local: bool) -> str:
    """Fail early with an actionable host error where the host is inspectable.

    Remote-node device paths are not local to the bot process.  For those nodes
    the image's own entrypoint performs the authoritative KVM/FUSE checks and
    deployment reports its logs if it cannot start.
    """
    if not node_is_local:
        return "Remote node: KVM/FUSE will be validated by the Proxmox entrypoint."
    missing = []
    if config.PROXMOX_REQUIRE_KVM and not Path("/dev/kvm").exists():
        missing.append("/dev/kvm (hardware virtualization/KVM)")
    if config.PROXMOX_REQUIRE_FUSE and not Path("/dev/fuse").exists():
        missing.append("/dev/fuse")
    if missing:
        raise ContainerError(
            "Proxmox host requirements missing: " + ", ".join(missing),
            user_message="This node cannot run Proxmox: required KVM/FUSE host devices are missing.",
        )
    return "Local KVM/FUSE host devices are available."


def password_hash(password: str) -> str:
    """Return a SHA-512 crypt hash without putting the password in process argv."""
    if not password:
        raise ContainerError("A nonempty Proxmox root password is required")
    salt = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    try:
        result = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
            input=password + "\n", text=True, capture_output=True,
            timeout=15, check=False,
        )
    except FileNotFoundError as exc:
        raise ContainerError(
            "OpenSSL is required on the bot host to hash the Proxmox root password.",
            user_message="The bot host is missing OpenSSL; Proxmox credentials could not be prepared safely.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ContainerError("OpenSSL timed out while hashing the Proxmox root password") from exc
    hashed = str(result.stdout or "").strip()
    if result.returncode != 0 or not hashed.startswith("$6$") or "\n" in hashed:
        raise ContainerError(
            "OpenSSL could not produce a SHA-512 password hash",
            detail=str(result.stderr or "")[:1000],
            user_message="Proxmox credentials could not be prepared safely.",
        )
    return hashed


def cluster_volume_name(container_name: str) -> str:
    # A separate suffix avoids collisions with the primary data volume and is
    # stable across recreation/reinstall.
    return f"{volumes.volume_name(container_name)}-pve-cluster"


def create_volumes(client: Any, container_name: str, *, labels: Dict[str, str]) -> Dict[str, str]:
    """Create both persistent volumes and return their canonical names."""
    primary = volumes.create(client, container_name, labels=labels)
    cluster = cluster_volume_name(container_name)
    try:
        client.volumes.create(name=cluster, driver="local", labels=dict(labels))
    except Exception as exc:
        raise VolumeError(f"Could not create Proxmox cluster volume {cluster}", detail=str(exc)) from exc
    return {"data": primary["Name"], "cluster": cluster}


def mounts_for(container_name: str, *, data_volume: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    return {
        str(data_volume or volumes.volume_name(container_name)): {"bind": VZ_MOUNT, "mode": "rw"},
        cluster_volume_name(container_name): {"bind": CLUSTER_MOUNT, "mode": "rw"},
    }


def assert_volumes_exist(client: Any, container_name: str, *,
                         data_volume: Optional[str] = None) -> None:
    """Never silently attach a fresh cluster volume to an existing Proxmox VPS."""
    for name in mounts_for(container_name, data_volume=data_volume):
        if volumes.get(client, name) is None:
            raise VolumeError(
                f"Proxmox persistent volume {name} is missing; refusing to recreate with empty storage.",
                user_message="A Proxmox data volume is missing. The container was not recreated.",
            )


def remove_cluster_volume(client: Any, container_name: str) -> bool:
    name = cluster_volume_name(container_name)
    try:
        volume = client.volumes.get(name)
        volume.remove(force=True)
        return True
    except Exception as exc:
        # Match volumes.remove()'s not-found semantics without hiding real
        # deletion failures from lifecycle cleanup.
        from docker.errors import NotFound
        if isinstance(exc, NotFound):
            return False
        raise VolumeError(f"Could not remove Proxmox cluster volume {name}", detail=str(exc)) from exc


def build_container_spec(*, container_name: str, image_ref: str, password_hash_value: Optional[str],
                         ram_mb: int, cpu: float, pids: int, publish_ip: str,
                         published_ports: Any, labels: Dict[str, str]) -> Dict[str, Any]:
    """Build only the Proxmox spec; ordinary VPS never inherit these overrides."""
    environment = {
        "REQUIRE_KVM": "Y" if config.PROXMOX_REQUIRE_KVM else "N",
        "REQUIRE_FUSE": "Y" if config.PROXMOX_REQUIRE_FUSE else "N",
    }
    if password_hash_value:
        # Do not add PASSWORD: docker inspect and committed image metadata must
        # never contain the plaintext credential.
        environment["PASSWORD_HASH"] = password_hash_value
    spec = ctr.build_container_spec(
        container_name=container_name, image=image_ref, command=None,
        volumes=mounts_for(container_name), publish_ip=publish_ip,
        published_ports=published_ports, labels={
            **labels, config.LABEL_WORKLOAD: config.WORKLOAD_PROXMOX,
        }, ram_mb=ram_mb, cpu=cpu, pids=effective_pids(pids),
    )
    spec["environment"] = environment
    spec.update({
        "privileged": True,
        "cap_drop": [],
        "security_opt": [],
        "devices": [],
        "sysctls": {},
        "init": False,
        "stop_signal": "SIGRTMIN+3",
        "tty": True,
        "stdin_open": True,
    })
    return spec


def verify_web(host: str, port: int, *, attempts: Optional[int] = None,
               interval: Optional[float] = None) -> tuple[bool, str]:
    """Wait for the unauthenticated Proxmox login page over its native HTTPS port."""
    tries = max(1, int(attempts or config.PROXMOX_READY_ATTEMPTS))
    pause = max(0.0, float(config.PROXMOX_READY_INTERVAL if interval is None else interval))
    last_error = "no response"
    for attempt in range(tries):
        connection = None
        try:
            # Proxmox installs a self-signed certificate on first boot.  This is
            # an availability probe only; no credentials are transmitted.
            context = ssl._create_unverified_context()
            connection = http.client.HTTPSConnection(host, int(port), timeout=8, context=context)
            connection.request("GET", "/", headers={"User-Agent": "tgdbot-proxmox-ready/1"})
            response = connection.getresponse()
            response.read(2048)
            if response.status in (200, 301, 302, 303, 307, 308):
                return True, f"Proxmox HTTPS console responded with HTTP {response.status}."
            last_error = f"HTTP {response.status}"
        except Exception as exc:  # connection resets are normal during systemd boot
            last_error = str(exc)[:300]
        finally:
            if connection is not None:
                connection.close()
        if attempt + 1 < tries and pause:
            time.sleep(pause)
    return False, f"Proxmox HTTPS console did not become ready after {tries} attempts: {last_error}"

