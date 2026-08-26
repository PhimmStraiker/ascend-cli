"""
A lost response must never be reported as a failed assessment start.

`POST /assessments` is not idempotent, so it is deliberately excluded from the session's automatic
retry policy. That leaves a real window: the server creates the run, the connection drops while the
response is being read, and the client sees an exception for an operation that SUCCEEDED.

Reporting that as a failure is worse than useless — the operator retries, a second assessment
starts, and now two runs burn the target's rate limit while the Console shows a duplicate nobody
meant to create. Observed live during a walkthrough rehearsal: `assess run` printed
"could not reach the API" for a run that was already running.

So the client asks the server what actually happened before deciding what to say.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "control"))

import api  # noqa: E402


class Dropped(Exception):
    """Stands in for requests' ConnectionError/RemoteDisconnected."""


def _client():
    return api.AscendAPI(token="s6r_pat_test")


class TestCreateAssessmentRecovery:
    def test_lost_response_returns_the_run_the_server_created(self):
        c = _client()
        listing = {"data": [{"id": "asmt_live", "name": "run 1", "status": "running"}]}

        def fake_req(method, path, **kw):
            if method == "POST":
                raise Dropped("Remote end closed connection without response")
            return listing

        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            got = c.create_assessment("aapp_x", "run 1")

        assert got["assessment_id"] == "asmt_live"
        assert got["recovered"] is True
        assert "did create" in got["recovery_note"]

    def test_a_genuine_failure_still_raises(self):
        """If the server really has no such run, the error must surface — not be swallowed."""
        c = _client()

        def fake_req(method, path, **kw):
            if method == "POST":
                raise Dropped("connection refused")
            return {"data": []}

        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            with pytest.raises(Dropped):
                c.create_assessment("aapp_x", "run 1")

    def test_an_unrelated_run_is_not_claimed_as_ours(self):
        """Matching must be by name; grabbing someone else's run would be worse than failing."""
        c = _client()
        listing = {"data": [{"id": "asmt_other", "name": "a different run", "status": "running"}]}

        def fake_req(method, path, **kw):
            if method == "POST":
                raise Dropped("dropped")
            return listing

        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            with pytest.raises(Dropped):
                c.create_assessment("aapp_x", "run 1")

    def test_a_failed_run_does_not_count_as_recovery(self):
        c = _client()
        listing = {"data": [{"id": "asmt_dead", "name": "run 1", "status": "failed"}]}

        def fake_req(method, path, **kw):
            if method == "POST":
                raise Dropped("dropped")
            return listing

        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            with pytest.raises(Dropped):
                c.create_assessment("aapp_x", "run 1")

    def test_a_failing_lookup_surfaces_the_original_error(self):
        """If we cannot check, we must not guess — raise what actually happened."""
        c = _client()

        with mock.patch.object(api.AscendAPI, "_req", side_effect=Dropped("dropped")):
            with pytest.raises(Dropped):
                c.create_assessment("aapp_x", "run 1")

    def test_the_happy_path_is_untouched(self):
        c = _client()
        with mock.patch.object(api.AscendAPI, "_req",
                               return_value={"id": "asmt_new", "status": "created"}):
            got = c.create_assessment("aapp_x", "run 1")
        assert got["id"] == "asmt_new"
        assert "recovered" not in got

    @pytest.mark.parametrize("envelope", [
        {"data": [{"id": "a1", "name": "run 1", "status": "running"}]},
        {"assessments": [{"id": "a1", "name": "run 1", "status": "running"}]},
        {"items": [{"id": "a1", "name": "run 1", "status": "running"}]},
        [{"id": "a1", "name": "run 1", "status": "running"}],
    ])
    def test_every_list_envelope_shape_is_understood(self, envelope):
        """Envelope drift here would silently turn recovery back into a false failure."""
        c = _client()

        def fake_req(method, path, **kw):
            if method == "POST":
                raise Dropped("dropped")
            return envelope

        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            assert c.create_assessment("aapp_x", "run 1")["assessment_id"] == "a1"


