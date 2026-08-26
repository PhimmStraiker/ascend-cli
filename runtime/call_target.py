"""
call_target.py — the SDK seam, wired to the adapter framework.

`bridge_client.py` leaves one function for you to write: take a leased probe's
body/headers, call your target, return (status_code, response). Here that seam
dispatches into the proven adapter framework (13 adapters) with a conversation/
session model, so a single implementation handles REST, SSE, WebSocket (incl.
chunked text/json framing), multi-step session APIs, browser widgets, etc.
"""
import copy
import logging
import time
from typing import Any, Dict, Optional, Tuple

from dispatch import (ConversationRouter, load_config, extract_prompt,
                      shape_result, conversation_key, STATEFUL_ADAPTERS, merge_auth)

logger = logging.getLogger("ascendbridge.call_target")

# Refresh an oauth2 token this long after the last materialize — comfortably under a typical
# 1h Entra/OAuth token TTL, so a long assessment never sends an expired token.
_DEFAULT_AUTH_REFRESH_S = 2700.0


class TargetCaller:
    """Builds a lease-client handler bound to one adapter config."""

    def __init__(self, adapter_type: str, config_name: str,
                 config: Optional[Dict[str, Any]] = None,
                 timeout_s: float = 110.0) -> None:
        # 110s stays under probe_shadow's 120s BRIDGE_RESPONSE_TIMEOUT.
        raw = config if config is not None else load_config(config_name)
        # Keep the pristine, unmerged config so we can re-materialize a fresh token mid-run.
        self._raw = copy.deepcopy(raw)
        # Resolve any auth block up-front so the LIVE relay sends the same credentials
        # `adapter validate` proved worked. Without this, validate=ok and every probe 401s.
        self.config = merge_auth(copy.deepcopy(self._raw))
        self.adapter_type = adapter_type or self.config.get("adapter", "direct_api")
        self.config_name = config_name if isinstance(config_name, str) else "inline"
        self.timeout_s = timeout_s
        self.router = ConversationRouter()
        # oauth2 tokens expire mid-run; re-materialize on a TTL (B5).
        auth = self._raw.get("auth")
        self._auth_refreshable = isinstance(auth, dict) and auth.get("type") == "oauth2"
        self._refresh_s = float(self._raw.get("auth_refresh_ms", _DEFAULT_AUTH_REFRESH_S * 1000)) / 1000.0
        self._last_auth = time.monotonic()

    def _maybe_refresh_auth(self) -> None:
        """Re-mint a short-lived oauth2 token before it expires, so a long assessment
        doesn't start 401ing halfway through. No-op for static/none auth."""
        if not self._auth_refreshable:
            return
        if (time.monotonic() - self._last_auth) < self._refresh_s:
            return
        self.config = merge_auth(copy.deepcopy(self._raw))
        self._last_auth = time.monotonic()
        logger.info("auth: refreshed oauth2 credentials mid-run")

    @property
    def is_stateful(self) -> bool:
        return (self.adapter_type in STATEFUL_ADAPTERS
                and not self.config.get("conversation_key"))

    def recommended_workers(self) -> int:
        """Sequential for stateful/multi-turn targets unless they expose a key."""
        if "max_workers" in self.config:
            return int(self.config["max_workers"])
        return 1 if self.is_stateful else 10

    def handler(self, message: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        self._maybe_refresh_auth()
        payload = message.get("payload", {})
        body = payload.get("body")
        try:
            prompt = extract_prompt(body, self.config)
        except Exception as e:
            return 400, {"response": "", "_error": f"prompt-extract: {e}"}
        conv = conversation_key(message, self.config)
        result = self.router.send(
            self.adapter_type, self.config, self.config_name,
            prompt, conv, self.timeout_s)
        return shape_result(result, self.config)

    def reset(self) -> int:
        return self.router.reset()
