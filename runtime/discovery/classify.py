"""
discovery.classify — the deterministic per-layer classifiers.

Input is *evidence*: a parsed HAR (dict) and/or a list of captured
request/response pairs. For each of the six adapter layers
(``transport``, ``auth``, ``auth_lifecycle``, ``session``, ``identity``,
``rate``) a bounded classifier emits::

    {"value": <chosen>, "params": {...}, "confidence": 0.0-1.0, "evidence": "why"}

:func:`compose` folds the six results into a runnable adapter config, choosing
the closest of the existing adapters and its known knobs (see
``docs/CAPABILITY_MATRIX.md``). :func:`classify_evidence` runs the whole thing and
reports which layers still need a human (``unresolved``).

Purity
------
Nothing here performs network I/O — every function is a deterministic transform
over evidence dicts, so the whole module is unit-testable offline. Secrets that
appear in evidence are **never** copied into the emitted config; auth params
carry an ``env:`` ``value_ref`` placeholder instead, and record only the header
*name* that carried the secret.
"""
from __future__ import annotations

import json
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

LAYER_NAMES = ("transport", "auth", "auth_lifecycle", "session", "identity", "rate")

# Confidence below this marks a layer "unresolved" (needs operator/agent input).
LOW_CONF = 0.5

# Header names that, if present on the chat request, carry an auth secret.
_SECRET_HEADERS = {
    "authorization", "x-api-key", "api-key", "apikey", "x-auth-token",
    "x-authentication", "authentication", "x-access-token", "cookie",
}
_CSRF_HEADERS = {"x-csrf-token", "x-xsrf-token", "csrf-token", "x-csrftoken"}
_ID_FIELDS = (
    "id", "sessionId", "session_id", "conversationId", "conversation_id",
    "threadId", "thread_id", "chatId", "chat_id", "ticketId", "ticket_id",
    "requestId", "request_id", "jobId", "job_id",
)
_PROMPT_FIELDS = ("prompt", "message", "input", "text", "query", "content", "question", "msg")
_RESPONSE_PATH_GUESSES = (
    "response", "message", "text", "content", "answer", "output", "reply",
    "data.text", "data.message", "data.content", "result", "completion",
    "choices.0.message.content", "messages.0.message", "messages.0.text",
    "candidates.0.content.parts.0.text",
)
_ASSET_RE = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|webp)(\?|$)", re.I)
_GREETINGS = {"hi", "hello", "hey", "hola", "start", "begin"}


class ClassifyError(ValueError):
    """Raised when evidence cannot be parsed into the normalized form."""


# --------------------------------------------------------------------------- #
# Evidence ingestion / normalization                                          #
# --------------------------------------------------------------------------- #
def _headers_to_dict(headers: Any) -> Dict[str, str]:
    """Normalize headers (HAR list or dict) to a lowercased-key dict.

    Drops HTTP/2 pseudo-headers (`:authority`, `:method`, `:path`, `:scheme`). Chrome writes these
    into every HAR of an HTTP/2 site — which today is nearly all of them — but they are protocol
    internals, not real headers: sending one over the wire raises
    "Invalid ... character(s) in header name: ':authority'" and the whole request fails. They carry
    nothing the URL and method don't already have.
    """
    out: Dict[str, str] = {}
    if isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and "name" in h:
                name = str(h["name"])
                if name.startswith(":"):
                    continue
                out[name.lower()] = str(h.get("value", ""))
    elif isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).startswith(":"):
                continue
            out[str(k).lower()] = str(v)
    return out


def _maybe_json(text: Optional[str]) -> Any:
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        return None


def _query_from_url(url: str) -> Dict[str, str]:
    from urllib.parse import urlparse, parse_qsl
    return dict(parse_qsl(urlparse(url).query))


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def _norm_entry(request: Dict[str, Any], response: Dict[str, Any],
                started: Optional[float] = None) -> Dict[str, Any]:
    """Normalize one request/response pair into the internal shape."""
    req_headers = _headers_to_dict(request.get("headers"))
    url = request.get("url", "")
    raw_body = request.get("body")
    if isinstance(raw_body, (dict, list)):
        req_json = raw_body
        raw_body_str = json.dumps(raw_body)
    else:
        raw_body_str = raw_body if isinstance(raw_body, str) else request.get("raw_body")
        req_json = _maybe_json(raw_body_str)

    resp_headers = _headers_to_dict(response.get("headers"))
    resp_body = response.get("body")
    if isinstance(resp_body, (dict, list)):
        resp_json = resp_body
        resp_body_str = json.dumps(resp_body)
    else:
        resp_body_str = resp_body if isinstance(resp_body, str) else response.get("raw_body")
        resp_json = _maybe_json(resp_body_str)

    query = request.get("query") or _query_from_url(url)
    content_type = resp_headers.get("content-type", "")
    return {
        "request": {
            "method": str(request.get("method", "GET")).upper(),
            "url": url,
            "headers": req_headers,
            "query": {str(k).lower(): str(v) for k, v in dict(query).items()},
            "json": req_json,
            "raw_body": raw_body_str or "",
        },
        "response": {
            "status": int(response.get("status", 0) or 0),
            "headers": resp_headers,
            "json": resp_json,
            "raw_body": resp_body_str or "",
            "content_type": content_type,
        },
        "started_ms": started,
    }


