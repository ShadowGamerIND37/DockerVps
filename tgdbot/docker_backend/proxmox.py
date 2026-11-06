"""The isolated Proxmox VE workload contract.

Proxmox is not an SSH-oriented Linux rootfs.  The community ``dockurr/proxmox``
image runs systemd and expects privileged mode, KVM/FUSE, its native stop
signal, two persistent volumes and its native HTTPS console on port 8006. These
helpers keep that contract out of the ordinary unprivileged VPS pipeline.
The pinned dockur image's healthcheck starts over HTTP; Proxmox redirects that
request to the HTTPS console.

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
from urllib.parse import urlsplit

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
CONSOLE_SCHEME = "https"
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_REQUEST_TIMEOUT = 8


def console_url(host: str, port: int) -> str:
    """Return the native console URL for the pinned dockur/proxmox runtime."""
    return f"{CONSOLE_SCHEME}://{host}:{int(port)}"


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
                         published_ports: Any, labels: Dict[str, str],
                         kvm_enabled: bool = True,
                         apparmor_enabled: bool = False) -> Dict[str, Any]:
    """Build only the Proxmox spec; ordinary VPS never inherit these overrides."""
    if not kvm_enabled:
        raise ContainerError(
            "The supported Proxmox runtime requires KVM; refusing a "
            "KVM-disabled privileged container.")
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
        kvm_enabled=kvm_enabled, apparmor_enabled=apparmor_enabled,
    )
    spec["environment"] = environment
    # Keep the specialized runtime's historical seccomp/no-new-privileges
    # behaviour, but retain the explicit AppArmor choice resolved for this VPS.
    spec["security_opt"] = [
        option for option in (spec.get("security_opt") or [])
        if str(option).lower().startswith("apparmor=")
    ]
    spec.update({
        "privileged": True,
        "cap_drop": [],
        "sysctls": {},
        "init": False,
        "stop_signal": "SIGRTMIN+3",
        "tty": True,
        "stdin_open": True,
    })
    return spec


_CERT_CHECK_SCRIPT = r"""cert=/etc/pve/local/pve-ssl.pem
key=/etc/pve/local/pve-ssl.key
[ -s "$cert" ] && [ -s "$key" ] || exit 1
openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 || exit 1
openssl pkey -in "$key" -noout -check >/dev/null 2>&1 || exit 1
cert_public=$(openssl x509 -in "$cert" -noout -pubkey 2>/dev/null)
key_public=$(openssl pkey -in "$key" -pubout 2>/dev/null)
[ -n "$cert_public" ] && [ "$cert_public" = "$key_public" ]
"""
_CFS_READY_SCRIPT = (
    "timeout 5s perl -MPVE::Cluster -e "
    "'PVE::Cluster::check_cfs_is_mounted(); PVE::Cluster::cfs_update(1); "
    "PVE::Cluster::check_cfs_quorum();' 2>&1"
)
_CERT_UPDATE_SCRIPT = r"""resolv_conf=/etc/resolv.conf
backup=
sanitized=
modified=0
cleanup() {
    rc=$?
    trap - EXIT
    if [ "$modified" -eq 1 ]; then
        if ! cat "$backup" > "$resolv_conf"; then
            echo "could not restore $resolv_conf after certificate update" >&2
            if [ "$rc" -eq 0 ]; then rc=1; fi
        fi
    fi
    [ -z "$backup" ] || rm -f "$backup"
    [ -z "$sanitized" ] || rm -f "$sanitized"
    exit "$rc"
}
trap cleanup EXIT

