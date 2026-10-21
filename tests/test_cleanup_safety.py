"""Offline regression coverage for v0.4 cleanup authorization and retries."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from guard import ApiError, ArrApi, ArrInstance, Config, Guard, PendingStore


class CleanupSafetyTests(unittest.TestCase):
    """A journal record alone must never authorize successful-import deletion."""

    def setUp(self):
        self.now = 1_800_000_000
        self.instance = ArrInstance("http://arr:7878", "test-key", "radarr")
        self.config = Config("http://qb:8080", "u", "p", {"RADARR": self.instance},
                             {"radarr": "RADARR"}, dry_run=False, state_db=":memory:")
        self.qbit, self.arr, self.validator = Mock(), Mock(), Mock()
        self.torrent = {"hash": "abc123", "category": "radarr", "name": "Movie",
                        "added_on": self.now - 3600, "completion_on": self.now - 1800,
                        "progress": 1, "amount_left": 0, "state": "uploading"}
        self.qbit.torrents.return_value = [self.torrent]
        self.qbit.files.return_value = [{"name": "Movie.mkv", "size": 100, "progress": 1}]
        self.validator.inspect.return_value = ("accept", "valid video")
        self.event = {"date": datetime.fromtimestamp(self.now - 1000, timezone.utc).isoformat(),
                      "data": {"droppedPath": "/downloads/complete/Movie.mkv"}}
        self.arr.imported_history.return_value = {"abc123": [self.event]}
        verifier_patch = patch("guard.FileVerifier")
        self.verifier = verifier_patch.start().return_value
        self.addCleanup(verifier_patch.stop)
        self.verifier.snapshot.side_effect = lambda torrent, files: {"version": 1, "torrent": torrent, "files": files, "items": [{"path": "mock download"}]}
        self.verifier.remaining.return_value = []
        self.guard = Guard(self.config, self.qbit, {"RADARR": self.arr}, validator=self.validator)
        self.addCleanup(self.guard.store.db.close)

    def schedule(self, phase="delete"):
        """Simulate a durable imported decision, including older journal phases."""
        self.guard.store.add("abc123", "RADARR", "Movie", "import confirmed", kind="imported")
        self.guard.store.set_phase("abc123", phase, 42)

    def assert_preserved(self):
        self.qbit.stop.assert_not_called()
        self.qbit.delete_with_data.assert_not_called()
        self.arr.queue_by_hash.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.assertTrue(self.guard.store.entries())

    def test_imported_all_phases_bypass_arr_queue_and_blocklist(self):
        for phase in ("match", "arr", "delete", "verify"):
            with self.subTest(phase=phase):
                self.schedule(phase)
                self.qbit.reset_mock()
                self.guard.poll(now=self.now)
                self.qbit.delete_with_data.assert_called_once_with("abc123")
                self.qbit.stop.assert_not_called()
                self.arr.queue_by_hash.assert_not_called()
                self.arr.blocklist_and_remove.assert_not_called()
                self.guard.store.remove("abc123")

    def test_imported_queue_missing_still_cleans(self):
        self.arr.queue_by_hash.return_value = {}
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_called_once_with("abc123")
        self.arr.queue_by_hash.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()

    def test_sonarr_import_uses_only_owning_history_client(self):
        sonarr = Mock()
        sonarr.imported_history.return_value = {"abc123": [self.event]}
        self.guard.config = replace(self.config, instances={"SONARR": self.instance},
                                    categories={"sonarr": "SONARR"})
        self.guard.arr_clients = {"SONARR": sonarr, "RADARR": self.arr}
        self.torrent["category"] = "sonarr"
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_called_once_with("abc123")
        sonarr.blocklist_and_remove.assert_not_called()
        sonarr.queue_by_hash.assert_not_called()
        self.arr.imported_history.assert_not_called()

    def test_imported_ownership_change_preserves_data(self):
        self.schedule()
        self.torrent["category"] = "manual"
        self.guard.poll(now=self.now)
        self.assert_preserved()

    def test_retry_requires_current_import_evidence(self):
        self.schedule("verify")
        self.arr.imported_history.return_value = {}
        self.guard.poll(now=self.now)
        self.assert_preserved()

    def test_retry_history_outage_preserves_data(self):
        self.schedule()
        self.arr.imported_history.side_effect = ApiError("unavailable", 503)
        self.guard.poll(now=self.now)
        self.assert_preserved()

    def test_retry_incomplete_or_checking_metadata_preserves_data(self):
        for changes in ({"state": "checkingUP"}, {"state": "moving"},
                        {"amount_left": None}, {"state": ""}, {"progress": 0.8},
                        {"added_on": 0}, {"completion_on": self.now - 5}):
            with self.subTest(changes=changes):
                self.schedule()
                self.qbit.torrents.return_value = [{**self.torrent, **changes}]
                self.guard.poll(now=self.now)
                self.assert_preserved()

    def test_retry_missing_media_or_invalid_payload_preserves_data(self):
        for result in ("defer", "reject"):
            with self.subTest(result=result):
                self.schedule()
                self.validator.inspect.return_value = (result, "media changed")
                self.guard.poll(now=self.now)
                self.assert_preserved()

    def test_retry_new_selected_video_requires_its_import(self):
        self.schedule()
        self.qbit.files.return_value.append({"name": "Episode2.mkv", "size": 100, "progress": 1})
        self.guard.poll(now=self.now)
        self.assert_preserved()

    def test_reused_hash_with_old_history_is_preserved(self):
        self.schedule()
        self.torrent["added_on"] = self.now - 900
        self.guard.poll(now=self.now)
        self.assert_preserved()

    def test_pending_dry_run_does_not_mutate_apis_or_journal(self):
        self.schedule("arr")
        before = self.guard.store.entries()
        self.guard.config = replace(self.config, dry_run=True)
        self.guard.poll(now=self.now)
        self.assert_preserved()
        self.assertEqual(self.guard.store.entries(), before)

    def test_restart_revalidates_persisted_import_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            store = PendingStore(path)
            store.add("abc123", "RADARR", "Movie", "imported", kind="imported")
            store.db.close()
            store = PendingStore(path)
            self.addCleanup(store.db.close)
            other = Guard(self.config, self.qbit, {"RADARR": self.arr},
                          store=store, validator=self.validator)
            self.arr.imported_history.return_value = {}
            other.poll(now=self.now)
            self.qbit.delete_with_data.assert_not_called()
            self.assertTrue(store.entries())
            self.arr.imported_history.return_value = {"abc123": [self.event]}
            other.poll(now=self.now + 5)
            self.qbit.delete_with_data.assert_called_once_with("abc123")

    def test_arr_404_is_not_confirmed_blocklisting(self):
        self.guard.store.add("abc123", "RADARR", "Movie", "risky payload")
        self.arr.queue_by_hash.return_value = {"abc123": 42}
        self.arr.blocklist_and_remove.side_effect = ApiError("not found", 404)
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "arr")
        self.guard.poll(now=self.now + 5)
        self.qbit.delete_with_data.assert_not_called()

    def test_legacy_verify_absence_without_manifest_requires_manual_review(self):
        self.schedule("verify")
        self.qbit.torrents.return_value = []
        self.guard.poll(now=self.now)
        self.assertTrue(self.guard.store.entries())
        self.arr.imported_history.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()


class HistoryPaginationTests(unittest.TestCase):
    """Never accept a prematurely truncated Arr history response."""

    def test_only_actual_import_events_are_accepted(self):
        for event_type in (3, "3", "downloadFolderImported", 5, "5", 6, "episodeFileDeleted"):
            with self.subTest(event_type=event_type):
                http = Mock()
                http.request.return_value = {"records": [{"date": "2026-10-19T12:00:00Z",
                    "eventType": event_type, "downloadId": "ABC"}], "totalRecords": 1}
                arr = ArrApi(ArrInstance("http://arr:7878", "test-key", "radarr"), http)
                imports = arr.imported_history(0)
                self.assertEqual("abc" in imports, event_type in (3, "3", "downloadFolderImported"))

    def test_truncated_history_is_rejected(self):
        http = Mock()
        http.request.return_value = {"records": [], "totalRecords": 1}
        arr = ArrApi(ArrInstance("http://arr:7878", "test-key", "radarr"), http)
        with self.assertRaisesRegex(ApiError, "incomplete"):
            arr.imported_history(0)

    def test_history_paginates_to_the_reported_total(self):
        http = Mock()
        event = {"date": "2026-10-19T12:00:00Z", "eventType": "downloadFolderImported", "downloadId": "ABC"}
        http.request.side_effect = [{"records": [event] * 200, "totalRecords": 201},
                                    {"records": [event], "totalRecords": 201}]
        arr = ArrApi(ArrInstance("http://arr:7878", "test-key", "radarr"), http)
        self.assertEqual(len(arr.imported_history(0)["abc"]), 201)
