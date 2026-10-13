"""Bounded recovery of text-image CAPTCHA on the caller's existing page."""

from __future__ import annotations

import asyncio
import hashlib
import struct
from typing import Any
from urllib.parse import parse_qs, urlsplit

from service.captcha_solver import CaptchaSolver, CaptchaSolverError
from service.captcha_points import puzzle_points
from service.research.content import is_captcha_gate

IMAGE_SELECTOR = (
    'img[data-qa~="account-captcha-picture"], .AdvancedCaptcha-Image, '
    'img[src*="captchaimg"], img[src*="/captcha/image"], img[src*="captcha"]'
)
INPUT_SELECTOR = (
    'input[data-qa~="account-captcha-input"], input[name="rep"], '
    'input[name="captcha"], input[name="captchaText"], input[name="captchaCode"]'
)
SUBMIT_SELECTOR = 'button[type="submit"], input[type="submit"], .AdvancedCaptcha-SubmitButton'
INSTRUCTION_SELECTOR = (
    ".AdvancedCaptcha-Instruction, .AdvancedCaptcha-Task, [data-captcha-instructions]"
)
MAX_ATTEMPTS = 2
RECOVERY_TIMEOUT_SECONDS = 45


async def _visible(scope: Any, selector: str) -> Any:
    matches = scope.locator(selector)
    for index in range(min(await matches.count(), 20)):
        candidate = matches.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def _is_challenge(page: Any) -> bool:
    return is_captcha_gate(await page.locator("body").inner_text(timeout=5000), await page.title())


def _at_target(actual: str, target: str) -> bool:
    current, expected = urlsplit(actual), urlsplit(target)
    expected_query = parse_qs(expected.query, keep_blank_values=True)
    current_query = parse_qs(current.query, keep_blank_values=True)
    return (
        current.scheme == expected.scheme
        and current.hostname == expected.hostname
        and (current.port or (443 if current.scheme == "https" else 80))
        == (expected.port or (443 if expected.scheme == "https" else 80))
        and current.path.rstrip("/") == expected.path.rstrip("/")
        and all(current_query.get(key) == values for key, values in expected_query.items())
    )


async def _text_form(page: Any) -> tuple[Any, Any, Any] | None:
    # Only a CAPTCHA-specific image/input pair inside the same visible form.
    # Never send a whole page, or fill an arbitrary text field from model output.
    forms = page.locator("form")
    for index in range(min(await forms.count(), 20)):
        form = forms.nth(index)
        field = await _visible(form, INPUT_SELECTOR)
        if field is None:
            continue
        image = await _visible(form, IMAGE_SELECTOR)
        submit = await _visible(form, SUBMIT_SELECTOR)
        if image is not None and submit is not None:
            return image, field, submit
    return None


async def _wait_after_submit(page: Any) -> None:
    # A DOM change or navigation is necessary before accepting the answer.
    for _ in range(10):
        await asyncio.sleep(0.3)
        if not await _is_challenge(page):
            return


async def _point_form(page: Any) -> tuple[Any, str, Any] | None:
    forms = page.locator("form")
    for index in range(min(await forms.count(), 20)):
        form = forms.nth(index)
        image = await _visible(form, IMAGE_SELECTOR)
        instruction = await _visible(form, INSTRUCTION_SELECTOR)
        submit = await _visible(form, SUBMIT_SELECTOR)
        if image is not None and instruction is not None and submit is not None:
            text = await instruction.inner_text(timeout=5000)
            if text.strip():
                return image, text.strip(), submit
    return None