class TestRunPropagatesRecovery:
    def test_run_carries_the_flag_through_to_the_caller(self):
        c = _client()
        created = {"id": "asmt_live", "assessment_id": "asmt_live", "recovered": True,
                   "recovery_note": "the response was lost (Dropped), but the server did create it"}
        with mock.patch.object(api.AscendAPI, "create_assessment", return_value=created), \
             mock.patch.object(api.AscendAPI, "_safe_transition"):
            out = c.run("aapp_x", "run 1", wait=False)
        assert out["assessment_id"] == "asmt_live"
        assert out["recovered"] is True
        assert out["recovery_note"]

    def test_no_flag_on_a_normal_run(self):
        c = _client()
        with mock.patch.object(api.AscendAPI, "create_assessment",
                               return_value={"id": "asmt_new"}), \
             mock.patch.object(api.AscendAPI, "_safe_transition"):
            out = c.run("aapp_x", "run 1", wait=False)
        assert "recovered" not in out


class TestRetryPolicyStillExcludesPost:
    def test_post_is_not_auto_retried(self):
        """Auto-retrying a create would double-start runs; the guard above exists BECAUSE of this."""
        src = (REPO / "control" / "api.py").read_text()
        assert "allowed_methods" in src
        block = src[src.index("allowed_methods"):src.index("allowed_methods") + 160]
        assert "POST" not in block, "POST must stay out of the automatic retry set"


class TestFailureAfterCreate:
    """Past the create, the assessment demonstrably exists. Saying otherwise causes a duplicate.

    Observed live during a walkthrough rehearsal: the connection dropped during the POLL and
    `assess run` printed "could not reach the API" for a run that was already at 45%.
    """

    def _client_that_creates_then_drops(self, state):
        c = _client()
        calls = {"n": 0}

        def fake_req(method, path, **kw):
            if method == "POST" and path.endswith("/assessments"):
                return {"id": "asmt_live", "status": "created"}
            calls["n"] += 1
            if calls["n"] == 1:
                raise Dropped("Remote end closed connection without response")
            return state
        return c, fake_req

    def test_a_drop_after_create_returns_the_run_not_an_error(self):
        c, fake = self._client_that_creates_then_drops(
            {"id": "asmt_live", "status": "running", "progress": 0.45})
        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake):
            out = c.run("aapp_x", "run 1", wait=False)
        assert out["assessment_id"] == "asmt_live"
        assert out["recovered"] is True
        assert "connection dropped" in out["recovery_note"]
        assert "running" in out["recovery_note"]

    def test_a_run_left_unstarted_says_so_explicitly(self):
        """If the resume never landed, the run exists but is NOT going — that must be actionable."""
        c, fake = self._client_that_creates_then_drops({"id": "asmt_live", "status": "created"})
        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake):
            out = c.run("aapp_x", "run 1", wait=False)
        assert "NOT running" in out["recovery_note"]
        assert "assess resume" in out["recovery_note"]

    def test_if_the_state_cannot_be_read_the_real_error_surfaces(self):
        """Never invent reassurance we cannot support."""
        c = _client()

        def fake_req(method, path, **kw):
            if method == "POST" and path.endswith("/assessments"):
                return {"id": "asmt_live", "status": "created"}
            raise Dropped("everything is down")
        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            with pytest.raises(Dropped):
                c.run("aapp_x", "run 1", wait=False)

    def test_the_happy_path_is_unchanged(self):
        c = _client()

        def fake_req(method, path, **kw):
            if method == "POST" and path.endswith("/assessments"):
                return {"id": "asmt_new", "status": "created"}
            return {"id": "asmt_new", "status": "running"}
        with mock.patch.object(api.AscendAPI, "_req", side_effect=fake_req):
            out = c.run("aapp_x", "run 1", wait=False)
        assert out["assessment_id"] == "asmt_new"
        assert "recovered" not in out
