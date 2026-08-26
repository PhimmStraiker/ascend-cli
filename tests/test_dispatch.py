"""
test_dispatch.py — extract_prompt / shape_result / conversation_key.

These three pure functions are the router's contract with the assessment wire
format. They must handle every body shape a rendered probe can arrive in without
ever guessing wrong or raising on the happy paths. Heavily parametrized so a
regression in field precedence or JSON shaping trips at least one case.
"""
import importlib

import pytest

dispatch = importlib.import_module("dispatch")
extract_prompt = dispatch.extract_prompt
shape_result = dispatch.shape_result
conversation_key = dispatch.conversation_key
ConfigError = dispatch.ConfigError


# --------------------------------------------------------------------------- #
# extract_prompt — single recognised field
# --------------------------------------------------------------------------- #
SINGLE_FIELDS = ["prompt", "message", "input", "text", "query", "content", "question"]


@pytest.mark.parametrize("field", SINGLE_FIELDS)
@pytest.mark.parametrize("value", ["hello world", "", "multi\nline", 'q"uote'])
def test_extract_prompt_single_field(field, value):
    body = {field: value}
    assert extract_prompt(body, {}) == value


@pytest.mark.parametrize("field", SINGLE_FIELDS)
@pytest.mark.parametrize("value", [42, 3.14, 0])
def test_extract_prompt_numeric_coerced_to_str(field, value):
    body = {field: value}
    assert extract_prompt(body, {}) == str(value)


def test_extract_prompt_plain_string_body():
    assert extract_prompt("just a string", {}) == "just a string"


@pytest.mark.parametrize("s", ["", "  ", "unicode 🚀 你好", 'with "quotes"'])
def test_extract_prompt_string_body_variants(s):
    assert extract_prompt(s, {}) == s


# Field-precedence: _PROMPT_FIELDS order is prompt > message > input > text > query > content > question
PRECEDENCE = [
    ({"prompt": "A", "message": "B"}, "A"),
    ({"message": "B", "input": "C"}, "B"),
    ({"input": "C", "text": "D"}, "C"),
    ({"text": "D", "query": "E"}, "D"),
    ({"query": "E", "content": "F"}, "E"),
    ({"content": "F", "question": "G"}, "F"),
    ({"question": "G", "unrelated": "Z"}, "G"),
    ({"message": "B", "prompt": "A", "text": "D"}, "A"),
]


@pytest.mark.parametrize("body,expected", PRECEDENCE)
def test_extract_prompt_field_precedence(body, expected):
    assert extract_prompt(body, {}) == expected


# --------------------------------------------------------------------------- #
# extract_prompt — explicit prompt_field config
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["prompt", "message", "customField", "q", "user_text"])
def test_extract_prompt_explicit_field(field):
    decoy_key = "message" if field != "message" else "text"
    body = {field: "target-value", decoy_key: "decoy"}
    cfg = {"prompt_field": field}
    assert extract_prompt(body, cfg) == "target-value"


def test_extract_prompt_explicit_field_wins_over_default_order():
    # prompt would normally win, but prompt_field forces `message`
    body = {"prompt": "A", "message": "B"}
    assert extract_prompt(body, {"prompt_field": "message"}) == "B"


@pytest.mark.parametrize("value", ["x", 7, 1.5])
def test_extract_prompt_explicit_field_coerces(value):
    assert extract_prompt({"f": value}, {"prompt_field": "f"}) == str(value)


def test_extract_prompt_explicit_field_missing_raises():
    with pytest.raises(ConfigError):
        extract_prompt({"other": "x"}, {"prompt_field": "nope"})


# --------------------------------------------------------------------------- #
# extract_prompt — nested fallback (deepest lone string)
# --------------------------------------------------------------------------- #
NESTED_CASES = [
    ({"data": {"deep": "found"}}, "found"),
    ({"a": {"b": {"c": "deepvalue"}}}, "deepvalue"),
    ({"wrapper": {"items": ["first"]}}, "first"),
    ({"envelope": {"payload": {"unknown_key": "abc"}}}, "abc"),
    ({"list_at_top": ["hello"]}, "hello"),
]


@pytest.mark.parametrize("body,expected", NESTED_CASES)
def test_extract_prompt_nested_fallback(body, expected):
    assert extract_prompt(body, {}) == expected


# --------------------------------------------------------------------------- #
# extract_prompt — error cases
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [123, 4.5, None, True, ["a", "b"]])
def test_extract_prompt_bad_type_raises(body):
    # non-str/dict bodies cannot yield a prompt
    with pytest.raises(ConfigError):
        extract_prompt(body, {})


def test_extract_prompt_empty_dict_raises():
    with pytest.raises(ConfigError):
        extract_prompt({}, {})


def test_extract_prompt_dict_no_strings_raises():
    with pytest.raises(ConfigError):
        extract_prompt({"a": {"b": {"c": {}}}}, {})


