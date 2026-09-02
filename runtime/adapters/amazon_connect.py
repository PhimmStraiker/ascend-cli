"""
Amazon Connect adapter — for chatbots running on AWS Connect Chat.

Multi-step flow:
1. GET  token endpoint         → JWT security token
2. POST start endpoint         → ParticipantToken, ContactId
3. POST /participant/connection → ConnectionToken (+ WebSocket URL)
4. POST /participant/message   → send user prompt
5. POST /participant/transcript → poll for bot response

Works for: Any Amazon Connect Chat widget deployment.

Config keys:
  token_endpoint    - CloudFront /token URL (returns JWT)
  start_endpoint    - Connect widget start endpoint
  participant_base  - Participant Service base URL (default: us-east-1)
  display_name      - Name shown in chat (default: "Test User")
  attributes        - Contact attributes dict passed to Connect
  snippet_id        - Optional x-amz-snippet-id header value
  reuse_session     - Reuse existing session across prompts (default: false)
  greeting_wait_ms  - Time to wait for bot greeting after session create (default: 3000)
  timeout_ms        - HTTP request timeout (optional; otherwise derived from the platform's per-probe window)
  poll_interval_ms  - Transcript polling interval (default: 1500)
  poll_timeout_ms   - Max time to wait for bot response (default: 30000)

NOTE: Amazon Connect bot responses arrive via WebSocket. This adapter uses REST transcript
polling as a fallback. For real-time response capture, use browser_proxy.py with CDP instead.
"""

import json
import time
import uuid
import logging
from typing import Any, Dict, List, Optional

import requests

from .base import BotAdapter, resolve_timeout_s

logger = logging.getLogger(__name__)


