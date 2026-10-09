"""
test_trace_masks_credentials.py — the engine's host-call trace never prints a credential.

MEASURED 2026-10-09 on a WebSocket target behind an access code. `adaptor test` printed the
transcript of what the adaptor did, and two lines of it carried the app's URL with its
`?code=<access code>` in clear: the result of `config.get` and the `args` of `ws.connect`.
`redact_url` existed, but it recognised `key` and `apikey` only, and only a string that IS a URL;
the trace prints a `repr` of a dict with a URL inside it. The probe's own echo of the URL the
operator typed (`[1/5] probing wss://…?code=…`) had the same gap.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
import manual  # noqa: E402

CODE = "lab-code-9f13c2-SECRET"
WS = f"wss://kwaq.execute-api.us-east-2.amazonaws.com/demo?code={CODE}"
STEP = {"host_calls": [
    {"fn": "config.get", "ms": 0.0, "result": WS, "args": {"field": "endpoint"}},
    {"fn": "ws.connect", "ms": 84.8, "result": {"handle": "h-1", "reused": False},
     "args": {"url": WS, "opts": {"headers": {"Cookie": "sid=abc123def456", "x-lab-code": CODE}}}},
    {"fn": "http.request", "ms": 12.0, "error": f"connect to {WS} failed", "args": {"url": WS}},
]}


class TestTheHostCallTrace:
    def test_no_line_carries_the_access_code(self):
        text = "\n".join(ascend._render_host_calls(STEP))
        assert CODE not in text
        assert "code=[REDACTED]" in text
        assert "kwaq.execute-api.us-east-2.amazonaws.com/demo" in text, "the address stays readable"

    def test_the_result_the_args_and_the_error_are_all_masked(self):
        lines = ascend._render_host_calls(STEP)
        assert all(CODE not in ln for ln in lines)
        assert any("config.get" in ln and "[REDACTED]" in ln for ln in lines)
        assert any("ERROR" in ln and "[REDACTED]" in ln for ln in lines)

    def test_a_cookie_inside_the_request_options_is_masked_too(self):
        text = "\n".join(ascend._render_host_calls(STEP))
        assert "abc123def456" not in text

    def test_a_minted_session_value_is_shown_as_a_masked_tail(self):
        """Measured live: `state.conv.set` printed the per-conversation token in clear."""
        tok = "ct_8f2c41d07b3e5a8c1290ffeeaabbccdd"
        step = {"host_calls": [
            {"fn": "state.conv.set", "ms": 1.2, "result": None,
             "args": {"slot": "session_id", "value": tok, "opts": {"ttlMs": 3600000}}},
            {"fn": "state.conv.get", "ms": 0.3, "result": tok, "args": {"slot": "session_id"}},
        ]}
        text = "\n".join(ascend._render_host_calls(step))
        assert tok not in text.split("state.conv.get")[0], "the set line masks the value"
        assert "'slot': 'session_id'" in text and "(35 chars)" in text

    def test_a_short_state_value_such_as_a_counter_is_left_alone(self):
        step = {"host_calls": [{"fn": "state.set", "ms": 1, "result": None,
                                "args": {"slot": "turns", "value": "3"}}]}
        assert "'value': '3'" in "\n".join(ascend._render_host_calls(step))

    def test_a_public_query_string_is_left_alone(self):
        step = {"host_calls": [{"fn": "http.request", "ms": 1, "result": "ok",
                                "args": {"url": "https://h/openai/deployments/x?api-version=2024-06-01&model=gpt-4o"}}]}
        text = "\n".join(ascend._render_host_calls(step))
        assert "api-version=2024-06-01" in text and "model=gpt-4o" in text


class TestTheMask:
    @pytest.mark.parametrize("url", [
        WS,
        f"https://h/chat?key={CODE}",
        f"https://h/chat?token={CODE}&v=2",
        f"https://h/chat?x_api_key={CODE}",
        f"https://h/chat?opaque=AbCdEf0123456789_-abcdefGHIJ",
    ])
    def test_credential_shaped_parameters_are_masked(self, url):
        out = ascend._mask_urls(url)
        assert CODE not in out and "AbCdEf0123456789_-abcdefGHIJ" not in out
        assert "[REDACTED]" in out

    def test_a_url_inside_other_text_is_found(self):
        out = manual.redact_text(f"args: {{'url': '{WS}', 'opts': {{}}}}")
        assert CODE not in out and "'opts': {}" in out

    def test_a_dict_is_rendered_not_dropped(self):
        out = ascend._mask_urls({"url": WS, "n": 1})
        assert CODE not in out and "'n': 1" in out

    def test_masking_never_raises(self):
        assert ascend._mask_urls(None) == "None"
        assert ascend._mask_urls(object()).startswith("<object")


def test_the_probe_echo_lines_go_through_the_mask():
    """Source discipline: the three places a typed URL is echoed use the same mask."""
    src = (REPO / "shells" / "cli" / "ascend.py").read_text()
    assert 'f"probing {_mask_urls(args.ws)}"' in src
    assert 'f"probing {_mask_urls(args.api)}"' in src
    assert "_ok(f\"WS {_mask_urls(res['ws_url'])}" in src
    assert 'f"capturing the contract from {_mask_urls(args.url or args.har)}"' in src
