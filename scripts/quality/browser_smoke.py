"""Deterministic real-browser acceptance using only owned loopback fixtures.

Run with CRAWL4_AI_BASE_DIRECTORY set to task scratch. Optional --screenshot
writes desktop and mobile PNGs outside Git for a named reviewer.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
import html
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from service.browser import BrowserRuntime, create_browser_router  # noqa: E402
from service.research import create_router  # noqa: E402

TEXT = " ".join(
    "Local fixture evidence describes research collection with durable sources, provenance, "
    "explicit status, careful browser ownership, and unknown wider web coverage."
    for _ in range(5)
)
XSS = "<script>window.__smoke_xss = true</script>"


@contextmanager
def environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def server(app):
    """Bind an exact owned socket, gracefully reap only its server thread."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    instance = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5))
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not instance.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Owned fixture server failed to start within 10 seconds")
            time.sleep(0.02)
        yield address
    finally:
        instance.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Owned fixture server did not stop within 10 seconds")


def fixture_app():
    app = FastAPI()

    @app.get("/search")
    def search(request: Request):
        base = str(request.base_url).rstrip("/")
        return {
            "results": [
                {"url": base + "/read", "title": "Readable fixture", "engine": "fixture"},
                {"url": base + "/captcha", "title": "Browser fixture", "engine": "fixture"},
            ],
            "unresponsive_engines": [],
        }

    @app.post("/md")
    async def markdown(request: Request):
        payload = await request.json()
        gate = payload["url"].endswith("/captcha")
        return {
            "url": payload["url"],
            "markdown": "Verify you are human" if gate else TEXT + XSS,
            "title": "Verify you are human" if gate else "Readable fixture",
            "status_code": 200,
        }

    @app.get("/read", response_class=HTMLResponse)
    @app.get("/captcha", response_class=HTMLResponse)
    def browser_page():
        return (
            "<!doctype html><title>Owned browser fixture</title><main><h1>Fixture evidence</h1>"
            + '<p id="evidence">'
            + html.escape(TEXT + XSS)
            + '</p><label for="name">Name</label>'
            + '<input id="name"><button id="apply" onclick="document.getElementById(\'result\')'
            + ".textContent=document.getElementById('name').value\">Apply</button>"
            + '<p id="result"></p></main>'
        )

    return app


async def json_request(client, method, url, **kwargs):
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


