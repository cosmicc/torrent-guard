"""Persistent stalled deadlines with real file evidence and mocked remote APIs."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from guard import ApiError, ArrInstance, Config, Guard, stall_rule


class StalledTimeoutTests(unittest.TestCase):
    """Exercise destructive ordering, races, recovery and scoped retention."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "incomplete").mkdir()
        (self.root / "complete").mkdir()
        self.path = self.root / "incomplete/Movie.mkv.!qB"
        self.path.write_bytes(b"partial media")
        self.now = 1_800_000_000
        instance = ArrInstance("http://arr", "test", "movies")
        self.config = Config("http://qb", "u", "p", {"RADARR": instance}, {"movies": "RADARR"},
                             dry_run=False, state_db=str(self.root / "state.sqlite3"),
                             guard_download_root=str(self.root), qbit_complete_dir="/downloads/complete",
                             qbit_incomplete_dir="/downloads/incomplete")
        self.torrent = {"hash": "abc", "name": "Movie", "category": "movies", "tags": "movies",
                        "state": "stalledDL", "added_on": self.now - 200000,
                        "progress": 0, "downloaded": 0, "completed": 0, "completion_on": -1,
                        "num_complete": 0, "num_seeds": 0, "dlspeed": 0, "total_size": 1000,
                        "size": 1000, "amount_left": 1000, "save_path": "/downloads/incomplete",
                        "download_path": "/downloads/incomplete",
                        "content_path": "/downloads/incomplete/Movie.mkv"}
        self.files = [{"name": "Movie.mkv", "size": 1000, "priority": 1, "progress": 0}]
        self.qb, self.arr = Mock(), Mock()
        self.qb.torrents.return_value = [self.torrent]
        self.qb.files.return_value = self.files
        self.arr.queue_by_hash.return_value = {"abc": 42}
        self.guard = self.make_guard()

    def make_guard(self, config=None):
        """Open a real SQLite journal and register connection cleanup."""
        guard = Guard(config or self.config, self.qb, {"RADARR": self.arr})
        self.addCleanup(guard.store.db.close)
        return guard

    def seedless(self):
        """Configure an incomplete torrent that has already downloaded payload."""
        self.torrent.update(progress=0.2, downloaded=200, completed=200, amount_left=800)

    def expire(self, seconds=3600):
        """Observe the condition before advancing to its deadline."""
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + seconds)

    def assert_retained(self):
        """No destructive remote call may have occurred."""
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_hour_boundary_arr_order_and_saved_files(self):
        order = []
        self.qb.stop.side_effect = lambda h: order.append("stop")
        self.arr.blocklist_and_remove.side_effect = lambda q: order.append("blocklist")
        def deleted(h):
            manifest, requested = self.guard.store.evidence(h)
            self.assertEqual(manifest["version"], 1)
            self.assertTrue(manifest["items"])
            self.assertEqual(requested, self.now + 3600)
            order.append("delete")
        self.qb.delete_with_data.side_effect = deleted
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 3599)
        self.assert_retained()
        self.guard.poll(now=self.now + 3600)
        self.assertEqual(order, ["stop", "blocklist", "stop", "delete"])
        self.assertEqual(self.guard.store.entries()[0][-1], "stalled")

    def test_seedless_week_boundary(self):
        self.seedless()
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 604799)
        self.assert_retained()
        self.guard.poll(now=self.now + 604800)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_tags_are_required_even_when_category_matches(self):
        for tags in ("", "manual", "movies-backup", "tv-archive"):
            with self.subTest(tags=tags):
                self.torrent["tags"] = tags
                self.expire(604801)
                self.assert_retained()
                self.assertFalse(self.guard.stalled_waits)

    def test_either_tag_with_whitespace_and_case_is_accepted(self):
        for tag in ("movies", "tv", "other, TV, extra", "MOVIES"):
            self.assertEqual(stall_rule({**self.torrent, "tags": tag}), "empty")

    def test_unrelated_or_ambiguous_arr_ownership_is_excluded(self):
        self.torrent["category"] = "manual"
        self.expire()
        self.assert_retained()

    def test_paused_queued_checking_and_active_downloads_excluded(self):
        for state in ("pausedDL", "stoppedDL", "queuedDL", "downloading", "forcedDL", "checkingDL", "moving"):
            with self.subTest(state=state):
                self.torrent["state"] = state
                self.expire(604800)
                self.assert_retained()
                self.assertFalse(self.guard.stalled_waits)

    def test_swarm_unknown_or_nonzero_never_qualifies_for_week(self):
        self.seedless()
        for seeds in (None, -1, 1, True, "0", float("nan")):
            self.assertIsNone(stall_rule({**self.torrent, "num_complete": seeds}))
        self.assertIsNone(stall_rule({**self.torrent, "num_seeds": 1}))

    def test_quarter_complete_and_invalid_counters_are_retained(self):
        self.seedless()
        for key, value in (("progress", 0.25), ("progress", -0.1), ("completed", 250),
                           ("total_size", 0), ("dlspeed", 1), ("completion_on", self.now),
                           ("downloaded", None), ("completed", True), ("progress", float("inf"))):
            self.assertIsNone(stall_rule({**self.torrent, key: value}), (key, value))

    def test_download_progress_and_seeder_recovery_reset_week(self):
        self.seedless()
        self.guard.poll(now=self.now)
        self.torrent.update(downloaded=201, completed=201, progress=0.201)
        self.guard.poll(now=self.now + 604799)
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now + 604799)
        self.guard.poll(now=self.now + 604800)
        self.assert_retained()
        self.torrent["num_complete"] = 1
        self.guard.poll(now=self.now + 604801)
        self.assertFalse(self.guard.stalled_waits)
        self.torrent["num_complete"] = 0
        self.guard.poll(now=self.now + 604802)
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now + 604802)

    def test_state_tag_removal_disappearance_and_readd_reset(self):
        for change in ({"state": "downloading"}, {"tags": ""}, {"added_on": self.now + 10}):
            self.guard.poll(now=self.now)
            original = self.torrent.copy()
            self.torrent.update(change)
            self.guard.poll(now=self.now + 3500)
            self.torrent.update(original)
            self.guard.poll(now=self.now + 3600)
            self.assert_retained()
            self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now + 3600)
            self.qb.torrents.return_value = []
            self.guard.poll(now=self.now + 3601)
            self.assertFalse(self.guard.stalled_waits)
            self.qb.torrents.return_value = [self.torrent]

    def test_restart_and_clock_rollback(self):
        self.guard.poll(now=self.now)
        resumed = self.make_guard()
        resumed.poll(now=self.now - 10)
        self.assertEqual(resumed.stalled_waits["abc"]["first_seen"], self.now - 10)
        resumed.poll(now=self.now + 3590)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_arr_outage_and_404_preserve_download(self):
        self.arr.blocklist_and_remove.side_effect = ApiError("offline", 503)
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.arr.blocklist_and_remove.side_effect = ApiError("gone", 404)
        self.guard.poll(now=self.now + 3661)
        self.qb.delete_with_data.assert_not_called()

    def test_missing_queue_and_guard_stop_survive_restart(self):
        self.arr.queue_by_hash.return_value = {}
        self.qb.stop.side_effect = lambda h: self.torrent.update(state="stoppedDL")
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.arr.queue_by_hash.return_value = {"abc": 42}
        resumed = self.make_guard()
        resumed.poll(now=self.now + 3605)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_recovery_after_stop_prevents_blocklist(self):
        self.qb.stop.side_effect = lambda h: self.torrent.update(state="downloading")
        self.expire()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_recovery_or_tag_removal_after_blocklist_prevents_delete(self):
        self.arr.blocklist_and_remove.side_effect = lambda q: self.torrent.update(tags="manual")
        self.expire()
        self.qb.delete_with_data.assert_not_called()

    def test_swarm_recovery_after_blocklist_prevents_delete(self):
        self.seedless()
        self.arr.blocklist_and_remove.side_effect = lambda q: self.torrent.update(num_complete=1)
        self.expire(604800)
        self.qb.delete_with_data.assert_not_called()

    def test_fresh_progress_change_before_stop_prevents_all_actions(self):
        self.guard.poll(now=self.now)
        self.qb.torrents.side_effect = [[self.torrent], [{**self.torrent, "downloaded": 1}]]
        self.guard.poll(now=self.now + 3600)
        self.assert_retained()

    def test_file_api_and_filesystem_errors_preserve_download(self):
        self.guard.poll(now=self.now)
        self.qb.files.side_effect = ApiError("offline", 503)
        self.guard.poll(now=self.now + 3600)
        self.assert_retained()
        self.qb.files.side_effect = OSError("unreadable mount")
        self.guard.poll(now=self.now + 3661)
        self.assert_retained()
        self.assertIn("abc", self.guard.filesystem_retries)

    def test_empty_file_list_requires_review(self):
        self.qb.files.return_value = []
        self.expire()
        self.assert_retained()

    def test_dry_run_changes_no_saved_state(self):
        self.guard.poll(now=self.now)
        before = list(self.guard.store.db.iterdump())
        dry = self.make_guard(replace(self.config, dry_run=True))
        dry.poll(now=self.now + 3600)
        self.assert_retained()
        self.assertEqual(list(self.guard.store.db.iterdump()), before)

    def test_files_must_be_absent_before_finish_and_no_local_stall_unlink(self):
        self.expire()
        self.qb.torrents.return_value = []
        self.guard.config = replace(self.config, cleanup_leftovers=True, cleanup_write_root=str(self.root / "complete"))
        self.guard.poll(now=self.now + 3800)
        self.assertTrue(self.path.is_file())
        self.assertTrue(self.guard.store.entries())
        self.path.unlink()
        self.guard.poll(now=self.now + 3805)
        self.assertFalse(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute("SELECT count(*) FROM deletion_audit").fetchone()[0], 1)

    def test_pending_progress_recovery_resets_authorization_after_restart(self):
        self.arr.queue_by_hash.return_value = {}
        self.expire()
        self.qb.stop.reset_mock()
        self.torrent.update(downloaded=1, completed=1, progress=0.001)
        resumed = self.make_guard()
        resumed.poll(now=self.now + 3605)
        self.qb.stop.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_sonarr_stall_uses_only_owning_arr(self):
        self.config = replace(self.config, instances={"SONARR": ArrInstance("http://sonarr", "test", "tv")},
                              categories={"tv": "SONARR"})
        self.torrent.update(category="tv", tags="tv")
        sonarr, radarr = Mock(), Mock()
        sonarr.queue_by_hash.return_value = {"abc": 12}
        self.guard = Guard(self.config, self.qb, {"SONARR": sonarr, "RADARR": radarr})
        self.addCleanup(self.guard.store.db.close)
        self.expire()
        sonarr.blocklist_and_remove.assert_called_once_with(12)
        radarr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_completed_root_stalled_leftovers_never_locally_unlinked(self):
        self.seedless()
        self.path.unlink()
        self.path = self.root / "complete/Movie.mkv"
        self.path.write_bytes(b"retained partial payload")
        self.torrent.update(save_path="/downloads/complete", download_path="/downloads/complete",
                            content_path="/downloads/complete/Movie.mkv")
        self.guard.config = replace(self.config, cleanup_leftovers=True,
                                    cleanup_write_root=str(self.root / "complete"))
        self.expire(604800)
        self.qb.torrents.return_value = []
        self.guard.poll(now=self.now + 605000)
        self.assertTrue(self.path.is_file())
        self.assertTrue(self.guard.store.entries())
        self.assertEqual(self.guard.store.evidence("abc")[0]["timeout"]["rule"], "seedless")

    def test_final_inventory_refresh_detects_recovery(self):
        self.seedless()
        self.guard.poll(now=self.now)
        def blocklisted(queue):
            # First refresh authorizes the second stop, then the deletion's own
            # inventory refresh must catch a newly discovered swarm seeder.
            self.qb.torrents.side_effect = [
                [dict(self.torrent)], [{**self.torrent, "num_complete": 1}]]
        self.arr.blocklist_and_remove.side_effect = blocklisted
        self.guard.poll(now=self.now + 604800)
        self.qb.delete_with_data.assert_not_called()

    def test_retry_verification_revalidates_current_condition(self):
        self.expire()
        self.qb.delete_with_data.reset_mock()
        self.torrent.update(state="downloading", downloaded=10, completed=10, progress=0.01)
        self.guard.poll(now=self.now + 3605)
        self.qb.delete_with_data.assert_not_called()
        self.assertTrue(self.guard.store.entries())

    def test_api_outage_preserves_observation(self):
        self.guard.poll(now=self.now)
        self.qb.torrents.side_effect = ApiError("offline", 503)
        with self.assertRaises(ApiError):
            self.guard.poll(now=self.now + 100)
        self.qb.torrents.side_effect = None
        resumed = self.make_guard()
        resumed.poll(now=self.now + 3600)
        self.qb.delete_with_data.assert_called_once_with("abc")
