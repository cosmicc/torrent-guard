"""Unit tests: no live services or API credentials needed."""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from guard import ArrApi, ArrInstance, Config, Guard, Qbit, ApiError, PendingStore, classify


class ClassificationTests(unittest.TestCase):
    def test_accept_video_and_subtitle(self):
        self.assertIsNone(classify([{"name": "Film.mkv"}, {"name": "subtitles/en.srt"}]))

    def test_reject_video_named_executable(self):
        self.assertIn("Film.mkv.exe", classify([{"name": "Film.mkv.exe"}]))

    def test_reject_exe_even_with_video(self):
        self.assertIn("Installer.EXE", classify([{"name": "Film.mp4"}, {"name": "extras/Installer.EXE"}]))

    def test_windows_path_and_script(self):
        self.assertIn("script.ps1", classify([{"name": "folder\\script.ps1"}]))

    def test_no_metadata_is_not_bad(self):
        self.assertIsNone(classify([]))

    def test_no_video_rejected(self):
        self.assertIn("no recognized video", classify([{"name": "notes.txt"}]))

    def test_no_video_check_can_be_disabled(self):
        self.assertIsNone(classify([{"name": "notes.txt"}], reject_no_video=False))


class ApiTests(unittest.TestCase):
    def test_stop_falls_back_to_pause_on_old_qbit(self):
        qb = object.__new__(Qbit)
        qb.request = Mock(side_effect=[ApiError("not found", 404), "ok"])
        qb.stop("abc")
        self.assertEqual(qb.request.call_args_list[0].args[1], "/api/v2/torrents/stop")
        self.assertEqual(qb.request.call_args_list[1].args[1], "/api/v2/torrents/pause")

    def test_qbit_delete_explicitly_removes_data(self):
        qb = object.__new__(Qbit)
        qb.request = Mock()
        qb.delete_with_data("abcdef")
        args, kwargs = qb.request.call_args
        self.assertEqual(args, ("POST", "/api/v2/torrents/delete"))
        self.assertEqual(kwargs["fields"], {"hashes": "abcdef", "deleteFiles": "true"})

    def test_arr_queue_pagination(self):
        http = Mock()
        arr = ArrApi(ArrInstance("http://arr:7878", "key", "radarr"), http)
        http.request.side_effect = [
            {"records": [{"id": i, "downloadId": f"HASH{i}"} for i in range(200)], "totalRecords": 201},
            {"records": [{"id": 201, "downloadId": "ABCDEF"}], "totalRecords": 201},
        ]
        result = arr.queue_by_hash()
        self.assertEqual(result["abcdef"], 201)
        self.assertEqual(len(result), 201)

    def test_arr_blocklists_without_deleting_download(self):
        http = Mock()
        arr = ArrApi(ArrInstance("http://arr:7878", "key", "radarr"), http)
        arr.blocklist_and_remove(42)
        args, kwargs = http.request.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertEqual(urlsplit(args[1]).path, "/api/v3/queue/42")
        params = parse_qs(urlsplit(args[1]).query)
        self.assertEqual(params["removeFromClient"], ["false"])
        self.assertEqual(params["blocklist"], ["true"])
        self.assertEqual(params["skipRedownload"], ["false"])
        self.assertEqual(kwargs["headers"]["X-Api-Key"], "key")

    def test_duplicate_category_rejected(self):
        env = {
            "QBIT_URL": "http://qb:8080", "QBIT_USERNAME": "u", "QBIT_PASSWORD": "p",
            "SONARR_URL": "http://s:8989", "SONARR_API_KEY": "x", "SONARR_CATEGORY": "shared",
            "RADARR_URL": "http://r:7878", "RADARR_API_KEY": "y", "RADARR_CATEGORY": "shared",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                Config.from_env()


class PollTests(unittest.TestCase):
    def setUp(self):
        inst = ArrInstance("http://arr:7878", "key", "radarr")
        self.cfg = Config("http://qb:8080", "u", "p", {"RADARR": inst}, {"radarr": "RADARR"}, dry_run=False, state_db=":memory:")
        self.qb = Mock()
        self.arr = Mock()
        self.now = 1_800_000_000
        self.torrent = {"hash": "ABCDEF", "name": "Film.EXE", "category": "radarr", "added_on": self.now - 10}
        self.qb.torrents.return_value = [self.torrent]
        self.qb.files.return_value = [{"name": "Film.EXE"}]
        self.arr.queue_by_hash.return_value = {"abcdef": 42}
        verifier_patch = patch("guard.FileVerifier")
        self.verifier = verifier_patch.start().return_value
        self.addCleanup(verifier_patch.stop)
        self.verifier.snapshot.side_effect = lambda torrent, files: {"version": 1, "torrent": torrent, "files": files, "items": [{"path": "mock download"}]}
        self.verifier.remaining.return_value = []
        self.guard = Guard(self.cfg, self.qb, {"RADARR": self.arr})

    def tearDown(self):
        self.guard.store.db.close()

    def test_stop_then_arr_blocklist_then_qbit_delete(self):
        order = []
        self.qb.stop.side_effect = lambda h: order.append("stop")
        self.arr.blocklist_and_remove.side_effect = lambda q: order.append("blocklist")
        self.qb.delete_with_data.side_effect = lambda h: order.append("delete")
        self.guard.poll(now=self.now)
        self.assertEqual(order, ["stop", "blocklist", "stop", "delete"])
        self.arr.blocklist_and_remove.assert_called_once_with(42)
        self.qb.delete_with_data.assert_called_once_with("abcdef")
        self.assertEqual(self.guard.store.entries()[0][-2], "verify")
        self.qb.torrents.return_value = []
        self.guard.poll(now=self.now + 5)
        self.assertEqual(self.guard.store.entries(), [])

    def test_dry_run_no_api_mutations_or_journal(self):
        cfg = Config(**{**self.cfg.__dict__, "dry_run": True})
        guard = Guard(cfg, self.qb, {"RADARR": self.arr})
        guard.poll(now=self.now)
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()
        self.assertFalse(guard.store.entries())
        guard.store.db.close()

    def test_missing_arr_queue_keeps_torrent_stopped(self):
        self.arr.queue_by_hash.return_value = {}
        self.guard.poll(now=self.now)
        self.qb.stop.assert_called_once_with("abcdef")
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "match")
        self.arr.queue_by_hash.return_value = {"abcdef": 42}
        self.guard.poll(now=self.now + 5)
        self.qb.delete_with_data.assert_called_once_with("abcdef")

    def test_stop_failure_means_no_delete(self):
        self.qb.stop.side_effect = ApiError("failed", 403)
        self.guard.poll(now=self.now)
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "match")

    def test_blocklist_error_means_no_delete(self):
        self.arr.blocklist_and_remove.side_effect = ApiError("unavailable", 500)
        self.guard.poll(now=self.now)
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "arr")
        self.arr.blocklist_and_remove.side_effect = None
        self.guard.poll(now=self.now + 5)
        self.qb.delete_with_data.assert_called_once()

    def test_delete_error_retries(self):
        self.qb.delete_with_data.side_effect = [ApiError("unavailable", 500), None]
        self.guard.poll(now=self.now)
        self.assertEqual(self.guard.store.entries()[0][-2], "delete")
        self.guard.poll(now=self.now + 5)
        self.assertEqual(self.qb.delete_with_data.call_count, 2)
        self.assertEqual(self.guard.store.entries()[0][-2], "verify")

    def test_verify_retries_if_still_present(self):
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 5)
        self.assertEqual(self.qb.delete_with_data.call_count, 2)
        self.assertEqual(self.guard.store.entries()[0][-2], "verify")

    def test_category_change_stops_cleanup(self):
        self.guard.poll(now=self.now)
        self.qb.delete_with_data.reset_mock()
        self.qb.torrents.return_value = [{**self.torrent, "category": "manual"}]
        self.guard.poll(now=self.now + 5)
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "verify")

    def test_unscoped_and_old_skipped(self):
        self.qb.torrents.return_value = [
            {"hash": "abcd", "name": "manual.exe", "category": "manual", "added_on": self.now - 10},
            {"hash": "efgh", "name": "old.exe", "category": "radarr", "added_on": self.now - 90000},
        ]
        self.guard.poll(now=self.now)
        self.qb.files.assert_not_called()
        self.qb.stop.assert_not_called()

    def test_metadata_waits(self):
        self.qb.files.return_value = []
        self.guard.poll(now=self.now)
        self.qb.stop.assert_not_called()

    def test_journal_survives_restart_and_age_cutoff(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.sqlite3")
            cfg = Config(**{**self.cfg.__dict__, "state_db": path})
            arr = Mock()
            arr.queue_by_hash.return_value = {}
            g1 = Guard(cfg, self.qb, {"RADARR": arr})
            g1.poll(now=self.now)
            self.assertEqual(g1.store.entries()[0][-2], "match")
            g1.store.db.close()
            arr.queue_by_hash.return_value = {"abcdef": 42}
            g2 = Guard(cfg, self.qb, {"RADARR": arr})
            g2.poll(now=self.now + 90000)
            self.qb.delete_with_data.assert_called_once_with("abcdef")
            g2.store.db.close()

    def test_dont_remove_missing_torrent_before_queue_match(self):
        self.arr.queue_by_hash.return_value = {}
        self.guard.poll(now=self.now)
        self.qb.torrents.return_value = []
        self.guard.poll(now=self.now + 5)
        self.qb.delete_with_data.assert_not_called()
        self.assertTrue(self.guard.store.entries())


if __name__ == "__main__":
    unittest.main()
