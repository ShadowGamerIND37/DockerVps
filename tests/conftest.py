"""Shared fixtures: an isolated database, a fake Docker node, and a repo."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tgdbot import config                                        # noqa: E402
from tgdbot.database import migrations                           # noqa: E402
from tgdbot.database.repository import Repository                # noqa: E402
from tgdbot.docker_backend import nodes as nodes_mod             # noqa: E402
from tests.fake_docker import make_client                           # noqa: E402


@pytest.fixture(autouse=True)
def fast_probes(monkeypatch):
    """
    Shrink the real-time waits so the suite runs in seconds.

    ``ssh.wait_ready`` polls a container up to ``SSH_READY_ATTEMPTS`` times with
    a real sleep between tries; against the in-memory engine nothing is ever
    going to become "more ready", so the defaults would add 30s per VPS.
    """
    monkeypatch.setattr(config, "SSH_READY_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(config, "SSH_READY_INTERVAL", 0.0, raising=False)
    monkeypatch.setattr(config, "DOCKER_IMAGE_PULL_TIMEOUT", 10, raising=False)
    monkeypatch.setattr(config, "DOCKER_START_TIMEOUT", 2, raising=False)
    monkeypatch.setattr(config, "DOCKER_OPERATION_TIMEOUT", 5, raising=False)


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """A fresh, fully migrated database in a temp directory."""
    path = tmp_path / "vps_data.db"
    monkeypatch.setattr(config, "DATABASE_PATH", path, raising=False)
    monkeypatch.setattr(config, "DOCKER_VPS_DATA_DIR", tmp_path / "vps", raising=False)
    monkeypatch.setattr(config, "DOCKER_RUNTIME_DIR", tmp_path / "runtime", raising=False)
    monkeypatch.setattr(config, "DOCKER_SECRET_DIR", tmp_path / "secrets", raising=False)
    monkeypatch.setattr(config, "DOCKER_BACKUP_DIR", tmp_path / "backups", raising=False)
    monkeypatch.setattr(config, "MIGRATION_REPORT_DIR", tmp_path / "reports", raising=False)
    report = migrations.migrate(path, skip_backup=True)
    assert report.ok, report.summary_text()
    return path


@pytest.fixture()
def repo(db_path):
    return Repository(db_path)


@pytest.fixture()
def docker_client(tmp_path):
    """A ``docker.DockerClient``-shaped object backed by the in-memory engine."""
    return make_client(volume_root=str(tmp_path / "volumes"))


@pytest.fixture()
def engine(docker_client):
    """The engine state behind ``docker_client`` (for assertions)."""
    return docker_client._engine


@pytest.fixture()
def fake_node(repo, docker_client, monkeypatch):
    """Register a local node whose Docker client is the in-memory fake."""
    node_id = repo.upsert_node({
        "name": "Local Node", "location": "Test", "total_vps": 50,
        "max_cpu": 32, "max_ram_gb": 64, "is_local": True,
        "transport": "local", "docker_host": "unix:///var/run/docker.sock",
        "enabled": True,
    })
    node = repo.get_node(node_id)

    def _client_for(_node):
        return docker_client

    monkeypatch.setattr(nodes_mod, "client_for", _client_for)
    monkeypatch.setattr(nodes_mod, "require_client", _client_for)
    return node


@pytest.fixture()
def no_ssh_probe(monkeypatch):
    """Skip real network probes of published ports (nothing is listening here)."""
    from tgdbot.docker_backend import ssh as ssh_mod

    monkeypatch.setattr(ssh_mod, "tcp_reachable", lambda *a, **k: True)
    monkeypatch.setattr(ssh_mod, "ssh_banner", lambda *a, **k: "SSH-2.0-OpenSSH_9.6")
    monkeypatch.setattr(ssh_mod, "verify_ssh_login",
                        lambda *a, **k: (True, "login ok (faked)"))
