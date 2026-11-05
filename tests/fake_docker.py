"""
Fake Docker SDK client.

Implements just enough of ``docker.DockerClient`` for the UnixNodes backend to
run end-to-end in a sandbox with no Docker daemon.  It records every call so
tests can assert on what the backend actually asked for, and it can be told to
fail at a chosen step so rollback paths are exercised for real.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class DockerAPIError(Exception):
    """Stand-in for ``docker.errors.APIError``."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotFound(DockerAPIError):
    def __init__(self, message: str = "not found") -> None:
        super().__init__(message, 404)


class Conflict(DockerAPIError):
    def __init__(self, message: str = "conflict") -> None:
        super().__init__(message, 409)


# ── containers ──────────────────────────────────────────────────────────────

def _port_bindings(ports: Any) -> Dict[str, List[Dict[str, str]]]:
    """
    Turn the SDK's ``ports={"22/tcp": [(ip, host_port), …]}`` shape into the
    ``NetworkSettings.Ports`` shape the Engine reports back.

    The mapping key already carries the protocol, so it must not be appended
    again.
    """
    out: Dict[str, List[Dict[str, str]]] = {}
    for key, bindings in (ports or {}).items():
        out[str(key)] = [{"HostIp": str(bind[0]), "HostPort": str(int(bind[1]))}
                         for bind in (bindings or [])]
    return out


class FakeContainer:
    def __init__(self, engine: "FakeEngine", name: str, **kwargs: Any) -> None:
        self.engine = engine
        self.name = name
        self.id = f"{abs(hash(name)) % (16 ** 12):012x}"
        self.short_id = self.id[:12]
        self.kwargs = kwargs
        self.status = "created"
        self.attrs: Dict[str, Any] = {
            "Id": self.id,
            "Name": f"/{name}",
            "Created": "2026-01-01T00:00:00.000000000Z",
            "State": {"Status": "created", "Running": False, "OOMKilled": False,
                      "StartedAt": "0001-01-01T00:00:00Z"},
            "Config": {"Image": kwargs.get("image"), "Labels": dict(kwargs.get("labels") or {}),
                       "Hostname": kwargs.get("hostname") or name},
            "HostConfig": {
                "Memory": int(kwargs.get("mem_limit") or 0),
                "MemoryReservation": int(kwargs.get("memswap_limit") or 0),
                "NanoCpus": int(kwargs.get("nano_cpus") or 0),
                "CpuQuota": int(kwargs.get("cpu_quota") or 0),
                "CpuPeriod": int(kwargs.get("cpu_period") or 0),
                "CpuShares": int(kwargs.get("cpu_shares") or 0),
                "PidsLimit": int(kwargs.get("pids_limit") or 0),
                "Privileged": bool(kwargs.get("privileged")),
                "Init": bool(kwargs.get("init")),
                "SecurityOpt": list(kwargs.get("security_opt") or []),
                "CapDrop": list(kwargs.get("cap_drop") or []),
                "Sysctls": dict(kwargs.get("sysctls") or {}),
                "Binds": [f"{src}:{spec['bind']}:{spec.get('mode', 'rw')}"
                          for src, spec in (kwargs.get("volumes") or {}).items()],
                "PortBindings": _port_bindings(kwargs.get("ports")),
                "Devices": list(kwargs.get("devices") or []),
            },
            "Mounts": [{"Name": src, "Destination": spec["bind"], "Type": "volume"}
                       for src, spec in (kwargs.get("volumes") or {}).items()],
            "NetworkSettings": {
                "Ports": _port_bindings(kwargs.get("ports")),
                "Networks": {
                    "bridge": {"IPAddress": "172.20.0.7", "Gateway": "172.20.0.1"},
                },
            },
        }
        self.exec_calls: List[Tuple[Any, Dict[str, Any]]] = []

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        self.engine.fail_if("container.start")
        if self.status == "running":
            raise DockerAPIError("container already started")
        self.status = "running"
        self.attrs["State"].update({"Status": "running", "Running": True,
                                    "StartedAt": "2026-01-01T00:00:01Z"})
        self.engine.record("start", self.name)

    def stop(self, timeout: int = 10) -> None:
        self.engine.fail_if("container.stop")
        self.status = "exited"
        self.attrs["State"].update({"Status": "exited", "Running": False})
        self.engine.record("stop", self.name)

    def restart(self, timeout: int = 10) -> None:
        self.stop()
        self.start()

    def kill(self) -> None:
        self.stop()

    def pause(self) -> None:
        self.status = "paused"
        self.attrs["State"]["Status"] = "paused"

    def unpause(self) -> None:
        self.status = "running"
        self.attrs["State"]["Status"] = "running"

    def remove(self, force: bool = False, v: bool = False) -> None:
        self.engine.fail_if("container.remove")
        self.engine.containers.pop(self.name, None)
        self.engine.record("remove", self.name)

    def reload(self) -> None:
        self.engine.fail_if("container.reload")

    def rename(self, name: str) -> None:
        self.engine.containers.pop(self.name, None)
        self.name = name
        self.engine.containers[name] = self

    def wait(self, timeout: Optional[int] = None) -> Dict[str, Any]:
        return {"StatusCode": 0}

    def logs(self, tail: Any = "all", follow: bool = False, timestamps: bool = False) -> bytes:
        self.engine.fail_if("container.logs")
        return self.engine.container_logs.get(self.name, b"unixnodes: sshd ready\n")

    def stats(self, stream: bool = True) -> Any:
        self.engine.fail_if("container.stats")
        ram_limit = int(self.kwargs.get("mem_limit") or 1024 * 1024 * 1024)
        sample = {
            "read": "2026-01-01T00:00:01.000000000Z",
            "preread": "2026-01-01T00:00:00.000000000Z",
            "cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000},
                          "system_cpu_usage": 10_000_000_000, "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000},
                             "system_cpu_usage": 9_000_000_000},
            "memory_stats": {"usage": ram_limit // 4, "limit": ram_limit,
                             "stats": {"cache": 1024 * 1024}},
            "networks": {"eth0": {"rx_bytes": 4096, "tx_bytes": 2048}},
            "blkio_stats": {"io_service_bytes_recursive": []},
        }
        return iter([sample]) if stream else sample

    def put_archive(self, path: str, data: bytes) -> bool:
        self.engine.archives[(self.name, path)] = bytes(data)
        return True

    def get_archive(self, path: str) -> Tuple[bytes, Dict[str, Any]]:
        payload = self.engine.archives.get((self.name, path), b"fake-archive")
        return payload, {"size": len(payload)}

    # ── exec ────────────────────────────────────────────────────────────────
    def exec_run(self, cmd: Any, **kwargs: Any) -> Any:
        self.engine.fail_if("container.exec_run")
        self.exec_calls.append((cmd, kwargs))
        text = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) > 2 else str(cmd)
        stdout = self.engine.exec_response(self, text)
        self.engine.record("exec", f"{self.name}:{text[:60]}")
        return _ExecResult(0, stdout.encode() if isinstance(stdout, str) else stdout)


