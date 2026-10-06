"""Exercise packaged routes and bundled Chromium without network or dev dependencies."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="local-web-image-smoke-") as scratch:
        os.environ["CRAWL4_AI_BASE_DIRECTORY"] = scratch
        os.environ["LOCAL_WEB_BROWSER_CDP_URL"] = ""
        from fastapi.testclient import TestClient
        from playwright.sync_api import sync_playwright
        from service import crawl4ai_api as api

        content = "A substantive fixture source for deterministic image acceptance. " * 40
        result = SimpleNamespace(
            success=True,
            markdown=content,
            metadata={"title": "Packaged fixture"},
            status_code=200,
            links={},
        )
        with (
            patch.object(api, "_crawl", AsyncMock(return_value=result)),
            TestClient(api.app) as client,
        ):
            assert client.get("/health").json()["status"] == "healthy"
            dashboard = client.get("/")
            assert dashboard.status_code == 200
            assert "text/html" in dashboard.headers["content-type"]
            assert client.get("/browser/status").json()["configured"] is False
            response = client.post("/md", json={"url": "https://example.com"})
            assert response.status_code == 200
            assert response.json()["markdown"] == content
            assert response.json()["assessment"]["status"] == "read"
        with sync_playwright() as driver:
            assert Path(driver.chromium.executable_path).is_file()
            browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.set_content("<html><body><h1>Owned image fixture</h1></body></html>")
                assert page.locator("h1").inner_text() == "Owned image fixture"
            finally:
                browser.close()
    print(
        "Image smoke passed: health, dashboard, browser status, mocked /md, real bundled Chromium"
    )


if __name__ == "__main__":
    main()
