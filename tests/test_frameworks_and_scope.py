"""Response-shape recognition names a reply path (advisory), and capability->scope proposes the
right controls with a reason. Both are read-only and additive."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.frameworks import recognize  # noqa: E402
from scope import propose  # noqa: E402


def _ev(body):
    import json
    return {"prompt_sent": "hi", "pairs": [{"request": {"raw_body": '{"message":"hi"}'},
            "response": {"raw_body": json.dumps(body), "content_type": "application/json"}}]}


def test_recognises_the_common_envelopes():
    assert recognize(_ev({"choices": [{"message": {"role": "assistant", "content": "hello"}}]}))["response_path"] == "choices.0.message.content"
    assert recognize(_ev({"content": [{"type": "text", "text": "hi"}]}))["response_path"] == "content.*.text"
    assert recognize(_ev([{"recipient_id": "u", "text": "hi"}]))["response_path"] == "*.text"
    assert recognize(_ev({"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}))["response_path"] == "messages.-1.content"
    assert recognize(_ev({"reply": "hello"}))["response_path"] == "reply"
    assert recognize(_ev({"result": {"answer": "hello"}}))["response_path"] == "result.answer"
    assert recognize(_ev({"data": '{"reply":"x"}'}))["response_path"] == "data~json"


def test_unknown_shape_and_no_body_are_none_not_a_guess():
    assert recognize(_ev({"foo": {"bar": 1}}))["framework"] is None
    assert recognize({"pairs": []})["framework"] is None


def test_scope_maps_capabilities_to_controls_with_reasons():
    p = propose({"rag": True, "tools": True, "code_interpreter": False})
    controls = [x["control"] for x in p["scope"]]
    assert "app_grounding" in controls and "data_leak" in controls and "agentic_tmu" in controls
    assert "agentic_rce" not in controls
    assert all(x["why"] for x in p["scope"])
    assert [b["control"] for b in p["baseline"]] == ["sys_prompt_leak", "jailbreak"]
    assert "rag" in p["matched"] and "tools" in p["matched"]


def test_scope_accepts_a_flat_capability_list_and_dedupes():
    p = propose(["retrieval knowledge base", "user PII records", "sub-agent orchestrator"])
    controls = [x["control"] for x in p["scope"]]
    assert controls.count("data_leak") == 1                 # rag + user_data both imply it, listed once
    assert "agentic_data_exfil" in controls and "agentic_tmu" in controls


def test_no_capabilities_recommends_only_the_baseline_and_says_so():
    p = propose({})
    assert p["scope"] == [] and p["matched"] == []
    assert any("no agentic capabilities" in n for n in p["notes"])
