"""
Slack Direct adapter — Slack Web API using a xoxp user OAuth token.

Sends prompts to a Slack bot DM and polls conversations.history for the response.
No browser required. Works against any Slack bot the authenticated user can DM.

Auth: Standard Bearer token (xoxp-...) — create via api.slack.com/apps,
      add User Token Scopes: chat:write, im:history, install to workspace.

Flow per prompt:
  1. POST /api/chat.postMessage  → send prompt to the bot's DM channel
  2. Poll GET /api/conversations.history?oldest=<sent_ts>  → wait for bot reply
  3. Extract text from Slack Block Kit blocks or plain text field
  4. Return response

Speed: ~5–30s per prompt depending on the bot's LLM response time.

Required config keys:
  slack_token     - User OAuth token (xoxp-...) from api.slack.com/apps
  channel_id - DM channel ID with the bot (D...)
  user_id         - Your Slack user ID (U...) to filter out self-messages

Optional config keys:
  target_bot_id     - Bot's bot_id (B...) for reliable filtering
  timeout_ms      - Max wait time for bot response in ms (default 90000)
  poll_interval_ms - How often to check for new messages in ms (default 2000)
  warmup_message  - Send this first and discard response (handles greeting flows)
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from .base import BotAdapter

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"


class SlackDirectAdapter(BotAdapter):
    """Slack Web API adapter — DM polling via xoxp user OAuth token."""

    def __init__(self):
        self._warmed_up = False

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _post_message(self, channel: str, text: str, token: str, timeout: float) -> str:
        """Post a message to a channel. Returns the message ts."""
        body = json.dumps({"channel": channel, "text": text}).encode()
        req = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage",
            data=body,
            headers=self._headers(token),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            raise RuntimeError(f"chat.postMessage failed: {data.get('error', 'unknown')}")
        return data["ts"]

    def _get_replies(self, channel: str, thread_ts: str, token: str, timeout: float) -> List[Dict]:
        """Fetch all replies in a thread. First message is the parent (our prompt); skip it."""
        params = urllib.parse.urlencode({"channel": channel, "ts": thread_ts})
        req = urllib.request.Request(
            f"{SLACK_API}/conversations.replies?{params}",
            headers=self._headers(token),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            raise RuntimeError(f"conversations.replies failed: {data.get('error', 'unknown')}")
        # First message is always our own prompt — skip it
        return data.get("messages", [])[1:]

    def _extract_text(self, msg: Dict) -> str:
        """Extract readable text from a Slack message (Block Kit or plain)."""
        blocks = msg.get("blocks", [])
        if blocks:
            parts = []
            for block in blocks:
                btype = block.get("type", "")
                if btype in ("section", "header", "context"):
                    t = block.get("text", {})
                    if t.get("text"):
                        parts.append(t["text"])
                elif btype == "rich_text":
                    for el in block.get("elements", []):
                        chunk = "".join(
                            sub.get("text", "")
                            for sub in el.get("elements", [])
                            if sub.get("type") == "text"
                        )
                        if chunk:
                            parts.append(chunk)
            if parts:
                return "\n".join(p for p in parts if p.strip())

        text = msg.get("text", "")
        if text:
            return text

        for att in msg.get("attachments", []):
            t = att.get("text") or att.get("fallback", "")
            if t:
                return t

        return ""

    def _is_bot_response(self, msg: Dict, user_id: str, target_bot_id: str) -> bool:
        if msg.get("user") == user_id:
            return False
        if target_bot_id and msg.get("bot_id") == target_bot_id:
            return True
        if msg.get("bot_id"):
            return True
        if msg.get("subtype") == "bot_message":
            return True
        return False

    def _poll_for_reply(
        self, channel: str, sent_ts: str, token: str,
        user_id: str, target_bot_id: str,
        poll_interval: float, http_timeout: float, deadline: float,
    ) -> str:
        # The bot replies in threads — poll conversations.replies on our sent message's thread.
        # Two-stage response pattern: the bot posts a loading/thinking message first, then
        # either edits it or appends the real answer. We use two defenses:
        # 1. loading_signals filter — skip known placeholder messages
        # 2. Stability check — require candidate to be unchanged across two consecutive
        #    polls before returning (handles thinking messages that don't match known signals)
        loading_signals = ["connecting to platforms", "might take a minute", "alert you of a new message"]
        last_candidate_ts = None
        last_candidate_text = None
        attempts = 0

        while time.time() < deadline:
            time.sleep(min(poll_interval, max(0, deadline - time.time())))
            if time.time() >= deadline:
                break
            attempts += 1
            replies = self._get_replies(channel, sent_ts, token, http_timeout)
            bot_msgs = [m for m in replies if self._is_bot_response(m, user_id, target_bot_id)]
            if not bot_msgs:
                last_candidate_ts = None
                last_candidate_text = None
                continue

            final_msgs = [
                m for m in bot_msgs
                if not any(sig in self._extract_text(m).lower() for sig in loading_signals)
            ]
            target = final_msgs[-1] if final_msgs else None
            if not target:
                last_candidate_ts = None
                last_candidate_text = None
                continue

            text = self._extract_text(target)
            if not text.strip():
                continue

            # Stability check: only return if the same message ts and content appeared
            # on the previous poll too — ensures thinking/editing messages have settled.
            if target["ts"] == last_candidate_ts and text == last_candidate_text:
                logger.info(f"SlackDirect: stable reply after {attempts} polls ({len(text)} chars)")
                return text

            last_candidate_ts = target["ts"]
            last_candidate_text = text

        # Deadline reached — return the last seen candidate rather than timing out.
        # If the bot responded in the final poll window and stability couldn't be confirmed,
        # this still captures the response. Loading-signal filter already excluded placeholders.
        if last_candidate_text:
            logger.info(f"SlackDirect: returning deadline candidate ({len(last_candidate_text)} chars)")
            return last_candidate_text
        return ""

    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()

        token = config.get("slack_token", "")
        channel = config.get("channel_id", "")
        user_id = config.get("user_id", "")
        target_bot_id = config.get("target_bot_id", "")
        timeout_ms = config.get("timeout_ms", 90000)
        poll_interval = config.get("poll_interval_ms", 2000) / 1000
        warmup_message = config.get("warmup_message", "")
        http_timeout = min(timeout_ms / 1000, 30)

        if not all([token, channel, user_id]):
            return self._fail("Missing required config: slack_token, channel_id, user_id", start)

        try:
            if warmup_message and not self._warmed_up:
                logger.info("SlackDirect: sending warmup")
                try:
                    wts = self._post_message(channel, warmup_message, token, http_timeout)
                    self._poll_for_reply(channel, wts, token, user_id, target_bot_id,
                                         poll_interval, http_timeout, time.time() + 30)
                    time.sleep(1.0)
                except Exception as e:
                    logger.warning(f"SlackDirect: warmup failed (non-fatal): {e}")
                finally:
                    self._warmed_up = True

            logger.info(f"SlackDirect: posting prompt ({len(prompt)} chars) to {channel}")
            sent_ts = self._post_message(channel, prompt, token, http_timeout)
            logger.info(f"SlackDirect: sent ts={sent_ts}")

            text = self._poll_for_reply(
                channel, sent_ts, token, user_id, target_bot_id,
                poll_interval, http_timeout, time.time() + timeout_ms / 1000,
            )

            if not text:
                return self._fail(
                    f"Timeout ({timeout_ms}ms): no response from bot",
                    start, adapter="slack_direct", channel=channel,
                )
            return self._ok(text, start, adapter="slack_direct", channel=channel)

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            logger.error(f"SlackDirect HTTP {e.code}: {body}")
            return self._fail(f"HTTP {e.code}: {body}", start, adapter="slack_direct")
        except Exception as e:
            logger.error(f"SlackDirect error: {e}", exc_info=True)
            return self._fail(str(e), start, adapter="slack_direct")
