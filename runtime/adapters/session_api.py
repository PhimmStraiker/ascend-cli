"""
Session API adapter — for chatbots that require session/thread creation before messaging.

Two-step flow:
1. POST to session endpoint → extract session ID from response
2. POST to message endpoint (with session ID injected) → extract bot response

Works for: Salesforce Agentforce Agent API, Amazon Bedrock agents, Azure AI Agent Service.

Config keys:
  session_endpoint  - URL to create a new session
  session_body      - Request body for session creation ({{UUID}} is auto-replaced)
  session_extract   - Dot-path to extract session ID from session response (default: "sessionId")
  session_variable  - Variable name injected into message endpoint/body (default: "SESSION_ID")
  message_endpoint  - URL to send messages ({{SESSION_ID}} is replaced with extracted value)
  message_body      - Request body template with {{PROMPT}} and {{SESSION_ID}} placeholders
  response_path     - Dot-path to extract response text (default: "messages.0.message")
  headers           - Dict of headers shared across both calls
  timeout_ms        - Request timeout in milliseconds (default: 60000)
"""

import json
import time
import uuid
import logging
from typing import Any, Dict, Optional

import requests

from . import auth_lifecycle
from .base import BotAdapter, resolve_timeout_s
from .websocket_direct import _json_escape

logger = logging.getLogger(__name__)


class SessionAPIAdapter(BotAdapter):
    """Create a session, then send a prompt through it."""

    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()

        session_endpoint = config.get("session_endpoint")
        message_endpoint = config.get("message_endpoint")
        if not session_endpoint or not message_endpoint:
            return self._fail("session_endpoint and message_endpoint are required", start)

        timeout = resolve_timeout_s(config)

        base_headers = {"Content-Type": "application/json"}
        base_headers.update(config.get("headers", {}))
        base_endpoint = session_endpoint

        def _authed():
            """(endpoint, headers, manager) with a live credential substituted in.

            Rebuilt from the ORIGINAL config each attempt: once {{TOKEN}} has been substituted the
            placeholder is gone, so re-applying to already-rendered headers would be a no-op.
            """
            return auth_lifecycle.apply_auth(config, base_endpoint, base_headers, None)

        # --- Step 1: Create session ---
        session_body = config.get("session_body", {})
        session_body_str = json.dumps(session_body)
        session_body_str = session_body_str.replace("{{UUID}}", str(uuid.uuid4()))
        session_body = json.loads(session_body_str)

        # A session target's credential expires mid-run exactly like any other, and a stale one
        # turns every later probe into a 401 that scores as a target refusal. No-op without `auth`.
        session_data = None
        for attempt in range(2):
            try:
                session_endpoint, headers, _, mgr = _authed()
            except auth_lifecycle.AuthError as e:
                return self._fail(f"auth lifecycle: {e}", start, status_code=401)
            try:
                logger.info(f"SessionAPI: creating session at {session_endpoint}")
                resp = requests.post(
                    session_endpoint, json=session_body, headers=headers, timeout=timeout
                )
            except requests.RequestException as e:
                return self._fail(f"Session creation failed: {e}", start)
            if (resp.status_code in (401, 403) and mgr is not None
                    and mgr.retries_on_401() and attempt == 0):
                logger.info("SessionAPI: HTTP %s creating a session — re-minting the credential "
                            "and retrying once", resp.status_code)
                mgr.invalidate()
                continue
            try:
                resp.raise_for_status()
                session_data = resp.json()
            except requests.RequestException as e:
                return self._fail(f"Session creation failed: {e}", start)
            except json.JSONDecodeError:
                return self._fail("Session response is not JSON", start)
            break
        if session_data is None:
            return self._fail("Session creation failed after re-authenticating", start,
                              status_code=401)

        extract_path = config.get("session_extract", "sessionId")
        session_value = _extract(session_data, extract_path)
        if not session_value:
            return self._fail(
                f"Could not extract '{extract_path}' from session response",
                start,
                raw=json.dumps(session_data)[:500],
            )

        variable_name = config.get("session_variable", "SESSION_ID")
        logger.debug("SessionAPI: extracted session id (elided)")

        # --- Step 1b (optional): warm-up / greeting discard ---
        # Some agents return a mandatory greeting/consent on the FIRST message; send a
        # throwaway first so the probe gets the real answer, not a false PASS on the greeting.
        resolved_endpoint = message_endpoint.replace(f"{{{{{variable_name}}}}}", str(session_value))
        warmup_message = config.get("warmup_message")
        if warmup_message:
            wb = json.dumps(config.get("message_body", {}))
            wb = wb.replace("{{PROMPT}}", _json_escape(str(warmup_message)))
            wb = wb.replace(f"{{{{{variable_name}}}}}", _json_escape(str(session_value)))
            try:
                requests.post(resolved_endpoint, json=json.loads(wb), headers=headers, timeout=timeout)
            except Exception as e:
                logger.debug("SessionAPI: warmup send failed (non-fatal): %s", e)

        # --- Step 2: Send message ---

        message_body = config.get("message_body", {})
        message_body_str = json.dumps(message_body)
        message_body_str = message_body_str.replace("{{PROMPT}}", _json_escape(prompt))
        message_body_str = message_body_str.replace(f"{{{{{variable_name}}}}}", _json_escape(str(session_value)))
        try:
            message_body = json.loads(message_body_str)
        except ValueError as e:
            return self._fail(f"message template render failed: {e}", start)

        try:
            logger.info(f"SessionAPI: sending message to {resolved_endpoint}")
            resp = requests.post(
                resolved_endpoint, json=message_body, headers=headers, timeout=timeout
            )
            resp.raise_for_status()
            message_data = resp.json()
        except requests.RequestException as e:
            return self._fail(f"Message send failed: {e}", start)
        except json.JSONDecodeError:
            return self._fail("Message response is not JSON", start)

        response_path = config.get("response_path", "messages.0.message")
        response_text = _extract(message_data, response_path)

        if response_text is None:
            return self._fail(
                f"Could not extract response at path '{response_path}'",
                start,
                raw=json.dumps(message_data)[:500],
            )

        return self._ok(
            str(response_text).strip(),
            start,
            adapter="session_api",
            session_id=str(session_value),
        )


def _extract(data: Any, path: str) -> Any:
    """Extract a value from nested dict/list using dot notation."""
    parts = path.split(".")
    current = data
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current
