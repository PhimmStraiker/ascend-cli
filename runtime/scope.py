"""Propose which of Ascend's controls fit what a target IS — and say why.

A single probe is not a coverage claim, and the surface worth testing depends on what the target
can do: a retrieval bot's risk is grounding and data leakage; a bot with tools adds tool misuse;
one that can run code adds code execution; one that holds user data adds exfiltration. The
operator picks the scope today. This maps discovered capabilities to a proposed control scope with
a one-line reason each, so the assessment covers the surface that exists instead of a default.

Deterministic and read-only: it recommends, it does not start anything. The control ids are the
live catalog's (see the `controls` knowledge topic); a name the catalog does not carry is dropped
by the caller, never invented here. `propose(capabilities)` takes a dict of booleans/flags and
returns ``{scope: [{control, why}], baseline: [...], notes: [...]}``.
"""
from __future__ import annotations

from typing import Any, Dict, List

# Baseline: every conversational target, whatever it is. sys_prompt_leak and a jailbreak/prompt
# injection probe are the floor — they need no special capability.
BASELINE = [
    ("sys_prompt_leak", "every agent has a system prompt; leaking it is the first thing to test"),
    ("jailbreak", "instruction-manipulation resistance is a floor for any conversational target"),
]

# capability flag -> [(control_id, why)]. Flags are matched loosely (any truthy of the aliases).
_CAP_RULES: List[Dict[str, Any]] = [
    {"aliases": ("rag", "research", "retrieval", "knowledge", "search", "grounding"),
     "controls": [("app_grounding", "a retrieval/RAG bot can be pushed off its grounding — test that it stays on-source"),
                  ("data_leak", "retrieval surfaces let a bot disclose data it should not — test for leakage")]},
    {"aliases": ("tools", "agentic", "tool", "function_calling", "actions"),
     "controls": [("agentic_tmu", "an agent with tools can be steered into misusing them — tool-misuse assessment")]},
    {"aliases": ("code", "code_interpreter", "shell", "exec", "sandbox", "repl"),
     "controls": [("agentic_rce", "an agent that can run code is where code-execution abuse is meaningful")]},
    {"aliases": ("memory", "user_data", "pii", "crm", "profile", "database", "db", "records"),
     "controls": [("agentic_data_exfil", "an agent with access to user data or memory can be driven to exfiltrate it"),
                  ("data_leak", "a target holding personal or internal data must be tested for leakage")]},
    {"aliases": ("browser", "web", "fetch", "url", "link"),
     "controls": [("indirect_prompt_injection", "a bot that fetches web content can be hijacked by content it reads")]},
    {"aliases": ("file", "upload", "attachment", "document"),
     "controls": [("indirect_prompt_injection", "a bot that reads uploaded files can be hijacked by their contents")]},
    {"aliases": ("sub_agent", "sub_agents", "multi_agent", "orchestrator", "handoff"),
     "controls": [("agentic_tmu", "a bot that delegates to sub-agents widens the tool-misuse surface")]},
]


def _canon(text: str) -> str:
    # fold separators so 'user data', 'user-data' and 'user_data' all match the alias 'user_data'
    return "".join(c if c.isalnum() else "_" for c in str(text).lower())


def _flagged(capabilities: Dict[str, Any], aliases) -> bool:
    for key, val in (capabilities or {}).items():
        if any(a in _canon(key) for a in aliases) and val not in (None, False, 0, "", "false", "no", "none"):
            return True
    return False


def _flagged_any(capabilities: Any, aliases) -> bool:
    if isinstance(capabilities, dict):
        return _flagged(capabilities, aliases)
    if isinstance(capabilities, (list, tuple, set)):
        blob = "_".join(_canon(x) for x in capabilities)
        return any(a in blob for a in aliases)
    return any(a in _canon(capabilities) for a in aliases)


def propose(capabilities: Any) -> Dict[str, Any]:
    """Proposed control scope for a target with these capabilities. Deterministic, additive, de-duped."""
    scope: List[Dict[str, str]] = []
    seen = set()

    def add(control: str, why: str) -> None:
        if control not in seen:
            seen.add(control)
            scope.append({"control": control, "why": why})

    matched: List[str] = []
    for rule in _CAP_RULES:
        if _flagged_any(capabilities, rule["aliases"]):
            matched.append(rule["aliases"][0])
            for control, why in rule["controls"]:
                add(control, why)

    baseline = [{"control": c, "why": w} for c, w in BASELINE]
    notes: List[str] = []
    if not matched:
        notes.append("no agentic capabilities discovered — the baseline is the honest scope; run recon or "
                     "ask the operator what the target can do before widening")
    else:
        notes.append("capabilities matched: " + ", ".join(matched))
    notes.append("proposed names are catalog categories or controls — confirm them against the live catalog "
                 "before registering; a name the tenant does not carry is dropped, not invented")
    notes.append("a small run on each control proves interaction; widen only after the baseline is clean and "
                 "the answer rate is real — a single probe is never a coverage claim")
    return {"scope": scope, "baseline": baseline, "matched": matched, "notes": notes}
