"""Short SQLite transactions; no transaction survives a network await."""

import json
import hashlib
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .content import canonical_url


class Store:
    def __init__(self, path=None):
        self.path = str(path or os.environ.get("LOCAL_WEB_RESEARCH_DB", "work/research.sqlite3"))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, canonical_url TEXT NOT NULL,
                    status TEXT NOT NULL, lease REAL DEFAULT 0, token TEXT, data TEXT NOT NULL,
                    UNIQUE(run_id, canonical_url));
                CREATE INDEX IF NOT EXISTS source_queue ON sources(run_id,status);
                CREATE TABLE IF NOT EXISTS searches (
                    run_id TEXT, query TEXT, page INTEGER, status TEXT, data TEXT,
                    PRIMARY KEY(run_id,query,page));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, payload):
        data = dict(
            payload,
            id=uuid.uuid4().hex,
            created_at=time.time(),
            status="active",
            requests_used=0,
            content_chars=0,
            forum_pages=0,
        )
        queries = [
            data["query"],
            *data["variants"],
            *(data["query"] + " " + topic for topic in data["subtopics"]),
        ]
        data["queries"] = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[
            : data["max_queries"]
        ]
        with self.connection() as db:
            db.execute("INSERT INTO runs VALUES (?,?)", (data["id"], json.dumps(data)))
        return data

    def run(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row["data"])

    def runs(self):
        with self.connection() as db:
            return [
                json.loads(r["data"])
                for r in db.execute("SELECT data FROM runs ORDER BY rowid DESC")
            ]

    def sources(self, run_id):
        with self.connection() as db:
            return [
                json.loads(r["data"])
                for r in db.execute(
                    "SELECT data FROM sources WHERE run_id=? ORDER BY rowid", (run_id,)
                )
            ]

    def source(self, source_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise KeyError(source_id)
        return json.loads(row["data"])

    def add_source(self, run_id, url, title, provenance, forum=False, return_created=False):
        canonical = canonical_url(url)
        with self.connection() as db:
            run = json.loads(
                db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()["data"]
            )
            row = db.execute(
                "SELECT data FROM sources WHERE run_id=? AND canonical_url=?", (run_id, canonical)
            ).fetchone()
            if row:
                data = json.loads(row["data"])
                if provenance not in data["provenance"]:
                    data["provenance"].append(provenance)
                    db.execute(
                        "UPDATE sources SET data=? WHERE id=?", (json.dumps(data), data["id"])
                    )
                return (data, False) if return_created else data
            count = db.execute("SELECT COUNT(*) FROM sources WHERE run_id=?", (run_id,)).fetchone()[
                0
            ]
            if (
                count >= run["max_sources"]
                or forum
                and run["forum_pages"] >= run["max_forum_pages"]
            ):
                return (None, False) if return_created else None
            data = dict(
                id=uuid.uuid4().hex,
                run_id=run_id,
                url=canonical,
                canonical_url=canonical,
                title=title or "",
                discovery_title=title or "",
                status="queued",
                retry_count=0,
                attempts=0,
                markdown="",
                content_sha256=None,
                history=[],
                reasons=[],
                provenance=[provenance],
                method=None,
                error=None,
                next_action=None,
            )
            db.execute(
                "INSERT INTO sources(id,run_id,canonical_url,status,data) VALUES (?,?,?,?,?)",
                (data["id"], run_id, canonical, "queued", json.dumps(data)),
            )
            if forum:
                run["forum_pages"] += 1
                db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
            return (data, True) if return_created else data

    def reserve(self, run_id, query=None, page=None):
        with self.connection() as db:
            run = json.loads(
                db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()["data"]
            )
            if (
                run["requests_used"] >= run["max_requests"]
                or run["content_chars"] >= run["max_content_chars"]
            ):
                return False
            if query is not None:
                distinct = db.execute(
                    "SELECT COUNT(DISTINCT query) FROM searches WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                known = db.execute(
                    "SELECT 1 FROM searches WHERE run_id=? AND query=?", (run_id, query)
                ).fetchone()
                if page > run["max_pages"] or not known and distinct >= run["max_queries"]:
                    return False
                existing = db.execute(
                    "SELECT status,data FROM searches WHERE run_id=? AND query=? AND page=?",
                    (run_id, query, page),
                ).fetchone()
                prior = json.loads(existing["data"]) if existing else {}
                attempts = prior.get("attempts", 1 if existing else 0)
                if existing:
                    live = existing["status"] == "running" and prior.get("lease", 0) > time.time()
                    successful = existing["status"] == "finished" and not prior.get("error")
                    if live or successful or attempts >= 3:
                        return False
                reservation = uuid.uuid4().hex
                metadata = {
                    "attempts": attempts + 1,
                    "lease": time.time() + 300,
                    "token": reservation,
                    "reserved_at": time.time(),
                }
                db.execute(
                    "INSERT INTO searches VALUES (?,?,?,?,?) ON CONFLICT(run_id,query,page) DO UPDATE SET status=excluded.status,data=excluded.data",
                    (run_id, query, page, "running", json.dumps(metadata)),
                )
            run["requests_used"] += 1
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
            return reservation if query is not None else True

    def search_result(self, run_id, query, page, data, reservation=None):
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM searches WHERE run_id=? AND query=? AND page=?",
                (run_id, query, page),
            ).fetchone()
            if not row:
                return
            metadata = json.loads(row["data"])
            if reservation is not None and metadata.get("token") != reservation:
                return
            metadata.update(data, lease=0, finished_at=time.time())
            db.execute(
                "UPDATE searches SET status=?,data=? WHERE run_id=? AND query=? AND page=?",
                ("finished", json.dumps(metadata), run_id, query, page),
            )

    def searches(self, run_id):
        with self.connection() as db:
            return [
                dict(r, data=json.loads(r["data"]))
                for r in db.execute("SELECT * FROM searches WHERE run_id=?", (run_id,))
            ]

    def claim(self, run_id):
        with self.connection() as db:
            run = json.loads(
                db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()["data"]
            )
            if (
                run["requests_used"] >= run["max_requests"]
                or run["content_chars"] >= run["max_content_chars"]
            ):
                return None
            row = db.execute(
                "SELECT * FROM sources WHERE run_id=? AND (status='queued' OR (status='reading' AND lease<?)) ORDER BY rowid LIMIT 1",
                (run_id, time.time()),
            ).fetchone()
            if not row:
                return None
            run["requests_used"] += 1
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
            data = json.loads(row["data"])
            token = uuid.uuid4().hex
            data.update(status="reading", attempts=data["attempts"] + 1)
            db.execute(
                "UPDATE sources SET status=?,lease=?,token=?,data=? WHERE id=?",
                ("reading", time.time() + 300, token, json.dumps(data), data["id"]),
            )
            return data, token

    def finish(self, source_id, changes, token=None, retry=False):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise KeyError(source_id)
            if token and row["token"] != token:
                return json.loads(row["data"])
            if not token and row["status"] == "reading" and row["lease"] > time.time():
                raise ValueError("Source is currently being read")
            data = json.loads(row["data"])
            if retry:
                if data["retry_count"] >= 5:
                    raise ValueError("Retry limit reached")
                changes = dict(changes, retry_count=data["retry_count"] + 1)
            if "markdown" in changes:
                run = json.loads(
                    db.execute("SELECT data FROM runs WHERE id=?", (data["run_id"],)).fetchone()[
                        "data"
                    ]
                )
                available = run["max_content_chars"] - run["content_chars"] + len(data["markdown"])
                original = changes["markdown"]
                changes = dict(changes, markdown=original[: max(0, available)])
                if len(original) > available:
                    changes.update(
                        status="partial",
                        next_action="content_budget_exhausted",
                        reasons=[*changes.get("reasons", []), "content_budget_exhausted"],
                    )
                run["content_chars"] += len(changes["markdown"]) - len(data["markdown"])
                db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), data["run_id"]))
            changes = dict(changes)
            if "provenance" in changes:
                for receipt in changes.pop("provenance"):
                    if receipt not in data["provenance"]:
                        data["provenance"].append(receipt)
            # Merge against the latest durable discovery, never a worker snapshot.
            data.update(changes, updated_at=time.time())
            data["content_sha256"] = (
                hashlib.sha256(data["markdown"].encode()).hexdigest() if data["markdown"] else None
            )
            if token or "markdown" in changes:
                receipt = {
                    key: data.get(key)
                    for key in ("updated_at", "status", "method", "reasons", "error")
                }
                data["history"] = [*data.get("history", []), receipt][-50:]
            db.execute(
                "UPDATE sources SET status=?,lease=0,token=NULL,data=? WHERE id=?",
                (data["status"], json.dumps(data), source_id),
            )
            return data
