"""Tests for the installer's non-executing .env reader."""

from __future__ import annotations

from tgdbot.scripts.env_value import value_for


def test_legacy_paths_are_read_without_executing_env_contents(tmp_path):
    marker = tmp_path / "must-not-be-created"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DOCKER_RUNTIME_DIR=/var/lib/unixnodes/runtime\n"
        "DOCKER_SECRET_DIR=/var/lib/unixnodes/secrets\n"
        f"HOST_MOTD=bash <(touch {marker})\n",
        encoding="utf-8",
    )

    assert value_for("DOCKER_RUNTIME_DIR", "/var/lib/tgdbot/runtime", env_file, {}) == \
        "/var/lib/unixnodes/runtime"
    assert value_for("DOCKER_SECRET_DIR", "/var/lib/tgdbot/secrets", env_file, {}) == \
        "/var/lib/unixnodes/secrets"
    assert not marker.exists()


def test_explicit_shell_value_and_defaults_have_correct_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DOCKER_RUNTIME_DIR=/from/file\n", encoding="utf-8")

    assert value_for("DOCKER_RUNTIME_DIR", "/default", env_file,
                     {"DOCKER_RUNTIME_DIR": "/from/shell"}) == "/from/shell"
    assert value_for("DOCKER_BACKUP_DIR", "/default-backup", env_file, {}) == "/default-backup"
    assert value_for("DOCKER_RUNTIME_DIR", "/default", env_file,
                     {"DOCKER_RUNTIME_DIR": ""}) == "/default"
