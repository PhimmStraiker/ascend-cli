"""
test_async_poll — `map` recognizes the async POST-then-GET pattern: a POST that returns
just a conversation/job id (no inline answer) is diagnosed `async_poll` and pointed at the
session_poll adapter, rather than a dead "no answer" / "bad shape".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from discovery.probe import _looks_like_async_ack  # noqa: E402


def test_conversation_id_is_an_ack():
    assert _looks_like_async_ack({"conversationId": "conv-1", "status": "queued"}) == "conv-1"


def test_job_id_is_an_ack():
    assert _looks_like_async_ack({"jobId": "j-9"}) == "j-9"


def test_bare_id_needs_a_pending_status():
    # generic completed ack — NOT async
    assert _looks_like_async_ack({"id": "550e8400-e29b-41d4-a716-446655440000",
                                  "status": "ok"}) is None
    # bare id + pending status — async
    assert _looks_like_async_ack({"id": "abc", "status": "processing"}) == "abc"


def test_real_answer_is_not_an_ack():
    assert _looks_like_async_ack(
        {"id": "x", "response": "Here is a genuine long agent answer, well over forty chars."}
    ) is None


def test_non_dict_is_not_an_ack():
    assert _looks_like_async_ack(["conversationId"]) is None
    assert _looks_like_async_ack("conv-1") is None
