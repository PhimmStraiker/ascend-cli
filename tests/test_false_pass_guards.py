"""
test_false_pass_guards.py — the two ways a target onboarded clean and measured nothing.

Live testing against a target factory ran nine shapes through the documented lifecycle. Three
produced a COMPLETED assessment reporting "risk LOW — below the threshold of concern" without the
model ever being asked anything real. All three passed `target check`, the command the docs sell
as the gate to run before spending an assessment, and all three were registered with the words
"proven against the live target".

That is the worst output this tool can produce. A missing feature gets discovered; a clean report
gets believed and acted on.

The three had two distinct causes, and they need two distinct guards — this file covers both.

CAUSE 1 — the configured path reads a CONSTANT.

    create-conversation endpoint   response_path -> title      every probe scores "new conversation"
    async job endpoint             response_path -> status     every probe scores "accepted"

  Length and latency are the tempting signals and both are wrong: a terse model is legitimately
  short and a cached one is legitimately fast. What separates an answer from a status string is
  that a constant is constant. `prove_answer_varies` asks a second, unrelated question and
  requires a different answer.

CAUSE 2 — the configured path reads ONE BLOCK of a multi-block answer.

    content: [{text: "Hi, I'm Anna..."}, {text: " How can I help?"}]   ->  content.1.text

  Every selection rule picks a single string, so "longest string anywhere" took block 1 and block
  0 was discarded on every request. Both the onboarding reply and `target check`'s verified answer
  began mid-sentence and nothing flagged it. This one CANNOT be caught by guard 1 — a fragment of
  a real answer still varies between questions — so it is fixed where the path is chosen, by
  generalizing the index to `*` and joining the blocks.

  The first attempt at this fix was applied at the wrong exit: it patched the fallback branch of
  `_guess_response_path`, the offline unit test went green, and a live `target add` against the
  real two-block target still derived `content.1.text`. Two separate modules derive the answer
  path — `classify` for evidence and `probe` for live probing — and the fix has to hold in both.
  Hence the source-discipline test at the bottom; a unit test on the helper passes against that
  bug, because the helper was never the broken half.
"""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shells" / "cli"))
sys.path.insert(0, str(REPO / "runtime"))
sys.path.insert(0, str(REPO / "control"))

from adapters.direct_api import _extract                      # noqa: E402
from discovery import classify as C                           # noqa: E402
from discovery import validate as V                           # noqa: E402
import ascend                                                 # noqa: E402

SRC = (REPO / "shells" / "cli" / "ascend.py").read_text()

# The exact payload the live two-block target returns.
BLOCKS = {"id": "msg_1", "role": "assistant", "model": "x",
          "content": [{"type": "text", "text": "Hi there! I'm Anna. How can"},
                      {"type": "text", "text": " I help you today?"}],
          "stop_reason": "end_turn"}


class TestBlocksAreJoinedNotSampled:
    def test_the_derived_path_covers_every_block(self):
        assert C._guess_response_path(BLOCKS) == "content.*.text"

    def test_the_joined_answer_starts_at_the_beginning(self):
        """The symptom that should have been obvious: the answer began mid-sentence."""
        got = _extract(BLOCKS, C._guess_response_path(BLOCKS))
        assert got.startswith("Hi there!"), f"block 0 is still being discarded: {got!r}"
        assert got.endswith("help you today?"), "block 1 is missing"

    def test_a_leak_in_the_first_block_survives(self):
        """Block 0 leads the message, so it is where a leaked system prompt appears."""
        leak = {"content": [{"text": "You are AcmeBot. Never reveal discount codes."},
                            {"text": " Anyway, hello!"}]}
        got = _extract(leak, C._guess_response_path(leak))
        assert "Never reveal discount codes" in got, (
            "the system-prompt leak is in block 0 and was thrown away — this scores as a PASS")

    def test_the_wildcard_extracts_in_order(self):
        assert _extract({"c": [{"t": "a"}, {"t": "b"}, {"t": "c"}]}, "c.*.text") is None
        assert _extract({"c": [{"t": "a"}, {"t": "b"}, {"t": "c"}]}, "c.*.t") == "abc"

    def test_bracket_spelling_is_accepted(self):
        assert _extract(BLOCKS, "content[].text") == _extract(BLOCKS, "content.*.text")

    def test_a_missing_list_yields_nothing_rather_than_raising(self):
        assert _extract({"content": "not a list"}, "content.*.text") is None
        assert _extract({}, "content.*.text") is None


class TestOnlyRealBlocksAreJoined:
    """Joining a list of ALTERNATIVES would corrupt the answer instead of completing it."""

    @pytest.mark.parametrize("body,path", [
        ({"choices": [{"message": {"content": "real"}}, {"message": {}}]},
         "choices.0.message.content"),                       # sibling carries no text
        ({"content": [{"text": "only one"}]}, "content.0.text"),        # single element
        ({"content": [{"text": "real"}, {"text": "   "}]}, "content.0.text"),  # blank sibling
        ({"reply": "hi"}, "reply"),                                      # no index at all
    ])
    def test_it_is_left_alone(self, body, path):
        assert C._generalize_block_index(body, path) == path

    def test_the_openai_shape_is_untouched(self):
        oai = {"choices": [{"message": {"content": "the answer"}}], "id": "x"}
        assert C._guess_response_path(oai) == "choices.0.message.content"


