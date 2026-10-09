"""
test_bridge_vocabulary — the thing that forwards probes is a bridge, everywhere a user reads.

"Relay" was the old name and it survived in help text, printed messages and one generated file.
A reader who meets both words has to work out whether they are two things. They are not. The
noun is `bridge`; the verb is `forwards`. Identifiers are exempt — `ascend relay` still works as
a hidden alias, the `relays` JSON key and `ASCEND_RELAY_APP_ID` are contracts other code reads —
because renaming those breaks callers without helping any reader.

The generated adapter module ships inside the customer's own repository, so it is held to the
same standard as the help text, and to the internal-name rule as well.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "shells" / "cli" / "ascend.py"
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from runtime.discovery import codegen  # noqa: E402
from test_no_internal_codenames import INTERNAL  # noqa: E402  — the one list of internal names

# The word on its own. An identifier (`relay_state`, `ASCEND_RELAY_APP_ID`, `relays_running`) is
# joined to its neighbours by an underscore or a letter and does not match.
RELAY = re.compile(r"(?<![A-Za-z0-9_])relays?(?![A-Za-z0-9_])", re.I)
#: String literals that ARE identifiers: the alias name, the JSON key, the state directory.
IDENTIFIER_LITERALS = {"relay", "relays"}


def _internal_name_pattern(word):
    loose = re.escape(word).replace("_", "[ _-]?")
    return re.compile(rf"\b{loose}\b", re.I)


CONFIGS = {
    "direct_api": {"adapter": "direct_api", "endpoint": "https://bot.example.com/api/chat",
                   "method": "POST", "body": {"message": "{{PROMPT}}"}, "response_path": "reply",
                   "headers": {"x-demo-key": "pass"}},
    "sentinel_stream": {"adapter": "sentinel_stream", "url": "https://bot.example.com/stream",
                        "body": {"q": "{{PROMPT}}"}, "begin_marker": "<<", "end_marker": ">>",
                        "extract": {"events_path": "events", "text_field": "text"}},
    "custom": {"adapter": "something_unknown", "url": "https://bot.example.com/x",
               "body": {"prompt": "{{PROMPT}}"}},
}


class TestTheGeneratedAdapterModule:
    @pytest.mark.parametrize("kind", sorted(CONFIGS))
    @pytest.mark.parametrize("source", ["build", "har", "url"])
    def test_it_names_no_internal_service_and_no_relay(self, kind, source):
        src = codegen.generate_adapter_module("demo-bot", CONFIGS[kind], source=source)
        assert "def send_prompt(" in src                      # the module is what it claims to be
        for word in INTERNAL:
            assert not _internal_name_pattern(word).search(src), \
                f"the generated adapter names {word!r}; a customer reads {INTERNAL[word]!r}"
        assert not RELAY.search(src), "the generated adapter says 'relay'; the word is 'bridge'"

    def test_the_preamble_says_bridge(self):
        src = codegen.generate_adapter_module("demo-bot", CONFIGS["direct_api"])
        assert "Ascend -> bridge -> adapter" in src


SCREENS = [[], ["target"], ["target", "add"], ["target", "inspect"], ["target", "check"],
           ["bridge"], ["bridge", "start"], ["assess", "run"], ["app", "create"],
           ["runtime", "start"]]


class TestHelpScreens:
    @pytest.mark.parametrize("screen", SCREENS, ids=lambda s: " ".join(s) or "top")
    def test_say_bridge_not_relay(self, screen):
        env = dict(os.environ, NO_COLOR="1", ASCEND_NO_SPINNER="1", ASCEND_SKIP_TENANT_CHECK="1",
                   STRAIKER_PAT="s6r_pat_test", COLUMNS="100")
        r = subprocess.run([sys.executable, str(CLI), *screen, "--help"],
                           capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        hits = [l.strip() for l in r.stdout.splitlines() if RELAY.search(l)]
        assert not hits, f"`ascend {' '.join(screen)} --help` says relay:\n  " + "\n  ".join(hits)


#: Every module that prints to a user. Docstrings are not user-facing and are left alone.
PRINTING_MODULES = ["shells/cli/ascend.py", "runtime/supervisor.py", "runtime/tenant.py",
                    "runtime/apicompat.py"]


def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


class TestPrintedMessages:
    @pytest.mark.parametrize("rel", PRINTING_MODULES)
    def test_every_string_a_user_can_see_says_bridge(self, rel):
        path = REPO / rel
        tree = ast.parse(path.read_text())
        skip = _docstring_ids(tree)
        hits = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in skip or node.value in IDENTIFIER_LITERALS:
                continue
            if RELAY.search(node.value):
                hits.append(f"{rel}:{node.lineno}: {node.value.strip()[:90]!r}")
        assert not hits, "these messages say relay; the word is bridge:\n  " + "\n  ".join(hits)
