#!/usr/bin/env python3
"""
Ascend custom controls ("custom objectives") — build and validate the payload, fully offline.

WHAT THIS IS FOR
----------------
A custom control tests a policy that is specific to one organisation: a NAME, a natural-language
GOAL that states what pass and fail look like, and one of two prompt sources:

  objective   the platform expands the goal into test prompts itself    (wire: promptType "auto")
  prompts     the operator brings the exact prompts to run, at most 100 (wire: promptType "custom")

This module turns operator input — arguments, or a .txt / .csv / .jsonl prompt file — into that
payload, and refuses the combinations the platform would store and then quietly run as something
else. Every function here is pure: no network, no argparse, no global state. The caller owns
transport and the CLI verb.

THE CONTRACT (verified 2026-09-19, re-measured against the API 2026-10-09)
------------------------------------------------------------------------
The payload is the Console's Custom Control form (Settings -> Ascend -> Custom Controls), field
for field:

    name          string, 1-255 chars after trimming
    description   string <= 255 chars, or null
    goal          string, required — it drives prompt generation AND the pass/fail judgement,
                  so it is required in BOTH modes
    strategyType  "none" | "custom" | "all"            (default "none")
    strategies    [strategy id, ...]                   (only meaningful with "custom")
    promptType    "auto" | "custom"                    (default "auto")
    prompts       [string, ...] at most 100, trimmed, duplicates ignored

Those seven keys are the whole object. Nothing else is accepted here, because nothing else exists.

THE ROUTE (measured live 2026-10-09, prod spec 2026.10.8)
-----------------------------------------------------------
    POST   /ascend/custom-controls          create -> 201 with the record
    GET    /ascend/custom-controls          list   -> {object: "list", data: [...], has_more}
    GET    /ascend/custom-controls/{id}     one record
    PATCH  /ascend/custom-controls/{id}     change
    DELETE /ascend/custom-controls/{id}

The wire record is snake_case — `name`, `goal`, `prompt_type`, `prompts`, `strategy_type`,
`strategies`, and `description` when one was given — plus `id` (`custom-<N>`), `object`
("ascend.custom_control"), `created_at` and `updated_at`. The list endpoint returned every row
and `has_more: false` regardless of `limit` when measured; the client still follows `has_more`
in case that changes. `api_body` turns the validated form payload into the POST body and
`record_summary` reads a record back for display; `record_payload` re-validates one so lint and
estimate work on it. `/controls/custom` is still a DIFFERENT object — a Defend runtime detector
(`tcc_...`, types such as `denied_topics`), not a red-team objective. Never send this payload
there. The Console's "Upload CSV File" hand-off (`to_console_csv`) remains for operators who
build the control by hand.

Once it exists the control is addressed as `custom-<N>` and rides in an application's
`control_ids` next to the built-in ids; it runs only when that application's `control_type` is
`custom`. Results come back in the assessment's `category_summary` under category `custom` with
the control's id.

GOTCHAS (each one is a run that looks fine and tested the wrong thing)
----------------------------------------------------------------------
  - Bring-your-own with ZERO prompts is accepted upstream, where a control with no prompts of its
    own is run from generated ones — the operator's regression suite silently becomes a different
    test. Refused here.
  - Prompts supplied alongside objective mode are stored and never run as written. Refused here.
  - strategyType "custom" with no strategies behaves as "none"; strategies alongside "none" or
    "all" are stored and ignored. Both refused here — a setting that does nothing is a bug.
  - The Console's CSV upload parses at most the first 100 rows of a file. This module fails
    loudly on a longer list instead, and counts AFTER de-duplication.
  - An unquoted comma inside a single-column CSV splits the prompt; the first fragment would be
    kept and the rest lost. Refused here, naming the row.
  - `custom-<N>` ids are absent from `GET /ascend/controls`, so a validator that only knows the
    catalog calls them "unknown". Split them off first (`split_control_ids`) and never rebuild an
    application's control list from the recognised ids alone — merge, do not replace
    (`merge_control_ids`, `build_attach_patch`).
"""

