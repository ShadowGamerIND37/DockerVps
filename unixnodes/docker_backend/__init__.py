"""
Docker Engine backend.

Public surface used by the Discord layer and the services:

=========================  ==================================================
``client``                 connection pool, transports, endpoint validation
``images``                 catalogue, pulling, approved custom images
``containers``             create / start / stop / restart / remove / exec
``networking``             networks, addresses, published-port verification
``ports``                  reservation ledger + publish spec
``volumes``                persistent VPS volumes
``resources``              cgroup limits + real XFS/Btrfs disk quotas
``monitoring``             container stats, node stats, host stats
``backups``                volume archives, metadata, restore, retention
``ssh``                    guest init, root credential, SSH verification
``nodes``                  node health, capacity, placement
``security``               hardening policy and input validation
=========================  ==================================================
"""

from __future__ import annotations

from . import (
    backups,
    client,
    containers,
    images,
    monitoring,
    networking,
    nodes,
    ports,
    resources,
    security,
    ssh,
    volumes,
)
from .client import Endpoint, POOL, DockerClientPool, reset_pool, validate_remote_endpoint
from .containers import (
    create,
    exists,
    exec_argv,
    exec_script,
    find_container,
    get_container,
    list_managed,
    logs,
    remove,
    restart,
    simple_state,
    start,
    state,
    stop,
)
from .ports import PortAllocator, Reservation
from .resources import QuotaSupportReport, detect_quota_support

__all__ = [
    "backups", "client", "containers", "images", "monitoring", "networking",
    "nodes", "ports", "resources", "security", "ssh", "volumes",
    "Endpoint", "POOL", "DockerClientPool", "reset_pool", "validate_remote_endpoint",
    "PortAllocator", "Reservation", "QuotaSupportReport", "detect_quota_support",
    "create", "exists", "exec_argv", "exec_script", "find_container", "get_container",
    "list_managed", "logs", "remove", "restart", "simple_state", "start", "state", "stop",
]
