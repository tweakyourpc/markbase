"""
MarkBase FastAPI application.

Serves the single-file frontend and the JSON API backing the ingestion
dashboard + reader. The background queue worker is started on app startup.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import ingest
import queue_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("markbase.app")

app = FastAPI(title="MarkBase", version="1.0.0")
STARTED_AT = datetime.now(timezone.utc).isoformat()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.on_event("startup")
def _startup() -> None:
    ingest.ensure_dirs()
    ingest.purge_expired_trash(days=30)  # 30-day retention
    ingest.update_index()
    queue_worker.start_worker()
    log.info("MarkBase ready. Library: %s", ingest.library_path())


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _safe_item_dir(rel_path: str) -> Path:
    """Resolve a library-relative item path, refusing traversal escapes."""
    rel = unquote(rel_path).strip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="Invalid path")
    if any(part.startswith("_") for part in Path(rel).parts):
        raise HTTPException(status_code=400, detail="Invalid path")
    root = ingest.library_path()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Item not found")
    return target


def _read_content_md(item_dir: Path) -> str:
    for name in ("content.md", "transcript.md"):
        f = item_dir / name
        if f.exists():
            return f.read_text(encoding="utf-8")
    return ""


YT_VIDEO_RE = re.compile(r"(youtube\.com/watch\?|youtu\.be/|youtube\.com/shorts/)", re.I)
YT_CHANNEL_RE = re.compile(r"youtube\.com/(@[\w.-]+|channel/|c/|user/|playlist\?)", re.I)


def detect_source_type(value: str) -> str:
    v = value.strip()
    if v.startswith("@") and " " not in v:
        return "youtube_channel"
    if "youtube.com" in v or "youtu.be" in v:
        if YT_VIDEO_RE.search(v):
            return "youtube_video"
        if YT_CHANNEL_RE.search(v):
            return "youtube_channel"
        return "youtube_video"
    return "url"


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/whoami")
def whoami() -> JSONResponse:
    return JSONResponse(
        {
            "service": "markbase",
            "version": app.version,
            "pid": os.getpid(),
            "startedAt": STARTED_AT,
            "host": os.environ.get("MARKBASE_HOST", "0.0.0.0"),
            "port": int(os.environ["MARKBASE_PORT"]) if os.environ.get("MARKBASE_PORT") else None,
        }
    )


# --------------------------------------------------------------------------- #
# Library / items
# --------------------------------------------------------------------------- #


@app.get("/api/library")
def api_library() -> JSONResponse:
    # Serves a cached index; only rebuilds when the library actually changed.
    return JSONResponse(ingest.get_index())


@app.get("/api/item/{path:path}")
def api_item(path: str) -> JSONResponse:
    item_dir = _safe_item_dir(path)
    meta = ingest.read_json(item_dir / "metadata.json")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail="Item metadata not found")
    meta = {**ingest.new_metadata(), **meta}
    meta["path"] = item_dir.relative_to(ingest.library_path()).as_posix()
    return JSONResponse({"metadata": meta, "markdown": _read_content_md(item_dir)})


@app.delete("/api/item/{path:path}")
def api_delete_item(path: str, permanent: bool = False) -> JSONResponse:
    """Delete an item — moved to _trash by default, or removed with ?permanent=true."""
    item_dir = _safe_item_dir(path)
    return JSONResponse(ingest.delete_item(item_dir, permanent=permanent))


@app.get("/api/trash")
def api_trash_list() -> JSONResponse:
    return JSONResponse({"items": ingest.list_trash()})


@app.post("/api/trash/empty")
def api_empty_trash() -> JSONResponse:
    return JSONResponse({"purged": ingest.empty_trash()})


@app.post("/api/trash/{trash_name}/restore")
def api_trash_restore(trash_name: str) -> JSONResponse:
    try:
        return JSONResponse(ingest.restore_item(unquote(trash_name)))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/trash/{trash_name}")
def api_trash_delete(trash_name: str) -> JSONResponse:
    try:
        return JSONResponse(ingest.delete_trash_item(unquote(trash_name)))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/channel/{handle}")
def api_channel(handle: str) -> JSONResponse:
    handle = unquote(handle)
    if not handle.startswith("@"):
        handle = "@" + handle
    channel_root = ingest.youtube_dir() / handle
    if not channel_root.is_dir():
        raise HTTPException(status_code=404, detail="Channel not found")

    channel_meta = ingest.read_json(channel_root / "channel.json", default={"handle": handle})
    videos: list[dict[str, Any]] = []
    videos_root = channel_root / "videos"
    if videos_root.is_dir():
        for meta_path in sorted(videos_root.rglob("metadata.json")):
            m = ingest.read_json(meta_path)
            if isinstance(m, dict):
                m = {**ingest.new_metadata(), **m}
                m["path"] = meta_path.parent.relative_to(ingest.library_path()).as_posix()
                videos.append(m)
    videos.sort(key=lambda m: m.get("date_published") or m.get("date_ingested") or "", reverse=True)
    return JSONResponse({"channel": channel_meta, "videos": videos})


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


@app.post("/api/ingest")
async def api_ingest(
    url: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> JSONResponse:
    if file is not None:
        # Persist the upload to a temp location for the worker to convert.
        uploads = ingest.library_path() / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "upload").suffix
        fd, tmp = tempfile.mkstemp(dir=str(uploads), suffix=suffix)
        with open(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        job_id = queue_worker.add_job("file", tmp, original_name=file.filename)
        return JSONResponse({"job_id": job_id, "type": "file", "name": file.filename})

    if url and url.strip():
        stype = detect_source_type(url)
        job_type = {
            "youtube_video": "youtube_video",
            "youtube_channel": "youtube_channel",
            "url": "url",
        }[stype]
        job_id = queue_worker.add_job(job_type, url.strip())
        return JSONResponse({"job_id": job_id, "type": job_type, "url": url.strip()})

    raise HTTPException(status_code=400, detail="Provide a 'url' or a 'file'.")


@app.get("/api/queue")
def api_queue() -> JSONResponse:
    return JSONResponse(queue_worker.queue_status())


@app.post("/api/queue/clear-completed")
def api_queue_clear_completed() -> JSONResponse:
    return JSONResponse({"cleared": queue_worker.clear_finished_jobs()})


# --------------------------------------------------------------------------- #
# Notes ("Save to MarkBase") — synchronous, no conversion needed
# --------------------------------------------------------------------------- #


@app.post("/api/note")
def api_note(body: dict[str, Any]) -> JSONResponse:
    title = str(body.get("title") or "").strip()
    content = str(body.get("content") or "")
    if not title and not content.strip():
        raise HTTPException(status_code=400, detail="A note needs a title or content.")
    tags = body.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    path = ingest.save_note(
        title=title,
        content=content,
        tags=list(tags),
        source_url=(str(body.get("source_url")).strip() or None) if body.get("source_url") else None,
    )
    return JSONResponse({"path": path, "title": title or "Untitled Note"})


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


@app.get("/api/search")
def api_search(q: str = "") -> JSONResponse:
    q = q.strip().lower()
    results: list[dict[str, Any]] = []
    if not q:
        return JSONResponse({"query": q, "results": results})

    root = ingest.library_path()
    for meta_path in ingest.iter_metadata_files():
        meta = ingest.read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        meta = {**ingest.new_metadata(), **meta}
        item_dir = meta_path.parent
        meta["path"] = item_dir.relative_to(root).as_posix()

        haystack = " ".join(
            str(meta.get(k) or "") for k in ("title", "channel", "source_url")
        ).lower()
        haystack += " " + " ".join(meta.get("tags") or []).lower()

        body = _read_content_md(item_dir).lower()
        snippet = ""
        in_meta = q in haystack
        idx = body.find(q)
        if idx >= 0:
            start = max(0, idx - 60)
            snippet = ("…" if start else "") + body[start : idx + 120].replace("\n", " ")

        if in_meta or idx >= 0:
            results.append({**meta, "snippet": snippet})

    return JSONResponse({"query": q, "results": results})


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #


@app.post("/api/tag")
def api_tag(body: dict[str, Any]) -> JSONResponse:
    path = body.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="'path' is required")
    item_dir = _safe_item_dir(path)
    meta_file = item_dir / "metadata.json"
    meta = ingest.read_json(meta_file)
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail="Item metadata not found")
    meta = {**ingest.new_metadata(), **meta}

    tags = list(meta.get("tags") or [])
    if "tags" in body and isinstance(body["tags"], list):
        tags = [str(t).strip() for t in body["tags"] if str(t).strip()]
    if body.get("add"):
        t = str(body["add"]).strip()
        if t and t not in tags:
            tags.append(t)
    if body.get("remove"):
        tags = [t for t in tags if t != str(body["remove"]).strip()]

    # De-duplicate while preserving order.
    seen: set[str] = set()
    meta["tags"] = [t for t in tags if not (t in seen or seen.add(t))]

    ingest.atomic_write_json(meta_file, meta)
    ingest.update_index()
    return JSONResponse({"path": path, "tags": meta["tags"]})
