"""capture.py had ZERO tests (305 lines) per the QA audit.

These cover the failure classification that turns a Playwright internal error into
something a user can act on — the real-world case being a bot-protected site that
TERMINATES the browser session, which previously surfaced as
"Target page, context or browser has been closed".
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
from discovery.capture import diagnose_browser_failure, capture_url
import discovery.capture as cap


URL = "https://example.com/support"


def test_killed_session_reads_as_bot_protection():
    d = diagnose_browser_failure(
        Exception("Page.wait_for_timeout: Target page, context or browser has been closed"), URL)
    assert d["diagnosis"] == "bot_protection"
    assert "--manual" in d["hint"] and "--har" in d["hint"]


def test_crash_also_reads_as_bot_protection():
    assert diagnose_browser_failure(Exception("Page crashed"), URL)["diagnosis"] == "bot_protection"


def test_timeout_is_its_own_diagnosis():
    d = diagnose_browser_failure(Exception("Timeout 60000ms exceeded."), URL)
    assert d["diagnosis"] == "navigation_timeout" and "--settle" in d["hint"]


def test_dns_failure_is_named():
    d = diagnose_browser_failure(Exception("net::ERR_NAME_NOT_RESOLVED"), URL)
    assert d["diagnosis"] == "dns" and "VPN" in d["hint"]


def test_unknown_failure_still_returns_a_hint():
    d = diagnose_browser_failure(Exception("something novel"), URL)
    assert d["diagnosis"] == "browser_error" and d["hint"]


def test_capture_url_never_raises_a_playwright_error(monkeypatch):
    """The boundary: a browser explosion must become evidence with a diagnosis."""
    def boom(*a, **kw):
        raise Exception("Target page, context or browser has been closed")
    monkeypatch.setattr(cap, "_capture_async", boom)
    ev = capture_url(URL, headless=True)
    assert ev["diagnosis"] == "bot_protection"
    assert ev["send_verified"] is False and ev["pairs"] == []


def test_capture_url_keyboard_interrupt_is_not_a_crash(monkeypatch):
    """Manual mode literally instructs 'Ctrl-C when done' — that must not look like a bug."""
    def boom(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cap, "_capture_async", boom)
    ev = capture_url(URL, manual=True)
    assert ev["diagnosis"] == "interrupted" and ev["pairs"] == []


def test_noise_regex_excludes_analytics_and_assets():
    for u in ("https://x.datadoghq.com/api", "https://cdn/app.css", "https://t.tiktok.com/i"):
        assert cap.NOISE.search(u), f"{u} should be treated as noise"
    assert not cap.NOISE.search("https://bot.example.com/api/chat")


def test_search_inputs_are_scored_down():
    assert cap.SEARCHY.search("search our help center")
    assert cap.CHATTY.search("Type your message to the assistant")
