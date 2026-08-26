"""
SCRT2 Direct adapter — Salesforce Agentforce (SCRT2) REST API + Server-Sent Events.

No browser required. Implements the Salesforce Embedded Messaging (SCRT2) API chain:
  1. POST /iamessage/v1/authorization/unauthenticated/accessToken  → JWT
  2. POST /iamessage/v1/conversation                               → conversationId
  3. POST /iamessage/v1/conversation/{id}/message                  → send prompt
  4. GET  /eventrouter/v1/sse                                      → read bot response

Speed: ~4–6s per prompt (vs 15–30s for browser adapter) — recommended for Ascend automated runs.
Creates a fresh conversation per prompt — stateless and parallelizable.

When to use:
  - Target uses Salesforce Agentforce (SCRT2 Embedded Messaging)
  - HAR confirms unauthenticated JWT endpoint + SSE response stream
  - Speed is a priority for the Ascend automated run

Warm-up pattern:
  SCRT2 bots always send a consent notice + bot greeting as the FIRST message in
  every new conversation, regardless of what the user said. Without a warm-up,
  Ascend probes receive this boilerplate instead of a real contextual response.

  Set "warmup_message": "Hello" in the customer config to enable the warm-up:
  the adapter sends the warm-up, discards the greeting, then sends the real probe
  and reads the actual response.

  Always enable warm-up for SCRT2 engagements.

Required config keys (from AscendProxy/configs/<customer>.json):
  scrt_base        - SCRT2 API base URL  (e.g. https://<org>.my.salesforce-scrt.com)
  org_id           - Salesforce Org ID   (e.g. 00D...)
  developer_name   - Agentforce deployment developer name
  widget_origin    - Widget iframe origin (used as CORS Origin for API calls)
  url              - Main site URL (used as Origin for the token request)

Optional config keys:
  capabilities_ver - Capabilities version string (default "258")
  sse_timeout      - SSE response timeout in seconds (default 45)
  warmup_message   - Warm-up text to discard the greeting (recommended: "Hello")
"""

import json
import logging
import time
import uuid
import urllib.request
import urllib.error
from typing import Any, Dict

from .base import BotAdapter

logger = logging.getLogger(__name__)


