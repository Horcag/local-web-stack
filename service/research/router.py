import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .content import assess_content, canonical_url
from .models import BrowserResult, CollectRequest, DiscoverRequest, RunRequest
from .runner import Runner, coverage
from .store import Store


def create_router(browser_reader=None, db_path=None):
    router = APIRouter(prefix="/research", tags=["research"])
    # Initialize lazily so importing the service never writes its runtime database.
    storage = None

    def store():
        nonlocal storage
        if storage is None:
            storage = Store(db_path)
        return storage

    def run(run_id):
        try:
            return store().run(run_id)
        except KeyError:
            raise HTTPException(404, "Run not found")

    def source(source_id):
        try:
            return store().source(source_id)
        except KeyError:
            raise HTTPException(404, "Source not found")

    @router.post("/runs")
    def create_run(request: RunRequest):
        return store().create(request.model_dump())

    @router.get("/runs")
    def list_runs():
        return {"runs": store().runs()}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str):
        return run(run_id)

    @router.post("/runs/{run_id}/discover")
    async def discover(run_id: str, request: DiscoverRequest = DiscoverRequest()):
        run(run_id)
        return await Runner(store(), browser_reader).discover(run_id, request)

    @router.post("/runs/{run_id}/collect")
    async def collect(run_id: str, request: CollectRequest = CollectRequest()):
        run(run_id)
        return await Runner(store(), browser_reader).collect(run_id, request.batch_size)

    @router.get("/runs/{run_id}/sources")
    def list_sources(run_id: str):
        run(run_id)
        return {"sources": store().sources(run_id)}

    @router.get("/runs/{run_id}/coverage")
    def get_coverage(run_id: str):
        run(run_id)
        return coverage(store(), run_id)

    @router.get("/runs/{run_id}/export")
    def export(run_id: str, format: str = Query(default="markdown", pattern="^(markdown|json)$")):
        data = {
            "run": run(run_id),
            "sources": store().sources(run_id),
            "coverage": coverage(store(), run_id),
        }
        if format == "json":
            return data
        lines = [
            "# " + data["run"]["title"],
            "",
            "Query: " + data["run"]["query"],
            "",
            "Search coverage: unknown. Forum coverage: bounded discovered pages only.",
            "",
            "## Coverage",
            "",
            json.dumps(data["coverage"]["counts"]),
            "",
        ]
        for item in data["sources"]:
            lines.extend(
                [
                    "## " + (item["title"] or item["url"]),
                    "",
                    item["url"],
                    "",
                    "Status: " + item["status"] + "; method: " + str(item["method"]),
                    "Reasons: " + ", ".join(item["reasons"]),
                    "Discovery provenance: " + json.dumps(item["provenance"], ensure_ascii=False),
                    "",
                    item["markdown"],
                    "",
                ]
            )
        return PlainTextResponse("\n".join(lines), media_type="text/markdown")

    @router.get("/sources/{source_id}")
    def get_source(source_id: str):
        return source(source_id)

    @router.post("/sources/{source_id}/browser-result")
    def browser_result(source_id: str, request: BrowserResult):
        item = source(source_id)
        try:
            if canonical_url(request.url) != item["canonical_url"]:
                raise ValueError("Browser result URL must match source canonical URL")
            assessment = assess_content(
                request.markdown,
                request.title or item.get("discovery_title", item["title"]),
                request.status_code,
            )
            return store().finish(
                source_id,
                dict(
                    assessment,
                    markdown=request.markdown,
                    title=request.title or item.get("discovery_title", item["title"]),
                    method=request.method,
                    error=None,
                    next_action=None
                    if assessment["status"] == "read"
                    else "retry_or_import_browser_result",
                    provenance=[
                        *item["provenance"],
                        {"kind": "browser_import", "url": request.url, "method": request.method},
                    ],
                ),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.post("/sources/{source_id}/retry")
    def retry(source_id: str):
        item = source(source_id)
        if item["retry_count"] >= 5:
            raise HTTPException(409, "Retry limit reached")
        try:
            return store().finish(
                source_id, {"status": "queued", "next_action": "collect", "error": None}, retry=True
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.post("/sources/{source_id}/skip")
    def skip(source_id: str):
        source(source_id)
        try:
            return store().finish(source_id, {"status": "skipped", "next_action": None})
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    return router
