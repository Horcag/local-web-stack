import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from service.browser import BrowserRuntime, TabAction, create_browser_router


class Page:
    url = "https://example.com/"

    def __init__(self):
        self.close = AsyncMock()
        self.goto = AsyncMock()
        self.title = AsyncMock(return_value="Example")
        self.locator_result = AsyncMock()
        self.locator_result.inner_text.return_value = "Visible text " * 60
        self.locator_result.evaluate_all.return_value = []
        self.mouse = AsyncMock()

    def is_closed(self):
        return False

    def locator(self, selector):
        return self.locator_result


def runtime():
    subject = BrowserRuntime("http://127.0.0.1:9222")
    page = Page()
    browser = AsyncMock()
    browser.is_connected = lambda: True
    browser.contexts = [AsyncMock()]
    browser.contexts[0].new_page.return_value = page
    subject._browser = browser
    subject._driver = AsyncMock()
    return subject, page, browser


def test_unconfigured_browser_is_explicit_and_no_launch():
    subject = BrowserRuntime("")
    assert asyncio.run(subject.read("https://example.com")) is None
    with pytest.raises(HTTPException) as exc:
        asyncio.run(subject.open("https://example.com"))
    assert exc.value.status_code == 503


def test_collect_closes_only_task_tab_and_never_borrowed_browser():
    subject, page, browser = runtime()
    result = asyncio.run(subject.read("https://example.com"))
    assert result["method"] == "cdp-visible-text"
    page.close.assert_awaited_once()
    browser.close.assert_not_awaited()
    assert not subject._tabs


@pytest.mark.parametrize(
    "action,selector,value",
    [
        ("click", "button", ""),
        ("fill", "input", "text"),
        ("scroll", "", ""),
        ("navigate", "", "https://example.com/next"),
    ],
)
def test_owned_tab_actions_and_shutdown(action, selector, value):
    subject, page, browser = runtime()

    async def run():
        result = await subject.open("https://example.com")
        await subject.action(
            result["tab_id"], TabAction(action=action, selector=selector, value=value)
        )
        await subject.close_tab(result["tab_id"])
        await subject.close()

    asyncio.run(run())
    assert not subject._tabs
    browser.close.assert_not_awaited()


def test_private_urls_and_foreign_tabs_rejected():
    subject = BrowserRuntime("")
    for url in [
        "file:///etc/passwd",
        "http://localhost",
        "http://127.0.0.1",
        "https://user:pass@example.com",
    ]:
        with pytest.raises(HTTPException):
            subject._validate_url(url)
    with pytest.raises(HTTPException):
        asyncio.run(subject.snapshot("foreign-tab"))


def test_cancelled_open_releases_own_tab():
    subject, page, browser = runtime()
    page.goto.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(subject.open("https://example.com"))
    page.close.assert_awaited_once()
    browser.close.assert_not_awaited()
    assert not subject._tabs


def test_browser_routes():
    subject, page, browser = runtime()
    app = FastAPI()
    app.include_router(create_browser_router(subject))
    with TestClient(app) as client:
        assert client.get("/browser/status").json()["configured"]
        tab = client.post("/browser/tabs", json={"url": "https://example.com"}).json()["tab_id"]
        assert client.get(f"/browser/tabs/{tab}").status_code == 200
        assert (
            client.post(f"/browser/tabs/{tab}/action", json={"action": "scroll"}).status_code == 200
        )
        assert client.delete(f"/browser/tabs/{tab}").status_code == 200
        assert client.get("/browser/tabs/foreign").status_code == 404


def test_failed_open_snapshot_releases_tab():
    subject, page, browser = runtime()
    page.locator_result.inner_text.side_effect = RuntimeError("body unavailable")
    with pytest.raises(RuntimeError):
        asyncio.run(subject.open("https://example.com"))
    assert not subject._tabs
    page.close.assert_awaited_once()
    browser.close.assert_not_awaited()
