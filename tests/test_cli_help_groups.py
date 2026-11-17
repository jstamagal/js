"""Every parser option belongs to one actual argparse help section."""

from js import command_line


def test_every_option_belongs_to_exactly_one_help_group():
    parser = command_line.parser()
    expected = {"RUNNING", "PROMPT", "WHAT THE MODEL GETS", "SAMPLING", "SESSIONS",
                "CONFIG", "OUTPUT & DEBUG", "MODELS & LOGINS", "MODES"}
    groups = [group for group in parser._action_groups if group._group_actions]
    assert {group.title for group in groups} == expected
    for action in parser._actions:
        owners = [group.title for group in groups if action in group._group_actions]
        assert len(owners) == 1, (action.option_strings, owners)
    help_text = parser.format_help()
    assert all(name + ":" in help_text for name in expected)
    assert parser.allow_abbrev is False
