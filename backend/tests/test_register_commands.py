"""Validates the Discord slash-command definitions in register_commands.py
against Discord's own schema limits.

Real bug this exists to catch: alert-tracker-record's description was 106
characters (Discord's cap is 100), which made Discord reject the ENTIRE
command list with a 400 on every single registration attempt - not just
that one command. /cron/register-discord-commands swallowed the real error
behind a generic 500, and the CI step that calls it after every deploy only
*warns* on a non-401 failure rather than failing the build, so this went
unnoticed: /smart-wallets pending (built and "deployed" in good faith,
fully tested) never actually reached Discord at all, because the ENTIRE
command list silently failed to register on every deploy since the
unrelated command that broke the limit shipped, not just the deploys that
touched smart-wallets.
"""
import register_commands

_NAME_MAX = 32
_DESCRIPTION_MAX = 100


def _walk_commands(commands):
    """Yields (path, node) for every command, subcommand, subcommand group,
    and option in the tree - a description-length bug can hide at any of
    these levels, not just the top-level command."""
    for cmd in commands:
        yield cmd["name"], cmd
        yield from _walk_options(cmd["name"], cmd.get("options") or [])


def _walk_options(parent_path, options):
    for opt in options:
        path = f"{parent_path}.{opt['name']}"
        yield path, opt
        yield from _walk_options(path, opt.get("options") or [])
        for choice in opt.get("choices") or []:
            yield f"{path}[choice={choice.get('name')}]", choice


def test_every_command_name_is_within_discords_length_limit():
    over_limit = [
        (path, len(node["name"]))
        for path, node in _walk_commands(register_commands.COMMANDS)
        if "name" in node and len(node["name"]) > _NAME_MAX
    ]
    assert over_limit == [], f"name(s) over Discord's {_NAME_MAX}-char limit: {over_limit}"


def test_every_command_description_is_within_discords_length_limit():
    # The exact real bug: alert-tracker-record's description alone broke
    # registration for every command in the same PUT request, including
    # ones that were never touched by whatever change introduced it.
    over_limit = [
        (path, len(node["description"]))
        for path, node in _walk_commands(register_commands.COMMANDS)
        if "description" in node and len(node["description"]) > _DESCRIPTION_MAX
    ]
    assert over_limit == [], f"description(s) over Discord's {_DESCRIPTION_MAX}-char limit: {over_limit}"


def test_top_level_command_names_are_lowercase_with_no_spaces():
    # Discord requires top-level command names to match ^[-_\\p{L}\\p{N}]+$ -
    # no spaces, no uppercase ASCII. Scoped to top-level only here (options
    # follow the same rule but aren't user-typed the same way, and this is
    # the level a copy-paste-from-a-sentence mistake is most likely to hit).
    bad = [cmd["name"] for cmd in register_commands.COMMANDS if cmd["name"] != cmd["name"].lower() or " " in cmd["name"]]
    assert bad == [], f"command name(s) with uppercase or spaces (not valid Discord command names): {bad}"


def test_no_command_has_more_than_25_options():
    # Discord's own hard cap per command/subcommand level.
    over_limit = [
        cmd["name"] for cmd in register_commands.COMMANDS
        if len(cmd.get("options") or []) > 25
    ]
    assert over_limit == [], f"command(s) with more than 25 options: {over_limit}"
