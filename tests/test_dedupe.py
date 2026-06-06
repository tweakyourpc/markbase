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
            ingest.normalize_source_url("https://www.youtube.com/watch?v=abc123&utm_source=x"),
            "https://www.youtube.com/watch?v=abc123",
        )

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

    def test_queue_records_duplicate_existing_source_as_done(self):
        rel = self._write_item("docs/existing", "https://example.com/docs/page")

        job_id = queue_worker.add_job("url", "https://example.com/docs/page?utm_campaign=test")

        jobs = queue_worker.get_jobs(limit=10)
        self.assertEqual(jobs[0]["id"], job_id)
        self.assertEqual(jobs[0]["status"], "done")
        self.assertEqual(jobs[0]["result_path"], rel)


if __name__ == "__main__":
    unittest.main()
