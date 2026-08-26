"""
runtime.discovery — deterministic "build-adapter" pipeline.

Given captured evidence (a HAR export and/or a list of request/response pairs),
this package classifies each of the six adapter layers independently, composes a
runnable adapter config from the closest of the existing adapters, validates that
config against the live target, and iterates low-confidence layers until one
validates.

    load_har(path) ------------------┐
                                     ▼
    classify_evidence(evidence) --> {layers, config, overall_confidence, unresolved}
                                     │
                                     ▼
    validate_config(adapter, cfg, prompt)   # HARD GATE: usable only when ok=True
                                     │
                                     ▼
    iterate(adapter, cfg, alternates, prompt)   # try alternates for a shaky layer

See ``docs/DISCOVERY.md`` for the full walkthrough and ``docs/CAPABILITY_MATRIX.md``
for the layer model.

The classification half (:mod:`discovery.classify`) is **pure** — no network —
so it is fully unit-testable over evidence dicts. Only :mod:`discovery.validate`
touches the live target, and only when called.
"""
from .classify import (
    load_har,
    classify_evidence,
    compose,
    LAYER_NAMES,
    ClassifyError,
)
from .validate import validate_config, iterate

__all__ = [
    "load_har",
    "classify_evidence",
    "compose",
    "LAYER_NAMES",
    "ClassifyError",
    "validate_config",
    "iterate",
]
