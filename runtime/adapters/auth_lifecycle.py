"""
auth_lifecycle.py — L3 auth lifecycle for HTTP adapters (the `build-adapter` skill's layer 3).

A long assessment outlives a short-lived credential. A bearer token captured at adapter-build
time — a mobile app's access token, an OAuth token, a login cookie — expires part-way through the
run. Every probe after that gets a 401, the adapter reports a failure, and the assessment scores a
wall of "refusals" that look like the target held up. The run finishes looking clean while
measuring nothing. That is the same false-pass class as a dead bridge, and it is the single most
common blocker when onboarding a mobile/authenticated target.

This module keeps the credential valid for the whole run, declaratively, for any HTTP adapter.

Config (all optional — with no `auth` block the adapter behaves EXACTLY as before):

    "auth": {
      "lifecycle": "reauth_on_401",   # static | refresh_on_ttl | reauth_on_401 | cookie_rotation
      "token_endpoint": "https://api.example.com/oauth/token",
      "token_method": "POST",
      "token_headers": {"Content-Type": "application/json"},
      "token_body": {"grant_type": "refresh_token", "refresh_token": "..."},
      "token_form": false,             # true -> send token_body form-encoded, not JSON
      "token_path": "access_token",    # dot-path to the token in the mint response
      "expires_in_path": "expires_in", # dot-path to a TTL in SECONDS (optional)
      "ttl_s": 900,                    # explicit TTL if the response carries none (optional)
      "refresh_skew_s": 60,            # re-mint this long BEFORE expiry
      "variable": "TOKEN"              # substitutes {{TOKEN}} in headers/endpoint/body
    }

Lifecycles:
  static          - use the configured headers as-is (default; unchanged behavior)
  refresh_on_ttl  - mint, then re-mint proactively once the TTL (minus skew) has elapsed
  reauth_on_401   - mint lazily; on a 401/403 from the target, re-mint once and retry the probe
  cookie_rotation - re-run the login request and carry its cookies forward (session targets)

`refresh_on_ttl` and `reauth_on_401` compose: a TTL-refreshed token is ALSO re-minted on a 401,
because a server-side revocation does not wait for your clock.

Thread-safety matters here: the bridge answers probes on up to `max_workers` threads against one
config. A per-call manager would let 10 threads stampede the token endpoint (and some IdPs
rate-limit or invalidate the previous token on each mint). Managers are therefore cached per
auth-config and mint under a lock, so exactly one mint happens and every worker shares the result.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

LIFECYCLES = ("static", "refresh_on_ttl", "reauth_on_401", "cookie_rotation")
DEFAULT_SKEW_S = 60.0
DEFAULT_TTL_S = 900.0          # only used when the mint response carries no expiry and none is set

_MANAGERS: Dict[str, "TokenManager"] = {}
_MANAGERS_LOCK = threading.Lock()


def _extract(data: Any, path: str) -> Any:
    """Dot-notation extraction, list indices included (same contract as the adapters')."""
    if not path:
        return None
    cur = data
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _substitute(obj: Any, var: str, value: str) -> Any:
    """Replace {{VAR}} anywhere in a str / dict / list structure."""
    token_ph = "{{%s}}" % var
    if isinstance(obj, str):
        return obj.replace(token_ph, value)
    if isinstance(obj, dict):
        return {k: _substitute(v, var, value) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, var, value) for v in obj]
    return obj


def needs_auth(config: Dict[str, Any]) -> bool:
    """True only when the config declares a non-static auth lifecycle worth managing."""
    auth = (config or {}).get("auth")
    if not isinstance(auth, dict):
        return False
    lifecycle = str(auth.get("lifecycle") or "static").lower()
    if lifecycle == "static":
        return False
    return bool(auth.get("token_endpoint"))


class AuthError(Exception):
    """Minting failed — surfaced so the adapter reports 'auth expired' instead of a fake refusal."""


