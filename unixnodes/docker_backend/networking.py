"""
Docker networking helpers: the customer network, container addresses and
interface inspection.

Customer containers live on a normal Docker bridge.  They get outbound internet
through NAT, they can reach each other only if an administrator puts them on a
shared custom network, and the host is reachable through the bridge gateway —
never through the Docker API, which stays on the host socket / mTLS listener.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .. import config

logger = logging.getLogger("unixnodes.docker.networking")


def ensure_network(client: Any, name: str = "") -> Optional[Dict[str, Any]]:
    """
    Create the customer network if it does not exist yet.

    The default ``bridge`` network already exists on every Docker install, so
    this only does work when a dedicated network name is configured.
    """
    name = (name or config.DOCKER_DEFAULT_NETWORK or "bridge").strip()
    if name in ("bridge", "host", "none"):
        return {"Name": name, "Driver": "bridge", "pre_existing": True}
    try:
        existing = client.networks.get(name)
        return dict(existing.attrs)
    except Exception:
        pass
    if not config.DOCKER_CREATE_NETWORK:
        raise RuntimeError(f"Docker network '{name}' does not exist and DOCKER_CREATE_NETWORK is off")
    kwargs: Dict[str, Any] = {"name": name, "driver": "bridge",
                              "labels": {config.LABEL_MANAGED: "true"}, "check_duplicate": False}
    if config.DOCKER_NETWORK_SUBNET:
        from docker.types import IPAMConfig, IPAMPool

        kwargs["ipam"] = IPAMConfig(pool_configs=[IPAMPool(subnet=config.DOCKER_NETWORK_SUBNET)])
    network = client.networks.create(**kwargs)
    logger.info("created docker network %s", name)
    return dict(network.attrs)


def list_networks(client: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for network in client.networks.list():
        attrs = dict(network.attrs or {})
        ipam = (attrs.get("IPAM") or {}).get("Config") or [{}]
        out.append({
            "name": attrs.get("Name"),
            "driver": attrs.get("Driver"),
            "subnet": ipam[0].get("Subnet") if ipam else None,
            "gateway": ipam[0].get("Gateway") if ipam else None,
            "internal": bool(attrs.get("Internal")),
            "containers": len(attrs.get("Containers") or {}),
        })
    return out


def container_addresses(container: Any) -> Dict[str, str]:
    """Map ``network name → IPv4 address`` for a container object."""
    attrs = dict(getattr(container, "attrs", {}) or {})
    settings = attrs.get("NetworkSettings") or {}
    networks = settings.get("Networks") or {}
    out: Dict[str, str] = {}
    for name, info in networks.items():
        address = str(info.get("IPAddress") or "")
        if address:
            out[str(name)] = address
    if not out:
        legacy = str(settings.get("IPAddress") or "")
        if legacy:
            out["default"] = legacy
    return out


def container_ip(container: Any) -> Optional[str]:
    addresses = container_addresses(container)
    preferred = config.DOCKER_DEFAULT_NETWORK
    if preferred in addresses:
        return addresses[preferred]
    for key in ("bridge", "default"):
        if key in addresses:
            return addresses[key]
    return next(iter(addresses.values()), None)


def published_ports(container: Any) -> Dict[str, List[Dict[str, str]]]:
    """
    The ports Docker actually bound for a container.

    Shaped like ``{"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "22014"}]}``.
    """
    attrs = dict(getattr(container, "attrs", {}) or {})
    ports = ((attrs.get("NetworkSettings") or {}).get("Ports")) or {}
    return {str(key): [dict(item) for item in (value or [])] for key, value in ports.items()}


def verify_binding(container: Any, container_port: int, protocol: str,
                   expected_host_port: int, publish_ip: str = "") -> bool:
    """
    Confirm Docker really published ``container_port`` on ``expected_host_port``.

    Publishing can silently fail when another process already owns the port, so
    every provisioning step verifies the binding instead of trusting the
    ``docker run`` return value.
    """
    key = f"{int(container_port)}/{protocol}"
    bindings = published_ports(container).get(key) or []
    for binding in bindings:
        if str(binding.get("HostPort")) == str(int(expected_host_port)):
            if not publish_ip or publish_ip in ("0.0.0.0", "::"):
                return True
            if str(binding.get("HostIp")) in (publish_ip, "0.0.0.0", "::"):
                return True
    return False


def network_usage(container: Any) -> Dict[str, Dict[str, int]]:
    """Per-interface RX/TX byte counters from the container network namespace."""
    try:
        output = container.exec_run(
            ["sh", "-c", "cat /proc/net/dev 2>/dev/null | tail -n +3"],
            demux=True,
        )
        stdout = (output.output[0] if isinstance(output.output, tuple) else output.output) or b""
    except Exception:
        return {}
    result: Dict[str, Dict[str, int]] = {}
    for line in stdout.decode("utf-8", "replace").splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 9:
            continue
        name = name.strip()
        if name == "lo":
            continue
        try:
            result[name] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
        except ValueError:
            continue
    return result
