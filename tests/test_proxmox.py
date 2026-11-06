"""Grant-gated Proxmox provisioning and its isolated runtime contract."""

from __future__ import annotations

import asyncio

import pytest

from tgdbot import config
from tgdbot.docker_backend import images, proxmox, security
from tgdbot.errors import (BackupError, ContainerError, ImageNotApproved,
                           ProvisioningError, SecurityPolicyError)
from tgdbot.services import backup_service, lifecycle, provisioning


@pytest.fixture()
def proxmox_ready(fake_node, engine, monkeypatch, no_ssh_probe):
    engine.images[config.PROXMOX_IMAGE] = "sha256:proxmox"
    monkeypatch.setattr(proxmox, "preflight", lambda **_kwargs: "KVM/FUSE ready (test)")
    monkeypatch.setattr(proxmox, "password_hash", lambda _password: "$6$testsalt$testhash")
    monkeypatch.setattr(proxmox, "verify_web", lambda *_args, **_kwargs: (True, "HTTPS ready"))
    return fake_node


def request(**overrides):
    values = dict(owner_id=111, requester_id=111, container_name="pve-vps-1",
                  image=config.PROXMOX_IMAGE, ram_mb=4096, cpu=2.0,
                  storage_gb=40, node_id=None, pids=256, expiry_days=30)
    values.update(overrides)
    return provisioning.ProvisionRequest(**values)


def test_ungranted_user_is_rejected_before_any_docker_mutation(repo, proxmox_ready, engine):
    with pytest.raises(ProvisioningError) as caught:
        provisioning.provision(repo, request())
    assert "granted Proxmox access" in str(caught.value)
    assert engine.containers == {}
    assert engine.volumes == {}
    assert repo.active_ports_for_container("pve-vps-1") == []
    assert repo.get_vps_by_container("pve-vps-1") is None


def test_admin_can_provision_without_a_grant_row(repo, proxmox_ready, engine):
    result = provisioning.provision(repo, request(requester_admin=True))
    assert result.workload == config.WORKLOAD_PROXMOX
    assert result.container_port == 8006
    assert result.access_verified is True
    assert result.ssh_verified is False
    assert result.web_console_url("node.example") == (
        f"https://node.example:{result.ssh_host_port}")
    assert not repo.has_proxmox_grant(111)

    container = engine.containers["pve-vps-1"]
    host = container.attrs["HostConfig"]
    assert host["Privileged"] is True
    assert host["CapDrop"] == []
    assert host["Devices"] == ["/dev/kvm:/dev/kvm"]
    assert "apparmor=unconfined" in host["SecurityOpt"]
    assert host["PidsLimit"] == proxmox.MIN_PIDS
    assert container.kwargs["stop_signal"] == "SIGRTMIN+3"
    assert container.kwargs.get("command") is None
    assert container.kwargs["environment"]["PASSWORD_HASH"] == "$6$testsalt$testhash"
    assert "PASSWORD" not in container.kwargs["environment"]
    assert result.password not in repr(container.kwargs)

    mounts = {mount["Destination"]: mount["Name"] for mount in container.attrs["Mounts"]}
    assert mounts == {
        "/var/lib/vz": result.volume_name,
        "/var/lib/pve-cluster": proxmox.cluster_volume_name("pve-vps-1"),
    }
    assert set(mounts.values()).issubset(engine.volumes)
    ports = container.attrs["NetworkSettings"]["Ports"]
    assert ports["8006/tcp"][0]["HostPort"] == str(result.ssh_host_port)
    assert "22/tcp" not in ports


def test_explicit_grant_allows_a_non_admin_and_revoke_removes_access(repo, proxmox_ready):
    repo.grant_proxmox(111, granted_by=999)
    assert repo.has_proxmox_grant(111)
    result = provisioning.provision(repo, request())
    assert result.workload == config.WORKLOAD_PROXMOX
    assert repo.revoke_proxmox(111)
    assert not repo.has_proxmox_grant(111)


