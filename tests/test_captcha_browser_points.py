"""Offline ordered-point CAPTCHA browser regression cases."""

import asyncio
import json
from types import SimpleNamespace
import struct

import pytest

from service import captcha_browser as browser
from service.captcha_solver import PNG_SIGNATURE
from test_captcha_browser import Control, Form, Page, Solver, TARGET, arrive, recover

INSTRUCTIONS = "Click the cat, then the dog"
PLAN = {"points": [[80, 20], [10, 60]]}


def png(width=100, height=80, content=b"first"):
    return (
        PNG_SIGNATURE
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + content
    )


class PuzzleControl(Control):
    async def get_attribute(self, name, **kwargs):
        assert name == "src"
        return self.page.image_src

    async def screenshot(self, **kwargs):
        assert self.kind == "image"
        assert kwargs == {"type": "png", "scale": "css", "timeout": 5000}
        return self.page.image

    async def bounding_box(self):
        return self.page.box.copy() if self.page.box else None

    async def inner_text(self, **kwargs):
        return self.page.instructions

    async def click(self, **kwargs):
        if self.kind != "image":
            await super().click(**kwargs)
            return
        self.page.points.append(kwargs["position"])
        self.page.clicks.append("image")
        if self.page.point_transition:
            self.page.point_transition(self.page)


class PuzzlePage(Page):
    def __init__(self):
        super().__init__()
        self.image = png()
        self.image_src = "/captcha/image?id=first"
        self.instructions = INSTRUCTIONS
        self.box = {"x": 25, "y": 40, "width": 200, "height": 40}
        self.points = []
        self.point_transition = None
        form = Form(self, field=False)
        form.controls[browser.IMAGE_SELECTOR] = [PuzzleControl(self, "image")]
        form.controls[browser.INSTRUCTION_SELECTOR] = [PuzzleControl(self, "instruction")]
        self.forms = [form]


class PuzzleSolver(Solver):
    def __init__(self, plan=PLAN, **kwargs):
        super().__init__(**kwargs)
        self.raw = json.dumps(plan)
        self.prompts = []

    async def solve(self, image):
        raise AssertionError("Text OCR is not appropriate for ordered-point puzzles")

    async def complete_image(self, image, prompt):
        self.images.append(image)
        self.prompts.append(prompt)
        if self.action:
            self.action()
        return self.raw


@pytest.fixture(autouse=True)
def immediate_wait(monkeypatch):
    async def wait(page):
        return None

    monkeypatch.setattr(browser, "_wait_after_submit", wait)


def assert_no_clicks(page):
    assert page.points == []
    assert page.clicks == []
    assert not page.closed
    assert all(not form.unrelated_field.values for form in page.forms)


def test_ordered_image_points_scale_to_css_and_preserve_order():
    page = PuzzlePage()
    page.submissions = [arrive]
    solver = PuzzleSolver()
    receipt = recover(page, solver)
    assert receipt == {"attempts": 1, "kind": "ordered_points", "status": "solved"}
    assert solver.images == [png()]
    assert INSTRUCTIONS in solver.prompts[0]
    assert "100 pixels wide and 80 pixels high" in solver.prompts[0]
    assert page.points == [{"x": 160.0, "y": 10.0}, {"x": 20.0, "y": 30.0}]
    assert page.clicks == ["image", "image", "submit"]
    assert not page.closed
    assert all(not form.answers and not form.unrelated_field.values for form in page.forms)


@pytest.mark.parametrize("missing", ["image", "instructions", "submit", "split", "image_source"])
def test_missing_or_separate_puzzle_controls_do_not_send_model(missing):
    page = PuzzlePage()
    form = page.forms[0]
    selector = {
        "image": browser.IMAGE_SELECTOR,
        "instructions": browser.INSTRUCTION_SELECTOR,
        "submit": browser.SUBMIT_SELECTOR,
    }
    if missing == "image_source":
        page.image_src = None
    elif missing == "split":
        other = Form(page, image=False, field=False)
        other.controls[browser.INSTRUCTION_SELECTOR] = form.controls.pop(
            browser.INSTRUCTION_SELECTOR
        )
        page.forms.append(other)
    else:
        form.controls[selector[missing]] = []
    solver = PuzzleSolver()
    assert recover(page, solver) == {"attempts": 0, "status": "unsupported_challenge"}
    assert not solver.images
    assert_no_clicks(page)


