"""
Host port management.

Docker publishes ports at *container creation* time, so the port set of a
running container is fixed.  To keep the existing UnixNodes workflow (add or
remove a forward on a live VPS) this module maintains a **database-backed
reservation ledger** and the container is recreated — preserving its volume —
whenever the ledger changes.

Guarantees:

* A ``UNIQUE(node_id, host_ip, host_port, protocol)`` partial index means two
  VPS instances can never claim the same host address + port + protocol, even
  under concurrent creation.
* Ports are reserved *transactionally before* the container is created and
  released if provisioning fails, so a crash never leaves a phantom allocation.
* Released ports stay in the table with ``status='released'`` which gives the
  ``!ports history`` audit trail.
"""

from __future__ import annotations

import logging
import random
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import config
from ..errors import NoFreePort, PortConflict, PortError
from .security import protocols_for, validate_port, validate_protocol

logger = logging.getLogger("unixnodes.docker.ports")

PURPOSE_SSH = "ssh"
PURPOSE_USER = "user"

STATUS_RESERVED = "reserved"
STATUS_BOUND = "bound"
STATUS_RELEASED = "released"


@dataclass
class Reservation:
    id: int
    node_id: int
    host_ip: str
    host_port: int
    container_name: str
    container_port: int
    protocol: str
    purpose: str
    status: str

    @property
    def docker_key(self) -> str:
        return f"{self.container_port}/{self.protocol}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "node_id": self.node_id, "host_ip": self.host_ip,
            "host_port": self.host_port, "container_name": self.container_name,
            "container_port": self.container_port, "protocol": self.protocol,
            "purpose": self.purpose, "status": self.status,
        }


def _host_port_in_use(host_ip: str, port: int, protocol: str) -> bool:
    """Best-effort OS check that nothing else already owns the port."""
    bind_ip = "0.0.0.0" if host_ip in ("", "0.0.0.0") else host_ip
    family = socket.AF_INET
    if ":" in bind_ip:
        family = socket.AF_INET6
        bind_ip = "::" if bind_ip in ("::", "") else bind_ip
    if protocol == "udp":
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((bind_ip, port))
            except OSError:
                return True
        return False
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_ip, port))
        except OSError:
            return True
    return False