import csv
import io
import json
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence

# The platform's cap on bring-your-own prompts per control.
MAX_PROMPTS = 100
MAX_NAME_CHARS = 255
MAX_DESCRIPTION_CHARS = 255
# The Console refuses a prompt file larger than this; a file that loads here loads there too.
MAX_FILE_BYTES = 10 * 1024 * 1024

# Operator-facing mode -> the wire value of `promptType`.
MODE_OBJECTIVE = "objective"
MODE_PROMPTS = "prompts"
MODES = (MODE_OBJECTIVE, MODE_PROMPTS)
PROMPT_TYPE_BY_MODE = {MODE_OBJECTIVE: "auto", MODE_PROMPTS: "custom"}
MODE_BY_PROMPT_TYPE = {v: k for k, v in PROMPT_TYPE_BY_MODE.items()}
# What people actually type. Anything outside this table is an error, never a guess.
_MODE_ALIASES = {
    "objective": MODE_OBJECTIVE, "generate": MODE_OBJECTIVE, "generated": MODE_OBJECTIVE,
    "auto": MODE_OBJECTIVE,
    "prompts": MODE_PROMPTS, "byo": MODE_PROMPTS, "custom": MODE_PROMPTS,
}

STRATEGY_TYPES = ("none", "custom", "all")
PROMPT_FILE_TYPES = (".txt", ".csv", ".jsonl")

# The whole object. `validate_custom_control` rejects any other key rather than passing it along.
PAYLOAD_KEYS = ("name", "description", "goal", "strategyType", "strategies",
                "promptType", "prompts")

CUSTOM_ID_PREFIX = "custom-"
_CUSTOM_ID = re.compile(r"^custom-([1-9][0-9]*)$")

# The documented weak goals ("Don't talk about competitors.", "Always follow policy.") are both
# under this length; the documented strong one is ~400 characters. Advisory only — see lint.
_SHORT_GOAL_CHARS = 40


class CustomControlError(ValueError):
    """A custom control the platform would reject — or accept and then mis-run."""


# ---- prompts -----------------------------------------------------------------
def normalize_prompts(prompts: Optional[Iterable[Any]], *, limit: int = MAX_PROMPTS) -> List[str]:
    """Trim, drop blanks, drop exact duplicates (first occurrence wins, order kept), enforce the cap.

    The cap is measured AFTER cleaning: 120 lines holding 90 distinct prompts is a valid file, and
    101 distinct prompts is not. Over the cap is an error rather than a truncation — silently
    running the first 100 of a regression suite is how a missing test goes unnoticed.

    Matching is exact after trimming. Case and inner whitespace are part of a prompt: two probes
    that differ only in casing are two probes.
    """
    if prompts is None:
        return []
    if isinstance(prompts, (str, bytes)):
        raise CustomControlError("prompts must be a list of strings, not one string")
    out: List[str] = []
    seen = set()
    for i, p in enumerate(prompts, 1):
        if not isinstance(p, str):
            raise CustomControlError(f"prompt {i} is {type(p).__name__}, not text")
        p = p.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    if len(out) > limit:
        raise CustomControlError(
            f"{len(out)} distinct prompts — a custom control holds at most {limit}.\n"
            f"  split them across several controls, or trim the list")
    return out


def _field_key(name: Any) -> str:
    """Header/key normalisation, identical to the Console's CSV import: drop spaces, lowercase."""
    return str(name).replace(" ", "").lower()


def _read_txt(text: str) -> List[str]:
    # CR/LF only. str.splitlines() also breaks on form feed, U+2028 and friends, which would cut a
    # prompt that legitimately contains one of them into two.
    return re.split(r"\r\n|\n|\r", text)