def _normalize_pairs(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in pairs:
        if "request" in p and "response" in p:
            out.append(_norm_entry(p["request"], p["response"], p.get("started_ms")))
        else:  # flat shape
            out.append(_norm_entry(p, p.get("response", {}), p.get("started_ms")))
    return out


def load_har(path: str, prompt_sent: Optional[str] = None) -> Dict[str, Any]:
    """Parse a HAR file at ``path`` into normalized evidence.

    ``prompt_sent`` is the text the operator actually typed into the chat during the session the
    HAR captured. A browser export contains dozens of requests; without knowing what was typed,
    the classifier has to GUESS which one is the chat turn, and on a noisy real page it guesses
    wrong. With it, the chat request is simply the one whose body contains that exact string —
    ground truth that beats every heuristic. Pass it whenever you can.

    Returns ``{"pairs": [...], "ws_messages": [...], "prompt_sent": ...}`` ready for
    :func:`classify_evidence`. Pure/offline — just file + JSON parsing.
    """
    with open(path, "r", encoding="utf-8") as fh:
        har = json.load(fh)
    return har_to_evidence(har, prompt_sent=prompt_sent)


def har_to_evidence(har: Dict[str, Any], prompt_sent: Optional[str] = None) -> Dict[str, Any]:
    """Convert an in-memory parsed HAR dict into normalized evidence."""
    entries = (((har or {}).get("log") or {}).get("entries")) or []
    pairs: List[Dict[str, Any]] = []
    ws_messages: List[Dict[str, Any]] = []
    for e in entries:
        req = e.get("request", {}) or {}
        resp = e.get("response", {}) or {}
        request = {
            "method": req.get("method", "GET"),
            "url": req.get("url", ""),
            "headers": req.get("headers", []),
            "query": {q.get("name", "").lower(): q.get("value", "")
                      for q in (req.get("queryString") or [])},
            "raw_body": (req.get("postData") or {}).get("text"),
        }
        response = {
            "status": resp.get("status", 0),
            "headers": resp.get("headers", []),
            "raw_body": (resp.get("content") or {}).get("text"),
        }
        started = _har_started_ms(e)
        pair = _norm_entry(request, response, started)
        # Carry the HAR mimeType even when there is no body text.
        if not pair["response"]["content_type"]:
            pair["response"]["content_type"] = (resp.get("content") or {}).get("mimeType", "")
        pairs.append(pair)
        for m in e.get("_webSocketMessages", []) or []:
            ws_messages.append({"url": req.get("url", ""), **m})
    ev = {"pairs": pairs, "ws_messages": ws_messages}
    if prompt_sent:
        ev["prompt_sent"] = prompt_sent.strip()
    return ev


def _har_started_ms(entry: Dict[str, Any]) -> Optional[float]:
    ts = entry.get("startedDateTime")
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0
    except (ValueError, TypeError):
        return None


_REPLY_TEXT: Dict[str, Any] = {"v": None}


def _evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either normalized evidence, a raw HAR, or a bare pairs list."""
    if isinstance(evidence, list):
        return {"pairs": _normalize_pairs(evidence), "ws_messages": []}
    if not isinstance(evidence, dict):
        raise ClassifyError(f"evidence must be dict/list, got {type(evidence).__name__}")
    if "log" in evidence and "pairs" not in evidence:
        return har_to_evidence(evidence)
    pairs = evidence.get("pairs")
    if pairs is None:
        raise ClassifyError("evidence dict has no 'pairs' (or 'log') key")
    # Re-normalize in case caller passed raw pairs.
    if pairs and "request" in pairs[0] and "headers" in (pairs[0]["request"] or {}) \
            and isinstance(pairs[0]["request"].get("headers"), dict):
        norm = pairs  # already normalized
    else:
        norm = _normalize_pairs(pairs)
    # carry the capture ground-truth through — the classifiers use it to pick the
    # request that actually carried our prompt (beats every heuristic).
    _REPLY_TEXT["v"] = evidence.get("reply_text")
    return {"pairs": norm, "ws_messages": evidence.get("ws_messages", []),
            "prompt_sent": evidence.get("prompt_sent"),
            "reply_text": evidence.get("reply_text")}


# --------------------------------------------------------------------------- #
# Chat-pair selection                                                         #
# --------------------------------------------------------------------------- #
def _is_asset(url: str) -> bool:
    return bool(_ASSET_RE.search(url or ""))


def _pick_chat_index(pairs: List[Dict[str, Any]], known_prompt: Optional[str] = None) -> Optional[int]:
    """Heuristically pick the pair that carries the scored prompt/answer.

    Prefers non-asset POST/PUT requests whose response looks like a chat answer,
    scored by response size and a prompt-like request body. Falls back to the
    largest non-asset response.
    """
    # GROUND TRUTH: during a live capture we know exactly what we typed, so the chat
    # call is the request whose body literally contains that prompt. This beats every
    # heuristic and prevents picking an analytics/personalization vendor's traffic.
    if known_prompt:
        needle = known_prompt.strip()
        exact = [i for i, p in enumerate(pairs)
                 if needle and needle in (p["request"].get("raw_body") or "")]
        if exact:
            # if several carry it, prefer the one with the biggest response (the answer)
            return max(exact, key=lambda i: len(pairs[i]["response"].get("raw_body") or ""))

    best_idx, best_score = None, -1.0
    for i, p in enumerate(pairs):
        req, resp = p["request"], p["response"]
        if _is_asset(req["url"]):
            continue
        ct = resp["content_type"]
        score = 0.0
        if req["method"] in ("POST", "PUT", "PATCH"):
            score += 2.0
        if "event-stream" in ct or "ndjson" in ct or "application/json" in ct:
            score += 2.0
        if _request_has_prompt(req) is not None:
            score += 3.0
        score += min(len(resp["raw_body"] or "") / 500.0, 4.0)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


def _request_has_prompt(req: Dict[str, Any]) -> Optional[str]:
    """Return the prompt-like string in a request body, if any."""
    body = req.get("json")
    if isinstance(body, dict):
        for f in _PROMPT_FIELDS:
            if isinstance(body.get(f), str):
                return body[f]
        # deepest / longest string fallback
        longest = _longest_string(body)
        if longest and len(longest) >= 3:
            return longest
    elif isinstance(body, str) and body.strip():
        return body
    return None


def _longest_string(obj: Any) -> Optional[str]:
    best = None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if best is None or len(cur) > len(best):
                best = cur
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return best


# --------------------------------------------------------------------------- #
# Layer 1 — transport                                                         #
# --------------------------------------------------------------------------- #
def classify_transport(ev: Dict[str, Any], chat_idx: Optional[int]) -> Dict[str, Any]:
    pairs = ev["pairs"]
    # A WebSocket is only the transport if it ACTUALLY carried the conversation.
    # Pages routinely open sockets for analytics/personalization; picking those
    # produces a confidently-wrong config. Require evidence of real traffic, and
    # if we know the prompt we typed, require the socket to have carried it.
    known_prompt = (ev.get("prompt_sent") or "").strip()
    ws_live = []
    for w in (ev.get("ws_messages") or []):
        sent = w.get("sent") or []
        recv = w.get("received") or []
        if not sent and not recv:
            continue          # handshake only — not the chat channel
        if known_prompt and not any(known_prompt in str(f) for f in sent):
            continue          # this socket never carried our prompt
        ws_live.append(w)
    if ws_live:
        return {
            "value": "websocket", "confidence": 0.9,
            "evidence": f"{len(ws_live)} WebSocket channel(s) carried the conversation",
            "params": _ws_params({**ev, "ws_messages": ws_live}),
        }
    if chat_idx is None:
        return {"value": None, "confidence": 0.0, "evidence": "no chat pair found", "params": {}}

    p = pairs[chat_idx]
    req, resp = p["request"], p["response"]
    ct = (resp["content_type"] or "").lower()
    body = resp["raw_body"] or ""

    # Upgrade / 101 -> websocket (no captured frames but a handshake).
    if resp["status"] == 101 or req["headers"].get("upgrade", "").lower() == "websocket":
        return {"value": "websocket", "confidence": 0.75,
                "evidence": "HTTP 101 / Upgrade: websocket handshake",
                "params": _ws_params(ev)}

    if "text/event-stream" in ct or (body.lstrip().startswith("data:")):
        return {"value": "sse", "confidence": 0.9,
                "evidence": f"content-type={ct or 'n/a'}, SSE data: frames",
                "params": _http_params(req, resp, stream="sse")}

    # Sentinel-framed streams: MARKER_BEGIN{json}MARKER_END repeated in a text/plain body.
    sent = _detect_sentinel(body)
    if sent is not None:
        sent["params"] = {**_http_params(req, resp, stream=None), **sent["params"]}
        return sent

    if "ndjson" in ct or _looks_ndjson(body):
        return {"value": "ndjson", "confidence": 0.8,
                "evidence": f"content-type={ct or 'n/a'}, newline-delimited json",
                "params": _http_params(req, resp, stream="ndjson")}

    # poll: submit returns an id, a later GET on a URL containing that id returns a transcript.
    poll = _detect_poll(pairs, chat_idx)
    if poll is not None:
        return poll

    if "application/json" in ct or resp["json"] is not None:
        return {"value": "rest_json", "confidence": 0.85,
                "evidence": f"content-type={ct or 'n/a'}, single JSON body",
                "params": _http_params(req, resp, stream=None)}

    # Reachable-only-in-page style responses (html) => browser_dom, but we have no
    # dedicated generic-DOM adapter registered; flag low confidence.
    return {"value": "rest_json", "confidence": 0.3,
            "evidence": f"ambiguous content-type={ct or 'n/a'}; defaulting to rest_json",
            "params": _http_params(req, resp, stream=None)}


def _detect_sentinel(body: str) -> Optional[Dict[str, Any]]:
    """Detect a MARKER_BEGIN{json}MARKER_END framed body (sentinel_stream transport).

    Generic: finds a repeated <NAME>_BEGIN ... <NAME>_END pair wrapping JSON. Returns a
    transport classification with the discovered markers, or None.
    """
    if not body or "{" not in body:
        return None
    # The name must not run across a preceding `_END`. Real streams concatenate frames with no
    # separator (`..._ENDNAME_BEGIN`), and an unanchored match captured
    # `BOT_CHAT_EVENT_ENDBOT_CHAT_EVENT` as a second "name". Both candidates then had count 1, so
    # `max(set(...), key=count)` picked between them by SET ORDERING — the detector recognised the
    # same payload or not depending on hash order.
    names = re.findall(r"(?:^|[^A-Z0-9_])([A-Z][A-Z0-9_]{2,}?)_BEGIN", body)
    if not names:
        return None
    # Choose by what actually WORKS — the name whose markers bracket the most parseable JSON —
    # rather than by frequency, which cannot distinguish a real marker from a lucky substring.
    best = None
    for name in dict.fromkeys(names):
        begin, end = f"{name}_BEGIN", f"{name}_END"
        if end not in body:
            continue
        parsed = 0
        for f in re.findall(re.escape(begin) + r"(.*?)" + re.escape(end), body, re.S):
            try:
                json.loads(f.strip())
                parsed += 1
            except Exception:
                pass
        if parsed and (best is None or parsed > best[0]):
            best = (parsed, begin, end)
    if best is None:
        return None
    parsed, begin, end = best
    return {
        "value": "sentinel_stream",
        "confidence": 0.9,
        "evidence": f"{parsed} JSON frame(s) delimited by {begin}/{end}",
        "params": {"begin_marker": begin, "end_marker": end, "_sentinel": True},
    }


def _looks_ndjson(body: str) -> bool:
    lines = [l for l in (body or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    ok = 0
    for l in lines[:5]:
        try:
            json.loads(l)
            ok += 1
        except (ValueError, TypeError):
            return False
    return ok >= 2


def _http_params(req: Dict[str, Any], resp: Dict[str, Any], stream: Optional[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "endpoint": _strip_query(req["url"]),
        "method": req["method"],
        "headers": _nonsecret_headers(req["headers"]),
        "body": _body_template(req),
    }
    if stream:
        params["stream"] = {"format": stream}
    else:
        params["response_path"] = _guess_response_path(resp["json"], _REPLY_TEXT.get("v"))
    return params


def _ws_params(ev: Dict[str, Any]) -> Dict[str, Any]:
    url = ""
    framing = "text"
    for m in ev.get("ws_messages", []):
        url = m.get("url", url)
        data = m.get("data")
        if isinstance(data, str) and data.strip().startswith(("{", "[")):
            try:
                json.loads(data)
                framing = "json"
            except (ValueError, TypeError):
                pass
    if url.startswith("http"):
        url = "ws" + url[len("http"):]  # http->ws, https->wss
    return {"ws_url": url, "framing": framing,
            "send_template": {"type": "message", "text": "{{PROMPT}}"},
            "idle_ms": 1500}


def _detect_poll(pairs: List[Dict[str, Any]], chat_idx: int) -> Optional[Dict[str, Any]]:
    submit = pairs[chat_idx]
    sid = _first_id(submit["response"]["json"])
    if not sid:
        return None
    for j in range(chat_idx + 1, len(pairs)):
        later = pairs[j]
        if later["request"]["method"] == "GET" and str(sid) in later["request"]["url"]:
            return {"value": "poll", "confidence": 0.7,
                    "evidence": f"submit returned id={sid!r}; GET {(_strip_query(later['request']['url']))} polls it",
                    "params": {
                        "submit": {"endpoint": _strip_query(submit["request"]["url"]),
                                   "method": submit["request"]["method"],
                                   "body": _body_template(submit["request"])},
                        "poll": {"endpoint_template": _strip_query(later["request"]["url"]).replace(str(sid), "{{ID}}"),
                                 "id_field": _id_field_of(submit["response"]["json"], sid)},
                        "response_path": _guess_response_path(later["response"]["json"], _REPLY_TEXT.get("v")),
                    }}
    return None


# --------------------------------------------------------------------------- #
# Layer 2 — auth                                                              #
# --------------------------------------------------------------------------- #
def classify_auth(ev: Dict[str, Any], chat_idx: Optional[int]) -> Dict[str, Any]:
    if chat_idx is None:
        return {"value": "none", "confidence": 0.2, "evidence": "no chat pair", "params": {}}
    pairs = ev["pairs"]
    req = pairs[chat_idx]["request"]
    headers = req["headers"]
    query = req["query"]

    # Values produced by earlier responses (login/token/csrf), for reuse detection.
    prior_values = _collect_prior_values(pairs, chat_idx)

    # 1) Authorization header.
    authz = headers.get("authorization")
    if authz:
        low = authz.lower()
        if low.startswith("bearer "):
            token = authz.split(" ", 1)[1]
            origin = _reuse_origin(token, prior_values)
            if origin is not None:
                oi, ofield, ourl = origin
                if _looks_token_endpoint(ourl) and _has_access_token(pairs[oi]["response"]["json"]):
                    return _auth_oauth2(pairs[oi], ourl)
                return _auth_derived(pairs, chat_idx, oi, ofield, ourl)
            return {"value": "static", "confidence": 0.85,
                    "evidence": "constant 'Authorization: Bearer' on chat request",
                    "params": {"mode": "bearer", "name": "Authorization",
                               "value_ref": "env:DISCOVERED_TOKEN"}}
        if low.startswith("basic "):
            return {"value": "static", "confidence": 0.85,
                    "evidence": "HTTP Basic auth on chat request",
                    "params": {"mode": "basic", "username_ref": "env:BASIC_USER",
                               "password_ref": "env:BASIC_PASS"}}
        # custom scheme
        return {"value": "static", "confidence": 0.6,
                "evidence": f"custom Authorization scheme {authz.split(' ',1)[0]!r}",
                "params": {"mode": "custom", "name": "Authorization",
                           "template": authz.split(" ", 1)[0] + " {{VALUE}}",
                           "value_ref": "env:DISCOVERED_TOKEN"}}

    # 2) CSRF header echoed from a prior bootstrap.
    for h in _CSRF_HEADERS:
        if h in headers:
            origin = _reuse_origin(headers[h], prior_values)
            if origin is not None:
                oi, ofield, ourl = origin
                return {"value": "csrf", "confidence": 0.8,
                        "evidence": f"'{h}' echoes a token from GET {_strip_query(ourl)}",
                        "params": {"bootstrap_url": _strip_query(ourl),
                                   "extract": {"path": ofield} if ofield else {"regex": "TOKEN=([A-Za-z0-9_-]+)"},
                                   "into_header": _orig_header_name(pairs[chat_idx], h)}}
            return {"value": "csrf", "confidence": 0.5,
                    "evidence": f"CSRF-style header '{h}' present (origin not in capture)",
                    "params": {"bootstrap_url": "", "extract": {},
                               "into_header": _orig_header_name(pairs[chat_idx], h)}}

    # 3) API-key style headers.
    for name_lower, value in headers.items():
        if name_lower in _SECRET_HEADERS and name_lower not in ("authorization", "cookie"):
            return {"value": "static", "confidence": 0.8,
                    "evidence": f"API-key header '{name_lower}' on chat request",
                    "params": {"mode": "api_key", "in": "header",
                               "name": _orig_header_name(pairs[chat_idx], name_lower),
                               "value_ref": "env:DISCOVERED_API_KEY"}}

    # 4) API-key in query string.
    for qn in ("api_key", "apikey", "key", "token", "access_token"):
        if qn in query:
            return {"value": "static", "confidence": 0.7,
                    "evidence": f"API-key query param '{qn}'",
                    "params": {"mode": "api_key", "in": "query", "name": qn,
                               "value_ref": "env:DISCOVERED_API_KEY"}}

    # 5) Cookie session (possibly derived from a login).
    if "cookie" in headers:
        origin = _reuse_origin(headers["cookie"], prior_values, substring=True)
        if origin is not None:
            oi, ofield, ourl = origin
            return _auth_derived(pairs, chat_idx, oi, ofield, ourl, kind_hint="cookie")
        return {"value": "static", "confidence": 0.6,
                "evidence": "session Cookie on chat request",
                "params": {"mode": "cookie", "name": _cookie_name(headers["cookie"]),
                           "value_ref": "env:DISCOVERED_COOKIE"}}

    return {"value": "none", "confidence": 0.8,
            "evidence": "no secret observed on the chat request", "params": {}}


def _auth_oauth2(login_pair: Dict[str, Any], url: str) -> Dict[str, Any]:
    return {"value": "oauth2", "confidence": 0.75,
            "evidence": f"token endpoint {_strip_query(url)} precedes chat; access_token reused downstream",
            "params": {"grant": "client_credentials", "token_url": _strip_query(url),
                       "client_id_ref": "env:OAUTH_CLIENT_ID",
                       "client_secret_ref": "env:OAUTH_CLIENT_SECRET"}}


def _auth_derived(pairs: List[Dict[str, Any]], chat_idx: int, origin_idx: int,
                  field: Optional[str], url: str, kind_hint: str = "bearer") -> Dict[str, Any]:
    login = pairs[origin_idx]["request"]
    step = {
        "method": login["method"],
        "url": _strip_query(login["url"]),
        "extract": [{"path": field, "var": "AUTH_VALUE"}] if field
                   else [{"regex": "\"[^\"]*token[^\"]*\"\\s*:\\s*\"([^\"]+)\"", "var": "AUTH_VALUE"}],
    }
    if login.get("json") is not None:
        step["json"] = login["json"]
    attach_header = "Cookie" if kind_hint == "cookie" else "Authorization"
    attach_val = "{{AUTH_VALUE}}" if kind_hint == "cookie" else "Bearer {{AUTH_VALUE}}"
    return {"value": "derived_multihop", "confidence": 0.7,
            "evidence": f"value from {login['method']} {_strip_query(login['url'])} reappears on the chat request",
            "params": {"steps": [step], "attach": {"headers": {attach_header: attach_val}},
                       "inputs": {}}}


# --------------------------------------------------------------------------- #
# Layer 3 — auth lifecycle                                                    #
# --------------------------------------------------------------------------- #
def classify_auth_lifecycle(ev: Dict[str, Any], chat_idx: Optional[int],
                            auth: Dict[str, Any]) -> Dict[str, Any]:
    pairs = ev["pairs"]

    # 401/403 -> re-auth -> retry pattern.
    for i, p in enumerate(pairs):
        if p["response"]["status"] in (401, 403):
            # a later successful request to the same endpoint = reauth+retry
            for j in range(i + 1, len(pairs)):
                if _same_endpoint(pairs[j]["request"], p["request"]) and pairs[j]["response"]["status"] < 400:
                    return {"value": "reauth_on_401", "confidence": 0.75,
                            "evidence": f"{p['response']['status']} then a successful retry on the same endpoint",
                            "params": {"challenge_statuses": [p["response"]["status"]]}}
            return {"value": "reauth_on_401", "confidence": 0.55,
                    "evidence": f"{p['response']['status']} challenge observed",
                    "params": {"challenge_statuses": [p["response"]["status"]]}}

    # JWT exp on the bearer token.
    if chat_idx is not None:
        authz = pairs[chat_idx]["request"]["headers"].get("authorization", "")
        if authz.lower().startswith("bearer "):
            claims = _jwt_claims(authz.split(" ", 1)[1])
            if claims and "exp" in claims:
                ttl = None
                if "iat" in claims:
                    ttl = int(claims["exp"]) - int(claims["iat"])
                return {"value": "refresh_on_ttl", "confidence": 0.7,
                        "evidence": f"bearer token is a JWT with exp{' (ttl≈%ss)' % ttl if ttl else ''}",
                        "params": {"ttl_s": ttl} if ttl else {}}

    # Set-Cookie churn -> cookie rotation.
    cookie_setters = sum(1 for p in pairs if any(
        k == "set-cookie" for k in p["response"]["headers"]))
    if cookie_setters >= 2:
        return {"value": "cookie_rotation", "confidence": 0.55,
                "evidence": f"Set-Cookie observed on {cookie_setters} responses",
                "params": {}}

    if auth.get("value") in ("oauth2", "csrf", "derived_multihop"):
        return {"value": "reauth_on_401", "confidence": 0.4,
                "evidence": "dynamic auth without an observed challenge; default to reauth_on_401",
                "params": {"challenge_statuses": [401]}}

    return {"value": "static", "confidence": 0.7,
            "evidence": "no expiry/challenge/cookie-churn observed", "params": {}}


# --------------------------------------------------------------------------- #
# Layer 4 — session / conversation                                            #
# --------------------------------------------------------------------------- #
def _host_of(url: str) -> str:
    """Hostname of a URL, lowercased. '' when it cannot be parsed."""
    try:
        from urllib.parse import urlsplit
        return (urlsplit(str(url)).netloc or "").lower()
    except Exception:
        return ""


def classify_session(ev: Dict[str, Any], chat_idx: Optional[int]) -> Dict[str, Any]:
    pairs = ev["pairs"]
    if chat_idx is None:
        return {"value": "stateless", "confidence": 0.3, "evidence": "no chat pair", "params": {}}

    # id-flow: an id produced by an earlier response reappears in a later request.
    #
    # Constrained to the CHAT endpoint's own host and to ids the chat call actually uses. Without
    # that, a real page's third-party traffic supplies the match by coincidence: on one live site
    # an Adobe Target (omtrdc.net) response id happened to recur later, so the classifier declared
    # the chat needed a session created at an analytics vendor and validation died there. A session
    # the chat request never uses is not the chat's session.
    chat_req = pairs[chat_idx]["request"]
    chat_host = _host_of(chat_req["url"])
    for i, p in enumerate(pairs):
        if _host_of(p["request"]["url"]) != chat_host:
            continue                      # a session for THIS chat comes from THIS service
        rid, rfield = _first_id(p["response"]["json"]), None
        if not rid:
            continue
        rfield = _id_field_of(p["response"]["json"], rid)
        for j in range(i + 1, len(pairs)):
            later = pairs[j]["request"]
            if _host_of(later["url"]) != chat_host:
                continue
            in_url = str(rid) in later["url"]
            in_body = str(rid) in (later["raw_body"] or "")
            if in_url or in_body:
                if in_url and re.search(rf"/[^/]*/{re.escape(str(rid))}(/|$)", later["url"]):
                    return {"value": "create_conversation", "confidence": 0.8,
                            "evidence": f"id {rfield}={rid!r} from step {i} appears in the URL path of step {j}",
                            "params": {"create_req": {"endpoint": _strip_query(p["request"]["url"]),
                                                      "method": p["request"]["method"],
                                                      "body": _body_template(p["request"])},
                                       "id_field": rfield,
                                       "send_url_template": later["url"].replace(str(rid), "{{SESSION_ID}}")}}
                return {"value": "create_session", "confidence": 0.75,
                        "evidence": f"id {rfield}={rid!r} from step {i} injected into step {j}'s body",
                        "params": {"session_endpoint": _strip_query(p["request"]["url"]),
                                   "session_extract": rfield,
                                   "message_endpoint": _strip_query(later["url"]),
                                   "message_body": _body_template(later)}}

    # warmup: an early greeting turn distinct from the scored prompt.
    chat_prompt = _request_has_prompt(pairs[chat_idx]["request"])
    for i, p in enumerate(pairs):
        if i == chat_idx:
            continue
        pr = _request_has_prompt(p["request"])
        if pr and pr.strip().lower() in _GREETINGS and pr != chat_prompt:
            return {"value": "warmup", "confidence": 0.45,
                    "evidence": f"greeting turn {pr!r} precedes the scored prompt",
                    "params": {"warmup_message": pr}}

    # multiple turns to the same chat endpoint => multi_turn context server-side.
    same = sum(1 for p in pairs
               if _same_endpoint(p["request"], pairs[chat_idx]["request"])
               and _request_has_prompt(p["request"]))
    if same >= 2:
        return {"value": "multi_turn", "confidence": 0.55,
                "evidence": f"{same} prompt turns to the same endpoint (context held server-side)",
                "params": {}}

    return {"value": "stateless", "confidence": 0.7,
            "evidence": "each request independent; no id-flow or warmup", "params": {}}


# --------------------------------------------------------------------------- #
# Layer 5 — identity                                                          #
# --------------------------------------------------------------------------- #
def classify_identity(ev: Dict[str, Any], chat_idx: Optional[int]) -> Dict[str, Any]:
    pairs = ev["pairs"]
    # Per-user rate-limit hints or 429s suggest rotation *may* be warranted, but
    # identity is an operator decision, so default to fixed and note the hints.
    hints = []
    for p in pairs:
        rh = p["response"]["headers"]
        if any(k.startswith("x-ratelimit") or k == "ratelimit-remaining" for k in rh):
            hints.append("per-response rate-limit headers")
            break
    if any(p["response"]["status"] == 429 for p in pairs):
        hints.append("HTTP 429 observed")

    if hints:
        return {"value": "fixed", "confidence": 0.4,
                "evidence": "identity is an operator choice; rotation may help (" + "; ".join(sorted(set(hints))) + ")",
                "params": {"mode": "fixed", "per_user_ratelimit": True}}
    return {"value": "fixed", "confidence": 0.5,
            "evidence": "identity is an operator choice; defaulting to a single fixed identity",
            "params": {"mode": "fixed"}}


# --------------------------------------------------------------------------- #
# Layer 6 — rate / concurrency                                                #
# --------------------------------------------------------------------------- #
def classify_rate(ev: Dict[str, Any], session: Dict[str, Any]) -> Dict[str, Any]:
    pairs = ev["pairs"]
    stateful = session.get("value") in (
        "create_session", "create_conversation", "warmup", "multi_turn")
    max_workers = 1 if stateful else 10

    times = [p["started_ms"] for p in pairs if p.get("started_ms") is not None]
    times = sorted(times)
    qpm: Optional[int] = None
    evidence = "no request timing in capture; qpm left unset"
    if len(times) >= 2:
        gaps = [t2 - t1 for t1, t2 in zip(times, times[1:]) if t2 > t1]
        if gaps:
            median_gap = statistics.median(gaps)
            if median_gap > 0:
                qpm = max(1, int(60000.0 / median_gap))
                evidence = f"median inter-request gap {median_gap:.0f}ms -> ~{qpm} qpm observed"
    conf = 0.6 if qpm is not None else 0.5
    return {"value": "rate", "confidence": conf, "evidence": evidence,
            "params": {"qpm": qpm, "max_workers": max_workers}}


# --------------------------------------------------------------------------- #
# Compose                                                                      #
# --------------------------------------------------------------------------- #
# Host/URL substrings that map to a purpose-built preset adapter (integration
# TYPES, not customer names).
_PRESET_HOST_HINTS = (
    ("salesforce-scrt", "scrt2_direct"),
    ("einstein/ai-agent", "agentforce"),
    ("directline", "copilot_studio"),
    ("powerplatform", "copilot_studio"),
    ("direct.botframework", "copilot_studio"),
    ("slack.com", "slack_direct"),
    ("reasoningengines", "vertex_ai"),
    (":streamquery", "vertex_ai"),
    ("connectparticipant", "amazon_connect"),
)


def _preset_for_url(url: str) -> Optional[str]:
    low = (url or "").lower()
    for hint, adapter in _PRESET_HOST_HINTS:
        if hint in low:
            return adapter
    return None


def compose(classified: Dict[str, Any]) -> Dict[str, Any]:
    """Fold the six classified layers into a runnable adapter config.

    Chooses the closest existing adapter for the detected transport (honouring
    platform host hints), then attaches the auth/identity/lifecycle/rate blocks.
    Secrets are referenced via ``env:`` placeholders, never inlined.
    """
    layers = classified["layers"] if "layers" in classified else classified
    transport = layers["transport"]
    auth = layers["auth"]
    lifecycle = layers["auth_lifecycle"]
    session = layers["session"]
    identity = layers["identity"]
    rate = layers["rate"]

    tp = transport.get("value")
    tparams = transport.get("params", {})
    endpoint = tparams.get("endpoint", "")

    # Preset host override (e.g. salesforce/slack/vertex) takes precedence.
    adapter = _preset_for_url(endpoint)

    config: Dict[str, Any] = {}
    if adapter is None:
        if tp == "sse":
            adapter = "sse_stream"
            base, path = _split_base_path(endpoint)
            config.update({"base_url": base, "chat_path": path,
                           "request_template": tparams.get("body", {"message": "{{PROMPT}}"}),
                           "stream": tparams.get("stream", {"format": "sse"})})
        elif tp == "ndjson":
            adapter = "sse_stream"
            base, path = _split_base_path(endpoint)
            stream = dict(tparams.get("stream", {})); stream["format"] = "ndjson"
            config.update({"base_url": base, "chat_path": path,
                           "request_template": tparams.get("body", {"message": "{{PROMPT}}"}),
                           "stream": stream})
        elif tp == "websocket":
            adapter = "websocket_direct"
            config.update({"ws_url": tparams.get("ws_url", ""),
                           "send_template": tparams.get("send_template", {"type": "message", "text": "{{PROMPT}}"}),
                           "idle_ms": tparams.get("idle_ms", 1500)})
            if tparams.get("framing"):
                config["framing"] = tparams["framing"]
        elif tp == "sentinel_stream":
            adapter = "sentinel_stream"
            config.update({
                "url": endpoint,
                "method": tparams.get("method", "POST"),
                "begin_marker": tparams.get("begin_marker", "BOT_CHAT_EVENT_BEGIN"),
                "end_marker": tparams.get("end_marker", "BOT_CHAT_EVENT_END"),
                "message": {"body": tparams.get("body", {"message": "{{PROMPT}}"})},
            })
        elif tp == "poll":
            # Generic watermark/transcript polling (create -> send -> GET-poll).
            adapter = "session_poll"
            config.update(_session_poll_from_poll(tparams))
        elif session.get("value") in ("create_session", "create_conversation"):
            adapter = "session_api"
            config.update(_session_api_from_session(session, tparams))
        else:  # rest_json (default)
            adapter = "direct_api"
            config.update({"endpoint": endpoint, "method": tparams.get("method", "POST"),
                           "body": tparams.get("body", {"prompt": "{{PROMPT}}"}),
                           "response_path": tparams.get("response_path", "response")})
        # Session upgrade for a rest_json transport that actually has id-flow.
        if adapter == "direct_api" and session.get("value") in ("create_session", "create_conversation"):
            adapter = "session_api"
            config = _session_api_from_session(session, tparams)
    else:
        # Preset adapter: keep endpoint hints; operator fills preset-specific keys.
        config["_preset_endpoint"] = endpoint

    # Non-secret request headers.
    if tparams.get("headers"):
        config.setdefault("headers", {}).update(tparams["headers"])

    # Warmup preset support.
    if session.get("value") == "warmup":
        config["warmup_message"] = session["params"].get("warmup_message", "Hello")

    # Layer blocks (auth secrets always via env refs).
    if auth.get("value") and auth["value"] != "none":
        config["auth"] = _auth_block(auth)
    config["auth_lifecycle"] = _lifecycle_block(lifecycle)
    config["identity"] = {"mode": identity.get("params", {}).get("mode", "fixed")}

    # Rate / concurrency.
    rparams = rate.get("params", {})
    if rparams.get("qpm"):
        config["qpm"] = rparams["qpm"]
    # Only pin max_workers when the capture actually justified it. Writing a default of
    # 10 here overrode the relay's stateful=1 safety rule (recommended_workers knows the
    # full STATEFUL_ADAPTERS set, which classify_rate does not), so a discovered websocket/
    # sentinel/session_poll config would run 10 concurrent conversations and corrupt every
    # multi-turn chain. Leave it unset and let recommended_workers() decide.
    if "max_workers" in rparams and rparams["max_workers"] == 1:
        config["max_workers"] = 1

    config["adapter"] = adapter
    config["_discovery"] = {name: {"value": layers[name]["value"],
                                   "confidence": round(layers[name]["confidence"], 3)}
                            for name in LAYER_NAMES}
    return config


def _auth_block(auth: Dict[str, Any]) -> Dict[str, Any]:
    value, params = auth["value"], dict(auth.get("params", {}))
    block: Dict[str, Any] = {"type": value}
    block.update(params)
    return block


def _lifecycle_block(lifecycle: Dict[str, Any]) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": lifecycle["value"]}
    block.update(lifecycle.get("params", {}))
    return block


def _session_api_from_session(session: Dict[str, Any], tparams: Dict[str, Any]) -> Dict[str, Any]:
    p = session.get("params", {})
    if session["value"] == "create_conversation":
        create = p.get("create_req", {})
        return {
            "session_endpoint": create.get("endpoint", ""),
            "session_body": create.get("body", {}),
            "session_extract": p.get("id_field", "id"),
            "session_variable": "SESSION_ID",
            "message_endpoint": p.get("send_url_template", ""),
            "message_body": tparams.get("body", {"message": "{{PROMPT}}"}),
            "response_path": tparams.get("response_path", "messages.0.message"),
        }
    return {
        "session_endpoint": p.get("session_endpoint", ""),
        "session_extract": p.get("session_extract", "sessionId"),
        "session_variable": "SESSION_ID",
        "message_endpoint": p.get("message_endpoint", ""),
        "message_body": p.get("message_body", tparams.get("body", {"message": "{{PROMPT}}"})),
        "response_path": tparams.get("response_path", "messages.0.message"),
    }


def _session_poll_from_poll(tparams: Dict[str, Any]) -> Dict[str, Any]:
    """Build a session_poll config from a detected create/submit + fetch pattern."""
    return {
        "create": {"url": tparams.get("create_url", tparams.get("endpoint", "")),
                   "method": tparams.get("create_method", "POST"),
                   "body": tparams.get("create_body", {}),
                   "extract": tparams.get("id_path", "conversation_id")},
        "send": {"url": tparams.get("send_url", tparams.get("endpoint", "")),
                 "method": tparams.get("method", "POST"),
                 "body": tparams.get("body", {"message": "{{PROMPT}}"})},
        "poll": {"url": tparams.get("poll_url", ""),
                 "method": tparams.get("poll_method", "GET"),
                 "list_path": tparams.get("list_path", "messages"),
                 "role_field": tparams.get("role_field", "role"),
                 "bot_roles": tparams.get("bot_roles", ["assistant", "bot", "agent"]),
                 "text_path": tparams.get("text_path", "text"),
                 "interval_ms": 1000, "timeout_ms": 30000},
    }


def _session_api_from_poll(tparams: Dict[str, Any]) -> Dict[str, Any]:
    submit = tparams.get("submit", {})
    poll = tparams.get("poll", {})
    return {
        "session_endpoint": submit.get("endpoint", ""),
        "session_body": submit.get("body", {}),
        "session_extract": poll.get("id_field", "id"),
        "session_variable": "ID",
        "message_endpoint": poll.get("endpoint_template", ""),
        "message_body": {},
        "response_path": tparams.get("response_path", "response"),
        "_note": "poll transport approximated via session_api (submit + fetch); verify polling semantics",
    }


# --------------------------------------------------------------------------- #
# Top-level entry point                                                        #
# --------------------------------------------------------------------------- #
def classify_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Run every layer classifier, compose a config, and report resolution.

    Returns::

        {
          "layers": {<name>: {value, params, confidence, evidence}, ...},
          "config": {...runnable adapter config...},
          "overall_confidence": float,   # the weakest layer's confidence
          "unresolved": [<layer names below the confidence floor / unvalued>],
        }
    """
    ev = _evidence(evidence)
    chat_idx = _pick_chat_index(ev["pairs"], (evidence or {}).get("prompt_sent"))

    transport = classify_transport(ev, chat_idx)
    auth = classify_auth(ev, chat_idx)
    lifecycle = classify_auth_lifecycle(ev, chat_idx, auth)
    session = classify_session(ev, chat_idx)
    identity = classify_identity(ev, chat_idx)
    rate = classify_rate(ev, session)

    layers = {
        "transport": transport, "auth": auth, "auth_lifecycle": lifecycle,
        "session": session, "identity": identity, "rate": rate,
    }
    config = compose({"layers": layers})

    unresolved = [name for name in LAYER_NAMES
                  if layers[name]["value"] is None or layers[name]["confidence"] < LOW_CONF]
    overall = min((layers[n]["confidence"] for n in LAYER_NAMES), default=0.0)
    return {
        "layers": layers,
        "config": config,
        "overall_confidence": round(overall, 3),
        "unresolved": unresolved,
        "chat_pair_index": chat_idx,
    }


# --------------------------------------------------------------------------- #
# Small shared helpers                                                         #
# --------------------------------------------------------------------------- #
def _nonsecret_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Keep request headers that are safe to bake into a config (drop secrets)."""
    keep = {}
    drop = _SECRET_HEADERS | _CSRF_HEADERS | {
        "content-length", "host", "connection", "accept-encoding",
    }
    for k, v in headers.items():
        if k in drop or k.startswith(":"):     # ':authority' etc. — HTTP/2 internals
            continue
        # Preserve a canonical-ish casing for a couple of common headers.
        keep[_canonical_header(k)] = v
    return keep


def _canonical_header(name_lower: str) -> str:
    special = {"content-type": "Content-Type", "user-agent": "User-Agent",
               "accept": "Accept", "accept-language": "Accept-Language"}
    return special.get(name_lower, "-".join(w.capitalize() for w in name_lower.split("-")))


def _orig_header_name(pair: Dict[str, Any], lower: str) -> str:
    # We stored headers lowercased; return a canonicalized display name.
    return _canonical_header(lower)


def _body_template(req: Dict[str, Any]) -> Any:
    """Turn a captured request body into a template with ``{{PROMPT}}``."""
    body = req.get("json")
    prompt = _request_has_prompt(req)
    if isinstance(body, (dict, list)):
        if prompt is not None:
            replaced = json.loads(json.dumps(body).replace(json.dumps(prompt)[1:-1], "{{PROMPT}}"))
            return replaced
        return body
    if isinstance(body, str) and body:
        return "{{PROMPT}}"
    return {"prompt": "{{PROMPT}}"}


def _paths_to_strings(obj: Any, prefix: str = "") -> List[Tuple[str, str]]:
    """Every (dot_path, string_value) in a nested JSON structure."""
    out: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_paths_to_strings(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_paths_to_strings(v, f"{prefix}.{i}" if prefix else str(i)))
    elif isinstance(obj, str):
        out.append((prefix, obj))
    return out


def _guess_response_path(resp_json: Any, reply_text: Optional[str] = None) -> str:
    """Dot-path to the answer text in a JSON response body.

    GROUND TRUTH FIRST: if the capture read the bot's reply off the page, the correct
    path is the one whose value matches it. Otherwise fall back to well-known keys, then
    to the longest string ANYWHERE in the body (not just at the top level — answers are
    usually nested, e.g. data.answer, while short status flags sit on top).
    """
    if not isinstance(resp_json, (dict, list)):
        return "response"

    candidates = _paths_to_strings(resp_json)

    if reply_text:
        needle = " ".join(reply_text.split())[:120].strip()
        core = needle.split(":", 1)[-1].strip() if ":" in needle[:20] else needle
        best = None
        for path, val in candidates:
            v = " ".join(val.split())
            if not v:
                continue
            if v in needle or needle in v or (core and (core in v or v in core)):
                if best is None or len(v) > len(best[1]):
                    best = (path, v)
        if best:
            return best[0]

    for path in _RESPONSE_PATH_GUESSES:
        val = _dot(resp_json, path)
        if isinstance(val, str) and val.strip():
            return path

    # Longest string anywhere beats longest top-level string.
    real = [(p, v) for p, v in candidates if v.strip()]
    if real:
        return max(real, key=lambda pv: len(pv[1]))[0]
    return "response"


def _first_id(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for f in _ID_FIELDS:
            v = obj.get(f)
            if isinstance(v, (str, int)) and str(v):
                return str(v)
        for v in obj.values():
            r = _first_id(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _first_id(v)
            if r:
                return r
    return None


def _id_field_of(obj: Any, target: str) -> Optional[str]:
    """Return the (dot-)field name whose value equals ``target``."""
    def walk(o: Any, prefix: str) -> Optional[str]:
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (str, int)) and str(v) == target:
                    return f"{prefix}{k}"
                r = walk(v, f"{prefix}{k}.")
                if r:
                    return r
        elif isinstance(o, list):
            for i, v in enumerate(o):
                r = walk(v, f"{prefix}{i}.")
                if r:
                    return r
        return None
    return walk(obj, "")


def _collect_prior_values(pairs: List[Dict[str, Any]], chat_idx: int) -> List[Tuple[int, str, str, str]]:
    """Collect (index, field, value, url) strings from responses before chat_idx."""
    out: List[Tuple[int, str, str, str]] = []
    for i in range(chat_idx):
        rj = pairs[i]["response"]["json"]
        url = pairs[i]["request"]["url"]
        for field, val in _iter_string_leaves(rj):
            if len(val) >= 8:
                out.append((i, field, val, url))
    return out


def _iter_string_leaves(obj: Any, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_string_leaves(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_string_leaves(v, f"{prefix}{i}.")
    elif isinstance(obj, str):
        yield (prefix.rstrip("."), obj)


def _reuse_origin(needle: str, prior: List[Tuple[int, str, str, str]],
                  substring: bool = False) -> Optional[Tuple[int, Optional[str], str]]:
    """Find the earliest prior response value that equals/appears-in ``needle``."""
    for idx, field, val, url in prior:
        if (val in needle) if substring else (val == needle or val in needle):
            return (idx, field, url)
    return None


def _looks_token_endpoint(url: str) -> bool:
    low = (url or "").lower()
    return any(s in low for s in ("/oauth", "/token", "/auth/token", "/connect/token", "/authorize"))


def _has_access_token(resp_json: Any) -> bool:
    return isinstance(resp_json, dict) and ("access_token" in resp_json or "token" in resp_json)


def _same_endpoint(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a.get("method") == b.get("method") and _strip_query(a.get("url", "")) == _strip_query(b.get("url", ""))


def _cookie_name(cookie_header: str) -> str:
    first = cookie_header.split(";", 1)[0]
    return first.split("=", 1)[0].strip() if "=" in first else "session"


def _split_base_path(endpoint: str) -> Tuple[str, str]:
    from urllib.parse import urlparse
    u = urlparse(endpoint)
    base = f"{u.scheme}://{u.netloc}" if u.scheme else ""
    path = u.path or "/"
    return base, path


def _jwt_claims(token: str) -> Optional[Dict[str, Any]]:
    import base64
    if not token or token.count(".") != 2:
        return None
    seg = token.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:  # noqa: BLE001 - malformed token payload
        return None


def _dot(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
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
