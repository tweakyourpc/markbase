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
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import ingest
import queue_worker

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("markbase.app")

app = FastAPI(title="MarkBase", version="1.0.0")
STARTED_AT = datetime.now(timezone.utc).isoformat()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.on_event("startup")
def _startup() -> None:
    ingest.ensure_dirs()
    ingest.purge_expired_trash(days=30)  # 30-day retention
    ingest.purge_stale_upload_staging(days=7)
    ingest.purge_expired_retained_originals()
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


def _content_file(item_dir: Path) -> Path | None:
    for name in ("content.md", "transcript.md"):
        f = item_dir / name
        if f.exists():
            return f
    return None


def _read_content_md(item_dir: Path) -> str:
    f = _content_file(item_dir)
    return f.read_text(encoding="utf-8") if f else ""


def _ensure_editable_markdown(
    item_dir: Path, metadata: dict[str, Any]
) -> tuple[Path, str]:
    existing = _content_file(item_dir)
    title = metadata.get("title") or "Untitled"
    source_text = existing.read_text(encoding="utf-8") if existing else ""
    content_path = item_dir / "content.md"
    if content_path.exists():
        return content_path, source_text

    structured = (
        f"# {title}\n\n"
        "## Original transcription / imported source\n\n"
        f"{source_text.strip() or '_No original source text available._'}\n\n"
        "---\n\n"
        "## Notes / annotations\n\n"
        "_Add notes, highlights, corrections, or commentary here._\n\n"
        "---\n\n"
        "## Edited version\n\n"
        "_Optionally create a cleaned up or corrected version here._\n"
    )
    ingest.atomic_write_text(content_path, structured)
    return content_path, structured


YT_VIDEO_RE = re.compile(
    r"(youtube\.com/watch\?|youtu\.be/|youtube\.com/shorts/)", re.I
)
YT_CHANNEL_RE = re.compile(
    r"youtube\.com/(@[\w.-]+|channel/|c/|user/|playlist\?)", re.I
)


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
            "port": int(os.environ["MARKBASE_PORT"])
            if os.environ.get("MARKBASE_PORT")
            else None,
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
    markdown = _read_content_md(item_dir)
    editable_file = _content_file(item_dir)
    return JSONResponse(
        {
            "metadata": meta,
            "markdown": markdown,
            "editable": editable_file.name if editable_file else None,
        }
    )


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

    channel_meta = ingest.read_json(
        channel_root / "channel.json", default={"handle": handle}
    )
    videos: list[dict[str, Any]] = []
    videos_root = channel_root / "videos"
    if videos_root.is_dir():
        for meta_path in sorted(videos_root.rglob("metadata.json")):
            m = ingest.read_json(meta_path)
            if isinstance(m, dict):
                m = {**ingest.new_metadata(), **m}
                m["path"] = meta_path.parent.relative_to(
                    ingest.library_path()
                ).as_posix()
                videos.append(m)
    videos.sort(
        key=lambda m: m.get("date_published") or m.get("date_ingested") or "",
        reverse=True,
    )
    return JSONResponse({"channel": channel_meta, "videos": videos})


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


