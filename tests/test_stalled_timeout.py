"""Persistent stalled deadlines with real file evidence and mocked remote APIs."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from guard import ApiError, ArrInstance, Config, Guard, stall_conditions, stall_rule, stall_timeout, save_tagged


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
        self.torrent.update(progress=0.2, downloaded=200, completed=200, amount_left=800, num_complete=0)

    def expire(self, seconds=10800):
        """Observe the condition before advancing to its deadline."""
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + seconds)

    def assert_retained(self):
        """No destructive remote call may have occurred."""
        self.qb.stop.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_three_hour_boundary_arr_order_and_saved_files(self):
        order = []
        self.qb.stop.side_effect = lambda h: order.append("stop")
        self.arr.blocklist_and_remove.side_effect = lambda q: order.append("blocklist")
        def deleted(h):
            manifest, requested = self.guard.store.evidence(h)
            self.assertEqual(manifest["version"], 1)
            self.assertTrue(manifest["items"])
            self.assertEqual(requested, self.now + 10800)
            order.append("delete")
        self.qb.delete_with_data.side_effect = deleted
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 10799)
        self.assert_retained()
        self.guard.poll(now=self.now + 10800)
        self.assertEqual(order, ["stop", "blocklist", "stop", "delete"])
        self.assertEqual(self.guard.store.entries()[0][-1], "stalled")

    def test_zero_total_seeder_downloading_and_forced_downloads_expire_after_three_hours(self):
        for state in ("downloading", "forcedDL"):
            with self.subTest(state=state):
                self.torrent.update(state=state, num_complete=0)
                self.guard = self.make_guard(replace(self.config, state_db=":memory:"))
                self.qb.reset_mock()
                self.arr.reset_mock()
                self.guard.poll(now=self.now)
                self.guard.poll(now=self.now + 10799)
                self.assert_retained()
                self.guard.poll(now=self.now + 10800)
                self.qb.delete_with_data.assert_called_once_with("abc")
                self.arr.blocklist_and_remove.assert_called_once_with(42)

    def test_switching_zero_total_seeder_active_states_keeps_same_timer(self):
        self.torrent["num_complete"] = 0
        self.guard.poll(now=self.now)
        self.torrent["state"] = "downloading"
        self.guard.poll(now=self.now + 7200)
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now)
        self.torrent["state"] = "forcedDL"
        self.guard.poll(now=self.now + 10800)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_legacy_timer_cannot_authorize_new_policy_after_upgrade(self):
        self.guard.poll(now=self.now)
        legacy = self.guard.stalled_waits["abc"].copy()
        legacy.pop("observations")
        legacy["signature"].pop("policy")
        legacy["rule"] = "empty"
        legacy["first_seen"] = self.now - 86400
        self.guard.store.save_stalled_waits({"abc": legacy})
        resumed = self.make_guard()
        resumed.poll(now=self.now + 3600)
        self.assert_retained()
        self.assertEqual(resumed.stalled_waits["abc"]["first_seen"], self.now + 3600)
        resumed.poll(now=self.now + 14400)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_positive_total_seeders_protect_below_fifteen_percent_from_short_timeouts(self):
        self.torrent.update(progress=.149, completed=149, downloaded=149, amount_left=851,
                            num_complete=8, num_seeds=0)
        self.expire()
        self.assert_retained()
        self.assertEqual(set(self.guard.stalled_waits["abc"]["observations"]), {"unconnected"})

    def test_inactive_fifteen_percent_boundary_and_active_seeders_preserve_download(self):
        for changes in ({"progress": .15}, {"completed": 150}, {"num_seeds": 1},
                        {"num_seeds": None}, {"num_seeds": -1}, {"num_seeds": True},
                        {"num_seeds": "0"}, {"num_seeds": float("nan")}, {"dlspeed": 1}):
            with self.subTest(changes=changes):
                self.assertNotIn("inactive", stall_conditions({**self.torrent, **changes}))

    def test_inactive_seed_and_progress_recovery_reset_observed_timer(self):
        self.torrent.update(progress=.1, completed=100, downloaded=100)
        self.guard.poll(now=self.now)
        self.torrent["num_seeds"] = 1
        self.guard.poll(now=self.now + 1000)
        self.assertNotIn("inactive", self.guard.stalled_waits["abc"]["observations"])
        self.torrent["num_seeds"] = 0
        self.guard.poll(now=self.now + 2000)
        self.assertEqual(self.guard.stalled_waits["abc"]["observations"]["inactive"]["first_seen"], self.now + 2000)
        self.torrent.update(progress=.101, completed=101, downloaded=101)
        self.guard.poll(now=self.now + 2001)
        self.assertEqual(self.guard.stalled_waits["abc"]["observations"]["inactive"]["first_seen"], self.now + 2001)
        self.assert_retained()

    def test_zero_total_seeders_rejects_middle_progress_and_peer_transfer_after_one_week(self):
        self.torrent.update(state="downloading", progress=.6, completed=600, downloaded=600,
                            amount_left=400, num_complete=0, num_seeds=0, dlspeed=20)
        self.guard.poll(now=self.now)
        self.torrent.update(progress=.65, completed=650, downloaded=650, amount_left=350)
        self.guard.poll(now=self.now + 604799)
        self.assert_retained()
        self.guard.poll(now=self.now + 604800)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_conditions_use_independent_clocks_and_earliest_deadline(self):
        self.torrent["num_complete"] = 2
        self.guard.poll(now=self.now)
        self.torrent["num_complete"] = 0
        self.guard.poll(now=self.now + 6000)
        observations = self.guard.stalled_waits["abc"]["observations"]
        self.assertEqual(observations["unconnected"]["first_seen"], self.now)
        self.assertEqual(observations["seedless"]["first_seen"], self.now + 6000)
        self.guard.poll(now=self.now + 16800)
        self.qb.delete_with_data.assert_called_once_with("abc")
        self.assertEqual(self.guard.store.evidence("abc")[0]["timeout"]["rule"], "seedless")

    def test_recovering_one_condition_does_not_reset_other_clock(self):
        self.torrent["num_complete"] = 0
        self.guard.poll(now=self.now)
        self.torrent["num_seeds"] = 1
        self.guard.poll(now=self.now + 6000)
        self.assertEqual(tuple(self.guard.stalled_waits["abc"]["observations"]), ("seedless",))
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now)
        self.guard.poll(now=self.now + 10800)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_active_seeder_reappearing_after_blocklist_prevents_long_wait_delete(self):
        self.torrent["num_complete"] = 3
        self.arr.blocklist_and_remove.side_effect = lambda _: self.torrent.update(num_seeds=1)
        self.expire(30 * 86400)
        self.qb.delete_with_data.assert_not_called()

    def test_completed_and_seeding_torrents_cannot_qualify_for_replacement_rules(self):
        for state in ("uploading", "forcedUP", "stalledUP", "pausedUP", "queuedUP", "checkingUP"):
            self.assertEqual(stall_conditions({**self.torrent, "state": state, "num_complete": 0}), ())
        self.assertEqual(stall_conditions({**self.torrent, "progress": 1, "num_complete": 0}), ())

    def test_both_observations_survive_restart_and_dry_run_preserves_database(self):
        self.torrent["num_complete"] = 0
        self.guard.poll(now=self.now)
        resumed = self.make_guard()
        resumed.poll(now=self.now + 6000)
        self.assertEqual(set(resumed.stalled_waits["abc"]["observations"]), {"inactive", "seedless", "unconnected"})
        before = list(self.guard.store.db.iterdump())
        dry = self.make_guard(replace(self.config, dry_run=True))
        dry.poll(now=self.now + 10800)
        self.assert_retained()
        self.assertEqual(list(self.guard.store.db.iterdump()), before)

    def test_legacy_guard_stopped_pending_can_finish_new_observation_after_upgrade(self):
        self.guard.poll(now=self.now)
        legacy = self.guard.stalled_waits["abc"].copy()
        legacy.pop("observations")
        legacy["signature"].pop("policy")
        legacy["rule"] = "empty"
        legacy["stopped_by_guard"] = True
        self.guard.store.add("abc", "RADARR", "Movie", "old timeout", kind="stalled")
        self.guard.store.save_stalled_waits({"abc": legacy})
        self.torrent["state"] = "stoppedDL"
        resumed = self.make_guard()
        resumed.poll(now=self.now + 1)
        self.assert_retained()
        self.assertTrue(resumed.stalled_waits["abc"]["stopped_by_guard"])
        resumed.poll(now=self.now + 2)
        self.assertTrue(resumed.stalled_waits["abc"]["stopped_by_guard"])
        resumed.poll(now=self.now + 10801)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_old_guard_stop_never_authorizes_readded_manually_stopped_torrent(self):
        self.arr.queue_by_hash.return_value = {}
        self.qb.stop.side_effect = lambda _: self.torrent.update(state="stoppedDL")
        self.expire()
        self.qb.stop.reset_mock()
        self.torrent["added_on"] = self.now + 10801
        resumed = self.make_guard()
        resumed.poll(now=self.now + 10802)
        self.assertFalse(resumed.stalled_waits)
        self.qb.stop.assert_not_called()
        self.qb.delete_with_data.assert_not_called()

    def test_active_partial_downloads_qualify_for_zero_total_seeder_timeout(self):
        self.seedless()
        for state in ("downloading", "forcedDL"):
            self.assertEqual(stall_rule({**self.torrent, "state": state}), "seedless")

    def test_seedless_three_day_boundary_for_fifteen_to_fifty_percent(self):
        self.seedless()
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 259199)
        self.assert_retained()
        self.guard.poll(now=self.now + 259200)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def set_progress(self, fraction):
        """Keep explicit percentage and completed-byte evidence in agreement."""
        self.torrent.update(progress=fraction, completed=round(1000 * fraction),
                            downloaded=round(1000 * fraction), amount_left=1000 - round(1000 * fraction))

    def test_seedless_exact_percentage_bands_and_deadlines(self):
        for fraction, deadline in ((0, 10800), (.149, 10800), (.15, 259200),
                                   (.499, 259200), (.5, 604800), (.799, 604800)):
            with self.subTest(fraction=fraction):
                self.set_progress(fraction)
                self.torrent.update(state="downloading", num_complete=0)
                self.guard = self.make_guard(replace(self.config, state_db=":memory:"))
                self.qb.reset_mock()
                self.arr.reset_mock()
                self.guard.poll(now=self.now)
                self.assertEqual(stall_timeout("seedless", self.torrent), deadline)
                self.guard.poll(now=self.now + deadline - 1)
                self.assert_retained()
                self.guard.poll(now=self.now + deadline)
                self.qb.delete_with_data.assert_called_once_with("abc")
                self.arr.blocklist_and_remove.assert_called_once_with(42)

    def test_reaching_higher_band_extends_wait_without_resetting_seedless_history(self):
        self.torrent.update(state="downloading", num_complete=0)
        self.guard.poll(now=self.now)
        self.set_progress(.15)
        self.guard.poll(now=self.now + 10800)
        self.assert_retained()
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now)
        self.set_progress(.5)
        self.guard.poll(now=self.now + 259200)
        self.assert_retained()
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now)
        self.guard.poll(now=self.now + 604800)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_eighty_percent_and_above_retained_even_after_thirty_days_without_seeders(self):
        for fraction in (.8, .81, .99):
            with self.subTest(fraction=fraction):
                self.set_progress(fraction)
                self.torrent.update(state="downloading", num_complete=0)
                self.guard = self.make_guard(replace(self.config, state_db=":memory:"))
                self.guard.poll(now=self.now)
                self.guard.poll(now=self.now + 40 * 86400)
                self.assert_retained()
                self.assertFalse(self.guard.stalled_waits)

    def test_thirty_observed_days_with_positive_totals_zero_active_seeders(self):
        self.set_progress(.79)
        self.torrent.update(state="downloading", num_complete=20, num_seeds=0, dlspeed=500,
                            added_on=self.now - 60 * 86400)
        self.guard.poll(now=self.now)
        self.assert_retained()
        self.guard.poll(now=self.now + 30 * 86400 - 1)
        self.assert_retained()
        self.guard.poll(now=self.now + 30 * 86400)
        self.qb.delete_with_data.assert_called_once_with("abc")
        self.arr.blocklist_and_remove.assert_called_once_with(42)
        self.assertEqual(self.guard.store.evidence("abc")[0]["timeout"]["rule"], "unconnected")

    def test_old_torrent_age_alone_never_authorizes_rejection(self):
        self.set_progress(.6)
        self.torrent.update(num_complete=10, num_seeds=1, added_on=self.now - 90 * 86400)
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 40 * 86400)
        self.assert_retained()
        self.assertFalse(self.guard.stalled_waits)

    def test_old_paused_stopped_queued_checking_and_moving_downloads_excluded(self):
        self.set_progress(.4)
        self.torrent.update(num_complete=0, added_on=self.now - 40 * 86400)
        for state in ("pausedDL", "stoppedDL", "queuedDL", "checkingDL", "moving"):
            self.torrent["state"] = state
            self.guard.poll(now=self.now)
            self.assert_retained()
            self.assertFalse(self.guard.stalled_waits)

    def test_byte_percentage_cannot_bypass_retention_with_stale_lower_progress(self):
        for progress, completed in ((.1, 800), (.8, 100)):
            self.torrent.update(num_complete=0, progress=progress, completed=completed,
                                added_on=self.now - 40 * 86400)
            self.guard.poll(now=self.now)
            self.assert_retained()
            self.assertFalse(self.guard.stalled_waits)

    def test_reaching_eighty_percent_after_long_wait_blocklist_prevents_deletion(self):
        self.set_progress(.6)
        self.torrent.update(num_complete=5, num_seeds=0, added_on=self.now - 31 * 86400)
        self.arr.blocklist_and_remove.side_effect = lambda _: self.set_progress(.8)
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 30 * 86400)
        self.qb.delete_with_data.assert_not_called()

    def test_crossing_seedless_band_after_blocklist_rechecks_longer_deadline(self):
        self.torrent.update(state="downloading", num_complete=0)
        self.arr.blocklist_and_remove.side_effect = lambda _: self.set_progress(.15)
        self.expire()
        self.qb.delete_with_data.assert_not_called()

    def test_thirty_day_observation_rejection_still_waits_for_arr_replacement(self):
        self.set_progress(.6)
        self.torrent.update(num_complete=10, num_seeds=0, added_on=self.now - 31 * 86400)
        self.arr.blocklist_and_remove.side_effect = ApiError("offline", 503)
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 30 * 86400)
        self.qb.delete_with_data.assert_not_called()
        self.assertTrue(self.guard.store.entries())

    def test_long_wait_guard_owned_stop_retries_after_restart(self):
        self.set_progress(.6)
        self.torrent.update(num_complete=10, num_seeds=0, added_on=self.now - 31 * 86400)
        self.arr.queue_by_hash.return_value = {}
        self.qb.stop.side_effect = lambda _: self.torrent.update(state="stoppedDL")
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 30 * 86400)
        self.qb.delete_with_data.assert_not_called()
        self.arr.queue_by_hash.return_value = {"abc": 42}
        resumed = self.make_guard()
        resumed.poll(now=self.now + 30 * 86400 + 61)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_band_timer_survives_restart_and_dry_run_keeps_saved_state(self):
        self.set_progress(.6)
        self.torrent.update(state="downloading", num_complete=0)
        self.guard.poll(now=self.now)
        resumed = self.make_guard()
        resumed.poll(now=self.now + 259200)
        self.assert_retained()
        before = list(self.guard.store.db.iterdump())
        dry = self.make_guard(replace(self.config, dry_run=True))
        dry.poll(now=self.now + 604800)
        self.assert_retained()
        self.assertEqual(list(self.guard.store.db.iterdump()), before)
        resumed.poll(now=self.now + 604800)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_save_tag_is_exact_case_insensitive_and_blocks_timeouts(self):
        for tag in ("save", "SAVE", "tv, SaVe, custom"):
            self.assertTrue(save_tagged({"tags": tag}))
        for tag in ("saved", "save-copy", "unsave", ""):
            self.assertFalse(save_tagged({"tags": tag}))
        self.torrent.update(tags="movies, save", num_complete=0, added_on=self.now - 40 * 86400)
        self.guard.poll(now=self.now)
        self.assert_retained()
        self.assertFalse(self.guard.stalled_waits)
        self.assertFalse(self.guard.store.entries())

    def test_save_tag_added_after_blocklist_prevents_delete(self):
        self.arr.blocklist_and_remove.side_effect = lambda _: self.torrent.update(tags="movies, save")
        self.expire()
        self.qb.delete_with_data.assert_not_called()

    def test_positive_total_seeders_survive_short_deadlines_at_every_band(self):
        for fraction in (0, .1, .2, .6):
            self.set_progress(fraction)
            self.torrent.update(num_complete=3, num_seeds=0)
            self.guard = self.make_guard(replace(self.config, state_db=":memory:"))
            self.guard.poll(now=self.now)
            self.guard.poll(now=self.now + 7 * 86400)
            self.assert_retained()

    def test_active_seeder_connection_resets_thirty_day_observation(self):
        self.set_progress(.6)
        self.torrent.update(num_complete=3, num_seeds=0)
        self.guard.poll(now=self.now)
        self.torrent["num_seeds"] = 1
        self.guard.poll(now=self.now + 29 * 86400)
        self.assertFalse(self.guard.stalled_waits)
        self.torrent["num_seeds"] = 0
        self.guard.poll(now=self.now + 30 * 86400)
        self.guard.poll(now=self.now + 59 * 86400)
        self.assert_retained()
        self.guard.poll(now=self.now + 60 * 86400)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_unknown_active_seeders_never_establish_thirty_day_wait(self):
        self.set_progress(.6)
        for count in (None, -1, True, "0", float("nan")):
            self.torrent.update(num_complete=3, num_seeds=count)
            self.guard.poll(now=self.now)
            self.guard.poll(now=self.now + 31 * 86400)
            self.assert_retained()
            self.assertFalse(self.guard.stalled_waits)

    def test_peer_progress_and_total_seeder_changes_preserve_zero_active_history(self):
        self.set_progress(.2)
        self.torrent.update(num_complete=3, num_seeds=0, dlspeed=50)
        self.guard.poll(now=self.now)
        self.set_progress(.6)
        self.torrent["num_complete"] = 9
        self.guard.poll(now=self.now + 29 * 86400)
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now)
        self.guard.poll(now=self.now + 30 * 86400)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_save_tag_added_before_pending_stop_prevents_all_actions(self):
        self.guard.poll(now=self.now)
        self.qb.torrents.side_effect = [[self.torrent], [{**self.torrent, "tags": "movies, save"}]]
        self.guard.poll(now=self.now + 10800)
        self.assert_retained()

    def test_removing_save_tag_starts_new_observation(self):
        self.torrent["tags"] = "movies, save"
        self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 31 * 86400)
        self.assert_retained()
        self.torrent["tags"] = "movies"
        self.guard.poll(now=self.now + 31 * 86400 + 1)
        self.assert_retained()
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now + 31 * 86400 + 1)

    def test_tags_are_required_even_when_category_matches(self):
        for tags in ("", "manual", "movies-backup", "tv-archive"):
            with self.subTest(tags=tags):
                self.torrent["tags"] = tags
                self.expire(604801)
                self.assert_retained()
                self.assertFalse(self.guard.stalled_waits)

    def test_either_tag_with_whitespace_and_case_is_accepted(self):
        for tag in ("movies", "tv", "other, TV, extra", "MOVIES"):
            self.assertIn("inactive", stall_conditions({**self.torrent, "tags": tag}))

    def test_unrelated_or_ambiguous_arr_ownership_is_excluded(self):
        self.torrent["category"] = "manual"
        self.expire()
        self.assert_retained()

    def test_paused_queued_checking_and_moving_downloads_excluded(self):
        self.torrent["num_complete"] = 0
        for state in ("pausedDL", "stoppedDL", "queuedDL", "checkingDL", "moving"):
            with self.subTest(state=state):
                self.torrent["state"] = state
                self.expire(10800)
                self.assert_retained()
                self.assertFalse(self.guard.stalled_waits)

    def test_unknown_or_nonzero_total_seeders_does_not_qualify_for_seedless(self):
        self.seedless()
        for seeds in (None, -1, 1, True, "0", float("nan")):
            self.assertNotIn("seedless", stall_conditions({**self.torrent, "num_complete": seeds}))
        self.assertEqual(stall_rule({**self.torrent, "num_seeds": 1}), "seedless")

    def test_invalid_seedless_counters_are_retained(self):
        self.seedless()
        for key, value in (("progress", 1), ("progress", -0.1), ("completed", 1000),
                           ("total_size", 0), ("completion_on", self.now),
                           ("downloaded", None), ("completed", True), ("progress", float("inf"))):
            self.assertIsNone(stall_rule({**self.torrent, key: value}), (key, value))

    def test_progress_from_peers_keeps_seedless_timer_and_seed_recovery_resets_it(self):
        self.seedless()
        self.guard.poll(now=self.now)
        self.torrent.update(downloaded=201, completed=201, progress=0.201, dlspeed=10)
        self.guard.poll(now=self.now + 10799)
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now)
        self.assert_retained()
        self.torrent["num_complete"] = 1
        self.guard.poll(now=self.now + 10800)
        self.assertNotIn("seedless", self.guard.stalled_waits["abc"]["observations"])
        self.torrent["num_complete"] = 0
        self.guard.poll(now=self.now + 10801)
        self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now + 10801)

    def test_state_tag_removal_disappearance_and_readd_reset(self):
        for change in ({"state": "checkingDL"}, {"tags": ""}, {"added_on": self.now + 10}):
            self.guard.poll(now=self.now)
            original = self.torrent.copy()
            self.torrent.update(change)
            self.guard.poll(now=self.now + 10700)
            self.torrent.update(original)
            self.guard.poll(now=self.now + 10800)
            self.assert_retained()
            self.assertEqual(self.guard.stalled_waits["abc"]["first_seen"], self.now + 10800)
            self.qb.torrents.return_value = []
            self.guard.poll(now=self.now + 10801)
            self.assertFalse(self.guard.stalled_waits)
            self.qb.torrents.return_value = [self.torrent]

    def test_restart_and_clock_rollback(self):
        self.guard.poll(now=self.now)
        resumed = self.make_guard()
        resumed.poll(now=self.now - 10)
        self.assertEqual(resumed.stalled_waits["abc"]["first_seen"], self.now - 10)
        resumed.poll(now=self.now + 10790)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_arr_outage_and_404_preserve_download(self):
        self.arr.blocklist_and_remove.side_effect = ApiError("offline", 503)
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.arr.blocklist_and_remove.side_effect = ApiError("gone", 404)
        self.guard.poll(now=self.now + 10861)
        self.qb.delete_with_data.assert_not_called()

    def test_missing_queue_and_guard_stop_survive_restart(self):
        self.arr.queue_by_hash.return_value = {}
        self.qb.stop.side_effect = lambda h: self.torrent.update(state="stoppedDL")
        self.expire()
        self.qb.delete_with_data.assert_not_called()
        self.arr.queue_by_hash.return_value = {"abc": 42}
        resumed = self.make_guard()
        resumed.poll(now=self.now + 10805)
        self.qb.delete_with_data.assert_called_once_with("abc")

    def test_recovery_after_stop_prevents_blocklist(self):
        self.qb.stop.side_effect = lambda h: self.torrent.update(state="downloading", downloaded=1, completed=1, progress=0.001, num_complete=1, num_seeds=1)
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
        self.expire(259200)
        self.qb.delete_with_data.assert_not_called()

    def test_fresh_total_seeder_recovery_before_stop_prevents_all_actions(self):
        self.guard.poll(now=self.now)
        self.qb.torrents.side_effect = [[self.torrent], [{**self.torrent, "num_complete": 1}]]
        self.guard.poll(now=self.now + 10800)
        self.assert_retained()

    def test_file_api_and_filesystem_errors_preserve_download(self):
        self.guard.poll(now=self.now)
        self.qb.files.side_effect = ApiError("offline", 503)
        self.guard.poll(now=self.now + 10800)
        self.assert_retained()
        self.qb.files.side_effect = OSError("unreadable mount")
        self.guard.poll(now=self.now + 10861)
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
        dry.poll(now=self.now + 10800)
        self.assert_retained()
        self.assertEqual(list(self.guard.store.db.iterdump()), before)

    def test_files_must_be_absent_before_finish_and_no_local_stall_unlink(self):
        self.expire()
        self.qb.torrents.return_value = []
        self.guard.config = replace(self.config, cleanup_leftovers=True, cleanup_write_root=str(self.root / "complete"))
        self.guard.poll(now=self.now + 11000)
        self.assertTrue(self.path.is_file())
        self.assertTrue(self.guard.store.entries())
        self.path.unlink()
        self.guard.poll(now=self.now + 11005)
        self.assertFalse(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute("SELECT count(*) FROM deletion_audit").fetchone()[0], 1)

    def test_pending_seeder_recovery_resets_authorization_after_restart(self):
        self.arr.queue_by_hash.return_value = {}
        self.expire()
        self.qb.stop.reset_mock()
        self.torrent.update(downloaded=1, completed=1, progress=0.001, num_complete=1, num_seeds=1)
        resumed = self.make_guard()
        resumed.poll(now=self.now + 10805)
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
        self.expire(259200)
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
        self.guard.poll(now=self.now + 259200)
        self.qb.delete_with_data.assert_not_called()

    def test_retry_verification_revalidates_current_condition(self):
        self.expire()
        self.qb.delete_with_data.reset_mock()
        self.torrent.update(state="downloading", downloaded=10, completed=10, progress=0.01, num_complete=1, num_seeds=1)
        self.guard.poll(now=self.now + 10805)
        self.qb.delete_with_data.assert_not_called()
        self.assertTrue(self.guard.store.entries())

    def test_api_outage_preserves_observation(self):
        self.guard.poll(now=self.now)
        self.qb.torrents.side_effect = ApiError("offline", 503)
        with self.assertRaises(ApiError):
            self.guard.poll(now=self.now + 100)
        self.qb.torrents.side_effect = None
        resumed = self.make_guard()
        resumed.poll(now=self.now + 10800)
        self.qb.delete_with_data.assert_called_once_with("abc")
