"""Offline browser state transitions for bounded CAPTCHA recovery."""

import asyncio

import pytest

from service import captcha_browser as browser
from service.captcha_solver import CaptchaSolverError, PNG_SIGNATURE

TARGET = "https://search.test/results?q=sample"
PNG_A = PNG_SIGNATURE + b"challenge-a"
PNG_B = PNG_SIGNATURE + b"challenge-b"


class Matches:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class Control:
    def __init__(self, page, kind, visible=True):
        self.page = page
        self.kind = kind
        self.visible = visible
        self.values = []

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name):
        assert self.kind == "checkbox" and name == "type"
        return "submit"

    async def screenshot(self, **kwargs):
        assert self.kind == "image"
        assert kwargs == {"type": "png", "timeout": 5000}
        return self.page.image

    async def fill(self, value, **kwargs):
        self.values.append(value)

    async def click(self, **kwargs):
        self.page.clicks.append(self.kind)
        transition = (
            self.page.checkbox_transition
            if self.kind == "checkbox"
            else self.page.submissions.pop(0)
            if self.page.submissions
            else None
        )
        if transition:
            transition(self.page)

    async def inner_text(self, **kwargs):
        return "Verify you are human" if self.page.gate else "Search results"


class Form:
    def __init__(self, page, *, image=True, field=True, submit=True, visible=True):
        self.controls = {
            browser.IMAGE_SELECTOR: [Control(page, "image", visible)] if image else [],
            browser.INPUT_SELECTOR: [Control(page, "captcha", visible)] if field else [],
            browser.SUBMIT_SELECTOR: [Control(page, "submit", visible)] if submit else [],
        }
        self.unrelated_field = Control(page, "search")

    def locator(self, selector):
        return Matches(self.controls.get(selector, []))

    @property
    def answers(self):
        return [
            value for control in self.controls[browser.INPUT_SELECTOR] for value in control.values
        ]


class Page:
    def __init__(self, *, gate=True, url="https://search.test/captcha", checkbox=False):
        self.gate = gate
        self.url = url
        self.image = PNG_A
        self.clicks = []
        self.forms = [Form(self)]
        self.checkbox = Control(self, "checkbox") if checkbox else None
        self.checkbox_transition = None
        self.submissions = []
        self.closed = False

    def locator(self, selector):
        if selector == "body":
            return Control(self, "body")
        if selector == "form":
            return Matches(self.forms)
        if selector == "#advanced-captcha-form":
            return Matches([])
        if selector == ".CheckboxCaptcha-Button":
            return Matches([self.checkbox] if self.checkbox else [])
        raise AssertionError(f"Unexpected page selector: {selector}")

    async def title(self):
        return "Verify you are human" if self.gate else "Search results"

    async def screenshot(self, **kwargs):
        raise AssertionError("Whole-page screenshots must never reach the model")

    async def close(self):
        self.closed = True
        raise AssertionError("The caller owns the page lifecycle")


class Solver:
    def __init__(self, *, enabled=True, configured=True, answer="1234", action=None):
        self.enabled = enabled
        self.configured = configured
        self.answer = answer
        self.action = action
        self.images = []

    async def solve(self, image):
        self.images.append(image)
        if self.action:
            self.action()
        return self.answer


def arrive(page, url=TARGET):
    page.gate = False
    page.url = url


def recover(page, solver):
    receipt = asyncio.run(browser.recover_captcha(page, solver, TARGET))
    return {
        key: value
        for key, value in receipt.items()
        if key not in {"challenge_observed", "final_status_code"}
    }


def assert_untouched(page):
    assert not page.clicks
    assert all(not form.answers and not form.unrelated_field.values for form in page.forms)
    assert not page.closed


@pytest.fixture(autouse=True)
def immediate_wait(monkeypatch):
    async def wait(page):
        return None

    monkeypatch.setattr(browser, "_wait_after_submit", wait)


@pytest.mark.parametrize(
    "gate,enabled,configured,status",
    [
        (True, False, True, "disabled"),
        (False, True, True, "not_needed"),
        (True, True, False, "unconfigured"),
    ],
)
def test_inactive_recovery_never_sends_images_or_mutates_page(gate, enabled, configured, status):
    page = Page(gate=gate)
    solver = Solver(enabled=enabled, configured=configured)
    assert recover(page, solver) == {"attempts": 0, "status": status}
    assert not solver.images
    assert_untouched(page)


@pytest.mark.parametrize("missing", ["image", "field", "submit", "hidden", "split"])
def test_unsupported_or_separate_form_controls_never_reach_model(missing):
    page = Page()
    if missing == "split":
        page.forms = [Form(page, field=False), Form(page, image=False)]
    elif missing == "hidden":
        page.forms = [Form(page, visible=False)]
    else:
        page.forms = [Form(page, **{missing: False})]
    solver = Solver()
    assert recover(page, solver) == {"attempts": 0, "status": "unsupported_challenge"}
    assert not solver.images
    assert_untouched(page)


def test_success_submits_only_captcha_answer_and_cropped_png():
    page = Page()
    page.forms.insert(0, Form(page, image=False))
    page.submissions = [arrive]
    solver = Solver(answer="два слова")
    assert recover(page, solver) == {"attempts": 1, "status": "solved"}
    assert solver.images == [PNG_A]
    assert page.forms[1].answers == ["два слова"]
    assert not page.forms[0].answers
    assert all(not form.unrelated_field.values for form in page.forms)
    assert page.clicks == ["submit"]
    assert not page.closed


@pytest.mark.parametrize("change", ["image", "url", "gate", "form"])
def test_challenge_changed_during_model_call_is_not_filled_or_submitted(change):
    page = Page()

    def mutate():
        if change == "image":
            page.image = PNG_B
        elif change == "url":
            page.url += "?replacement=1"
        elif change == "gate":
            page.gate = False
        else:
            page.forms = [Form(page, image=False)]

    solver = Solver(action=mutate)
    assert recover(page, solver) == {"attempts": 1, "status": "challenge_changed"}
    assert solver.images == [PNG_A]
    assert_untouched(page)


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/results?q=sample",
        "https://search.test/home",
        TARGET.replace("sample", "bad"),
        TARGET.replace("https:", "http:"),
        TARGET.replace("search.test", "search.test:444"),
        TARGET + "&q=sample",
    ],
)
def test_gate_disappearing_at_wrong_target_is_not_success(url):
    page = Page()
    page.submissions = [lambda current: arrive(current, url)]
    assert recover(page, Solver()) == {"attempts": 1, "status": "unexpected_page"}
    assert page.clicks == ["submit"]


def test_rejected_unchanged_image_is_not_sent_to_model_twice():
    page = Page()
    solver = Solver()
    assert recover(page, solver) == {"attempts": 1, "status": "unchanged_challenge"}
    assert solver.images == [PNG_A]
    assert page.forms[0].answers == ["1234"]
    assert page.clicks == ["submit"]


@pytest.mark.parametrize("second_succeeds", [False, True])
def test_changed_image_allows_exactly_two_attempts(second_succeeds):
    page = Page()
    page.submissions = [lambda current: setattr(current, "image", PNG_B)]
    if second_succeeds:
        page.submissions.append(arrive)
    solver = Solver()
    assert recover(page, solver) == {
        "attempts": 2,
        "status": "solved" if second_succeeds else "rejected",
    }
    assert solver.images == [PNG_A, PNG_B]
    assert page.forms[0].answers == ["1234", "1234"]
    assert page.clicks == ["submit", "submit"]


def test_empty_answer_does_not_fill_or_submit():
    page = Page()
    assert recover(page, Solver(answer=None)) == {"attempts": 1, "status": "empty_model_result"}
    assert_untouched(page)


def test_solver_error_receipt_contains_only_sanitized_reason():
    page = Page()

    def fail():
        raise CaptchaSolverError("private-api-key-and-gateway-response")

    receipt = recover(page, Solver(action=fail))
    assert receipt == {"attempts": 1, "status": "gateway_invalid_response"}
    assert_untouched(page)


def test_deadline_cancels_pending_model_without_submitting(monkeypatch):
    page = Page()
    cancelled = []

    class PendingSolver(Solver):
        async def solve(self, image):
            self.images.append(image)
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

    monkeypatch.setattr(browser, "RECOVERY_TIMEOUT_SECONDS", 0.01)
    solver = PendingSolver()
    assert recover(page, solver) == {"attempts": 1, "status": "deadline"}
    assert solver.images == [PNG_A]
    assert cancelled == [True]
    assert_untouched(page)


def test_caller_cancellation_propagates_without_submitting():
    page = Page()

    def cancel():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        recover(page, Solver(action=cancel))
    assert_untouched(page)


@pytest.mark.parametrize("outcome", ["target", "unsupported", "wrong_target"])
def test_yandex_checkbox_transition_does_not_send_model_without_text_form(outcome):
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    if outcome == "unsupported":
        page.forms = []
    else:
        page.checkbox_transition = lambda current: arrive(
            current, TARGET if outcome == "target" else "https://search.test/home"
        )
    solver = Solver()
    status = {
        "target": "solved",
        "unsupported": "unsupported_challenge",
        "wrong_target": "unexpected_page",
    }[outcome]
    assert recover(page, solver) == {"attempts": 0, "status": status}
    assert not solver.images
    assert page.clicks == ["checkbox"]
    assert all(not form.answers for form in page.forms)


@pytest.mark.parametrize("gate,url", [(False, "https://yandex.ru/"), (True, "https://other.test/")])
def test_checkbox_is_ignored_outside_recognized_yandex_gate(gate, url):
    page = Page(gate=gate, url=url, checkbox=True)
    page.forms = []
    solver = Solver()
    assert recover(page, solver)["status"] == ("unsupported_challenge" if gate else "not_needed")
    assert not solver.images
    assert_untouched(page)


def test_browser_failure_is_sanitized_without_filling_or_submitting():
    page = Page()

    def fail():
        raise RuntimeError("private-page-content")

    receipt = recover(page, Solver(action=fail))
    assert receipt == {"attempts": 1, "status": "browser_error"}
    assert_untouched(page)


def test_yandex_checkbox_can_reveal_text_challenge_then_submit():
    page = Page(url="https://yandex.ru/showcaptcha", checkbox=True)
    page.forms = []
    page.checkbox_transition = lambda current: setattr(current, "forms", [Form(current)])
    page.submissions = [arrive]
    solver = Solver()
    assert recover(page, solver) == {"attempts": 1, "status": "solved"}
    assert solver.images == [PNG_A]
    assert page.forms[0].answers == ["1234"]
    assert page.clicks == ["checkbox", "submit"]
    assert not page.closed


@pytest.mark.parametrize("outcome", ["browser_error", "deadline"])
def test_observed_gate_survives_failure_after_submission(outcome, monkeypatch):
    page = Page()
    page.submissions = [arrive]

    async def fail_after_submit(current):
        if outcome == "browser_error":
            raise RuntimeError("private page")
        await asyncio.Future()

    monkeypatch.setattr(browser, "_wait_after_submit", fail_after_submit)
    monkeypatch.setattr(browser, "RECOVERY_TIMEOUT_SECONDS", 0.01)
    receipt = asyncio.run(browser.recover_captcha(page, Solver(), TARGET))
    assert receipt == {"attempts": 1, "status": outcome, "challenge_observed": True}
    assert page.clicks == ["submit"]
    assert not page.gate


def test_ordinary_page_receipt_does_not_claim_gate_observed():
    page = Page(gate=False)
    assert asyncio.run(browser.recover_captcha(page, Solver(), TARGET)) == {
        "attempts": 0,
        "status": "not_needed",
    }
    assert_untouched(page)
