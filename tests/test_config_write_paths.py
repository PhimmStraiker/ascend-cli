"""
test_config_write_paths — where a config gets written, and what it must never destroy.

Six live defects, found by mapping the config model against the source. The first two are data
loss with an exit-0 success message, which is the worst failure mode this tool has:

  1. The config name is derived from the URL's HOST, so two endpoints on one host derived the SAME
     filename and the second run silently overwrote the first — including any `_ascend` app
     binding it carried.
  2. Re-deriving a config overwrote it wholesale, so a refresh threw away the `_ascend` binding
     written at registration and unbound the target from its application.
  3. Updating a config wrote to `config_dir()`, which depends on the current directory, so the
     same edit run from elsewhere created a SECOND copy instead of updating the one in use.
  4. `--out ./mybot.json` did not write to the current directory: `Path("./x").parent == Path(".")`
     is true, so an explicitly written path was indistinguishable from a bare name.
  5. `--out out/mybot` (no extension) wrote a file literally named `mybot`, which
     `adapter configs` globs for `*.json` and so could never list again.
  6. `--code` reduced `--out` to its stem, silently ignoring the directory — while the docs
     promised "`--out` with a directory in it writes exactly there".

COMPATIBILITY IS THE POINT of the first four tests: a bare `--out name` must keep landing in the
config dir, and a same-target re-run must keep overwriting in place. Customers and their coding
agents already depend on both.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend      # noqa: E402


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "cfgdir"
    d.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(d))
    return d


# ---- unchanged behaviour (the compatibility guarantee) -----------------------------------------
def test_bare_name_still_lands_in_the_config_dir(cfgdir):
    assert ascend.resolve_out_path("mybot") == cfgdir / "mybot.json"


def test_bare_name_with_extension_is_unchanged(cfgdir):
    assert ascend.resolve_out_path("mybot.json") == cfgdir / "mybot.json"


def test_absolute_path_is_unchanged(cfgdir, tmp_path):
    p = tmp_path / "elsewhere" / "bot.json"
    assert ascend.resolve_out_path(str(p)) == p


def test_same_target_rerun_overwrites_in_place(cfgdir):
    """An intentional refresh. Scripts rely on this staying a plain overwrite."""
    a = {"adapter": "direct_api", "endpoint": "https://h/chat"}
    p1, n1 = ascend._write_named_config(a, "h")
    p2, n2 = ascend._write_named_config({**a, "note": "again"}, "h")
    assert (p1, n1) == (p2, n2)
    assert json.loads(p1.read_text())["note"] == "again"


# ---- 4 + 5: an explicitly written path is honoured ---------------------------------------------
def test_explicit_dot_slash_writes_to_the_working_directory(cfgdir):
    assert ascend.resolve_out_path("./mybot.json") == Path("mybot.json")


def test_extension_is_added_on_every_branch(cfgdir):
    assert ascend.resolve_out_path("out/mybot") == Path("out/mybot.json")
    assert ascend.resolve_out_path("./mybot") == Path("mybot.json")


def test_nested_relative_path_is_written_exactly_there(cfgdir):
    assert ascend.resolve_out_path("out/deep/bot.json") == Path("out/deep/bot.json")


# ---- 1: a different target must never clobber an existing config -------------------------------
def test_different_endpoint_on_the_same_host_keeps_both(cfgdir):
    first = {"adapter": "direct_api", "endpoint": "https://h/chat",
             "_ascend": {"app_id": "aapp_FIRST"}}
    second = {"adapter": "direct_api", "endpoint": "https://h/v1/chat"}
    p1, n1 = ascend._write_named_config(first, "h")
    p2, n2 = ascend._write_named_config(second, "h")
    assert n1 == "h" and n2 == "h-2", "the second target must be moved aside, not overwrite"
    assert p1 != p2
    survived = json.loads(p1.read_text())
    assert survived["endpoint"] == "https://h/chat"
    assert survived["_ascend"] == {"app_id": "aapp_FIRST"}      # binding intact


def test_a_user_chosen_name_is_honoured_exactly(cfgdir):
    """--save-as is explicit intent: it overwrites rather than inventing a -2 name."""
    ascend._write_named_config({"adapter": "direct_api", "endpoint": "https://h/chat"}, "mine")
    _, name = ascend._write_named_config(
        {"adapter": "direct_api", "endpoint": "https://other/chat"}, "mine", exact=True)
    assert name == "mine"


def test_collision_suffix_keeps_counting(cfgdir):
    for i, ep in enumerate(("https://h/a", "https://h/b", "https://h/c")):
        _, name = ascend._write_named_config({"adapter": "direct_api", "endpoint": ep}, "h")
        assert name == ("h" if i == 0 else f"h-{i + 1}")


# ---- 2: a refresh must not unbind the target ---------------------------------------------------
def test_app_binding_survives_a_refresh(cfgdir):
    bound = {"adapter": "direct_api", "endpoint": "https://h/chat",
             "_ascend": {"app_id": "aapp_X"}}
    p, _ = ascend._write_named_config(bound, "h")
    # a freshly discovered config never carries _ascend
    ascend._write_named_config({"adapter": "direct_api", "endpoint": "https://h/chat"}, "h")
    assert json.loads(p.read_text())["_ascend"] == {"app_id": "aapp_X"}


def test_a_new_binding_wins_over_the_old_one(cfgdir):
    p, _ = ascend._write_named_config(
        {"adapter": "direct_api", "endpoint": "https://h/chat", "_ascend": {"app_id": "old"}}, "h")
    ascend._write_named_config(
        {"adapter": "direct_api", "endpoint": "https://h/chat", "_ascend": {"app_id": "new"}}, "h")
    assert json.loads(p.read_text())["_ascend"] == {"app_id": "new"}


# ---- 3: an update goes back to the file it came from -------------------------------------------
def test_update_rewrites_the_resolved_file_not_a_second_copy(cfgdir, tmp_path, monkeypatch):
    a = {"adapter": "direct_api", "endpoint": "https://h/chat"}
    p1, _ = ascend._write_named_config(a, "h")
    # now run from a directory that has its own ./configs — which would become config_dir()
    other = tmp_path / "other"
    (other / "configs").mkdir(parents=True)
    monkeypatch.delenv("ASCEND_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(cfgdir))     # still resolvable
    monkeypatch.chdir(other)
    p2, _ = ascend._write_named_config({**a, "note": "updated"}, "h")
    assert p2 == p1, "an update must rewrite the file it resolved from"
    assert not (other / "configs" / "h.json").exists()
    assert json.loads(p1.read_text())["note"] == "updated"


# ---- endpoint comparison underpins all of the above --------------------------------------------
def test_endpoint_is_read_however_the_adapter_spells_it():
    assert ascend._config_endpoint({"endpoint": "https://h/c"}) == "https://h/c"
    assert ascend._config_endpoint({"url": "https://h/c"}) == "https://h/c"
    assert ascend._config_endpoint({"message_endpoint": "https://h/c"}) == "https://h/c"
    assert ascend._config_endpoint(
        {"base_url": "https://h", "chat_path": "/c"}) == "https://h/c"
    assert ascend._config_endpoint({}) is None
    assert ascend._config_endpoint(None) is None


# ---- the refresh contract must hold for EVERY adapter shape ------------------------------------
# The first version of the collision guard read "endpoint unknown" as "a different target", so an
# ordinary re-run forked <name>-2, -3, -4… for every adapter whose endpoint it could not see.
# `example-bedrock.json` carries only a region; `example-session_poll.json` carries no URL at all.
# That would have broken the refresh contract for six preset families while looking like success.
EXAMPLES = sorted((REPO / "configs").glob("example-*.json"))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.stem)
def test_rerunning_any_shipped_adapter_shape_refreshes_in_place(example, cfgdir):
    cfg = json.loads(example.read_text())
    p1, n1 = ascend._write_named_config(cfg, "same-target")
    p2, n2 = ascend._write_named_config(cfg, "same-target")
    assert n1 == n2 == "same-target", f"{example.stem} forked a sibling on an unchanged re-run"
    assert p1 == p2
    assert len(list(cfgdir.glob("*.json"))) == 1, "a second file appeared for the same target"


def test_unknown_endpoint_on_both_sides_is_treated_as_a_refresh(cfgdir):
    """The fail-safe rule: cannot tell => same target => overwrite in place (today's behaviour)."""
    blind = {"adapter": "session_poll", "poll": {"interval_ms": 500}}
    _, n1 = ascend._write_named_config(blind, "blind")
    _, n2 = ascend._write_named_config({**blind, "note": "again"}, "blind")
    assert n1 == n2 == "blind"


def test_one_sided_unknown_endpoint_is_also_a_refresh(cfgdir):
    known = {"adapter": "direct_api", "endpoint": "https://h/chat"}
    blind = {"adapter": "session_poll"}
    _, n1 = ascend._write_named_config(known, "half")
    _, n2 = ascend._write_named_config(blind, "half")
    assert n2 == "half", "an unknowable endpoint must not be read as 'differs'"


# ---- endpoint normalization stops spurious siblings --------------------------------------------
@pytest.mark.parametrize("a,b", [
    ("https://H.Example.com/chat", "https://h.example.com/chat"),   # host case
    ("https://h.example.com/chat/", "https://h.example.com/chat"),  # trailing slash
    ("https://h.example.com:443/chat", "https://h.example.com/chat"),
    ("http://h.example.com:80/chat", "http://h.example.com/chat"),
])
def test_equivalent_urls_are_the_same_target(a, b, cfgdir):
    _, n1 = ascend._write_named_config({"adapter": "direct_api", "endpoint": a}, "norm")
    _, n2 = ascend._write_named_config({"adapter": "direct_api", "endpoint": b}, "norm")
    assert n2 == "norm", f"{a} and {b} must be one target"


# ---- the sibling search converges -------------------------------------------------------------
def test_refreshing_the_second_bot_reuses_its_sibling(cfgdir):
    """Otherwise every refresh mints a new -N and the one you point --config at goes stale."""
    first = {"adapter": "direct_api", "endpoint": "https://h/chat"}
    second = {"adapter": "direct_api", "endpoint": "https://h/v1/chat"}
    ascend._write_named_config(first, "h")
    _, n2 = ascend._write_named_config(second, "h")
    assert n2 == "h-2"
    for _ in range(3):                      # refresh the second bot repeatedly
        _, again = ascend._write_named_config({**second, "t": "x"}, "h")
        assert again == "h-2", "a refresh must reuse the sibling, not fork a new one"
    assert sorted(p.stem for p in cfgdir.glob("*.json")) == ["h", "h-2"]


# ---- a re-pointed name must not inherit the old target's app binding --------------------------
def test_explicit_repoint_does_not_inherit_the_previous_app_binding(cfgdir):
    ascend._write_named_config(
        {"adapter": "direct_api", "endpoint": "https://old/chat",
         "_ascend": {"app_id": "aapp_OLD"}}, "mine")
    p, _ = ascend._write_named_config(
        {"adapter": "direct_api", "endpoint": "https://new/chat"}, "mine", exact=True)
    assert "_ascend" not in json.loads(p.read_text()), \
        "a deliberately re-pointed config must not stay bound to the old target's application"


# ---- --out must not be handed a directory ------------------------------------------------------
@pytest.mark.parametrize("bad", ["./", ".", "out/", ".."])
def test_directory_valued_out_is_a_clean_usage_error(bad, cfgdir):
    with pytest.raises(SystemExit) as e:
        ascend.resolve_out_path(bad)
    assert e.value.code == 3


def test_existing_directory_is_also_refused(cfgdir, tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(SystemExit):
        ascend.resolve_out_path(str(d))