class _ExecResult:
    def __init__(self, exit_code: int, output: bytes) -> None:
        self.exit_code = exit_code
        self.output = output


# ── collections ─────────────────────────────────────────────────────────────

class FakeContainerCollection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def create(self, **kwargs: Any) -> FakeContainer:
        self.engine.fail_if("containers.create")
        name = str(kwargs.get("name") or "")
        if name in self.engine.containers:
            raise Conflict(f"Conflict. The container name /{name} is already in use")
        # `name` is a positional argument of FakeContainer; keep the full kwargs
        # for inspection but do not pass the key twice.
        container = FakeContainer(self.engine, name,
                                  **{k: v for k, v in kwargs.items() if k != "name"})
        self.engine.containers[name] = container
        self.engine.record("create", name)
        self.engine.created.append(container)
        return container

    def get(self, name: str) -> FakeContainer:
        try:
            return self.engine.containers[name]
        except KeyError:
            for container in self.engine.containers.values():
                if container.id == name or container.short_id == name:
                    return container
            raise NotFound(f"No such container: {name}")

    def list(self, all: bool = False, filters: Optional[Dict[str, Any]] = None,
             ignore_removed: bool = False) -> List[FakeContainer]:
        label = (filters or {}).get("label")
        out: List[FakeContainer] = []
        for container in self.engine.containers.values():
            if not all and container.status not in ("running", "paused", "created"):
                continue
            if label and not self._matches(container, label):
                continue
            out.append(container)
        return out

    @staticmethod
    def _matches(container: FakeContainer, label: Any) -> bool:
        labels = container.attrs["Config"]["Labels"]
        if isinstance(label, (list, tuple)):
            return all(FakeContainerCollection._matches(container, item) for item in label)
        key, _, value = str(label).partition("=")
        if not value:
            return key in labels
        return labels.get(key) == value

    def run(self, image: str, **kwargs: Any) -> Any:
        """Helper containers (backups, volume copies) use run()."""
        self.engine.fail_if("containers.run")
        name = str(kwargs.get("name") or f"helper-{len(self.engine.containers)}")
        container = self.create(name=name, image=image, **{k: v for k, v in kwargs.items()
                                                           if k not in ("name", "image")})
        container.start()
        output = self.engine.helper_output(image, kwargs.get("command"))
        self.engine.record("run", f"{name}:{image}")
        return _RunResult(container, output)

    def prune(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"ContainersDeleted": [], "SpaceReclaimed": 0}


