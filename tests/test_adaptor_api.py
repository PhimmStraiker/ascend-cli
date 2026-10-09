"""
control/api.py — the five custom-adaptor routes, fully offline.

Pins the paths, the request bodies (the engine's camelCase field names), and that a call which
RUNS an adaptor gets a timeout sized for its budget rather than the client's 60s default. All
HTTP is mocked through the shared Session seam; no sockets.
"""
import importlib

import pytest

from conftest import FakeResponse, install_fake_requests

api = importlib.import_module("api")


def _recording_client(monkeypatch, status=200, body=None, text=None):
    seen = []

    def handler(method, url, kwargs):
        if url.endswith("/auth/token"):
            return FakeResponse(200, {"access_token": "JWT-1"})
        seen.append((method, url, kwargs))
        if text is not None:
            return FakeResponse(status, text=text)
        return FakeResponse(status, body if body is not None else {"ok": True})

    install_fake_requests(monkeypatch, handler)
    return api.AscendAPI(token="s6r_pat_test"), seen


def test_spec_and_gate(monkeypatch):
    c, seen = _recording_client(monkeypatch)
    c.adapter_spec()
    c.adapter_gate("function sendTurn() {}")
    assert seen[0][0] == "GET" and seen[0][1].endswith("/api/v3/ascend/adapters/spec")
    assert seen[1][0] == "POST" and seen[1][1].endswith("/api/v3/ascend/adapters/gate")
    assert seen[1][2]["json"] == {"adapterSource": "function sendTurn() {}"}


def test_test_sends_the_engines_body_and_a_budget_sized_timeout(monkeypatch):
    c, seen = _recording_client(monkeypatch)
    c.adapter_test("src", "3f2a9c1e-7b4d-4e8f-9a0b-1c2d3e4f5a6b", ("a", "b"), 90)
    method, url, kw = seen[0]
    assert method == "POST" and url.endswith("/api/v3/ascend/adapters/test")
    assert kw["json"] == {"adapterSource": "src", "appId": "3f2a9c1e-7b4d-4e8f-9a0b-1c2d3e4f5a6b",
                          "prompts": ["a", "b"], "appConfig": {}, "runBudgetSeconds": 90}
    assert kw["timeout"] == 150, "budget + 60s headroom, not the client default"


def test_get_and_verify(monkeypatch):
    c, seen = _recording_client(monkeypatch)
    c.get_app_adapter("3f2a9c1e-7b4d-4e8f-9a0b-1c2d3e4f5a6b")
    c.verify_app_adapter("3f2a9c1e-7b4d-4e8f-9a0b-1c2d3e4f5a6b", 240)
    assert seen[0][0] == "GET"
    assert seen[0][1].endswith("/ascend/applications/3f2a9c1e-7b4d-4e8f-9a0b-1c2d3e4f5a6b/adapter")
    assert seen[1][0] == "POST"
    assert seen[1][1].endswith("/ascend/applications/3f2a9c1e-7b4d-4e8f-9a0b-1c2d3e4f5a6b/adapter/verify")
    assert seen[1][2]["json"] == {"runBudgetSeconds": 240}
    assert seen[1][2]["timeout"] == 300


def test_ordinary_calls_keep_the_client_timeout(monkeypatch):
    c, seen = _recording_client(monkeypatch)
    c.adapter_spec()
    assert seen[0][2]["timeout"] == c.timeout


@pytest.mark.parametrize("budget,expected", [(120, 180.0), (1, 61.0), ("x", 300.0), (None, 300.0)])
def test_run_timeout_headroom(budget, expected):
    assert api._adapter_run_timeout(budget) == expected


def test_the_engines_404_surfaces_with_its_text(monkeypatch):
    """The CLI explains this one by shape, so the text must survive into the exception."""
    c, _ = _recording_client(monkeypatch, status=404,
                             text='{"detail":"application aapp_x could not be read"}')
    with pytest.raises(api.AscendAPIError) as ei:
        c.get_app_adapter("aapp_x")
    assert "-> 404" in str(ei.value) and "could not be read" in str(ei.value)
