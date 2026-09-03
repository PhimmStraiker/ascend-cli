"""
test_poll_derivation.py — the ACK-only web-chat contract must be derivable from evidence.

The bug this locks down: `_detect_poll` published its findings as `submit` / `poll.
endpoint_template`, while `_session_poll_from_poll` read `create_url` / `send_url` / `poll_url`.
Two schemas, never connected. Every lookup fell through to a default, so the composed config
always carried three EMPTY urls plus plausible-looking guesses, and `session_poll` refused it
with "needs create.url, send.url and poll.url" -- unconditionally, for any input.

That made the shape session_poll's own example calls "the most common enterprise web-chat
contract" impossible to onboard from a capture. It had to be hand-written every time, which is
exactly the friction that makes the CLI feel broken on a real engagement.

The fixture is REAL captured traffic, not hand-written: agent-forge served the three-step ACK
contract, the calls were made for real, and the HAR was recorded off the wire, then had only its
host and conversation id normalized so it compares equal on any machine. A hand-written HAR would
encode my assumption about the payload shape, which is how a test ends up agreeing with the bug.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from discovery import classify  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ack_poll.har"


@pytest.fixture(scope="module")
def composed():
    """Classify + compose the real capture, the way `target add <file.har>` does."""
    har = json.loads(FIXTURE.read_text())
    ev = classify.har_to_evidence(har)
    return classify.compose(classify.classify_evidence(ev))


class TestAckOnlyContractIsDerivable:
    def test_it_picks_session_poll(self, composed):
        assert composed["adapter"] == "session_poll"

    def test_all_three_urls_are_populated(self, composed):
        """The whole bug in one assertion: these were empty strings for every input."""
        for step in ("create", "send", "poll"):
            url = composed[step]["url"]
            assert url, f"{step}.url is empty — the adapter refuses this config outright"
            assert url.startswith("http"), f"{step}.url is not a url: {url!r}"

    def test_the_conversation_id_is_templated_not_baked_in(self, composed):
        """A captured id left in the URL means every probe reuses one dead conversation."""
        assert "{{CONV}}" in composed["send"]["url"]
        assert "{{CONV}}" in composed["poll"]["url"]
        assert "c0nv3rsat10n1d" not in json.dumps(composed), \
            "the captured conversation id survived into the config"

    def test_the_create_step_is_found_by_looking_backward(self, composed):
        """create happens BEFORE the prompt-carrying pair, so a forward-only scan misses it."""
        assert composed["create"]["url"].endswith("/conversation/new")
        assert composed["create"]["extract"] == "conversation_id"

    def test_the_prompt_is_templated_into_the_send_body(self, composed):
        assert "{{PROMPT}}" in json.dumps(composed["send"]["body"])

    def test_the_poll_query_string_survives(self, composed):
        """The transcript endpoint keys off a query param; dropping it polls the wrong thing."""
        assert "conversation_id={{CONV}}" in composed["poll"]["url"]


class TestTranscriptShapeComesFromEvidence:
    """list_path / role_field / text_path / bot_roles were hardcoded; now they are read."""

    def test_bot_roles_are_observed_not_defaulted(self, composed):
        # The capture contains exactly one bot role. Getting the stock 4-5 item default list
        # back means the evidence was ignored -- which is what happened while the detector
        # sampled the FIRST poll response, fired before the bot had answered.
        assert composed["poll"]["bot_roles"] == ["assistant"], \
            f"expected the observed role, got {composed['poll']['bot_roles']}"

    def test_transcript_paths_match_the_capture(self, composed):
        p = composed["poll"]
        assert p["list_path"] == "messages"
        assert p["role_field"] == "role"
        assert p["text_path"] == "text"

    def test_the_last_poll_response_is_sampled(self, composed):
        """Six entries, four of them polls; only the last contains the assistant turn."""
        har = json.loads(FIXTURE.read_text())
        polls = [e for e in har["log"]["entries"] if e["request"]["method"] == "GET"]
        assert len(polls) >= 2, "fixture must contain a real polling loop, not one lucky GET"
        first = json.loads(polls[0]["response"]["content"]["text"])
        last = json.loads(polls[-1]["response"]["content"]["text"])
        assert not [m for m in first["messages"] if m["role"] == "assistant"], \
            "fixture no longer proves the first poll is incomplete"
        assert [m for m in last["messages"] if m["role"] == "assistant"]


class TestTwoStepJobApiIsExplained:
    """A submit->status API has no create call, and no shipped adapter models it.

    The adapter's own error ("needs create.url, send.url and poll.url") reads like a capture
    problem, so the config now carries a note saying what the shape actually is.
    """

    def test_a_capture_with_no_create_gets_an_explanatory_note(self):
        har = json.loads(FIXTURE.read_text())
        # drop the create call, leaving submit + polls: the two-step job shape
        har["log"]["entries"] = [e for e in har["log"]["entries"]
                                 if not e["request"]["url"].endswith("/conversation/new")]
        ev = classify.har_to_evidence(har)
        composed = classify.compose(classify.classify_evidence(ev))
        if composed.get("adapter") != "session_poll":
            pytest.skip("without a create call this capture no longer classifies as poll")
        assert "_note" in composed
        assert "two-step job" in composed["_note"]
