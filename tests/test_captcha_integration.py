"""CAPTCHA outcomes survive crawler hooks, readers and research assessment."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from service import crawl4ai_api as api
from service.research.runner import Runner

TEXT = "Substantive evidence from the requested source page. " * 30


@pytest.mark.parametrize("cached", [False, True])
def test_crawler_solves_before_extracting_and_closes_owned_browser(monkeypatch, cached):
    hooks = {}
    counts = []
    result = SimpleNamespace(success=True, markdown=TEXT, metadata={"title": "Source"})

    async def arun(url, config):
        counts.append(config.cache_mode)
        if cached and config.cache_mode != api.CacheMode.BYPASS:
            return SimpleNamespace(
                success=True,
                markdown="Please confirm that you are not a robot",
                metadata={"title": "Are you not a robot?"},
            )
        await hooks["before_retrieve_html"](object())
        return result

    crawler = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        arun=arun,
        crawler_strategy=SimpleNamespace(set_hook=lambda key, hook: hooks.update({key: hook})),
    )
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_ENABLED", "true")
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_BASE_URL", "http://gateway.test/v1")
    monkeypatch.setattr(api, "AsyncWebCrawler", lambda **kwargs: crawler)
    recovery = AsyncMock(return_value={"status": "solved", "attempts": 1})
    monkeypatch.setattr(api, "recover_captcha", recovery)
    request = api.CrawlRequest(
        url="https://source.test/", browser_type="chromium", use_persistent_context=False
    )
    actual = asyncio.run(api._crawl(request))
    assert actual.metadata["captcha_recovery"] == {"status": "solved", "attempts": 1}
    recovery.assert_awaited_once()
    crawler.start.assert_awaited_once()
    crawler.close.assert_awaited_once()
    assert counts == [api.CacheMode.BYPASS]


def test_crawler_cancellation_closes_owned_browser(monkeypatch):
    crawler = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        arun=AsyncMock(side_effect=asyncio.CancelledError),
    )
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_ENABLED", "false")
    monkeypatch.setattr(api, "AsyncWebCrawler", lambda **kwargs: crawler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            api._crawl(
                api.CrawlRequest(
                    url="https://source.test/",
                    browser_type="chromium",
                    use_persistent_context=False,
                )
            )
        )
    crawler.close.assert_awaited_once()


@pytest.mark.parametrize("status", ["unexpected_page", "challenge_changed"])
def test_unverified_recovery_is_blocked_in_api_and_research(monkeypatch, status):
    captcha = {"status": status, "attempts": 1}
    result = SimpleNamespace(
        success=True,
        markdown=TEXT,
        metadata={"title": "Readable foreign content", "captcha_recovery": captcha},
        status_code=200,
        links={},
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=result))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": "https://source.test/"}).json()
    assert payload["captcha"] == captcha
    assert payload["assessment"] == {
        "status": "blocked",
        "reasons": ["captcha_target_unverified"],
    }
    assert payload["next_action"] == "browser"
    source = {"title": "Source", "url": "https://source.test/", "run_id": "run"}
    assessed = asyncio.run(Runner(SimpleNamespace()).assess(source, payload, "crawl4ai"))
    assert assessed["status"] == "browser_pending"
    assert assessed["assessment_status"] == "blocked"
    assert assessed["reasons"] == ["captcha_target_unverified"]


@pytest.mark.parametrize("status", ["deadline", "browser_error", "gateway_timeout", "rejected"])
def test_interrupted_observed_challenge_cannot_be_readable_foreign_content(monkeypatch, status):
    captcha = {"status": status, "attempts": 1, "challenge_observed": True}
    result = SimpleNamespace(
        success=True,
        markdown=TEXT,
        metadata={"title": "Readable content", "captcha_recovery": captcha},
        status_code=200,
        links={},
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=result))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": "https://source.test/"}).json()
    assert payload["assessment"]["status"] == "blocked"
    assert payload["next_action"] == "browser"


@pytest.mark.parametrize(
    "final_status,expected", [(200, "read"), (403, "blocked"), (None, "blocked")]
)
def test_solved_gate_preserves_initial_and_actual_final_http_status(
    monkeypatch, final_status, expected
):
    captcha = {"status": "solved", "attempts": 1, "challenge_observed": True}
    if final_status is not None:
        captcha["final_status_code"] = final_status
    result = SimpleNamespace(
        success=True,
        markdown=TEXT,
        metadata={"title": "Verified source", "captcha_recovery": captcha},
        status_code=403,
        links={},
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=result))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": "https://source.test/"}).json()
    assert payload["initial_status_code"] == 403
    assert payload["status_code"] == (final_status if final_status is not None else 403)
    assert payload["assessment"]["status"] == expected


def test_two_reads_do_not_replay_unverified_captcha_cache(monkeypatch):
    hooks = {}
    cache = {}
    reads = []
    recovery = {"status": "unexpected_page", "attempts": 1, "challenge_observed": True}

    async def arun(url, config):
        reads.append(config.cache_mode)
        if config.cache_mode != api.CacheMode.BYPASS and cache:
            return SimpleNamespace(**cache)
        await hooks["before_retrieve_html"](object())
        live = dict(success=True, markdown=TEXT, metadata={"title": "Foreign"}, status_code=200)
        if config.cache_mode != api.CacheMode.BYPASS:
            cache.update(live)
        return SimpleNamespace(**live)

    crawler = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        arun=arun,
        crawler_strategy=SimpleNamespace(set_hook=lambda key, hook: hooks.update({key: hook})),
    )
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_ENABLED", "true")
    monkeypatch.setattr(api, "AsyncWebCrawler", lambda **kwargs: crawler)
    monkeypatch.setattr(api, "recover_captcha", AsyncMock(return_value=recovery))
    request = api.CrawlRequest(
        url="https://source.test/", browser_type="chromium", use_persistent_context=False
    )

    async def read_twice():
        return [await api.markdown(request), await api.markdown(request)]

    results = asyncio.run(read_twice())
    assert reads == [api.CacheMode.BYPASS, api.CacheMode.BYPASS]
    assert not cache
    assert all(r["assessment"]["status"] == "blocked" for r in results)
    assert all(r["effective_cache_mode"] == "bypass" for r in results)
