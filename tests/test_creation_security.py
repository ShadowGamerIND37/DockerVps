"""Admin-only KVM and AppArmor creation policy."""

from __future__ import annotations

import pytest

from tgdbot import config
from tgdbot.docker_backend import containers as ctr
from tgdbot.docker_backend import security
from tgdbot.errors import ProvisioningError, SecurityPolicyError
from tgdbot.services import lifecycle, provisioning


def request(**overrides):
    values = dict(
        owner_id=111, requester_id=111, container_name="security-vps-1",
        image="ubuntu:24.04", ram_mb=1024, cpu=1.0, storage_gb=10,
        node_id=None, pids=256, expiry_days=30,
    )
    values.update(overrides)
    return provisioning.ProvisionRequest(**values)


def host_config(**overrides):
    values = dict(
        container_name="security-vps-1", image="ubuntu:24.04", ram_mb=1024,
        cpu=1.0, pids=256, publish_ip="0.0.0.0", published_ports=[],
        volumes={},
    )
    values.update(overrides)
    return ctr.build_container_spec(**values)


def test_explicit_kvm_and_apparmor_options_reach_docker_payload():
    enabled = host_config(kvm_enabled=True, apparmor_enabled=True)
    assert enabled["devices"] == ["/dev/kvm:/dev/kvm"]
    assert "apparmor=docker-default" in enabled["security_opt"]

    disabled = host_config(kvm_enabled=False, apparmor_enabled=False)
    assert disabled["devices"] == []
    assert "apparmor=unconfined" in disabled["security_opt"]
    assert not any(item.startswith("apparmor=docker-default")
                   for item in disabled["security_opt"])


def test_enabled_apparmor_cannot_be_silently_unconfined(monkeypatch):
    monkeypatch.setattr(config, "DOCKER_APPARMOR_PROFILE", "unconfined")
    with pytest.raises(SecurityPolicyError):
        security.apparmor_security_opt(True)


def test_kvm_disabled_cannot_be_created_privileged(monkeypatch):
    monkeypatch.setattr(config, "DOCKER_PRIVILEGED", True)
    with pytest.raises(SecurityPolicyError):
        host_config(kvm_enabled=False, apparmor_enabled=False)


def test_non_admin_security_overrides_are_rejected_before_docker_mutation(
        repo, fake_node, engine):
    for field in ("kvm_enabled", "apparmor_enabled"):
        with pytest.raises(ProvisioningError) as caught:
            provisioning.provision(repo, request(**{field: False}))
        assert "Only administrators" in str(caught.value)
    assert engine.containers == {}
    assert engine.volumes == {}
    assert repo.active_ports_for_container("security-vps-1") == []


def test_non_admin_defaults_are_safe_and_persisted(repo, fake_node, no_ssh_probe, engine):
    result = provisioning.provision(repo, request())
    assert result.kvm_enabled is False
    assert result.apparmor_enabled is True
    host = engine.containers["security-vps-1"].attrs["HostConfig"]
    assert host["Devices"] == []
    assert "apparmor=docker-default" in host["SecurityOpt"]
    row = repo.get_vps_by_container("security-vps-1")
    assert row["kvm_enabled"] is False
    assert row["apparmor_enabled"] is True


def test_admin_security_choices_are_persisted_and_survive_recreate(
        repo, fake_node, no_ssh_probe, engine, monkeypatch):
    # The fake host has no real /dev/kvm; the payload test above covers the
    # mapping, while this test focuses on persistence and lifecycle reuse.
    monkeypatch.setattr(security, "assert_kvm_available", lambda **_kwargs: None)
    result = provisioning.provision(repo, request(
        requester_admin=True, kvm_enabled=True, apparmor_enabled=False))
    assert result.kvm_enabled is True
    assert result.apparmor_enabled is False
    host = engine.containers["security-vps-1"].attrs["HostConfig"]
    assert host["Devices"] == ["/dev/kvm:/dev/kvm"]
    assert "apparmor=unconfined" in host["SecurityOpt"]

    lifecycle.recreate(repo, repo.get_vps_by_container("security-vps-1"))
    rebuilt = engine.containers["security-vps-1"].attrs["HostConfig"]
    assert rebuilt["Devices"] == host["Devices"]
    assert rebuilt["SecurityOpt"] == host["SecurityOpt"]
    row = repo.get_vps_by_container("security-vps-1")
    assert row["kvm_enabled"] is True
    assert row["apparmor_enabled"] is False


def test_kvm_disabled_proxmox_is_rejected_by_policy():
    with pytest.raises(SecurityPolicyError):
        security.resolve_creation_security(
            workload=config.WORKLOAD_PROXMOX, requester_admin=True,
            kvm_enabled=False, apparmor_enabled=False)


def test_record_defaults_match_the_workload():
    assert security.security_from_record({}, config.WORKLOAD_VPS) == (False, True)
    assert security.security_from_record({}, config.WORKLOAD_PROXMOX) == (True, False)
    assert security.security_from_record(
        {"kvm_enabled": 0, "apparmor_enabled": 1}, config.WORKLOAD_PROXMOX) == (False, True)
    assert security.security_from_record(
        {"kvm_enabled": "0", "apparmor_enabled": "1"}, config.WORKLOAD_VPS) == (False, True)
