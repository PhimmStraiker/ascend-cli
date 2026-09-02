"""
test_shared_conversation_concurrency — a config that shares one conversation must run sequentially.

Statefulness is not only a property of the adapter. `sse_stream` is not in STATEFUL_ADAPTERS, but
a `create` block without `per_prompt` mints exactly ONE conversation and reuses it for every
prompt. Such a config was still getting the stateless default of 10 workers, so ten probes
interleaved inside a single conversation and each one saw the others' turns as its own context.
Nothing failed loudly: every probe was answered and the run completed, so the corruption showed up
only as unreliable multi-turn findings.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from call_target import TargetCaller     # noqa: E402


def caller(cfg, adapter="sse_stream"):
    c = TargetCaller.__new__(TargetCaller)
    c.adapter_type = adapter
    c.config = cfg
    return c


def test_shared_conversation_forces_sequential():
    c = caller({"endpoint": "http://x/s", "create": {"url": "http://x/c"}})
    assert c.is_stateful is True
    assert c.recommended_workers() == 1


def test_per_prompt_conversation_may_parallelize():
    """A fresh conversation per prompt has nothing to interleave."""
    c = caller({"endpoint": "http://x/s", "create": {"url": "http://x/c", "per_prompt": True}})
    assert c.is_stateful is False
    assert c.recommended_workers() == 10


def test_no_create_block_is_still_stateless():
    c = caller({"endpoint": "http://x/s"})
    assert c.is_stateful is False
    assert c.recommended_workers() == 10


def test_conversation_key_remains_the_escape_hatch():
    """conversation_key means the adapter keys conversations per probe, so parallel is safe."""
    c = caller({"endpoint": "http://x/s", "create": {"url": "http://x/c"},
                "conversation_key": "probe_id"})
    assert c.is_stateful is False


def test_explicit_max_workers_still_wins():
    c = caller({"endpoint": "http://x/s", "create": {"url": "http://x/c"}, "max_workers": 4})
    assert c.recommended_workers() == 4


def test_declared_stateful_adapters_are_unaffected():
    """The existing rule must be unchanged for adapters that were already sequential."""
    assert caller({}, adapter="session_api").recommended_workers() == 1
    assert caller({}, adapter="browser").recommended_workers() == 1
    assert caller({}, adapter="direct_api").recommended_workers() == 10
