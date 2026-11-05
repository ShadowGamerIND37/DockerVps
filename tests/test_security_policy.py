"""
Container security policy.

These assert the hard rules the project must never violate: no privileged
containers, no host device/namespace sharing, no Docker socket handed to a
customer, capabilities dropped, and PID/memory limits actually present in the
create payload.
"""

from __future__ import annotations

import pytest

from tgdbot import config
from tgdbot.docker_backend import containers as ctr
from tgdbot.docker_backend import security


def _spec(**overrides):
    kwargs = dict(
        container_name="test-vps-1", image="ubuntu:24.04", ram_mb=1024, cpu=1.0,
        pids=512, publish_ip="0.0.0.0",
        published_ports=[(22001, 22, "tcp"), (25001, 8080, "tcp")],
        volumes={"tgdbot_test-vps-1": {"bind": "/", "mode": "rw"}},
    )
    kwargs.update(overrides)
    return ctr.build_container_spec(**kwargs)


def test_never_privileged_by_default():
    spec = _spec()
    assert spec.get("privileged") is False
    assert spec.get("network_mode") != "host"
    assert spec.get("pid_mode") != "host"
    assert spec.get("ipc_mode") != "host"
    assert not spec.get("devices")


def test_privileged_can_be_forced_but_is_off_in_config():
    assert config.DOCKER_PRIVILEGED is False


def test_docker_socket_is_never_mounted_into_a_container():
    spec = _spec()
    binds = " ".join(spec.get("volumes", {}).keys())
    assert "docker.sock" not in binds
    assert "/var/run/docker.sock" not in str(spec)


def test_capabilities_are_dropped():
    spec = _spec()
    dropped = set(spec.get("cap_drop") or [])
    assert "ALL" in dropped or len(dropped) >= 10
    # The capabilities sshd and chown need must survive the drop list, so they
    # must NOT appear in it.  `NET_ADMIN`, `SYS_ADMIN` and `SYS_MODULE` must.
    for needed in security.CAP_KEEP:
        assert needed not in dropped, f"{needed} was dropped but the guest needs it"
    for dangerous in ("NET_ADMIN", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO"):
        assert dangerous in dropped, f"{dangerous} was not dropped"


def test_no_new_privileges_is_opt_in_and_off_by_default():
    """
    ``no-new-privileges`` breaks su/sudo and PAM-backed sshd root logins, so it
    must stay off unless an operator explicitly asks for it.
    """
    assert config.DOCKER_NO_NEW_PRIVILEGES is False
    spec = _spec()
    assert not any("no-new-privileges:true" in str(item)
                   for item in (spec.get("security_opt") or []))


def test_memory_and_cpu_limits_are_in_the_payload():
    spec = _spec(ram_mb=2048, cpu=2.0, pids=256)
    assert int(spec["mem_limit"]) == 2048 * 1024 * 1024
    # Swap must match RAM or the guest can burst past its memory limit.
    assert int(spec["memswap_limit"]) == 2048 * 1024 * 1024
    assert int(spec["cpu_quota"]) == int(2.0 * config.DOCKER_CPU_PERIOD)
    assert int(spec["cpu_period"]) == config.DOCKER_CPU_PERIOD
    assert int(spec["pids_limit"]) == 256


def test_ports_are_published_through_the_sdk_shape():
    spec = _spec()
    ports = spec["ports"]
    assert ports["22/tcp"] == [("0.0.0.0", 22001)]
    assert ports["8080/tcp"] == [("0.0.0.0", 25001)]


def test_named_volume_is_mounted_at_root():
    spec = _spec()
    assert spec["volumes"]["tgdbot_test-vps-1"]["bind"] == "/"


def test_platform_labels_ownership():
    spec = _spec()
    labels = spec["labels"]
    assert labels[config.LABEL_MANAGED] == "true"
    assert labels[config.LABEL_CONTAINER] == "test-vps-1"
    assert labels[config.LABEL_ROLE] == config.ROLE_VPS


@pytest.mark.parametrize("name,ok", [
    ("good-vps-1", True),
    ("a" * 63, True),
    ("UPPER", False),
    ("has space", False),
    ("-leading-dash", False),
    ("", False),
    ("a" * 64, False),
])
def test_container_name_validation(name, ok):
    assert security.validate_container_name(name)[0] is ok


@pytest.mark.parametrize("reference,ok", [
    ("ubuntu:24.04", True),
    ("debian:12", True),
    ("alpine:3.20", True),
    ("ghcr.io/acme/base:1.2.3", True),
    ("ubuntu:24.04; rm -rf /", False),
    ("ubuntu:24.04 && curl evil", False),
    ("", False),
])
def test_image_reference_validation(reference, ok):
    assert security.validate_image_reference(reference)[0] is ok


def test_describe_policy_reports_the_truth():
    policy = security.describe_policy()
    assert policy["privileged"] is False
    assert policy["docker_socket_exposed"] is False
    assert policy["host_namespaces_shared"] is False
    assert len(policy["capabilities_dropped"]) >= 1
    assert policy["pids_limit"] == config.DOCKER_DEFAULT_PID_LIMIT
