"""
Transactional provisioning and rollback.

The central promise of v11: a failed deployment must not leave an orphaned
container, volume, port reservation or database row behind.  Every test here
drives the real pipeline in ``tgdbot.services.provisioning`` against the
in-memory Docker engine, and injects failures at specific steps.
"""

from __future__ import annotations

import pytest

from tgdbot import config
from tgdbot.database import models
from tgdbot.docker_backend import resources, volumes
from tgdbot.errors import ProvisioningError, QuotaSupportReport
from tgdbot.services import provisioning


@pytest.fixture()
def ready(no_ssh_probe, fake_node):
    """Everything provisioning needs, minus the real network probes."""
    return fake_node


def make_request(container="alice-vps-1", **overrides):
    kwargs = dict(
        owner_id=111, container_name=container, image="ubuntu:24.04",
        ram_mb=1024, cpu=1.0, storage_gb=10, node_id=None, pids=256,
        expiry_days=30, is_public=False,
    )
    kwargs.update(overrides)
    return provisioning.ProvisionRequest(**kwargs)


def test_happy_path_creates_everything(repo, ready, engine):
    result = provisioning.provision(repo, make_request())

    assert result.container_name == "alice-vps-1"
    assert result.image == "ubuntu:24.04"
    assert result.ram_mb == 1024
    assert result.cpu == 1.0
    assert result.ssh_host_port
    assert result.password
    assert result.volume_name

    # The container exists with the right limits.
    container = engine.containers["alice-vps-1"]
    host = container.attrs["HostConfig"]
    assert int(host["Memory"]) == 1024 * 1024 * 1024
    assert int(host["PidsLimit"]) == 256
    assert host["Privileged"] is False

    # The named volume exists at a valid persistent-data destination. Docker
    # rejects a volume mounted at '/', so no mount may use that destination.
    assert result.volume_name in engine.volumes
    assert any(m["Destination"] == volumes.MOUNT_POINT for m in container.attrs["Mounts"])
    assert all(m["Destination"] != "/" for m in container.attrs["Mounts"])

    # The SSH port is published.
    ports = container.attrs["NetworkSettings"]["Ports"]
    assert ports["22/tcp"][0]["HostPort"] == str(result.ssh_host_port)


def test_database_row_is_written_with_docker_metadata(repo, ready):
    result = provisioning.provision(repo, make_request())
    row = repo.get_vps_by_container("alice-vps-1")
    assert row is not None
    assert int(row["id"]) == result.vps_id
    assert row["backend"] == models.DOCKER_BACKEND
    assert row["docker_container_id"] == result.container_id
    assert row["docker_image"] == "ubuntu:24.04"
    assert row["volume_name"] == result.volume_name
    assert int(row["ssh_host_port"]) == result.ssh_host_port
    assert row["provision_state"] == "active"
    # The service hands the plaintext back to the caller and stores nothing —
    # persistence is the Platform's job (see test_platform_stores_the_password).
    assert row["root_password"] in (None, "")
    assert result.password


def test_public_visibility_is_written_to_the_database(repo, ready):
    provisioning.provision(repo, make_request(is_public=True))
    row = repo.get_vps_by_container("alice-vps-1")
    assert row is not None
    assert int(row["is_public"]) == 1


def test_port_ledger_records_the_ssh_reservation(repo, ready):
    result = provisioning.provision(repo, make_request())
    rows = repo.active_ports_for_container("alice-vps-1")
    assert len(rows) == 1
    assert int(rows[0]["host_port"]) == result.ssh_host_port
    assert int(rows[0]["container_port"]) == config.SSH_CONTAINER_PORT
    assert rows[0]["purpose"] == "ssh"


def test_progress_callback_reports_every_step(repo, ready):
    seen = []
    provisioning.provision(repo, make_request(),
                           progress=lambda i, state, detail: seen.append((i, state)))
    indices = {index for index, _state in seen}
    # Every pipeline step must have been reported at least once.
    assert indices == set(range(len(provisioning.STEPS))), sorted(indices)


def test_generated_password_meets_every_character_class(repo, ready):
    result = provisioning.provision(repo, make_request())
    password = result.password
    assert len(password) >= 16
    assert any(c.islower() for c in password)
    assert any(c.isupper() for c in password)
    assert any(c.isdigit() for c in password)
    assert any(c in "!@#%^*-_=+" for c in password)


def test_password_is_never_in_the_container_environment(repo, ready, engine):
    """
    A plaintext root password must not appear in the container config, its
    labels or its environment — `docker inspect` would expose it.
    """
    result = provisioning.provision(repo, make_request())
    container = engine.containers["alice-vps-1"]
    blob = repr(container.kwargs) + repr(container.attrs)
    assert result.password not in blob


