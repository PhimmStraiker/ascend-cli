"""
test_sse_variants — the one streaming decoder must reassemble every SSE flavour we see:
classic `data:` JSON, NDJSON, raw plaintext token stream, and NAMED events (event: done)
whose content has no JSON `type` discriminator. Offline; drives `_read_stream` with a fake
streamed response.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from adapters.sse_stream import SSEStreamAdapter  # noqa: E402


class FakeStream:
    """Minimal requests.Response for _read_stream: iter_lines() + close()."""
    def __init__(self, lines):
        # lines are strings; SSE frames are separated by "" (blank line)
        self._lines = [l.encode("utf-8") if l else b"" for l in lines]

    def iter_lines(self, decode_unicode=False):
        for l in self._lines:
            yield l

    def close(self):
        pass


def _read(lines, cfg):
    a = SSEStreamAdapter()
    text, truncated, stalled = a._read_stream(FakeStream(lines), cfg, deadline=time.time() + 30)
    return text


def test_classic_sse_data_json():
    lines = [
        'data: {"type":"token","content":"Hello"}', "",
        'data: {"type":"status","content":"thinking"}', "",
        'data: {"type":"token","content":" world"}', "",
        'data: {"type":"done"}', "",
    ]
    assert _read(lines, {"format": "sse"}) == "Hello world"


def test_ndjson():
    lines = [
        '{"type":"token","content":"a"}',
        '{"type":"token","content":"b"}',
        '{"type":"done"}',
    ]
    assert _read(lines, {"format": "ndjson"}) == "ab"


def test_plaintext_stream():
    lines = ["The ", "answer ", "is ", "42", "[DONE]"]
    assert _read(lines, {"format": "plaintext"}) == "The answer is 42"


def test_named_event_content_only_in_done():
    # billing-copilot shape: status events carry no answer; the answer is the `done` data.
    lines = [
        "event: status", 'data: {"state":"working"}', "",
        "event: done", 'data: {"answer":"Final reply."}', "",
    ]
    cfg = {"format": "sse", "token_events": ["done"], "done_events": ["done"],
           "text_path": "answer"}
    assert _read(lines, cfg) == "Final reply."


def test_named_event_token_stream_then_done():
    lines = [
        "event: message", 'data: {"delta":"Hel"}', "",
        "event: message", 'data: {"delta":"lo"}', "",
        "event: done", "",              # bodyless terminator
    ]
    cfg = {"format": "sse", "token_events": ["message"], "done_events": ["done"],
           "text_path": "delta"}
    assert _read(lines, cfg) == "Hello"


# --------------------------------------------------------------------------- create step
def test_conversation_create_server_id(monkeypatch):
    """REST create conversation -> POST /conversations/{id}/responses -> SSE named events.
    This was the LAST legacy-only adapter pattern; {{CONV}} closes it."""
    a = SSEStreamAdapter()
    calls = []

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        def json(self): return {"id": "conv-77"}
        def raise_for_status(self): pass

    class FakeSession:
        def request(self, method, url, **kw):
            calls.append((method, url))
            return FakeResp()

    monkeypatch.setattr(a, "_get_session", lambda cfg: FakeSession())
    cfg = {"base_url": "http://x", "create": {"url": "/api/conversations", "id_path": "id"}}
    assert a._ensure_conversation(cfg, 5.0) == "conv-77"
    assert calls == [("POST", "http://x/api/conversations")]
    # reused, not re-minted, on the next prompt
    assert a._ensure_conversation(cfg, 5.0) == "conv-77"
    assert len(calls) == 1


def test_conversation_create_client_id(monkeypatch):
    a = SSEStreamAdapter()
    sent = {}

    class FakeResp:
        status_code = 200
        headers = {}
        def json(self): return {}
        def raise_for_status(self): pass

    class FakeSession:
        def request(self, method, url, **kw):
            sent["body"] = kw.get("data")
            return FakeResp()

    monkeypatch.setattr(a, "_get_session", lambda cfg: FakeSession())
    cfg = {"base_url": "http://x",
           "create": {"url": "/c", "id_mode": "client", "body": {"id": "{{CONV}}"}}}
    conv = a._ensure_conversation(cfg, 5.0)
    assert conv and conv.startswith("abv2-")
    assert conv.encode() in sent["body"]        # the id we generated was POSTed


def test_no_create_block_means_no_conversation():
    assert SSEStreamAdapter()._ensure_conversation({"base_url": "http://x"}, 5.0) is None