async def _recover_points(page: Any, solver: CaptchaSolver, target: str, receipt: dict) -> dict:
    previous_digest = None
    for attempt in range(receipt["attempts"] + 1, MAX_ATTEMPTS + 1):
        challenge = await _point_form(page)
        if challenge is None:
            return receipt | {"status": "unsupported_challenge"}
        image, instructions, submit = challenge
        challenge_url = page.url
        image_source = await image.get_attribute("src")
        if not image_source:
            return receipt | {"status": "unsupported_challenge"}
        original_box = await image.bounding_box()
        if not original_box or original_box["width"] <= 0 or original_box["height"] <= 0:
            return receipt | {"status": "challenge_changed"}
        captured = await image.screenshot(type="png", scale="css", timeout=5000)
        digest = hashlib.sha256(captured).digest()
        if digest == previous_digest:
            return receipt | {"status": "unchanged_challenge"}
        previous_digest = digest
        width, height = struct.unpack(">II", captured[16:24])
        receipt["attempts"] = attempt
        receipt["kind"] = "ordered_points"
        points = await puzzle_points(solver, captured, instructions, width, height)
        if not points:
            return receipt | {"status": "unsupported_challenge"}
        if page.url != challenge_url or not await _is_challenge(page):
            return receipt | {"status": "challenge_changed"}
        fresh = await _point_form(page)
        if (
            fresh is None
            or fresh[1] != instructions
            or await fresh[0].get_attribute("src") != image_source
        ):
            return receipt | {"status": "challenge_changed"}
        image, _, submit = fresh
        current = await image.screenshot(type="png", scale="css", timeout=5000)
        if hashlib.sha256(current).digest() != digest:
            return receipt | {"status": "challenge_changed"}
        box = await image.bounding_box()
        if box != original_box:
            return receipt | {"status": "challenge_changed"}
        for x, y in points:
            if (
                page.url != challenge_url
                or await image.bounding_box() != box
                or await image.get_attribute("src") != image_source
            ):
                return receipt | {"status": "challenge_changed"}
            await image.click(
                position={"x": x * box["width"] / width, "y": y * box["height"] / height},
                timeout=5000,
            )
        if not await _is_challenge(page):
            status = "solved" if _at_target(page.url, target) else "unexpected_page"
            return receipt | {"status": status}
        fresh = await _point_form(page)
        if (
            page.url != challenge_url
            or fresh is None
            or fresh[1] != instructions
            or await fresh[0].bounding_box() != box
            or await fresh[0].get_attribute("src") != image_source
        ):
            return receipt | {"status": "challenge_changed"}
        submit = fresh[2]
        await submit.click(timeout=5000)
        await _wait_after_submit(page)
        if not await _is_challenge(page):
            status = "solved" if _at_target(page.url, target) else "unexpected_page"
            return receipt | {"status": status}
    return receipt | {"status": "rejected"}


async def _recover(page: Any, solver: CaptchaSolver, target: str, receipt: dict) -> dict:
    if not await _is_challenge(page):
        return receipt | {"status": "not_needed"}
    receipt["challenge_observed"] = True
    if not solver.configured:
        return receipt | {"status": "unconfigured"}

    # Yandex can first display a checkbox and then a text-image challenge.
    # The click is limited to its recognized challenge and known control.
    host = urlsplit(page.url).hostname or ""
    if host in {"yandex.ru", "www.yandex.ru", "yandex.com", "www.yandex.com"}:
        checkbox = await _visible(page, ".CheckboxCaptcha-Button")
        if checkbox is not None:
            await checkbox.click(timeout=5000)
            await _wait_after_submit(page)
            if not await _is_challenge(page):
                status = "solved" if _at_target(page.url, target) else "unexpected_page"
                return receipt | {"status": status}

    previous_digest = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        challenge = await _text_form(page)
        if challenge is None:
            return await _recover_points(page, solver, target, receipt)
        image, field, submit = challenge
        challenge_url = page.url
        captured = await image.screenshot(type="png", timeout=5000)
        digest = hashlib.sha256(captured).digest()
        if digest == previous_digest:
            return receipt | {"status": "unchanged_challenge"}
        previous_digest = digest
        receipt["attempts"] = attempt
        answer = await solver.solve(captured)
        if not answer:
            return receipt | {"status": "empty_model_result"}

        # Challenge identity must still match when the model finishes.
        if page.url != challenge_url or not await _is_challenge(page):
            return receipt | {"status": "challenge_changed"}
        fresh = await _text_form(page)
        if fresh is None:
            return receipt | {"status": "challenge_changed"}
        image, field, submit = fresh
        current = await image.screenshot(type="png", timeout=5000)
        if hashlib.sha256(current).digest() != digest:
            return receipt | {"status": "challenge_changed"}
        await field.fill(answer, timeout=5000)
        await submit.click(timeout=5000)
        await _wait_after_submit(page)
        if not await _is_challenge(page):
            status = "solved" if _at_target(page.url, target) else "unexpected_page"
            return receipt | {"status": status}
    return receipt | {"status": "rejected"}


async def recover_captcha(page: Any, solver: CaptchaSolver, target_url: str) -> dict:
    """Return an outcome only; model output and screenshots stay in memory."""
    receipt = {"attempts": 0, "status": "disabled"}
    if not solver.enabled:
        return receipt

    def navigation_response(response):
        if response.request.is_navigation_request() and response.request.frame == page.main_frame:
            receipt["final_status_code"] = response.status

    observing = callable(getattr(page, "on", None))
    if observing:
        page.on("response", navigation_response)
    try:
        return await asyncio.wait_for(
            _recover(page, solver, target_url, receipt), RECOVERY_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return receipt | {"status": "deadline"}
    except CaptchaSolverError as exc:
        return receipt | {"status": exc.reason}
    except Exception:
        return receipt | {"status": "browser_error"}
    finally:
        if observing:
            page.remove_listener("response", navigation_response)