def test_regular_vps_never_inherits_proxmox_privileges(repo, proxmox_ready, engine):
    result = provisioning.provision(repo, request(
        container_name="alice-vps-1", image="ubuntu:24.04", ram_mb=1024,
        cpu=1.0, storage_gb=10, requester_admin=False))
    container = engine.containers["alice-vps-1"]
    assert result.workload == config.WORKLOAD_VPS
    assert container.attrs["HostConfig"]["Privileged"] is False
    assert "SYS_ADMIN" in container.attrs["HostConfig"]["CapDrop"]
    assert container.kwargs["command"] is not None
    assert all(m["Destination"] not in ("/var/lib/vz", "/var/lib/pve-cluster")
               for m in container.attrs["Mounts"])


def test_proxmox_minimum_plan_is_enforced(repo, proxmox_ready):
    with pytest.raises(ProvisioningError) as caught:
        provisioning.provision(repo, request(requester_admin=True, ram_mb=1024))
    assert "at least 2 GB RAM" in str(caught.value)


def test_failed_proxmox_create_rolls_back_both_volumes(repo, proxmox_ready, engine):
    engine.fail_at("containers.create")
    with pytest.raises(ProvisioningError):
        provisioning.provision(repo, request(requester_admin=True))
    assert "pve-vps-1" not in engine.containers
    assert f"{config.DOCKER_VOLUME_PREFIX}pve-vps-1" not in engine.volumes
    assert proxmox.cluster_volume_name("pve-vps-1") not in engine.volumes
    assert repo.active_ports_for_container("pve-vps-1") == []


def test_custom_proxmox_image_cannot_bypass_builtin_grant(repo):
    reference = "example/unreviewed-proxmox:latest"
    repo.add_image(reference, label="Unreviewed")
    assert reference not in {item["value"] for item in images.catalogue(repo.list_approved_images())}
    with pytest.raises(SecurityPolicyError):
        images.resolve(reference, repo.list_approved_images())
    with pytest.raises(ImageNotApproved):
        images.resolve("example/unapproved-linux:latest", repo.list_approved_images())