class SCRT2DirectAdapter(BotAdapter):
    """Salesforce Agentforce SCRT2 REST + SSE adapter — no browser required."""

    def _base_headers(self, origin: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": origin,
        }

    def _get_token(self, scrt_base, org_id, developer_name, capabilities_ver, origin) -> str:
        url = f"{scrt_base}/iamessage/v1/authorization/unauthenticated/accessToken"
        payload = json.dumps({
            "orgId": org_id,
            "developerName": developer_name,
            "capabilitiesVersion": capabilities_ver,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers=self._base_headers(origin), method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["accessToken"]

    def _create_conversation(self, scrt_base, jwt, widget_origin) -> str:
        conv_id = str(uuid.uuid4())
        url = f"{scrt_base}/iamessage/v1/conversation"
        payload = json.dumps({
            "conversationId": conv_id,
            "language": "en_US",
            "routingAttributes": {},
        }).encode()
        headers = {**self._base_headers(widget_origin), "Authorization": f"Bearer {jwt}"}
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return conv_id

    def _send_message(self, scrt_base, jwt, conv_id, text, widget_origin) -> None:
        url = f"{scrt_base}/iamessage/v1/conversation/{conv_id}/message"
        payload = json.dumps({
            "id": str(uuid.uuid4()),
            "messageType": "StaticContentMessage",
            "staticContent": {"formatType": "Text", "text": text},
        }).encode()
        headers = {**self._base_headers(widget_origin), "Authorization": f"Bearer {jwt}"}
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

    def _read_sse_response(self, scrt_base, jwt, org_id, widget_origin, timeout=45, skip_count=0) -> str:
        """Read the SSE stream and return the (skip_count+1)th bot message.

        SCRT2 replays all conversation events when a new SSE connection is opened.
        skip_count=1 skips the replayed warm-up greeting and returns the actual
        probe response (second bot message in the stream).
        """
        url = f"{scrt_base}/eventrouter/v1/sse"
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {jwt}",
            "x-org-id": org_id,
            "Origin": widget_origin,
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        deadline = time.time() + timeout
        bot_messages_seen = 0

        try:
            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                event_type = None
                data_lines = []

                for raw_line in resp:
                    if time.time() > deadline:
                        break

                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        data_lines = []
                    elif line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str.isdigit():
                            continue  # heartbeat ping
                        data_lines.append(data_str)
                    elif line == "":
                        if event_type == "CONVERSATION_MESSAGE" and data_lines:
                            try:
                                event = json.loads("".join(data_lines))
                                entry = event.get("conversationEntry", {})
                                role = entry.get("sender", {}).get("role", "")
                                if role in ("EndUser", "Guest"):
                                    event_type = None
                                    data_lines = []
                                    continue

                                payload_str = entry.get("entryPayload", "")
                                if payload_str:
                                    payload = json.loads(payload_str)
                                    text = (
                                        payload
                                        .get("abstractMessage", {})
                                        .get("staticContent", {})
                                        .get("text", "")
                                    )
                                    if text:
                                        if bot_messages_seen < skip_count:
                                            bot_messages_seen += 1
                                            logger.info(
                                                f"SCRT2Direct: skipping warm-up response "
                                                f"({bot_messages_seen}/{skip_count}): {text[:60]}..."
                                            )
                                            event_type = None
                                            data_lines = []
                                            continue
                                        return text
                            except Exception as e:
                                logger.warning(f"SCRT2Direct SSE parse error: {e}")
                        event_type = None
                        data_lines = []

        except Exception as e:
            logger.warning(f"SCRT2Direct SSE stream error: {e}")

        return ""

    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()

        scrt_base = config.get("scrt_base", "")
        org_id = config.get("org_id", "")
        developer_name = config.get("developer_name", "")
        widget_origin = config.get("widget_origin", "")
        main_origin = config.get("url", widget_origin)
        capabilities_ver = config.get("capabilities_ver", "258")
        sse_timeout = config.get("sse_timeout", 45)
        warmup_message = config.get("warmup_message", "")

        if not all([scrt_base, org_id, developer_name, widget_origin]):
            return self._fail(
                "Missing required config: scrt_base, org_id, developer_name, widget_origin",
                start,
            )

        try:
            logger.info("SCRT2Direct: acquiring token")
            jwt = self._get_token(scrt_base, org_id, developer_name, capabilities_ver, main_origin)

            logger.info("SCRT2Direct: creating conversation")
            conv_id = self._create_conversation(scrt_base, jwt, widget_origin)

            if warmup_message:
                # Send warm-up to consume the mandatory consent+greeting response.
                # SCRT2 always opens with this boilerplate — warm-up discards it so
                # Ascend probes receive the bot's actual contextual response.
                logger.info(f"SCRT2Direct: sending warm-up ('{warmup_message}')")
                self._send_message(scrt_base, jwt, conv_id, warmup_message, widget_origin)

                logger.info("SCRT2Direct: reading and discarding greeting response")
                self._read_sse_response(scrt_base, jwt, org_id, widget_origin, sse_timeout, skip_count=0)

                logger.info(f"SCRT2Direct: sending probe ({len(prompt)} chars)")
                self._send_message(scrt_base, jwt, conv_id, prompt, widget_origin)

                # skip_count=1, NOT 0. Each read opens a NEW SSE connection and SCRT2 replays the
                # whole conversation on connect, so the first bot message in this stream is the
                # greeting we just discarded — the second is the actual answer to the probe.
                #
                # With 0 here every probe was answered with the bot's boilerplate greeting. That
                # is benign text, so every adversarial probe scored as a refusal and the whole
                # assessment came back clean having never seen a real response: a systemic FALSE
                # PASS across an entire adapter family, with nothing in the output to show it.
                logger.info("SCRT2Direct: reading probe response (skipping the replayed greeting)")
                response = self._read_sse_response(scrt_base, jwt, org_id, widget_origin,
                                                   sse_timeout, skip_count=1)
            else:
                logger.info(f"SCRT2Direct: sending message ({len(prompt)} chars)")
                self._send_message(scrt_base, jwt, conv_id, prompt, widget_origin)

                logger.info("SCRT2Direct: reading SSE response")
                response = self._read_sse_response(scrt_base, jwt, org_id, widget_origin, sse_timeout)

            if not response:
                return self._fail("No response received from SSE stream", start, adapter="scrt2_direct")

            logger.info(f"SCRT2Direct: got response ({len(response)} chars)")
            return self._ok(response, start, adapter="scrt2_direct", conv_id=conv_id)

        except Exception as e:
            logger.error(f"SCRT2Direct adapter error: {e}", exc_info=True)
            return self._fail(str(e), start, adapter="scrt2_direct")
