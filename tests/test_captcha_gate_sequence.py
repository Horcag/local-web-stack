"""A provider can replace a submitted silhouette with another known gate."""

import asyncio
from types import SimpleNamespace

import pytest

from service import captcha_browser as browser
from test_captcha_browser import Form, Matches, Page, Solver, arrive


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def immediate(_):
        pass

    monkeypatch.setattr(browser.asyncio, "sleep", immediate)


@pytest.mark.parametrize(
    "destination",
    [
        "https://evil.test/showcaptcha",
        "https://yandex.ru/other",
        "http://yandex.ru/showcaptcha",
        "https://yandex.ru:8443/showcaptcha",
        "https://user:password@yandex.ru/showcaptcha",
    ],
)
def test_foreign_gate_after_yandex_submit_is_not_retried(monkeypatch, destination):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []
    calls = []

    async def yandex(page, solver, target, receipt, at_target, seen_images):
        calls.append(True)
        page.url = destination
        return receipt | {"attempts": 1, "status": "unexpected_page"}

    monkeypatch.setattr(browser, "recover_yandex_points", yandex)
    result = asyncio.run(browser.recover_captcha(page, Solver(), "https://yandex.ru/search"))
    assert result["status"] == "unexpected_page" and len(calls) == 1


def test_new_yandex_checkbox_preserves_attempt_budget(monkeypatch):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []
    calls = []

    async def yandex(page, solver, target, receipt, at_target, seen_images):
        calls.append(receipt["attempts"])
        page.url = "https://yandex.ru/showcaptcha?next=1"
        return receipt | {"attempts": receipt["attempts"] + 1, "status": "unexpected_page"}

    monkeypatch.setattr(browser, "recover_yandex_points", yandex)
    result = asyncio.run(browser.recover_captcha(page, Solver(), "https://yandex.ru/search"))
    assert result["status"] == "new_challenge" and result["attempts"] == 2
    assert calls == [0, 1] and page.clicks == ["checkbox", "checkbox"]


def test_second_yandex_challenge_can_reach_verified_target(monkeypatch):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []

    async def yandex(page, solver, target, receipt, at_target, seen_images):
        if receipt["attempts"]:
            arrive(page, target)
            return receipt | {"attempts": 2, "status": "solved"}
        page.url = "https://yandex.ru/showcaptcha?next=1"
        return receipt | {"attempts": 1, "status": "unexpected_page"}

    monkeypatch.setattr(browser, "recover_yandex_points", yandex)
    result = asyncio.run(browser.recover_captcha(page, Solver(), "https://yandex.ru/search"))
    assert result["status"] == "solved" and result["attempts"] == 2


def test_new_silhouette_gate_preserves_attempt_budget(monkeypatch):
    page = Page(url="https://yandex.ru/showcaptcha")
    page.forms = []
    original_locator = page.locator
    form = SimpleNamespace(locator=lambda selector: Matches([object()]))
    page.locator = lambda selector: (
        Matches([form]) if selector == browser.YANDEX_FORM else original_locator(selector)
    )
    calls = []

    async def yandex(page, solver, target, receipt, at_target, seen_images):
        calls.append(receipt["attempts"])
        page.url = "https://yandex.ru/showcaptcha?status=failed"
        return receipt | {"attempts": receipt["attempts"] + 1, "status": "unexpected_page"}

    monkeypatch.setattr(browser, "recover_yandex_points", yandex)
    result = asyncio.run(browser.recover_captcha(page, Solver(), "https://yandex.ru/search"))
    assert result["status"] == "new_challenge" and calls == [0, 1]


def test_text_gate_after_silhouette_uses_remaining_attempt(monkeypatch):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []

    async def yandex(page, solver, target, receipt, at_target, seen_images):
        page.url = "https://yandex.ru/showcaptcha?next=1"
        page.forms = [Form(page)]
        page.submissions = [lambda p: arrive(p, target)]
        return receipt | {"attempts": 1, "status": "unexpected_page"}

    monkeypatch.setattr(browser, "recover_yandex_points", yandex)
    subject = Solver()
    result = asyncio.run(browser.recover_captcha(page, subject, "https://yandex.ru/search"))
    assert result["status"] == "solved" and result["attempts"] == 2
    assert len(subject.images) == 1