def test_expired_request_sets_an_expiration_date(repo, ready):
    result = provisioning.provision(repo, make_request(expiry_days=7))
    row = repo.get_vps_by_container("alice-vps-1")
    assert row["expiration_date"]
    assert result.record.get("expiration_date")


@pytest.mark.parametrize("step", [
    "images.pull",
    "volumes.create",
    "containers.create",
    "container.start",
])
def test_failure_at_each_step_rolls_everything_back(repo, ready, engine, step):
    # `images.pull` only fires for an image that is not already local.
    repo.add_image("debian:11", label="Debian 11", emoji="🔴")
    engine.images.pop("debian:11", None)
    engine.fail_at(step)
    with pytest.raises(ProvisioningError):
        provisioning.provision(repo, make_request(image="debian:11"))

    assert "alice-vps-1" not in engine.containers, f"container survived a {step} failure"
    assert f"{config.DOCKER_VOLUME_PREFIX}alice-vps-1" not in engine.volumes, \
        f"volume survived a {step} failure"
    assert repo.get_vps_by_container("alice-vps-1") is None, \
        f"database row survived a {step} failure"
    assert repo.active_ports_for_container("alice-vps-1") == [], \
        f"port reservation survived a {step} failure"


def test_failure_reports_what_was_cleaned_up(repo, ready, engine):
    engine.fail_at("containers.create")
    with pytest.raises(ProvisioningError) as excinfo:
        provisioning.provision(repo, make_request())
    assert excinfo.value.cleanup, "the error did not say what was rolled back"


def test_quota_failure_has_the_correct_step_and_rolls_back(repo, ready, engine, monkeypatch):
    def fail_quota(_self, _container, _storage_gb):
        raise RuntimeError("quota backend unavailable")

    monkeypatch.setattr(provisioning.Provisioner, "_apply_quota", fail_quota)
    with pytest.raises(ProvisioningError) as excinfo:
        provisioning.provision(repo, make_request())

    assert "step 17 (Apply the storage quota)" in str(excinfo.value)
    assert "quota backend unavailable" in str(excinfo.value)
    assert "alice-vps-1" not in engine.containers
    assert repo.active_ports_for_container("alice-vps-1") == []


def test_database_failure_releases_enforced_quota_before_volume(repo, ready, engine, monkeypatch):
    support = QuotaSupportReport(True, "xfs-project-quota", "xfs", "test quota")
    monkeypatch.setattr(resources, "detect_quota_support", lambda *_args, **_kwargs: support)
    monkeypatch.setattr(resources, "apply_quota",
                        lambda *_args, **_kwargs: (True, "test quota applied"))
    releases = []

    def release(mountpoint, method, project_id):
        releases.append((mountpoint, method, project_id))
        return True, "released"

    def fail_insert(_record):
        raise RuntimeError("database registration failed")

    monkeypatch.setattr(resources, "release_quota", release)
    monkeypatch.setattr(repo, "insert_vps", fail_insert)

    with pytest.raises(ProvisioningError) as excinfo:
        provisioning.provision(repo, make_request())

    assert "step 18 (Register in the database)" in str(excinfo.value)
    assert len(releases) == 1
    assert releases[0][0]
    assert releases[0][1] == "xfs-project-quota"
    assert releases[0][2] > 0
    assert "alice-vps-1" not in engine.containers
    assert f"{config.DOCKER_VOLUME_PREFIX}alice-vps-1" not in engine.volumes
    assert repo.get_quota("alice-vps-1") is None
    assert repo.get_vps_by_container("alice-vps-1") is None
    assert repo.active_ports_for_container("alice-vps-1") == []
    assert "host storage quota released" in excinfo.value.cleanup


def test_a_second_vps_still_provisions_after_a_rollback(repo, ready, engine):
    engine.fail_at("containers.create")
    with pytest.raises(ProvisioningError):
        provisioning.provision(repo, make_request("doomed-vps-1"))
    engine.clear_failures()

    result = provisioning.provision(repo, make_request("alice-vps-1"))
    assert result.container_name == "alice-vps-1"
    assert repo.get_vps_by_container("doomed-vps-1") is None


