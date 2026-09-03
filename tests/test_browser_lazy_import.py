"""
test_browser_lazy_import — Playwright is optional, and the tool has to behave that way.

`pyproject.toml`, the README ("the bridge never needs a browser") and `doctor` all list Playwright
as optional. It was not: `adapters/__init__` imports the browser adapter eagerly and the adapter
imported Playwright at module scope, so `import dispatch` — and with it every run path — failed on a
clean install with a bare `ModuleNotFoundError`. Anyone who happened to have Playwright installed
never saw it. These two cases pin the contract the other lazy adapters (`websocket_direct`,
`bedrock`) already keep:

  1. the adapter framework imports with Playwright absent;
  2. the browser adapter itself names the missing package through the normal `_fail` path.

Playwright is made "absent" by setting its `sys.modules` entries to None, which makes `import`
raise ImportError — the same trick `test_importers` uses for `yaml`.
"""
import asyncio
import sys

import pytest

_PW_MODULES = ("playwright", "playwright.async_api")
_OURS = ("adapters", "adapters.browser", "adapters.base", "dispatch")


@pytest.fixture
def no_playwright(monkeypatch):
    for name in _PW_MODULES:
        monkeypatch.setitem(sys.modules, name, None)
    # Other test modules may already have imported these with Playwright present; drop the cached
    # copies so the import under test really runs. monkeypatch restores them at teardown.
    for name in list(sys.modules):
        if name in _OURS or name.startswith("adapters."):
            monkeypatch.delitem(sys.modules, name, raising=False)


def test_adapter_package_imports_without_playwright(no_playwright):
    import dispatch  # noqa: F401  — this is the import that used to raise
    from adapters import BrowserAdapter  # noqa: F401  — and the class is still registered


def test_browser_adapter_names_the_missing_package(no_playwright):
    from adapters.browser import BrowserAdapter

    result = asyncio.run(BrowserAdapter().send_prompt("hi", {"url": "https://bot.example/chat"}))

    assert result["success"] is False
    assert "playwright" in result["error"]
    assert "pip install playwright" in result["error"]


def test_missing_url_is_reported_before_missing_playwright(no_playwright):
    """A misconfigured target gets the config error first; the dependency hint is not a distraction."""
    from adapters.browser import BrowserAdapter

    result = asyncio.run(BrowserAdapter().send_prompt("hi", {}))

    assert result["success"] is False
    assert "No url configured" in result["error"]
