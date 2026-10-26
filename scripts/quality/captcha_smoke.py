"""Owned Chromium/Crawl4AI fixtures; --live-model also checks the configured API.

No third-party CAPTCHA is bypassed by the fixtures. All browser state is temporary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote
from unittest.mock import patch
from pydantic import HttpUrl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from service import crawl4ai_api as api  # noqa: E402
from service.captcha_solver import CaptchaSolver, PNG_SIGNATURE  # noqa: E402
from service.research.content import assess_content  # noqa: E402

SOURCE = "Verified source evidence from the requested local fixture. " * 20


class FixtureSolver:
    enabled = configured = True

    async def solve(self, image):
        assert image.startswith(PNG_SIGNATURE)
        return "48271"

    async def complete_image(self, image, prompt):
        assert image.startswith(PNG_SIGNATURE)
        assert "300 pixels wide" in prompt
        return '{"points":[[75,80],[225,80]]}'


def fixture_html(kind: str) -> str:
    if kind == "text":
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="160">'
            '<rect width="300" height="160" fill="white"/>'
            '<text x="30" y="100" font-size="58" fill="black">48271</text></svg>'
        )
        controls = '<input name="captcha" data-qa="account-captcha-input" autocomplete="off">'
        check = "document.querySelector('input').value === '48271'"
    else:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="160">'
            '<rect width="300" height="160" fill="white"/>'
            '<circle cx="75" cy="80" r="28" fill="red"/>'
            '<rect x="197" y="52" width="56" height="56" fill="blue"/></svg>'
        )
        controls = "<p data-captcha-instructions>Click the red circle, then the blue square.</p>"
        check = (
            "points.length === 2 && Math.hypot(points[0][0]-75,points[0][1]-80)<28 "
            "&& Math.abs(points[1][0]-225)<28 && Math.abs(points[1][1]-80)<28"
        )
    image = "data:image/svg+xml," + quote(svg, safe="")
    return (
        "<!doctype html><title>Are you not a robot?</title>"
        "<h1>Please confirm that you are not a robot</h1>"
        '<form><img data-qa="account-captcha-picture" src="'
        + image
        + '" width="300" height="160" style="display:block">'
        + controls
        + '<button type="submit">Submit</button></form><script>'
        "const points=[];const image=document.querySelector('img');"
        "image.addEventListener('click',e=>{const b=image.getBoundingClientRect();"
        "points.push([e.clientX-b.x,e.clientY-b.y]);});"
        "document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();if("
        + check
        + "){document.title='Verified fixture source';document.body.innerHTML='<h1>Source</h1><p>"
        + SOURCE
        + "</p>';}});</script>"
    )


async def smoke(live_model: bool) -> dict:
    solver = CaptchaSolver.from_env() if live_model else FixtureSolver()
    if not solver.enabled or not solver.configured:
        raise RuntimeError("Explicit configured CAPTCHA gateway is required for --live-model")
    real_crawler = api.AsyncWebCrawler
    receipts = []
    with tempfile.TemporaryDirectory(prefix="local-web-captcha-smoke-") as scratch:
        for kind in ("text", "points"):

            async def route_request(route):
                await route.fulfill(status=200, content_type="text/html", body=fixture_html(kind))

            def crawler_factory(**kwargs):
                crawler = real_crawler(**kwargs)

                async def own_fixture(page, context, **_kwargs):
                    await context.route("https://captcha-fixture.example/**", route_request)
                    return page

                crawler.crawler_strategy.set_hook("on_page_context_created", own_fixture)
                return crawler

            request = api.CrawlRequest(
                url=HttpUrl(f"https://captcha-fixture.example/{kind}"),
                browser_type="chromium",
                headless=True,
                use_persistent_context=False,
                user_data_dir=scratch,
                cache_mode="bypass",
                domain_delay_seconds=0,
                chrome_channel="chrome",
                channel="chrome",
            )
            with (
                patch.object(api, "AsyncWebCrawler", crawler_factory),
                patch.object(api.CaptchaSolver, "from_env", return_value=solver),
            ):
                result = await api._crawl(request)
            captcha = (result.metadata or {}).get("captcha_recovery")
            markdown = api._markdown_text(result)
            accepted = (
                result.success
                and captcha is not None
                and captcha["status"] == "solved"
                and (captcha.get("kind") == "ordered_points") == (kind == "points")
                and SOURCE.split(".")[0] in markdown
                and assess_content(markdown, result.metadata.get("title", ""))["status"] == "read"
            )
            receipts.append({"case": kind, "accepted": accepted, "captcha": captcha})
    return {"model_route": "configured-gateway" if live_model else "fixture", "cases": receipts}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(smoke(args.live_model))
    print(json.dumps(result))
    if not all(case["accepted"] for case in result["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