def test_node_without_capacity_is_refused(repo, ready, engine):
    node = repo.get_node(int(ready["id"]))
    repo.update_node(int(node["id"]), total_vps=1, max_ram_gb=1, max_cpu=1)
    provisioning.provision(repo, make_request("first-vps-1", ram_mb=512, cpu=0.5))
    with pytest.raises(ProvisioningError) as excinfo:
        provisioning.provision(repo, make_request("second-vps-2", ram_mb=4096, cpu=4))
    assert "enough" in str(excinfo.value).lower() or "full" in str(excinfo.value).lower()
    assert "second-vps-2" not in engine.containers


def test_unapproved_custom_image_is_refused(repo, ready, engine, monkeypatch):
    monkeypatch.setattr(config, "DOCKER_ALLOW_CUSTOM_IMAGES", False, raising=False)
    with pytest.raises(ProvisioningError):
        provisioning.provision(repo, make_request(image="ghcr.io/evil/base:latest"))
    assert repo.get_vps_by_container("alice-vps-1") is None


def test_approved_custom_image_is_accepted(repo, ready, engine):
    repo.add_image("ghcr.io/acme/base:1.2.3", label="Acme Base", emoji="🐳")
    result = provisioning.provision(repo, make_request(image="ghcr.io/acme/base:1.2.3"))
    assert result.image == "ghcr.io/acme/base:1.2.3"


def test_invalid_container_name_is_refused(repo, ready):
    with pytest.raises(ProvisioningError):
        provisioning.provision(repo, make_request("Bad Name!"))


def test_zero_ram_is_refused(repo, ready):
    with pytest.raises(ProvisioningError):
        provisioning.provision(repo, make_request(ram_mb=0))


def test_verification_host_resolution():
    """The SSH probe must dial an address that actually reaches the node."""
    local_wildcard = {"is_local": True, "docker_host": "unix:///var/run/docker.sock"}
    assert provisioning.verification_host(local_wildcard, "0.0.0.0") == "127.0.0.1"

    local_specific = {"is_local": True, "docker_host": "unix:///var/run/docker.sock"}
    assert provisioning.verification_host(local_specific, "192.168.1.50") == "192.168.1.50"

    remote = {"is_local": False, "docker_host": "ssh://ops@node2.example.com"}
    assert provisioning.verification_host(remote, "0.0.0.0") == "node2.example.com"


def test_platform_stores_the_encrypted_password(db_path, fake_node, no_ssh_probe):
    """
    The services never see the encryption key, so the Platform must persist the
    password itself.  Without this the user is DM'd a password that nothing in
    the system remembers — `!vps password` and the web terminal would both fail.
    """
    from tgdbot.platform import Platform

    platform = Platform(db_path)
    platform.set_vault(lambda plaintext: "f1:" + plaintext[::-1])
    result = platform.repo  # touch to make the intent obvious
    assert result is not None

    import asyncio

    outcome = asyncio.get_event_loop().run_until_complete(
        platform.provision(provisioning.ProvisionRequest(
            owner_id=111, container_name="vault-vps-1", image="ubuntu:24.04",
            ram_mb=512, cpu=0.5, storage_gb=5)))
    row = platform.repo.get_vps_by_container("vault-vps-1")
    assert row["root_password"] == "f1:" + outcome.password[::-1]
    assert row["root_password"] != outcome.password


def test_platform_without_a_vault_warns_and_stores_nothing(db_path, fake_node, no_ssh_probe):
    import asyncio
    import logging

    from tgdbot.platform import Platform

    platform = Platform(db_path)          # no set_vault()
    outcome = asyncio.get_event_loop().run_until_complete(
        platform.provision(provisioning.ProvisionRequest(
            owner_id=111, container_name="vault-vps-2", image="ubuntu:24.04",
            ram_mb=512, cpu=0.5, storage_gb=5)))
    assert outcome.password
    row = platform.repo.get_vps_by_container("vault-vps-2")
    assert row["root_password"] in (None, "")


def test_plan_validation_rejects_nonsense(repo, ready):
    for kwargs in ({"ram_mb": 0}, {"ram_mb": -512}, {"cpu": 0}, {"storage_gb": 0}):
        with pytest.raises(ProvisioningError):
            provisioning.provision(repo, make_request(**kwargs))


def test_unspecified_plan_falls_back_to_the_defaults(repo, ready):
    result = provisioning.provision(repo, provisioning.ProvisionRequest(
        owner_id=111, container_name="default-vps-1", image="ubuntu:24.04",
        ram_mb=None, cpu=None, storage_gb=None))
    assert result.ram_mb == config.DOCKER_DEFAULT_RAM
    assert result.cpu == config.DOCKER_DEFAULT_CPU
    assert result.storage_gb == config.DOCKER_DEFAULT_STORAGE
