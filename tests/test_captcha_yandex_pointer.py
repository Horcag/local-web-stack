"""Recognized pointer parking restores hovered marker numbers without selection changes."""

import pytest

from service import captcha_yandex as yandex
from test_captcha_yandex import Page, Solver, arrive, run


def test_hovered_marker_number_is_restored_by_pointer_parking(monkeypatch):
    page, solver = Page(), Solver()
    page.on_submit = arrive
    page.on_click = lambda p: p.markers.__setitem__(-1, "")
    page.mouse.on_move = lambda p: p.markers.__setitem__(-1, str(len(p.markers)))

    async def sleep(_):
        pytest.fail("Parking the pointer restores the label without a readiness delay")

    monkeypatch.setattr(yandex.asyncio, "sleep", sleep)
    assert run(page, solver)["status"] == "solved"
    assert page.mouse.moves == [(0, 0), (0, 0)]
    assert len(solver.calls) == 1 and len(page.clicks) == 2 and page.submitted == 1


def test_first_corner_inside_image_selects_safe_other_corner():
    page = Page()
    page.boxes["main"].update(x=0, y=0)
    page.on_submit = arrive
    assert run(page)["status"] == "solved"
    assert page.mouse.moves == [(1279, 0), (1279, 0)]


def test_no_safe_corner_rejects_capture_without_mouse_move_or_model():
    page, solver = Page(), Solver()
    page.boxes["main"] = {"x": 0, "y": 0, "width": 1280, "height": 900}
    page.markers = ["1"]
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not page.mouse.moves and not solver.calls and not page.clicks


def test_zero_markers_do_not_move_pointer_before_model():
    page = Page()
    solver = Solver(answer=None)
    assert run(page, solver)["status"] == "empty_model_result"
    assert not page.mouse.moves


def test_pointer_parking_does_not_claim_preexisting_selection():
    page, solver = Page(), Solver()
    page.markers = [""]
    page.mouse.on_move = lambda p: p.markers.__setitem__(0, "1")
    assert run(page, solver)["status"] == "selection_present"
    assert page.mouse.moves == [(0, 0)] and not solver.calls and not page.clicks
