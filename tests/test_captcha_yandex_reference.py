"""Native reference bitmap capture from a tainted rendered canvas."""

import pytest

from service import captcha_yandex as yandex
from service.captcha_solver import CaptchaSolverError
from test_captcha_yandex import Element, Page, Solver, arrive, png, run


def test_tainted_canvas_is_captured_at_native_resolution_with_scoped_style(monkeypatch):
    page, solver = Page(), Solver()
    page.tainted = True
    page.on_submit = arrive
    captured = []
    original = Element.screenshot

    async def screenshot(element, **kwargs):
        if element.kind == "canvas":
            captured.append(kwargs)
        return await original(element, **kwargs)

    monkeypatch.setattr(Element, "screenshot", screenshot)
    assert run(page, solver)["status"] == "solved"
    reference = solver.calls[0][0][1]
    assert yandex._dimensions(reference) == (480, 80)
    assert page.boxes["canvas"]["width"] == 120 and page.boxes["canvas"]["height"] == 20
    assert set(page.screenshots) == {"main", "canvas"}
    assert all(
        shot["style"].startswith(
            "#advanced-captcha-form .TaskImage.TaskImage_painted > canvas.TaskImage-Canvas {"
        )
        and "width:480px!important;height:80px!important" in shot["style"]
        and "position:fixed!important" in shot["style"]
        and "background:#202020!important" in shot["style"]
        for shot in captured
    )


@pytest.mark.parametrize(
    "dimensions",
    [
        {"width": 0, "height": 80},
        {"width": 4097, "height": 80},
        {"width": True, "height": 80},
        {"width": 1400, "height": 80},
        None,
    ],
)
def test_invalid_or_nonfitting_native_reference_is_not_captured(monkeypatch, dimensions):
    page, solver = Page(), Solver()
    page.tainted = True
    original = Element.evaluate

    async def evaluate(element, script):
        if script == yandex._CANVAS_DIMENSIONS:
            return dimensions
        return await original(element, script)

    monkeypatch.setattr(Element, "evaluate", evaluate)
    with pytest.raises(CaptchaSolverError, match="invalid_image"):
        run(page, solver)
    assert "canvas" not in page.screenshots and not solver.calls


def test_clipped_reference_screenshot_is_rejected(monkeypatch):
    page, solver = Page(), Solver()
    page.tainted = True
    original = Element.screenshot

    async def screenshot(element, **kwargs):
        if element.kind == "canvas":
            return png(120, 20, b"clipped")
        return await original(element, **kwargs)

    monkeypatch.setattr(Element, "screenshot", screenshot)
    with pytest.raises(CaptchaSolverError, match="invalid_image"):
        run(page, solver)
    assert not solver.calls


def test_reference_layout_not_restored_is_rejected(monkeypatch):
    page, solver = Page(), Solver()
    page.tainted = True
    original = Element.screenshot

    async def screenshot(element, **kwargs):
        image = await original(element, **kwargs)
        if element.kind == "canvas":
            page.boxes["canvas"].update(width=480, height=80)
        return image

    monkeypatch.setattr(Element, "screenshot", screenshot)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls and not page.clicks
