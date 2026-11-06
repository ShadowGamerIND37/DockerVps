"""
Container security policy.

A customer VPS is a *shared-kernel* Linux environment.  Docker gives us
namespaces, cgroups and capabilities — not a hypervisor.  This module is the
single place where the hardening decisions are made so they can never drift
between the provisioning, resize and rebuild paths.

Hard rules (not configurable):

* Ordinary customer VPS never enable ``privileged`` — a privileged container is
  root on the host. The separately gated Proxmox workload is an explicit,
  documented exception with its own runtime contract.
* ``/var/run/docker.sock`` is never mounted into a customer container.
* Host devices and host namespaces (``pid=host``, ``network=host``,
  ``ipc=host``) are never shared implicitly. The only device exception is an
  explicit, administrator-selected ``/dev/kvm`` mapping for a new VPS.
* Only the bot's own read-only init script and credential file are bind
  mounted.  Everything else lives on the VPS volume.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import config
from ..errors import SecurityPolicyError

# ── capability policy ───────────────────────────────────────────────────────
#
# Docker's default set already excludes SYS_ADMIN/SYS_MODULE/etc.  We drop the
# remainder that a normal Linux workload does not need. CHOWN/FOWNER/DAC_*/
# SETUID/SETGID/AUDIT_WRITE are deliberately *kept* for APT, dpkg, apk and PAM.
# OpenSSH's mandatory pre-auth privilege separation also requires SYS_CHROOT.
#: Candidate capabilities to remove from a customer VPS.
#:
#: Docker already withholds most of these (``SYS_ADMIN``, ``SYS_MODULE``,
#: ``SYS_RAWIO`` …), so listing them is belt-and-braces: it keeps the policy
#: correct even if a future runtime default changes or something else re-adds
#: them. ``CAP_KEEP`` below is subtracted before the list is used and always
#: wins for capabilities required by the guest's core services.
DEFAULT_CAP_DROP: Tuple[str, ...] = (
    "MKNOD",
    "AUDIT_READ",
    "NET_RAW",
    "SYS_ADMIN",
    "SYS_BOOT",
    "SYS_MODULE",
    "SYS_NICE",
    "SYS_PACCT",
    "SYS_PTRACE",
    "SYS_RAWIO",
    "SYS_RESOURCE",
    "SYS_TIME",
    "SYS_TTY_CONFIG",
    "SYSLOG",
    "WAKE_ALARM",
    "BLOCK_SUSPEND",
    "IPC_LOCK",
    "IPC_OWNER",
    "LEASE",
    "LINUX_IMMUTABLE",
    "MAC_ADMIN",
    "MAC_OVERRIDE",
    "NET_ADMIN",
    "NET_BROADCAST",
    "SETFCAP",
    "DAC_READ_SEARCH",
    "FOWNER",
    "SYS_CHROOT",
)

#: Capabilities required by package management, PAM and OpenSSH.
#:
#: In particular, dropping SYS_CHROOT makes sshd accept TCP and send its banner,
#: then kill every pre-auth session when ``chroot("/run/sshd")`` is denied. To
#: a Paramiko client that looks like an EOFError/ConnectionResetError, not an
#: authentication failure. APT also needs FOWNER when it chmods package-index
#: files after changing their ownership to its sandbox user; without it, root
#: gets SetupAPTPartialDirectory/Item::QueueURI "Operation not permitted" warnings.
CAP_KEEP: frozenset = frozenset({
    "CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL", "AUDIT_WRITE", "FSETID", "SETFCAP",
    "FOWNER", "SYS_CHROOT",
})

# Paths that must never appear as a bind mount inside a customer container.
FORBIDDEN_MOUNTS: Tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/containerd",
    "/etc/shadow",
    "/etc/sudoers",
    "/proc/sys",
    "/sys/fs",
    "/boot",
    "/root",
    "/home",
    "/etc/ssh",
    "/var/lib/docker",
)

# /dev/kvm is handled separately by device_mappings(): it is never mapped by
# default, but an administrator may explicitly enable it for a new VPS.
FORBIDDEN_DEVICES: Tuple[str, ...] = (
    "/dev/fuse", "/dev/mem", "/dev/kmsg", "/dev/net/tun",
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:@]{0,255}$")


# ── validation ──────────────────────────────────────────────────────────────

def validate_container_name(name: str) -> Tuple[bool, str]:
    name = str(name or "").strip()
    if not name:
        return False, "Container name is empty."
    if len(name) > 63:
        return False, "Container names are limited to 63 characters."
    if not _NAME_RE.match(name):
        return False, "Use lowercase letters, digits, dashes, dots or underscores."
    if name in ("tgdbot", "docker", "host"):
        return False, "That container name is reserved."
    return True, "ok"


def validate_image_reference(image: str) -> Tuple[bool, str]:
    image = str(image or "").strip()
    if not image:
        return False, "No image specified."
    if not _IMAGE_RE.match(image):
        return False, "That does not look like a valid Docker image reference."
    if any(token in image.lower() for token in ("docker.sock", "../", "~")):
        return False, "Image reference contains a forbidden path."
    return True, "ok"


def validate_port(port: Any) -> Tuple[bool, str]:
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False, "Port must be a number."
    if not 1 <= value <= 65535:
        return False, "Port must be between 1 and 65535."
    if value in config.DOCKER_RESERVED_PORTS:
        return False, f"Port {value} is reserved by the platform."
    return True, "ok"


def validate_protocol(protocol: Any) -> Tuple[bool, str]:
    value = str(protocol or "tcp").strip().lower()
    if value in ("both", "tcp+udp"):
        return True, "ok"
    if value in ("tcp", "udp"):
        return True, "ok"
    return False, "Protocol must be tcp, udp or both."


def assert_not_forbidden_mount(host_path: str) -> None:
    path = str(host_path or "").rstrip("/") or "/"
    lowered = path.lower()
    for forbidden in FORBIDDEN_MOUNTS:
        if lowered == forbidden or lowered.startswith(forbidden.rstrip("/") + "/"):
            raise SecurityPolicyError(f"Mounting {path} into a customer container is not allowed.")


def protocols_for(protocol: str) -> Tuple[str, ...]:
    value = str(protocol or "both").strip().lower()
    if value in ("both", "tcp+udp"):
        return ("tcp", "udp")
    if value == "udp":
        return ("udp",)
    return ("tcp",)


# ── host config assembly ────────────────────────────────────────────────────

def apparmor_security_opt(enabled: Optional[bool]) -> Optional[str]:
    """Return the explicit AppArmor mode for a new container.

    ``None`` retains legacy low-level behaviour. Provisioning always passes a
    boolean: enabled uses the configured profile (or Docker's default), while
    disabled is deliberately unconfined. An enabled request can never resolve
    to the ``unconfined`` profile.
    """
    if enabled is False:
        return "apparmor=unconfined"
    profile = config.DOCKER_APPARMOR_PROFILE
    if enabled is True:
        profile = profile or "docker-default"
        if profile.strip().lower() == "unconfined":
            raise SecurityPolicyError(
                "AppArmor is enabled for this VPS but DOCKER_APPARMOR_PROFILE "
                "is set to unconfined. Configure a real AppArmor profile first.")
        return f"apparmor={profile}"
    return f"apparmor={profile}" if profile else None


def build_security_opts(apparmor_enabled: Optional[bool] = None) -> List[str]:
    opts: List[str] = []
    if config.DOCKER_NO_NEW_PRIVILEGES:
        opts.append("no-new-privileges:true")
    apparmor = apparmor_security_opt(apparmor_enabled)
    if apparmor:
        opts.append(apparmor)
    if config.DOCKER_SECCOMP_PROFILE:
        opts.append(f"seccomp={config.DOCKER_SECCOMP_PROFILE}")
    return opts


def device_mappings(kvm_enabled: Optional[bool] = None) -> List[str]:
    """Map KVM only when the resolved creation policy explicitly enables it."""
    return ["/dev/kvm:/dev/kvm"] if kvm_enabled is True else []


def creation_security_defaults(workload: str) -> Tuple[bool, bool]:
    """Return ``(kvm, apparmor)`` defaults which users cannot override."""
    if workload == config.WORKLOAD_PROXMOX:
        # The supported dockur/proxmox runtime is KVM-backed. AppArmor defaults
        # off for this privileged systemd workload, not for ordinary VPSes.
        return True, False
    return False, True


def _stored_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def security_from_record(record: Dict[str, Any], workload: str) -> Tuple[bool, bool]:
    """Read persisted settings, with defaults for pre-migration records."""
    default_kvm, default_apparmor = creation_security_defaults(workload)
    kvm = record.get("kvm_enabled")
    apparmor = record.get("apparmor_enabled")
    return (default_kvm if kvm is None else _stored_bool(kvm),
            default_apparmor if apparmor is None else _stored_bool(apparmor))


def resolve_creation_security(*, workload: str, requester_admin: bool,
                              kvm_enabled: Optional[bool],
                              apparmor_enabled: Optional[bool]) -> Tuple[bool, bool]:
    """Authorize and resolve creation-only security overrides.

    This check lives in the provisioning backend, not in Discord components.
    A non-admin request containing either field is rejected even when the value
    equals the default, preventing callers from smuggling an override through
    another entry point.
    """
    if not requester_admin and (kvm_enabled is not None or apparmor_enabled is not None):
        raise SecurityPolicyError(
            "Only administrators may change KVM or AppArmor settings.")
    default_kvm, default_apparmor = creation_security_defaults(workload)
    kvm = default_kvm if kvm_enabled is None else bool(kvm_enabled)
    apparmor = default_apparmor if apparmor_enabled is None else bool(apparmor_enabled)
    if workload == config.WORKLOAD_PROXMOX and not kvm:
        raise SecurityPolicyError(
            "The supported Proxmox runtime requires KVM. KVM-disabled Proxmox "
            "creation is refused rather than starting a guest with an unsafe "
            "or unsupported device policy.")
    if not kvm and config.DOCKER_PRIVILEGED:
        raise SecurityPolicyError(
            "KVM-disabled VPS creation is incompatible with privileged Docker mode.")
    # Validate the profile before any volume or port mutation.
    apparmor_security_opt(apparmor)
    return kvm, apparmor


def assert_kvm_available(*, node_is_local: bool, kvm_enabled: bool) -> None:
    """Fail clearly before creation when a local host has no KVM device."""
    if kvm_enabled and node_is_local and not Path("/dev/kvm").exists():
        raise SecurityPolicyError(
            "KVM was enabled for this VPS but /dev/kvm is not available on "
            "the selected node. Disable KVM or choose a KVM-capable node.")


def build_ulimits() -> List[Dict[str, Any]]:
    return [
        {"name": "nofile", "soft": config.DOCKER_NOFILE_LIMIT, "hard": config.DOCKER_NOFILE_LIMIT},
        {"name": "nproc", "soft": max(512, config.DOCKER_DEFAULT_PID_LIMIT),
         "hard": max(512, config.DOCKER_DEFAULT_PID_LIMIT)},
    ]


def build_sysctls() -> Dict[str, str]:
    """
    Namespaced sysctls applied at container creation.

    These replace the old LXC ``apply_internal_permissions`` pass: inside an
    unprivileged container ``/proc/sys`` is read-only, so the values must be
    handed to the runtime instead of being written from inside the guest.
    Only ``net.*`` sysctls that Docker marks as namespaced are allowed.
    """
    return {
        "net.ipv4.ip_unprivileged_port_start": "0",
        "net.ipv4.ping_group_range": "0 2147483647",
        "net.ipv4.ip_local_port_range": "1024 65535",
    }


def cap_drop_list(extra_drop: Optional[Iterable[str]] = None) -> List[str]:
    if not config.DOCKER_DROP_CAPABILITIES:
        return []
    dropped = set(DEFAULT_CAP_DROP)
    if extra_drop:
        dropped.update(str(item).upper() for item in extra_drop)
    return sorted(dropped - CAP_KEEP)


def build_host_config(*, ram_mb: int, cpu: float, pids: int, publish_ip: str,
                      published_ports: Sequence[Tuple[int, int, str]] = (),
                      volumes: Optional[Dict[str, Dict[str, str]]] = None,
                      network: str = "", extra: Optional[Dict[str, Any]] = None,
                      restart_policy: Optional[Dict[str, Any]] = None,
                      kvm_enabled: Optional[bool] = None,
                      apparmor_enabled: Optional[bool] = None) -> Dict[str, Any]:
    """
    Assemble the create-time keyword payload for a customer container.

    The keys are the flat keyword arguments accepted by
    ``docker.DockerClient.containers.create`` (which builds the real
    ``HostConfig`` internally).  ``published_ports`` is a sequence of
    ``(host_port, container_port, protocol)``.
    """
    if config.DOCKER_PRIVILEGED:
        # Allowed only because an administrator explicitly turned it on; we log
        # loudly rather than silently ignoring the request.
        import logging
        logging.getLogger("tgdbot.security").warning(
            "DOCKER_PRIVILEGED is enabled — containers will run with host-level access.")

    mem_limit = max(64, int(ram_mb)) * 1024 * 1024
    mem_reservation = int(mem_limit * max(0.0, min(1.0, config.DOCKER_MEMORY_RESERVATION_RATIO)))

    cpu_count = max(0.0, float(cpu))
    cpu_quota = int(round(cpu_count * config.DOCKER_CPU_PERIOD)) if cpu_count > 0 else 0

    ports: Dict[str, Any] = {}
    for host_port, container_port, protocol in published_ports:
        key = f"{int(container_port)}/{protocol}"
        ports.setdefault(key, []).append((str(publish_ip or "0.0.0.0"), int(host_port)))

    for host_path in (volumes or {}):
        assert_not_forbidden_mount(host_path)

    kwargs: Dict[str, Any] = {
        "mem_limit": mem_limit,
        "mem_reservation": mem_reservation,
        "memswap_limit": mem_limit,          # no swap escape hatch
        "cpu_period": config.DOCKER_CPU_PERIOD if cpu_quota else 0,
        "cpu_quota": cpu_quota,
        "cpu_shares": max(2, int(cpu_count * 1024)) if cpu_count else 1024,
        "pids_limit": max(32, int(pids or config.DOCKER_DEFAULT_PID_LIMIT)),
        "privileged": bool(config.DOCKER_PRIVILEGED),
        "cap_drop": cap_drop_list(),
        "security_opt": build_security_opts(apparmor_enabled),
        "ulimits": build_ulimits(),
        "sysctls": build_sysctls(),
        "init": bool(config.DOCKER_CONTAINER_INIT),
        "publish_all_ports": False,
        "ports": ports,
        "devices": device_mappings(kvm_enabled),
        "device_requests": [],
        "ipc_mode": "private",
        "network_mode": network or config.DOCKER_DEFAULT_NETWORK,
        "restart_policy": restart_policy or {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "auto_remove": False,
        "read_only": False,
    }
    if config.DOCKER_USERNS_MODE:
        kwargs["userns_mode"] = config.DOCKER_USERNS_MODE
    if extra:
        kwargs.update(extra)
    if kvm_enabled is False:
        # Privileged mode can expose host devices without an explicit mapping;
        # never let it silently defeat the KVM-disabled contract.
        if kwargs.get("privileged"):
            raise SecurityPolicyError(
                "KVM-disabled VPS creation is incompatible with privileged Docker mode.")
        if any("/dev/kvm" in str(device) for device in (kwargs.get("devices") or [])):
            raise SecurityPolicyError("A KVM-disabled VPS cannot receive /dev/kvm.")
    return kwargs



def describe_policy() -> Dict[str, Any]:
    """Human-readable snapshot of the active policy (shown by ``!node status``)."""
    return {
        "privileged": bool(config.DOCKER_PRIVILEGED),
        "no_new_privileges": bool(config.DOCKER_NO_NEW_PRIVILEGES),
        "capabilities_dropped": cap_drop_list(),
        "pids_limit": int(config.DOCKER_DEFAULT_PID_LIMIT),
        "container_init": bool(config.DOCKER_CONTAINER_INIT),
        "swap": "disabled (memswap == memory limit)",
        "docker_socket_exposed": False,
        "host_namespaces_shared": False,
        "sysctls": build_sysctls(),
    }
