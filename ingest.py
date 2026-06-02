"""
MarkBase ingestion pipeline (Phase 1 — deterministic, no AI).

Responsibilities:
  * Resolve the library location from MARKBASE_LIBRARY_PATH.
  * Convert YouTube videos / channels / files into Markdown + metadata.
  * Maintain the master library/index.json.

All file writes are atomic (temp file + os.replace) to avoid corruption,
and index.json is always rebuilt from scratch by walking the tree.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("markbase.ingest")

# --------------------------------------------------------------------------- #
# Paths / configuration
# --------------------------------------------------------------------------- #


def library_path() -> Path:
    """Root of the content library. Honors MARKBASE_LIBRARY_PATH."""
    root = Path(os.environ.get("MARKBASE_LIBRARY_PATH", "./library")).expanduser()
    return root.resolve()


def youtube_dir() -> Path:
    return library_path() / "youtube"


def docs_dir() -> Path:
    return library_path() / "docs"


def notes_dir() -> Path:
    return library_path() / "notes"


def trash_dir() -> Path:
    return library_path() / "_trash"


def state_path() -> Path:
    """
    Location for operational state (e.g. the SQLite job queue). Defaults to the
    library, but can be set separately via MARKBASE_STATE_PATH — useful when the
    library lives on a network mount where SQLite locking is unreliable.
    """
    p = os.environ.get("MARKBASE_STATE_PATH")
    return Path(p).expanduser().resolve() if p else library_path()


# Top-level directory names that hold content (everything else, e.g. names
# starting with "_", is internal: _trash, _uploads, and is never indexed).
def _is_internal(rel: Path) -> bool:
    return any(part.startswith("_") for part in rel.parts)


def iter_metadata_files() -> list[Path]:
    """All item metadata.json paths, excluding internal dirs (_trash, _uploads)."""
    root = library_path()
    out: list[Path] = []
    for p in root.rglob("metadata.json"):
        if not _is_internal(p.parent.relative_to(root)):
            out.append(p)
    return sorted(out)


def index_file() -> Path:
    return library_path() / "index.json"


def ensure_dirs() -> None:
    """Create the baseline directory layout if it does not yet exist."""
    youtube_dir().mkdir(parents=True, exist_ok=True)
    docs_dir().mkdir(parents=True, exist_ok=True)
    notes_dir().mkdir(parents=True, exist_ok=True)
    state_path().mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Atomic IO helpers
# --------------------------------------------------------------------------- #


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to `path` atomically (write temp, fsync, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Slugs
# --------------------------------------------------------------------------- #


def slugify(text: str) -> str:
    """Deterministic lowercase-hyphenated slug."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text or "untitled"


def unique_slug(base: str, parent: Path) -> str:
    """
    Collision-safe slug: if `parent/base` already exists, append -2, -3, ...
    Deterministic given the existing contents of `parent`.
    """
    slug = slugify(base)
    candidate = slug
    n = 2
    while (parent / candidate).exists():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


