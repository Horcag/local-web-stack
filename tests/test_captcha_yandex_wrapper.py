"""Same-form click target alignment regressions."""

import pytest

from test_captcha_yandex import Page, Solver, run


@pytest.mark.parametrize("state", ["hidden", "missing", "misaligned"])
def test_wrapper_must_be_visible_unique_and_aligned(state):
    page, solver = Page(), Solver()
    if state == "hidden":
        page.hidden.add("wrapper")
    elif state == "missing":
        page.missing.add("wrapper")
    else:
        page.boxes["wrapper"] = dict(page.boxes["main"], x=11)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls and not page.clicks


def test_wrapper_movement_after_model_result_is_rejected():
    page = Page()
    solver = Solver(action=lambda: page.boxes.update(wrapper=dict(page.boxes["main"], y=21)))
    assert run(page, solver)["status"] == "challenge_changed"
    assert not page.clicks and not page.submitted


@pytest.mark.parametrize("markers", [["unexpected"], ["2"], [str(i) for i in range(1, 10)]])
def test_unknown_marker_content_cannot_be_hidden(markers):
    page, solver = Page(), Solver()
    page.markers = markers
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls and not page.clicks


def test_underlying_image_change_after_own_marker_still_rejected():
    from test_captcha_yandex import png

    page = Page()
    page.on_click = lambda p: setattr(p, "main_png", png(payload=b"changed underneath marker"))
    assert run(page)["status"] == "challenge_changed"
    assert len(page.clicks) == len(page.markers) == 1 and not page.submitted
