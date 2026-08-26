"""
test_map_auth — `ascend map` target-auth flag parsing + baking (offline).

`map --bearer/--api-key/--basic/--cookie/--header/--token-file` fold into headers (and a
query param for `in=query`), then bake into the discovered config so it carries its own auth
into validate/runtime. These pin that translation.
"""
import base64
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402


def _args(**kw):
    base = dict(header=None, bearer=None, api_key=None, basic=None, cookie=None, token_file=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_bearer_to_authorization_header():
    h, q = ascend._target_auth(_args(bearer="TOK"))
    assert h["Authorization"] == "Bearer TOK"
    assert q == {}


def test_api_key_header_vs_query():
    h, _ = ascend._target_auth(_args(api_key="x-api-key:abc"))
    assert h["x-api-key"] == "abc"
    _, q = ascend._target_auth(_args(api_key="key:abc:in=query"))
    assert q == {"key": "abc"}


def test_basic_is_base64():
    h, _ = ascend._target_auth(_args(basic="user:pass"))
    assert h["Authorization"] == "Basic " + base64.b64encode(b"user:pass").decode()


def test_cookie_and_raw_header_merge():
    h, _ = ascend._target_auth(_args(cookie="sid=9", header=["X-Env: prod"]))
    assert h["Cookie"] == "sid=9"
    assert h["X-Env"] == "prod"


def test_token_file(tmp_path):
    f = tmp_path / "tok.txt"
    f.write_text("  FILETOK\n")
    h, _ = ascend._target_auth(_args(token_file=str(f)))
    assert h["Authorization"] == "Bearer FILETOK"


def test_bake_query_not_doubled_when_already_present():
    cfg = {"endpoint": "http://x/chat?key=abc"}
    ascend._bake_auth(cfg, {}, {"key": "abc"})
    assert cfg["endpoint"].count("key=abc") == 1


def test_bake_headers_and_query():
    cfg = {"endpoint": "http://x/chat"}
    ascend._bake_auth(cfg, {"Authorization": "Bearer T"}, {"key": "z"})
    assert cfg["headers"]["Authorization"] == "Bearer T"
    assert cfg["endpoint"] == "http://x/chat?key=z"
