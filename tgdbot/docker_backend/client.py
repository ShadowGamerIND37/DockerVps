"""
Docker Engine client management.

One :class:`docker.DockerClient` per node, cached and reused.  Supported
transports:

``local``
    The Docker socket on the machine running the bot (``unix:///var/run/docker.sock``
    or whatever ``DOCKER_HOST_URL`` says).

``ssh``
    ``ssh://user@host[:port]`` — the Docker CLI/SDK tunnel over an SSH session.
    No Docker API port is exposed at all, which is the recommended way to reach
    a remote node.

``tls``
    ``tcp://host:2376`` with mutual TLS (client certificate + CA).  Plain
    ``tcp://`` **without** TLS is rejected outright: an unauthenticated Docker
    API is root on the remote host.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .. import config
from ..errors import DockerUnavailable, NodeNotFound

try:  # the SDK is a hard requirement for the infrastructure layer
    import docker
    from docker.errors import APIError, DockerException, NotFound
    from docker.tls import TLSConfig
except Exception:  # pragma: no cover - environment dependent
    docker = None  # type: ignore
    APIError = DockerException = NotFound = Exception  # type: ignore
    TLSConfig = None  # type: ignore

logger = logging.getLogger("tgdbot.docker.client")


# ── transient error detection ───────────────────────────────────────────────

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}


def is_transient(exc: BaseException) -> bool:
    """True when retrying the call is likely to succeed."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    text = str(exc).lower()
    if isinstance(exc, Exception) and docker is not None:
        if isinstance(exc, DockerException) and not isinstance(exc, NotFound):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in TRANSIENT_STATUS:
                return True
    return any(
        token in text
        for token in ("timeout", "timed out", "temporarily unavailable",
                      "connection reset", "connection aborted", "broken pipe",
                      "503", "502", "bad gateway")
    )


# ── endpoint description ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Endpoint:
    """How to reach the Docker Engine on one node."""

    node_id: int
    name: str = "local"
    transport: str = "local"          # local | ssh | tls
    base_url: str = "unix:///var/run/docker.sock"
    #: SSH transport options
    ssh_user: str = "root"
    ssh_key_path: Optional[str] = None
    #: mTLS options
    tls_ca: Optional[str] = None
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    tls_verify: bool = True
    timeout: int = config.DOCKER_API_TIMEOUT
    api_version: Optional[str] = None

    @property
    def is_local(self) -> bool:
        return self.transport == "local"

    def describe(self) -> str:
        if self.transport == "ssh":
            return f"ssh://{self.ssh_user}@{self.base_url}"
        return self.base_url

    @classmethod
    def from_node(cls, node: Dict[str, Any]) -> "Endpoint":
        """Build an endpoint from a ``nodes`` database row."""
        if not node:
            raise NodeNotFound("node record is empty")
        node_id = int(node.get("id") or 1)
        transport = str(node.get("transport") or ("local" if node.get("is_local") else "tls")).lower()
        base_url = str(node.get("docker_host") or node.get("url") or "").strip()
        timeout = int(node.get("docker_timeout") or config.DOCKER_API_TIMEOUT)

        if transport == "local":
            return cls(node_id=node_id, name=str(node.get("name") or "local"),
                       transport="local", base_url=config.DOCKER_HOST_URL,
                       timeout=timeout, api_version=config.DOCKER_API_VERSION)

        if transport == "ssh":
            user, _, host = parse_ssh_target(base_url)
            return cls(node_id=node_id, name=str(node.get("name") or host), transport="ssh",
                       base_url=host, ssh_user=user,
                       ssh_key_path=node.get("docker_tls_ca") or node.get("ssh_key_path"),
                       timeout=timeout, api_version=config.DOCKER_API_VERSION)

        if transport in ("tls", "tcp"):
            return cls(node_id=node_id, name=str(node.get("name") or base_url), transport="tls",
                       base_url=base_url,
                       tls_ca=node.get("docker_tls_ca"), tls_cert=node.get("docker_tls_cert"),
                       tls_key=node.get("docker_tls_key"),
                       tls_verify=bool(int(node.get("docker_tls_verify") or 1)),
                       timeout=timeout, api_version=config.DOCKER_API_VERSION)

        raise DockerUnavailable(f"Unknown Docker transport '{transport}' for node {node_id}")


def parse_ssh_target(target: str) -> Tuple[str, str, str]:
    """``ssh://user@host:port`` → ``(user, host:port, host)``."""
    raw = str(target or "").strip()
    raw = re.sub(r"^ssh://", "", raw, flags=re.I)
    user = "root"
    if "@" in raw:
        user, _, raw = raw.partition("@")
    host = raw.split("/")[0]
    return user or "root", host, host