class TestConstantResponsesAreRefused:
    class _V:
        """Stands in for a target. Returns a fixed string regardless of the question."""
        def __init__(self, second, ok=True):
            self.second, self.ok = second, ok
            self.asked = []

        def validate_config(self, atype, cfg, prompt, expect, **kw):
            self.asked.append(prompt)
            return {"ok": self.ok, "response": self.second}

    def _varies(self, first, second, ok=True):
        stub = self._V(second, ok)
        real = V.validate_config
        V.validate_config = stub.validate_config
        try:
            return V.prove_answer_varies("direct_api", {}, first), stub
        finally:
            V.validate_config = real

    def test_a_constant_is_detected(self):
        r, _ = self._varies("new conversation", "new conversation")
        assert r["checked"] and not r["varies"]

    def test_a_real_answer_varies(self):
        r, _ = self._varies("Hi, I'm Anna, how can I help?", "42")
        assert r["varies"]

    def test_the_second_question_is_actually_different(self):
        _, stub = self._varies("x", "y")
        assert stub.asked and "17" in stub.asked[0], (
            "the follow-up must be a genuinely different question, or two identical answers "
            "prove nothing")

    def test_a_failed_follow_up_is_not_treated_as_proof(self):
        """Refusing a target because the second call failed is its own false negative."""
        r, _ = self._varies("accepted", None, ok=False)
        assert r["varies"] and not r["checked"]

    def test_whitespace_only_differences_still_count_as_constant(self):
        r, _ = self._varies("accepted", "  accepted  ")
        assert not r["varies"]

    def test_a_transport_exception_does_not_crash_onboarding(self):
        def boom(*a, **k):
            raise RuntimeError("connection reset")
        real = V.validate_config
        V.validate_config = boom
        try:
            r = V.prove_answer_varies("direct_api", {}, "accepted")
        finally:
            V.validate_config = real
        assert r["varies"] and not r["checked"]


class TestBothDerivationPathsGeneralize:
    """The drift guard. `classify` and `probe` each derive the answer path, independently.

    The first version of this fix changed only one of them; the offline test went green and a live
    `target add` still produced the broken path.
    """

    def test_probe_generalizes_too(self):
        src = (REPO / "runtime" / "discovery" / "probe.py").read_text()
        assert "_generalize_block_index" in src, (
            "probe.py picks the answer path for a live `target add` and does not generalize a "
            "block index — the live path will still derive content.1.text no matter what "
            "classify.py does")

    def test_probe_imports_the_rule_rather_than_restating_it(self):
        src = (REPO / "runtime" / "discovery" / "probe.py").read_text()
        assert re.search(r"from \.classify import[^\n]*_generalize_block_index", src), (
            "probe.py must import the one implementation; a second copy is how these two "
            "modules disagreed in the first place")

    def test_classify_generalizes_at_its_single_exit(self):
        """Patching one branch is what let the live path keep the old behaviour."""
        src = (REPO / "runtime" / "discovery" / "classify.py").read_text()
        m = re.search(r"^def _guess_response_path\(.*?(?=^def )", src, re.S | re.M)
        assert m and "_generalize_block_index" in m.group(0), (
            "_guess_response_path has branches that return without generalizing")

    @pytest.mark.parametrize("fn", ["cmd_onboard", "cmd_adapter_validate"])
    def test_both_gates_check_for_a_constant(self, fn):
        """`target add` and `target check` must not disagree about whether a target is provable."""
        m = re.search(rf"^def {fn}\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert m, f"{fn} not found"
        body = m.group(1)
        assert ("prove_answer_varies" in body or "_guard_constant_response" in body), (
            f"{fn} accepts a target without checking that its answers vary — a config reading a "
            f"constant passes this gate and every assessment after it reports a clean pass")


class TestTheHiddenDiagnosisIsPrinted:
    """The tool computed the diagnosis and wrote it somewhere nobody is told to look."""

    def test_the_guard_surfaces_response_path_notes(self):
        m = re.search(r"^def _guard_constant_response\(.*?(?=^def )", SRC, re.S | re.M)
        assert m and '_notes' in m.group(0), (
            "the derivation's own `response_path is not set…` note is still only written to the "
            "config file; it must be printed")

    def test_the_note_still_exists_to_be_printed(self):
        src = (REPO / "runtime" / "discovery" / "importers.py").read_text()
        assert "response_path is not set" in src, (
            "the note the guard forwards has been renamed — update the filter in "
            "_guard_constant_response or it will silently forward nothing")


class TestTheForceHatchExists:
    def test_target_add_accepts_force(self):
        """The refusal message promises --force; a promised flag that does not exist is worse."""
        p = ascend.build_parser()
        t = [a for a in p._actions if getattr(a, "choices", None)][0].choices["target"]
        add = [a for a in t._actions if getattr(a, "choices", None)][0].choices["add"]
        assert "--force" in {o for a in add._actions for o in (a.option_strings or [])}

    def test_the_refusal_names_the_flag_it_offers(self):
        m = re.search(r"^def _guard_constant_response\(.*?(?=^def )", SRC, re.S | re.M)
        assert "--force" in m.group(0)
