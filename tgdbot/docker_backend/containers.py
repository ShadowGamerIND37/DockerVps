"""
Container lifecycle: create, start, stop, restart, remove, exec, logs and the
volume-preserving recreation used whenever Docker needs a fresh container to
apply a change (resource limits, published ports, security policy).

All functions are synchronous — call them through
:func:`tgdbot.concurrency.run_in_thread`.
"""

from __future__ import annotations

import base64
import logging
import shlex
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import config
from ..concurrency import LIMITS
from ..errors import ContainerError, ContainerNotFound
from . import security
from .client import is_transient
from .networking import container_ip, published_ports
from .resources import limits_for

logger = logging.getLogger("tgdbot.docker.containers")

#: Cached "does this container have bash?" answers (Alpine/busybox do not).
_SHELL_CACHE: Dict[str, str] = {}
_SHELL_LOCK = threading.Lock()

TERMINAL_STATES = {"exited", "dead"}
ACTIVE_STATES = {"running", "restarting", "paused", "created"}


# ── lookup ──────────────────────────────────────────────────────────────────

def get_container(client: Any, name: str) -> Any:
    try:
        return client.containers.get(name)
    except Exception as exc:
        raise ContainerNotFound(f"Container {name} does not exist", detail=str(exc)) from exc


def find_container(client: Any, name: str) -> Optional[Any]:
    try:
        return client.containers.get(name)
    except Exception:
        return None


def exists(client: Any, name: str) -> bool:
    return find_container(client, name) is not None


def inspect(client: Any, name: str) -> Dict[str, Any]:
    return dict(get_container(client, name).attrs)


def state(client: Any, name: str) -> str:
    """
    Authoritative container state, never cached.

    Returns one of ``running``, ``restarting``, ``paused``, ``created``,
    ``exited``, ``dead``, ``missing`` or ``unknown``.
    """
    container = find_container(client, name)
    if container is None:
        return "missing"
    try:
        return str((container.attrs.get("State") or {}).get("Status") or "unknown")
    except Exception:
        return "unknown"


def simple_state(client: Any, name: str) -> str:
    """Collapse Docker's states into the running/stopped vocabulary the UI uses."""
    current = state(client, name)
    if current == "running":
        return "running"
    if current in ("paused", "restarting", "created"):
        return current
    if current == "missing":
        return "missing"
    return "stopped"


def managed_label_filter() -> Dict[str, Any]:
    """
    Docker API filter matching containers this platform manages.

    Derived from ``config.LABEL_MANAGED`` instead of a hardcoded string, so the
    filter can never drift from the label applied at creation time.  The legacy
    label is matched too: containers created before the TGD Bot rebrand keep
    working instead of being reported as orphans.
    """
    return {"label": [f"{config.LABEL_MANAGED}=true",
                      f"{config.LEGACY_LABEL_MANAGED}=true"]}


def list_managed(client: Any) -> List[Dict[str, Any]]:
    """Every container this platform manages on the node (running and stopped)."""
    out: List[Dict[str, Any]] = []
    for container in client.containers.list(all=True, filters=managed_label_filter()):
        attrs = dict(container.attrs or {})
        labels = ((attrs.get("Config") or {}).get("Labels")) or {}
        out.append({
            "id": container.short_id,
            "name": container.name,
            "container_name": config.label_value(labels, config.LABEL_CONTAINER, container.name),
            "status": (attrs.get("State") or {}).get("Status"),
            "image": (attrs.get("Config") or {}).get("Image"),
            "created": attrs.get("Created"),
            "started_at": (attrs.get("State") or {}).get("StartedAt"),
            "restart_count": int(attrs.get("RestartCount") or 0),
            "owner": config.label_value(labels, config.LABEL_OWNER),
            "vps_id": config.label_value(labels, config.LABEL_VPS_ID),
        })
    return out


def count_by_state(client: Any) -> Dict[str, int]:
    filters = managed_label_filter()
    running = len(client.containers.list(filters=filters))
    total = len(client.containers.list(all=True, filters=filters))
    return {"running": running, "stopped": max(0, total - running), "total": total}


# ── creation ────────────────────────────────────────────────────────────────