@pytest.mark.parametrize(
    "plan,status",
    [
        ({"points": []}, "unsupported_challenge"),
        ({"points": [[100, 0]]}, "invalid_answer"),
        ({"points": [[0, 80]]}, "invalid_answer"),
        ({"points": [[-1, 0]]}, "invalid_answer"),
        ({"points": [[1.5, 2]]}, "invalid_answer"),
        ({"points": [[True, 2]]}, "invalid_answer"),
        ({"points": [[1, 2], [1, 2]]}, "invalid_answer"),
        ({"points": [[1, 2]], "command": "submit"}, "invalid_answer"),
    ],
)
def test_invalid_plan_never_clicks_or_submits(plan, status):
    page = PuzzlePage()
    solver = PuzzleSolver(plan)
    assert recover(page, solver) == {"attempts": 1, "kind": "ordered_points", "status": status}
    assert solver.images == [png()]
    assert_no_clicks(page)


@pytest.mark.parametrize(
    "change", ["image", "image_src", "instruction", "url", "gate", "missing_box", "moved_box"]
)
def test_stale_puzzle_after_model_never_clicks_or_submits(change):
    page = PuzzlePage()

    def mutate():
        if change == "image":
            page.image = png(content=b"replacement")
        elif change == "image_src":
            page.image_src = "/captcha/image?id=replacement"
        elif change == "instruction":
            page.instructions = "Click the dog, then the cat"
        elif change == "url":
            page.url += "?new=1"
        elif change == "gate":
            page.gate = False
        elif change == "moved_box":
            page.box = page.box | {"x": 30}
        else:
            page.box = None

    solver = PuzzleSolver(action=mutate)
    assert recover(page, solver) == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": "challenge_changed",
    }
    assert solver.images == [png()]
    assert_no_clicks(page)


@pytest.mark.parametrize("change", ["bbox", "url", "image_src"])
def test_identity_drift_between_points_stops_remaining_clicks_and_submit(change):
    page = PuzzlePage()

    def mutate(current):
        if change == "bbox":
            current.box = current.box | {"width": 250}
        elif change == "image_src":
            current.image_src = "/captcha/image?id=replacement"
        else:
            current.url += "?new=1"

    page.point_transition = mutate
    assert recover(page, PuzzleSolver()) == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": "challenge_changed",
    }
    assert page.points == [{"x": 160.0, "y": 10.0}]
    assert page.clicks == ["image"]


def test_wrong_target_after_puzzle_submission_is_not_solved():
    page = PuzzlePage()
    page.submissions = [lambda current: arrive(current, "https://other.test/")]
    assert recover(page, PuzzleSolver()) == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": "unexpected_page",
    }
    assert page.clicks == ["image", "image", "submit"]


def test_unchanged_rejected_puzzle_uses_one_model_attempt():
    page = PuzzlePage()
    solver = PuzzleSolver()
    assert recover(page, solver) == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": "unchanged_challenge",
    }
    assert solver.images == [png()]
    assert page.clicks == ["image", "image", "submit"]


@pytest.mark.parametrize("second_succeeds", [False, True])
def test_changed_puzzle_allows_at_most_two_model_attempts(second_succeeds):
    page = PuzzlePage()
    replacement = png(content=b"replacement")
    page.submissions = [lambda current: setattr(current, "image", replacement)]
    if second_succeeds:
        page.submissions.append(arrive)
    solver = PuzzleSolver()
    assert recover(page, solver) == {
        "attempts": 2,
        "kind": "ordered_points",
        "status": "solved" if second_succeeds else "rejected",
    }
    assert solver.images == [png(), replacement]
    assert page.clicks == ["image", "image", "submit"] * 2
    assert page.points == [{"x": 160.0, "y": 10.0}, {"x": 20.0, "y": 30.0}] * 2


