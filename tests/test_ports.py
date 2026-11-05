"""
Host port allocation.

Covers the DB-backed reservation ledger: TCP and UDP separately, ranges,
conflict detection, the SSH pool versus the user pool, release/history, and the
publish spec handed to Docker.

``PortAllocator.reserve()`` returns a list of ``Reservation`` rows — one per
protocol — because ``protocol='both'`` legitimately creates two.
"""

from __future__ import annotations

import pytest

from unixnodes import config
from unixnodes.docker_backend.ports import PURPOSE_SSH, PURPOSE_USER, PortAllocator
from unixnodes.errors import NoFreePort, PortConflict


@pytest.fixture()
def allocator(repo, fake_node):
    return PortAllocator(repo, int(fake_node["id"]), "0.0.0.0")


def one(allocator, container, port, **kwargs) -> int:
    """Reserve a single-protocol port and return the host port."""
    rows = allocator.reserve(container, port, **kwargs)
    assert len(rows) == 1, rows
    return int(rows[0].host_port)


def test_ssh_port_comes_from_the_ssh_pool(allocator):
    port = one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    assert config.DOCKER_SSH_PORT_START <= port <= config.DOCKER_SSH_PORT_END


def test_user_port_comes_from_the_user_pool(allocator):
    port = one(allocator, "alice-vps-1", 8080, purpose=PURPOSE_USER, protocol="tcp")
    assert config.DOCKER_PORT_RANGE_START <= port <= config.DOCKER_PORT_RANGE_END


def test_protocol_both_creates_two_reservations(allocator):
    rows = allocator.reserve("alice-vps-1", 8080, protocol="both")
    assert len(rows) == 2
    assert {row.protocol for row in rows} == {"tcp", "udp"}
    assert rows[0].host_port == rows[1].host_port


def test_two_vps_never_share_a_host_port(allocator):
    first = one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    second = one(allocator, "bob-vps-2", 22, purpose=PURPOSE_SSH, protocol="tcp")
    assert first != second


def test_tcp_and_udp_can_share_a_host_port(allocator):
    """The ledger keys on (node, ip, port, protocol), so UDP may reuse a TCP port."""
    tcp = one(allocator, "alice-vps-1", 53, purpose=PURPOSE_USER, protocol="tcp")
    udp = one(allocator, "alice-vps-1", 53, purpose=PURPOSE_USER, protocol="udp",
              host_port=tcp)
    assert udp == tcp
    assert {row["protocol"] for row in allocator.active("alice-vps-1")} == {"tcp", "udp"}


def test_duplicate_reservation_is_rejected(allocator):
    port = one(allocator, "alice-vps-1", 8080, protocol="tcp")
    with pytest.raises(PortConflict):
        allocator.reserve("bob-vps-2", 9090, protocol="tcp", host_port=port)


def test_conflicting_reservation_leaves_nothing_behind(allocator):
    port = one(allocator, "alice-vps-1", 8080, protocol="tcp")
    with pytest.raises(PortConflict):
        allocator.reserve("bob-vps-2", 9090, protocol="tcp", host_port=port)
    assert allocator.active("bob-vps-2") == [], "the failed reservation leaked a row"


def test_explicit_host_port_is_honoured(allocator):
    wanted = config.DOCKER_PORT_RANGE_START + 7
    assert one(allocator, "alice-vps-1", 3000, protocol="tcp", host_port=wanted) == wanted


def test_reserved_ports_are_never_reused(allocator):
    ports = {one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
             for _ in range(25)}
    assert len(ports) == 25, "the allocator handed out a duplicate SSH port"


def test_release_makes_the_port_available_again(allocator):
    port = one(allocator, "alice-vps-1", 8080, protocol="tcp")
    rows = allocator.active("alice-vps-1")
    assert allocator.release_by_id(int(rows[0]["id"])) is True
    assert allocator.active("alice-vps-1") == []
    assert one(allocator, "alice-vps-1", 8080, protocol="tcp", host_port=port) == port


