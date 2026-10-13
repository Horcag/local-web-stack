"""Commit navigation keeps bounded CAPTCHA DOM readiness without an extra model budget."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from service import browser as module
from test_browser_runtime import runtime
from test_captcha_browser import Page, Solver, TARGET, arrive
from test_captcha_browser_points import PuzzlePage, PuzzleSolver


@pytest.mark.parametrize("ordered", [False, True])
def test_commit_navigation_waits_for_real_text_or_point_form_before_recovery(monkeypatch, ordered):
    subject, _, borrowed = runtime()
    page = PuzzlePage() if ordered else Page()
    solver = PuzzleSolver() if ordered else Solver()
    ready_forms, page.forms = page.forms, []
    page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
    page.is_closed = lambda: False
    page.close = AsyncMock()
    page.submissions = [arrive]
    original = page.locator

    def locator(selector):
        return (
            SimpleNamespace(evaluate_all=AsyncMock(return_value=[]))
            if selector == "a[href]"
            else original(selector)
        )

    async def ready(state, **kwargs):
        assert state == "domcontentloaded" and kwargs == {"timeout": 3000}
        page.forms = ready_forms

    page.locator, page.wait_for_load_state = locator, AsyncMock(side_effect=ready)
    borrowed.contexts[0].new_page.return_value = page
    monkeypatch.setattr(module.CaptchaSolver, "from_env", lambda: solver)
    result = asyncio.run(subject.read(TARGET))
    assert result["captcha"]["status"] == "solved"
    assert len(solver.images) == 1 and page.clicks[-1] == "submit"
    page.goto.assert_awaited_once_with(TARGET, wait_until="commit", timeout=45000)
    page.wait_for_load_state.assert_awaited_once()
    page.close.assert_awaited_once()
    borrowed.close.assert_not_awaited()


def test_readiness_timeout_is_bounded_and_still_attempts_recovery(monkeypatch):
    subject, page, _ = runtime()
    page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeoutError("No DOM event"))
    recover = AsyncMock(return_value={"status": "not_needed", "attempts": 0})
    monkeypatch.setattr(module, "recover_captcha", recover)
    asyncio.run(subject.read("https://example.com"))
    page.wait_for_load_state.assert_awaited_once_with("domcontentloaded", timeout=3000)
    recover.assert_awaited_once()


def test_internal_no_model_fallback_skips_dom_load_wait(monkeypatch):
    subject, page, _ = runtime()
    page.wait_for_load_state = AsyncMock(side_effect=AssertionError("No extra readiness wait"))
    recover = AsyncMock(side_effect=AssertionError("No additional CAPTCHA recovery"))
    monkeypatch.setattr(module, "recover_captcha", recover)
    asyncio.run(subject.read("https://example.com", allow_captcha_recovery=False))
    page.wait_for_load_state.assert_not_awaited()
    recover.assert_not_awaited()
