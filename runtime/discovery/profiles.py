"""
Targets that publish their own contract.

Probing a bare endpoint finds *a* body the target accepts, which is not the same as the body its
own client sends. A minimal `{"message": …}` earns a 200 and a sensible answer while leaving out
the fields that decide what is actually under test — which workspace, which tools are live, whose
guardrail key the turn is scored against. The run that follows is green and measures the wrong
agent.

Some applications remove the guesswork: they expose an endpoint (or a "point Ascend here" panel)
that states the contract outright. A person onboarding that target would read it before doing
anything else. This module does the same, first, and falls back to probing only when the target
says nothing about itself.

A profile is three functions and no state:

    detect(origin)   -> is this that application? (unauthenticated, one GET)
    inspect(origin)  -> what it is, what it needs from the operator, what can be targeted
    build(origin, …) -> the adapter config, in the same shape probing produces

Nothing here stores a credential. Values pass through into the config exactly as a probed
target's would.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

TIMEOUT = 20


def origin_of(url: str) -> str:
    p = urlparse(url if "//" in str(url) else f"https://{url}")
    return f"{p.scheme or 'https'}://{p.netloc}"


def _get(url: str, headers: Optional[Dict[str, str]] = None, verify: bool = True) -> Tuple[int, Any]:
    try:
        r = requests.get(url, headers=headers or {}, timeout=TIMEOUT, verify=verify)
    except requests.RequestException:
        return 0, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


# --------------------------------------------------------------------------- Doppelganger
class Doppelganger:
    """Straiker's multi-vertical agentic demo host.

    One origin serves several workspaces (verticals), each its own agent with its own system
    prompt and tools. Access takes a passcode header; every turn must also carry a Straiker
    Defend key, which decides whose tenant scores it. The contract below mirrors the app's own
    "Point Ascend at this agent" panel field for field.
    """

    name = "doppelganger"
    label = "Doppelganger"

    @staticmethod
    def detect(origin: str, verify: bool = True) -> bool:
        status, body = _get(f"{origin}/api/config", verify=verify)
        return status == 200 and isinstance(body, dict) and body.get("app_name") == "Doppelganger"

    @staticmethod
    def needs() -> List[Dict[str, str]]:
        return [
            {"name": "x-demo-key", "kind": "header",
             "why": "the access passcode for this host"},
            {"name": "apiKey", "kind": "body",
             "why": "a Straiker Defend key — every turn is scored against the tenant it belongs to"},
        ]

    @classmethod
    def workspaces(cls, origin: str, headers: Dict[str, str], verify: bool = True) -> List[Dict[str, Any]]:
        status, body = _get(f"{origin}/api/workspaces", headers=headers, verify=verify)
        rows = (body or {}).get("workspaces") if isinstance(body, dict) else body
        return [w for w in (rows or []) if isinstance(w, dict)] if status == 200 else []

    @classmethod
    def inspect(cls, origin: str, headers: Dict[str, str], verify: bool = True) -> Dict[str, Any]:
        rows = cls.workspaces(origin, headers, verify)
        return {
            "profile": cls.name, "label": cls.label, "origin": origin,
            "endpoint": f"{origin}/api/chat",
            "needs": cls.needs(),
            "choose": "workspace",
            "workspaces": [{"slug": w.get("slug"), "name": w.get("name"),
                            "description": w.get("description")} for w in rows],
            "note": ("One origin, several agents. Each workspace is a separate target and should "
                     "be registered as its own application."),
        }

    @classmethod
    def build(cls, origin: str, *, workspace: str, headers: Dict[str, str],
              body_fields: Dict[str, Any], bearer: Optional[str] = None,
              verify: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """(config, facts). Raises ValueError naming exactly what is missing."""
        hdrs = {k: v for k, v in (headers or {}).items()}
        low = {k.lower(): v for k, v in hdrs.items()}
        key = body_fields.get("apiKey") or bearer or ""
        if not key and low.get("authorization", "").lower().startswith("bearer "):
            key = low["authorization"][7:].strip()
        missing = []
        if not low.get("x-demo-key"):
            missing.append("the access passcode (header x-demo-key)")
        if not key:
            missing.append("a Straiker Defend key (body field apiKey)")
        if missing:
            raise ValueError("this is a Doppelganger host and it needs " + " and ".join(missing))
        if not workspace:
            slugs = ", ".join(str(w.get("slug")) for w in cls.workspaces(origin, hdrs, verify))
            raise ValueError(f"this host serves several agents — choose one with --workspace "
                             f"({slugs or 'list them with `ascend target inspect`'})")

        status, ws = _get(f"{origin}/api/workspaces/{workspace}", headers=hdrs, verify=verify)
        if status != 200 or not isinstance(ws, dict):
            raise ValueError(f"workspace {workspace!r} was not found on {origin} (HTTP {status})")

        send = {k: v for k, v in hdrs.items() if k.lower() != "authorization"}
        send.setdefault("Content-Type", "application/json")
        body = {
            "message": "{{PROMPT}}",
            "apiKey": key,
            # The share token, not the slug: it is what the app's own panel emits.
            "workspace": ws.get("share_id") or ws.get("slug") or workspace,
            "userName": body_fields.get("userName") or "ascend",
            "activeConnectors": body_fields.get("activeConnectors") or [],
            "connectorCredentials": {},
            "llmProvider": body_fields.get("llmProvider") or (ws.get("llm") or {}).get("provider") or "bedrock",
            "llmKey": None,
            "systemPrompt": None,
            "ragContent": None,
        }
        cfg = {
            "adapter": "direct_api",
            "endpoint": f"{origin}/api/chat",
            "method": "POST",
            "headers": send,
            "body": body,
            "response_path": "message",
            "_profile": cls.name,
        }
        tools = [t.get("name") for t in (ws.get("tool_specs") or []) if isinstance(t, dict)]
        facts = {
            "profile": cls.name,
            "name": f"Doppelganger · {ws.get('name') or workspace}",
            "workspace": ws.get("slug") or workspace,
            "system_prompt": ws.get("system_prompt") or "",
            "purpose": ws.get("description") or "",
            "tools": tools,
            "agentic": bool(tools),
        }
        return cfg, facts


PROFILES = [Doppelganger]


def detect(url: str, verify: bool = True):
    """The profile for this target, or None. Costs one unauthenticated GET per known profile."""
    origin = origin_of(url)
    for prof in PROFILES:
        try:
            if prof.detect(origin, verify=verify):
                return prof
        except Exception:
            continue
    return None
