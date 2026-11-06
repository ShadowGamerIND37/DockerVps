"""
Transactional VPS provisioning.

The engine walks the full deployment pipeline and, if *any* step fails, rolls
back everything it created: the container is removed, the volume is deleted
(a failed first boot has no customer data in it), the reserved ports are
released and the partial database row is dropped.  Nothing is left behind that
``!delete-useless-vps`` would have to clean up.

All functions are synchronous; the Discord layer calls them through
:func:`tgdbot.concurrency.run_in_thread`.
"""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config
from ..concurrency import THREAD_LIMITS
from ..docker_backend import containers as ctr
from ..docker_backend import images as images_mod
from ..docker_backend import monitoring, nodes as nodes_mod, resources, security, ssh, volumes
from ..docker_backend.client import POOL
from ..docker_backend.ports import PURPOSE_SSH, PURPOSE_USER, PortAllocator
from ..errors import ProvisioningError, TgdBotError
from ..database import models

logger = logging.getLogger("tgdbot.services.provisioning")

Progress = Callable[[int, str, str], None]   # (step index, state, detail)

STEPS: Tuple[str, ...] = (
    "Validate the plan",                 # 0
    "Select a Docker node",              # 1
    "Check node resources",              # 2
    "Reserve the SSH host port",         # 3
    "Reserve the VPS identity",          # 4
    "Pull the operating system image",   # 5
    "Create the persistent volume",      # 6
    "Create the Docker container",       # 7
    "Apply CPU / RAM / PID limits",      # 8
    "Attach networking",                 # 9
    "Publish the SSH port",              # 10
    "Start the container",               # 11
    "Initialize the Linux environment",  # 12
    "Configure SSH access",              # 13
    "Verify container health",           # 14
    "Verify SSH connectivity",           # 15
    "Apply the storage quota",           # 16
    "Register in the database",          # 17
)


@dataclass
class ProvisionResult:
    container_name: str
    vps_id: int
    node_id: int
    node_name: str
    image: str
    image_label: str
    ram_mb: int
    cpu: float
    storage_gb: int
    pids: int
    ssh_host_port: int
    publish_ip: str
    password: str
    volume_name: str
    container_id: str
    quota_enforced: bool
    quota_method: str
    quota_detail: str
    ssh_verified: bool
    ssh_note: str
    record: Dict[str, Any] = field(default_factory=dict)
    steps: List[Dict[str, str]] = field(default_factory=list)

    def ssh_command(self, host: str) -> str:
        return f"ssh root@{host} -p {self.ssh_host_port}"


@dataclass
class ProvisionRequest:
    owner_id: Any
    container_name: str
    image: str
    ram_mb: int
    cpu: float
    storage_gb: int
    node_id: Optional[int] = None
    pids: int = 0
    expiry_days: Optional[int] = None
    is_public: bool = False
    password: Optional[str] = None
    extra_ports: List[Tuple[int, str]] = field(default_factory=list)
    host_motd: str = ""


def generate_password(length: int = 20) -> str:
    """Cryptographically random password covering every character class."""
    alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(max(16, length)))
        if (any(c.islower() for c in candidate) and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)
                and any(c in "!@#%^*-_=+" for c in candidate)):
            return candidate


def verification_host(node: Dict[str, Any], publish_ip: str) -> str:
    """
    Address the bot should dial to verify a freshly published SSH port.

    * local node + wildcard bind → loopback;
    * local node + specific bind → that address;
    * remote node                → the node's Docker endpoint host.
    """
    if publish_ip and publish_ip not in ("0.0.0.0", "::", ""):
        return publish_ip
    if node.get("is_local"):
        return "127.0.0.1"
    from ..docker_backend.client import parse_ssh_target

    endpoint = str(node.get("docker_host") or node.get("url") or "").strip()
    if endpoint.lower().startswith("ssh://") or "@" in endpoint:
        return parse_ssh_target(endpoint)[2] or "127.0.0.1"
    host = endpoint.split("//")[-1].split("/")[0].split(":")[0]
    return host or "127.0.0.1"