@app.post("/api/ingest")
async def api_ingest(
    url: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    keep_original: str | None = Form(default=None),
    title: str | None = Form(default=None),
    notes: str | None = Form(default=None),
) -> JSONResponse:
    user_title = (title or "").strip() or None
    user_notes = (notes or "").strip() or None
    if file is not None:
        uploads = ingest.upload_stage_dir()
        uploads.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "upload").suffix
        fd, tmp = tempfile.mkstemp(dir=str(uploads), suffix=suffix)
        with open(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        keep_original_bool = str(keep_original or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        job_id = queue_worker.add_job(
            "file",
            tmp,
            original_name=file.filename,
            keep_original=keep_original_bool,
            user_title=user_title,
            user_notes=user_notes,
        )
        return JSONResponse({"job_id": job_id, "type": "file", "name": file.filename})

    if url and url.strip():
        stype = detect_source_type(url)
        job_type = {
            "youtube_video": "youtube_video",
            "youtube_channel": "youtube_channel",
            "url": "url",
        }[stype]
        job_id = queue_worker.add_job(
            job_type,
            url.strip(),
            user_title=user_title,
            user_notes=user_notes,
        )
        return JSONResponse({"job_id": job_id, "type": job_type, "url": url.strip()})

    raise HTTPException(status_code=400, detail="Provide a 'url' or a 'file'.")


@app.get("/api/queue")
def api_queue() -> JSONResponse:
    return JSONResponse(queue_worker.queue_status())


@app.post("/api/queue/clear-completed")
def api_queue_clear_completed() -> JSONResponse:
    return JSONResponse({"cleared": queue_worker.clear_finished_jobs()})


@app.post("/api/maintenance/restart")
def api_maintenance_restart() -> JSONResponse:
    subprocess.Popen(
        ["/bin/bash", "-lc", "sleep 1 && systemctl --user restart markbase.service"],
        start_new_session=True,
    )
    return JSONResponse({"ok": True, "message": "Restart scheduled"})


@app.get("/api/maintenance/settings")
def api_maintenance_settings() -> JSONResponse:
    settings = ingest.get_settings()
    settings["paths"] = {
        "library": str(ingest.library_path()),
        "uploads": str(ingest.upload_stage_dir()),
        "trash": str(ingest.trash_dir()),
        "state": str(ingest.state_path()),
    }
    return JSONResponse(settings)


@app.post("/api/maintenance/settings")
def api_maintenance_settings_save(body: dict[str, Any]) -> JSONResponse:
    settings = {
        "retain_original_uploads_default": bool(
            body.get("retain_original_uploads_default")
        ),
        "retained_originals_purge_mode": str(
            body.get("retained_originals_purge_mode") or "days"
        ),
        "retained_originals_days": int(body.get("retained_originals_days") or 30),
    }
    saved = ingest.save_settings(settings)
    purged = ingest.purge_expired_retained_originals()
    saved["purged_retained_originals"] = purged
    saved["paths"] = {
        "library": str(ingest.library_path()),
        "uploads": str(ingest.upload_stage_dir()),
        "trash": str(ingest.trash_dir()),
        "state": str(ingest.state_path()),
    }
    return JSONResponse(saved)


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
    user_notes = (str(body.get("notes") or "").strip()) or None
    path = ingest.save_note(
        title=title,
        content=content,
        tags=list(tags),
        source_url=(str(body.get("source_url")).strip() or None)
        if body.get("source_url")
        else None,
        user_notes=user_notes,
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
    index = ingest.get_index()

    for meta in index.get("items", []):
        if not isinstance(meta, dict):
            continue

        haystack = " ".join(
            str(meta.get(k) or "") for k in ("title", "channel", "source_url")
        ).lower()
        haystack += " " + " ".join(str(t) for t in (meta.get("tags") or [])).lower()

        in_meta = q in haystack
        snippet = ""
        idx = -1

        if not in_meta:
            item_dir = (root / meta["path"]) if meta.get("path") else None
            if item_dir and item_dir.is_dir():
                for name in ("content.md", "transcript.md"):
                    f = item_dir / name
                    if f.exists():
                        body = f.read_text(encoding="utf-8").lower()
                        idx = body.find(q)
                        if idx >= 0:
                            start = max(0, idx - 60)
                            snippet = (
                                ("…" if start else "")
                                + body[start : idx + 120].replace("\n", " ")
                            )
                        break

        if in_meta or idx >= 0:
            results.append({**meta, "snippet": snippet})

    return JSONResponse({"query": q, "results": results})


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #


def _replace_h1(text: str, new_title: str) -> str:
    """Replace the first level-1 heading in text with the given title."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            lines[i] = f"# {new_title}"
            break
    return "\n".join(lines)


@app.post("/api/item/{path:path}/edit")
def api_edit_item(path: str, body: dict[str, Any]) -> JSONResponse:
    item_dir = _safe_item_dir(path)
    meta = ingest.read_json(item_dir / "metadata.json")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail="Item metadata not found")
    meta = {**ingest.new_metadata(), **meta}

    new_title = str(body.get("title") or "").strip() or None
    content = str(body.get("markdown") or "").strip()
    if not new_title and not content:
        raise HTTPException(
            status_code=400, detail="Provide at least one of 'title' or 'markdown'."
        )

    content_path, existing_content = _ensure_editable_markdown(item_dir, meta)

    # If a new title was given, update metadata and the H1 in content.
    if new_title:
        meta["title"] = new_title
        existing_content = _replace_h1(existing_content, new_title)

    # If new markdown was given, use it; otherwise use the (possibly
    # title-updated) existing content so the H1 stays in sync.
    updated_content = content if content else existing_content
    ingest.atomic_write_text(content_path, updated_content)
    meta["word_count"] = len(updated_content.split())
    ingest.atomic_write_json(item_dir / "metadata.json", meta)
    ingest.update_index()
    return JSONResponse({"path": path, "saved": True, "editable": content_path.name})


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
