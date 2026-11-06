"""
Central configuration for the TGD Bot Docker backend.

Everything is read from the environment (``.env`` is loaded by the caller) so a
single ``.env`` drives both the Discord bot and the infrastructure layer.

Every name here is documented in ``.env.example``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# ── env helpers (identical semantics to the ones in bot.py) ─────────────────


def env(key: str, default: Any = None) -> Optional[str]:
    val = os.getenv(key)
    if val is None:
        return default
    val = val.strip()
    return val if val != "" else default


def env_int(key: str, default: int) -> int:
    try:
        return int(str(env(key, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(str(env(key, default)).strip())
    except (TypeError, ValueError):
        return default


def env_bool(key: str, default: bool) -> bool:
    val = env(key, None)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on", "enabled")


def env_path(key: str, default: str) -> Path:
    return Path(str(env(key, default))).expanduser()


# ── Docker Engine ───────────────────────────────────────────────────────────

#: Docker socket / endpoint used for the *local* node.
DOCKER_HOST_URL: str = str(env("DOCKER_HOST_URL", env("DOCKER_HOST", "unix:///var/run/docker.sock")))
DOCKER_API_VERSION: Optional[str] = env("DOCKER_API_VERSION", None) or None
DOCKER_API_TIMEOUT: int = env_int("DOCKER_API_TIMEOUT", 30)
DOCKER_IMAGE_PULL_TIMEOUT: int = env_int("DOCKER_IMAGE_PULL_TIMEOUT", 600)
DOCKER_OPERATION_TIMEOUT: int = env_int("DOCKER_OPERATION_TIMEOUT", 180)
DOCKER_START_TIMEOUT: int = env_int("DOCKER_START_TIMEOUT", 120)
DOCKER_STOP_TIMEOUT: int = env_int("DOCKER_STOP_TIMEOUT", 60)
DOCKER_EXEC_TIMEOUT: int = env_int("DOCKER_EXEC_TIMEOUT", 120)
DOCKER_MAX_RETRIES: int = env_int("DOCKER_MAX_RETRIES", 3)
DOCKER_RETRY_BACKOFF: float = env_float("DOCKER_RETRY_BACKOFF", 1.5)

# Concurrency guards — unlimited parallel pulls/creates will melt a node.
DOCKER_MAX_CONCURRENT_PULLS: int = max(1, env_int("DOCKER_MAX_CONCURRENT_PULLS", 2))
DOCKER_MAX_CONCURRENT_CREATES: int = max(1, env_int("DOCKER_MAX_CONCURRENT_CREATES", 2))
DOCKER_WORKER_THREADS: int = max(2, env_int("DOCKER_WORKER_THREADS", 8))

# ── Images ──────────────────────────────────────────────────────────────────

DOCKER_DEFAULT_IMAGE: str = str(env("DOCKER_DEFAULT_IMAGE", "ubuntu:24.04"))
DOCKER_BACKUP_HELPER_IMAGE: str = str(env("DOCKER_BACKUP_HELPER_IMAGE", "alpine:3.20"))
#: Proxmox VE is a separate, grant-gated workload — never a public SSH image.
#: The tag follows dockur/proxmox's Docker tag (repository releases use a `v`
#: prefix, Docker image tags do not). Pinning avoids surprise major upgrades.
PROXMOX_IMAGE: str = str(env("PROXMOX_IMAGE", "dockurr/proxmox:9.2.20"))
PROXMOX_CONTAINER_PORT: int = 8006
PROXMOX_MIN_RAM_MB: int = 2048
PROXMOX_MIN_STORAGE_GB: int = 32
PROXMOX_REQUIRE_KVM: bool = env_bool("PROXMOX_REQUIRE_KVM", True)
PROXMOX_REQUIRE_FUSE: bool = env_bool("PROXMOX_REQUIRE_FUSE", True)
PROXMOX_READY_ATTEMPTS: int = env_int("PROXMOX_READY_ATTEMPTS", 90)
PROXMOX_READY_INTERVAL: float = env_float("PROXMOX_READY_INTERVAL", 2.0)

# ── Native Proxmox browser console ─────────────────────────────────────────
#: This is separate from ordinary VPS WebSSH. It proxies Proxmox's native UI,
#: API and noVNC WebSockets, rather than opening a text terminal in the guest.
#: Both the listener and the upstream default to loopback. A remote browser
#: needs an SSH tunnel (or an explicitly configured secure reverse proxy).
PROXMOX_CONSOLE_ENABLED: bool = env_bool("PROXMOX_CONSOLE_ENABLED", True)
PROXMOX_CONSOLE_HOST: str = str(env("PROXMOX_CONSOLE_HOST", "127.0.0.1"))
PROXMOX_CONSOLE_PORT: int = env_int("PROXMOX_CONSOLE_PORT", 5001)
PROXMOX_CONSOLE_PUBLIC_HOST: str = str(env("PROXMOX_CONSOLE_PUBLIC_HOST", "127.0.0.1"))
PROXMOX_CONSOLE_URL_FORMAT: str = str(env("PROXMOX_CONSOLE_URL_FORMAT", "http://{SERVER_IP}:{PORT}"))
PROXMOX_CONSOLE_UPSTREAM_HOST: str = str(env("PROXMOX_CONSOLE_UPSTREAM_HOST", "127.0.0.1"))
PROXMOX_CONSOLE_SESSION_TTL: int = max(1, env_int("PROXMOX_CONSOLE_SESSION_TTL", 1800))
PROXMOX_CONSOLE_MAX_SESSIONS: int = max(1, env_int("PROXMOX_CONSOLE_MAX_SESSIONS", 200))
#: Allow an administrator-approved custom image to be used for new VPS.
DOCKER_ALLOW_CUSTOM_IMAGES: bool = env_bool("DOCKER_ALLOW_CUSTOM_IMAGES", True)
#: Always `docker pull` before create, even when a local copy exists.
DOCKER_ALWAYS_PULL: bool = env_bool("DOCKER_ALWAYS_PULL", False)

# ── Networking / ports ──────────────────────────────────────────────────────

DOCKER_DEFAULT_NETWORK: str = str(env("DOCKER_DEFAULT_NETWORK", "bridge"))
DOCKER_CREATE_NETWORK: bool = env_bool("DOCKER_CREATE_NETWORK", True)
DOCKER_NETWORK_SUBNET: str = str(env("DOCKER_NETWORK_SUBNET", ""))

#: Address the published ports bind to on the host.  Use 0.0.0.0 to publish on
#: every interface, or a specific address to keep VPS traffic on one NIC.
DOCKER_PUBLISH_IP: str = str(env("DOCKER_PUBLISH_IP", "0.0.0.0"))

DOCKER_SSH_PORT_START: int = env_int("DOCKER_SSH_PORT_START", 22000)
DOCKER_SSH_PORT_END: int = env_int("DOCKER_SSH_PORT_END", 22999)
DOCKER_PORT_RANGE_START: int = env_int("DOCKER_PORT_RANGE_START", 25000)
DOCKER_PORT_RANGE_END: int = env_int("DOCKER_PORT_RANGE_END", 26999)
#: Extra ports never handed out (e.g. the node agent).
DOCKER_RESERVED_PORTS: List[int] = [
    int(p) for p in re.findall(r"\d+", str(env("DOCKER_RESERVED_PORTS", "")))
]

# ── Default plan / resources ────────────────────────────────────────────────

DOCKER_DEFAULT_RAM: int = env_int("DOCKER_DEFAULT_RAM", 1024)        # MiB
DOCKER_DEFAULT_CPU: float = env_float("DOCKER_DEFAULT_CPU", 1.0)     # vCPU
DOCKER_DEFAULT_STORAGE: int = env_int("DOCKER_DEFAULT_STORAGE", 10)  # GiB
DOCKER_DEFAULT_PID_LIMIT: int = env_int("DOCKER_DEFAULT_PID_LIMIT", 512)
DOCKER_CPU_PERIOD: int = env_int("DOCKER_CPU_PERIOD", 100_000)
DOCKER_MEMORY_RESERVATION_RATIO: float = env_float("DOCKER_MEMORY_RESERVATION_RATIO", 0.75)
DOCKER_NOFILE_LIMIT: int = env_int("DOCKER_NOFILE_LIMIT", 65536)

# ── Storage ─────────────────────────────────────────────────────────────────

DOCKER_VPS_DATA_DIR: Path = env_path("DOCKER_VPS_DATA_DIR", "/var/lib/tgdbot/vps")
DOCKER_BACKUP_DIR: Path = env_path("DOCKER_BACKUP_DIR", "/var/backups/tgdbot")
DOCKER_RUNTIME_DIR: Path = env_path("DOCKER_RUNTIME_DIR", "/var/lib/tgdbot/runtime")
DOCKER_SECRET_DIR: Path = env_path("DOCKER_SECRET_DIR", "/var/lib/tgdbot/secrets")
DOCKER_VOLUME_PREFIX: str = str(env("DOCKER_VOLUME_PREFIX", "tgdbot_"))
#: Apply real host-level quotas (XFS project quota / btrfs qgroup) when possible.
DOCKER_ENFORCE_DISK_QUOTA: bool = env_bool("DOCKER_ENFORCE_DISK_QUOTA", True)
DOCKER_BACKUP_KEEP: int = max(1, env_int("DOCKER_BACKUP_KEEP", 4))
DOCKER_BACKUP_RETENTION_DAYS: int = env_int("DOCKER_BACKUP_RETENTION_DAYS", 30)
DOCKER_BACKUP_INTERVAL_HOURS: int = env_int("DOCKER_BACKUP_INTERVAL_HOURS", 0)  # 0 = manual only

# ── Security policy ─────────────────────────────────────────────────────────

#: Never enabled by default: privileged containers are host-equivalent.
DOCKER_PRIVILEGED: bool = env_bool("DOCKER_PRIVILEGED", False)
#: `no-new-privileges` breaks su/sudo and PAM-backed sshd root logins, so it is
#: opt-in.  Enable it only if your guests never escalate privileges.
DOCKER_NO_NEW_PRIVILEGES: bool = env_bool("DOCKER_NO_NEW_PRIVILEGES", False)
DOCKER_DROP_CAPABILITIES: bool = env_bool("DOCKER_DROP_CAPABILITIES", True)
DOCKER_CONTAINER_INIT: bool = env_bool("DOCKER_CONTAINER_INIT", True)
DOCKER_SECCOMP_PROFILE: str = str(env("DOCKER_SECCOMP_PROFILE", ""))  # "" = Docker default
DOCKER_APPARMOR_PROFILE: str = str(env("DOCKER_APPARMOR_PROFILE", ""))
DOCKER_USERNS_MODE: str = str(env("DOCKER_USERNS_MODE", ""))

# ── Monitoring ──────────────────────────────────────────────────────────────

DOCKER_STATS_TIMEOUT: int = env_int("DOCKER_STATS_TIMEOUT", 15)
DOCKER_STATS_MAX_PARALLEL: int = max(1, env_int("DOCKER_STATS_MAX_PARALLEL", 8))
HOST_SNAPSHOT_INTERVAL: int = max(15, env_int("HOST_SNAPSHOT_INTERVAL", 60))

# ── Container labels ────────────────────────────────────────────────────────

LABEL_MANAGED: str = "tgdbot.managed"
LABEL_CONTAINER: str = "tgdbot.container"
LABEL_VPS_ID: str = "tgdbot.vps-id"
LABEL_OWNER: str = "tgdbot.owner"
LABEL_NODE: str = "tgdbot.node"
LABEL_IMAGE: str = "tgdbot.image"
LABEL_ROLE: str = "tgdbot.role"
LABEL_WORKLOAD: str = "tgdbot.workload"
WORKLOAD_VPS: str = "vps"
WORKLOAD_PROXMOX: str = "proxmox"
ROLE_VPS: str = "vps"
ROLE_BACKUP_HELPER: str = "backup-helper"

MANAGED_LABEL_FILTER: Dict[str, str] = {LABEL_MANAGED: "true"}

#: Pre-rebrand identifiers.  Docker labels are immutable after creation and
#: volumes cannot be renamed, so containers and volumes created under the old
#: brand must keep working.  Discovery matches both sets; nothing is rewritten
#: behind the operator's back.  See MIGRATION.md ("Rebranding").
LEGACY_LABEL_MANAGED: str = "unixnodes.managed"
LEGACY_LABEL_CONTAINER: str = "unixnodes.container"
LEGACY_LABEL_VPS_ID: str = "unixnodes.vps-id"
LEGACY_LABEL_OWNER: str = "unixnodes.owner"
LEGACY_LABEL_NODE: str = "unixnodes.node"
LEGACY_LABEL_IMAGE: str = "unixnodes.image"
LEGACY_LABEL_ROLE: str = "unixnodes.role"
LEGACY_VOLUME_PREFIX: str = "unixnodes_"

#: Every label key, current and legacy, so lookups try both.
LABEL_ALIASES: Dict[str, tuple] = {
    LABEL_MANAGED: (LABEL_MANAGED, LEGACY_LABEL_MANAGED),
    LABEL_CONTAINER: (LABEL_CONTAINER, LEGACY_LABEL_CONTAINER),
    LABEL_VPS_ID: (LABEL_VPS_ID, LEGACY_LABEL_VPS_ID),
    LABEL_OWNER: (LABEL_OWNER, LEGACY_LABEL_OWNER),
    LABEL_NODE: (LABEL_NODE, LEGACY_LABEL_NODE),
    LABEL_IMAGE: (LABEL_IMAGE, LEGACY_LABEL_IMAGE),
    LABEL_ROLE: (LABEL_ROLE, LEGACY_LABEL_ROLE),
}


def label_value(labels: Dict[str, str], key: str, default: str = "") -> str:
    """Read a label, falling back to its pre-rebrand spelling."""
    if not labels:
        return default
    for candidate in LABEL_ALIASES.get(key, (key,)):
        value = labels.get(candidate)
        if value not in (None, ""):
            return str(value)
    return default

# ── Runtime bootstrap ───────────────────────────────────────────────────────

#: Read-only bind mount target for the init/supervisor script.
INIT_SCRIPT_TARGET: str = "/usr/local/sbin/tgdbot-init"
#: Read-only bind mount target for the first-boot root credential.
CREDENTIAL_TARGET: str = "/run/tgdbot-root.credential"

SSH_CONTAINER_PORT: int = env_int("SSH_CONTAINER_PORT", 22)
SSH_READY_ATTEMPTS: int = env_int("SSH_READY_ATTEMPTS", 30)
SSH_READY_INTERVAL: float = env_float("SSH_READY_INTERVAL", 1.0)
SSH_VERIFY_LOGIN: bool = env_bool("SSH_VERIFY_LOGIN", True)

# Commands executed inside the guest right after SSH is up (branding etc.).
HOST_MOTD: str = str(env("HOST_MOTD", ""))

# ── Database ────────────────────────────────────────────────────────────────

# The v10 bot used vps.db. Keep that as the canonical default so the
# Discord UI and Docker services never initialize separate databases.
DATABASE_PATH: Path = env_path("DATABASE_PATH", str(BASE_DIR / "vps.db"))
DATABASE_BACKUP_DIR: Path = env_path("DATABASE_BACKUP_DIR", str(BASE_DIR / "backups"))
MIGRATION_REPORT_DIR: Path = env_path("MIGRATION_REPORT_DIR", str(BASE_DIR / "migration_reports"))

SCHEMA_VERSION = 13


# ── Image catalogue ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ImageSpec:
    """One selectable operating system."""

    label: str
    image: str
    emoji: str
    family: str          # debian | alpine | rhel
    description: str = ""
    features: List[str] = field(default_factory=list)
    #: ``vps`` uses the SSH supervisor; ``proxmox`` is a separately gated
    #: privileged systemd workload with its own persistence and web console.
    kind: str = "vps"

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.image.lower()).strip("-")

    def as_option(self) -> Dict[str, str]:
        return {"label": self.label, "value": self.image, "emoji": self.emoji}


#: The OS list offered in every Discord dropdown.  ``value`` is the Docker image
#: reference, so the same identifier is stored in ``vps.os_version``.
DEFAULT_IMAGES: List[ImageSpec] = [
    ImageSpec("Proxmox VE 9.2 (grant required)", PROXMOX_IMAGE, "🛡️", "proxmox",
              "Proxmox VE web console — admins and explicitly granted users only",
              ["Proxmox VE", "Web UI :8006", "KVM"], kind="proxmox"),
    ImageSpec("Ubuntu 24.04 LTS (Noble)", "ubuntu:24.04", "🟠", "debian",
              "Latest LTS Ubuntu — recommended", ["SSH", "APT", "Cron"]),
    ImageSpec("Ubuntu 22.04 LTS (Jammy)", "ubuntu:22.04", "🟠", "debian",
              "Previous LTS with long support", ["SSH", "APT", "Cron"]),
    ImageSpec("Ubuntu 20.04 LTS (Focal)", "ubuntu:20.04", "🟠", "debian",
              "Older LTS for legacy software", ["SSH", "APT", "Cron"]),
    ImageSpec("Debian 13 (Trixie)", "debian:13", "🔴", "debian",
              "Current Debian stable", ["SSH", "APT", "Lightweight"]),
    ImageSpec("Debian 12 (Bookworm)", "debian:12", "🔴", "debian",
              "Rock solid Debian release", ["SSH", "APT", "Lightweight"]),
    ImageSpec("Debian 11 (Bullseye)", "debian:11", "🔴", "debian",
              "Proven Debian stable", ["SSH", "APT", "Lightweight"]),
    ImageSpec("Alpine Linux 3.20", "alpine:3.20", "🏔️", "alpine",
              "Ultra lightweight (~8 MB)", ["SSH", "APK", "Fast boot"]),
]

IMAGE_BY_REF: Dict[str, ImageSpec] = {spec.image: spec for spec in DEFAULT_IMAGES}


def family_for(image_ref: str) -> str:
    """Return the package-manager family for an arbitrary image reference."""
    ref = (image_ref or "").lower()
    spec = IMAGE_BY_REF.get(ref)
    if spec:
        return spec.family
    if "alpine" in ref:
        return "alpine"
    if any(token in ref for token in ("centos", "rocky", "almalinux", "fedora", "rhel", "oraclelinux")):
        return "rhel"
    return "debian"


def image_label(image_ref: str) -> str:
    spec = IMAGE_BY_REF.get(image_ref)
    return spec.label if spec else image_ref


def image_emoji(image_ref: str) -> str:
    spec = IMAGE_BY_REF.get(image_ref)
    return spec.emoji if spec else "🐳"


# ── Resource plans ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResourcePlan:
    """An administrator-configurable bundle of limits."""

    name: str
    ram_mb: int
    cpu: float
    storage_gb: int
    pids: int = 0
    description: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ram_mb": int(self.ram_mb),
            "cpu": float(self.cpu),
            "storage_gb": int(self.storage_gb),
            "pids": int(self.pids or DOCKER_DEFAULT_PID_LIMIT),
            "description": self.description,
        }


DEFAULT_PLANS: List[ResourcePlan] = [
    ResourcePlan("Starter", 1024, 1.0, 10, 256, "1 GB RAM • 1 vCPU • 10 GB disk"),
    ResourcePlan("Basic", 2048, 1.0, 20, 384, "2 GB RAM • 1 vCPU • 20 GB disk"),
    ResourcePlan("Standard", 4096, 2.0, 40, 512, "4 GB RAM • 2 vCPU • 40 GB disk"),
    ResourcePlan("Performance", 8192, 4.0, 80, 768, "8 GB RAM • 4 vCPU • 80 GB disk"),
]


def describe() -> Dict[str, Any]:
    """Snapshot of the effective infrastructure configuration (safe to log)."""
    return {
        "docker_host": DOCKER_HOST_URL,
        "api_timeout": DOCKER_API_TIMEOUT,
        "default_image": DOCKER_DEFAULT_IMAGE,
        "network": DOCKER_DEFAULT_NETWORK,
        "publish_ip": DOCKER_PUBLISH_IP,
        "ssh_port_range": [DOCKER_SSH_PORT_START, DOCKER_SSH_PORT_END],
        "user_port_range": [DOCKER_PORT_RANGE_START, DOCKER_PORT_RANGE_END],
        "default_ram_mb": DOCKER_DEFAULT_RAM,
        "default_cpu": DOCKER_DEFAULT_CPU,
        "default_storage_gb": DOCKER_DEFAULT_STORAGE,
        "default_pid_limit": DOCKER_DEFAULT_PID_LIMIT,
        "data_dir": str(DOCKER_VPS_DATA_DIR),
        "backup_dir": str(DOCKER_BACKUP_DIR),
        "privileged": DOCKER_PRIVILEGED,
        "no_new_privileges": DOCKER_NO_NEW_PRIVILEGES,
        "enforce_disk_quota": DOCKER_ENFORCE_DISK_QUOTA,
        "images": [spec.image for spec in DEFAULT_IMAGES],
    }