# --------------------------------------------------------------------------- #
# Subprocess wrappers
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    log.info("exec: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_markitdown(source: str) -> str:
    """Run `markitdown <source>` and return the Markdown on stdout."""
    proc = _run(["markitdown", source])
    if proc.returncode != 0:
        raise RuntimeError(
            f"markitdown failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    if not proc.stdout.strip():
        raise RuntimeError("markitdown produced no output")
    return proc.stdout


def ytdlp_json(url: str, extra: Iterable[str] = ()) -> str:
    """Run yt-dlp dumping JSON; return raw stdout."""
    cmd = ["yt-dlp", "--no-warnings", "--skip-download", *extra, url]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def _ytdlp_available() -> bool:
    return shutil.which("yt-dlp") is not None


# --------------------------------------------------------------------------- #
# Transcript extraction via yt-dlp subtitles (robust path)
# --------------------------------------------------------------------------- #


def _vtt_to_text(vtt: str) -> str:
    """
    Convert a WebVTT subtitle file into readable prose: drop headers, cue
    timing, inline word-timing tags, and the rolling duplicate lines that
    auto-captions produce. Re-flow into paragraphs.
    """
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or "-->" in line:
            continue
        if line in ("WEBVTT",) or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if line.isdigit():  # cue index
            continue
        # Strip inline timing/styling tags like <00:00:01.000> and <c>...</c>.
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s+(align|position):\S+", "", line)
        line = line.strip()
        if not line:
            continue
        # Auto-captions repeat the previous line as a rolling window; skip dupes.
        if lines and (line == lines[-1] or line in lines[-1]):
            continue
        lines.append(line)

    # Re-flow short caption fragments into ~sentence-grouped paragraphs.
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    paras: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        buf = (buf + " " + sentence).strip()
        if len(buf) >= 320:
            paras.append(buf)
            buf = ""
    if buf:
        paras.append(buf)
    return "\n\n".join(paras)


def fetch_transcript_via_ytdlp(url: str) -> str | None:
    """
    Download captions with yt-dlp and return them as clean text. Prefers
    human-authored subtitles; falls back to auto-generated. Returns None if no
    captions are available or yt-dlp isn't installed.
    """
    if not _ytdlp_available():
        return None

    langs = "en.*,en"
    with tempfile.TemporaryDirectory(prefix="mb-subs-") as tmp:
        out_tmpl = os.path.join(tmp, "%(id)s.%(ext)s")

        def _grab(auto: bool) -> str | None:
            flag = "--write-auto-subs" if auto else "--write-subs"
            proc = _run(
                [
                    "yt-dlp", "--no-warnings", "--skip-download", flag,
                    "--sub-langs", langs, "--sub-format", "vtt",
                    "-o", out_tmpl, url,
                ]
            )
            if proc.returncode != 0:
                log.warning("yt-dlp subtitle fetch failed (auto=%s): %s", auto, proc.stderr.strip()[:200])
            vtts = sorted(Path(tmp).glob("*.vtt"), key=lambda p: p.stat().st_size, reverse=True)
            return vtts[0].read_text(encoding="utf-8", errors="replace") if vtts else None

        # 1) human subtitles, 2) auto-captions.
        vtt = _grab(auto=False)
        if not vtt:
            for f in Path(tmp).glob("*.vtt"):
                f.unlink()
            vtt = _grab(auto=True)

    if not vtt:
        return None
    text = _vtt_to_text(vtt)
    return text or None


def _looks_like_failed_markitdown(md: str) -> bool:
    """Detect markitdown's YouTube failure output (empty-XML retries + footer)."""
    low = md.lower()
    if "no element found" in low and "attempt" in low:
        return True
    # Just the YouTube chrome/footer, no real content.
    if "© " in md and "nfl sunday ticket" in low and len(md) < 1500:
        return True
    return False


# --------------------------------------------------------------------------- #
# Metadata schema helper
# --------------------------------------------------------------------------- #


def new_metadata(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": "",
        "title": "Untitled",
        "source_url": None,
        "source_type": "url",
        "channel": None,
        "date_ingested": now_iso(),
        "date_published": None,
        "duration_seconds": 0,
        "word_count": 0,
        "tags": [],
        "status": "converted",
        "ai_status": "none",
        "thumbnail_url": None,
    }
    meta.update(overrides)
    return meta


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _parse_upload_date(raw: Any) -> str | None:
    """yt-dlp gives YYYYMMDD; convert to ISO date."""
    if not raw:
        return None
    s = str(raw)
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None
    return s


_ISO_DURATION_RE = re.compile(
    r"^P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", re.I
)


def parse_duration_to_seconds(value: Any) -> int:
    """
    Normalize a duration into integer seconds. Accepts:
      * ints/floats (already seconds)
      * ISO 8601 durations like 'PT21M54S' (as markitdown emits for YouTube)
    Returns 0 if it can't be parsed.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    m = _ISO_DURATION_RE.match(s)
    if m:
        h, mins, secs = (int(g) if g else 0 for g in m.groups())
        return h * 3600 + mins * 60 + secs
    return 0


def parse_markitdown_youtube_header(md: str) -> dict[str, Any]:
    """
    Best-effort extraction of metadata from markitdown's YouTube output, used
    as a fallback when yt-dlp is unavailable. markitdown emits, e.g.:

        # YouTube
        ## Switching back to Windows?!?
        ### Video Metadata
        - **Runtime:** PT21M54S
        ### Description
        ...

    Returns any of: title, duration_seconds, channel.
    """
    out: dict[str, Any] = {}
    for line in md.splitlines()[:60]:
        line = line.strip()
        if not out.get("title") and line.startswith("## ") and not line.startswith("### "):
            candidate = line[3:].strip()
            if candidate and candidate.lower() != "youtube":
                out["title"] = candidate
        m = re.match(r"-\s*\*\*Runtime:\*\*\s*(.+)$", line, re.I)
        if m:
            out["duration_seconds"] = parse_duration_to_seconds(m.group(1).strip())
        m = re.match(r"-\s*\*\*(?:Author|Channel|Uploader):\*\*\s*(.+)$", line, re.I)
        if m:
            out["channel"] = m.group(1).strip()
    return out


# --------------------------------------------------------------------------- #
# YouTube video
# --------------------------------------------------------------------------- #


def ingest_youtube_video(url: str) -> str:
    """
    Convert a single YouTube video into transcript.md + metadata.json under
    library/youtube/<@channel>/videos/<slug>/. Returns the item path
    (relative to the library root).
    """
    ensure_dirs()

    # 1. Pull structured metadata via yt-dlp (preferred, but optional).
    info: dict[str, Any] = {}
    try:
        raw = ytdlp_json(url, extra=["--dump-single-json"])
        info = json.loads(raw.splitlines()[0]) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 — metadata is best-effort
        log.warning("yt-dlp metadata fetch failed for %s: %s", url, exc)

    # 2. Get the transcript. Prefer yt-dlp captions (reliable); fall back to
    #    markitdown only if captions are unavailable.
    header: dict[str, Any] = {}
    transcript_text = fetch_transcript_via_ytdlp(url)
    transcript_source = "yt-dlp captions"
    if not transcript_text:
        md = run_markitdown(url)
        if _looks_like_failed_markitdown(md):
            log.warning("markitdown returned no usable transcript for %s", url)
            transcript_text = ""
        else:
            header = parse_markitdown_youtube_header(md)
            transcript_text = md
        transcript_source = "markitdown"

    # 3. Merge metadata: yt-dlp first, markitdown header as fallback, url last.
    title = info.get("title") or header.get("title") or url

    raw_handle = (
        info.get("uploader_id")
        or info.get("channel_id")
        or info.get("uploader")
        or info.get("channel")
        or header.get("channel")
    )
    if raw_handle:
        handle = str(raw_handle)
        handle = handle if handle.startswith("@") else f"@{slugify(handle)}"
    else:
        handle = "@unknown"

    duration_seconds = parse_duration_to_seconds(info.get("duration")) or header.get(
        "duration_seconds", 0
    )

    # 4. Assemble the transcript document (title + description + transcript).
    description = (info.get("description") or "").strip()
    doc_parts = [f"# {title}", ""]
    if description:
        doc_parts += ["## Description", "", description, ""]
    doc_parts += ["## Transcript", ""]
    if transcript_text.strip():
        doc_parts.append(transcript_text.strip())
    else:
        doc_parts.append("_No transcript or captions were available for this video._")
    document = "\n".join(doc_parts).strip() + "\n"

    # 5. Determine collision-safe destination.
    channel_root = youtube_dir() / handle
    videos_root = channel_root / "videos"
    videos_root.mkdir(parents=True, exist_ok=True)
    slug = unique_slug(title, videos_root)
    item_dir = videos_root / slug
    item_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(item_dir / "transcript.md", document)
    log.info("transcript for %s via %s (%d words)", slug, transcript_source, _word_count(transcript_text))

    # 6. Build + persist metadata.
    meta = new_metadata(
        id=slug,
        title=title,
        source_url=info.get("webpage_url") or url,
        source_type="youtube_video",
        channel=handle,
        date_published=_parse_upload_date(info.get("upload_date") or info.get("release_date")),
        duration_seconds=duration_seconds,
        word_count=_word_count(transcript_text) or _word_count(document),
        thumbnail_url=info.get("thumbnail"),
    )
    atomic_write_json(item_dir / "metadata.json", meta)

    # 5. Maintain channel.json.
    _update_channel_json(channel_root, handle, info)

    update_index()
    rel = item_dir.relative_to(library_path()).as_posix()
    log.info("ingested youtube video -> %s", rel)
    return rel


def _update_channel_json(channel_root: Path, handle: str, info: dict[str, Any]) -> None:
    channel_root.mkdir(parents=True, exist_ok=True)
    existing = read_json(channel_root / "channel.json", default={}) or {}
    data = {
        "handle": handle,
        "title": existing.get("title") or info.get("channel") or info.get("uploader") or handle,
        "channel_url": existing.get("channel_url") or info.get("channel_url") or info.get("uploader_url"),
        "last_updated": now_iso(),
    }
    atomic_write_json(channel_root / "channel.json", data)


# --------------------------------------------------------------------------- #
# YouTube channel (fan-out)
# --------------------------------------------------------------------------- #


def ingest_youtube_channel(url_or_handle: str) -> list[str]:
    """
    Resolve a channel into its individual video URLs and enqueue each as a
    separate youtube_video job. Returns the list of enqueued URLs.
    """
    ensure_dirs()

    target = url_or_handle.strip()
    if target.startswith("@"):
        target = f"https://www.youtube.com/{target}/videos"
    elif not target.startswith("http"):
        target = f"https://www.youtube.com/@{target}/videos"

    raw = ytdlp_json(target, extra=["--flat-playlist", "--dump-json"])

    urls: list[str] = []
    handle = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle = handle or entry.get("uploader_id") or entry.get("channel_id")
        vid_url = entry.get("url") or entry.get("webpage_url")
        vid_id = entry.get("id")
        if vid_url and vid_url.startswith("http"):
            urls.append(vid_url)
        elif vid_id:
            urls.append(f"https://www.youtube.com/watch?v={vid_id}")

    # Pre-create the channel folder so it shows up immediately.
    if handle:
        h = handle if str(handle).startswith("@") else f"@{slugify(str(handle))}"
        _update_channel_json(youtube_dir() / h, h, {})

    # Lazy import to avoid an import cycle with the queue worker.
    from queue_worker import add_job

    for u in urls:
        add_job("youtube_video", u)

    log.info("channel %s fanned out into %d video jobs", url_or_handle, len(urls))
    return urls


# --------------------------------------------------------------------------- #
# Generic files / URLs
# --------------------------------------------------------------------------- #


def ingest_file(filepath: str, original_name: str | None = None, source_url: str | None = None) -> str:
    """
    Convert an uploaded file (or arbitrary URL) into content.md + metadata.json
    under library/docs/<slug>/. Returns the relative item path.
    """
    ensure_dirs()

    source = source_url or filepath
    name = original_name or Path(filepath).name or source
    title = Path(name).stem if not source_url else name

    docs_dir().mkdir(parents=True, exist_ok=True)
    slug = unique_slug(title, docs_dir())
    item_dir = docs_dir() / slug
    item_dir.mkdir(parents=True, exist_ok=True)

    content = run_markitdown(source)
    atomic_write_text(item_dir / "content.md", content)

    ext = Path(name).suffix.lower().lstrip(".")
    if source_url:
        source_type = "url"
    elif ext == "pdf":
        source_type = "pdf"
    elif ext in {"doc", "docx", "odt", "rtf", "txt", "md"}:
        source_type = "doc"
    else:
        source_type = "doc"

    meta = new_metadata(
        id=slug,
        title=title,
        source_url=source_url,
        source_type=source_type,
        word_count=_word_count(content),
    )
    atomic_write_json(item_dir / "metadata.json", meta)

    update_index()
    rel = item_dir.relative_to(library_path()).as_posix()
    log.info("ingested file -> %s", rel)
    return rel


# --------------------------------------------------------------------------- #
# Manual notes ("Save to MarkBase")
# --------------------------------------------------------------------------- #


def save_note(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_url: str | None = None,
) -> str:
    """
    Save a hand-written / programmatically-supplied Markdown note as a
    first-class library item under library/notes/<slug>/. This is the manual
    annotation feature and the write path the future MCP server will call.
    Returns the relative item path.
    """
    ensure_dirs()
    title = (title or "").strip() or "Untitled Note"
    content = content or ""

    notes_dir().mkdir(parents=True, exist_ok=True)
    slug = unique_slug(title, notes_dir())
    item_dir = notes_dir() / slug
    item_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(item_dir / "content.md", content)

    clean_tags: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            clean_tags.append(t)

    meta = new_metadata(
        id=slug,
        title=title,
        source_url=source_url,
        source_type="note",
        tags=clean_tags,
        word_count=_word_count(content),
    )
    atomic_write_json(item_dir / "metadata.json", meta)

    update_index()
    rel = item_dir.relative_to(library_path()).as_posix()
    log.info("saved note -> %s", rel)
    return rel


# --------------------------------------------------------------------------- #
# Index rebuild
# --------------------------------------------------------------------------- #


# In-memory index cache keyed by a cheap filesystem fingerprint. Reads serve
# from this without re-parsing every metadata.json; we only rebuild when the
# fingerprint changes (an item was added, removed, or modified — including by
# editing files directly on disk).
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict[str, Any] = {"fingerprint": None, "index": None}


def _library_fingerprint() -> str:
    """
    Cheap signature of the library: each item's path + mtime + size. Uses stat
    only (no JSON parsing), so it's far cheaper than a full rebuild and detects
    adds, removals, and content edits.
    """
    h = hashlib.md5()
    for p in iter_metadata_files():
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()


def _build_index() -> dict[str, Any]:
    root = library_path()
    items: list[dict[str, Any]] = []
    for meta_path in iter_metadata_files():
        meta = read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        meta = {**new_metadata(), **meta}  # backfill any missing fields
        meta["path"] = meta_path.parent.relative_to(root).as_posix()
        items.append(meta)
    items.sort(key=lambda m: m.get("date_ingested") or "", reverse=True)
    return {"last_updated": now_iso(), "total_items": len(items), "items": items}


def update_index() -> dict[str, Any]:
    """Rebuild index.json from scratch, persist it, and refresh the cache."""
    ensure_dirs()
    index = _build_index()
    atomic_write_json(index_file(), index)
    with _INDEX_LOCK:
        _INDEX_CACHE["fingerprint"] = _library_fingerprint()
        _INDEX_CACHE["index"] = index
    return index


def get_index() -> dict[str, Any]:
    """
    Return the library index, rebuilding only if the on-disk fingerprint has
    changed since the last build. This is what read endpoints should call.
    """
    ensure_dirs()
    fp = _library_fingerprint()
    with _INDEX_LOCK:
        if _INDEX_CACHE["index"] is not None and _INDEX_CACHE["fingerprint"] == fp:
            return _INDEX_CACHE["index"]
    return update_index()


# --------------------------------------------------------------------------- #
# Deletion (soft delete to _trash by default)
# --------------------------------------------------------------------------- #


TRASH_MANIFEST = ".trash_meta.json"


def _prune_empty_parents(item_dir: Path) -> None:
    """
    After an item is moved/removed, delete now-empty ancestor folders up to (but
    not including) the content roots — e.g. an emptied channel's videos/ and the
    channel dir itself (when only channel.json remains).
    """
    roots = {youtube_dir(), docs_dir(), notes_dir(), library_path()}
    p = item_dir.parent
    while p not in roots and library_path() in p.parents:
        try:
            entries = list(p.iterdir())
        except OSError:
            break
        if not entries:
            p.rmdir()
        elif p.parent == youtube_dir() and all(e.name == "channel.json" for e in entries):
            shutil.rmtree(p)  # channel with no remaining videos
        else:
            break
        p = p.parent


def _dedupe_path(target: Path) -> Path:
    """Return `target`, or a `-2`/`-3`… sibling if it already exists."""
    if not target.exists():
        return target
    n = 2
    while True:
        cand = target.with_name(f"{target.name}-{n}")
        if not cand.exists():
            return cand
        n += 1


def delete_item(item_dir: Path, permanent: bool = False) -> dict[str, Any]:
    """
    Remove an item. By default it is moved to library/_trash/ (recoverable, with
    a manifest recording its original path); with permanent=True it is deleted
    outright. `item_dir` must already be a validated path inside the library.
    """
    root = library_path()
    rel = item_dir.relative_to(root).as_posix()

    if permanent:
        shutil.rmtree(item_dir)
        _prune_empty_parents(item_dir)
        update_index()
        log.info("permanently deleted %s", rel)
        return {"deleted": rel, "permanent": True}

    trash_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = _dedupe_path(trash_dir() / f"{stamp}__{rel.replace('/', '__')}")
    shutil.move(str(item_dir), str(dest))

    meta = read_json(dest / "metadata.json", default={}) or {}
    atomic_write_json(
        dest / TRASH_MANIFEST,
        {
            "original_path": rel,
            "deleted_at": now_iso(),
            "title": meta.get("title") or rel,
            "source_type": meta.get("source_type"),
        },
    )
    _prune_empty_parents(item_dir)
    update_index()
    log.info("trashed %s -> %s", rel, dest.name)
    return {"deleted": rel, "permanent": False, "trash": dest.name}


def _trash_original_path(child: Path) -> str:
    """
    Original library-relative path of a trashed item: from its manifest, or
    reconstructed from the folder name ('<stamp>__a__b__c' -> 'a/b/c') for
    items trashed before manifests existed.
    """
    man = read_json(child / TRASH_MANIFEST, default={}) or {}
    if man.get("original_path"):
        return man["original_path"]
    name = child.name
    rest = name.split("__", 1)[1] if "__" in name else name
    return rest.replace("__", "/")


def list_trash() -> list[dict[str, Any]]:
    """List trashed items (most recently deleted first)."""
    t = trash_dir()
    if not t.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in t.iterdir():
        if not child.is_dir():
            continue
        man = read_json(child / TRASH_MANIFEST, default={}) or {}
        meta = read_json(child / "metadata.json", default={}) or {}
        out.append(
            {
                "trash_name": child.name,
                "original_path": _trash_original_path(child),
                "title": man.get("title") or meta.get("title") or child.name,
                "source_type": man.get("source_type") or meta.get("source_type"),
                "deleted_at": man.get("deleted_at"),
            }
        )
    out.sort(key=lambda x: x.get("deleted_at") or "", reverse=True)
    return out


def _safe_trash_child(trash_name: str) -> Path:
    """Resolve a direct child of _trash, refusing traversal."""
    if not trash_name or "/" in trash_name or "\\" in trash_name or trash_name.startswith("."):
        raise ValueError("Invalid trash name")
    target = (trash_dir() / trash_name).resolve()
    if target.parent != trash_dir().resolve() or not target.is_dir():
        raise ValueError("Trash item not found")
    return target


def restore_item(trash_name: str) -> dict[str, Any]:
    """Move a trashed item back to its original location (deduped if occupied)."""
    src = _safe_trash_child(trash_name)
    rel = _trash_original_path(src)
    dest = _dedupe_path(library_path() / rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    (dest / TRASH_MANIFEST).unlink(missing_ok=True)
    update_index()
    restored = dest.relative_to(library_path()).as_posix()
    log.info("restored %s -> %s", trash_name, restored)
    return {"restored": restored}


def delete_trash_item(trash_name: str) -> dict[str, Any]:
    """Permanently remove a single item from the trash."""
    target = _safe_trash_child(trash_name)
    shutil.rmtree(target)
    return {"purged": trash_name}


def empty_trash() -> int:
    """Permanently remove everything in _trash. Returns count of items purged."""
    t = trash_dir()
    if not t.is_dir():
        return 0
    n = 0
    for child in t.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        n += 1
    return n


def purge_expired_trash(days: int = 30) -> int:
    """Permanently remove trashed items older than `days`. Returns count purged."""
    t = trash_dir()
    if not t.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    n = 0
    for child in t.iterdir():
        if not child.is_dir():
            continue
        man = read_json(child / TRASH_MANIFEST, default={}) or {}
        deleted_at = man.get("deleted_at")
        try:
            ts = datetime.fromisoformat(deleted_at).timestamp() if deleted_at else child.stat().st_mtime
        except (ValueError, TypeError):
            ts = child.stat().st_mtime
        if ts < cutoff:
            shutil.rmtree(child)
            n += 1
    if n:
        log.info("auto-purged %d trash item(s) older than %d days", n, days)
    return n