class Provisioner:
    """Owns one provisioning run."""

    def __init__(self, repo: Any, request: ProvisionRequest, *,
                 progress: Optional[Progress] = None) -> None:
        self.repo = repo
        self.request = request
        self.progress = progress
        self.cleanup: List[str] = []
        self.cleanup_errors: List[str] = []
        self.step_log: List[Dict[str, str]] = []
        self.client: Any = None
        self.node: Dict[str, Any] = {}
        self.allocator: Optional[PortAllocator] = None
        self.reservations: List[Any] = []
        self.container_created = False
        self.volume_created = False
        self.row_inserted = False

    # ── helpers ─────────────────────────────────────────────────────────────
    def _step(self, index: int, state: str, detail: str = "") -> None:
        self.step_log.append({"index": str(index), "state": state, "detail": detail})
        logger.info("provision %s [%d/%d] %s %s", self.request.container_name, index + 1,
                    len(STEPS), state, detail)
        if self.progress:
            try:
                self.progress(index, state, detail)
            except Exception:  # never let the UI break provisioning
                logger.debug("progress callback failed", exc_info=True)

    def _fail(self, index: int, exc: BaseException) -> ProvisioningError:
        self._step(index, "failed", str(exc)[:200])
        return ProvisioningError(
            f"Provisioning failed at step {index + 1} ({STEPS[index]}): {exc}",
            detail=str(exc),
            cleanup=self.cleanup, cleanup_errors=self.cleanup_errors)

    # ── main pipeline ───────────────────────────────────────────────────────
    def run(self) -> ProvisionResult:
        request = self.request
        container = request.container_name

        ok, reason = security.validate_container_name(container)
        if not ok:
            raise ProvisioningError(reason, user_message=reason)
        if self.repo.get_vps_by_container(container):
            raise ProvisioningError(f"A VPS named {container} already exists")

        # 0 — plan validation ------------------------------------------------
        self._step(0, "running")
        # A zero or negative value is a caller bug, not "use the default":
        # silently substituting the default would hand out a VPS nobody asked
        # for.  Reject it instead.  `None` still means "unspecified".
        for label, value, minimum in (("RAM", request.ram_mb, 128),
                                      ("CPU", request.cpu, 0.1),
                                      ("storage", request.storage_gb, 1)):
            if value is not None and float(value) < minimum:
                raise self._fail(0, ProvisioningError(
                    f"{label} must be at least {minimum:g}, got {value}",
                    user_message=f"The requested {label.lower()} is too small "
                                 f"(minimum {minimum:g})."))
        ram_mb = int(request.ram_mb if request.ram_mb else config.DOCKER_DEFAULT_RAM)
        cpu = float(request.cpu if request.cpu else config.DOCKER_DEFAULT_CPU)
        storage_gb = int(request.storage_gb if request.storage_gb
                         else config.DOCKER_DEFAULT_STORAGE)
        pids = int(request.pids or config.DOCKER_DEFAULT_PID_LIMIT)
        try:
            image_entry = images_mod.resolve(
                request.image, self.repo.list_approved_images())
        except TgdBotError as exc:
            raise self._fail(0, exc) from exc
        image_ref = image_entry["value"]
        self._step(0, "done", f"{image_ref} • {ram_mb} MiB • {cpu:g} vCPU • {storage_gb} GB")

        # 1 — node selection -------------------------------------------------
        self._step(1, "running")
        node = nodes_mod.pick_node(self.repo, ram_gb=ram_mb / 1024.0, cpu=cpu,
                                   preferred_id=request.node_id)
        if not node:
            raise self._fail(1, ProvisioningError(
                "No node has enough capacity for this plan",
                user_message="No node can host a VPS of this size right now."))
        self.node = node
        try:
            self.client = nodes_mod.require_client(node)
        except TgdBotError as exc:
            raise self._fail(1, exc) from exc
        self.allocator = PortAllocator(self.repo, int(node["id"]),
                                       str(node.get("publish_ip") or config.DOCKER_PUBLISH_IP))
        self._step(1, "done", f"{node.get('name')} (id {node['id']})")

        # 2 — node resource check -------------------------------------------
        self._step(2, "running")
        has_room, why = nodes_mod.has_room(node, self.repo, ram_gb=ram_mb / 1024.0, cpu=cpu)
        if not has_room:
            raise self._fail(2, ProvisioningError(why, user_message=why))
        try:
            images_mod.ensure_image  # attribute check keeps the import honest
            monitoring.engine_healthy(self.client)
        except Exception as exc:
            raise self._fail(2, exc) from exc
        self._step(2, "done", "capacity available")

        # 3 — reserve the SSH port ------------------------------------------
        self._step(3, "running")
        try:
            self.reservations = self.allocator.reserve(
                container, config.SSH_CONTAINER_PORT, purpose=PURPOSE_SSH,
                protocol="tcp", user_id=request.owner_id)
            for host_port, container_port, protocol in request.extra_ports:
                self.reservations.extend(self.allocator.reserve(
                    container, int(container_port), purpose=PURPOSE_USER,
                    protocol=str(protocol), user_id=request.owner_id))
        except TgdBotError as exc:
            raise self._fail(3, exc) from exc
        ssh_host_port = self.allocator.ssh_port(container) or 0
        self._step(3, "done", f"host port {ssh_host_port}")

        # 4 — identity -------------------------------------------------------
        self._step(4, "running")
        vps_id = self.repo.next_vps_id()
        password = request.password or generate_password()
        self._step(4, "done", f"{container} • VPS #{vps_id}")

        # 5 — image ----------------------------------------------------------
        self._step(5, "running")

        def on_progress(message: str) -> None:
            self._step(5, "running", message[:80])

        try:
            self._with_guard("image_pull", images_mod.ensure_image, self.client, image_ref,
                             progress=on_progress)
        except TgdBotError as exc:
            raise self._fail(5, exc) from exc
        self._step(5, "done", image_ref)

        # 6 — volume ---------------------------------------------------------
        self._step(6, "running")
        try:
            ssh.ensure_runtime_assets()
            volumes.create(self.client, container, labels={
                config.LABEL_MANAGED: "true",
                config.LABEL_CONTAINER: container,
                config.LABEL_OWNER: str(request.owner_id),
                config.LABEL_VPS_ID: str(vps_id),
                config.LABEL_NODE: str(node["id"]),
            })
            self.volume_created = True
        except TgdBotError as exc:
            raise self._fail(6, exc) from exc
        self._step(6, "done", volumes.volume_name(container))

        # 7 — container (+8 limits, +9 network, +10 ports: all set at create)
        self._step(7, "running")
        ssh.write_credential(container, password)
        if request.host_motd:
            ssh.write_hook(container, request.host_motd)
        binds = volumes.mounts_for(container, extra_binds=ssh.bind_mounts(container))
        publish_spec = self.allocator.publish_spec(container)
        try:
            spec = ctr.build_container_spec(
                container_name=container, image=image_ref, ram_mb=ram_mb, cpu=cpu, pids=pids,
                publish_ip=self.allocator.publish_ip, published_ports=publish_spec,
                volumes=binds,
                labels={config.LABEL_VPS_ID: str(vps_id),
                        config.LABEL_OWNER: str(request.owner_id),
                        config.LABEL_NODE: str(node["id"]),
                        config.LABEL_IMAGE: image_ref},
                command=ssh.container_command(),
                hostname=container[:63],
            )
            container_obj = self._with_guard("container_create", ctr.create, self.client, spec)
            self.container_created = True
        except TgdBotError as exc:
            raise self._fail(7, exc) from exc
        container_id = str(container_obj.short_id)
        self._step(7, "done", f"container {container_id}")

        limits = resources.limits_for(ram_mb, cpu, pids)
        self._step(8, "done", f"RAM {ram_mb} MiB • CPU quota "
                              f"{limits['cpu_quota']}/{limits['cpu_period']} • {limits['pids_limit']} PIDs")
        self._step(9, "done", f"network `{spec.get('network_mode')}`")
        self._step(10, "done", f"{len(publish_spec)} port(s) published")

        # 11 — start ---------------------------------------------------------
        self._step(11, "running")
        try:
            ctr.start(self.client, container)
        except TgdBotError as exc:
            raise self._fail(11, exc) from exc
        self._step(11, "done", "running")

        # 12 — Linux initialization -----------------------------------------
        self._step(12, "running")
        report: Dict[str, Any] = {}
        for attempt in range(6):
            report = ssh.guest_report(self.client, container)
            if report.get("SSHD") == "yes" or report.get("OS"):
                break
        self._step(12, "done",
                   f"{report.get('OS') or 'unknown OS'} • kernel {report.get('KERNEL') or '?'}")

        # 13 — SSH -----------------------------------------------------------
        self._step(13, "running")
        if not ssh.wait_ready(self.client, container):
            logs = ctr.logs(self.client, container, tail=40)
            raise self._fail(13, ProvisioningError(
                "sshd did not start inside the container", detail=logs,
                user_message="SSH could not be started in the new VPS."))
        ok, note = ssh.ensure_ssh_running(self.client, container)
        if not ok:
            raise self._fail(13, ProvisioningError(note, user_message=note))
        self._step(13, "done", "sshd listening on port 22")

        # 14 — health --------------------------------------------------------
        self._step(14, "running")
        if not ctr.wait_running(self.client, container, timeout=60):
            raise self._fail(14, ProvisioningError("container is not running after start"))
        problems = self.allocator.verify_bindings(container_obj, container)
        if problems:
            raise self._fail(14, ProvisioningError(
                "Docker did not bind every requested port: " + "; ".join(problems),
                user_message="The requested ports could not be published on this node."))
        try:
            self.allocator.mark_bound(container)
        except Exception as exc:
            logger.warning("could not mark ports bound for %s: %s", container, exc)
        self._step(14, "done", "healthy • ports verified")

        # 15 — SSH connectivity from the host -------------------------------
        self._step(15, "running")
        ssh_ok, ssh_note = ssh.verify_access(
            verification_host(self.node, self.allocator.publish_ip),
            ssh_host_port, password)
        self._step(15, "done" if ssh_ok else "failed", ssh_note[:80])
        if not ssh_ok:
            raise self._fail(15, ProvisioningError(
                f"SSH verification failed: {ssh_note}", user_message=ssh_note))

        # 16 — storage quota -------------------------------------------------
        self._step(16, "running")
        quota = self._apply_quota(container, storage_gb)
        self._step(16, "done" if quota["enforced"] else "skipped", quota["detail"][:80])

        # 17 — database ------------------------------------------------------
        self._step(17, "running")
        expiry = None
        if request.expiry_days and request.expiry_days > 0:
            expiry = (datetime.now(timezone.utc)
                      + timedelta(days=int(request.expiry_days))).isoformat()
        record = {
            "id": vps_id,
            "user_id": str(request.owner_id),
            "node_id": int(node["id"]),
            "container_name": container,
            "ram": f"{ram_mb}MB",
            "cpu": f"{cpu:g}",
            "storage": f"{storage_gb}GB",
            "config": f"{ram_mb}MB RAM / {cpu:g} vCPU / {storage_gb}GB Disk",
            "os_version": image_ref,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expiration_date": expiry,
            "root_password": None,      # the caller stores the encrypted value
            "backend": models.DOCKER_BACKEND,
            "docker_container_id": container_id,
            "docker_image": image_ref,
            "volume_name": volumes.volume_name(container),
            "ssh_host_port": ssh_host_port,
            "pid_limit": limits["pids_limit"],
            "quota_method": quota["method"],
            "quota_enforced": quota["enforced"],
            "quota_project_id": quota["project_id"],
            "provision_state": "active",
            "last_health_ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.repo.insert_vps(record)
            self.row_inserted = True
        except Exception as exc:
            raise self._fail(17, exc) from exc
        self._step(17, "done", f"VPS #{vps_id} saved")

        return ProvisionResult(
            container_name=container, vps_id=vps_id, node_id=int(node["id"]),
            node_name=str(node.get("name")), image=image_ref,
            image_label=str(image_entry.get("label") or image_ref),
            ram_mb=ram_mb, cpu=cpu, storage_gb=storage_gb, pids=limits["pids_limit"],
            ssh_host_port=ssh_host_port, publish_ip=self.allocator.publish_ip,
            password=password, volume_name=volumes.volume_name(container),
            container_id=container_id,
            quota_enforced=bool(quota["enforced"]), quota_method=str(quota["method"]),
            quota_detail=str(quota["detail"]),
            ssh_verified=ssh_ok, ssh_note=ssh_note,
            record=record, steps=self.step_log,
        )

    # ── quota ───────────────────────────────────────────────────────────────
    def _apply_quota(self, container: str, storage_gb: int) -> Dict[str, Any]:
        mountpoint = volumes.mountpoint(self.client, container) or ""
        support = resources.detect_quota_support(mountpoint,
                                                 node_is_local=bool(self.node.get("is_local")))
        project_id = resources.suggested_project_id(volumes.volume_name(container))
        enforced = False
        detail = support.detail
        if support.supported:
            enforced, detail = resources.apply_quota(mountpoint, storage_gb, support.method,
                                                     project_id)
        detail = f"{detail} {resources.DATA_VOLUME_QUOTA_NOTE}".strip()
        self.repo.set_quota(container, node_id=int(self.node["id"]), method=support.method,
                            project_id=project_id if support.method == "xfs-project-quota" else None,
                            size_gb=storage_gb, enforced=enforced, detail=detail)
        if not enforced:
            logger.warning("disk quota NOT enforced for %s: %s", container, detail)
        return {"enforced": enforced, "method": support.method, "detail": detail,
                "project_id": project_id if enforced else None}

    # ── concurrency guard ───────────────────────────────────────────────────
    @staticmethod
    def _with_guard(limit_name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Run a long operation under the platform-wide concurrency cap.

        Provisioning already runs on a worker thread, so the asyncio semaphore
        is not usable here; :data:`tgdbot.concurrency.THREAD_LIMITS` gives
        the same "never more than N in flight" guarantee.
        """
        with THREAD_LIMITS.guard(limit_name):
            return func(*args, **kwargs)

    # ── rollback ────────────────────────────────────────────────────────────
    def rollback(self) -> List[str]:
        """Undo everything this run created.  Never raises."""
        container = self.request.container_name
        if self.container_created:
            try:
                ctr.remove(self.client, container, volumes=False, force=True)
                self.cleanup.append(f"container {container} removed")
            except Exception as exc:
                self.cleanup_errors.append(f"container removal failed: {exc}")
        if self.volume_created:
            try:
                volumes.remove(self.client, container, force=True)
                self.cleanup.append(f"volume {volumes.volume_name(container)} removed")
            except Exception as exc:
                self.cleanup_errors.append(f"volume removal failed: {exc}")
        if self.allocator is not None:
            try:
                released = self.allocator.release_all(container)
                if released:
                    self.cleanup.append(f"{released} port reservation(s) released")
            except Exception as exc:
                self.cleanup_errors.append(f"port release failed: {exc}")
        if self.row_inserted:
            try:
                self.repo.delete_vps_row(container)
                self.cleanup.append("partial database row removed")
            except Exception as exc:
                self.cleanup_errors.append(f"database cleanup failed: {exc}")
        try:
            self.repo.delete_quota(container)
        except Exception:
            pass
        ssh.clear_credential(container)
        logger.warning("provisioning rollback for %s: %s%s", container, self.cleanup,
                       f" (errors: {self.cleanup_errors})" if self.cleanup_errors else "")
        return list(self.cleanup)


def provision(repo: Any, request: ProvisionRequest, *,
              progress: Optional[Progress] = None) -> ProvisionResult:
    """Run one provisioning attempt with automatic rollback on failure."""
    runner = Provisioner(repo, request, progress=progress)
    try:
        return runner.run()
    except ProvisioningError as exc:
        cleanup = runner.rollback()
        exc.cleanup = cleanup or exc.cleanup
        exc.cleanup_errors = runner.cleanup_errors
        raise
    except Exception as exc:  # noqa: BLE001 - convert anything else
        cleanup = runner.rollback()
        raise ProvisioningError(
            f"Provisioning failed: {exc}", detail=str(exc),
            cleanup=cleanup, cleanup_errors=runner.cleanup_errors) from exc