def _read_csv(text: str, path: str) -> List[str]:
    rows = list(csv.reader(io.StringIO(text, newline="")))
    rows = [(n, r) for n, r in enumerate(rows, 1) if any(c.strip() for c in r)]
    if not rows:
        return []
    _, header = rows[0]
    keys = [_field_key(h) for h in header]
    if "prompt" not in keys:
        raise CustomControlError(
            f"{path}: no 'Prompt' column in the header row (found: "
            f"{', '.join(h.strip() or '(blank)' for h in header)}).\n"
            f"  a prompt CSV needs a header row with a column named Prompt")
    col = keys.index("prompt")
    out: List[str] = []
    for n, row in rows[1:]:
        if len(row) > len(header):
            raise CustomControlError(
                f"{path}: row {n} has {len(row)} columns but the header has {len(header)} — "
                f"a prompt containing a comma must be wrapped in double quotes, or everything "
                f"after the comma is lost")
        out.append(row[col] if col < len(row) else "")
    return out


def _read_jsonl(text: str, path: str) -> List[str]:
    out: List[str] = []
    for n, line in enumerate(_read_txt(text), 1):
        if not line.strip():
            continue
        try:
            val = json.loads(line)
        except ValueError:
            raise CustomControlError(f"{path}: line {n} is not valid JSON") from None
        if isinstance(val, dict):
            hits = [v for k, v in val.items() if _field_key(k) == "prompt"]
            if not hits:
                raise CustomControlError(
                    f"{path}: line {n} is an object with no 'prompt' key "
                    f"(keys: {', '.join(map(str, val)) or 'none'})")
            val = hits[0]
        if not isinstance(val, str):
            raise CustomControlError(
                f"{path}: line {n} must be a JSON string or an object with a string 'prompt'")
        out.append(val)
    return out