class _RunResult:
    def __init__(self, container: FakeContainer, output: str) -> None:
        self._container = container
        self._output = output

    def wait(self, **kwargs: Any) -> Dict[str, Any]:
        return {"StatusCode": 0}

    @property
    def logs(self) -> Callable[..., bytes]:
        return lambda **kwargs: self._output.encode()

    def remove(self, **kwargs: Any) -> None:
        self._container.remove(**kwargs)


class FakeImageCollection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def get(self, reference: str) -> Any:
        if reference in self.engine.images:
            return _FakeImage(reference, self.engine.images[reference])
        raise NotFound(f"No such image: {reference}")

    def pull(self, repository: str, tag: Optional[str] = None, **kwargs: Any) -> Any:
        self.engine.fail_if("images.pull")
        reference = f"{repository}:{tag}" if tag else repository
        self.engine.images.setdefault(reference, "sha256:fake" + str(len(self.engine.images)))
        self.engine.record("pull", reference)
        return _FakeImage(reference, self.engine.images[reference])

    def list(self, **kwargs: Any) -> List[Any]:
        return [_FakeImage(ref, digest) for ref, digest in self.engine.images.items()]

    def prune(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"ImagesDeleted": [], "SpaceReclaimed": 0}


class _FakeImage:
    def __init__(self, reference: str, digest: str) -> None:
        self.tags = [reference]
        self.id = digest
        self.short_id = digest[:19]
        self.attrs = {"Id": digest, "Size": 80 * 1024 * 1024, "RepoTags": [reference]}


class FakeVolumeCollection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def create(self, name: Optional[str] = None, **kwargs: Any) -> Any:
        self.engine.fail_if("volumes.create")
        volume_name = name or f"vol-{len(self.engine.volumes)}"
        self.engine.volumes[volume_name] = {
            "Name": volume_name,
            "Driver": "local",
            "Labels": dict(kwargs.get("labels") or {}),
            "Mountpoint": f"{self.engine.volume_root}/{volume_name}",
            "Options": dict(kwargs.get("driver_opt") or {}),
        }
        self.engine.record("volume.create", volume_name)
        return _FakeVolume(self.engine, volume_name)

    def get(self, name: str) -> Any:
        if name in self.engine.volumes:
            return _FakeVolume(self.engine, name)
        raise NotFound(f"No such volume: {name}")

    def list(self, filters: Optional[Dict[str, Any]] = None) -> Any:
        label = (filters or {}).get("label")
        items = []
        for name, attrs in self.engine.volumes.items():
            if label:
                key, _, value = str(label).partition("=")
                if value and attrs["Labels"].get(key) != value:
                    continue
            items.append(_FakeVolume(self.engine, name))
        return _VolumeList(items)

    def prune(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"VolumesDeleted": [], "SpaceReclaimed": 0}


class _VolumeList:
    def __init__(self, volumes: List[Any]) -> None:
        self.volumes = volumes


class _FakeVolume:
    def __init__(self, engine: "FakeEngine", name: str) -> None:
        self.engine = engine
        self.name = name
        self.attrs = engine.volumes[name]
        self.id = name

    def remove(self, force: bool = False) -> None:
        self.engine.fail_if("volume.remove")
        self.engine.volumes.pop(self.name, None)
        self.engine.record("volume.remove", self.name)


