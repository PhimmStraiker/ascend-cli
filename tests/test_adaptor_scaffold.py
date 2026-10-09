"""
The shipped adaptor JavaScript and `ascend adaptor scaffold`.

The toolkit's own scaffold suite drives a real engine isolate, which this repo cannot. What it
can pin is the contract that matters before any isolate runs: both files exist, define
`sendTurn`, and use nothing the publish gate refuses — so an SE's first paste is never refused by
our own gate — and the command hands them over byte for byte.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
import adaptor as AD  # noqa: E402

TEMPLATES = {"scaffold": REPO / "templates" / "adaptor_scaffold.js",
             "example": REPO / "templates" / "adaptor_example_chattie.js"}

# What the gate refuses (docs/CUSTOM_ADAPTOR.md, "Rules the gate enforces"). Checked on the
# source with comments stripped, because the JSDoc typedefs legitimately say `import("./host")`.
BANNED = ("async", "await", "Promise", "fetch", "XMLHttpRequest", "require", "import", "eval",
          "setTimeout", "setInterval", "console")


def _code_only(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
class TestTheShippedFiles:
    def test_it_ships_and_defines_send_turn(self, kind):
        src = TEMPLATES[kind].read_text(encoding="utf-8")
        assert "function sendTurn(turn, host)" in src
        assert AD.template_js(kind) == src

    def test_it_uses_nothing_the_gate_refuses(self, kind):
        code = _code_only(TEMPLATES[kind].read_text(encoding="utf-8"))
        for word in BANNED:
            assert not re.search(rf"\b{word}\b", code), f"{kind} uses `{word}`"

    def test_it_is_typed_against_the_spec_next_to_it(self, kind):
        """`ascend adaptor spec --out host.d.ts` lands beside the file, so the typedef says ./host."""
        src = TEMPLATES[kind].read_text(encoding="utf-8")
        assert 'import("./host")' in src and 'import("../host")' not in src

    def test_a_turn_built_by_hand_is_survivable(self, kind):
        """The debugger and every test build turns without params/headers; the file everyone
        copies must model the defensive read."""
        src = TEMPLATES[kind].read_text(encoding="utf-8")
        assert "turn.params || {}" in src
        assert "turn.payload" in src


class TestTheScaffoldContract:
    def test_reached_is_a_pass_and_unreachable_is_the_only_failure(self):
        src = TEMPLATES["scaffold"].read_text(encoding="utf-8")
        assert "status_code: 502" in src and "reached: false" in src
        assert "status_code: 200" in src and "reached: true" in src
        assert "target_status: res.status" in src, "the target's own status is reported, not judged"

    def test_the_example_returns_the_apps_reply_shape(self):
        src = TEMPLATES["example"].read_text(encoding="utf-8")
        assert "body: { response: text }" in src
        assert '"Content-Type": "application/json"' in src
