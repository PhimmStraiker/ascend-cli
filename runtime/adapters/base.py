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
# This number is NOT the binding constraint, and making it large does not make a slow target work.
# The bridge abandons a probe at the platform's response window (call_target's
# _DEFAULT_BRIDGE_RESPONSE_TIMEOUT_S), so a bigger value here buys nothing through the bridge and
# actively hurts: the router gives up while the HTTP call keeps running, holding a worker and a
# socket for the remainder. The default therefore sits just UNDER that window, which also means the
# adapter reports the timeout itself — a clean per-probe error carrying a status — instead of the
# router reporting a generic one.
#
# Raising this is only meaningful together with the bridge window (and for the paths that never go
# through it, such as `adapter validate`). The ceiling guards an explicitly configured value.
DEFAULT_TARGET_TIMEOUT_MS = 100_000
MAX_TARGET_TIMEOUT_MS = 900_000            # above this a target is hung, not slow


def resolve_ms(config: Optional[Dict[str, Any]], key: Optional[str],
               env_name: str, default_ms: int) -> int:
    """First positive value of: config[key], $env_name, default_ms.

    Shared by the target-reply and bridge-window resolvers so the precedence rule is written once.
    """
    sources = ((config or {}).get(key) if key else None, os.environ.get(env_name))
    for source in sources:
        try:
            ms = int(source or 0)
        except (TypeError, ValueError):
            continue
        if ms > 0:
            return ms
    return default_ms


# The platform gives a bridge a bounded window to return each probe result (iris probe_shadow's
# BRIDGE_RESPONSE_TIMEOUT). Two things make this sharper than it looks:
#   * the clock starts when the probe is QUEUED, not when the bridge starts calling the target, so a
#     probe can spend much of its budget waiting to be leased;
#   * blowing it surfaces as a synthetic 504 that is indistinguishable from a real target failure,
#     which feeds the platform's target-health streak and auto-pauses the assessment.
# So a target near this window does not merely run slowly — it produces a whole run of false
# failures. Env-tunable so that when the platform window is raised, nothing here needs a code change.
PLATFORM_PROBE_WINDOW_S = 120.0


def platform_probe_window_s() -> float:
    return resolve_ms(None, None, "ASCEND_PLATFORM_PROBE_WINDOW_MS",
                      int(PLATFORM_PROBE_WINDOW_S * 1000)) / 1000.0


def platform_window_warning(duration_ms: Any) -> Optional[str]:
    """Warn when a MEASURED target reply time will not survive the platform's per-probe window.

    Returns None when the target is comfortably inside it. This exists so the operator learns it
    from one probe, instead of from an assessment full of failures that reads as a broken bridge.
    """
    try:
        secs = float(duration_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    window = platform_probe_window_s()
    if secs >= window:
        return (f"this target replied in {secs:.0f}s, at or beyond the platform's ~{window:.0f}s "
                f"per-probe window. Every probe will time out platform-side and be recorded as a "
                f"failure, which auto-pauses the assessment — so the run would report no findings "
                f"having measured nothing. Raising the adapter timeout does NOT help; the window "
                f"has to be raised on the platform side first.")
    if secs >= window * 0.6:
        return (f"this target replied in {secs:.0f}s, against a ~{window:.0f}s platform per-probe "
                f"window. The probe's clock starts when it is QUEUED, not when the bridge calls the "
                f"target, so a probe that waits to be leased can still time out. Keep QPM and "
                f"max_workers low, and treat sporadic failures as this, not as target refusals.")
    return None


def resolve_timeout_s(config: Optional[Dict[str, Any]]) -> float:
    """Seconds to wait for one target reply.

    Precedence: the config's `timeout_ms`, then $ASCEND_TARGET_TIMEOUT_MS, then the default above —
    always clamped to $ASCEND_TARGET_MAX_TIMEOUT_MS so one hung target cannot hold a worker open
    indefinitely.
    """
    ms = resolve_ms(config, "timeout_ms", "ASCEND_TARGET_TIMEOUT_MS", DEFAULT_TARGET_TIMEOUT_MS)
    ceiling = resolve_ms(None, None, "ASCEND_TARGET_MAX_TIMEOUT_MS", MAX_TARGET_TIMEOUT_MS)
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