class TokenManager:
    """Mints and caches one credential for one auth config, shared across worker threads."""

    def __init__(self, auth: Dict[str, Any]):
        self.cfg = dict(auth or {})
        self.lifecycle = str(self.cfg.get("lifecycle") or "static").lower()
        self.variable = str(self.cfg.get("variable") or "TOKEN")
        self.skew = float(self.cfg.get("refresh_skew_s") or DEFAULT_SKEW_S)
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._cookies: Dict[str, str] = {}
        self.mints = 0                      # observable: how often we re-minted (QA asserts on this)

    # ---- minting -----------------------------------------------------------
    def _mint(self) -> Tuple[str, float, Dict[str, str]]:
        """Call the token endpoint once. Returns (token, expires_at, cookies)."""
        url = self.cfg.get("token_endpoint")
        if not url:
            raise AuthError("auth.token_endpoint is required for a non-static lifecycle")
        method = str(self.cfg.get("token_method") or "POST").upper()
        headers = dict(self.cfg.get("token_headers") or {})
        body = self.cfg.get("token_body")
        kw: Dict[str, Any] = {}
        if body is not None:
            if self.cfg.get("token_form"):
                kw["data"] = body
            else:
                kw["json"] = body
                headers.setdefault("Content-Type", "application/json")
        timeout = float(self.cfg.get("token_timeout_ms") or 20000) / 1000.0
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise AuthError(f"token endpoint unreachable: {e}") from e
        if resp.status_code >= 400:
            raise AuthError(f"token endpoint returned HTTP {resp.status_code}: "
                            f"{(resp.text or '')[:200]}")
        cookies = {c.name: c.value for c in resp.cookies}
        token = ""
        ttl = None
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = None
        if data is not None:
            tok = _extract(data, self.cfg.get("token_path") or "access_token")
            if tok is not None:
                token = str(tok)
            exp_path = self.cfg.get("expires_in_path")
            if exp_path:
                raw = _extract(data, exp_path)
                try:
                    ttl = float(raw)
                except (TypeError, ValueError):
                    ttl = None
        if not token and not cookies:
            # cookie_rotation may legitimately return only cookies; anything else must yield a token
            raise AuthError(
                f"could not extract a token at path "
                f"{self.cfg.get('token_path') or 'access_token'!r} from the mint response")
        if ttl is None:
            ttl = float(self.cfg.get("ttl_s") or DEFAULT_TTL_S)
        self.mints += 1
        logger.info("auth: minted a fresh credential (lifecycle=%s, ttl=%ss, mint #%d)",
                    self.lifecycle, int(ttl), self.mints)
        return token, time.time() + max(0.0, ttl - self.skew), cookies

    def _expired(self) -> bool:
        if self._token is None and not self._cookies:
            return True
        if self.lifecycle in ("refresh_on_ttl", "cookie_rotation"):
            return time.time() >= self._expires_at
        return False        # reauth_on_401 refreshes reactively, not on a clock

    def token(self) -> str:
        """A valid credential, minting/refreshing under the lock so workers never stampede."""
        with self._lock:
            if self._expired():
                self._token, self._expires_at, self._cookies = self._mint()
            return self._token or ""

    def cookies(self) -> Dict[str, str]:
        with self._lock:
            if self._expired():
                self._token, self._expires_at, self._cookies = self._mint()
            return dict(self._cookies)

    def invalidate(self) -> None:
        """Force the next use to re-mint (called on a 401/403 from the target)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            self._cookies = {}

    def retries_on_401(self) -> bool:
        return self.lifecycle in ("reauth_on_401", "refresh_on_ttl", "cookie_rotation")


def manager_for(config: Dict[str, Any]) -> Optional[TokenManager]:
    """The shared TokenManager for this config's auth block (None when auth is static/absent)."""
    if not needs_auth(config):
        return None
    auth = config["auth"]
    key = json.dumps(auth, sort_keys=True, default=str)
    with _MANAGERS_LOCK:
        mgr = _MANAGERS.get(key)
        if mgr is None:
            mgr = TokenManager(auth)
            _MANAGERS[key] = mgr
        return mgr


def apply_auth(config: Dict[str, Any], endpoint: str, headers: Dict[str, Any],
               body: Any) -> Tuple[str, Dict[str, Any], Any, Optional[TokenManager]]:
    """Substitute a live credential into (endpoint, headers, body).

    Returns them unchanged (and a None manager) when the config declares no managed lifecycle, so
    an existing static config takes exactly the same path it always did.
    """
    mgr = manager_for(config)
    if mgr is None:
        return endpoint, headers, body, None
    tok = mgr.token()
    var = mgr.variable
    endpoint = _substitute(endpoint, var, tok)
    headers = _substitute(dict(headers or {}), var, tok)
    body = _substitute(body, var, tok)
    ck = mgr.cookies()
    if ck:
        jar = "; ".join(f"{k}={v}" for k, v in ck.items())
        existing = headers.get("Cookie")
        headers["Cookie"] = f"{existing}; {jar}" if existing else jar
    return endpoint, headers, body, mgr


def reset_for_tests() -> None:
    """Drop every cached manager (test isolation only)."""
    with _MANAGERS_LOCK:
        _MANAGERS.clear()
