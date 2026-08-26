"""
test_utf8_text — the text/plain -> ISO-8859-1 mojibake guard.

requests decodes `.text` using `.encoding`, which defaults to ISO-8859-1 for
`text/*` responses that carry no charset (RFC 2616). Many hosted agents stream
UTF-8 as `text/plain` with no charset, so their curly quotes and accented
characters arrive mangled unless we override the encoding. `utf8_text` is that
override; these tests pin both branches.
"""
from adapters.base import utf8_text


class ReqLikeResponse:
    """Mimics requests.Response.text: decode `.content` through `.encoding`,
    which requests leaves at ISO-8859-1 for text/* with no declared charset."""

    def __init__(self, content: bytes, content_type: str, encoding=None):
        self.content = content
        self.headers = {"content-type": content_type}
        self.encoding = encoding  # requests: None -> ISO-8859-1 fallback for text/*

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "ISO-8859-1", errors="replace")


def test_utf8_default_when_no_charset():
    # "Here's a simple Python function" — the exact curly apostrophe that broke.
    body = "Here’s a simple Python function".encode("utf-8")
    r = ReqLikeResponse(body, "text/plain")
    # Raw requests would mangle this; utf8_text must recover the real string.
    assert utf8_text(r) == "Here’s a simple Python function"
    assert "Ã" not in utf8_text(r)  # no "Ã" mojibake


def test_raw_text_would_have_mangled_it():
    # Proves the bug exists without the override (guards against a no-op "fix").
    body = "café".encode("utf-8")
    r = ReqLikeResponse(body, "text/plain")
    assert r.text != "café"          # ISO-8859-1 decode is wrong
    assert utf8_text(r) == "café"    # override makes it right


def test_declared_charset_is_respected():
    # Server explicitly said latin-1 — honour it, don't force UTF-8.
    body = "café".encode("latin-1")
    r = ReqLikeResponse(body, "text/plain; charset=iso-8859-1", encoding="iso-8859-1")
    assert utf8_text(r) == "café"


def test_json_charset_left_alone():
    body = "{\"m\": \"hi\"}".encode("utf-8")
    r = ReqLikeResponse(body, "application/json; charset=utf-8", encoding="utf-8")
    assert utf8_text(r) == "{\"m\": \"hi\"}"
