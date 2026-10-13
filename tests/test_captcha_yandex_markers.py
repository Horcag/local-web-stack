"""Only asynchronous blank labels receive a bounded marker readiness wait."""

import pytest

from service import captcha_yandex as yandex
from test_captcha_yandex import Page, Solver, arrive, run


def test_blank_marker_becomes_expected_number_before_capture(monkeypatch):
    page, solver = Page(), Solver()
    page.markers = [""]
    page.on_submit = arrive
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        page.markers[0] = "1"

    monkeypatch.setattr(yandex.asyncio, "sleep", sleep)
    assert run(page, solver)["status"] == "selection_present"
    assert sleeps == [0.05] and not solver.calls and not page.clicks


def test_persistent_blank_marker_times_out_without_model_or_clicks(monkeypatch):
    page, solver = Page(), Solver()
    page.markers = [""]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(yandex.asyncio, "sleep", sleep)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert sleeps == [0.05] * 10
    assert not solver.calls and not page.clicks


@pytest.mark.parametrize("labels", [["2"], ["1", "wrong"], ["", "1"]])
def test_nonblank_mismatched_marker_does_not_wait(monkeypatch, labels):
    page, solver = Page(), Solver()
    page.markers = labels

    async def sleep(_):
        pytest.fail("A nonblank mismatched label must fail immediately")

    monkeypatch.setattr(yandex.asyncio, "sleep", sleep)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls


def test_marker_count_change_during_readiness_is_rejected(monkeypatch):
    page, solver = Page(), Solver()
    page.markers = [""]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        page.markers = ["1", "2"]

    monkeypatch.setattr(yandex.asyncio, "sleep", sleep)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert sleeps == [0.05] and not solver.calls and not page.clicks


def test_blank_label_after_own_click_is_waited_without_new_model_call(monkeypatch):
    page, solver = Page(), Solver()
    page.on_submit = arrive
    page.on_click = lambda p: p.markers.__setitem__(-1, "")
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        page.markers[-1] = str(len(page.markers))

    monkeypatch.setattr(yandex.asyncio, "sleep", sleep)
    assert run(page, solver)["status"] == "solved"
    assert sleeps == [0.05, 0.05] and len(solver.calls) == 1


@pytest.mark.parametrize("markers", [["1"], ["1", "2", "3"]])
def test_preexisting_selection_never_reaches_model(markers):
    page, solver = Page(), Solver()
    page.markers = markers
    assert run(page, solver)["status"] == "selection_present"
    assert not solver.calls and not page.clicks and not page.submitted


def test_concurrent_marker_added_during_model_stops_all_clicks():
    page = Page()
    solver = Solver(action=lambda: page.markers.append("1"))
    assert run(page, solver)["status"] == "challenge_changed"
    assert len(solver.calls) == 1 and not page.clicks and not page.submitted


def test_concurrent_marker_added_after_own_click_stops_remaining_points():
    page = Page()
    page.on_click = lambda p: p.markers.append(str(len(p.markers) + 1))
    assert run(page)["status"] == "challenge_changed"
    assert len(page.clicks) == 1 and not page.submitted


def test_own_marker_deleted_before_next_point_is_rejected():
    page = Page()
    page.on_click = lambda p: p.markers.clear()
    assert run(page)["status"] == "challenge_changed"
    assert len(page.clicks) == 1 and not page.submitted


def test_own_marker_deleted_before_submit_is_rejected():
    page = Page()
    page.on_click = lambda p: p.markers.clear() if len(p.clicks) == 2 else None
    assert run(page)["status"] == "challenge_changed"
    assert len(page.clicks) == 2 and not page.submitted
