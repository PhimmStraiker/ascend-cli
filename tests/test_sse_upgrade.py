"""
test_sse_upgrade — a streaming target must not be written as a direct_api config.

The live defect: `_upgrade_streaming_shape` only recognised marker-framed (sentinel) streams, and
onboarding never called it at all. So the evidence you happened to use decided whether your
results meant anything:

  * `target add --api <url>`      -> probing detects the transport -> sse_stream, correct replies
  * `target add <curl-file>`      -> request built from the evidence, straight to validate
                                     -> direct_api whose "answer" was the raw `data:` frames

The second one PASSES the hard gate — HTTP 200 with a non-empty body looks exactly like success —
and then hands the scorer SSE protocol noise for an entire assessment. That is a false pass: the
run completes, every probe is answered, and nothing was ever measured.

Reproduced against a real streaming target before the fix, and these fixtures are the shape that
target actually returned.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend      # noqa: E402

# A real SSE reply: token deltas under `text`, typed by `type`, terminated by a done event.
SSE_BODY = (
    'data: {"type": "turn.start", "turn_id": "abc"}\n\n'
    'data: {"type": "text.delta", "text": "Hello"}\n\n'
    'data: {"type": "text.delta", "text": " there"}\n\n'
    'data: {"type": "text.delta", "text": "!"}\n\n'
    'data: {"type": "turn.done"}\n\n'
)


def test_sse_body_is_recognised():
    assert ascend._looks_like_sse(SSE_BODY) is True
    assert ascend._looks_like_sse('data: {"a": 1}\n\n') is True


def test_ordinary_json_is_not_mistaken_for_a_stream():
    for body in ('{"response": "hello"}', "plain text answer", "", "no data: here but mid-line"):
        assert ascend._looks_like_sse(body) is False, body


class _FakeValidator:
    """Stands in for discovery.validate — the upgrade must PROVE itself against the target."""

    def __init__(self, ok=True, response="Hello there!"):
        self.ok, self.response, self.calls = ok, response, []

    def validate_config(self, adapter, cfg, prompt, _x, timeout_s=None, verify_tls=True):
        self.calls.append((adapter, cfg))
        return {"ok": self.ok, "response": self.response}


class _Args:
    prompt = "hi"
    timeout = 30
    insecure = False


def _direct_api_cfg():
    return {"adapter": "direct_api", "endpoint": "https://h.example.com/api/chat/stream",
            "method": "POST", "headers": {"x-demo-key": "code"},
            "body": {"message": "{{PROMPT}}", "apiKey": "k"}}


def test_streaming_reply_is_promoted_to_sse_stream():
    V = _FakeValidator()
    out = ascend._upgrade_sse_shape(_direct_api_cfg(), SSE_BODY, _Args(), V)
    assert out is not None, "an SSE reply must not stay a direct_api config"
    cfg, vres = out
    assert cfg["adapter"] == "sse_stream"
    assert cfg["stream"]["format"] == "sse"
    assert cfg["stream"]["text_path"] == "text"          # derived from the frames, not guessed
    assert "text.delta" in cfg["stream"]["token_types"]
    assert vres["ok"] is True
    assert V.calls and V.calls[0][0] == "sse_stream"     # it re-proved itself live


def test_credentials_and_request_shape_survive_the_upgrade():
    """The whole point is keeping a working request — including both credentials."""
    cfg, _ = ascend._upgrade_sse_shape(_direct_api_cfg(), SSE_BODY, _Args(), _FakeValidator())
    assert cfg["headers"]["x-demo-key"] == "code"        # header credential
    assert cfg["request_template"]["apiKey"] == "k"      # body credential
    assert cfg["request_template"]["message"] == "{{PROMPT}}"
    assert cfg["base_url"] == "https://h.example.com"
    assert cfg["chat_path"] == "/api/chat/stream"
    assert "endpoint" not in cfg and "body" not in cfg   # direct_api-only keys are dropped


def test_upgrade_is_abandoned_when_the_new_shape_cannot_answer():
    """An unproven 'better' config is worse than a working one — keep the original."""
    assert ascend._upgrade_sse_shape(_direct_api_cfg(), SSE_BODY, _Args(),
                                     _FakeValidator(ok=False)) is None


def test_no_upgrade_when_frames_reassemble_to_nothing():
    """Trading frames for an empty answer would read as a refusing target — strictly worse."""
    empty = 'data: {"type": "ping"}\n\ndata: {"type": "turn.done"}\n\n'
    assert ascend._upgrade_sse_shape(_direct_api_cfg(), empty, _Args(), _FakeValidator()) is None


def test_non_direct_api_configs_are_left_alone():
    cfg = {"adapter": "sse_stream", "base_url": "https://h", "chat_path": "/c"}
    V = _FakeValidator()
    assert ascend._upgrade_streaming_shape(cfg, {"response": SSE_BODY}, _Args(), V) == (
        cfg, {"response": SSE_BODY})
    assert V.calls == []
