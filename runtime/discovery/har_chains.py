"""
har_chains — read a HAR of a target and report what a custom adaptor would have to do.

THE INPUT THAT DECIDES EVERYTHING. The HAR is the only honest record of how a target really
handles auth and session, and it answers the one question that matters before writing an
adaptor: is a prompt ONE request, or a sequence?

This is a different reading from `classify.load_har` (which derives a CLI adapter config: which
request is the chat turn, where the answer lives). Here the question is ordering: a value a
response PRODUCED that a later request CARRIED is a login, a token mint or a conversation id,
and each one is a step the adaptor must perform in that order. No chains means one request, and
the adaptor is a thin one.

A HAR holds a real session: live bearer tokens, session cookies, sometimes the customer's own
credentials. This module exists to be run on one and to have its output pasted into a chat with
an agent, so everything it returns — the text AND the `--json` structure — carries shapes and
flows and never a usable secret. `redact` keeps enough of a value to correlate it across requests
and never enough to use it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

DEFAULT_IGNORE = r"\.(png|jpe?g|gif|svg|css|woff2?|ico|map)(\?|$)"

# Request headers that carry a credential or a session. Reported redacted, with where the value
# came from when an earlier response produced it.
AUTH_HEADERS = ("authorization", "cookie", "x-api-key", "x-auth-token", "x-csrf-token",
                "x-xsrf-token", "x-session-id", "api-key", "x-access-token", "proxy-authorization")

# Only a value this long can be a token worth chaining; shorter strings ("ok", "en-US") match
# everywhere and would invent steps.
_MIN_CHAIN_LEN = 16


class HarError(ValueError):
    """The file is not a HAR, or holds nothing to read."""


def redact(value: Any, keep: int = 4) -> str:
    """Enough to correlate a value across requests, never enough to use it."""
    s = str(value or "")
    return f"{s[:keep]}…{s[-keep:]} ({len(s)}b)" if len(s) > keep * 3 else f"<{len(s)}b>"


def json_shape(text: Any, depth: int = 2) -> Any:
    """The key structure of a JSON body with every value replaced by its type."""
    try:
        d = json.loads(text)
    except Exception:
        return None

    def walk(v, d):
        if isinstance(v, dict):
            return {k: (walk(x, d - 1) if d > 0 else "…") for k, x in list(v.items())[:12]}
        if isinstance(v, list):
            return [walk(v[0], d - 1)] if v else []
        return type(v).__name__
    return walk(d, depth)


def produced_values(entry: Dict[str, Any]) -> Dict[str, str]:
    """Values a response produced that a later request might carry: the chain to find.

    Set-Cookie values and every string leaf of a JSON body between 16 and 4096 bytes. Kept
    in-process only; nothing here is ever part of the report.
    """
    out: Dict[str, str] = {}
    res = entry.get("response") or {}
    for h in res.get("headers") or []:
        if str(h.get("name", "")).lower() == "set-cookie":
            v = str(h.get("value") or "")
            name, _, rest = v.partition("=")
            out[f"cookie {name.strip()}"] = rest.split(";")[0]
    body = ((res.get("content") or {}).get("text")) or ""
    try:
        d = json.loads(body)
    except Exception:
        return out

    def walk(v, path=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}" if path else k)
        elif isinstance(v, str) and _MIN_CHAIN_LEN <= len(v) <= 4096:
            out[path] = v
    walk(d)
    return out


def load_entries(path: str) -> List[Dict[str, Any]]:
    try:
        har = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        raise HarError(f"cannot read {path} as HAR JSON: {e}")
    entries = ((har or {}).get("log") or {}).get("entries") if isinstance(har, dict) else None
    if not entries:
        raise HarError(f"no entries in {path} — is it a HAR export?")
    return entries


def read_chains(path: str, *, ignore: Optional[str] = DEFAULT_IGNORE,
                bodies: bool = False) -> Dict[str, Any]:
    """The report, as data. Every value in it is already redacted or a shape."""
    entries = load_entries(path)
    keep = ([e for e in entries
             if not re.search(ignore, (e.get("request") or {}).get("url", ""), re.I)]
            if ignore else list(entries))

    hosts: Dict[str, int] = {}
    for e in keep:
        host = urlparse((e.get("request") or {}).get("url", "")).netloc
        hosts[host] = hosts.get(host, 0) + 1

    sequence: List[Dict[str, Any]] = []
    produced: Dict[int, Dict[str, str]] = {}
    for i, e in enumerate(keep, 1):
        req, res = e.get("request") or {}, e.get("response") or {}
        u = urlparse(req.get("url", ""))
        ct = next((h.get("value", "") for h in res.get("headers") or []
                   if str(h.get("name", "")).lower() == "content-type"), "")
        row: Dict[str, Any] = {
            "n": i, "method": req.get("method", "?"), "host": u.netloc, "path": u.path,
            "status": res.get("status", "?"), "content_type": ct.split(";")[0], "auth": [],
        }
        for h in req.get("headers") or []:
            if str(h.get("name", "")).lower() not in AUTH_HEADERS:
                continue
            src = None
            for j, vals in produced.items():
                for label, v in vals.items():
                    if v and v in str(h.get("value") or ""):
                        src = {"n": j, "label": label}
                        break
                if src:
                    break
            row["auth"].append({"name": h.get("name"), "value": redact(h.get("value")),
                                "from": src})
        if bodies:
            shape = json_shape((req.get("postData") or {}).get("text") or "")
            if shape is not None:
                row["body"] = shape
            shape = json_shape((res.get("content") or {}).get("text") or "")
            if shape is not None:
                row["reply"] = shape
        sequence.append(row)
        produced[i] = produced_values(e)

    chains: List[Dict[str, Any]] = []
    seen = set()
    for i, e in enumerate(keep, 1):
        blob = json.dumps(e.get("request") or {})
        for j, vals in produced.items():
            if j >= i:
                continue
            for label, v in vals.items():
                if v and len(v) >= _MIN_CHAIN_LEN and v in blob and (j, label, i) not in seen:
                    seen.add((j, label, i))
                    chains.append({"from": j, "label": label, "to": i})

    return {"file": str(path), "total": len(entries), "kept": len(keep),
            "hosts": sorted(hosts.items(), key=lambda kv: -kv[1]),
            "sequence": sequence, "chains": chains, "bodies": bool(bodies)}


def render(report: Dict[str, Any]) -> str:
    """The human reading. Each arrow under SESSION CHAINS is a step the adaptor performs in order."""
    out = [f"{report['kept']} request(s) kept of {report['total']}", "",
           "HOSTS (every one an adaptor touches must be the endpoint or in _adaptor_domains)"]
    for host, n in report["hosts"]:
        out.append(f"  {n:>4}  {host}")
    out += ["", "SEQUENCE"]
    for row in report["sequence"]:
        out.append(f"  {row['n']:>3}. {row['method']:<6} {row['path'][:58]:<58} "
                   f"-> {row['status']} {row['content_type']}")
        for a in row["auth"]:
            src = f"  <- from #{a['from']['n']} {a['from']['label']}" if a.get("from") else ""
            out.append(f"         {a['name']}: {a['value']}{src}")
        if "body" in row:
            out.append(f"         body    {json.dumps(row['body'])[:160]}")
        if "reply" in row:
            out.append(f"         reply   {json.dumps(row['reply'])[:160]}")
    out += ["", "SESSION CHAINS  (a value a response produced, carried by a later request)"]
    if not report["chains"]:
        out += ["  none found - every request stands alone.",
                "  If that holds for the real flow, this target is ONE request and the",
                "  adaptor is a thin one. Say so rather than inventing steps."]
    else:
        for c in report["chains"]:
            out.append(f"  #{c['from']} {c['label']}  ->  #{c['to']}")
        out += ["", "  Each arrow is a step the adaptor must perform IN ORDER. A template",
                "  cannot do this, which is why this app needs an adaptor."]
    return "\n".join(out)