def validate_remote_endpoint(transport: str, base_url: str) -> Tuple[bool, str]:
    """
    Refuse configurations that would expose an unauthenticated Docker API.

    Returns ``(ok, reason)``.
    """
    url = str(base_url or "").strip().lower()
    if transport == "ssh":
        if not parse_ssh_target(url)[1]:
            return False, "An SSH target like ssh://root@203.0.113.10 is required."
        return True, "ok"
    if not url.startswith("tcp://") and not url.startswith("http"):
        return False, "Remote nodes need tcp://host:2376 (mTLS) or ssh://user@host."
    if ":2375" in url:
        return False, ("Port 2375 is the *unauthenticated* Docker API. Refusing to use it — "
                       "use 2376 with mutual TLS, or an ssh:// target.")
    return True, "ok"


# ── client pool ─────────────────────────────────────────────────────────────

class DockerClientPool:
    """Thread-safe cache of Docker clients keyed by node id."""

    def __init__(self) -> None:
        self._clients: Dict[int, Any] = {}
        self._endpoints: Dict[int, Endpoint] = {}
        self._lock = threading.RLock()
        self._factories = {
            "local": self._build_local,
            "ssh": self._build_ssh,
            "tls": self._build_tls,
        }

    # ── construction ────────────────────────────────────────────────────────
    @staticmethod
    def _require_sdk() -> None:
        if docker is None:
            raise DockerUnavailable(
                "The Docker SDK is not installed. Run: pip install -r requirements.txt")

    def _build_local(self, endpoint: Endpoint) -> Any:
        return docker.DockerClient(
            base_url=endpoint.base_url,
            version=endpoint.api_version or config.DOCKER_API_VERSION,
            timeout=endpoint.timeout,
        )

    def _build_ssh(self, endpoint: Endpoint) -> Any:
        host = endpoint.base_url
        if endpoint.ssh_user and "@" not in host:
            host = f"{endpoint.ssh_user}@{host}"
        return docker.DockerClient(
            base_url=f"ssh://{host}",
            version=endpoint.api_version or config.DOCKER_API_VERSION,
            timeout=endpoint.timeout,
            use_ssh_client=True,
        )

    def _build_tls(self, endpoint: Endpoint) -> Any:
        if TLSConfig is None:  # pragma: no cover
            raise DockerUnavailable("docker.tls is unavailable in this SDK install")
        ok, reason = validate_remote_endpoint("tls", endpoint.base_url)
        if not ok:
            raise DockerUnavailable(reason)
        kwargs: Dict[str, Any] = {}
        if endpoint.tls_cert and endpoint.tls_key:
            kwargs["client_cert"] = (endpoint.tls_cert, endpoint.tls_key)
        if endpoint.tls_ca:
            kwargs["ca_cert"] = endpoint.tls_ca
        kwargs["verify"] = bool(endpoint.tls_verify)
        tls = TLSConfig(**kwargs)
        base = endpoint.base_url
        if base.startswith("http://"):
            base = "tcp://" + base[len("http://"):]
        return docker.DockerClient(base_url=base, tls=tls, timeout=endpoint.timeout,
                                   version=endpoint.api_version or config.DOCKER_API_VERSION)

    # ── public API ──────────────────────────────────────────────────────────
    def get(self, endpoint: Endpoint) -> Any:
        """Return (creating if needed) the client for ``endpoint``."""
        self._require_sdk()
        with self._lock:
            cached = self._clients.get(endpoint.node_id)
            previous = self._endpoints.get(endpoint.node_id)
            if cached is not None and previous == endpoint:
                return cached
            if cached is not None:
                self._close_unlocked(endpoint.node_id)
            factory = self._factories.get(endpoint.transport)
            if factory is None:
                raise DockerUnavailable(f"Unsupported transport: {endpoint.transport}")
            try:
                client = factory(endpoint)
            except DockerException as exc:
                raise DockerUnavailable(f"Could not connect to Docker on node {endpoint.node_id}: {exc}") from exc
            self._clients[endpoint.node_id] = client
            self._endpoints[endpoint.node_id] = endpoint
            return client

    def drop(self, node_id: int) -> None:
        with self._lock:
            self._close_unlocked(node_id)

    def _close_unlocked(self, node_id: int) -> None:
        client = self._clients.pop(node_id, None)
        self._endpoints.pop(node_id, None)
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover
                logger.debug("error closing docker client for node %s", node_id, exc_info=True)

    def close_all(self) -> None:
        with self._lock:
            for node_id in list(self._clients):
                self._close_unlocked(node_id)

    # ── diagnostics ─────────────────────────────────────────────────────────
    def ping(self, endpoint: Endpoint) -> Tuple[bool, str]:
        client = self.get(endpoint)
        try:
            client.ping()
            return True, "pong"
        except Exception as exc:
            return False, str(exc)

    def version(self, endpoint: Endpoint) -> Dict[str, Any]:
        return dict(self.get(endpoint).version())

    def info(self, endpoint: Endpoint) -> Dict[str, Any]:
        return dict(self.get(endpoint).info())


#: Process-wide pool.  Reset it with :func:`reset_pool` after node edits.
POOL = DockerClientPool()


def reset_pool(node_id: Optional[int] = None) -> None:
    """Drop cached clients (after a node was edited or removed)."""
    if node_id is None:
        POOL.close_all()
    else:
        POOL.drop(int(node_id))


def client_for(endpoint: Endpoint) -> Any:
    return POOL.get(endpoint)
