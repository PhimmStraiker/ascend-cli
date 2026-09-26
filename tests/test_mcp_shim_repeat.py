"""
A repeatable CLI flag has to be expressible as a tool argument.

Several `ascend` options are `action="append"` — a fleet of targets (`--app a --app b`), several
headers. `build_argv` only knew positionals, flags and single-valued options, so a list arrived
as its Python repr and was passed through as one nonsense value: an agent could drive one target
at a time and no more. That is the difference between testing an agent and testing an estate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shells" / "mcp"))
import server as shim  # noqa: E402


@pytest.fixture
def fleet_tool():
    tool = {"name": "t_repeat", "description": "x", "cli": ["assess", "run"],
            "params": {"app": {"kind": "repeat", "flag": "--app"},
                       "name": {"kind": "option", "flag": "--name"}},
            "schema": {}}
    shim.TOOLS.append(tool)
    shim.TOOLS_BY_NAME[tool["name"]] = tool
    yield tool
    shim.TOOLS.remove(tool)
    shim.TOOLS_BY_NAME.pop(tool["name"], None)


def tail(argv):
    return argv[argv.index("--json") + 1:]


def test_a_list_becomes_one_flag_per_item(fleet_tool):
    assert tail(shim.build_argv("t_repeat", {"app": ["a", "b", "c"], "name": "r"})) == [
        "assess", "run", "--app", "a", "--app", "b", "--app", "c", "--name", "r"]


def test_a_bare_value_still_works(fleet_tool):
    assert tail(shim.build_argv("t_repeat", {"app": "solo"})) == ["assess", "run", "--app", "solo"]


def test_empty_entries_are_dropped(fleet_tool):
    """A model producing a list with a hole in it must not emit `--app ''`, which argparse takes
    as a real (empty) app name and resolves to nothing."""
    assert tail(shim.build_argv("t_repeat", {"app": ["a", "", None, "b"]})) == [
        "assess", "run", "--app", "a", "--app", "b"]


def test_an_omitted_repeat_emits_nothing(fleet_tool):
    assert tail(shim.build_argv("t_repeat", {"name": "r"})) == ["assess", "run", "--name", "r"]


def test_the_repr_never_reaches_argv(fleet_tool):
    """The shape of the original bug: `['a', 'b']` passed as a single argument."""
    argv = shim.build_argv("t_repeat", {"app": ["a", "b"]})
    assert not any("[" in part for part in argv), argv
