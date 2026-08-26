"""
bedrock — AWS Bedrock adapter. Three modes, one clean answer:

  converse   bedrock-runtime.Converse           (any Bedrock foundation model)
  agent      bedrock-agent-runtime.InvokeAgent  (classic Bedrock Agents; streaming)
  agentcore  bedrock-agentcore.InvokeAgentRuntime (Bedrock AgentCore runtimes; streaming)

boto3 does the two things the HTTP adapters structurally cannot: **SigV4 request signing**
and **application/vnd.amazon.eventstream decoding**. Credentials come from the standard AWS
chain (env AWS_* / ~/.aws / profile / role) — nothing secret lives in the config.

Primary reason to exist: a **private / VPC-only AgentCore runtime**, where the bridge is the
only path in. The Straiker Console already assesses cloud-reachable Bedrock/Vertex natively.

Config:
  mode           converse | agent | agentcore        (default converse)
  region         AWS region (else AWS_REGION / profile default)
  profile        optional AWS profile name
  # converse:
  model_id       e.g. anthropic.claude-3-5-sonnet-20241022-v2:0
  system         optional system prompt
  max_tokens     default 1024
  # agent:
  agent_id, agent_alias_id
  # agentcore:
  runtime_arn    agentRuntimeArn
  qualifier      optional endpoint qualifier (default "DEFAULT")
  input_key      key the prompt is sent under in the payload (default "prompt")
  response_path  dot-path to the answer in a JSON agentcore response (best-effort otherwise)
  # stateful (agent/agentcore): a session id is threaded automatically per conversation.
  session_id     override to pin a specific session
  timeout_ms     default 60000
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from .base import BotAdapter

logger = logging.getLogger(__name__)


def _dot(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        cur = cur[int(part)] if (isinstance(cur, list) and part.isdigit()) else (
            cur.get(part) if isinstance(cur, dict) else None)
    return cur


class BedrockAdapter(BotAdapter):
    """AWS Bedrock — Converse / Agent / AgentCore. Stateful for agent & agentcore."""

    def __init__(self):
        self._session_id: Optional[str] = None

    # -- client ---------------------------------------------------------------
    def _client(self, service: str, config: Dict[str, Any]):
        import boto3  # lazy: only needed for AWS targets
        session = boto3.Session(profile_name=config.get("profile"),
                                region_name=config.get("region"))
        return session.client(service)

    def _sid(self, config: Dict[str, Any]) -> str:
        if config.get("session_id"):
            return str(config["session_id"])
        if not self._session_id:
            self._session_id = f"abv2-{uuid.uuid4().hex}"
        return self._session_id

    # -- entrypoint -----------------------------------------------------------
    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()
        mode = config.get("mode", "converse")
        handler = {"converse": self._converse, "agent": self._invoke_agent,
                   "agentcore": self._invoke_agentcore}.get(mode)
        if handler is None:
            return self._fail(f"unknown bedrock mode {mode!r} (converse|agent|agentcore)", start)
        try:
            text = await asyncio.to_thread(handler, prompt, config)
        except Exception as e:  # botocore ClientError, NoCredentials, etc. — never leak a traceback
            return self._fail(f"bedrock {mode} error: {type(e).__name__}: {e}", start)
        if not text:
            return self._fail(f"bedrock {mode} returned no text", start)
        return self._ok(text, start, mode=mode, session_id=self._session_id)

    # -- converse -------------------------------------------------------------
    def _converse(self, prompt: str, config: Dict[str, Any]) -> str:
        model_id = config.get("model_id") or config.get("modelId")
        if not model_id:
            raise ValueError("converse mode requires model_id")
        client = self._client("bedrock-runtime", config)
        kw: Dict[str, Any] = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": int(config.get("max_tokens", 1024))},
        }
        if config.get("system"):
            kw["system"] = [{"text": config["system"]}]
        resp = client.converse(**kw)
        parts = (((resp.get("output") or {}).get("message") or {}).get("content") or [])
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()

    # -- classic bedrock agent ------------------------------------------------
    def _invoke_agent(self, prompt: str, config: Dict[str, Any]) -> str:
        agent_id = config.get("agent_id")
        alias = config.get("agent_alias_id")
        if not agent_id or not alias:
            raise ValueError("agent mode requires agent_id and agent_alias_id")
        client = self._client("bedrock-agent-runtime", config)
        resp = client.invoke_agent(agentId=agent_id, agentAliasId=alias,
                                   sessionId=self._sid(config), inputText=prompt)
        out = []
        for event in resp.get("completion", []):     # boto3 decodes the eventstream for us
            b = (event.get("chunk") or {}).get("bytes")
            if b:
                out.append(b.decode("utf-8", "replace"))
        return "".join(out).strip()

    # -- bedrock agentcore ----------------------------------------------------
    def _invoke_agentcore(self, prompt: str, config: Dict[str, Any]) -> str:
        arn = config.get("runtime_arn") or config.get("agent_runtime_arn")
        if not arn:
            raise ValueError("agentcore mode requires runtime_arn")
        client = self._client("bedrock-agentcore", config)
        input_key = config.get("input_key", "prompt")
        payload = {input_key: prompt, **(config.get("payload_extra") or {})}
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=self._sid(config),
            qualifier=config.get("qualifier", "DEFAULT"),
            payload=json.dumps(payload).encode("utf-8"),
        )
        raw = self._read_agentcore_body(resp)
        return self._extract_agentcore_text(raw, config).strip()

    @staticmethod
    def _read_agentcore_body(resp: Dict[str, Any]) -> str:
        """AgentCore returns either a StreamingBody or an EventStream under 'response'."""
        body = resp.get("response")
        if body is None:
            return ""
        if hasattr(body, "read"):                     # StreamingBody
            return body.read().decode("utf-8", "replace")
        chunks = []                                   # EventStream of {chunk:{bytes:...}}
        for event in body:
            if isinstance(event, (bytes, bytearray)):
                chunks.append(bytes(event).decode("utf-8", "replace"))
            elif isinstance(event, dict):
                b = (event.get("chunk") or {}).get("bytes")
                if b:
                    chunks.append(b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b))
        return "".join(chunks)

    @staticmethod
    def _extract_agentcore_text(raw: str, config: Dict[str, Any]) -> str:
        if not raw:
            return ""
        # SSE-framed agentcore payloads: pull the data: lines
        if "data:" in raw and "\n" in raw:
            data = "".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
            raw = data or raw
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return raw                                 # plain text answer
        rp = config.get("response_path")
        if rp:
            val = _dot(obj, rp)
            return val if isinstance(val, str) else json.dumps(val) if val is not None else ""
        # best-effort: common answer keys
        for k in ("output", "response", "result", "text", "answer", "content", "message"):
            v = obj.get(k) if isinstance(obj, dict) else None
            if isinstance(v, str) and v:
                return v
        return raw
