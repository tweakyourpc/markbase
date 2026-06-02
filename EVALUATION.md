# MarkBase Evaluation

Reviewed against `README.md` as the source of truth for architecture, install flow, API, and design guarantees.

## Summary

MarkBase is a compact, understandable FastAPI app with a clean separation between content (`MARKBASE_LIBRARY_PATH`) and operational state (`MARKBASE_STATE_PATH`). The ingestion and indexing code is generally conservative: writes are atomic, the index is rebuilt from disk, and the worker catches job exceptions instead of dying. The largest risks were LAN exposure without an identity endpoint, unsanitized Markdown rendering, internal-library path access through item APIs, queue counts that only summarized the latest 50 jobs, and local startup docs/scripts that bypassed the port broker.

## Findings

1. **High - rendered ingested Markdown could execute active HTML before the fix.** `static/index.html:340-342` loaded rendering libraries from CDN, and the reader inserted `marked.parse(...)` output into `innerHTML` at `static/index.html:524-526`. Since content can come from external URLs and uploaded files via `app.py:197-211` and `ingest.py:569-610`, this was an XSS risk for a LAN-exposed service. Fixed by adding DOMPurify and sanitizing the rendered Markdown before insertion.

2. **Medium - item path validation allowed internal folders before the fix.** `_safe_item_dir()` resolved traversal escapes, but did not reject internal top-level folders such as `_trash` or `_uploads`; item routes call it at `app.py:125-140` and tag routes at `app.py:299-327`. Because trashed items retain `metadata.json`, a guessed `_trash/...` path could be read through `/api/item`. Fixed by rejecting empty paths and any path segment starting with `_` at `app.py:49-62`.

3. **Medium - queue status counts were misleading before the fix.** `queue_worker.queue_status()` now fetches the latest 50 jobs for display but counts statuses over the whole SQLite table at `queue_worker.py:103-110`. Before this change, counts were computed only from the latest 50 rows, which could hide older failures or queued work.

4. **Medium - LAN binding needs explicit trust boundary.** `run-service.sh:27-32` binds uvicorn to `0.0.0.0`, and `start.sh:21-42` now does the same to satisfy the homelab rule. The app has no authentication, CSRF protection, or origin checks. That is acceptable only behind a trusted LAN, VPN, reverse proxy, or firewall. The new `/whoami` endpoint at `app.py:100-111` satisfies the service identity requirement but does not provide access control.

5. **Medium - subprocess ingestion is argument-safe but still a remote-content trust boundary.** `ingest._run()` uses list arguments and `shell=False` behavior through `subprocess.run()` at `ingest.py:165-173`, and `yt-dlp` / `markitdown` calls pass user URLs as arguments at `ingest.py:176-196`, `ingest.py:249-286`, and `ingest.py:569-610`. This avoids shell injection, but a LAN user can still cause server-side URL fetches and potentially long conversions. Consider URL allow/deny rules, size limits, and per-job timeouts by content type.

6. **Low - uploaded temp files are not removed after successful conversion.** Uploads are written under `library/_uploads` at `app.py:202-210`, then passed to the worker at `queue_worker.py:157-160` and `ingest.py:569-610`. `_uploads` is ignored and not indexed, but files can accumulate and may contain personal data. A safe follow-up is to delete only worker-owned temp uploads after `ingest_file()` returns or fails.

7. **Low - filename-derived suffixes and titles are handled reasonably, but upload size is unlimited.** `Path(file.filename).suffix` at `app.py:206` avoids directory traversal in upload storage because `mkstemp()` chooses the path, and `original_name` is only used for title metadata in `ingest.py:576-578`. Still, FastAPI will accept large uploads unless deployment-level limits are added.

8. **Low - channel fan-out can create very large queues.** `ingest.ingest_youtube_channel()` calls `yt-dlp --flat-playlist` and enqueues every discovered video at `ingest.py:529-560`. This is correct for the README guarantee, but a maximum or confirmation threshold would prevent accidental massive backfills.

## Correctness Notes

- YouTube video ingestion prefers structured `yt-dlp` metadata, uses subtitle VTTs first, and falls back to `markitdown` only when needed (`ingest.py:403-496`). The fallback failure detector is pragmatic but heuristic (`ingest.py:289-297`).
- File and web URL ingestion are simple and deterministic (`ingest.py:569-610`). They rely on `markitdown` for conversion fidelity and error reporting.
- The worker is single-threaded, recovers stuck `processing` jobs on startup, and catches all job exceptions (`queue_worker.py:45-67`, `queue_worker.py:164-178`).
- The frontend escapes metadata before interpolation in most UI paths (`static/index.html:353-354`) and now sanitizes rendered Markdown (`static/index.html:524-526`).

## Robustness / Quality Notes

- Atomic writes are implemented for text and JSON with temp files, `fsync`, and `os.replace()` (`ingest.py:101-117`).
- The index cache uses a stat-only fingerprint over non-internal `metadata.json` files (`ingest.py:74-81`, `ingest.py:677-728`), which matches the README design.
- Soft-delete/trash uses direct-child validation for trash operations (`ingest.py:848-855`) and preserves restore metadata (`ingest.py:773-891`).
- Worker error handling records failures in SQLite and keeps the thread alive (`queue_worker.py:172-178`).

## Applied Fixes

1. Added `/whoami` with service/version/pid/start/host/port metadata.
2. Rejected internal `_...` paths in item/tag/delete path resolution.
3. Sanitized rendered Markdown with DOMPurify.
4. Made queue status counts cover all jobs, not just the displayed page.
5. Updated `start.sh` and README run instructions to use brokered LAN ports.
6. Added MIT `LICENSE`, README hero image reference, `/api/note` and `/whoami` API docs, and fixed the broken `portbroker` README link.

## Prioritized Follow-ups

1. Add authentication or document the expected reverse-proxy/VPN boundary before exposing beyond a private LAN.
2. Delete worker-owned upload temp files after conversion and add a small startup cleanup for stale `_uploads` files.
3. Add upload size limits and optional URL allow/deny controls for `markitdown`/`yt-dlp` jobs.
4. Add a channel fan-out cap or confirmation threshold.
5. Add focused tests for `_safe_item_dir`, trash restore/delete, queue status counts, and Markdown sanitization.
