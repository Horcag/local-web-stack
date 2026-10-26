import io
import json
from urllib.error import HTTPError

import pytest

from service import healthcheck


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def test_healthy_http_body_is_required(monkeypatch):
    monkeypatch.setattr(healthcheck, "urlopen", lambda *_a, **_k: response({"status": "healthy"}))
    healthcheck.check("crawl4ai")


def test_readiness_503_reports_only_allowlisted_failed_checks(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise HTTPError(
            "http://local/health",
            503,
            "unavailable",
            {},
            response(
                {
                    "status": "degraded",
                    "checks": {
                        "dns": {"status": "healthy"},
                        "proxy": {"status": "unhealthy", "error": "secret"},
                        "crawler": {"status": "healthy"},
                        "private": {"status": "unhealthy"},
                    },
                }
            ),
        )

    monkeypatch.setattr(healthcheck, "urlopen", unavailable)
    with pytest.raises(healthcheck.ReadinessError) as error:
        healthcheck.check("crawl4ai")
    assert error.value.checks == ["proxy"]
    assert "secret" not in str(error.value)


def test_mcp_probes_actual_tool_catalog(monkeypatch):
    captured = []

    def tools(request, **kwargs):
        captured.append(json.loads(request.data))
        return response(
            {
                "result": {
                    "tools": [
                        {"name": name} for name in ("read_url", "web_search", "local_web_health")
                    ]
                }
            }
        )

    monkeypatch.setattr(healthcheck, "urlopen", tools)
    healthcheck.check("mcp")
    assert captured[0]["method"] == "tools/list"


def test_mcp_missing_tool_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        healthcheck, "urlopen", lambda *_a, **_k: response({"result": {"tools": []}})
    )
    with pytest.raises(AssertionError):
        healthcheck.check("mcp")
