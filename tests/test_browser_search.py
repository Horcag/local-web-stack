"""Document binding and owned-tab cleanup for organic search extraction."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service import browser_search as module
from service.browser import BrowserRuntime


URL = "https://yandex.ru/search/?text=example"


def runtime(monkeypatch):
    card = {"href": "https://example.org/", "title": "Example", "source": "yandex-organic-card"}
    locator = SimpleNamespace(evaluate_all=AsyncMock(return_value=[card]))
    page = SimpleNamespace(url=URL, locator=lambda selector: locator, close=AsyncMock())
    watch = SimpleNamespace(
        native=True, document_id="loader-1", revision=2, inflight=False, refresh_status=AsyncMock()
    )
    subject = BrowserRuntime("http://configured", targeted_reads=False)
    subject._open = AsyncMock(return_value="own")
    subject._page = AsyncMock(return_value=page)
    subject._forget = AsyncMock(return_value=page)
    subject._snapshot = AsyncMock(return_value={"url": URL, "status_code": 200})
    subject._navigation_events["own"] = watch
    subject._navigation_status["own"] = 200
    monkeypatch.setattr(module, "wait_for_document", AsyncMock())
    return subject, page, watch, locator


def test_extracts_organic_cards_and_closes_only_owned_tab(monkeypatch):
    subject, page, watch, locator = runtime(monkeypatch)
    result = asyncio.run(subject.read_search(URL))
    assert result["search_results"][0]["source"] == "yandex-organic-card"
    assert result["search_document"] == {"verified": True, "document_id": "loader-1", "revision": 2}
    subject._open.assert_awaited_once_with(URL)
    subject._snapshot.assert_awaited_once_with("own", allow_captcha_recovery=False)
    subject._forget.assert_awaited_once_with("own")
    page.close.assert_awaited_once()
    locator.evaluate_all.assert_awaited_once_with(module.ORGANIC_SCRIPT)
    assert watch.refresh_status.await_count == 2


@pytest.mark.parametrize("change", ["document", "revision", "inflight", "url", "status", "native"])
def test_navigation_during_extraction_invalidates_cards(monkeypatch, change):
    subject, page, watch, locator = runtime(monkeypatch)

    async def extract(script):
        if change == "document":
            watch.document_id = "loader-2"
        elif change == "revision":
            watch.revision += 1
        elif change == "inflight":
            watch.inflight = True
        elif change == "url":
            page.url = URL + "&text=other"
        elif change == "status":
            subject._navigation_status["own"] = 403
        elif change == "native":
            watch.native = False
        return [{"href": "https://example.org/", "title": "Wrong document"}]

    locator.evaluate_all.side_effect = extract
    result = asyncio.run(subject.read_search(URL))
    assert result["search_results"] == []
    assert result["status_code"] is None
    assert result["search_document"]["verified"] is False
    page.close.assert_awaited_once()


@pytest.mark.parametrize("error", [ValueError, asyncio.CancelledError])
def test_extraction_failure_and_cancellation_close_owned_tab(monkeypatch, error):
    subject, page, _, locator = runtime(monkeypatch)
    locator.evaluate_all.side_effect = error()
    with pytest.raises(error):
        asyncio.run(subject.read_search(URL))
    subject._forget.assert_awaited_once_with("own")
    page.close.assert_awaited_once()


def test_search_disallows_captcha_recovery_and_unconfigured_runtime(monkeypatch):
    subject, _, _, _ = runtime(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(subject.read_search(URL, allow_captcha_recovery=True))
    subject._open.assert_not_awaited()
    assert asyncio.run(BrowserRuntime("").read_search(URL)) is None
