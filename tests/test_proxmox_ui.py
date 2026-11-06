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


class FakeDeferredResponse(FakeResponse):
    def __init__(self):
        super().__init__()
        self.done = False
        self.defer_kwargs = None

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.defer_kwargs = kwargs


class FakeMessage:
    next_id = 1000

    def __init__(self):
        self.id = FakeMessage.next_id
        FakeMessage.next_id += 1
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.calls = []
        self.messages = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        message = FakeMessage()
        self.messages.append(message)
        return message


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


def test_proxmox_panel_uses_native_ui_and_explains_novnc(monkeypatch):
    import bot

    monkeypatch.setattr(bot.PROXMOX_CONSOLE_SETTINGS, "public_host", "console.example.com")
    view = bot.ManageView(111, [vps_row(config.PROXMOX_IMAGE)])
    labels = {item.label for item in view.children if isinstance(item, discord.ui.Button)}
    assert "Proxmox UI" in labels
    assert "Host Shell" in labels
    assert "Cloudflare" in labels
    assert "Web SSH" not in labels

    interaction = SimpleNamespace(response=FakeResponse(), user=SimpleNamespace(id=111))
    asyncio.run(view._webssh(interaction))
    sent = interaction.response.kwargs
    assert sent is not None
    embed = sent["embed"]
    assert embed.title == "Proxmox UI & VM Console"
    assert "http://console.example.com:5001/c/" in embed.description
    assert "127.0.0.1:5001/c/" not in embed.description
    text = embed.description + "\n" + "\n".join(field.value for field in embed.fields)
    assert "Linux PAM standard authentication" in text
    assert "noVNC" in text
    assert "BIOS/boot menu" in text
    view.stop()

    ordinary = bot.ManageView(111, [vps_row("ubuntu:24.04")])
    ordinary_labels = {item.label for item in ordinary.children if isinstance(item, discord.ui.Button)}
    assert "Web SSH" in ordinary_labels
    assert "Console" in ordinary_labels
    assert "Cloudflare" not in ordinary_labels
    ordinary.stop()


def test_cloudflare_button_shows_live_progress_and_returns_url_ephemerally(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "anim_on", lambda: False)
    monkeypatch.setattr(bot, "audit", lambda *args, **kwargs: None)

    async def running_state(_container, _node_id):
        return "running"

    async def fake_setup(container_name, *, progress):
        assert container_name == "pve-vps-1"
        for index in range(5):
            progress(index, "running", f"stage {index}")
            await asyncio.sleep(0)
            progress(index, "done", f"stage {index}")
        return {"url": "https://unit-test-tunnel.trycloudflare.com", "log": "ready"}

    monkeypatch.setattr(bot, "container_state", running_state)
    monkeypatch.setattr(bot.INFRA, "cloudflared", fake_setup)

    view = bot.ManageView(111, [vps_row(config.PROXMOX_IMAGE)])
    response = FakeDeferredResponse()
    followup = FakeFollowup()
    interaction = SimpleNamespace(
        response=response, followup=followup, user=SimpleNamespace(id=111),
        channel=SimpleNamespace(),
    )
    asyncio.run(view._cloudflare(interaction))

    assert response.done is True
    assert response.defer_kwargs["ephemeral"] is True
    assert followup.calls[0]["ephemeral"] is True
    assert followup.calls[0]["wait"] is True
    message = followup.messages[0]
    assert len(message.edits) >= 2, "the progress message was never updated"
    progress_text = "\n".join(
        str(edit["embed"].description)
        for edit in message.edits[:-1]
        if edit.get("embed") is not None
    )
    assert "Update package lists" in progress_text
    final_embed = message.edits[-1]["embed"]
    assert final_embed.title == "✅ Cloudflare Tunnel Ready"
    assert "https://unit-test-tunnel.trycloudflare.com" in final_embed.description
    assert view._cloudflare_running is False
    view.stop()


def test_cloudflare_button_finishes_with_visible_setup_error(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "anim_on", lambda: False)

    async def running_state(_container, _node_id):
        return "running"

    async def fail_setup(_container_name, *, progress):
        progress(0, "running", "Running apt update")
        await asyncio.sleep(0)
        raise RuntimeError("apt repository unavailable")

    monkeypatch.setattr(bot, "container_state", running_state)
    monkeypatch.setattr(bot.INFRA, "cloudflared", fail_setup)
    view = bot.ManageView(111, [vps_row(config.PROXMOX_IMAGE)])
    response = FakeDeferredResponse()
    followup = FakeFollowup()
    interaction = SimpleNamespace(
        response=response, followup=followup, user=SimpleNamespace(id=111),
        channel=SimpleNamespace(),
    )
    asyncio.run(view._cloudflare(interaction))

    final_embed = followup.messages[0].edits[-1]["embed"]
    assert final_embed.title == "⛔ Cloudflare Setup Failed"
    assert "apt repository unavailable" in final_embed.description
    assert view._cloudflare_running is False
    view.stop()
