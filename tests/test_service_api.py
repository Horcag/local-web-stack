from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from service import crawl4ai_api as api


@pytest.mark.parametrize(
    "markdown,title,http_status,expected",
    [
        ("", "", 200, "failed"),
        ("Verify you are human", "Just a moment", 200, "blocked"),
        ("Short text", "Article", 200, "partial"),
        ("Substantive source content " * 60, "Article", 200, "read"),
        ("Denied", "Access denied", 403, "blocked"),
    ],
)
def test_md_semantic_assessment(markdown, title, http_status, expected):
    result = SimpleNamespace(
        success=True,
        markdown=markdown,
        metadata={"title": title},
        status_code=http_status,
        links={"internal": []},
    )
    with patch.object(api, "_crawl", AsyncMock(return_value=result)), TestClient(api.app) as client:
        response = client.post("/md", json={"url": "https://example.com"})
    assert response.status_code == 200
    assert response.json()["assessment"]["status"] == expected
    assert response.json()["markdown"] == markdown


def test_reader_failure_is_error_not_success():
    with (
        patch.object(api, "_crawl", AsyncMock(side_effect=RuntimeError("reader failed"))),
        TestClient(api.app) as client,
    ):
        assert client.post("/md", json={"url": "https://example.com"}).status_code == 502
    with (
        patch.object(
            api,
            "_crawl",
            AsyncMock(return_value=SimpleNamespace(success=False, error_message="failed")),
        ),
        TestClient(api.app) as client,
    ):
        assert client.post("/md", json={"url": "https://example.com"}).status_code == 502


def test_dashboard_assets_and_csrf_boundary():
    with TestClient(api.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200
        assert "default-src 'self'" in client.get("/").headers["content-security-policy"]
        denied = client.post(
            "/research/runs",
            json={"title": "X", "query": "Y"},
            headers={"origin": "https://foreign.example"},
        )
        assert denied.status_code == 403
        assert client.get("/health").json()["status"] == "healthy"


def test_config_helpers_and_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("CRAWL4AI_BROWSER_TYPE", "invalid")
    request = api.CrawlRequest(
        url="https://example.com",
        proxy_server="http://proxy:8080",
        proxy_username="name",
        proxy_password="secret",
    )
    assert request.browser_type == "chromium"
    config = api.build_browser_config(request)
    assert config.ignore_https_errors is False
    assert api._proxy_config(request)["username"] == "name"
    assert api.build_run_config(request).cache_mode == api.CacheMode.ENABLED
    for raw, expected in [("", 8), ("oops", 8), ("2", 2)]:
        monkeypatch.setenv("TEST_VALUE", raw)
        assert api._env_int("TEST_VALUE", 8) == expected
    monkeypatch.setenv("TEST_VALUE", "oops")
    assert api._env_float("TEST_VALUE", 2.5) == 2.5
    monkeypatch.setenv("TEST_VALUE", "true")
    assert api._env_bool("TEST_VALUE", False)


def test_attacker_host_cannot_bypass_origin_boundary():
    with TestClient(api.app) as client:
        response = client.post(
            "/research/runs",
            json={"title": "Injected", "query": "Injected"},
            headers={"host": "attacker.example", "origin": "http://attacker.example"},
        )
        assert response.status_code == 400
        assert (
            client.get("/browser/status", headers={"host": "attacker.example"}).status_code == 400
        )


def test_direct_reader_rejects_private_initial_target():
    with TestClient(api.app) as client:
        assert client.post("/md", json={"url": "http://127.0.0.1/admin"}).status_code == 422
