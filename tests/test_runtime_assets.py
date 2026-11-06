"""Host runtime-directory failure handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from tgdbot import config
from tgdbot.docker_backend import ssh


def test_unwritable_runtime_directory_has_remediation(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "DOCKER_RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DOCKER_SECRET_DIR", tmp_path / "secrets")
    monkeypatch.setattr(config, "DOCKER_VPS_DATA_DIR", tmp_path / "vps")
    monkeypatch.setattr(config, "DOCKER_BACKUP_DIR", tmp_path / "backups")

    original_mkdir = Path.mkdir

    def deny_runtime(self, *args, **kwargs):
        if self == runtime:
            raise PermissionError(13, "Permission denied", str(runtime))
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_runtime)
    with pytest.raises(PermissionError) as caught:
        ssh.ensure_runtime_assets()

    message = str(caught.value)
    assert str(runtime) in message
    assert "install.sh" in message
    assert "service user" in message