def test_password_hash_uses_stdin_not_process_arguments(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "$6$abcdefghijklmnop$hashed\n"
        stderr = ""

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(proxmox.subprocess, "run", run)
    hashed = proxmox.password_hash("correct horse battery staple")
    assert hashed.startswith("$6$")
    argv, kwargs = calls[0]
    assert "-stdin" in argv
    assert "correct horse battery staple" not in " ".join(argv)
    assert kwargs["input"] == "correct horse battery staple\n"


def create_proxmox(repo):
    result = provisioning.provision(repo, request(requester_admin=True))
    return result, repo.get_vps_by_container("pve-vps-1")


def test_rebuild_preserves_native_runtime_and_both_volumes(repo, proxmox_ready, engine):
    _result, row = create_proxmox(repo)
    outcome = lifecycle.rebuild(repo, row)
    assert outcome["volume_kept"] is True
    container = engine.containers["pve-vps-1"]
    assert container.attrs["HostConfig"]["Privileged"] is True
    assert container.kwargs.get("command") is None
    assert container.kwargs["stop_signal"] == "SIGRTMIN+3"
    assert {m["Destination"] for m in container.attrs["Mounts"]} == {
        "/var/lib/vz", "/var/lib/pve-cluster"}
    assert proxmox.cluster_volume_name("pve-vps-1") in engine.volumes


def test_reinstall_recreates_both_volumes_and_hashes_new_password(repo, proxmox_ready, engine):
    _result, row = create_proxmox(repo)
    primary = f"{config.DOCKER_VOLUME_PREFIX}pve-vps-1"
    cluster = proxmox.cluster_volume_name("pve-vps-1")
    engine.volumes[primary]["test_marker"] = "old-data"
    engine.volumes[cluster]["test_marker"] = "old-cluster"
    outcome = lifecycle.reinstall(repo, row, image=config.PROXMOX_IMAGE,
                                  new_password="New-Secret-123!")
    assert outcome["volume_kept"] is False
    assert "test_marker" not in engine.volumes[primary]
    assert "test_marker" not in engine.volumes[cluster]
    assert engine.containers["pve-vps-1"].kwargs["environment"]["PASSWORD_HASH"].startswith("$6$")
    assert "New-Secret-123!" not in repr(engine.containers["pve-vps-1"].kwargs)


def test_rebuild_refuses_to_replace_a_missing_cluster_volume(repo, proxmox_ready, engine):
    _result, row = create_proxmox(repo)
    old_container = engine.containers["pve-vps-1"]
    engine.volumes.pop(proxmox.cluster_volume_name("pve-vps-1"))
    with pytest.raises(Exception) as caught:
        lifecycle.rebuild(repo, row)
    assert "persistent volume" in str(caught.value)
    assert engine.containers["pve-vps-1"] is old_container


def test_workload_conversion_is_blocked(repo, proxmox_ready, engine):
    _result, row = create_proxmox(repo)
    with pytest.raises(ContainerError):
        lifecycle.reinstall(repo, row, image="ubuntu:24.04")
    assert engine.containers["pve-vps-1"].kwargs["image"] == config.PROXMOX_IMAGE


def test_delete_removes_both_proxmox_volumes(repo, proxmox_ready, engine):
    _result, row = create_proxmox(repo)
    outcome = lifecycle.delete(repo, row, delete_volume=True)
    assert outcome["errors"] == []
    assert f"{config.DOCKER_VOLUME_PREFIX}pve-vps-1" not in engine.volumes
    assert proxmox.cluster_volume_name("pve-vps-1") not in engine.volumes


def test_incomplete_single_volume_backup_and_clone_are_blocked(repo, proxmox_ready, engine):
    _result, row = create_proxmox(repo)
    before = list(engine.calls)
    with pytest.raises(BackupError):
        backup_service.create(repo, row)
    with pytest.raises(ContainerError):
        lifecycle.clone(repo, row, "pve-copy", owner_id=111)
    assert engine.calls == before


def test_stopped_proxmox_password_is_not_falsely_reported_as_staged(repo, proxmox_ready):
    _result, row = create_proxmox(repo)
    lifecycle.stop(repo, row)
    outcome = lifecycle.change_password(repo, row, "Another-Secret-123!")
    assert outcome["ok"] is False
    assert "Start the Proxmox VPS" in outcome["note"]


def test_platform_image_add_pulls_returns_entry_and_refreshes_catalogue(db_path, fake_node, engine):
    from tgdbot.platform import Platform

    async def scenario():
        platform = Platform(db_path)
        entry = await platform.add_image("example/debian-custom:latest", label="Debian Custom", added_by=999)
        assert entry["value"] == "example/debian-custom:latest"
        assert entry["label"] == "Debian Custom"
        assert "example/debian-custom:latest" in engine.images
        options = await platform.image_options(user_id=111)
        assert "example/debian-custom:latest" in {item["value"] for item in options}
        assert config.PROXMOX_IMAGE not in {item["value"] for item in options}
        platform.repo.grant_proxmox(111, granted_by=999)
        granted = await platform.image_options(user_id=111)
        assert config.PROXMOX_IMAGE in {item["value"] for item in granted}
        admin = await platform.image_options(user_id=222, is_admin=True)
        assert config.PROXMOX_IMAGE in {item["value"] for item in admin}
        await platform.close()

    asyncio.run(scenario())


def test_platform_image_add_rejects_non_linux_before_recording(db_path, fake_node, monkeypatch):
    from tgdbot.docker_backend import images as images_mod
    from tgdbot.errors import ImageError
    from tgdbot.platform import Platform

    monkeypatch.setattr(images_mod, "ensure_image", lambda *_args, **_kwargs: {"Os": "windows"})

    async def scenario():
        platform = Platform(db_path)
        with pytest.raises(ImageError):
            await platform.add_image("example/windows:latest")
        assert "example/windows:latest" not in {
            row["reference"] for row in platform.repo.list_images()}
        await platform.close()

    asyncio.run(scenario())


def test_os_options_for_checks_current_grant_and_admin(monkeypatch):
    import bot

    entries = [
        {"value": "ubuntu:24.04", "label": "Ubuntu"},
        {"value": config.PROXMOX_IMAGE, "label": "Proxmox"},
    ]
    monkeypatch.setattr(bot, "refresh_os_options", lambda: entries)
    monkeypatch.setattr(bot, "is_admin_id", lambda user_id: user_id == 999)
    monkeypatch.setattr(bot.INFRA.repo, "has_proxmox_grant", lambda user_id: user_id == 42)
    assert bot.os_options_for(42) == entries
    assert bot.os_options_for(43) == [entries[0]]
    assert bot.os_options_for(999) == entries