def read_prompts_file(path: str, *, limit: int = MAX_PROMPTS) -> List[str]:
    """Load bring-your-own prompts from a file and return them cleaned (see normalize_prompts).

      .txt    one prompt per line
      .csv    a header row with a `Prompt` column (case and spaces ignored); other columns are
              ignored; quoted cells may span lines. Same rule as the Console's upload, so a file
              that loads here uploads there.
      .jsonl  one JSON value per line: a string, or an object with a string `prompt`

    Raises CustomControlError for an unsupported extension, an unreadable or oversized file, a
    malformed row, more than `limit` distinct prompts, and — the one that matters most — a file
    that yields no prompts at all.
    """
    path = str(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in PROMPT_FILE_TYPES:
        raise CustomControlError(
            f"{path}: unsupported prompt file type '{ext or '(none)'}' — "
            f"use one of: {', '.join(PROMPT_FILE_TYPES)}")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise CustomControlError(f"{path}: cannot read the prompt file ({e.strerror or e})") from None
    if size > MAX_FILE_BYTES:
        raise CustomControlError(
            f"{path}: {size} bytes — a prompt file may be at most {MAX_FILE_BYTES} bytes")
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        raise CustomControlError(f"{path}: not UTF-8 text — re-save the file as UTF-8") from None
    except OSError as e:
        raise CustomControlError(f"{path}: cannot read the prompt file ({e.strerror or e})") from None

    if ext == ".txt":
        raw = _read_txt(text)
    elif ext == ".csv":
        raw = _read_csv(text, path)
    else:
        raw = _read_jsonl(text, path)

    prompts = normalize_prompts(raw, limit=limit)
    if not prompts:
        raise CustomControlError(
            f"{path}: no prompts found — the file is empty, or every line is blank.\n"
            f"  bring-your-own mode needs at least one prompt; with none, the platform would "
            f"generate its own and run a different test")
    return prompts


# ---- the payload -------------------------------------------------------------
def resolve_mode(mode: Any) -> str:
    """Map what the operator typed onto `objective` or `prompts`. Unknown is an error, not a default."""
    key = str(mode or "").strip().lower()
    if key not in _MODE_ALIASES:
        raise CustomControlError(
            f"unknown mode '{mode}' — choose one of: {', '.join(MODES)}\n"
            f"  objective  the platform generates test prompts from the goal\n"
            f"  prompts    you bring the exact prompts to run")
    return _MODE_ALIASES[key]


def _clean_ids(values: Optional[Iterable[Any]], what: str) -> List[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise CustomControlError(f"{what} must be a list of ids, not one string")
    out: List[str] = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise CustomControlError(f"{what} must be non-empty strings (got {v!r})")
        if v.strip() not in out:
            out.append(v.strip())
    return out


def validate_custom_control(payload: Dict[str, Any], *,
                            known_strategies: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Check a payload against the contract and return it normalised. The ONE copy of the rules.

    `build_custom_control` ends here too, so a definition typed as arguments and one loaded from a
    file the operator edited by hand are held to exactly the same rules.

    `known_strategies`, when given, is the live strategy id list; an id outside it is refused.
    Without it strategy ids cannot be checked offline and are passed through as written.
    """
    if not isinstance(payload, dict):
        raise CustomControlError("a custom control is a JSON object")
    extra = [k for k in payload if k not in PAYLOAD_KEYS]
    if extra:
        raise CustomControlError(
            f"unknown field(s): {', '.join(map(str, extra))}\n"
            f"  a custom control has exactly: {', '.join(PAYLOAD_KEYS)}")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CustomControlError("name is required")
    name = name.strip()
    if len(name) > MAX_NAME_CHARS:
        raise CustomControlError(f"name is {len(name)} characters — at most {MAX_NAME_CHARS}")

    description = payload.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise CustomControlError("description must be text")
        description = description.strip() or None
    if description and len(description) > MAX_DESCRIPTION_CHARS:
        raise CustomControlError(
            f"description is {len(description)} characters — at most {MAX_DESCRIPTION_CHARS}. "
            f"Put the detail in the goal; the description is a one-line note for your team")

    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise CustomControlError(
            "goal is required in both modes — it is what a response is judged against, not only "
            "what prompts are generated from")
    goal = goal.strip()

    prompt_type = payload.get("promptType", "auto")
    if prompt_type not in MODE_BY_PROMPT_TYPE:
        raise CustomControlError(
            f"promptType '{prompt_type}' — must be 'auto' (objective) or 'custom' (prompts)")
    prompts = normalize_prompts(payload.get("prompts"))
    if prompt_type == "auto" and prompts:
        raise CustomControlError(
            f"{len(prompts)} prompt(s) given in objective mode — they would be stored and never "
            f"run as written.\n  use mode 'prompts' to run exactly these, or drop them")
    if prompt_type == "custom" and not prompts:
        raise CustomControlError(
            "mode 'prompts' needs at least one prompt — with none, the platform generates its own "
            "and runs a different test")

    strategies = _clean_ids(payload.get("strategies"), "strategies")
    strategy_type = payload.get("strategyType")
    if strategy_type is None:
        strategy_type = "custom" if strategies else "none"
    if strategy_type not in STRATEGY_TYPES:
        raise CustomControlError(
            f"strategyType '{strategy_type}' — must be one of: {', '.join(STRATEGY_TYPES)}")
    if strategy_type == "custom" and not strategies:
        raise CustomControlError(
            "strategyType 'custom' with no strategies behaves exactly like 'none' — name the "
            "strategies, or choose 'none' or 'all'")
    if strategy_type != "custom" and strategies:
        raise CustomControlError(
            f"strategies given with strategyType '{strategy_type}' — they would be stored and "
            f"ignored.\n  use strategyType 'custom' to apply exactly these")
    if known_strategies is not None:
        known = set(known_strategies)
        bad = [s for s in strategies if s not in known]
        if bad:
            raise CustomControlError(f"unknown strategy id(s): {', '.join(bad)}")

    return {"name": name, "description": description, "goal": goal,
            "strategyType": strategy_type, "strategies": strategies,
            "promptType": prompt_type, "prompts": prompts}


def build_custom_control(*, name: str, goal: str, mode: str = MODE_OBJECTIVE,
                         description: Optional[str] = None,
                         prompts: Optional[Iterable[str]] = None,
                         prompts_file: Optional[str] = None,
                         strategy_type: Optional[str] = None,
                         strategies: Optional[Iterable[str]] = None,
                         known_strategies: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Build the payload for either mode, validating locally first.

    mode 'objective'  name + goal; the platform generates the prompts. Passing prompts is an error.
    mode 'prompts'    name + goal + the prompts, from `prompts` OR `prompts_file` (not both).

    `strategy_type` defaults to 'custom' when strategies are named and 'none' otherwise.
    Raises CustomControlError naming what is wrong, so the caller can print something actionable.
    """
    m = resolve_mode(mode)
    if prompts is not None and prompts_file is not None:
        raise CustomControlError("pass prompts or a prompts file, not both — which list wins "
                                 "would be a guess")
    supplied: Optional[List[str]] = None
    if prompts_file is not None:
        if m == MODE_OBJECTIVE:
            raise CustomControlError(
                "a prompts file was given in objective mode — it would never be run as written.\n"
                "  use mode 'prompts' to run exactly these prompts")
        supplied = read_prompts_file(prompts_file)
    elif prompts is not None:
        supplied = normalize_prompts(prompts)
    return validate_custom_control(
        {"name": name, "description": description, "goal": goal,
         "strategyType": strategy_type,
         # Passed through untouched: list("role_play") is nine one-letter strategies, and the
         # single-string guard in _clean_ids can only fire if it still sees the string.
         "strategies": strategies if strategies is not None else [],
         "promptType": PROMPT_TYPE_BY_MODE[m],
         "prompts": supplied or []},
        known_strategies=known_strategies)


# ---- the v3 wire shape -------------------------------------------------------
# Console form key -> platform API key. The form is camelCase; the API is snake_case.
WIRE_KEY = {"name": "name", "description": "description", "goal": "goal",
            "strategyType": "strategy_type", "strategies": "strategies",
            "promptType": "prompt_type", "prompts": "prompts"}
FORM_KEY = {v: k for k, v in WIRE_KEY.items()}
# Keys the platform adds to a record that are not part of the definition.
RECORD_ONLY_KEYS = ("id", "object", "created_at", "updated_at")


def api_body(payload: Dict[str, Any], *,
             known_strategies: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """The `POST /ascend/custom-controls` body for a form payload, validated first.

    Every definition key is sent explicitly, so a control is created with exactly the mode the
    operator chose rather than whatever the platform defaults a missing key to. `description` is
    the one exception: it is sent only when set (measured: the platform stores and returns it
    when given, and omits the key otherwise), because an explicit null is a different claim from
    "nothing to say".
    """
    p = validate_custom_control(payload, known_strategies=known_strategies)
    body = {"name": p["name"], "goal": p["goal"],
            "prompt_type": p["promptType"], "prompts": p["prompts"],
            "strategy_type": p["strategyType"], "strategies": p["strategies"]}
    if p["description"]:
        body["description"] = p["description"]
    return body


def record_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """A platform record back into the validated form payload, so lint and estimate work on it.

    Strict on purpose: a record the platform holds in a contradictory state (prompts stored under
    `prompt_type: auto`, say) is refused here with the same message a new definition would get.
    For display, use `record_summary`, which never refuses.
    """
    if not isinstance(record, dict):
        raise CustomControlError("a custom control record is a JSON object")
    form = {key: record[wire] for wire, key in FORM_KEY.items() if wire in record}
    return validate_custom_control(form)


def evasion_label(strategy_type: Any, strategies: Any) -> str:
    """One word for the strategy selection: `none`, `all`, or `custom (N)`."""
    st = str(strategy_type or "none")
    if st == "custom":
        n = len(strategies) if isinstance(strategies, list) else 0
        return f"custom ({n})"
    return st


def record_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """The row a listing shows for a record. Tolerant: it reads what is there and judges nothing."""
    r = record if isinstance(record, dict) else {}
    prompts = r.get("prompts") if isinstance(r.get("prompts"), list) else []
    return {"id": r.get("id"), "name": r.get("name") or "",
            "prompt_type": r.get("prompt_type") or "auto", "prompt_count": len(prompts),
            "evasions": evasion_label(r.get("strategy_type"), r.get("strategies")),
            "goal": r.get("goal") or ""}


def lint_custom_control(payload: Dict[str, Any]) -> List[str]:
    """Advisory notes on a VALID payload. Never blocks; the operator decides."""
    p = validate_custom_control(payload)
    notes: List[str] = []
    if len(p["goal"]) < _SHORT_GOAL_CHARS:
        notes.append(
            "the goal is very short — state the forbidden behaviour, what the agent should do "
            "instead, where it applies, and the grey areas; a response is judged against this text")
    return notes


def estimate_probes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """How many probes this control adds to a run: {'probes': int | None, 'basis': str}.

    Only bring-your-own prompts can be counted ahead of time. The count is the control's own
    contribution; an application whose strategy selection is 'custom' overrides the control's
    strategies for that run, so treat a non-exact figure as a planning number.
    """
    p = validate_custom_control(payload)
    n = len(p["prompts"])
    if p["promptType"] == "auto":
        return {"probes": None,
                "basis": "generated from the goal at run time — not knowable ahead of the run"}
    if p["strategyType"] == "none":
        return {"probes": n, "basis": "exact: each prompt runs once, as written"}
    if p["strategyType"] == "custom":
        k = len(p["strategies"])
        return {"probes": n * (1 + k),
                "basis": f"planning figure, not measured: each prompt as written plus one variant "
                         f"per strategy ({n} x (1 + {k}))"}
    return {"probes": None,
            "basis": f"at least {n}: each prompt as written plus one variant per available "
                     f"strategy; the strategy count is the platform's"}


# ---- hand-off to the Console -------------------------------------------------
def to_console_csv(prompts: Iterable[str]) -> str:
    """The prompts as CSV text the Console's "Upload CSV File" accepts: one `Prompt` column.

    Cells are always quoted, so commas, quotes and line breaks inside a prompt survive the trip.
    """
    rows = normalize_prompts(prompts)
    if not rows:
        raise CustomControlError("no prompts to write")
    buf = io.StringIO(newline="")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(["Prompt"])
    for p in rows:
        w.writerow([p])
    return buf.getvalue()


def write_console_csv(path: str, prompts: Iterable[str]) -> str:
    """Write `to_console_csv` to `path` atomically; returns the path.

    The content is built and validated BEFORE anything touches the disk, and the file appears in
    one rename — a refused prompt list leaves no half-written CSV behind for someone to upload.
    """
    text = to_console_csv(prompts)
    path = str(path)
    folder = os.path.dirname(os.path.abspath(path))
    try:
        fd, tmp = tempfile.mkstemp(prefix=".prompts-", suffix=".csv", dir=folder)
    except OSError as e:
        raise CustomControlError(f"{path}: cannot write the CSV ({e.strerror or e})") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise CustomControlError(f"{path}: cannot write the CSV ({e.strerror or e})") from None
        raise
    return path


# ---- `custom-<N>` ids on an application ---------------------------------------
def is_custom_id(control_id: Any) -> bool:
    """Whether this control id names a custom control (as opposed to a catalog control)."""
    return isinstance(control_id, str) and control_id.startswith(CUSTOM_ID_PREFIX)


def custom_id(n: Any) -> str:
    """`custom-<N>` from 7, '7' or 'custom-7'. N is the number in the control's Console edit URL."""
    s = str(n).strip()
    if s.isdigit():
        s = CUSTOM_ID_PREFIX + s.lstrip("0")
    if not _CUSTOM_ID.match(s):
        raise CustomControlError(
            f"not a custom control id: {n!r} — expected custom-<N>, e.g. custom-7")
    return s


def parse_custom_id(control_id: Any) -> int:
    """The N of `custom-<N>`."""
    return int(_CUSTOM_ID.match(custom_id(control_id)).group(1))


def split_control_ids(control_ids: Optional[Iterable[str]]) -> Dict[str, List[str]]:
    """Separate catalog ids from custom ones: {'builtin': [...], 'custom': [...]}.

    Validate only `builtin` against `GET /ascend/controls`. Custom ids are never in that catalog,
    so checking them there reports every one of them as unknown.
    """
    out: Dict[str, List[str]] = {"builtin": [], "custom": []}
    for cid in control_ids or []:
        out["custom" if is_custom_id(cid) else "builtin"].append(cid)
    return out


def merge_control_ids(existing: Optional[Iterable[str]], *, add: Sequence[str] = (),
                      remove: Sequence[str] = ()) -> List[str]:
    """`existing` plus `add` minus `remove`, order kept, no duplicates.

    Nothing is dropped for being unrecognised. `control_ids` is one flat list holding catalog ids
    and `custom-<N>` ids together, and PATCH replaces the whole list — so rebuilding it from "the
    ids I could validate" silently narrows every future run of that application.
    """
    gone = set(remove)
    out: List[str] = []
    for cid in list(existing or []) + list(add):
        if cid not in gone and cid not in out:
            out.append(cid)
    return out


def _app_control_ids(app: Dict[str, Any]) -> List[str]:
    ids = app.get("control_ids") if isinstance(app, dict) else None
    if not isinstance(ids, list):
        raise CustomControlError(
            "the application record carries no control_ids list — pass the full record from "
            "GET /ascend/applications/{id}. Merging onto a guess would replace its real scope")
    return ids


def build_attach_patch(app: Dict[str, Any], custom_ids: Iterable[Any], *,
                       replace: bool = False) -> Dict[str, Any]:
    """The PATCH body that adds custom controls to an application's scope without losing any.

    Always `control_type: "custom"` plus the explicit merged list — the one control selection
    shape the platform accepts on create. With `replace=True` the list is exactly `custom_ids`.

    Measured 2026-10-09: a v3 PATCH carrying a `custom-<N>` id is accepted and the id is there on
    read-back. Read the application back after sending anyway — the PATCH response is not the
    proof, the record is.
    """
    add = [custom_id(c) for c in custom_ids]
    if not add:
        raise CustomControlError("no custom control ids to attach")
    if replace:
        # The operator asked for exactly this list. Nothing is merged, and the empty-scope guard
        # below does not apply: shrinking the scope is the stated intent.
        return {"control_type": "custom", "control_ids": merge_control_ids([], add=add)}
    existing = _app_control_ids(app)
    if not existing and str(app.get("control_type") or "all").lower() != "custom":
        raise CustomControlError(
            "this application has no explicit control list — attaching here would shrink its "
            "scope to the custom controls alone. Resolve the full catalog list first, then attach")
    return {"control_type": "custom", "control_ids": merge_control_ids(existing, add=add)}


def build_detach_patch(app: Dict[str, Any], custom_ids: Iterable[Any]) -> Dict[str, Any]:
    """The PATCH body that removes custom controls from an application's scope, leaving the rest."""
    drop = [custom_id(c) for c in custom_ids]
    if not drop:
        raise CustomControlError("no custom control ids to detach")
    left = merge_control_ids(_app_control_ids(app), remove=drop)
    if not left:
        raise CustomControlError(
            "detaching these would leave the application with no controls at all — a run with no "
            "controls generates zero probes and scores clean without testing anything")
    return {"control_type": "custom", "control_ids": left}
