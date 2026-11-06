"""Persistent-storage layout and writable-layer lifecycle regressions."""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from pathlib import Path

import pytest

from tgdbot import config
from tgdbot.errors import BackupError, ContainerError
from tgdbot.docker_backend import backups as backups_mod
from tgdbot.docker_backend import containers as ctr
from tgdbot.docker_backend import nodes as nodes_mod
from tgdbot.docker_backend import volumes
from tgdbot.services import backup_service, lifecycle, provisioning
from tests.fake_docker import DockerAPIError


def _request(container="alice-vps-1", **overrides):
    values = dict(
        owner_id=111, container_name=container, image="ubuntu:24.04",
        ram_mb=1024, cpu=1.0, storage_gb=10, node_id=None, pids=256,
        expiry_days=30, is_public=False,
    )
    values.update(overrides)
    return provisioning.ProvisionRequest(**values)


def test_engine_rejects_root_volume_destination(docker_client):
    with pytest.raises(DockerAPIError, match="destination can't be '/'"):
        docker_client.containers.create(
            "ubuntu:24.04", name="invalid-root-mount",
            volumes={"tgdbot_invalid": {"bind": "/", "mode": "rw"}},
        )
    assert "invalid-root-mount" not in docker_client._engine.containers


def test_recreate_uses_snapshot_and_keeps_base_image_in_database(
        repo, fake_node, no_ssh_probe, engine):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    original = engine.containers[name]

    lifecycle.recreate(repo, repo.get_vps_by_container(name), ram_mb=2048)

    replacement = engine.containers[name]
    assert replacement is not original
    assert replacement.kwargs["image"] == ctr.snapshot_reference(name)
    assert replacement.kwargs["mem_limit"] == 2048 * 1024 * 1024
    assert any(m["Destination"] == volumes.MOUNT_POINT
               for m in replacement.attrs["Mounts"])
    assert all(m["Destination"] != "/" for m in replacement.attrs["Mounts"])
    assert repo.get_vps_by_container(name)["docker_image"] == "ubuntu:24.04"
    assert any(kind == "commit" for kind, _detail in engine.calls)


def test_recreate_retry_reuses_snapshot_after_create_failure(
        repo, fake_node, no_ssh_probe, engine):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    engine.fail_at("containers.create", "injected replacement failure")

    with pytest.raises(Exception, match="Could not create container"):
        lifecycle.recreate(repo, repo.get_vps_by_container(name))
    assert name not in engine.containers
    assert ctr.snapshot_reference(name) in engine.images

    engine.clear_failures()
    lifecycle.recreate(repo, repo.get_vps_by_container(name))
    assert engine.containers[name].kwargs["image"] == ctr.snapshot_reference(name)


def test_snapshot_export_retains_reference_for_migration(docker_client):
    docker_client.containers.create("ubuntu:24.04", name="source-vps")
    snapshot = ctr.commit_snapshot(docker_client, "source-vps")
    assert snapshot is not None

    payload = ctr.save_snapshot(docker_client, snapshot["reference"])
    # Use a separate fake engine without relying on Docker daemon availability.
    from tests.fake_docker import make_client
    target = make_client()
    ctr.load_snapshot(target, payload)
    assert target.images.get(snapshot["reference"]).id == snapshot["id"]


def test_backup_restore_reloads_rootfs_and_recreates(
        repo, fake_node, no_ssh_probe, engine, monkeypatch):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    backup_dir = Path(str(config.DOCKER_BACKUP_DIR))

    def helper(_client, command, *, volumes, timeout=1800, name_hint="backup"):
        text = " ".join(str(part) for part in command)
        if "tar -czf" in text:
            match = re.search(r"/backup/([^\s]+\.tar\.gz)", text)
            assert match, text
            path = backup_dir / match.group(1)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = b"fake volume archive"
            path.write_bytes(payload)
            return 0, f"ARCHIVE_OK\n{hashlib.sha256(payload).hexdigest()}\n", ""
        if "tar -tzf" in text:
            return 0, "./\n1\nVERIFY_OK\n", ""
        if "find . -mindepth" in text:
            return 0, "WIPED\n", ""
        if "tar -xzf" in text:
            return 0, "RESTORE_OK\n", ""
        return 0, "", ""

    monkeypatch.setattr(backups_mod, "run_helper", helper)
    vps = repo.get_vps_by_container(name)
    metadata = backup_service.create(repo, vps)
    assert Path(metadata["rootfs_archive"]).is_file()
    assert metadata["rootfs_size_bytes"] > 0
    assert metadata["configuration"]["image"] == "ubuntu:24.04"

    # Force restore to use the companion archive rather than an image already
    # cached on the node.
    snapshot_ref = ctr.snapshot_reference(name)
    engine.images.pop(snapshot_ref, None)
    restored = backup_service.restore(repo, repo.get_vps_by_container(name),
                                      metadata["name"])

    assert restored["rootfs_restored"] is True
    assert restored["container_recreated"] is True
    assert snapshot_ref in engine.images
    assert engine.containers[name].kwargs["image"] == snapshot_ref
    assert repo.get_vps_by_container(name)["docker_image"] == "ubuntu:24.04"

    # Retention removes both artifacts, and disk accounting includes rootfs.
    assert backups_mod.disk_usage()["bytes"] >= metadata["rootfs_size_bytes"]
    assert backups_mod.delete_backup(None, None, metadata["name"], node_is_local=True)
    assert not Path(metadata["archive"]).exists()
    assert not Path(metadata["rootfs_archive"]).exists()