# Proxmox appends the resolver search domain to the certificate CN. OpenSSL
# rejects a CN longer than 64 characters, common with cloud-internal suffixes.
node=$(hostname 2>/dev/null)
node=${node%%.*}
search=$(awk '$1 == "search" || $1 == "domain" { value=$2 } END { if (value != "") print value }' "$resolv_conf" 2>/dev/null)
if [ -n "$search" ] && [ $(( ${#node} + ${#search} + 1 )) -gt 64 ]; then
    backup=$(mktemp) || exit 1
    sanitized=$(mktemp) || exit 1
    if ! cp -p "$resolv_conf" "$backup"; then
        echo "could not back up $resolv_conf before certificate update" >&2
        exit 1
    fi
    if ! awk '$1 != "search" && $1 != "domain" { print }' "$backup" > "$sanitized"; then
        echo "could not prepare a temporary resolver file for certificate update" >&2
        exit 1
    fi
    # Set this before writing so even a partial write is restored by the trap.
    modified=1
    if ! cat "$sanitized" > "$resolv_conf"; then
        echo "could not temporarily shorten the resolver search domain" >&2
        exit 1
    fi
fi

timeout 100s pvecm updatecerts --force 2>&1
"""
_CERT_RESTART_SCRIPT = "timeout 100s systemctl restart pveproxy 2>&1"


def _run_certificate_command(client: Any, container_name: str, script: str,
                             *, user: str) -> tuple[int, str, str]:
    try:
        return ctr.exec_argv(client, container_name, ["sh", "-c", script], user=user)
    except Exception as exc:  # the service may still be initializing pmxcfs
        return -1, "", str(exc)


def _certificate_output(code: int, stdout: str, stderr: str) -> str:
    text = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    return text or (f"command exited with status {code}" if code else "")


def _wait_for_pmxcfs(client: Any, container_name: str, *, timeout: float,
                     interval: float) -> tuple[bool, str]:
    """Wait until pmxcfs is mounted, answering IPC, and quorate."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    # Avoid a tight loop when tests disable the repair backoff.
    pause = max(0.1, float(interval))
    last_output = "pmxcfs readiness was not checked"
    while True:
        code, stdout, stderr = _run_certificate_command(
            client, container_name, _CFS_READY_SCRIPT, user="root")
        last_output = _certificate_output(code, stdout, stderr)
        if code == 0:
            return True, last_output
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_output
        time.sleep(min(pause, remaining))


def ensure_console_certificate(client: Any, container_name: str, *, attempts: int = 2,
                               interval: float = 2.0, cluster_timeout: float = 90.0) -> str:
    """Ensure pveproxy has a usable node certificate before probing its console.

    The pinned image starts pveproxy even when its certificate-preparation
    ``ExecStartPre`` fails. That leaves a listening socket whose workers exit
    repeatedly, so a network-only readiness probe can wait forever. Check the
    certificate as the same user as the workers, wait for pmxcfs before
    repairing it with Proxmox's own supported command, then restart pveproxy.
    A valid existing certificate is never regenerated.
    """
    code, stdout, stderr = _run_certificate_command(
        client, container_name, _CERT_CHECK_SCRIPT, user="www-data")
    if code == 0:
        return "Proxmox console certificate is valid"

    last_output = _certificate_output(code, stdout, stderr)
    tries = max(1, int(attempts))
    pause = max(0.0, float(interval))
    for attempt in range(tries):
        # pvecm accesses pmxcfs before its own quorum wait. Do not invoke it
        # while the container's systemd stack is still starting pmxcfs.
        cluster_ready, cluster_output = _wait_for_pmxcfs(
            client, container_name, timeout=cluster_timeout, interval=pause)
        if not cluster_ready:
            last_output = "pmxcfs is not ready: " + cluster_output
            break

        code, stdout, stderr = _run_certificate_command(
            client, container_name, _CERT_UPDATE_SCRIPT, user="root")
        update_output = _certificate_output(code, stdout, stderr)
        last_output = update_output
        if code == 0:
            # Check again as www-data: root-only readability is not sufficient
            # for the pveproxy workers that load the TLS context.
            check_code, check_out, check_err = _run_certificate_command(
                client, container_name, _CERT_CHECK_SCRIPT, user="www-data")
            if check_code != 0:
                validation_output = ("Certificate is still unusable after pvecm updatecerts: "
                                     + _certificate_output(check_code, check_out, check_err))
                last_output = "\n".join(part for part in (update_output, validation_output) if part)
            else:
                code, stdout, stderr = _run_certificate_command(
                    client, container_name, _CERT_RESTART_SCRIPT, user="root")
                last_output = _certificate_output(code, stdout, stderr)
                if code == 0:
                    return "Proxmox console certificate repaired; pveproxy restarted"
        if attempt + 1 < tries and pause:
            time.sleep(pause)

    raise ContainerError(
        f"Proxmox console certificate setup failed: {last_output[:700]}",
        detail=last_output[:4000],
        user_message="Proxmox could not prepare its console certificate; see the deployment error.",
    )


def _get_console_response(host: str, port: int, scheme: str, path: str) -> tuple[int, Optional[str]]:
    """Make one unauthenticated request and return its status and redirect target."""
    if scheme == "https":
        # The image creates a self-signed certificate on first boot. This is an
        # availability check only; no credentials are transmitted.
        context = ssl._create_unverified_context()
        connection = http.client.HTTPSConnection(
            host, int(port), timeout=_REQUEST_TIMEOUT, context=context)
    else:
        connection = http.client.HTTPConnection(host, int(port), timeout=_REQUEST_TIMEOUT)
    try:
        connection.request("GET", path, headers={"User-Agent": "tgdbot-proxmox-ready/1"})
        response = connection.getresponse()
        status = int(response.status)
        location = response.getheader("Location")
        response.read(2048)
        return status, location
    finally:
        connection.close()


def verify_web(host: str, port: int, *, attempts: Optional[int] = None,
               interval: Optional[float] = None) -> tuple[bool, str]:
    """Wait for the Proxmox login page, including its native HTTP→HTTPS redirect.

    The pinned image healthcheck starts with ``curl -kLfSs http://localhost:8006``.
    pveproxy answers that plain HTTP request with a redirect to HTTPS. Follow the
    redirect just like the healthcheck, but only count a final HTTPS 200 as ready:
    a redirect alone does not prove that the TLS endpoint is usable.
    """
    tries = max(1, int(attempts or config.PROXMOX_READY_ATTEMPTS))
    pause = max(0.0, float(config.PROXMOX_READY_INTERVAL if interval is None else interval))
    last_error = "no response"
    for attempt in range(tries):
        try:
            status, location = _get_console_response(host, int(port), "http", "/")
            if status == 200:
                # The reviewed pveproxy contract is HTTPS. Do not silently call
                # an HTTP-only response ready and then hand out an HTTPS URL.
                last_error = "HTTP 200 was returned without the expected HTTPS redirect"
            elif status not in _REDIRECT_STATUSES:
                last_error = f"HTTP {status} from the HTTP console probe"
            elif not location:
                last_error = f"HTTP {status} redirect did not include a Location header"
            else:
                target = urlsplit(location)
                target_scheme = target.scheme.lower()
                target_port = target.port or (443 if target_scheme == "https" else 80)
                same_host = (target.hostname or "").lower() == str(host).lower()
                if (target_scheme != "https" or not same_host
                        or target_port != int(port) or target.username or target.password):
                    last_error = "HTTP redirect did not stay on the published HTTPS console endpoint"
                else:
                    path = target.path or "/"
                    if target.query:
                        path += "?" + target.query
                    https_status, _ = _get_console_response(host, int(port), "https", path)
                    if https_status == 200:
                        return True, ("Proxmox HTTPS console responded with HTTP 200 "
                                      "after the native HTTP redirect.")
                    last_error = f"HTTPS console returned HTTP {https_status} after the HTTP redirect"
        except Exception as exc:  # connection resets are normal during systemd boot
            last_error = str(exc)[:300]
        if attempt + 1 < tries and pause:
            time.sleep(pause)
    return False, f"Proxmox HTTPS console did not become ready after {tries} attempts: {last_error}"

