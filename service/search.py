"""One bounded search backend shared by MCP, HTTP and durable research runs."""

from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import json
import os
import time
from typing import Callable

import httpx
from fastapi import APIRouter, HTTPException

from service.search_browser import browser_search, supports_language
from service.search_diagnostics import engine_status
from service.search_models import SearchRequest
from service.search_policy import prepare, rank_results, refined_query


def _number(name: str, default: float, maximum: float) -> float:
    try:
        return min(maximum, max(0, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


class SearchBackend:
    def __init__(self, browser_runtime=None) -> None:
        self.browser = browser_runtime
        self._lock = asyncio.Lock()
        self._last_start = 0.0
        self._cache: dict[str, tuple[float, dict]] = {}

    async def _pace(self) -> None:
        delay = _number("LOCAL_WEB_SEARCH_DELAY_SECONDS", 1, 60)
        async with self._lock:
            await asyncio.sleep(max(0, delay - (time.monotonic() - self._last_start)))
            self._last_start = time.monotonic()

    async def _searx(self, request: SearchRequest, query: str, language: str, engines: str) -> dict:
        params = {"q": query, "format": "json", "pageno": str(request.page)}
        if language:
            params["language"] = language
        if request.time_range:
            params["time_range"] = request.time_range
        params["engines" if engines else "categories"] = engines or request.category
        url = os.getenv("SEARXNG_URL", "http://127.0.0.1:8088").rstrip("/") + "/search"
        async with httpx.AsyncClient(
            timeout=max(1, _number("LOCAL_WEB_SEARX_TIMEOUT_SECONDS", 25, 35)),
            follow_redirects=False,
        ) as client:
            response = await client.get(url, params=params, headers={"X-Real-IP": "127.0.0.1"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("Invalid upstream search response")
            return payload

    async def search(self, request: SearchRequest, admit_attempt: Callable[[], bool] | None = None):
        policy = prepare(request)
        query = policy["query"]
        if policy["domains"] and not any("site:" + d in query for d in policy["domains"]):
            query += " (" + " OR ".join("site:" + d for d in policy["domains"]) + ")"
        for entity in request.required_entities:
            if entity not in query:
                query += " " + json.dumps(entity, ensure_ascii=False)
        policy["query"] = query
        engines = request.engines.strip()
        if not engines and request.category == "general":
            engines = os.getenv("LOCAL_WEB_SEARCH_ENGINES", "mojeek,mwmbl,yandex").strip()
        key = json.dumps(request.model_dump(), sort_keys=True, ensure_ascii=False)
        ttl = _number("LOCAL_WEB_SEARCH_CACHE_TTL_SECONDS", 900, 3600)
        cached = self._cache.get(key)
        if cached and ttl and time.monotonic() - cached[0] < ttl:
            result = deepcopy(cached[1])
            result.update(
                cache_hit=True,
                cache_age_seconds=round(time.monotonic() - cached[0], 3),
                cached_attempts=result["attempts"],
                attempts=[],
                actual_requests=0,
            )
            return result
        attempts: list[dict] = []
        candidates: list[dict] = []
        errors: list = []
        rejected: list[dict] = []
        diagnostics: list[dict] = []
        ranked: list[dict] = []
        phase = "initial"
        stage_queries = [("initial", query)]
        if request.refine and (refinement := refined_query(policy)):
            stage_queries.append(("refinement", refinement))
        try:
            async with asyncio.timeout(90):
                for phase, stage_query in stage_queries:
                    await self._pace()
                    if admit_attempt is not None and not admit_attempt():
                        attempts.append(
                            {"stage": phase, "status": "budget_exhausted", "query": stage_query}
                        )
                        break
                    began = time.monotonic()
                    receipt = {
                        "stage": phase,
                        "provider": "searxng",
                        "query": stage_query,
                        "language": policy["language"],
                        "page": request.page,
                        "status": "pending",
                        "dispatched": True,
                    }
                    attempts.append(receipt)
                    try:
                        payload = await self._searx(
                            request, stage_query, policy["language"], engines
                        )
                        items = [item for item in payload["results"] if isinstance(item, dict)]
                        for item in items:
                            item = dict(item)
                            item["acquisition"] = {
                                "stage": phase,
                                "query": stage_query,
                                "provider": "searxng",
                            }
                            candidates.append(item)
                        ranked, rejected = rank_results(candidates, policy)
                        accepted_stage, _ = rank_results(items, policy)
                        states = engine_status(engines, payload, accepted_stage)
                        diagnostics.extend(dict(state, stage=phase) for state in states)
                        errors.extend(payload.get("unresponsive_engines", []))
                        receipt.update(
                            status="completed",
                            raw_count=len(items),
                            accepted_count=len(accepted_stage),
                            engine_status=states,
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        status = (
                            exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError)
                            else None
                        )
                        receipt.update(
                            status="failed",
                            error="upstream_http_" + str(status) if status else type(exc).__name__,
                        )
                        diagnostics.extend(engine_status(engines, {"results": []}, []))
                    finally:
                        receipt["elapsed_ms"] = round(1000 * (time.monotonic() - began))
                    if len(ranked) >= min(3, request.max_results) or receipt["status"] == "failed":
                        break
                browser_allowed = (
                    request.browser_fallback
                    and request.category == "general"
                    and "yandex" in {e.strip() for e in engines.split(",")}
                    and not request.time_range
                    and self.browser is not None
                    and self.browser.configured
                    and len(ranked) < min(3, request.max_results)
                    and len([a for a in attempts if a.get("dispatched")]) < 3
                    and not any(a["status"] == "budget_exhausted" for a in attempts)
                )
                if browser_allowed and not supports_language(policy["language"]):
                    attempts.append(
                        {
                            "stage": "browser",
                            "status": "rejected",
                            "query": query,
                            "reason": "unsupported_query_or_language",
                            "dispatched": False,
                        }
                    )
                    browser_allowed = False
                if browser_allowed:
                    phase = "browser"
                    await self._pace()
                    if admit_attempt is not None and not admit_attempt():
                        attempts.append(
                            {"stage": phase, "status": "budget_exhausted", "query": query}
                        )
                    else:
                        began = time.monotonic()
                        receipt = {
                            "stage": phase,
                            "provider": "browser-yandex",
                            "query": query,
                            "language": policy["language"],
                            "page": request.page,
                            "dispatched": True,
                            "status": "pending",
                        }
                        attempts.append(receipt)
                        browser = await browser_search(
                            self.browser, query, policy["language"], request.page
                        )
                        receipt.update(
                            browser["receipt"], elapsed_ms=round(1000 * (time.monotonic() - began))
                        )
                        for item in browser["results"]:
                            item["content"] = item.get("provenance", {}).get("card_text") or ""
                            item["acquisition"] = {
                                "stage": phase,
                                "query": query,
                                "provider": "browser-yandex",
                            }
                            candidates.append(item)
                        ranked, rejected = rank_results(candidates, policy)
        except TimeoutError:
            for attempt in attempts:
                if attempt.get("status") == "pending":
                    attempt.update(status="deadline", error="search_deadline")
            attempts.append({"stage": phase, "status": "deadline", "query": query})
        except asyncio.CancelledError:
            raise
        result = {
            "query": request.query,
            "effective_query": query,
            "effective_language": policy["language"],
            "normalizations": policy["normalizations"],
            "constraints": {
                "domains": policy["domains"],
                "required_entities": policy["required_entities"],
                "quoted_terms": policy["quoted_terms"],
            },
            "page": request.page,
            "source": "local-web",
            "searxng_url": os.getenv("SEARXNG_URL", "http://127.0.0.1:8088"),
            "requested_engines": engines or None,
            "engines": sorted({e for item in ranked for e in item["engines"]}) or None,
            "count": min(len(ranked), request.max_results),
            "results": ranked[: request.max_results],
            "unresponsive_engines": errors,
            "engine_status": diagnostics,
            "dropped_engines": sorted(
                {state["engine"] for state in diagnostics if state["status"] == "filtered"}
            )
            or None,
            "rejections": dict(Counter(item["reason"] for item in rejected)),
            "attempts": attempts,
            "actual_requests": sum(bool(a.get("dispatched")) for a in attempts),
            "cache_hit": False,
            "search_coverage": "unknown",
            "degraded": not ranked
            or any(s["status"] != "returned" for s in diagnostics)
            or any(
                a.get("status") in {"failed", "rejected", "deadline", "budget_exhausted"}
                for a in attempts
            ),
        }
        result["status"] = "degraded" if result["degraded"] else "ok"
        failures = [
            a
            for a in attempts
            if a.get("status") in {"failed", "deadline"}
            or a.get("reason") in {"browser_failed", "browser_timeout", "browser_unavailable"}
        ]
        if not ranked and failures:
            result["error"] = (
                failures[-1].get("error") or failures[-1].get("reason") or "search_deadline"
            )
        elif not ranked and any(s["status"] in {"error", "suspended"} for s in diagnostics):
            result["error"] = "providers_unavailable"
        if ranked and not result["degraded"] and ttl:
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), deepcopy(result))
        return result


_DEFAULT_BACKEND: SearchBackend | None = None


def default_backend() -> SearchBackend:
    global _DEFAULT_BACKEND
    if _DEFAULT_BACKEND is None:
        _DEFAULT_BACKEND = SearchBackend()
    return _DEFAULT_BACKEND


def create_search_router(backend: SearchBackend) -> APIRouter:
    router = APIRouter()

    @router.post("/search")
    async def search(request: SearchRequest):
        try:
            return await backend.search(request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
