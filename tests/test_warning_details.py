"""Warning retention, repeat grouping and concise non-destructive diagnostics."""
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from status_report import ISSUE_EVENT_LIMIT, StatusHistory, history_data, issue_details


class WarningDetailsTests(unittest.TestCase):
    def test_reported_orphan_examples_keep_filename_and_explain_safety_hold(self):
        folder = "Example.Series.S19E11.720p[Group]"
        for filename in ("[Group]Downloaded from tracker.example .txt", "NEW upcoming releases by Example.txt", folder + ".mkv.nfo", folder + ".mkv.!qB"):
            message = (f"ORPHAN deferred /downloads/incomplete/tv/{folder}/{filename}; timer reset: "
                       f"quarantined object changed; recover manually from {folder}/.torrent-guard-abc123/payload")
            data = issue_details(message)
            self.assertEqual(data["filename"], filename)
            self.assertEqual(data["category"], "Quarantine")
            self.assertIn("saved identity", data["action"])
            self.assertEqual(data["recovery"], ".torrent-guard-abc123")
            self.assertNotIn("/", data["reason"])
            self.assertNotIn(folder + "/", str(data))

    def test_download_reason_retry_and_quoted_paths_are_separate(self):
        data = issue_details("DEFER validation name='Movie' paths='/downloads/complete/Movie.mkv' reason=Permission denied: '/mnt/media/Movie.mkv'; retry_in=300s attempt=5 retry_at=2026-11-13 02:40:00 EST-0500")
        self.assertEqual(data["download"], "Movie")
        self.assertEqual(data["reason"], "Permission denied: 'Movie.mkv'")
        self.assertEqual(data["retry_seconds"], 300)
        self.assertEqual(data["attempt"], 5)

    def test_repeat_counts_include_only_the_last_day_and_real_errors_remain_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            history = StatusHistory(path)
            now = time.time() + 1
            for index in range(93):
                history.handle(logging.makeLogRecord({"msg": "ORPHAN deferred /downloads/incomplete/tv/episode.mkv; timer reset: quarantined object changed; recover manually from series/.torrent-guard-abc/payload", "levelno": logging.WARNING, "created": now - index * 60}))
            for index in range(30):
                history.handle(logging.makeLogRecord({"msg": "warning " + str(index), "levelno": logging.WARNING, "created": now - index}))
            history.handle(logging.makeLogRecord({"msg": "API error", "levelno": logging.ERROR, "created": now}))
            history.close()
            data = history_data(path, now)
            self.assertEqual(data["windows"]["6"]["warnings"], 123)
            grouped = next(row for row in data["issues"] if row["category"] == "Quarantine")
            self.assertEqual(grouped["occurrences"], 93)
            self.assertNotIn("/downloads", grouped["message"])
            self.assertEqual(len(data["issues"]), 32)
            self.assertTrue(any(row["level"] == "ERROR" for row in data["issues"]))
            self.assertEqual(history_data(path, now + 86401)["issues"], [])

    def test_retention_is_bounded_and_does_not_erase_event_counters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            history = StatusHistory(path)
            now = time.time() + 1
            for index in range(ISSUE_EVENT_LIMIT + 5):
                history.handle(logging.makeLogRecord({"msg": f"warning {index}", "levelno": logging.WARNING, "created": now}))
            history.close()
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute("SELECT count(*) FROM issues").fetchone()[0], ISSUE_EVENT_LIMIT)
            data = history_data(path, now)
            self.assertEqual(len(data["issues"]), 100)
            self.assertEqual(data["windows"]["6"]["warnings"], ISSUE_EVENT_LIMIT + 5)
