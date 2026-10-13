"""The crawl reader accepts only fresh, exact-target browser evidence."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import app_runtime
from service import crawl4ai_api as api
from service.research.runner import Runner

URL = "https://source.test/article?query=one"
TEXT = "Substantive evidence from the requested page including findings and limitations. " * 30
CAPTCHA = {"status": "deadline", "attempts": 2, "challenge_observed": True}


def browser_result(**overrides):
    return {
        "url": URL,
        "title": "Article",
        "markdown": TEXT,
        "status_code": 200,
        "captcha": {"status": "not_attempted", "attempts": 0},
        "method": "cdp-visible-text",
        "links": [{"href": "https://source.test/next", "text": "Next"}],
        **overrides,
    }


def read_with_fallback(monkeypatch, browser, *, initial_text="Verify you are human"):
    crawl = SimpleNamespace(
        success=True,
        markdown=initial_text,
        metadata={
            "title": "Just a moment",
            "captcha_recovery": CAPTCHA,
            "captcha_cache_mode": "bypass",
        },
        status_code=403,
        links={"internal": []},
    )
    runtime = SimpleNamespace(configured=True, read=AsyncMock(return_value=browser))
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=crawl))
    monkeypatch.setattr(api, "recover_captcha", AsyncMock(side_effect=AssertionError("extra CAP")))
    with TestClient(api.app) as client:
        response = client.post("/md", json={"url": URL})
    assert response.status_code == 200
    runtime.read.assert_awaited_once_with(URL, allow_captcha_recovery=False)
    api.recover_captcha.assert_not_awaited()
    payload = response.json()
    assert payload["initial_status_code"] == 403
    assert payload["effective_cache_mode"] == "bypass"
    return payload


@pytest.mark.parametrize("title,status", [("Article", 200), ("Search results", 206)])
def test_exact_target_article_and_serp_are_accepted(monkeypatch, title, status):
    browser = browser_result(title=title, status_code=status)
    payload = read_with_fallback(monkeypatch, browser)
    assert payload["initial_captcha"] == CAPTCHA
    assert payload["metadata"]["initial_captcha_recovery"] == CAPTCHA
    assert payload["captcha"] == browser["captcha"]
    assert payload["metadata"]["captcha_recovery"] == browser["captcha"]
    assert payload["markdown"] == TEXT
    assert payload["title"] == title
    assert payload["status_code"] == status
    assert payload["links"] == browser["links"]
    assert payload["assessment"] == {"status": "read", "reasons": []}
    assert payload["next_action"] is None
    assert payload["source"] == "browser-fallback"
    assert payload["method"] == "cdp-visible-text"
    assert payload["browser_fallback"]["status"] == "accepted"
    assert payload["browser_fallback"]["captcha"] == {"status": "not_attempted", "attempts": 0}
    assert payload["browser_fallback"]["initial_assessment"]["status"] == "blocked"
    assessed = asyncio.run(
        Runner(SimpleNamespace()).assess({"title": "Source"}, payload, "crawl4ai")
    )
    assert assessed["status"] == "read"


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"url": "https://foreign.test/article?query=one"}, "target_unverified"),
        ({"url": "https://source.test/other?query=one"}, "target_unverified"),
        ({"url": "https://source.test/article?query=two"}, "target_unverified"),
        ({"url": "http://source.test/article?query=one"}, "target_unverified"),
        ({"url": "https://source.test:444/article?query=one"}, "target_unverified"),
        ({"url": "https://user@source.test/article?query=one"}, "target_unverified"),
        ({"url": None}, "target_unverified"),
        ({"url": "about:blank"}, "target_unverified"),
        ({"status_code": 403}, "http_status_unverified"),
        ({"status_code": None}, "http_status_unverified"),
        ({"status_code": 302}, "http_status_unverified"),
        ({"status_code": "200"}, "http_status_unverified"),
        ({"markdown": ""}, "content_unverified"),
        ({"markdown": "Short text"}, "content_unverified"),
        ({"markdown": "Verify you are human", "title": "Just a moment"}, "content_unverified"),
        ({"captcha": CAPTCHA}, "content_unverified"),
        ({"title": "Privacy error"}, "content_unverified"),
        ({"markdown": "Your connection is not private\n" + TEXT}, "content_unverified"),
        ({"truncated": True}, "content_unverified"),
        ({"markdown": []}, "invalid_browser_content"),
        ({"captcha": "malformed"}, "invalid_browser_content"),
    ],
)
def test_unverified_browser_result_preserves_original_blocked_result(
    monkeypatch, overrides, reason
):
    payload = read_with_fallback(monkeypatch, browser_result(**overrides))
    assert payload["captcha"] == CAPTCHA
    assert payload["metadata"]["captcha_recovery"] == CAPTCHA
    assert payload["markdown"] == "Verify you are human"
    assert payload["title"] == "Just a moment"
    assert payload["status_code"] == 403
    assert payload["assessment"]["status"] == "blocked"
    assert payload["next_action"] == "browser"
    assert payload["browser_fallback"]["status"] == "rejected"
    assert payload["browser_fallback"]["reason"] == reason


def test_browser_exception_is_sanitized_and_retains_initial_receipt(monkeypatch):
    runtime = SimpleNamespace(
        configured=True, read=AsyncMock(side_effect=RuntimeError("private token"))
    )
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    crawl = SimpleNamespace(
        success=True, markdown=TEXT, status_code=403, metadata={"captcha_recovery": CAPTCHA}
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=crawl))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": URL}).json()
    assert payload["captcha"] == CAPTCHA
    assert payload["assessment"]["status"] == "blocked"
    assert payload["browser_fallback"]["reason"] == "browser_read_failed"
    assert "private token" not in str(payload)


@pytest.mark.parametrize(
    "text,status,configured", [(TEXT, 200, True), ("Short", 200, False)], ids=["read", "disabled"]
)
def test_read_or_unconfigured_result_does_not_start_fallback(monkeypatch, text, status, configured):
    runtime = SimpleNamespace(configured=configured, read=AsyncMock())
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    crawl = SimpleNamespace(
        success=True, markdown=text, status_code=status, metadata={"title": "Article"}
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=crawl))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": URL}).json()
    runtime.read.assert_not_awaited()
    assert "browser_fallback" not in payload


@pytest.mark.parametrize("body", ["Short", ""])
def test_thin_or_empty_content_uses_same_verified_fallback(monkeypatch, body):
    runtime = SimpleNamespace(configured=True, read=AsyncMock(return_value=browser_result()))
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    crawl = SimpleNamespace(
        success=True, markdown=body, status_code=200, metadata={"title": "Article"}
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=crawl))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": URL}).json()
    assert payload["assessment"]["status"] == "read"
    runtime.read.assert_awaited_once_with(URL, allow_captcha_recovery=False)


def test_runtime_exposes_and_closes_single_shared_instance(monkeypatch):
    runtime = SimpleNamespace(configured=False, read=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(app_runtime, "BrowserRuntime", lambda: runtime)
    app = FastAPI()
    app_runtime.attach_research(app)
    assert app.state.browser_runtime is runtime
    with TestClient(app):
        runtime.close.assert_not_awaited()
    runtime.close.assert_awaited_once()


def test_server_added_parameters_and_query_encoding_preserve_requested_target(monkeypatch):
    url = "https://source.test/article?query=one%20two&empty="
    runtime = SimpleNamespace(
        configured=True,
        read=AsyncMock(
            return_value=browser_result(
                url="https://source.test/article/?query=one+two&empty=&lr=51"
            )
        ),
    )
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    crawl = SimpleNamespace(success=True, markdown="Short", status_code=200, metadata={})
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=crawl))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": url}).json()
    assert payload["assessment"]["status"] == "read"
    runtime.read.assert_awaited_once_with(url, allow_captcha_recovery=False)


def test_fallback_deadline_cancels_reader_and_retains_initial_receipt(monkeypatch):
    cleaned = []

    async def waiting_reader(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    runtime = SimpleNamespace(configured=True, read=waiting_reader)
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    monkeypatch.setattr(api, "_BROWSER_FALLBACK_TIMEOUT_SECONDS", 0.01)
    crawl = SimpleNamespace(
        success=True, markdown=TEXT, status_code=403, metadata={"captcha_recovery": CAPTCHA}
    )
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=crawl))
    with TestClient(api.app) as client:
        payload = client.post("/md", json={"url": URL}).json()
    assert cleaned == [True]
    assert payload["captcha"] == CAPTCHA
    assert payload["assessment"]["status"] == "blocked"
    assert payload["browser_fallback"]["reason"] == "browser_read_timeout"


def test_missing_browser_result_retains_original_blocked_result(monkeypatch):
    payload = read_with_fallback(monkeypatch, None)
    assert payload["captcha"] == CAPTCHA
    assert payload["assessment"]["status"] == "blocked"
    assert payload["browser_fallback"]["reason"] == "no_browser_result"
