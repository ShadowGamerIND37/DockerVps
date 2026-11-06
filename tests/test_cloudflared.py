"""Cloudflare quick-tunnel setup and URL-safety regressions."""

from __future__ import annotations

import pytest

from tgdbot import config
from tgdbot.errors import ContainerError
from tgdbot.services import cloudflared


def pve_row():
    return {
        "container_name": "pve-vps-1",
        "docker_image": config.PROXMOX_IMAGE,
        "os_version": config.PROXMOX_IMAGE,
        "node_id": 1,
        "user_id": "111",
        "status": "running",
        "suspended": False,
    }


def test_quick_tunnel_url_accepts_cloudflared_output_and_ignores_ansi():
    log = (
        "2026-11-06T09:00:00Z INF Your quick Tunnel has been created! Visit it at "
        "(it may take some time to be reachable):\n"
        "2026-11-06T09:00:01Z INF \x1b[36mhttps://unit-test-tunnel.trycloudflare.com\x1b[0m\n"
    )
    assert cloudflared.quick_tunnel_url(log) == "https://unit-test-tunnel.trycloudflare.com"


def test_quick_tunnel_url_rejects_non_cloudflare_or_unsafe_addresses():
    assert cloudflared.quick_tunnel_url("http://unit-test-tunnel.trycloudflare.com") is None
    assert cloudflared.quick_tunnel_url("https://trycloudflare.com") is None
    assert cloudflared.quick_tunnel_url("https://user:pass@unit-test-tunnel.trycloudflare.com") is None
    assert cloudflared.quick_tunnel_url("https://trycloudflare.com.evil.example") is None
    assert cloudflared.quick_tunnel_url("https://unit-test-tunnel.trycloudflare.com.evil.example") is None
    assert cloudflared.quick_tunnel_url("https://unit-test-tunnel.trycloudflare.com/path") is None


def test_setup_runs_requested_commands_and_reports_live_stages(monkeypatch):
    commands = []
    events = []
    monkeypatch.setattr(cloudflared.lifecycle, "state", lambda _repo, _vps: "running")

    def exec_command(_repo, _vps, command, *, timeout=None):
        commands.append((command, timeout))
        if command == "cat /root/cloudflared.log":
            return 0, (
                "Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):\n"
                "https://unit-test-tunnel.trycloudflare.com\n"
            ), ""
        return 0, "ok", ""

    monkeypatch.setattr(cloudflared.lifecycle, "exec_command", exec_command)
    result = cloudflared.setup_quick_tunnel(
        object(), pve_row(), progress=lambda index, state, detail: events.append((index, state, detail)),
        poll_interval=0,
    )

    assert result["url"] == "https://unit-test-tunnel.trycloudflare.com"
    assert len(commands) == 5
    assert commands[0][0] == "apt update"
    assert commands[4][0] == "cat /root/cloudflared.log"
    prerequisite_script = commands[1][0]
    assert "apt install sudo -y" in prerequisite_script
    assert "cloudflare-main.gpg" in prerequisite_script
    assert "https://pkg.cloudflare.com/cloudflared any main" in prerequisite_script
    assert "apt-get update" in commands[2][0]
    assert "apt-get install -y cloudflared" in commands[2][0]
    assert (
        "nohup cloudflared tunnel --url https://localhost:8006 --no-tls-verify "
        "> /root/cloudflared.log 2>&1 &"
    ) in commands[3][0]
    assert [(index, state) for index, state, _detail in events] == [
        (0, "running"), (0, "done"),
        (1, "running"), (1, "done"),
        (2, "running"), (2, "done"),
        (3, "running"), (3, "done"),
        (4, "running"), (4, "done"),
    ]


def test_setup_rejects_ordinary_linux_before_any_exec(monkeypatch):
    monkeypatch.setattr(
        cloudflared.lifecycle, "state",
        lambda _repo, _vps: pytest.fail("ordinary Linux must not be inspected for Cloudflare setup"),
    )
    with pytest.raises(ContainerError, match="only for Proxmox"):
        cloudflared.setup_quick_tunnel(object(), {**pve_row(), "docker_image": "ubuntu:24.04",
                                                    "os_version": "ubuntu:24.04"})


def test_setup_surfaces_package_failures_without_starting_the_tunnel(monkeypatch):
    monkeypatch.setattr(cloudflared.lifecycle, "state", lambda _repo, _vps: "running")
    commands = []

    def fail_update(_repo, _vps, command, *, timeout=None):
        commands.append(command)
        return 1, "", "apt repository unavailable"

    monkeypatch.setattr(cloudflared.lifecycle, "exec_command", fail_update)
    with pytest.raises(ContainerError, match="Update package lists failed.*apt repository unavailable"):
        cloudflared.setup_quick_tunnel(object(), pve_row(), poll_interval=0)
    assert commands == ["apt update"]


def test_setup_times_out_when_cloudflared_never_prints_a_url(monkeypatch):
    monkeypatch.setattr(cloudflared.lifecycle, "state", lambda _repo, _vps: "running")
    monkeypatch.setattr(
        cloudflared.lifecycle, "exec_command",
        lambda _repo, _vps, _command, *, timeout=None: (0, "starting tunnel", ""),
    )
    with pytest.raises(ContainerError, match="no quick tunnel URL appeared"):
        cloudflared.setup_quick_tunnel(object(), pve_row(), tunnel_timeout=0, poll_interval=0)
