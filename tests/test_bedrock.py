"""
test_bedrock — the AWS Bedrock adapter's three modes (converse / agent / agentcore) parse the
real boto3 response shapes into one clean answer. boto3 is mocked (no AWS, no creds); this pins
the extraction + error handling. SigV4 and eventstream decoding are boto3's job by construction.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from adapters.bedrock import BedrockAdapter  # noqa: E402
from conftest import run_async as run  # shared throwaway-loop runner (don't pollute the global loop)


class FakeConverseClient:
    def converse(self, **kw):
        assert kw["modelId"]
        assert kw["messages"][0]["content"][0]["text"]
        return {"output": {"message": {"content": [{"text": "Converse "}, {"text": "answer"}]}}}


class FakeAgentClient:
    def invoke_agent(self, **kw):
        assert kw["agentId"] and kw["agentAliasId"] and kw["sessionId"] and kw["inputText"]
        return {"completion": [{"chunk": {"bytes": b"Agent "}},
                               {"chunk": {"bytes": b"answer"}}]}


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


class FakeAgentCoreClient:
    def __init__(self, body): self._body = body
    def invoke_agent_runtime(self, **kw):
        assert kw["agentRuntimeArn"] and kw["runtimeSessionId"] and kw["payload"]
        return {"response": _Body(self._body)}


def _patch(ad, client):
    ad._client = lambda service, config: client


def test_converse_mode():
    ad = BedrockAdapter(); _patch(ad, FakeConverseClient())
    r = run(ad.send_prompt("hi", {"mode": "converse", "model_id": "anthropic.claude-x"}))
    assert r["success"] and r["response"] == "Converse answer"


def test_converse_requires_model_id():
    ad = BedrockAdapter(); _patch(ad, FakeConverseClient())
    r = run(ad.send_prompt("hi", {"mode": "converse"}))
    assert r["success"] is False and "model_id" in r["error"]


def test_agent_mode_assembles_chunks():
    ad = BedrockAdapter(); _patch(ad, FakeAgentClient())
    r = run(ad.send_prompt("hi", {"mode": "agent", "agent_id": "A", "agent_alias_id": "B"}))
    assert r["success"] and r["response"] == "Agent answer"
    assert r["metadata"]["session_id"]           # a session id was threaded


def test_agentcore_json_body_with_response_path():
    ad = BedrockAdapter(); _patch(ad, FakeAgentCoreClient(b'{"output":"AgentCore answer"}'))
    r = run(ad.send_prompt("hi", {"mode": "agentcore", "runtime_arn": "arn:x",
                                  "response_path": "output"}))
    assert r["success"] and r["response"] == "AgentCore answer"


def test_agentcore_plain_text_body():
    ad = BedrockAdapter(); _patch(ad, FakeAgentCoreClient(b"just text"))
    r = run(ad.send_prompt("hi", {"mode": "agentcore", "runtime_arn": "arn:x"}))
    assert r["success"] and r["response"] == "just text"


def test_agentcore_sse_framed_body():
    ad = BedrockAdapter()
    _patch(ad, FakeAgentCoreClient(b'data: {"output":"streamed"}\n\n'))
    r = run(ad.send_prompt("hi", {"mode": "agentcore", "runtime_arn": "arn:x",
                                  "response_path": "output"}))
    assert r["success"] and r["response"] == "streamed"


def test_unknown_mode_fails_cleanly():
    ad = BedrockAdapter()
    r = run(ad.send_prompt("hi", {"mode": "nope"}))
    assert r["success"] is False and "unknown bedrock mode" in r["error"]


def test_boto_exception_becomes_error_not_traceback():
    class Boom:
        def converse(self, **kw): raise RuntimeError("AccessDenied")
    ad = BedrockAdapter(); _patch(ad, Boom())
    r = run(ad.send_prompt("hi", {"mode": "converse", "model_id": "m"}))
    assert r["success"] is False and "AccessDenied" in r["error"]
