"""Private challenge image deduplication survives retry URL changes."""

import asyncio

from service import captcha_yandex as yandex
from test_captcha_yandex import TARGET, Page, Solver, arrive, png


def run_shared(page, solver, seen):
    return asyncio.run(
        yandex.recover_yandex_points(
            page, solver, TARGET, {"attempts": 0}, lambda a, b: a == b, seen_images=seen
        )
    )


def test_same_image_pair_on_changed_url_is_never_sent_twice():
    first, second, solver, seen = Page(), Page(), Solver(), set()
    first.on_submit = arrive
    assert run_shared(first, solver, seen)["status"] == "solved"
    second.url += "&status=failed"
    result = run_shared(second, solver, seen)
    assert result == {"attempts": 0, "kind": "yandex_silhouette", "status": "unchanged_challenge"}
    assert len(solver.calls) == len(seen) == 1
    assert not second.clicks and not second.submitted
    assert all(not isinstance(value, (bytes, set)) for value in result.values())


def test_new_image_pair_is_allowed_with_shared_history():
    first, second, solver, seen = Page(), Page(), Solver(), set()
    first.on_submit = second.on_submit = arrive
    assert run_shared(first, solver, seen)["status"] == "solved"
    second.main_png = png(payload=b"fresh challenge")
    second.url += "&status=failed"
    assert run_shared(second, solver, seen)["status"] == "solved"
    assert len(solver.calls) == len(seen) == 2


def test_changed_reference_pair_is_allowed_with_shared_history():
    first, second, solver, seen = Page(), Page(), Solver(), set()
    first.on_submit = second.on_submit = arrive
    assert run_shared(first, solver, seen)["status"] == "solved"
    second.ref_png = png(480, 80, b"new reference order")
    assert run_shared(second, solver, seen)["status"] == "solved"
    assert len(solver.calls) == len(seen) == 2
