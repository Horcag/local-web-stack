"""The Yandex introductory guard must finish before the click submits its form."""

import asyncio
from types import SimpleNamespace

from service import captcha_browser as browser
from test_captcha_browser import Page, Solver, TARGET, arrive, recover


def test_checkbox_waits_until_submit_is_ready(monkeypatch):
    states = iter(["button", "button", "submit"])
    waits = []

    async def attribute(name):
        assert name == "type"
        return next(states)

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(browser.asyncio, "sleep", sleep)
    assert asyncio.run(browser._checkbox_ready(SimpleNamespace(get_attribute=attribute)))
    assert waits == [0.25, 0.25]


def test_unready_checkbox_does_not_click_or_send_model(monkeypatch):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []
    subject = Solver()
    calls = []

    async def attribute(name):
        calls.append(name)
        return "button"

    async def sleep(_delay):
        pass

    page.checkbox.get_attribute = attribute
    monkeypatch.setattr(browser.asyncio, "sleep", sleep)
    result = recover(page, subject)
    assert result["status"] == "checkbox_not_ready"
    assert len(calls) == 40 and page.clicks == [] and subject.images == []


def test_ready_checkbox_transition_keeps_source_verification():
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.checkbox_transition = lambda p: arrive(p, TARGET)
    assert recover(page, Solver())["status"] == "solved"
    assert page.clicks == ["checkbox"]


def test_orphan_theme_repair_is_limited_to_yandex_gate():
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.checkbox_transition = lambda p: arrive(p, TARGET)
    scripts = []

    async def evaluate(script):
        scripts.append(script)
        return 1

    page.evaluate = evaluate
    result = recover(page, Solver())
    assert result["render_repaired"] == "orphan_darkreader_fallback"
    assert len(scripts) == 1
    page = Page(url="https://ordinary.test/challenge")
    page.evaluate = evaluate
    page.submissions = [arrive]
    recover(page, Solver())
    assert len(scripts) == 1


def test_checkbox_redirect_cannot_repair_foreign_gate(monkeypatch):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []
    scripts = []

    async def evaluate(script):
        scripts.append(script)
        return 1

    async def immediate(_):
        pass

    page.evaluate = evaluate
    page.checkbox_transition = lambda p: setattr(p, "url", "https://foreign.test/challenge")
    monkeypatch.setattr(browser.asyncio, "sleep", immediate)
    result = recover(page, Solver())
    assert result["status"] == "unsupported_challenge"
    assert len(scripts) == 1  # Only the original recognized Yandex document was repaired.