def build_container_spec(*, container_name: str, image: str, ram_mb: int, cpu: float,
                         pids: int, publish_ip: str,
                         published_ports: Sequence[Tuple[int, int, str]] = (),
                         volumes: Optional[Dict[str, Dict[str, str]]] = None,
                         labels: Optional[Dict[str, str]] = None,
                         network: Optional[str] = None,
                         command: Optional[Sequence[str]] = None,
                         hostname: Optional[str] = None,
                         storage_opt: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Assemble the keyword payload for ``client.containers.create(**spec)``.

    The returned dict is a flat mapping of the SDK's high-level arguments; the
    SDK turns the resource keys into the real ``HostConfig`` for us (and stamps
    the negotiated API version into it).
    """
    ok, reason = security.validate_container_name(container_name)
    if not ok:
        raise ContainerError(reason)
    ok, reason = security.validate_image_reference(image)
    if not ok:
        raise ContainerError(reason)

    spec: Dict[str, Any] = security.build_host_config(
        ram_mb=ram_mb, cpu=cpu, pids=pids, publish_ip=publish_ip,
        published_ports=published_ports, volumes=volumes,
        network=network or config.DOCKER_DEFAULT_NETWORK,
    )
    if storage_opt:
        spec["storage_opt"] = dict(storage_opt)

    spec.update({
        "image": image,
        "name": container_name,
        "hostname": hostname or container_name,
        "detach": True,
        "tty": False,
        "stdin_open": False,
        "labels": {
            config.LABEL_MANAGED: "true",
            config.LABEL_CONTAINER: container_name,
            config.LABEL_ROLE: config.ROLE_VPS,
            **(labels or {}),
        },
        "command": list(command) if command else None,
        "volumes": volumes or {},
    })
    return spec


def create(client: Any, spec: Dict[str, Any], *, retries: int = 0) -> Any:
    """Create a container from a :func:`build_container_spec` payload."""
    payload = {key: value for key, value in spec.items() if value is not None}
    last_error: Optional[Exception] = None
    for attempt in range(max(1, retries + 1)):
        try:
            return client.containers.create(**payload)
        except Exception as exc:  # noqa: BLE001
            last_error = exc  # type: ignore[assignment]
            if not is_transient(exc) or attempt >= retries:
                break
            time.sleep(min(5.0, config.DOCKER_RETRY_BACKOFF ** (attempt + 1)))
    raise ContainerError(f"Could not create container {spec.get('name')}",
                         detail=str(last_error)) from last_error



def start(client: Any, name: str, *, timeout: Optional[int] = None) -> Any:
    container = get_container(client, name)
    deadline = time.time() + int(timeout or config.DOCKER_START_TIMEOUT)
    try:
        container.start()
    except Exception as exc:
        text = str(exc)
        if "already started" in text.lower():
            return container
        raise ContainerError(f"Could not start {name}", detail=text) from exc
    while time.time() < deadline:
        container.reload()
        status = (container.attrs.get("State") or {}).get("Status")
        if status == "running":
            return container
        if status in TERMINAL_STATES:
            error = (container.attrs.get("State") or {}).get("Error") or "container exited"
            raise ContainerError(f"{name} exited during start: {error}")
        time.sleep(0.5)
    raise ContainerError(f"{name} did not reach the running state",
                         user_message="The VPS did not start in time.")


def stop(client: Any, name: str, *, timeout: Optional[int] = None, force: bool = False) -> bool:
    container = find_container(client, name)
    if container is None:
        return False
    seconds = int(timeout or config.DOCKER_STOP_TIMEOUT)
    try:
        container.stop(timeout=seconds)
        return True
    except Exception as exc:
        text = str(exc).lower()
        if "not running" in text or "304" in text:
            return False
        if force:
            try:
                container.kill()
                return True
            except Exception as kill_error:
                logger.warning("could not kill %s: %s", name, kill_error)
                return False
        raise ContainerError(f"Could not stop {name}", detail=str(exc)) from exc


def restart(client: Any, name: str, *, timeout: Optional[int] = None) -> Any:
    container = get_container(client, name)
    try:
        container.restart(timeout=int(timeout or config.DOCKER_STOP_TIMEOUT))
    except Exception as exc:
        raise ContainerError(f"Could not restart {name}", detail=str(exc)) from exc
    deadline = time.time() + int(timeout or config.DOCKER_START_TIMEOUT)
    while time.time() < deadline:
        container.reload()
        if (container.attrs.get("State") or {}).get("Status") == "running":
            return container
        time.sleep(0.5)
    raise ContainerError(f"{name} did not come back after restart")


def pause(client: Any, name: str) -> bool:
    container = find_container(client, name)
    if container is None:
        return False
    try:
        container.pause()
        return True
    except Exception as exc:
        logger.debug("pause failed for %s: %s", name, exc)
        return False


def unpause(client: Any, name: str) -> bool:
    container = find_container(client, name)
    if container is None:
        return False
    try:
        container.unpause()
        return True
    except Exception as exc:
        logger.debug("unpause failed for %s: %s", name, exc)
        return False


def remove(client: Any, name: str, *, volumes: bool = False, force: bool = True) -> bool:
    """
    Remove a container.

    ``volumes=False`` (the default) keeps the persistent VPS volume — that is
    what makes resource changes and rebuilds non-destructive.
    """
    container = find_container(client, name)
    if container is None:
        return False
    try:
        container.remove(v=volumes, force=force)
        logger.info("removed container %s (volumes=%s)", name, volumes)
        return True
    except Exception as exc:
        raise ContainerError(f"Could not remove {name}", detail=str(exc)) from exc


# ── exec ────────────────────────────────────────────────────────────────────

def detect_shell(client: Any, name: str) -> str:
    with _SHELL_LOCK:
        cached = _SHELL_CACHE.get(name)
    if cached:
        return cached
    detected = "sh"
    try:
        container = get_container(client, name)
        result = container.exec_run(
            ["sh", "-c", "command -v bash >/dev/null 2>&1 && echo bash || echo sh"],
            demux=True,
        )
        stdout = (result.output[0] if isinstance(result.output, tuple) else result.output) or b""
        text = stdout.decode("utf-8", "replace").strip()
        detected = "bash" if text.endswith("bash") else "sh"
    except Exception as exc:
        logger.debug("shell detection failed for %s: %s", name, exc)
    with _SHELL_LOCK:
        _SHELL_CACHE[name] = detected
    return detected


def forget_shell(name: str) -> None:
    with _SHELL_LOCK:
        _SHELL_CACHE.pop(name, None)


def exec_argv(client: Any, name: str, argv: Sequence[str], *, user: str = "root",
              workdir: str = "/", timeout: Optional[int] = None,
              environment: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """Run a command inside the container and return ``(code, stdout, stderr)``."""
    container = get_container(client, name)
    try:
        result = container.exec_run(
            list(argv), user=user, workdir=workdir, demux=True,
            environment=environment or None,
        )
    except Exception as exc:
        raise ContainerError(f"exec failed in {name}", detail=str(exc)) from exc
    stdout_b, stderr_b = result.output if isinstance(result.output, tuple) else (result.output, b"")
    stdout = (stdout_b or b"").decode("utf-8", "replace")
    stderr = (stderr_b or b"").decode("utf-8", "replace")
    return int(result.exit_code if result.exit_code is not None else -1), stdout, stderr


def exec_script(client: Any, name: str, script: str, *, user: str = "root",
                timeout: Optional[int] = None) -> str:
    """
    Run a shell script inside a container.

    The script is base64-encoded and piped into the detected interpreter, which
    removes every quoting problem that inline ``bash -c "..."`` calls have.
    Works on bash *and* busybox ``sh``.
    """
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    shell = detect_shell(client, name)
    code, out, err = exec_argv(
        client, name,
        [shell, "-c", f"echo {encoded} | base64 -d | {shell}"],
        user=user, timeout=timeout,
    )
    if code != 0 and not out.strip():
        raise ContainerError(f"container script failed (exit {code})",
                             detail=(err or out or "no output").strip()[:500])
    return (out or err or "").strip()


def put_file(client: Any, name: str, container_path: str, data: bytes, *,
             filename: str = "payload", mode: int = 0o600) -> bool:
    """
    Copy a file into a container using ``put_archive`` (a tar stream in memory).

    Used to hand the root credential to the guest without ever putting it in an
    environment variable, an argument list or the Docker inspect metadata.
    """
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mode = mode
        info.uid = 0
        info.gid = 0
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    container = get_container(client, name)
    return bool(container.put_archive(container_path, buffer.getvalue()))


def logs(client: Any, name: str, tail: int = 60, since: Optional[int] = None) -> str:
    container = find_container(client, name)
    if container is None:
        return ""
    try:
        raw = container.logs(tail=int(tail), timestamps=False, since=since, stderr=True, stdout=True)
    except Exception as exc:
        return f"(could not read logs: {exc})"
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


# ── waiting helpers ─────────────────────────────────────────────────────────

def wait_running(client: Any, name: str, *, timeout: Optional[int] = None,
                 interval: float = 1.0) -> bool:
    deadline = time.time() + int(timeout or config.DOCKER_START_TIMEOUT)
    while time.time() < deadline:
        if state(client, name) == "running":
            return True
        time.sleep(interval)
    return False


def uptime_seconds(client: Any, name: str) -> float:
    container = find_container(client, name)
    if container is None:
        return 0.0
    started = (container.attrs.get("State") or {}).get("StartedAt")
    if not started or str(started).startswith("0001"):
        return 0.0
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    from datetime import timezone as _tz

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tz.utc)
    return max(0.0, (datetime.now(_tz.utc) - moment).total_seconds())


def restart_count(client: Any, name: str) -> int:
    container = find_container(client, name)
    if container is None:
        return 0
    return int(container.attrs.get("RestartCount") or 0)


def summary(client: Any, name: str) -> Dict[str, Any]:
    """Compact description used by the Discord panels."""
    container = find_container(client, name)
    if container is None:
        return {"exists": False, "status": "missing"}
    attrs = dict(container.attrs or {})
    host = attrs.get("HostConfig") or {}
    state_block = attrs.get("State") or {}
    return {
        "exists": True,
        "id": container.short_id,
        "status": state_block.get("Status"),
        "running": bool(state_block.get("Running")),
        "oom_killed": bool(state_block.get("OOMKilled")),
        "exit_code": state_block.get("ExitCode"),
        "started_at": state_block.get("StartedAt"),
        "finished_at": state_block.get("FinishedAt"),
        "restart_count": int(attrs.get("RestartCount") or 0),
        "image": (attrs.get("Config") or {}).get("Image"),
        "ip": container_ip(container),
        "ports": published_ports(container),
        "limits": limits_for(
            int((host.get("Memory") or 0) // (1024 * 1024)),
            (int(host.get("CpuQuota") or 0) / int(host.get("CpuPeriod"))) if host.get("CpuPeriod") else 0,
            int(host.get("PidsLimit") or 0),
        ),
    }
