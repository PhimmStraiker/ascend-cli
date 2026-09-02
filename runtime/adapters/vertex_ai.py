"""Vertex AI Agent Engine adapter — drives a deployed ADK agent via :streamQuery.

Auth uses Application Default Credentials (gcloud ADC) by default, minting a
short-lived token per request — no key needed, which sidesteps the
`iam.disableServiceAccountKeyCreation` org policy. On a host WITHOUT ambient ADC,
set `sa_key_file` to a service-account JSON key and it authenticates from that.

ADK agents on Agent Engine expose `stream_query` (not `query`). The endpoint
returns a streamed sequence of event objects; the assistant text is the
concatenation of `content.parts[*].text` across chunks.

Config keys:
  endpoint     - full :streamQuery URL (…/reasoningEngines/{ID}:streamQuery)
  user_id      - user_id sent to the agent (default "ascend-probe")
  sa_key_file  - optional path to a service-account JSON key (else ADC)
  timeout_ms   - request timeout in ms (optional; otherwise derived from the platform's per-probe window)
"""

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

from .base import BotAdapter, resolve_timeout_s
# google.auth is imported lazily in _token() so this adapter package still imports
# on machines without google-auth installed (it's only needed for vertex_ai).

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class VertexAIAdapter(BotAdapter):
    """Send a prompt to a Vertex AI Agent Engine ADK agent via :streamQuery."""

    def __init__(self) -> None:
        self._creds = None

    def _token(self, config: Optional[Dict[str, Any]] = None) -> str:
        import os
        import google.auth
        import google.auth.transport.requests
        if self._creds is None:
            key_file = (config or {}).get("sa_key_file") or (config or {}).get("service_account_key")
            if key_file:
                # Host without ambient ADC: authenticate from a service-account key file.
                from google.oauth2 import service_account
                self._creds = service_account.Credentials.from_service_account_file(
                    os.path.expanduser(key_file), scopes=_SCOPES)
            else:
                self._creds, _ = google.auth.default(scopes=_SCOPES)
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds.token

    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()
        endpoint = config.get("endpoint")
        if not endpoint:
            return self._fail("No endpoint configured", start)

        user_id = config.get("user_id", "ascend-probe")
        timeout = resolve_timeout_s(config)
        body = {
            "class_method": "stream_query",
            "input": {"message": prompt, "user_id": user_id},
        }

        try:
            token = self._token(config)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"ADC token error: {e}", start)

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            logger.info(f"VertexAI: POST {endpoint}")
            resp = requests.post(endpoint, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            return self._fail(
                f"HTTP error: {e}", start,
                status_code=getattr(getattr(e, "response", None), "status_code", None),
                raw=getattr(getattr(e, "response", None), "text", "")[:500],
            )

        text = _extract_text(resp.text)
        if not text:
            return self._fail("No text in agent response", start, raw=resp.text[:500])
        return self._ok(text, start, adapter="vertex_ai")


def _iter_objects(raw: str):
    """Yield JSON objects from a :streamQuery body (JSON array, NDJSON, or SSE)."""
    raw = raw.strip()
    if not raw:
        return
    # Whole-body JSON (array or single object)
    try:
        parsed = json.loads(raw)
        for o in (parsed if isinstance(parsed, list) else [parsed]):
            yield o
        return
    except json.JSONDecodeError:
        pass
    # NDJSON / SSE: one object per line, optionally "data: " prefixed
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _extract_text(raw: str) -> str:
    chunks = []
    for o in _iter_objects(raw):
        if not isinstance(o, dict):
            continue
        content = o.get("content") or {}
        for part in content.get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                chunks.append(part["text"])
    return "".join(chunks).strip()