# --------------------------------------------------------------------------- #
# shape_result
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["response text", "", "multi\nline", "unicode 🚀"])
def test_shape_result_success_default_field(text):
    status, body = shape_result({"response": text, "success": True}, {})
    assert status == 200
    assert body["response"] == text
    assert "_error" not in body


@pytest.mark.parametrize("field", ["answer", "output", "reply", "result_text"])
def test_shape_result_custom_response_field_mirrors(field):
    status, body = shape_result({"response": "hi", "success": True},
                                {"response_field": field})
    assert status == 200
    assert body["response"] == "hi"
    assert body[field] == "hi"


def test_shape_result_response_field_equal_default_no_dup():
    status, body = shape_result({"response": "hi", "success": True},
                                {"response_field": "response"})
    assert list(body.keys()) == ["response"]


def test_shape_result_includes_metadata():
    status, body = shape_result(
        {"response": "hi", "success": True, "metadata": {"adapter": "direct_api"}}, {})
    assert body["_meta"] == {"adapter": "direct_api"}


def test_shape_result_failure_default_502():
    status, body = shape_result(
        {"response": "", "success": False, "error": "boom", "metadata": {}}, {})
    assert status == 502
    assert body["_error"] == "boom"
    assert body["response"] == ""


@pytest.mark.parametrize("upstream", [400, 401, 403, 404, 429, 500, 503])
def test_shape_result_failure_honours_upstream_status(upstream):
    status, body = shape_result(
        {"response": "", "success": False, "error": "e",
         "metadata": {"status_code": upstream}}, {})
    assert status == upstream
    assert "_error" in body


def test_shape_result_failure_missing_error_has_default():
    status, body = shape_result({"success": False}, {})
    assert status == 502
    assert body["_error"] == "adapter failure"


def test_shape_result_none_response_becomes_empty_string():
    status, body = shape_result({"response": None, "success": True}, {})
    assert body["response"] == ""


# --------------------------------------------------------------------------- #
# conversation_key
# --------------------------------------------------------------------------- #
def test_conversation_key_none_by_default():
    msg = {"payload": {"headers": {"X-Conv": "abc"}, "body": {"conv_id": "xyz"}}}
    assert conversation_key(msg, {}) is None


@pytest.mark.parametrize("hdr,val", [
    ("X-Conversation-Id", "conv-1"),
    ("X-Session", "sess-9"),
    ("X-Thread", "t-42"),
])
def test_conversation_key_from_header(hdr, val):
    msg = {"payload": {"headers": {hdr: val}}}
    cfg = {"conversation_key": f"header:{hdr}"}
    assert conversation_key(msg, cfg) == val


def test_conversation_key_header_missing_returns_none():
    msg = {"payload": {"headers": {}}}
    assert conversation_key(msg, {"conversation_key": "header:X-Absent"}) is None


@pytest.mark.parametrize("field,val", [
    ("conv_id", "c-1"),
    ("thread_id", "th-2"),
    ("session", "s-3"),
])
def test_conversation_key_from_body(field, val):
    msg = {"payload": {"body": {field: val}}}
    cfg = {"conversation_key": f"body:{field}"}
    assert conversation_key(msg, cfg) == val


def test_conversation_key_body_not_dict_returns_none():
    msg = {"payload": {"body": "a plain string body"}}
    assert conversation_key(msg, {"conversation_key": "body:conv_id"}) is None


def test_conversation_key_missing_payload_safe():
    assert conversation_key({}, {"conversation_key": "header:X"}) is None


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #
def test_load_config_inline_dict_passthrough():
    cfg = {"adapter": "direct_api", "endpoint": "http://x"}
    assert dispatch.load_config(cfg) is cfg


def test_load_config_empty_raises():
    with pytest.raises(dispatch.ConfigError):
        dispatch.load_config("")


def test_load_config_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ASCENDBRIDGE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(dispatch.ConfigError):
        dispatch.load_config("does-not-exist")


def test_load_config_reads_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ASCENDBRIDGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "acme.json").write_text('{"adapter": "direct_api", "endpoint": "http://x"}')
    cfg = dispatch.load_config("acme")
    assert cfg["endpoint"] == "http://x"


def test_dot_traverses_json_encoded_as_string():
    """REGRESSION: real payloads nest JSON *as strings* several levels deep; a plain
    split('.') walk hits the string and silently returns None, losing the answer."""
    import json as _json, sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runtime"))
    from adapters.websocket_direct import _dot
    payload = {"envelope": _json.dumps({"data": _json.dumps({"message": {"text": "the answer"}})})}
    assert _dot(payload, "envelope.data.message.text") == "the answer"
    # plain traversal must be unaffected
    assert _dot({"a": {"b": ["x", {"c": "y"}]}}, "a.b.1.c") == "y"
    # a missing path is still None, not an exception
    assert _dot({"a": 1}, "a.b.c") is None
    # a scalar string stays a string
    assert _dot({"a": "plain"}, "a") == "plain"
