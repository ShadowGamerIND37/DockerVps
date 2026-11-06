"""Discord interaction tests for admin-only creation security controls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord


def run_async(coroutine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class Response:
    def __init__(self):
        self.deferred = False
        self.messages = []

    async def defer(self, **_kwargs):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class Interaction:
    def __init__(self, user_id, values=None):
        self.user = SimpleNamespace(id=user_id)
        self.response = Response()
        self.data = {"values": values or []}
        self.message = None
        self.guild = None


def make_view(bot, user_id=42, image="ubuntu:24.04"):
    owner = SimpleNamespace(id=987654321, name="owner")
    view = bot.SecurityOptionsView(user_id, owner, 1, image, 2, 2, 20, 30)
    run_async(view.build_options())
    return view


def test_security_view_has_two_selects_and_forwards_admin_choices(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "is_admin_id", lambda _user_id: True)
    view = make_view(bot)
    assert len([item for item in view.children if isinstance(item, discord.ui.Select)]) == 2
    assert view.kvm_enabled is False
    assert view.apparmor_enabled is True

    class FakeResponder:
        def __init__(self, target):
            self.target = target

        async def send(self, **_kwargs):
            return None

    captured = {}

    async def fake_provision(**kwargs):
        captured.update(kwargs)
        return {"container_name": "security-vps-1"}

    monkeypatch.setattr(bot, "Responder", FakeResponder)
    monkeypatch.setattr(bot, "provision_vps", fake_provision)
    run_async(view._choose_kvm(Interaction(42, ["1"])))
    run_async(view._choose_apparmor(Interaction(42, ["0"])))
    run_async(view._confirm(Interaction(42)))

    assert captured["requester_id"] == 42
    assert captured["kvm_enabled"] is True
    assert captured["apparmor_enabled"] is False


def test_security_view_rejects_non_admin_interaction(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "is_admin_id", lambda _user_id: False)
    view = make_view(bot)
    interaction = Interaction(42, ["1"])
    run_async(view._choose_kvm(interaction))
    assert view.kvm_enabled is False
    assert interaction.response.messages
    assert not interaction.response.deferred


def test_proxmox_security_view_refuses_kvm_disabled_before_provisioning(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "is_admin_id", lambda _user_id: True)
    view = make_view(bot, image=bot.tgd_config.PROXMOX_IMAGE)
    assert view.kvm_enabled is True
    view.kvm_enabled = False
    called = False

    async def fake_provision(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(bot, "provision_vps", fake_provision)
    interaction = Interaction(42)
    run_async(view._confirm(interaction))
    assert called is False
    assert interaction.response.messages
    assert not interaction.response.deferred