@pytest.mark.parametrize("change", ["image_src", "instruction", "bbox", "url"])
def test_final_point_challenge_change_prevents_submit(change):
    page = PuzzlePage()

    def mutate(current):
        if len(current.points) != 2:
            return
        if change == "image_src":
            current.image_src = "/captcha/image?id=replacement"
        elif change == "instruction":
            current.instructions = "Click the dog, then the cat"
        elif change == "bbox":
            current.box = current.box | {"x": 30}
        elif change == "image_src":
            current.image_src = "/captcha/image?id=replacement"
        else:
            current.url += "?new=1"

    page.point_transition = mutate
    assert recover(page, PuzzleSolver()) == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": "challenge_changed",
    }
    assert page.clicks == ["image", "image"]


@pytest.mark.parametrize("target_matches", [False, True])
def test_final_point_auto_navigation_checks_target_without_submit(target_matches):
    page = PuzzlePage()

    def navigate(current):
        if len(current.points) == 2:
            if target_matches:
                arrive(current)
            else:
                arrive(current, "https://other.test/")

    page.point_transition = navigate
    assert recover(page, PuzzleSolver()) == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": "solved" if target_matches else "unexpected_page",
    }
    assert page.clicks == ["image", "image"]


def test_selection_markers_change_pixels_without_invalidating_same_image_source():
    page = PuzzlePage()
    page.point_transition = lambda current: setattr(current, "image", png(content=b"selected"))
    page.submissions = [arrive]
    solver = PuzzleSolver()
    assert recover(page, solver) == {"attempts": 1, "kind": "ordered_points", "status": "solved"}
    assert solver.images == [png()]
    assert page.clicks == ["image", "image", "submit"]


class ResponsePage(PuzzlePage):
    def __init__(self):
        super().__init__()
        self.main_frame = object()
        self.listeners = []

    def on(self, event, listener):
        assert event == "response"
        self.listeners.append(listener)

    def remove_listener(self, event, listener):
        assert event == "response"
        self.listeners.remove(listener)

    def response(self, status, *, main=True, navigation=True):
        request = SimpleNamespace(
            frame=self.main_frame if main else object(),
            is_navigation_request=lambda: navigation,
        )
        for listener in self.listeners[:]:
            listener(SimpleNamespace(status=status, request=request))


@pytest.mark.parametrize("outcome", ["solved", "browser_error", "deadline"])
def test_final_mainframe_navigation_status_retained_and_listener_removed(outcome, monkeypatch):
    page = ResponsePage()

    def navigate(current):
        current.response(302)
        current.response(200)
        current.response(403, main=False)
        current.response(503, navigation=False)
        arrive(current)

    page.submissions = [navigate]

    async def wait(current):
        if outcome == "browser_error":
            raise RuntimeError("private page")
        if outcome == "deadline":
            await asyncio.Future()

    monkeypatch.setattr(browser, "_wait_after_submit", wait)
    monkeypatch.setattr(browser, "RECOVERY_TIMEOUT_SECONDS", 0.01)
    receipt = asyncio.run(browser.recover_captcha(page, PuzzleSolver(), TARGET))
    assert receipt == {
        "attempts": 1,
        "kind": "ordered_points",
        "status": outcome,
        "challenge_observed": True,
        "final_status_code": 200,
    }
    assert not page.listeners
    assert page.clicks == ["image", "image", "submit"]


@pytest.mark.parametrize(
    "target,actual,status",
    [
        (TARGET + "&empty=", TARGET, "unexpected_page"),
        (TARGET + "&empty=", TARGET + "&empty=", "solved"),
        (TARGET + "&q=sample", TARGET, "unexpected_page"),
        (TARGET + "&q=sample", TARGET + "&q=sample", "solved"),
        (TARGET, TARGET.replace("search.test", "search.test:443"), "solved"),
    ],
)
def test_target_query_blanks_and_multiplicity_are_preserved(target, actual, status):
    page = PuzzlePage()
    page.submissions = [lambda current: arrive(current, actual)]
    receipt = asyncio.run(browser.recover_captcha(page, PuzzleSolver(), target))
    assert receipt["status"] == status
    assert receipt["challenge_observed"] is True
