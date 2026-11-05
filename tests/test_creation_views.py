"""Regression tests for the guided VPS creation select menus."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord


def run_async(coroutine):
    """Run on a private loop without changing the process-wide default loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, **_kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(id=len(self.calls))


class FakeInteraction:
    def __init__(self, user_id, *, values=None):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.data = {"values": values or []}
        self.message = None
        self.guild = None


def selects(view):
    return [child for child in view.children if isinstance(child, discord.ui.Select)]


def test_guided_creation_builds_node_and_os_selects(monkeypatch):
    """The modal must populate its view before sending it to Discord.

    It used to send an empty NodeSelectView, producing an embed with no menu.
    Selecting a node must then produce a populated OS menu as well.
    """
    import bot

    requester_id = 42
    owner_id = 123456789012345
    owner = SimpleNamespace(id=owner_id, mention="<@123456789012345>")
    nodes = [{
        "id": 7,
        "name": "Kolkata Node",
        "location": "Kolkata",
        "total_vps": 4,
        "is_local": True,
    }]

    monkeypatch.setattr(bot, "is_admin_id", lambda _user_id: True)

    async def fetch_user(user_id):
        assert user_id == owner_id
        return owner

    async def fake_run_blocking(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(bot.bot, "fetch_user", fetch_user)
    monkeypatch.setattr(bot, "run_blocking", fake_run_blocking)
    monkeypatch.setattr(bot, "get_nodes", lambda: list(nodes))
    monkeypatch.setattr(bot, "get_current_vps_count", lambda _node_id: 0)

    modal = bot.CreateVpsModal(requester_id)
    modal.specs._value = "2_2_20"
    modal.expiry._value = "30"
    modal.owner._value = str(owner_id)
    interaction = FakeInteraction(requester_id)

    run_async(modal.on_submit(interaction))

    assert interaction.response.deferred
    assert len(interaction.followup.calls) == 1
    node_view = interaction.followup.calls[0][1]["view"]
    node_selects = selects(node_view)
    assert len(node_selects) == 1, "guided creation sent an empty node view"
    assert [option.value for option in node_selects[0].options] == ["7"]
    assert node_selects[0].options[0].label == "Kolkata Node"

    # Choose the node through the same callback Discord invokes. The next
    # followup must carry the OS select, not another empty view.
    os_interaction = FakeInteraction(requester_id, values=["7"])
    run_async(node_view._choose(os_interaction))

    assert len(os_interaction.followup.calls) == 1
    os_view = os_interaction.followup.calls[0][1]["view"]
    os_selects = selects(os_view)
    assert len(os_selects) == 1, "node selection produced an empty OS view"
    assert [option.value for option in os_selects[0].options] == [
        str(image["value"]) for image in bot.OS_OPTIONS
    ]
