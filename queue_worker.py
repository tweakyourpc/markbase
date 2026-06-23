"""
MarkBase background job queue.

A SQLite-backed FIFO queue processed by a single background thread, one job at
a time. Jobs map to ingest.py entry points by `type`:

    youtube_video   -> ingest.ingest_youtube_video(url)
    youtube_channel -> ingest.ingest_youtube_channel(url)   (fans out more jobs)
    file            -> ingest.ingest_file(path, original_name)
    url             -> ingest.ingest_file(path=url, source_url=url)

Failures are caught and recorded so they surface in the UI.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ingest

log = logging.getLogger("markbase.queue")

_LOCK = threading.Lock()
_WORKER_STARTED = False


def _db_path() -> Path:
    ingest.ensure_dirs()
    return ingest.state_path() / "jobs.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                type          TEXT NOT NULL,
                payload       TEXT NOT NULL,          -- url or file path
                original_name TEXT,
                keep_original INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'queued',
                result_path   TEXT,
                error_message TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "keep_original" not in cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN keep_original INTEGER NOT NULL DEFAULT 0"
            )
        if "user_title" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_title TEXT")
        if "user_notes" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_notes TEXT")
        # Recover any jobs that were mid-flight on a previous crash.
        conn.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE status='processing'",
            (_now(),),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Public queue API
# --------------------------------------------------------------------------- #


def _dedupe_key(job_type: str, payload: str) -> str | None:
    if job_type not in {"url", "youtube_video"}:
        return None
    return ingest.normalize_source_url(payload)


def _find_active_duplicate(
    conn: sqlite3.Connection, job_type: str, payload: str
) -> int | None:
    key = _dedupe_key(job_type, payload)
    if not key:
        return None
    rows = conn.execute(
        "SELECT id, payload FROM jobs WHERE type=? AND status IN ('queued', 'processing') ORDER BY id ASC",
        (job_type,),
    ).fetchall()
    for row in rows:
        if ingest.normalize_source_url(row["payload"]) == key:
            return int(row["id"])
    return None


def add_job(
    job_type: str,
    payload: str,
    original_name: str | None = None,
    keep_original: bool = False,
    user_title: str | None = None,
    user_notes: str | None = None,
) -> int:
    ts = _now()
    existing_result = None
    if job_type in {"url", "youtube_video"}:
        existing_result = ingest.find_existing_by_source_url(payload)

    with _LOCK, _connect() as conn:
        duplicate_id = _find_active_duplicate(conn, job_type, payload)
        if duplicate_id is not None:
            log.info(
                "duplicate active job #%s reused for (%s) %s",
                duplicate_id,
                job_type,
                payload,
            )
            return duplicate_id

        if existing_result:
            cur = conn.execute(
                """INSERT INTO jobs (type, payload, original_name, keep_original, user_title, user_notes, status, result_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'done', ?, ?, ?)""",
                (
                    job_type,
                    payload,
                    original_name,
                    int(bool(keep_original)),
                    user_title,
                    user_notes,
                    existing_result,
                    ts,
                    ts,
                ),
            )
            job_id = cur.lastrowid
            log.info(
                "source already ingested; recorded job #%s done -> %s",
                job_id,
                existing_result,
            )
            return int(job_id)

        cur = conn.execute(
            """INSERT INTO jobs (type, payload, original_name, keep_original, user_title, user_notes, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (
                job_type,
                payload,
                original_name,
                int(bool(keep_original)),
                user_title,
                user_notes,
                ts,
                ts,
            ),
        )
        job_id = cur.lastrowid
    log.info("queued job #%s (%s) %s", job_id, job_type, payload)
    return int(job_id)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def get_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def queue_status() -> dict[str, Any]:
    jobs = get_jobs(limit=50)
    counts: dict[str, int] = {"queued": 0, "processing": 0, "done": 0, "failed": 0}
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return {"counts": counts, "jobs": jobs}


def clear_finished_jobs() -> int:
    """Remove finished jobs (done or failed) from the queue history."""
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE status IN ('done', 'failed')")
        return int(cur.rowcount or 0)


def _set_status(
    job_id: int,
    status: str,
    error_message: str | None = None,
    result_path: str | None = None,
) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """UPDATE jobs
               SET status=?, error_message=?, result_path=COALESCE(?, result_path), updated_at=?
               WHERE id=?""",
            (status, error_message, result_path, _now(), job_id),
        )


def _claim_next() -> dict[str, Any] | None:
    """Atomically claim the oldest queued job, marking it processing."""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        return _row_to_dict(row)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


def _process(job: dict[str, Any]) -> str | None:
    jtype = job["type"]
    payload = job["payload"]
    user_title = job.get("user_title")
    user_notes = job.get("user_notes")
    if jtype == "youtube_video":
        return ingest.ingest_youtube_video(
            payload,
            user_title=user_title,
            user_notes=user_notes,
        )
    if jtype == "youtube_channel":
        urls = ingest.ingest_youtube_channel(payload)
        return f"fanned out {len(urls)} videos"
    if jtype == "file":
        return ingest.ingest_file(
            payload,
            original_name=job.get("original_name"),
            keep_original=bool(job.get("keep_original")),
            user_title=user_title,
            user_notes=user_notes,
        )
    if jtype == "url":
        return ingest.ingest_file(
            payload,
            source_url=payload,
            user_title=user_title,
            user_notes=user_notes,
        )
    raise ValueError(f"unknown job type: {jtype}")


def _worker_loop() -> None:
    log.info("queue worker started")
    while True:
        job = _claim_next()
        if job is None:
            time.sleep(2)
            continue
        log.info("processing job #%s (%s)", job["id"], job["type"])
        try:
            result = _process(job)
            _set_status(job["id"], "done", result_path=result)
            log.info("job #%s done -> %s", job["id"], result)
        except Exception as exc:  # noqa: BLE001 — never let the worker die
            log.exception("job #%s failed", job["id"])
            _set_status(job["id"], "failed", error_message=str(exc))
        finally:
            if job.get("type") == "file":
                try:
                    Path(str(job.get("payload") or "")).unlink(missing_ok=True)
                except OSError:
                    pass


def start_worker() -> None:
    """Start the background worker thread exactly once."""
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    init_db()
    t = threading.Thread(target=_worker_loop, name="markbase-worker", daemon=True)
    t.start()
