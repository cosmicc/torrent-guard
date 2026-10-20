"""v0.2 completion, tags, media validation and import cleanup tests (offline)."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from guard import (ArrApi, ArrInstance, Config, Guard, MediaValidator, PendingStore,
                   classify, import_confirmed, is_complete, torrent_app, video_files)

NOW = 1_800_000_000


def when(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class CompletionTests(unittest.TestCase):
    def setUp(self):
        i = ArrInstance("http://radarr:7878", "key", "radarr", "movies")
        self.cfg = Config("http://qbit:8080", "u", "p", {"RADARR": i}, {"radarr": "RADARR"},
                          dry_run=False, state_db=":memory:", tags={"movies": "RADARR"})
        self.qbit = Mock()
        self.arr = Mock()
        self.validator = Mock()
        self.validator.inspect.return_value = ("accept", "ffprobe verified 1 video")
        self.torrent = {"name": "Movie 2026", "hash": "ABCDE1", "category": "unrelated", "tags": "other,movies",
                        "added_on": NOW - 3600, "completion_on": NOW - 1900,
                        "progress": 1.0, "amount_left": 0, "state": "uploading",
                        "save_path": "/downloads/movies"}
        self.file = {"name": "Movie 2026.mkv", "size": 8_000_000_000, "progress": 1.0, "priority": 1}
        self.qbit.torrents.return_value = [self.torrent]
        self.qbit.files.return_value = [self.file]
        self.event = {"downloadId": "ABCDE1", "eventType": "downloadFolderImported", "date": when(NOW-1000),
                      "data": {"droppedPath": "/downloads/movies/Movie 2026.mkv", "importedPath": "/library/Movie 2026.mkv"}}
        self.arr.imported_history.return_value = {"abcde1": [self.event]}
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr}, validator=self.validator)

    def tearDown(self):
        self.guard.store.db.close()

    def test_tag_matches_other_category(self):
        self.assertEqual(torrent_app(self.torrent, self.cfg), "RADARR")

    def test_tag_conflict_refuses_deletion(self):
        cfg = Config(**{**self.cfg.__dict__, "categories": {"tv-sonarr": "SONARR"}, "tags": {"tv": "SONARR", "movies": "RADARR"}})
        self.assertIsNone(torrent_app({**self.torrent, "category": "tv-sonarr"}, cfg))
        self.assertIsNone(torrent_app({**self.torrent, "tags": "movies,tv"}, cfg))

    def test_successfully_imported_after_ten_minutes_is_cleaned(self):
        self.guard.poll(now=NOW)
        self.arr.blocklist_and_remove.assert_not_called()
        self.qbit.delete_with_data.assert_called_once_with("abcde1")
        self.assertEqual(self.guard.store.entries()[0][-1], "imported")
        self.assertEqual(self.guard.store.entries()[0][-2], "verify")
        self.qbit.torrents.return_value = []
        self.guard.poll(now=NOW + 5)
        self.assertEqual(self.guard.store.entries(), [])

    def test_import_age_below_ten_min_does_not_cleanup(self):
        self.event["date"] = when(NOW-599)
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries(), [])

    def test_old_import_before_new_grab_not_accepted(self):
        self.event["date"] = when(NOW-4000)
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()

    def test_no_arr_import_do_not_cleanup(self):
        self.arr.imported_history.return_value = {}
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()

    def test_arr_unavailable_do_not_cleanup(self):
        self.arr.imported_history.side_effect = OSError("network disconnected")
        # Driver must not crash; state stays pending-free.
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()

    def test_dryrun_import_does_not_delete(self):
        cfg = Config(**{**self.cfg.__dict__, "dry_run": True})
        other = Guard(cfg, self.qbit, {"RADARR": self.arr}, validator=self.validator)
        other.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()
        self.assertEqual(other.store.entries(), [])
        other.store.db.close()

    def test_bad_completed_media_rejects_and_blocklists(self):
        self.validator.inspect.return_value = ("reject", "ffprobe no video stream")
        self.arr.queue_by_hash.return_value = {"abcde1": 42}
        self.guard.poll(now=NOW)
        self.arr.blocklist_and_remove.assert_called_once_with(42)
        self.qbit.delete_with_data.assert_called_once_with("abcde1")
        self.assertEqual(self.guard.store.entries()[0][-1], "reject")

    def test_mount_unavailable_never_delete(self):
        self.validator.inspect.return_value = ("defer", "mount unavailable")
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries(), [])

    def test_finished_old_torrent_eligible(self):
        self.torrent["added_on"] = NOW - 3*86400
        self.event["date"] = when(NOW-1000)
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_called_once()

    def test_partial_multi_episode_import_is_not_deleted(self):
        self.qbit.files.return_value = [self.file, {"name": "Episode2.mkv", "size": 1, "progress": 1.0}]
        self.guard.poll(now=NOW)
        self.qbit.delete_with_data.assert_not_called()

    def test_reject_unrecognized_video_when_completed_even_if_no_early_check(self):
        self.torrent["added_on"] = NOW - 3 * 86400
        self.qbit.files.return_value = [{"name": "Fake.mkv.exe", "size": 1000, "progress": 1.0}]
        self.arr.queue_by_hash.return_value = {"abcde1": 42}
        self.guard.poll(now=NOW)
        self.arr.blocklist_and_remove.assert_called_once_with(42)

    def test_completion_requires_complete_state(self):
        self.assertTrue(is_complete(self.torrent))
        self.assertFalse(is_complete({**self.torrent, "progress": 0.99}))
        self.assertFalse(is_complete({**self.torrent, "state": "missingFiles"}))


class ValidationTests(unittest.TestCase):
    def test_no_selected_video_rejected(self):
        self.assertIn("no", classify([{"name": "movie.mkv", "priority": 0}, {"name": "notes.nfo"}]))
        self.assertEqual(video_files([{"name": "film.mp4", "priority": 0}]), [])

    def test_valid_ffprobe_stream_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "movies" / "Film.mkv"
            path.parent.mkdir()
            path.write_bytes(b"fake test data")
            c = Config("http://qbit", "u", "p", {}, {}, guard_download_root=d, qbit_download_root="/torrents")
            validator = MediaValidator(c)
            torrent = {"save_path": "/torrents/movies"}
            files = [{"name": "Film.mkv", "size": 10, "progress": 1.0}]
            process = Mock(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "video", "width": 1280, "height": 720}]}), stderr="")
            with patch("guard.subprocess.run", return_value=process) as mocked:
                decision, msg = validator.inspect(torrent, files)
            self.assertEqual(decision, "accept")
            self.assertIn("1 complete", msg)
            self.assertEqual(mocked.call_args.args[0][-1], str(path))

    def test_exe_disguised_as_mkv_rejected_by_ffprobe(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.mkv"
            path.write_bytes(b"MZ binary executable")
            c = Config("http://qbit", "u", "p", {}, {}, guard_download_root=d, qbit_download_root="/downloads")
            v = MediaValidator(c)
            proc = Mock(returncode=1, stdout="", stderr="Invalid data found")
            with patch("guard.subprocess.run", return_value=proc):
                result, reason = v.inspect({"save_path": "/downloads"}, [{"name": "bad.mkv", "size": 20, "progress": 1.0}])
            self.assertEqual(result, "reject")
            self.assertIn("no readable", reason)

    def test_symlink_escape_defers(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "escape.mkv"
            path.symlink_to("/etc/passwd")
            c = Config("http://qbit", "u", "p", {}, {}, guard_download_root=d, qbit_download_root="/downloads")
            result, reason = MediaValidator(c).inspect({"save_path": "/downloads"}, [{"name": "escape.mkv", "size": 20, "progress": 1.0}])
            self.assertEqual(result, "defer")
            self.assertIn("mapping", reason)

    def test_path_traversal_defers(self):
        c = Config("http://qbit", "u", "p", {}, {})
        result, _ = MediaValidator(c).inspect({"save_path": "/downloads"}, [{"name": "../Movie.mkv", "size": 20, "progress": 1.0}])
        self.assertEqual(result, "defer")

    def test_multi_episode_import_all_needed(self):
        torrent = {"added_on": NOW-2000}
        files = [{"name": "ep1.mkv"}, {"name": "ep2.mkv"}]
        events = [{"date": when(NOW-1000), "data": {"droppedPath": "/path/ep1.mkv"}}]
        self.assertFalse(import_confirmed(torrent, files, events, NOW, 10)[0])
        events.append({"date": when(NOW-900), "data": {"droppedPath": "/path/ep2.mkv"}})
        self.assertTrue(import_confirmed(torrent, files, events, NOW, 10)[0])

    def test_import_journal_migrates_01(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "state.sqlite3")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE pending(hash TEXT PRIMARY KEY, app TEXT NOT NULL, name TEXT NOT NULL, reason TEXT NOT NULL, queue_id INTEGER, phase TEXT NOT NULL)")
            conn.execute("INSERT INTO pending VALUES ('abcd','RADARR','bad.exe','bad',5,'verify')")
            conn.commit()
            conn.close()
            store = PendingStore(db)
            self.assertEqual(store.entries()[0][-1], "reject")
            store.add("efgh", "RADARR", "good.mkv", "imported", kind="imported")
            self.assertEqual(store.entries()[1][-2:], ("delete", "imported"))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
