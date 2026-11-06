"""Proxmox management-panel routing to the native graphical console."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from tgdbot import config


class FakeResponse:
    def __init__(self):
        self.kwargs = None

    async def send_message(self, **kwargs):
        self.kwargs = kwargs


def vps_row(image):
    return {
        "container_name": "pve-vps-1",
        "docker_image": image,
        "os_version": image,
        "ssh_host_port": 22786,
        "status": "running",
        "suspended": False,
        "node_id": 1,
        "user_id": "111",
    }


def test_proxmox_panel_uses_native_ui_and_explains_novnc():
    import bot

    view = bot.ManageView(111, [vps_row(config.PROXMOX_IMAGE)])
    labels = {item.label for item in view.children if isinstance(item, discord.ui.Button)}
    assert "Proxmox UI" in labels
    assert "Host Shell" in labels
    assert "Web SSH" not in labels

    interaction = SimpleNamespace(response=FakeResponse(), user=SimpleNamespace(id=111))
    asyncio.run(view._webssh(interaction))
    sent = interaction.response.kwargs
    assert sent is not None
    embed = sent["embed"]
    assert embed.title == "Proxmox UI & VM Console"
    assert "http://127.0.0.1:5001/c/" in embed.description
    text = embed.description + "\n" + "\n".join(field.value for field in embed.fields)
    assert "Linux PAM standard authentication" in text
    assert "noVNC" in text
    assert "BIOS/boot menu" in text
    view.stop()

    ordinary = bot.ManageView(111, [vps_row("ubuntu:24.04")])
    ordinary_labels = {item.label for item in ordinary.children if isinstance(item, discord.ui.Button)}
    assert "Web SSH" in ordinary_labels
    assert "Console" in ordinary_labels
    ordinary.stop()