class AmazonConnectAdapter(BotAdapter):
    """Interact with an Amazon Connect Chat widget via its REST API."""

    def __init__(self):
        self._session_data = None

    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()

        token_endpoint = config.get("token_endpoint")
        start_endpoint = config.get("start_endpoint")
        participant_base = config.get(
            "participant_base",
            "https://participant.connect.us-east-1.amazonaws.com"
        )

        if not token_endpoint or not start_endpoint:
            return self._fail("token_endpoint and start_endpoint are required", start)

        timeout = resolve_timeout_s(config)
        poll_interval = config.get("poll_interval_ms", 1500) / 1000
        poll_timeout = config.get("poll_timeout_ms", 30000) / 1000
        reuse_session = config.get("reuse_session", False)

        try:
            if reuse_session and self._session_data:
                session = self._session_data
                logger.info("AmazonConnect: reusing existing session")
            else:
                session = self._create_session(
                    token_endpoint, start_endpoint, config, timeout
                )
                self._session_data = session
                logger.info(f"AmazonConnect: new session created "
                            f"(contact={session['contact_id'][:12]}...)")

                greeting_wait = config.get("greeting_wait_ms", 3000) / 1000
                time.sleep(greeting_wait)

            msg_url = f"{participant_base}/participant/message"
            msg_resp = requests.post(
                msg_url,
                json={
                    "ClientToken": str(uuid.uuid4()),
                    "Content": prompt,
                    "ContentType": "text/plain",
                },
                headers={
                    "Content-Type": "application/json",
                    "x-amz-bearer": session["connection_token"],
                },
                timeout=timeout,
            )
            msg_resp.raise_for_status()
            msg_data = msg_resp.json()
            msg_time = msg_data.get("AbsoluteTime", "")
            logger.info(f"AmazonConnect: message sent (id={msg_data.get('Id', '')[:12]})")

            response_text = self._poll_for_response(
                participant_base, session, msg_time, poll_interval, poll_timeout, timeout
            )

            if not response_text:
                return self._fail(
                    "Timed out waiting for bot response",
                    start,
                    adapter="amazon_connect",
                    contact_id=session["contact_id"]
                )

            logger.info(f"AmazonConnect: got response ({len(response_text)} chars)")
            return self._ok(
                response_text.strip(), start,
                adapter="amazon_connect",
                contact_id=session["contact_id"]
            )

        except requests.RequestException as e:
            logger.error(f"AmazonConnect HTTP error: {e}", exc_info=True)
            self._session_data = None
            return self._fail(f"HTTP error: {e}", start, adapter="amazon_connect")
        except Exception as e:
            logger.error(f"AmazonConnect error: {e}", exc_info=True)
            self._session_data = None
            return self._fail(str(e), start, adapter="amazon_connect")

    def _create_session(
        self, token_endpoint: str, start_endpoint: str,
        config: Dict[str, Any], timeout: float
    ) -> Dict[str, str]:
        """Steps 1-3: Token → Start → Connection."""

        participant_base = config.get(
            "participant_base",
            "https://participant.connect.us-east-1.amazonaws.com"
        )

        # Step 1: Get JWT token
        logger.info(f"AmazonConnect: getting token from {token_endpoint}")
        token_resp = requests.get(token_endpoint, timeout=timeout)
        token_resp.raise_for_status()
        jwt_token = token_resp.json().get("data")
        if not jwt_token:
            raise ValueError("No JWT token in response")

        # Step 2: Start chat
        display_name = config.get("display_name", "Test User")
        attributes = config.get("attributes", {
            "name": display_name,
        })

        start_body = {
            "ParticipantDetails": {
                "DisplayName": display_name,
            },
            "Attributes": attributes,
            "SupportedMessagingContentTypes": [
                "text/plain",
                "text/markdown",
                "application/json",
                "application/vnd.amazonaws.connect.message.interactive",
                "application/vnd.amazonaws.connect.message.interactive.response",
            ],
        }

        start_headers = {
            "Content-Type": "application/json",
            "x-amz-security-token": jwt_token,
            "x-amz-active-chat": "false",
        }
        snippet_id = config.get("snippet_id")
        if snippet_id:
            start_headers["x-amz-snippet-id"] = snippet_id

        logger.info(f"AmazonConnect: starting chat at {start_endpoint}")
        start_resp = requests.post(
            start_endpoint, json=start_body, headers=start_headers, timeout=timeout
        )
        start_resp.raise_for_status()
        start_data = start_resp.json()

        chat_result = start_data.get("data", {}).get("startChatResult", {})
        participant_token = chat_result.get("ParticipantToken")
        contact_id = chat_result.get("ContactId")
        participant_id = chat_result.get("ParticipantId")

        if not participant_token or not contact_id:
            raise ValueError(f"Missing ParticipantToken or ContactId: "
                             f"{json.dumps(start_data)[:500]}")

        # Step 3: Create connection
        conn_url = f"{participant_base}/participant/connection"
        conn_resp = requests.post(
            conn_url,
            json={"Type": ["WEBSOCKET", "CONNECTION_CREDENTIALS"]},
            headers={
                "Content-Type": "application/json",
                "x-amz-bearer": participant_token,
            },
            timeout=timeout,
        )
        conn_resp.raise_for_status()
        conn_data = conn_resp.json()

        connection_token = conn_data.get("ConnectionCredentials", {}).get("ConnectionToken")
        ws_url = conn_data.get("Websocket", {}).get("Url")

        if not connection_token:
            raise ValueError("No ConnectionToken in connection response")

        return {
            "contact_id": contact_id,
            "participant_id": participant_id,
            "participant_token": participant_token,
            "connection_token": connection_token,
            "ws_url": ws_url,
        }

    def _poll_for_response(
        self, participant_base: str, session: Dict[str, str],
        after_time: str, poll_interval: float, poll_timeout: float,
        http_timeout: float
    ) -> Optional[str]:
        """Poll transcript for AGENT/SYSTEM messages after our message."""
        transcript_url = f"{participant_base}/participant/transcript"
        deadline = time.time() + poll_timeout

        while time.time() < deadline:
            time.sleep(poll_interval)

            try:
                resp = requests.post(
                    transcript_url,
                    json={
                        "ContactId": session["contact_id"],
                        "MaxResults": 100,
                        "ScanDirection": "BACKWARD",
                        "SortOrder": "ASCENDING",
                        "StartPosition": {},
                    },
                    headers={
                        "Content-Type": "application/json",
                        "x-amz-bearer": session["connection_token"],
                    },
                    timeout=http_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"Transcript poll error: {e}")
                continue

            transcript = data.get("Transcript", [])
            if not transcript:
                continue

            agent_messages = self._extract_agent_responses(transcript, after_time)
            if agent_messages:
                combined = "\n".join(agent_messages)
                if combined.strip():
                    return combined

        return None

    def _extract_agent_responses(
        self, transcript: List[Dict], after_time: str
    ) -> List[str]:
        """Extract AGENT/SYSTEM text messages from transcript after a given time."""
        results = []
        found_our_message = False

        for item in transcript:
            item_time = item.get("AbsoluteTime", "")
            role = item.get("ParticipantRole", "")
            content = item.get("Content", "")
            content_type = item.get("ContentType", "")

            if content_type not in ("text/plain", "text/markdown"):
                continue

            if role == "CUSTOMER" and item_time >= after_time:
                found_our_message = True
                continue

            if found_our_message and role in ("AGENT", "SYSTEM"):
                if content.strip():
                    results.append(content.strip())

        return results
