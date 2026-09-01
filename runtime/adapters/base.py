"""
Base adapter interface for Ascend Proxy.

All bot adapters implement this interface so the Lambda handler
can route to any adapter type with a consistent contract.
"""

import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

# How long to wait for ONE reply from the target.
#
# Agentic targets routinely take 2-3 minutes, and some take considerably longer. A short fixed
# default silently converts a slow-but-healthy target into 100% probe failures: measured live, a
# 110s agent under a 20s config timeout failed every probe, the platform then auto-paused the
# assessment, and the run looked like "the bridge broke". So the default here is generous rather
# than tight, and both ends are tunable without editing code.
#
# There is still a ceiling, because a genuinely hung target must not pin a worker forever.
DEFAULT_TARGET_TIMEOUT_MS = 600_000        # 10 min — a real agent took exactly this and a 5 min
#                                            default failed it at 300s; "slow" has no small bound
MAX_TARGET_TIMEOUT_MS = 900_000            # 15 min — beyond this the target is hung, not slow


def _env_int(name: str, fallback: int) -> int:
    try:
        v = int(os.environ.get(name) or 0)
    except (TypeError, ValueError):
        return fallback
    return v if v > 0 else fallback


def resolve_timeout_s(config: Optional[Dict[str, Any]], default_ms: Optional[int] = None) -> float:
    """Seconds to wait for one target reply.

    Precedence: the config's `timeout_ms`, then $ASCEND_TARGET_TIMEOUT_MS, then a default sized for
    an agentic target — always clamped to $ASCEND_TARGET_MAX_TIMEOUT_MS so one hung target cannot
    hold a worker open indefinitely.
    """
    base = default_ms or _env_int("ASCEND_TARGET_TIMEOUT_MS", DEFAULT_TARGET_TIMEOUT_MS)
    try:
        ms = int((config or {}).get("timeout_ms") or base)
    except (TypeError, ValueError):
        ms = base
    ceiling = _env_int("ASCEND_TARGET_MAX_TIMEOUT_MS", MAX_TARGET_TIMEOUT_MS)
    return max(1.0, min(ms, ceiling) / 1000.0)


def utf8_text(r) -> str:
    """Response body as text, decoded UTF-8 when the server declares no charset.

    requests falls back to ISO-8859-1 for text/* responses with no charset
    (RFC 2616), which mangles UTF-8 agent replies (curly quotes render as
    "Ã¢â‚¬â„¢"). Many hosted agents stream text/plain with no charset, so honour a
    declared charset but default the silent case to UTF-8.
    """
    ct = ((getattr(r, "headers", None) or {}).get("content-type") or "")
    if "charset" not in ct.lower():
        try:
            r.encoding = "utf-8"
        except Exception:
            pass
    return r.text


def tls_min_adapter(minimum: str):
    """A requests HTTPAdapter pinning a minimum TLS version (the legacy bridge's
    `tls_config.min_version`). Returns None when the value isn't recognized."""
    import ssl
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.poolmanager import PoolManager
    except Exception:
        return None
    versions = {"1.0": ssl.TLSVersion.TLSv1, "1.1": ssl.TLSVersion.TLSv1_1,
                "1.2": ssl.TLSVersion.TLSv1_2, "1.3": ssl.TLSVersion.TLSv1_3}
    key = str(minimum).lower().replace("tlsv", "").replace("tls", "").strip()
    want = versions.get(key)
    if want is None:
        return None

    class _MinTLSAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            ctx = ssl.create_default_context()
            ctx.minimum_version = want
            kw["ssl_context"] = ctx
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize,
                                           block=block, **kw)

    return _MinTLSAdapter()


def tls_kwargs(config: Dict[str, Any]) -> Dict[str, Any]:
    """requests TLS kwargs from a config: `verify` (bool or CA-bundle path) and, for mTLS,
    `cert` (client cert). Lets one config reach self-signed internal targets and cert-gated
    enterprise gateways alike.

      verify_tls    false to skip verification (self-signed internal target)
      ca_bundle     path to a custom CA bundle (overrides verify_tls when set)
      client_cert   client certificate (PEM); with client_key for a split cert/key pair
      client_key    client private key (PEM)
      tls_min       minimum TLS version ("1.2"/"1.3"); applied via tls_min_adapter on a Session
    """
    import os
    kw: Dict[str, Any] = {}
    ca = config.get("ca_bundle")
    kw["verify"] = os.path.expanduser(ca) if ca else config.get("verify_tls", True)
    cc, ck = config.get("client_cert"), config.get("client_key")
    if cc and ck:
        kw["cert"] = (os.path.expanduser(cc), os.path.expanduser(ck))
    elif cc:
        kw["cert"] = os.path.expanduser(cc)
    return kw


class BotAdapter(ABC):
    """Abstract base for all bot adapters."""

    @abstractmethod
    async def send_prompt(self, prompt: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send a prompt to the target bot, return the response.

        Args:
            prompt: The text prompt to send.
            config: Adapter-specific configuration dict.

        Returns:
            {
                "response": str,        # The bot's text response
                "success": bool,
                "error": str | None,
                "duration_ms": int,
                "metadata": dict        # Adapter-specific metadata
            }
        """
        raise NotImplementedError

    def _ok(self, response: str, start: float, **metadata) -> Dict[str, Any]:
        return {
            "response": response,
            "success": True,
            "error": None,
            "duration_ms": int((time.time() - start) * 1000),
            "metadata": metadata,
        }

    def _fail(self, error: str, start: float, **metadata) -> Dict[str, Any]:
        return {
            "response": "",
            "success": False,
            "error": error,
            "duration_ms": int((time.time() - start) * 1000),
            "metadata": metadata,
        }