class PortAllocator:
    """Reserves, releases and verifies host ports for one node."""

    def __init__(self, repo: Any, node_id: int, publish_ip: Optional[str] = None) -> None:
        self.repo = repo
        self.node_id = int(node_id)
        self.publish_ip = str(publish_ip or config.DOCKER_PUBLISH_IP or "0.0.0.0")

    # ── ranges ──────────────────────────────────────────────────────────────
    def _range_for(self, purpose: str) -> Tuple[int, int]:
        if purpose == PURPOSE_SSH:
            return config.DOCKER_SSH_PORT_START, config.DOCKER_SSH_PORT_END
        return config.DOCKER_PORT_RANGE_START, config.DOCKER_PORT_RANGE_END

    def _taken(self) -> set:
        return {int(row["host_port"]) for row in self.repo.active_ports_for_node(self.node_id)}

    # ── reservation ─────────────────────────────────────────────────────────
    def pick_free_port(self, purpose: str, *, attempts: int = 400) -> int:
        start, end = self._range_for(purpose)
        if end <= start:
            raise NoFreePort(f"Port range {start}-{end} is invalid")
        taken = self._taken()
        candidates = list(range(start, end + 1))
        random.shuffle(candidates)
        checked = 0
        for port in candidates:
            if checked >= attempts:
                break
            checked += 1
            if port in taken or port in config.DOCKER_RESERVED_PORTS:
                continue
            if _host_port_in_use(self.publish_ip, port, "tcp"):
                continue
            return int(port)
        raise NoFreePort(
            f"No free host port in {start}-{end} on node {self.node_id}",
            user_message="This node has run out of free ports. Ask an administrator to widen the range.")

    def reserve(self, container_name: str, container_port: int, *, purpose: str = PURPOSE_USER,
                protocol: str = "both", host_port: Optional[int] = None,
                user_id: Optional[Any] = None) -> List[Reservation]:
        """
        Reserve a host port for ``container_port``.

        ``protocol='both'`` creates one reservation per protocol so TCP and UDP
        are tracked (and released) independently.
        """
        ok, reason = validate_port(container_port)
        if not ok:
            raise PortError(reason)
        ok, reason = validate_protocol(protocol)
        if not ok:
            raise PortError(reason)

        host_port = int(host_port) if host_port else self.pick_free_port(purpose)
        created: List[Reservation] = []
        try:
            for proto in protocols_for(protocol):
                row_id = self.repo.reserve_port(
                    node_id=self.node_id, host_ip=self.publish_ip, host_port=host_port,
                    container_name=container_name, container_port=int(container_port),
                    protocol=proto, purpose=purpose, user_id=user_id,
                )
                created.append(Reservation(row_id, self.node_id, self.publish_ip, host_port,
                                           container_name, int(container_port), proto, purpose,
                                           STATUS_RESERVED))
        except PortConflict:
            raise
        except Exception as exc:
            # A unique-constraint violation means another VPS won the race.
            text = str(exc).lower()
            if "unique" in text or "constraint" in text:
                for item in created:
                    self.release_by_id(item.id)
                raise PortConflict(
                    f"Host port {host_port} is already taken on node {self.node_id}",
                    user_message="That port was claimed by another VPS at the same moment.") from exc
            for item in created:
                self.release_by_id(item.id)
            raise PortError(f"Could not reserve port {host_port}", detail=str(exc)) from exc
        return created

    def release_by_id(self, reservation_id: int) -> bool:
        return bool(self.repo.release_port(int(reservation_id)))

    def release_container_port(self, container_name: str, container_port: int) -> int:
        rows = [row for row in self.active(container_name)
                if int(row["container_port"]) == int(container_port)]
        count = 0
        for row in rows:
            if self.release_by_id(int(row["id"])):
                count += 1
        return count

    def release_all(self, container_name: str) -> int:
        rows = self.active(container_name)
        count = 0
        for row in rows:
            if self.release_by_id(int(row["id"])):
                count += 1
        return count

    # ── queries ─────────────────────────────────────────────────────────────
    def active(self, container_name: str) -> List[Dict[str, Any]]:
        return self.repo.active_ports_for_container(container_name, node_id=self.node_id)

    def history(self, container_name: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self.repo.port_history(container_name, node_id=self.node_id, limit=limit)

    def ssh_port(self, container_name: str) -> Optional[int]:
        for row in self.active(container_name):
            if int(row["container_port"]) == config.SSH_CONTAINER_PORT:
                return int(row["host_port"])
        return None

    def publish_spec(self, container_name: str) -> List[Tuple[int, int, str]]:
        """``[(host_port, container_port, protocol), …]`` for container creation."""
        spec: List[Tuple[int, int, str]] = []
        seen = set()
        for row in self.active(container_name):
            key = (int(row["host_port"]), int(row["container_port"]), str(row["protocol"]))
            if key in seen:
                continue
            seen.add(key)
            spec.append(key)
        return sorted(spec, key=lambda item: (item[1], item[2], item[0]))

    # ── state transitions ───────────────────────────────────────────────────
    def mark_bound(self, container_name: str) -> int:
        return self.repo.set_port_status(self.active(container_name), STATUS_BOUND)

    def verify_bindings(self, container: Any, container_name: str) -> List[str]:
        """
        Compare the ledger with what Docker actually bound.

        Returns a list of human-readable problems (empty means everything is
        published as promised).
        """
        from .networking import verify_binding

        problems: List[str] = []
        for row in self.active(container_name):
            if not verify_binding(container, int(row["container_port"]), str(row["protocol"]),
                                  int(row["host_port"]), str(row["host_ip"])):
                problems.append(
                    f"{row['container_port']}/{row['protocol']} → host {row['host_port']} is not bound")
        return problems

    def summary(self, container_name: str) -> Dict[str, Any]:
        rows = self.active(container_name)
        return {
            "count": len(rows),
            "ssh_port": self.ssh_port(container_name),
            "rows": rows,
            "publish_ip": self.publish_ip,
        }
