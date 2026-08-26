"""
sentinel_stream — for agents whose reply arrives as SENTINEL-DELIMITED JSON frames
in a plain-text body (not SSE, not NDJSON, not WebSocket).

Some hosted agent platforms stream a single HTTP response body framed with literal
begin/end markers, e.g.:

    BEGIN_MARKER{"type":"state","state":{"events":[...]}}END_MARKER
    BEGIN_MARKER{"type":"state","state":{"events":[...]}}END_MARKER

Content-Type is often `text/plain`, so neither the SSE nor the NDJSON reader applies.
This adapter extracts every frame between the markers, walks each frame for agent
turns, and returns the last (or concatenated) agent text.

It also supports the very common two-step lifecycle on these platforms:
  1. an optional "start" call that mints a conversation id (+ optional session key),
  2. subsequent message calls that carry that id.

Config:
  url               endpoint (same for start and message on most platforms)
  method            default POST
  headers           extra headers
  begin_marker      frame start sentinel (default "BOT_CHAT_EVENT_BEGIN")
  end_marker        frame end sentinel   (default "BOT_CHAT_EVENT_END")
  start:            optional session bootstrap
      body          request body template for the start call
      conv_path     dot-path to the conversation id in a parsed frame
                    (default "conversationID")
      key_path      optional dot-path to a session key (default "encryptionKey")
  message:
      body          body template for a message; placeholders substituted:
                    {{PROMPT}}, {{CONV}}, {{KEY}}, {{INDEX}}
  extract:
      events_path   dot-path to the events array inside a frame
                    (default "state.events")
      message_path  dot-path to the message object inside an event (default "message")
      author_field  field naming the author/role (default "author")
      agent_authors authors that count as the agent (default ["AGENT","assistant","bot"])
      text_field    field holding the text (default "text")
      skip_flags    truthy fields marking a non-answer frame
                    (default ["isProgressIndicator"])
      aggregate     "last" (default) or "concat"
  timeout_ms        default 60000
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

from .base import BotAdapter, utf8_text
from .websocket_direct import _json_escape, _dot

logger = logging.getLogger(__name__)

DEFAULT_BEGIN = "BOT_CHAT_EVENT_BEGIN"
DEFAULT_END = "BOT_CHAT_EVENT_END"
DEFAULT_AGENT_AUTHORS = ["AGENT", "assistant", "bot", "agent"]


def parse_frames(text: str, begin: str, end: str):
    """Yield decoded JSON objects found between the begin/end sentinels."""
    pattern = re.compile(re.escape(begin) + r"(.*?)" + re.escape(end), re.S)
    for m in pattern.finditer(text or ""):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except (ValueError, TypeError):
            continue


class SentinelStreamAdapter(BotAdapter):
    def __init__(self):
        self._conv = None
        self._key = None
        self._index = 0

    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        start_t = time.time()
        url = config.get("url")
        if not url:
            return self._fail("sentinel_stream needs a url", start_t)
        method = config.get("method", "POST").upper()
        timeout = config.get("timeout_ms", 60000) / 1000
        headers = {"Content-Type": "application/json", **(config.get("headers") or {})}
        begin = config.get("begin_marker", DEFAULT_BEGIN)
        end = config.get("end_marker", DEFAULT_END)
        ex = config.get("extract") or {}

        # 1. bootstrap a conversation if configured and not already held
        start_cfg = config.get("start") or {}
        if start_cfg and self._conv is None:
            try:
                r = requests.request(method, url, json=self._render(start_cfg.get("body", {}), prompt="", conv="", key=""),
                                     headers=headers, timeout=timeout)
                r.raise_for_status()
            except requests.RequestException as e:
                return self._fail(f"start failed: {e}", start_t,
                                  status_code=getattr(getattr(e, "response", None), "status_code", None))
            conv_path = start_cfg.get("conv_path", "conversationID")
            key_path = start_cfg.get("key_path", "encryptionKey")
            for obj in parse_frames(utf8_text(r), begin, end):
                self._conv = self._conv or _dot(obj, conv_path)
                self._key = self._key or _dot(obj, key_path)
            if not self._conv:
                return self._fail(f"could not extract conversation id via '{conv_path}'", start_t,
                                  raw=utf8_text(r)[:400])

        # 2. send the message
        msg_cfg = config.get("message") or {}
        body = self._render(msg_cfg.get("body", {"message": "{{PROMPT}}"}),
                            prompt=prompt, conv=self._conv or "", key=self._key or "")
        try:
            r = requests.request(method, url, json=body, headers=headers, timeout=timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            return self._fail(f"message failed: {e}", start_t,
                              status_code=getattr(getattr(e, "response", None), "status_code", None))
        self._index += 1

        texts = self._agent_texts(utf8_text(r), begin, end, ex)
        if not texts:
            return self._fail("no agent frames found (check begin/end markers or extract paths)",
                              start_t, raw=utf8_text(r)[:400])
        out = " ".join(texts) if ex.get("aggregate") == "concat" else texts[-1]
        return self._ok(out.strip(), start_t, adapter="sentinel_stream",
                        conv=self._conv, frames=len(texts))

    def _render(self, template: Any, *, prompt: str, conv: str, key: str) -> Any:
        s = json.dumps(template)
        s = (s.replace("{{PROMPT}}", _json_escape(prompt))
               .replace("{{CONV}}", _json_escape(str(conv)))
               .replace("{{KEY}}", _json_escape(str(key)))
               .replace('"{{INDEX}}"', str(self._index))
               .replace("{{INDEX}}", str(self._index)))
        return json.loads(s)

    def _agent_texts(self, text: str, begin: str, end: str, ex: Dict[str, Any]) -> List[str]:
        events_path = ex.get("events_path", "state.events")
        message_path = ex.get("message_path", "message")
        author_field = ex.get("author_field", "author")
        agent_authors = [a.lower() for a in (ex.get("agent_authors") or DEFAULT_AGENT_AUTHORS)]
        text_field = ex.get("text_field", "text")
        skip_flags = ex.get("skip_flags", ["isProgressIndicator"])
        out: List[str] = []
        for obj in parse_frames(text, begin, end):
            evs = _dot(obj, events_path)
            if not isinstance(evs, list):
                evs = [obj] if isinstance(obj, dict) else []
            for ev in evs:
                msg = _dot(ev, message_path) if message_path else ev
                if not isinstance(msg, dict):
                    continue
                if any(msg.get(f) for f in skip_flags):
                    continue
                author = str(msg.get(author_field, "")).lower()
                if agent_authors and author and author not in agent_authors:
                    continue
                t = msg.get(text_field)
                if t:
                    out.append(str(t))
        return out
