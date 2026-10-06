"""Research, browser and static dashboard wiring for the Crawl4AI service."""

from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from service.browser import BrowserRuntime, create_browser_router
from service.research import create_router


def attach_research(app: FastAPI) -> None:
    browser = BrowserRuntime()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await browser.close()

    app.router.lifespan_context = lifespan
    app.include_router(create_router(browser_reader=browser.read if browser.configured else None))
    app.include_router(create_browser_router(browser))

    @app.middleware("http")
    async def local_ui_security(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse(
                    {"detail": "Cross-origin mutation is not allowed"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    hosts = os.environ.get(
        "LOCAL_WEB_ALLOWED_HOSTS",
        "localhost,127.0.0.1,[::1],testserver,local-web-crawl4ai,crawl4ai",
    ).split(",")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[host.strip() for host in hosts if host.strip()]
    )
    static_dir = Path(__file__).with_name("static")
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="assets")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")
