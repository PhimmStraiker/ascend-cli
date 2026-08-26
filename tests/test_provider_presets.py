"""
test_provider_presets — the shipped example-*.json presets for the model providers must be
structurally valid AND extract the answer from that provider's real response shape. Guards the
">95% coverage" claim: OpenAI, Anthropic, Azure OpenAI (api-key + Entra), Gemini, Ollama, vLLM.
No network, no keys — the response shapes are hard-coded from each provider's API docs and run
through the adapter's own extractor.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from adapters.direct_api import _extract  # noqa: E402
sys.path.insert(0, str(REPO / "runtime"))
import dispatch  # noqa: E402

CFG = REPO / "configs"

# preset -> (a real-shaped response body, the expected extracted answer)
SHAPES = {
    "example-openai.json": (
        {"choices": [{"message": {"role": "assistant", "content": "OpenAI answer"}}]},
        "OpenAI answer"),
    "example-anthropic.json": (
        {"content": [{"type": "text", "text": "Claude answer"}]},
        "Claude answer"),
    "example-azure-openai.json": (
        {"choices": [{"message": {"content": "Azure answer"}}]},
        "Azure answer"),
    "example-azure-openai-entra.json": (
        {"choices": [{"message": {"content": "Azure Entra answer"}}]},
        "Azure Entra answer"),
    "example-gemini.json": (
        {"candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]},
        "Gemini answer"),
    "example-ollama.json": (
        {"message": {"role": "assistant", "content": "Ollama answer"}},
        "Ollama answer"),
    "example-vllm.json": (
        {"choices": [{"message": {"content": "vLLM answer"}}]},
        "vLLM answer"),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_preset_parses_and_registered(name):
    cfg = json.loads((CFG / name).read_text())
    assert cfg["adapter"] in dispatch.ADAPTER_REGISTRY
    assert "{{PROMPT}}" in json.dumps(cfg["body"])          # prompt placeholder present
    assert cfg.get("response_path")


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_preset_response_path_extracts_answer(name):
    cfg = json.loads((CFG / name).read_text())
    body, expected = SHAPES[name]
    assert _extract(body, cfg["response_path"]) == expected


# preset -> (env var it reads, the header name that must appear after merge_auth)
_APPLIES = {
    "example-openai.json": ("OPENAI_API_KEY", "authorization"),
    "example-anthropic.json": ("ANTHROPIC_API_KEY", "x-api-key"),
    "example-azure-openai.json": ("AZURE_OPENAI_KEY", "api-key"),
    "example-gemini.json": ("GEMINI_API_KEY", "x-goog-api-key"),
    "example-vllm.json": ("LOCAL_LLM_KEY", "authorization"),
}


@pytest.mark.parametrize("name", sorted(_APPLIES))
def test_static_preset_auth_actually_applies(name, monkeypatch):
    # Regression guard: the auth-block field is `type` (not `kind`); a wrong field name
    # would make merge_auth silently skip auth and every probe 401. Prove a header appears.
    env_var, header = _APPLIES[name]
    monkeypatch.setenv(env_var, "unit-test-secret")
    sys.path.insert(0, str(REPO / "runtime"))
    from dispatch import merge_auth
    cfg = json.loads((CFG / name).read_text())
    merged = merge_auth(dict(cfg))
    lower = {k.lower(): v for k, v in (merged.get("headers") or {}).items()}
    assert header in lower, f"{name}: merge_auth did not apply {header}"
    assert "unit-test-secret" in lower[header]


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_preset_auth_block_is_valid_and_env_only(name):
    cfg = json.loads((CFG / name).read_text())
    auth = cfg.get("auth")
    if not auth:
        return
    assert auth["type"] in ("static", "oauth2")
    # every secret is an env: reference — never a literal in a shipped file
    blob = json.dumps(auth)
    for marker in ("value", "client_id_ref", "client_secret_ref"):
        if marker in auth and isinstance(auth[marker], str) and auth[marker]:
            assert auth[marker].startswith("env:"), f"{name}:{marker} must be an env: ref"