def test_missing_required_rootfs_fails_before_stop_or_wipe(
        repo, fake_node, no_ssh_probe, docker_client, engine):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    original = engine.containers[name]
    if original.status != "running":
        original.start()

    with pytest.raises(BackupError, match="companion is missing"):
        backups_mod.restore_backup(
            docker_client, name, archive="missing.tar.gz", node_is_local=True,
            volume_name=result.volume_name, rootfs_required=True)

    assert engine.containers[name] is original
    assert original.status == "running"
    assert not any(kind == "stop" and detail == name for kind, detail in engine.calls)


def test_clone_uses_stored_legacy_volume_and_independent_snapshot(
        repo, fake_node, no_ssh_probe, engine, monkeypatch):
    result = provisioning.provision(repo, _request())
    source = result.container_name
    legacy = f"unixnodes_{source}"
    engine.volumes[legacy] = engine.volumes.pop(result.volume_name)
    engine.volumes[legacy]["Name"] = legacy
    repo.patch_vps(source, volume_name=legacy)

    def copy_volume(_client, source_name, new_name, *, stored=None):
        assert source_name == source
        assert stored == legacy
        destination = volumes.volume_name(new_name)
        engine.volumes[destination] = {
            "Name": destination, "Driver": "local", "Labels": {},
            "Mountpoint": f"/fake/{destination}", "Options": {},
        }
        return True

    monkeypatch.setattr(volumes, "rename", copy_volume)
    cloned = lifecycle.clone(repo, repo.get_vps_by_container(source),
                             "alice-copy", owner_id=111)

    assert cloned["container"] == "alice-copy"
    assert engine.containers["alice-copy"].kwargs["image"] == ctr.snapshot_reference("alice-copy")
    assert ctr.snapshot_reference(source) in engine.images
    assert ctr.snapshot_reference("alice-copy") in engine.images
    assert engine.containers["alice-copy"].kwargs["volumes"][volumes.volume_name("alice-copy")]["bind"] == volumes.MOUNT_POINT
    assert repo.get_vps_by_container("alice-copy")["volume_name"] == volumes.volume_name("alice-copy")


def test_missing_recorded_volume_never_falls_back_to_new_name(
        repo, fake_node, no_ssh_probe, docker_client, engine):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    vps = repo.get_vps_by_container(name)
    original = engine.containers[name]
    engine.volumes.pop(result.volume_name)

    with pytest.raises(ContainerError, match="Recorded persistent volume"):
        lifecycle.recreate(repo, vps)

    assert engine.containers[name] is original
    assert original.status == "running"
    assert result.volume_name not in engine.volumes


def test_legacy_whole_rootfs_backup_is_imported_not_redirected(
        repo, fake_node, no_ssh_probe, docker_client, engine):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    backup_dir = Path(str(config.DOCKER_BACKUP_DIR))
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / "legacy-backup.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as bundle:
        payload = b"legacy /etc and /root content"
        info = tarfile.TarInfo("./etc/legacy-marker")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    archive.write_bytes(gzip.compress(raw.getvalue()))
    repo.add_backup({
        "container_name": name, "name": "legacy-backup",
        "archive": str(archive), "configuration": {}, "size_bytes": archive.stat().st_size,
    })

    restored = backup_service.restore(repo, repo.get_vps_by_container(name), "legacy-backup")

    assert restored["legacy_rootfs"] is True
    assert restored["volume_restored"] is False
    assert restored["rootfs_restored"] is True
    assert engine.containers[name].kwargs["image"] == ctr.snapshot_reference(name)
    assert any(kind == "image.import" for kind, _detail in engine.calls)


def test_migration_transfers_rootfs_archive(
        repo, fake_node, no_ssh_probe, docker_client, engine, monkeypatch):
    result = provisioning.provision(repo, _request())
    name = result.container_name
    target_id = repo.upsert_node({
        "name": "Target Node", "location": "Test", "total_vps": 50,
        "max_cpu": 32, "max_ram_gb": 64, "is_local": True,
        "transport": "local", "docker_host": "unix:///target.sock", "enabled": True,
    })
    from tests.fake_docker import make_client
    target_client = make_client(volume_root="/fake/target-volumes")
    clients = {1: docker_client, int(target_id): target_client}
    monkeypatch.setattr(nodes_mod, "require_client",
                        lambda node: clients[int(node["id"])])
    backup_dir = Path(str(config.DOCKER_BACKUP_DIR))

    def helper(_client, command, *, volumes, timeout=1800, name_hint="backup"):
        text = " ".join(str(part) for part in command)
        if "tar -czf" in text:
            match = re.search(r"/backup/([^\s]+\.tar\.gz)", text)
            assert match, text
            path = backup_dir / match.group(1)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = b"fake volume archive"
            path.write_bytes(payload)
            return 0, f"ARCHIVE_OK\n{hashlib.sha256(payload).hexdigest()}\n", ""
        if "tar -tzf" in text:
            return 0, "./\n1\nVERIFY_OK\n", ""
        if "tar -xzf" in text:
            return 0, "RESTORE_OK\n", ""
        return 0, "", ""

    monkeypatch.setattr(backups_mod, "run_helper", helper)
    transfers = []
    monkeypatch.setattr(lifecycle, "_write_archive",
                        lambda _client, data, filename: transfers.append((filename, data)))

    migrated = lifecycle.migrate(repo, repo.get_vps_by_container(name), int(target_id))

    assert migrated["node_id"] == int(target_id)
    assert any(filename.endswith(backups_mod.ROOTFS_SUFFIX) for filename, _data in transfers)
    assert target_client._engine.containers[name].kwargs["image"] == ctr.snapshot_reference(name)
    assert name not in engine.containers
    assert repo.get_vps_by_container(name)["node_id"] == int(target_id)
