"""Observed metadata deadlines and safe Arr-first magnet rejection/recovery."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from deletion import metadata_manifest, validate_metadata_manifest
from guard import ApiError, ArrInstance, Config, Guard


class MetadataTimeoutTests(unittest.TestCase):
    """Use real SQLite/evidence handling without calling remote APIs or deleting media."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        instance = ArrInstance("http://arr", "test", "tv")
        self.config = Config("http://qb", "u", "p", {"SONARR": instance}, {"tv": "SONARR"},
                             dry_run=False, state_db=str(self.base / "state.sqlite3"),
                             guard_download_root=str(self.base / "absent-download-mount"),
                             qbit_complete_dir="/downloads/complete", qbit_incomplete_dir="/downloads/incomplete")
        self.now = 1_800_000_000
        # Older than MAX_AGE_HOURS: metadata monitoring must still apply.
        self.torrent = {"hash": "ABC", "name": "Stalled magnet", "category": "tv", "tags": "tv",
                        "state": "metaDL", "added_on": self.now - 172800,
                        "progress": 0, "downloaded": 0, "completed": 0, "completion_on": -1,
                        "save_path": "/downloads/complete/tv", "download_path": "/downloads/incomplete/tv",
                        "content_path": ""}
        self.qb, self.arr = Mock(), Mock()
        self.qb.torrents.return_value = [self.torrent]
        self.qb.files.return_value = []
        self.arr.queue_by_hash.return_value = {"abc": 42}
        self.guard = self.make_guard()

    def make_guard(self, config=None):
        """Keep all opened journal connections registered for test cleanup."""
        guard = Guard(config or self.config, self.qb, {"SONARR": self.arr})
        self.addCleanup(guard.store.db.close)
        return guard

    def expire(self):
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 601)

    def test_boundary_and_arr_before_delete_and_zero_target_audit(self):
        order = []
        self.qb.stop.side_effect = lambda h: order.append("stop")
        self.arr.blocklist_and_remove.side_effect = lambda queue: order.append("blocklist")
        def deleted(h):
            manifest, requested = self.guard.store.evidence(h)
            validate_metadata_manifest(manifest)
            self.assertEqual(requested, self.now + 601)
            self.assertEqual(manifest["items"], [])
            order.append("delete")
        self.qb.delete_with_data.side_effect = deleted
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 600)
        self.assertFalse(order)
        self.guard.poll(now=self.now + 601)
        self.assertEqual(order, ["stop", "blocklist", "stop", "delete"])
        self.assertEqual(self.guard.store.entries()[0][-1], "metadata")
        self.qb.torrents.return_value = []
        self.guard.poll(now=self.now + 3606)
        self.assertFalse(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute("SELECT count(*) FROM deletion_audit").fetchone()[0], 1)
        self.assertFalse((self.base / "absent-download-mount").exists())

    def test_restart_preserves_timer(self):
        self.guard.poll(now=self.now)
        resumed = self.make_guard()
        resumed.poll(now=self.now + 601)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_leaving_metadata_or_disappearing_resets_timer(self):
        self.guard.poll(now=self.now)
        self.qb.torrents.return_value = [{**self.torrent, "state": "queuedDL"}]
        self.guard.poll(now=self.now + 500)
        self.assertFalse(self.guard.metadata_waits)
        self.qb.torrents.return_value = [self.torrent]
        self.guard.poll(now=self.now + 601)
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.metadata_waits["abc"][2], self.now + 601)
        self.qb.torrents.return_value = []
        self.guard.poll(now=self.now + 700)
        self.assertFalse(self.guard.store.metadata_waits())

    def test_readded_hash_and_clock_rollback_start_new_timer(self):
        self.guard.poll(now=self.now)
        self.torrent["added_on"] = self.now + 20
        self.guard.poll(now=self.now + 601)
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.metadata_waits["abc"][2], self.now + 601)
        self.guard.poll(now=self.now + 30)
        self.assertEqual(self.guard.metadata_waits["abc"][2], self.now + 30)

    def test_forced_metadata_is_same_continuous_wait(self):
        self.guard.poll(now=self.now)
        self.torrent["state"] = "forcedMetaDL"
        self.guard.poll(now=self.now + 601)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_unrelated_categories_and_plain_queues_never_timeout(self):
        for torrent in ({**self.torrent, "category": "manual"}, {**self.torrent, "state": "queuedDL"}):
            self.qb.torrents.return_value = [torrent]
            self.guard.poll(now=self.now)
            self.guard.poll(now=self.now + 7200)
        self.assertFalse(self.guard.store.metadata_waits())
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_dry_run_never_mutates_existing_timer_or_rejection_tables(self):
        self.guard.poll(now=self.now)
        before = self.guard.store.metadata_waits()
        dry = self.make_guard(replace(self.config, dry_run=True))
        dry.poll(now=self.now + 601)
        self.assertEqual(dry.store.metadata_waits(), before)
        self.assertFalse(dry.store.entries())
        self.assertIsNone(dry.store.evidence("abc"))
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()
        self.qb.torrents.return_value = []
        dry.poll(now=self.now + 700)
        self.assertEqual(dry.store.metadata_waits(), before)

    def test_arr_outage_or_missing_queue_never_authorizes_delete(self):
        self.arr.queue_by_hash.return_value = {}
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "match")
        self.arr.queue_by_hash.return_value = {"abc": 42}
        self.arr.blocklist_and_remove.side_effect = ApiError("offline", 503)
        self.guard.poll(now=self.now + 610)
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "arr")

    def test_arr_404_is_not_confirmation(self):
        self.arr.blocklist_and_remove.side_effect = ApiError("gone", 404)
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.assertEqual(self.guard.store.entries()[0][-2], "arr")

    def test_metadata_recovery_between_blocklist_and_delete_retains_download(self):
        self.arr.blocklist_and_remove.side_effect = lambda queue: setattr(self.qb.files, "return_value", [{"name": "Movie.mkv"}])
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.assertTrue(self.guard.store.entries())

    def test_inconclusive_or_nonzero_payload_never_rejected(self):
        self.guard.poll(now=self.now)
        for key, value in (("downloaded", 1), ("completed", 1), ("progress", 0.2), ("completed", None)):
            self.qb.torrents.return_value = [{**self.torrent, key: value}]
            self.guard.poll(now=self.now + 601)
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_file_api_failure_never_treated_as_empty_metadata(self):
        self.guard.poll(now=self.now)
        self.qb.files.side_effect = ApiError("offline", 503)
        self.guard.poll(now=self.now + 601)
        self.qb.stop.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_unresolved_filesystem_hold_prevents_timeout_rejection(self):
        self.guard.poll(now=self.now)
        self.guard._defer_filesystem("abc", "unresolved filesystem access", self.now + 600)
        self.guard.poll(now=self.now + 601)
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_stopped_metadata_rejection_resumes_after_restart(self):
        self.arr.queue_by_hash.return_value = {}
        self.expire()
        self.torrent["state"] = "stoppedDL"
        self.arr.queue_by_hash.return_value = {"abc": 42}
        resumed = self.make_guard()
        resumed.poll(now=self.now + 610)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_stop_state_transition_retries_without_repeating_blocklist(self):
        self.qb.stop.side_effect = lambda h: setattr(
            self.qb.torrents, "return_value", [{**self.torrent, "state": "stoppedDL"}])
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.guard.poll(now=self.now + 665)
        self.qb.delete_with_data.assert_called_once_with("abc")
        self.arr.blocklist_and_remove.assert_called_once_with(42)

    def test_timer_migration_preserves_existing_journals_and_evidence(self):
        self.guard.store.add("existing", "SONARR", "Saved download", "test")
        self.guard.store.save_manifest("existing", {"version": 1, "items": [{"path": "saved"}]})
        self.guard.store.set_cleanup_retry("existing", 2, self.now + 100, "saved retry")
        self.guard.store.db.execute("DROP TABLE metadata_wait")
        self.guard.store.db.commit()
        upgraded = self.make_guard()
        self.assertFalse(upgraded.store.metadata_waits())
        self.assertEqual(upgraded.store.entries()[0][0], "existing")
        self.assertEqual(upgraded.store.evidence("existing")[0]["items"], [{"path": "saved"}])
        self.assertEqual(upgraded.store.cleanup_retries()["existing"][0], 2)

    def test_hash_reuse_after_stop_is_preserved(self):
        self.arr.queue_by_hash.return_value = {}
        self.expire()
        self.qb.stop.reset_mock()
        self.torrent["added_on"] = self.now + 602
        self.guard.poll(now=self.now + 610)
        self.qb.stop.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_manifest_rejects_unknown_data_or_fake_empty_file_evidence(self):
        for key, value in (("downloaded", 1), ("completion_on", self.now), ("completed", None), ("state", "downloading")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                metadata_manifest({**self.torrent, key: value})
        with self.assertRaises(ValueError):
            validate_metadata_manifest({"version": 1, "items": [], "roots": {}})
        with self.assertRaises(ValueError):
            self.guard.verifier.remaining(metadata_manifest(self.torrent))

    def test_category_without_media_tag_never_times_out(self):
        for tags in ("", "manual", "tv-backup", "movies-extra"):
            self.torrent["tags"] = tags
            self.expire()
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_tag_removal_after_guard_stop_prevents_blocklist(self):
        self.qb.stop.side_effect = lambda h: self.torrent.update(tags="manual")
        self.expire()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_tag_removal_on_pending_retry_prevents_actions(self):
        self.arr.queue_by_hash.return_value = {}
        self.expire()
        self.qb.stop.reset_mock()
        self.torrent["tags"] = ""
        self.guard.poll(now=self.now + 610)
        self.qb.stop.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_tag_removal_resets_observation(self):
        self.guard.poll(now=self.now)
        self.torrent["tags"] = ""
        self.guard.poll(now=self.now + 500)
        self.torrent["tags"] = "tv"
        self.guard.poll(now=self.now + 601)
        self.qb.stop.assert_not_called()
        self.assertEqual(self.guard.metadata_waits["abc"][2], self.now + 601)