async def exercise(base, fixture, runtime, scratch, screenshot):
    receipts = []
    async with async_playwright() as playwright:
        # This launch and its profile belong entirely to this smoke run.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            cdp_port = reservation.getsockname()[1]
        context = await playwright.chromium.launch_persistent_context(
            str(scratch / "browser-profile"),
            headless=True,
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None,
            args=[f"--remote-debugging-port={cdp_port}", "--remote-debugging-address=127.0.0.1"],
            viewport={"width": 1440, "height": 1000},
        )
        try:
            preserved = context.pages[0]
            await preserved.goto(fixture + "/read")
            page = await context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base, wait_until="networkidle")
            await page.get_by_test_id("title").fill("Fixture research acceptance")
            await page.get_by_test_id("query").fill("Owned local evidence")
            await page.get_by_role("button", name="Create research").click()
            await page.locator("#notice").get_by_text("Research created.", exact=False).wait_for()
            await page.get_by_test_id("discover").click()
            await page.get_by_test_id("source-count").get_by_text("2", exact=True).wait_for()
            await page.get_by_test_id("collect").click()
            await (
                page.locator("#notice")
                .get_by_text("Collection batch finished.", exact=False)
                .wait_for()
            )
            async with httpx.AsyncClient(base_url=base, timeout=10) as client:
                runs = await json_request(client, "GET", "/research/runs")
                run_id = runs["runs"][0]["id"]
                coverage = await json_request(client, "GET", f"/research/runs/{run_id}/coverage")
                assert coverage["counts"]["read"] == 1, coverage
                assert coverage["counts"]["browser_pending"] == 1, coverage
                assert coverage["search_coverage"] == "unknown"
                receipts.append(
                    "UI create, discover, collect: 2 discovered / 1 read / 1 browser_pending"
                )
                await page.get_by_test_id("source-filter").select_option("read")
                assert await page.locator("#sources tr").count() == 1
                await page.get_by_role("button", name="Inspect").click()
                await page.locator("#detail-content").get_by_text(XSS, exact=False).wait_for()
                assert not await page.evaluate("Boolean(window.__smoke_xss)")
                await page.keyboard.press("Escape")
                assert not await page.locator("#source-dialog").is_visible()
                await page.get_by_test_id("source-filter").select_option("browser_pending")
                await page.get_by_role("button", name="Inspect").click()
                await page.get_by_role("button", name="Skip source").click()
                await page.locator("#detail-notice").get_by_text("Source skipped.").wait_for()
                coverage = await json_request(client, "GET", f"/research/runs/{run_id}/coverage")
                assert coverage["counts"]["skipped"] == 1
                await page.get_by_role("button", name="Retry collection").click()
                await (
                    page.locator("#detail-notice")
                    .get_by_text("Source queued for retry.")
                    .wait_for()
                )
                await page.keyboard.press("Escape")
                await page.get_by_test_id("collect").click()
                await (
                    page.locator("#notice")
                    .get_by_text("Collection batch finished.", exact=False)
                    .wait_for()
                )
                await page.get_by_test_id("source-filter").select_option("browser_pending")
                await page.get_by_role("button", name="Inspect").click()
                receipts.append("UI skip, retry, recollect returns the fixture to browser_pending")
                source_url = await page.locator("#browser-url").input_value()
                browser_page = await context.new_page()
                try:
                    await browser_page.goto(source_url)
                    actual = await browser_page.locator("#evidence").inner_text()
                    actual_title = await browser_page.title()
                finally:
                    await browser_page.close()
                await page.locator("#browser-markdown").fill(actual)
                await page.locator("#browser-title").fill(actual_title)
                await page.get_by_role("button", name="Save browser result").click()
                await (
                    page.locator("#detail-notice")
                    .get_by_text("Browser result saved", exact=False)
                    .wait_for()
                )
                await page.keyboard.press("Escape")
                coverage = await json_request(client, "GET", f"/research/runs/{run_id}/coverage")
                assert coverage["counts"]["read"] == 2, coverage
                receipts.append(
                    "Filter, source details, plain-text XSS, Escape, real fixture browser import"
                )
                await page.get_by_test_id("source-filter").select_option("")
                for selector, suffix in [
                    ("#export-markdown", "markdown"),
                    ("#export-json", "json"),
                ]:
                    async with page.expect_download() as download:
                        await page.locator(selector).click()
                    download_value = await download.value
                    destination = scratch / f"export.{suffix}"
                    await download_value.save_as(destination)
                    assert destination.stat().st_size > 100
                await page.reload(wait_until="networkidle")
                await page.get_by_role("button", name="Fixture research acceptance").click()
                await page.get_by_test_id("source-count").get_by_text("2", exact=True).wait_for()
                await page.keyboard.press("Tab")
                assert await page.evaluate("document.activeElement !== document.body")
                receipts.append("Markdown + JSON downloads, durable reload/reopen, keyboard focus")
                assert (
                    await page.locator("#coverage").get_by_text("Unknown", exact=True).count() == 1
                )
                if screenshot:
                    await page.screenshot(path=str(screenshot), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    "Mobile page overflows"
                )
                if screenshot:
                    await page.screenshot(
                        path=str(screenshot.with_name(screenshot.stem + "-mobile.png")),
                        full_page=True,
                    )
                receipts.append(
                    "Desktop 1440 and mobile 390 layout, no document horizontal overflow"
                )
                # Enable only our launched browser endpoint; preserve its pre-existing fixture tab.
                runtime.endpoint = f"http://127.0.0.1:{cdp_port}"
                status = await json_request(client, "GET", "/browser/status")
                assert status["configured"] is True
                opened = await json_request(
                    client, "POST", "/browser/tabs", json={"url": fixture + "/read"}
                )
                tab_id = opened["tab_id"]
                await json_request(
                    client,
                    "POST",
                    f"/browser/tabs/{tab_id}/action",
                    json={"action": "fill", "selector": "#name", "value": "Owned CDP action"},
                )
                snapshot = await json_request(
                    client,
                    "POST",
                    f"/browser/tabs/{tab_id}/action",
                    json={"action": "click", "selector": "#apply"},
                )
                assert "Owned CDP action" in snapshot["markdown"]
                assert (await json_request(client, "GET", f"/browser/tabs/{tab_id}"))[
                    "url"
                ] == fixture + "/read"
                await json_request(client, "DELETE", f"/browser/tabs/{tab_id}")
                assert not preserved.is_closed() and preserved.url == fixture + "/read"
                status = await json_request(client, "GET", "/browser/status")
                assert status["owned_tabs"] == 0
                receipts.append(
                    "Real configured CDP open/fill/click/snapshot/close; existing owned fixture tab preserved"
                )
            assert not errors, errors
            receipts.append("No dashboard JavaScript page errors")
        finally:
            # Close only the browser launch owned by this test; server lifespan disconnects its adapter.
            await context.close()
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--screenshot", type=Path, help="Outside-Git desktop PNG; mobile sibling also written"
    )
    args = parser.parse_args()
    if args.screenshot:
        args.screenshot = args.screenshot.resolve()
        if args.screenshot.is_relative_to(ROOT):
            parser.error("Screenshots must be outside the repository")
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
    base_directory = os.environ.get("CRAWL4_AI_BASE_DIRECTORY") or os.environ.get(
        "AGENT_STORAGE_SCRATCH"
    )
    if base_directory:
        Path(base_directory).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="local-web-ui-smoke-", dir=base_directory) as directory:
        scratch = Path(directory)
        runtime = BrowserRuntime(endpoint="")

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app):
            try:
                yield
            finally:
                await runtime.close()

        app = FastAPI(lifespan=lifespan)
        app.include_router(
            create_router(browser_reader=runtime.read, db_path=scratch / "research.db")
        )
        app.include_router(create_browser_router(runtime))
        app.mount("/", StaticFiles(directory=ROOT / "service/static", html=True))
        with ExitStack() as stack:
            fixture = stack.enter_context(server(fixture_app()))
            stack.enter_context(
                environment(
                    {
                        "LOCAL_WEB_RESEARCH_ALLOW_PRIVATE": "1",
                        "SEARXNG_URL": fixture,
                        "CRAWL4AI_URL": fixture,
                        "CRAWL4_AI_BASE_DIRECTORY": str(scratch),
                    }
                )
            )
            base = stack.enter_context(server(app))
            receipts = asyncio.run(
                asyncio.wait_for(
                    exercise(base, fixture, runtime, scratch, args.screenshot), timeout=120
                )
            )
        for receipt in receipts:
            print("PASS:", receipt)
    print(
        "CLEANUP: owned browser/profile, export fixtures, SQLite database and both server threads removed/stopped"
    )


if __name__ == "__main__":
    main()
