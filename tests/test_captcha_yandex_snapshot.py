"""Reject mixed or clipped image captures without scrolling the caller's page."""

import pytest

from test_captcha_yandex import Element, Page, Solver, run


@pytest.mark.parametrize("kind", ["main", "canvas"])
@pytest.mark.parametrize("change", [{"x": -1}, {"y": -1}, {"x": 1279}, {"y": 899}])
def test_outside_viewport_does_not_capture_or_request_model(kind, change):
    page, solver = Page(), Solver()
    page.boxes[kind].update(change)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not page.screenshots and not solver.calls and not page.clicks


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 213, "height": 121},
        None,
        {"width": 0, "height": 900},
        {"width": float("nan"), "height": 900},
    ],
)
def test_small_or_invalid_viewport_cannot_capture(monkeypatch, viewport):
    page, solver = Page(), Solver()
    page.boxes["main"].update(width=320, height=180)

    async def evaluate(_):
        return viewport

    monkeypatch.setattr(page, "evaluate", evaluate)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not page.screenshots and not solver.calls


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.boxes["main"].update(y=0),
        lambda p: p.boxes["canvas"].update(y=0),
        lambda p: p.boxes.update(wrapper=dict(p.boxes["main"], x=11)),
        lambda p: p.sources.update(main="/captchaimage?new=1"),
        lambda p: p.sources.update(refimg="/captchaimage?ref=new"),
        lambda p: setattr(p, "instruction", "Changed during capture"),
        lambda p: setattr(p, "url", "https://evil.test/"),
    ],
)
def test_screenshot_mutation_is_rejected_before_model(monkeypatch, change):
    page, solver = Page(), Solver()
    original = Element.screenshot

    async def screenshot(element, **kwargs):
        captured = await original(element, **kwargs)
        if element.kind == "main":
            change(element.page)
        return captured

    monkeypatch.setattr(Element, "screenshot", screenshot)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls and not page.clicks


def test_viewport_change_during_capture_cannot_request_model(monkeypatch):
    page, solver = Page(), Solver()
    calls = 0

    async def evaluate(_):
        nonlocal calls
        calls += 1
        return {"width": 1280 if calls == 1 else 1290, "height": 900}

    monkeypatch.setattr(page, "evaluate", evaluate)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls
