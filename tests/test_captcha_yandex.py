"""Offline Yandex silhouette contract; no external model or browser I/O."""

import asyncio
import base64
import struct

import pytest

from service import captcha_yandex as yandex
from service.captcha_solver import PNG_SIGNATURE, CaptchaSolverError
from captcha_yandex_mouse_fixture import Mouse

TARGET = "https://yandex.ru/search/?text=example"


def png(width=320, height=180, payload=b"main"):
    return (
        PNG_SIGNATURE
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + payload
    )


class Matches:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    async def inner_text(self, **kwargs):
        return await self.items[0].inner_text(**kwargs)


class Element:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    def locator(self, selector):
        return self.page.scoped(self.kind, selector)

    async def is_visible(self):
        return self.kind not in self.page.hidden

    async def get_attribute(self, name):
        assert name == "src"
        return self.page.sources[self.kind]

    async def inner_text(self, **kwargs):
        if self.kind == "body":
            return "Press in the following order: captcha" if self.page.gate else "Results"
        if self.kind.startswith("marker"):
            return self.page.markers[int(self.kind[6:])]
        return self.page.instruction

    async def bounding_box(self):
        box = self.page.boxes.get(self.kind)
        if self.kind == "wrapper" and box is None:
            box = self.page.boxes["main"]
        return dict(box) if box else None

    async def screenshot(self, **kwargs):
        expected = {"type": "png", "scale": "css", "timeout": 5000}
        if self.kind == "main":
            expected["style"] = yandex._MARKER_STYLE
        else:
            expected["style"] = yandex._reference_style(480, 80)
        assert kwargs == expected
        self.page.screenshots.append(self.kind)
        return self.page.main_png if self.kind == "main" else self.page.ref_png

    async def evaluate(self, script):
        assert self.kind == "canvas"
        if script == yandex._CANVAS_DIMENSIONS:
            return {"width": 480, "height": 80}
        if self.page.tainted:
            return None
        return "data:image/png;base64," + base64.b64encode(self.page.ref_png).decode()

    async def click(self, **kwargs):
        if self.kind == "submit":
            self.page.submitted += 1
            if self.page.on_submit:
                self.page.on_submit(self.page)
        else:
            assert self.kind == "wrapper"
            self.page.clicks.append(kwargs["position"])
            self.page.markers.append(str(len(self.page.markers) + 1))
            if self.page.on_click:
                self.page.on_click(self.page)


class Page:
    def __init__(self):
        self.url = "https://yandex.ru/showcaptcha?key=private"
        self.gate = True
        self.sources = {"main": "/captchaimage?id=1", "refimg": "/captchaimage?ref=1"}
        self.instruction = "Press in the following order:"
        self.main_png, self.ref_png = png(), png(480, 80, b"order")
        self.boxes = {
            "main": {"x": 10, "y": 20, "width": 160, "height": 90},
            "canvas": {"x": 10, "y": 5, "width": 120, "height": 20},
        }
        self.hidden, self.missing = set(), set()
        self.tainted = False
        self.clicks, self.screenshots, self.markers = [], [], []
        self.submitted = 0
        self.on_submit = self.on_click = None
        self.mouse = Mouse(self)

    def locator(self, selector):
        return self.scoped("page", selector)

    def scoped(self, scope, selector):
        if scope == "wrapper" and selector == ":scope > " + yandex.MARKER:
            return Matches([Element(self, f"marker{i}") for i in range(len(self.markers))])
        mapping = {
            ("page", yandex.FORM): "form",
            ("page", "body"): "body",
            ("form", yandex.SILHOUETTE): "silhouette",
            ("form", yandex.MAIN): "main",
            ("form", yandex.CLICK_TARGET): "wrapper",
            ("form", yandex.INSTRUCTION): "instruction",
            ("form", yandex.REFERENCE): "reference",
            ("form", yandex.SUBMIT): "submit",
            ("reference", yandex.REFERENCE_IMAGE): "refimg",
            ("reference", yandex.REFERENCE_CANVAS): "canvas",
        }
        kind = mapping.get((scope, selector))
        if kind is None or kind in self.missing or (kind == "form" and not self.gate):
            return Matches([])
        return Matches([Element(self, kind)])

    async def evaluate(self, script):
        assert script == "() => ({width: innerWidth, height: innerHeight})"
        return {"width": 1280, "height": 900}

    async def title(self):
        return "CAPTCHA" if self.gate else "Results"


class Solver:
    def __init__(self, answer='{"points":[[100,60],[200,120]]}', action=None):
        self.answer, self.action = answer, action
        self.calls = []

    async def complete_images(self, images, prompt):
        self.calls.append((images, prompt))
        if self.action:
            self.action()
        return self.answer


def run(page, solver=None, receipt=None):
    return asyncio.run(
        yandex.recover_yandex_points(
            page, solver or Solver(), TARGET, receipt or {"attempts": 0}, lambda a, b: a == b
        )
    )


def arrive(page):
    page.gate = False
    page.url = TARGET


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def immediate(_):
        pass

    monkeypatch.setattr(yandex.asyncio, "sleep", immediate)


def test_two_images_ordered_clicks_are_scaled_to_main_image():
    page, solver = Page(), Solver()
    page.hidden.add("refimg")  # Hidden source image is allowed when the canvas is rendered.
    page.on_submit = arrive
    result = run(page, solver)
    assert result == {"attempts": 1, "kind": "yandex_silhouette", "status": "solved"}
    assert page.clicks == [{"x": 50, "y": 30}, {"x": 100, "y": 60}]
    assert page.submitted == 1
    images, prompt = solver.calls[0]
    assert images == [page.main_png, page.ref_png]
    assert "FIRST image" in prompt and "SECOND image" in prompt
    assert "IGNORE the background illustration" in prompt
    assert "including rotated symbols" in prompt and "left to right" in prompt
    assert "Do not describe either image or provide prose" in prompt
    assert "width=320, height=180" in prompt and len(prompt) <= 2000
    assert set(page.screenshots) == {"main"}
    assert "private" not in str(result)


@pytest.mark.parametrize("host", ["evil.test", "yandex.ru.evil.test", "images.yandex.ru"])
def test_foreign_host_does_not_claim_challenge(host):
    page, solver = Page(), Solver()
    page.url = f"https://{host}/showcaptcha"
    assert run(page, solver) is None
    assert not solver.calls and not page.clicks


@pytest.mark.parametrize("missing", ["form", "silhouette"])
def test_unrecognized_form_returns_none(missing):
    page, solver = Page(), Solver()
    page.missing.add(missing)
    assert run(page, solver) is None
    assert not solver.calls


@pytest.mark.parametrize("kind", ["form", "main", "canvas", "instruction", "submit", "reference"])
def test_hidden_required_control_cannot_be_submitted(kind):
    page, solver = Page(), Solver()
    page.hidden.add(kind)
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls and not page.clicks and not page.submitted


def test_reference_from_other_form_is_not_accepted():
    page, solver = Page(), Solver()
    page.missing.add("reference")
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.sources.update(main="/captchaimage?id=2"),
        lambda p: p.sources.update(refimg="/captchaimage?ref=2"),
        lambda p: setattr(p, "main_png", png(payload=b"new")),
        lambda p: setattr(p, "ref_png", png(480, 80, b"changed")),
        lambda p: setattr(p, "instruction", "New order"),
        lambda p: p.boxes["main"].update(x=50),
        lambda p: p.boxes["canvas"].update(width=90),
        lambda p: setattr(p, "url", "https://evil.test/"),
    ],
)
def test_stale_model_answer_never_clicks(change):
    page = Page()
    assert run(page, Solver(action=lambda: change(page)))["status"] == "challenge_changed"
    assert not page.clicks and not page.submitted


def test_reference_change_between_clicks_stops_remaining_points():
    page = Page()
    page.on_click = lambda p: setattr(p, "ref_png", png(480, 80, b"new"))
    assert run(page)["status"] == "challenge_changed"
    assert len(page.clicks) == 1 and page.submitted == 0


@pytest.mark.parametrize(
    "answer",
    [
        '{"points":[[320,0]]}',
        '{"points":[[-1,0]]}',
        '{"points":[[1.5,2]]}',
        '{"points":[[1,2],[1,2]]}',
        "bad",
    ],
)
def test_bad_model_coordinates_never_click(answer):
    page = Page()
    with pytest.raises(CaptchaSolverError, match="invalid_answer"):
        run(page, Solver(answer))
    assert not page.clicks and not page.submitted


@pytest.mark.parametrize("answer", [None, '{"points":[]}'])
def test_empty_model_answer_cannot_solve(answer):
    page = Page()
    assert run(page, Solver(answer))["status"] == "empty_model_result"
    assert not page.clicks


def test_unchanged_rejected_challenge_is_not_sent_again():
    page, solver = Page(), Solver()
    assert run(page, solver)["status"] == "unchanged_challenge"
    assert len(solver.calls) == 1 and page.submitted == 1


def test_two_changed_challenges_are_maximum():
    page, solver = Page(), Solver()
    page.on_submit = lambda p: (
        setattr(p, "main_png", png(payload=str(p.submitted).encode())),
        p.markers.clear(),
    )
    assert run(page, solver)["status"] == "rejected"
    assert len(solver.calls) == page.submitted == 2


def test_gate_disappearance_at_foreign_destination_is_not_success():
    page = Page()
    page.on_submit = lambda p: (setattr(p, "gate", False), setattr(p, "url", "https://evil.test/"))
    assert run(page)["status"] == "unexpected_page"


def test_target_navigation_with_gate_still_present_is_not_success():
    page = Page()
    page.on_submit = lambda p: setattr(p, "url", TARGET)
    assert run(page)["status"] == "unexpected_page"


@pytest.mark.parametrize(
    "image", [b"bad", png(0, 180), png(4097, 180), png(payload=b"x" * 1048576)]
)
def test_invalid_images_are_rejected_before_model(image):
    page, solver = Page(), Solver()
    page.main_png = image
    with pytest.raises(CaptchaSolverError, match="invalid_image"):
        run(page, solver)
    assert not solver.calls


def test_cancelled_model_propagates_without_submit():
    page = Page()

    def cancel():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(page, Solver(action=cancel))
    assert not page.clicks and not page.submitted


@pytest.mark.parametrize("kind", ["main", "refimg"])
def test_missing_source_prevents_model_request(kind):
    page, solver = Page(), Solver()
    page.sources[kind] = ""
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls


@pytest.mark.parametrize("kind", ["main", "canvas"])
@pytest.mark.parametrize(
    "box",
    [
        None,
        {"x": 0, "y": 0, "width": 0, "height": 20},
        {"x": float("nan"), "y": 0, "width": 10, "height": 20},
    ],
)
def test_bad_css_box_prevents_model_request(kind, box):
    page, solver = Page(), Solver()
    page.boxes[kind] = box
    assert run(page, solver)["status"] == "unsupported_challenge"
    assert not solver.calls


def test_invalid_reference_png_prevents_model_request():
    page, solver = Page(), Solver()
    page.ref_png = b"invalid"
    with pytest.raises(CaptchaSolverError, match="invalid_image"):
        run(page, solver)
    assert not solver.calls


def test_instruction_is_bounded_and_encoded_as_untrusted_text():
    page, solver = Page(), Solver()
    page.instruction = '\\"\x00' * 800
    page.on_submit = arrive
    assert run(page, solver)["status"] == "solved"
    prompt = solver.calls[0][1]
    assert len(prompt) <= 2000 and "\x00" not in prompt
    assert "\\u0000" in prompt


def test_duplicate_form_does_not_claim_challenge(monkeypatch):
    page, solver = Page(), Solver()
    original = page.locator

    def locator(selector):
        if selector == yandex.FORM:
            return Matches([Element(page, "form"), Element(page, "form")])
        return original(selector)

    monkeypatch.setattr(page, "locator", locator)
    assert run(page, solver) is None
    assert not solver.calls


def test_gate_text_remains_after_form_disappears(monkeypatch):
    page = Page()
    original = Element.inner_text

    async def inner_text(element, **kwargs):
        if element.kind == "body":
            return "Confirm that you are not a robot"
        return await original(element, **kwargs)

    monkeypatch.setattr(Element, "inner_text", inner_text)
    page.on_submit = arrive
    assert run(page)["status"] == "unexpected_page"