class FakeNetworkCollection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def create(self, name: str, **kwargs: Any) -> Any:
        self.engine.networks[name] = {"Name": name, "Id": name, **kwargs}
        return _FakeNetwork(self.engine, name)

    def get(self, name: str) -> Any:
        if name in self.engine.networks:
            return _FakeNetwork(self.engine, name)
        raise NotFound(f"No such network: {name}")

    def list(self, names: Optional[List[str]] = None, **kwargs: Any) -> List[Any]:
        return [_FakeNetwork(self.engine, name) for name in self.engine.networks
                if not names or name in names]


class _FakeNetwork:
    def __init__(self, engine: "FakeEngine", name: str) -> None:
        self.engine = engine
        self.name = name
        self.id = name
        self.attrs = engine.networks[name]


# ── engine ──────────────────────────────────────────────────────────────────

class FakeEngine:
    """In-memory Docker Engine plus the fault-injection hooks tests use."""

    def __init__(self, *, volume_root: str = "/var/lib/docker/volumes") -> None:
        self.containers: Dict[str, FakeContainer] = {}
        self.volumes: Dict[str, Dict[str, Any]] = {}
        self.networks: Dict[str, Dict[str, Any]] = {"bridge": {"Name": "bridge", "Id": "bridge"}}
        self.images: Dict[str, str] = {"ubuntu:24.04": "sha256:ubuntu2404",
                                       "debian:12": "sha256:debian12",
                                       "alpine:3.20": "sha256:alpine320"}
        self.archives: Dict[Tuple[str, str], bytes] = {}
        self.container_logs: Dict[str, bytes] = {}
        self.calls: List[Tuple[str, str]] = []
        self.created: List[FakeContainer] = []
        self.failures: Dict[str, DockerAPIError] = {}
        self.exec_script: Optional[Callable[[FakeContainer, str], str]] = None
        self.volume_root = volume_root
        #: Guest state the exec emulation reports.  Flip these to simulate a
        #: VPS whose sshd never came up.
        self.sshd_listening = True
        self.sshd_installed = True

        self.containers_api = FakeContainerCollection(self)
        self.images_api = FakeImageCollection(self)
        self.volumes_api = FakeVolumeCollection(self)
        self.networks_api = FakeNetworkCollection(self)

    # ── fault injection ─────────────────────────────────────────────────────
    def fail_at(self, step: str, message: str = "injected failure",
                status_code: int = 500) -> None:
        self.failures[step] = DockerAPIError(message, status_code)

    def clear_failures(self) -> None:
        self.failures.clear()

    def fail_if(self, step: str) -> None:
        error = self.failures.get(step)
        if error is not None:
            raise error

    def record(self, kind: str, detail: str = "") -> None:
        self.calls.append((kind, detail))

    def kinds(self) -> List[str]:
        return [kind for kind, _detail in self.calls]

    # ── exec emulation ──────────────────────────────────────────────────────
    def exec_response(self, container: FakeContainer, script: str) -> str:
        """
        Answer the probe scripts the backend really sends.

        The scripts arrive base64-encoded (see ``containers.exec_script``), so
        they are decoded before matching.  ``sshd_listening`` lets a test
        simulate a guest whose sshd never came up.
        """
        if self.exec_script is not None:
            return self.exec_script(container, script)

        decoded = ""
        match = re.search(r"echo ([A-Za-z0-9+/=]{8,}) \| base64 -d \|", script)
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8", "replace")
            except Exception:
                decoded = ""
        text = decoded or script

        # ssh.is_listening() — `ss -lnt | grep ':22 '` / `pgrep -x sshd`
        if ("ss -lnt" in text or "netstat -lnt" in text
                or "pgrep -x sshd" in text):
            return "LISTENING\n" if self.sshd_listening else ""

        # ssh.guest_report() — a KEY=VALUE block
        if "PRETTY_NAME" in text or "SSHD=" in text:
            sshd = "yes" if self.sshd_installed else "no"
            return ("OS=Ubuntu 24.04.1 LTS\n"
                    "KERNEL=6.8.0-45-generic\n"
                    f"SSHD={sshd}\n"
                    "CRON=/usr/sbin/cron\n"
                    "UPTIME=12.34\n"
                    "DISK_USED=51200\n"
                    "DISK_TOTAL=10485760\n"
                    "DISK_PCT=1%\n")

        # containers.detect_shell()
        if "command -v bash" in text:
            return "bash\n"

        if "id -u" in text:
            return "0\nroot\nSHELL=sh\n"
        if "uname" in text:
            return "Linux\n"
        if "sshd -t" in text or "sshd -T" in text:
            return ""
        if "command -v" in text:
            return "/usr/sbin/sshd\n" if self.sshd_installed else ""
        if "df -Pk" in text or "df -k" in text:
            return "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10485760 51200 10434560 1% /\n"
        return "OK\n"

    def helper_output(self, image: str, command: Any) -> str:
        text = json.dumps(command) if isinstance(command, (list, tuple)) else str(command)
        if "tar" in text and "--list" in text:
            return "./\n./root/\n./etc/\n"
        if "du " in text or "df " in text:
            return "10485760\n"
        return "helper-ok\n"

    # ── DockerClient surface ────────────────────────────────────────────────
    def ping(self) -> bool:
        self.fail_if("ping")
        return True

    def version(self) -> Dict[str, Any]:
        return {"Version": "27.3.1", "ApiVersion": "1.45", "MinAPIVersion": "1.24",
                "Os": "linux", "Arch": "amd64", "KernelVersion": "6.8.0-45-generic"}

    def info(self) -> Dict[str, Any]:
        self.fail_if("info")
        return {
            "ServerVersion": "27.3.1", "ApiVersion": "1.45",
            "OperatingSystem": "Ubuntu 24.04.1 LTS", "OSType": "linux",
            "Architecture": "x86_64", "KernelVersion": "6.8.0-45-generic",
            "NCPU": 8, "MemTotal": 16 * 1024 ** 3,
            "Driver": "overlay2",
            "DriverStatus": [["Backing Filesystem", "extfs"], ["Supports d_type", "true"]],
            "DockerRootDir": "/var/lib/docker",
            "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin"],
            "CgroupDriver": "systemd", "CgroupVersion": "2",
            "Containers": len(self.containers),
            "ContainersRunning": sum(1 for c in self.containers.values()
                                     if c.status == "running"),
            "ContainersStopped": sum(1 for c in self.containers.values()
                                     if c.status == "exited"),
            "Images": len(self.images),
        }

    def df(self) -> Dict[str, Any]:
        return {"LayersSize": 1024 ** 3, "Images": [], "Containers": [], "Volumes": []}

    def close(self) -> None:
        self.record("close", "")

    # ── low-level APIClient surface ─────────────────────────────────────────
    class _Api:
        def __init__(self, engine: "FakeEngine") -> None:
            self.engine = engine

        def pull(self, repository: str, tag: Optional[str] = None,
                 stream: bool = False, decode: bool = False, **kwargs: Any) -> Any:
            self.engine.fail_if("images.pull")
            reference = f"{repository}:{tag}" if tag and ":" not in repository else repository
            self.engine.images.setdefault(reference, "sha256:fake" + str(len(self.engine.images)))
            self.engine.record("pull", reference)
            chunks = [{"status": "Pulling from " + reference, "id": "latest"},
                      {"status": "Downloading", "progressDetail": {}, "id": "abc123"},
                      {"status": "Pull complete", "id": "abc123"},
                      {"status": "Digest: sha256:fake"}]
            return iter(chunks) if stream else chunks

    @property
    def api(self) -> Any:
        return FakeEngine._Api(self)


def make_client(*, volume_root: str = "/var/lib/docker/volumes") -> Any:
    """
    Build an object shaped like ``docker.DockerClient``.

    The engine state is reachable as ``client._engine`` for assertions.
    """
    engine = FakeEngine(volume_root=volume_root)

    class _Client:
        containers = engine.containers_api
        images = engine.images_api
        volumes = engine.volumes_api
        networks = engine.networks_api
        _engine = engine

        ping = engine.ping
        version = engine.version
        info = engine.info
        df = engine.df
        close = engine.close
        api = engine.api

        def exec_run(self, container: str, cmd: Any, **kwargs: Any) -> Any:
            target = engine.containers_api.get(container)
            return target.exec_run(cmd, **kwargs)

    return _Client()