def test_release_all_drops_every_reservation(allocator):
    one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    one(allocator, "alice-vps-1", 80, protocol="tcp")
    one(allocator, "alice-vps-1", 443, protocol="tcp")
    assert len(allocator.active("alice-vps-1")) == 3
    assert allocator.release_all("alice-vps-1") == 3
    assert allocator.active("alice-vps-1") == []


def test_release_by_container_port_only_drops_that_port(allocator):
    one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    one(allocator, "alice-vps-1", 80, protocol="tcp")
    assert allocator.release_container_port("alice-vps-1", 80) == 1
    remaining = allocator.active("alice-vps-1")
    assert len(remaining) == 1
    assert int(remaining[0]["container_port"]) == 22


def test_history_is_kept_after_release(allocator):
    one(allocator, "alice-vps-1", 8080, protocol="tcp")
    allocator.release_all("alice-vps-1")
    history = allocator.history("alice-vps-1")
    assert len(history) == 1
    assert history[0]["status"] == "released"


def test_ssh_port_lookup(allocator):
    port = one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    assert allocator.ssh_port("alice-vps-1") == port
    assert allocator.ssh_port("nobody") is None


def test_publish_spec_shape(allocator):
    one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    one(allocator, "alice-vps-1", 8080, protocol="tcp")
    one(allocator, "alice-vps-1", 5353, protocol="udp")
    spec = allocator.publish_spec("alice-vps-1")
    assert len(spec) == 3
    pairs = {(container_port, protocol) for _host, container_port, protocol in spec}
    assert pairs == {(22, "tcp"), (8080, "tcp"), (5353, "udp")}
    for host_port, _container_port, _protocol in spec:
        assert isinstance(host_port, int)


def test_mark_bound_moves_the_ledger_state(allocator):
    one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    assert allocator.mark_bound("alice-vps-1") == 1
    assert all(row["status"] == "bound" for row in allocator.active("alice-vps-1"))


def test_blacklisted_port_is_never_handed_out(allocator, monkeypatch):
    probe = allocator.pick_free_port(PURPOSE_USER)
    monkeypatch.setattr(config, "DOCKER_RESERVED_PORTS", [probe], raising=False)
    fresh = PortAllocator(allocator.repo, allocator.node_id, allocator.publish_ip)
    for _ in range(50):
        assert fresh.pick_free_port(PURPOSE_USER) != probe


def test_invalid_range_raises(repo, fake_node, monkeypatch):
    monkeypatch.setattr(config, "DOCKER_PORT_RANGE_START", 25000, raising=False)
    monkeypatch.setattr(config, "DOCKER_PORT_RANGE_END", 25000, raising=False)
    allocator = PortAllocator(repo, int(fake_node["id"]), "0.0.0.0")
    with pytest.raises(NoFreePort):
        allocator.pick_free_port(PURPOSE_USER)


def test_exhausted_pool_raises_instead_of_hanging(repo, fake_node, monkeypatch):
    """With no free port left the allocator must fail loudly, not loop forever."""
    monkeypatch.setattr(config, "DOCKER_PORT_RANGE_START", 25000, raising=False)
    monkeypatch.setattr(config, "DOCKER_PORT_RANGE_END", 25001, raising=False)
    allocator = PortAllocator(repo, int(fake_node["id"]), "0.0.0.0")
    one(allocator, "a", 80, protocol="tcp")
    one(allocator, "b", 81, protocol="tcp")
    with pytest.raises(NoFreePort):
        allocator.pick_free_port(PURPOSE_USER, attempts=5)


def test_summary_reports_counts(allocator):
    ssh = one(allocator, "alice-vps-1", 22, purpose=PURPOSE_SSH, protocol="tcp")
    one(allocator, "alice-vps-1", 8080, protocol="tcp")
    summary = allocator.summary("alice-vps-1")
    assert summary["count"] == 2
    assert summary["ssh_port"] == ssh
    assert summary["publish_ip"] == "0.0.0.0"
    assert len(summary["rows"]) == 2


def test_reservation_docker_key(allocator):
    rows = allocator.reserve("alice-vps-1", 8080, protocol="tcp")
    assert rows[0].docker_key == "8080/tcp"
    assert rows[0].as_dict()["container_name"] == "alice-vps-1"
