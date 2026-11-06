"""Application-command coverage and payload integrity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView


def run_async(coroutine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def slash_commands(bot_module):
    return {
        command.name: command
        for command in bot_module.bot.tree.get_commands()
        if isinstance(command, (app_commands.Command, app_commands.Group))
    }


def test_every_prefix_command_has_a_slash_equivalent():
    """Every top-level prefix command must have a slash equivalent."""
    import bot

    prefix = {command.name: command for command in bot.bot.commands}
    slash = slash_commands(bot)

    assert len(prefix) == 89
    assert len(slash) == 89
    assert set(slash) == set(prefix), (
        f"missing={sorted(set(prefix) - set(slash))}; "
        f"extra={sorted(set(slash) - set(prefix))}"
    )

    # Registration alone does not serialize a hybrid command. Validate the
    # actual payloads that CommandTree.sync() sends to Discord.
    for name, command in slash.items():
        payload = command.to_dict(bot.bot.tree)
        assert payload["name"] == name
        assert 1 <= len(payload["description"]) <= 100
        assert len(payload.get("options", [])) <= 25
        seen_optional = False
        for option in payload.get("options", []):
            if option.get("required", False):
                assert not seen_optional, f"{name}: required option follows optional"
            else:
                seen_optional = True
            assert 1 <= len(option["description"]) <= 100


def test_vps_and_ports_expose_their_remaining_arguments():
    """Discord has no *args option; both commands expose one optional string."""
    import bot

    for name, first_parameter in (("vps", "action"), ("ports", "subcommand")):
        command = slash_commands(bot)[name]
        parameters = {parameter.name: parameter for parameter in command.parameters}
        assert first_parameter in parameters
        assert "args" in parameters
        assert not parameters["args"].required
        assert parameters["args"].type.name == "string"


def test_proxmox_grant_commands_are_real_slash_subcommands():
    import bot

    group = next(command for command in bot.bot.tree.get_commands()
                 if isinstance(command, app_commands.Group) and command.name == "proxmox")
    assert {command.name for command in group.commands} == {"give", "remove", "list"}
    payload = group.to_dict(bot.bot.tree)
    subcommands = {option["name"]: option for option in payload["options"]}
    assert set(subcommands) == {"give", "remove", "list"}
    for name in ("give", "remove"):
        options = subcommands[name]["options"]
        assert options[0]["name"] == "userid"
        assert options[0]["required"] is True
        assert len(options[0]["description"]) > 3


def test_vps_context_menu_remains_registered():
    import bot

    assert any(
        isinstance(command, app_commands.ContextMenu) and command.name == "VPS info"
        for command in bot.bot.tree.get_commands()
    )


def test_prefix_rest_arguments_remain_compatible():
    """The new slash-safe signatures still parse the original prefix forms."""
    import bot

    for name, raw, first, rest in (
        ("vps", "create", "create", ""),
        ("ports", "add 1 8080", "add", "1 8080"),
    ):
        command = bot.bot.get_command(name)
        message = SimpleNamespace(attachments=[], _state=bot.bot._connection)
        context = commands.Context(
            message=message,
            bot=bot.bot,
            view=StringView(raw),
            command=command,
        )
        run_async(command._parse_arguments(context))
        assert context.args[1:] == [first]
        assert context.kwargs == {"args": rest}
