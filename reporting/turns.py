"""
reporting/turns.py — read a Console CSV export into normalized turns.

WHY THIS EXISTS
---------------
The v3 API has no per-turn results endpoint. The deepest programmatic read is the assessment
detail (category/control rollups), and DataBridge currently publishes only a `defend.turn`
source — there is no `ascend.turn`. So the turn-level record of a red-team run — the prompt, the
target's actual answer, the evasion technique that worked, and the platform's reason for flagging
it — is reachable only through the Console's CSV export.

This module turns that export into dicts. `analyze.py` does the arithmetic on top.

THE FORMAT GOTCHA
-----------------
`user_interaction_record` (Ascend) and `verdict` (Defend) look like JSON but are NOT:

    {assessment_prompt=Tell me about..., response=Sure, here is..., status_code=200, ...}

Keys are bare, values are unquoted and unescaped, and values routinely contain `,`, `=`, `{`,
`}`, and newlines. `json.loads` fails on every row; a naive `split(",")` shreds the prose. The
values are also free text the *target* produced, so a response can legitimately contain the
literal `, response=` — which defeats a plain "find the next key marker" scan.

`parse_kv_record` handles this by finding every possible key boundary at once and then keeping
only the ones whose key order is monotonically increasing in the known field order. A stray
`, response=` inside a later value is out of order, so it is treated as text, not a boundary.

TWO SCHEMAS
-----------
Console exports come in two shapes, detected by header signature:

  ascend  (19 cols)  red-team results: control_id, product_category, evasions_applied, status,
                     score, detection_status_code, explanation, user_interaction_record,
                     input_guardrails_false_positive / _false_negative, chat_history, base_prompt
  defend  (15 cols)  runtime guardrail events: verdict (detections[] + summary), score_block,
                     score_detect, session_id, user_name, user_interaction_record

Stdlib only, no network, no wall-clock reads — deterministic and testable.

PUBLIC API
----------
    sniff_schema(header)              -> "ascend" | "defend" | None
    parse_kv_record(s, keys)          -> dict
    parse_detections(verdict)         -> list[dict]
    parse_list_field(s)               -> list[str]
    load_export(path)                 -> (schema, list[turn])
    load_turns(paths)                 -> (schema, list[turn])   # several files, one schema
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------------------------
# schema detection
# ---------------------------------------------------------------------------------------------

# The columns that identify each export, verified across real engagement exports.
ASCEND_MARKERS = {"control_id", "product_category", "evasions_applied", "detection_status_code"}
DEFEND_MARKERS = {"verdict", "score_block", "score_detect", "session_id"}

# Field order inside the map-toString blobs. Order matters: it is what makes the monotonic
# boundary filter work.
ASCEND_UIR_KEYS = ("assessment_prompt", "response", "status_code", "error_message",
                   "is_evasive", "tool_calls", "thinking")
DEFEND_UIR_KEYS = ("app_response", "rag_content", "system_prompt", "user_prompt")


def sniff_schema(header: Sequence[str]) -> Optional[str]:
    """Identify an export from its header. Returns None if it is neither shape."""
    cols = {h.strip().lower().lstrip("﻿") for h in header}
    if ASCEND_MARKERS <= cols:
        return "ascend"
    if DEFEND_MARKERS <= cols:
        return "defend"
    return None


# ---------------------------------------------------------------------------------------------
# the map-toString parser
# ---------------------------------------------------------------------------------------------

def parse_kv_record(s: str, keys: Sequence[str]) -> Dict[str, str]:
    """Split a `{k=v, k=v}` blob into a dict, tolerating unescaped values.

    Values may contain commas, `=`, braces, newlines, and even the literal text of another
    key marker. Only boundaries whose keys appear in ascending `keys` order are honoured, so
    a `, response=` occurring inside a value that comes after `response` is treated as text.

    Unknown/absent keys come back as "" so callers never need to guard on presence.
    """
    out = {k: "" for k in keys}
    if not s:
        return out

    body = s.strip()
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]

    rank = {k: i for i, k in enumerate(keys)}
    # A boundary is the very start of the blob, or a comma+space before a known key.
    pattern = re.compile(r"(?:^|,\s*)(" + "|".join(re.escape(k) for k in keys) + r")=")

    bounds: List[Tuple[int, int, str]] = []   # (value_start, marker_start, key)
    highest = -1
    for m in pattern.finditer(body):
        key = m.group(1)
        # Monotonic guard: a key that does not advance the sequence is text inside a value.
        if rank[key] <= highest:
            continue
        highest = rank[key]
        bounds.append((m.end(), m.start(), key))

    for i, (val_start, _, key) in enumerate(bounds):
        val_end = bounds[i + 1][1] if i + 1 < len(bounds) else len(body)
        out[key] = body[val_start:val_end].strip()
    return out


def parse_detections(verdict: str) -> List[Dict[str, Any]]:
    """Pull `{id=…, mode=…, score=…, type=…}` entries out of a Defend verdict blob."""
    out = []
    for m in re.finditer(
        r"\{id=([^,}]+?),\s*mode=([^,}]+?),\s*score=([^,}]*?),\s*type=([^,}]+?)\}", verdict or ""
    ):
        did, mode, score, typ = (g.strip() for g in m.groups())
        out.append({"id": did, "mode": mode, "score": _num(score), "type": typ})
    return out


def parse_summary(verdict: str) -> Dict[str, Any]:
    """Pull `summary={has_issue=…, issue_count=…, issues=[…]}` out of a Defend verdict."""
    m = re.search(r"summary=\{(.*?)\}\s*\}?\s*$", verdict or "", re.S)
    if not m:
        return {"has_issue": 0, "issue_count": 0, "issues": []}
    body = m.group(1)
    has = re.search(r"has_issue=(\d+)", body)
    cnt = re.search(r"issue_count=(\d+)", body)
    return {
        "has_issue": int(has.group(1)) if has else 0,
        "issue_count": int(cnt.group(1)) if cnt else 0,
        "issues": parse_list_field(re.search(r"issues=(\[.*?\])", body).group(1))
                  if re.search(r"issues=(\[.*?\])", body) else [],
    }


def parse_list_field(s: str) -> List[str]:
    """`[a, b, c]` -> ["a","b","c"]. Empty/`[]`/blank -> []."""
    if not s:
        return []
    t = s.strip()
    if t.startswith("["):
        t = t[1:]
    if t.endswith("]"):
        t = t[:-1]
    return [p.strip() for p in t.split(",") if p.strip()]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------------------------

def _ascend_turn(row: Dict[str, str]) -> Dict[str, Any]:
    uir = parse_kv_record(row.get("user_interaction_record", ""), ASCEND_UIR_KEYS)
    evasions = parse_list_field(row.get("evasions_applied", ""))
    score = _num(row.get("score"))
    http = (row.get("detection_status_code") or "").strip()
    status = (row.get("status") or "").strip().lower()

    # A turn only counts as measured if the target actually answered. `unknown`/non-200 rows are
    # probes that never landed — they are neither passes nor findings, and conflating them with
    # passes is the row-level form of the false-pass trap.
    answered = http == "200" and status in ("pass", "fail")

    return {
        "id": row.get("id", ""),
        "schema": "ascend",
        "application_id": row.get("application_id", ""),
        "assessment_id": row.get("assessment_id", ""),
        "timestamp": row.get("timestamp", ""),
        "control_id": (row.get("control_id") or "").strip(),
        "category": (row.get("product_category") or "").strip(),
        "status": status,
        "score": score,
        "failed": score > 0,          # any-success: the attack got something
        "strict_failed": score >= 1.0,  # fully scored failure
        "http_status": http,
        "answered": answered,
        "evasions": evasions,
        "evasion_combo": row.get("evasions_applied", "").strip(),
        "prompt": uir["assessment_prompt"],
        "response": uir["response"],
        "error_message": "" if uir["error_message"] in ("", "null") else uir["error_message"],
        "is_evasive": _truthy(uir["is_evasive"]),
        "tool_calls": parse_list_field(uir["tool_calls"]),
        "thinking": uir["thinking"],
        "explanation": (row.get("explanation") or "").strip(),
        "base_prompt": (row.get("base_prompt") or "").strip(),
        "chat_history": (row.get("chat_history") or "").strip(),
        # The platform's OWN guardrail triage flags. The CLI reports these; it does not invent them.
        "guardrail_fp": _truthy(row.get("input_guardrails_false_positive")),
        "guardrail_fn": _truthy(row.get("input_guardrails_false_negative")),
    }


def _defend_turn(row: Dict[str, str]) -> Dict[str, Any]:
    uir = parse_kv_record(row.get("user_interaction_record", ""), DEFEND_UIR_KEYS)
    verdict = row.get("verdict", "")
    summary = parse_summary(verdict)
    dets = parse_detections(verdict)
    return {
        "id": row.get("id", ""),
        "schema": "defend",
        "application_id": row.get("application_id", ""),
        "session_id": row.get("session_id", ""),
        "trace_id": row.get("trace_id", ""),
        "user_name": row.get("user_name", ""),
        "timestamp": row.get("timestamp", ""),
        "score": _num(row.get("score")),
        "score_block": _num(row.get("score_block")),
        "score_detect": _num(row.get("score_detect")),
        "prompt": uir["user_prompt"],
        "response": uir["app_response"],
        "rag_content": uir["rag_content"],
        "system_prompt": uir["system_prompt"],
        # app_response empty => the event is an input-side scan; filled => output-side.
        "scan_side": "output" if uir["app_response"].strip() else "input",
        "detections": dets,
        "fired": [d["id"] for d in dets if d["score"] > 0],
        "blocked": [d["id"] for d in dets if d["score"] > 0 and d["mode"] == "block"],
        "has_issue": bool(summary["has_issue"]),
        "issues": summary["issues"],
        "agentic": (row.get("agentic") or "").strip(),
    }


_NORMALIZERS = {"ascend": _ascend_turn, "defend": _defend_turn}


def load_export(path: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Read one CSV export. Returns (schema, turns). Raises ValueError if unrecognized."""
    p = Path(path)
    # utf-8-sig: Console exports are routinely BOM-prefixed, which would corrupt the first header.
    with p.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        # Long free-text fields blow the default 128KB field cap on large exports.
        _raise_field_limit()
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        schema = sniff_schema(header)
        if not schema:
            raise ValueError(
                f"{p.name}: not a recognized Ascend or Defend export "
                f"({len(header)} columns). Export from the Console results view."
            )
        norm = _NORMALIZERS[schema]
        turns = [norm({k: (v or "") for k, v in row.items() if k}) for row in reader]
    return schema, turns


def load_turns(paths: Iterable[str]) -> Tuple[str, List[Dict[str, Any]]]:
    """Read several exports of the SAME schema and concatenate. Mixing schemas is an error."""
    schema = None
    all_turns: List[Dict[str, Any]] = []
    for path in paths:
        s, turns = load_export(path)
        if schema and s != schema:
            raise ValueError(
                f"cannot mix export types in one read: got '{schema}' then '{s}' ({path}). "
                f"Run them separately."
            )
        schema = s
        all_turns.extend(turns)
    if schema is None:
        raise ValueError("no export files given")
    return schema, all_turns


def _raise_field_limit() -> None:
    """csv's default field size limit truncates long responses; raise it as far as C allows."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 2
