import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ingest
import queue_worker


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Path(self.tmp.name) / "library"
        self.state = Path(self.tmp.name) / "state"
        self.old_library = os.environ.get("MARKBASE_LIBRARY_PATH")
        self.old_state = os.environ.get("MARKBASE_STATE_PATH")
        os.environ["MARKBASE_LIBRARY_PATH"] = str(self.library)
        os.environ["MARKBASE_STATE_PATH"] = str(self.state)
        importlib.reload(ingest)
        importlib.reload(queue_worker)
        ingest.ensure_dirs()
        queue_worker.init_db()

    def tearDown(self):
        if self.old_library is None:
            os.environ.pop("MARKBASE_LIBRARY_PATH", None)
        else:
            os.environ["MARKBASE_LIBRARY_PATH"] = self.old_library
        if self.old_state is None:
            os.environ.pop("MARKBASE_STATE_PATH", None)
        else:
            os.environ["MARKBASE_STATE_PATH"] = self.old_state
        importlib.reload(ingest)
        importlib.reload(queue_worker)
        self.tmp.cleanup()

    def _write_item(self, rel_path, source_url):
        item = self.library / rel_path
        item.mkdir(parents=True, exist_ok=True)
        ingest.atomic_write_text(item / "content.md", "# Existing\n")
        ingest.atomic_write_json(
            item / "metadata.json",
            ingest.new_metadata(id=item.name, title="Existing", source_url=source_url, source_type="url"),
        )
        ingest.update_index()
        return rel_path

    def test_normalize_source_url_drops_tracking_and_canonicalizes_youtube(self):
        self.assertEqual(
            ingest.normalize_source_url("https://learn.microsoft.com/en-us/powershell/?utm_source=x&view=powershell-7.5"),
            "https://learn.microsoft.com/en-us/powershell?view=powershell-7.5",
        )
        self.assertEqual(
            ingest.normalize_source_url("https://youtu.be/abc123?si=ignored"),
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertEqual(
            ingest.normalize_source_url("https://www.youtube.com/watch?v=abc123&list=playlist&utm_source=x"),
            "https://www.youtube.com/watch?v=abc123",
        )

    def test_tool_command_prefers_virtualenv_sibling(self):
        venv_bin = self.library / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        real_python = self.library / "python"
        real_python.touch()
        python = venv_bin / "python"
        python.symlink_to(real_python)
        tool = venv_bin / "markitdown"
        tool.touch()

        with mock.patch.object(ingest.sys, "executable", str(python)):
            self.assertEqual(ingest._tool_command("markitdown"), str(tool))

    def test_ingest_file_returns_existing_item_for_duplicate_source_url(self):
        rel = self._write_item("docs/existing", "https://example.com/docs/page?utm_source=newsletter")

        with mock.patch.object(ingest, "run_markitdown", side_effect=AssertionError("should not convert duplicate")):
            result = ingest.ingest_file(
                "https://example.com/docs/page",
                source_url="https://example.com/docs/page?utm_medium=email",
            )

        self.assertEqual(result, rel)
        self.assertFalse((self.library / "docs" / "page").exists())

    def test_queue_reuses_active_duplicate_job(self):
        first = queue_worker.add_job("url", "https://example.com/docs/page?utm_source=a")
        second = queue_worker.add_job("url", "https://example.com/docs/page?utm_medium=b")

        self.assertEqual(second, first)
        jobs = queue_worker.get_jobs(limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "queued")

    def test_channel_jobs_share_batch_key_and_cancel_cascades(self):
        channel_job = queue_worker.add_job("youtube_channel", "@ExampleChannel")
        video_job = queue_worker.add_job(
            "youtube_video",
            "https://www.youtube.com/watch?v=abc123",
            batch_key="channel:@examplechannel",
        )

        queue_worker.cancel_job(channel_job)

        jobs = {job["id"]: job for job in queue_worker.get_jobs(limit=10)}
        self.assertEqual(jobs[channel_job]["status"], "cancelled")
        self.assertEqual(jobs[video_job]["status"], "cancelled")

    def test_purge_job_output_deletes_ingested_item(self):
        rel = self._write_item("docs/existing", "https://example.com/docs/page")
        job_id = queue_worker.add_job("url", "https://example.com/docs/page")

        result = queue_worker.purge_job_output(job_id)

        self.assertTrue(result["purged"])
        self.assertFalse((self.library / rel).exists())

    def test_queue_records_duplicate_existing_source_as_done(self):
        rel = self._write_item("docs/existing", "https://example.com/docs/page")

        job_id = queue_worker.add_job("url", "https://example.com/docs/page?utm_campaign=test")

        jobs = queue_worker.get_jobs(limit=10)
        self.assertEqual(jobs[0]["id"], job_id)
        self.assertEqual(jobs[0]["status"], "done")
        self.assertEqual(jobs[0]["result_path"], rel)


class MarkitdownFailureTests(unittest.TestCase):
    def test_detects_youtube_transcript_api_rate_limit_output(self):
        output = """Attempt 1 failed: Could not retrieve a transcript for the video
Request to YouTube failed: 429 Client Error: Too Many Requests
https://github.com/jdepoix/youtube-transcript-api/issues
Attempt 2 failed: Could not retrieve a transcript for the video
About Press Copyright Contact us NFL Sunday Ticket
"""

        self.assertTrue(ingest._looks_like_failed_markitdown(output))

    def test_does_not_reject_normal_transcript_text(self):
        self.assertFalse(
            ingest._looks_like_failed_markitdown(
                "# A video\n\n## Transcript\n\nThis is ordinary transcript text."
            )
        )


class TranscriptVttTests(unittest.TestCase):
    def test_vtt_parser_collapses_rolling_youtube_caption_overlap(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
I think this is

00:00:01.000 --> 00:00:02.000
this is important

00:00:02.000 --> 00:00:03.000
important because it matters.
"""

        segments = ingest._vtt_to_segments(vtt)

        self.assertEqual(
            [segment["text"] for segment in segments],
            ["I think this is", "important", "because it matters."],
        )
        self.assertEqual(
            ingest._segments_to_text(segments),
            "I think this is important because it matters.",
        )


if __name__ == "__main__":
    unittest.main()
