"""Real temporary-file tests for deletion evidence and bounded leftover cleanup."""
from dataclasses import replace
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import ArrInstance, Config, Guard, PendingStore, is_unstarted_download


class DeletionTests(unittest.TestCase):
    """Use actual files and SQLite while replacing only remote API clients."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "downloads"
        self.completed = self.root / "completed"
        self.incomplete = self.root / "incomplete"
        self.completed.mkdir(parents=True)
        self.incomplete.mkdir()
        self.library = self.base / "library"
        self.library.mkdir()
        self.path = self.completed / "Movie.mkv"
        self.path.write_bytes(b"real test payload")
        self.sidecar = self.completed / "Movie.nfo"
        self.sidecar.write_bytes(b"sidecar")
        self.now = 1_800_000_000
        instance = ArrInstance("http://arr:7878", "dummy", "movies")
        self.cfg = Config("http://qbit:8080", "u", "p", {"RADARR": instance}, {"movies": "RADARR"},
                          dry_run=False, verify_video_content=False, state_db=str(self.base / "state.sqlite3"),
                          qbit_complete_dir="/downloads/completed", qbit_incomplete_dir="/downloads/incomplete",
                          guard_download_root=str(self.root), cleanup_leftovers=True,
                          cleanup_write_root=str(self.completed))
        self.torrent = {"hash": "abc123", "name": "Movie", "category": "movies", "added_on": self.now - 3600,
                        "completion_on": self.now - 1800, "progress": 1, "amount_left": 0, "state": "uploading",
                        "save_path": "/downloads/completed", "content_path": "/downloads/completed/Movie.mkv"}
        self.files = [{"name": "Movie.mkv", "size": self.path.stat().st_size, "priority": 1, "progress": 1},
                      {"name": "Movie.nfo", "size": self.sidecar.stat().st_size, "priority": 0, "progress": 1}]
        self.qbit, self.arr = Mock(), Mock()
        self.qbit.torrents.return_value = [self.torrent]
        self.qbit.files.return_value = self.files
        self.event = {"date": datetime.fromtimestamp(self.now - 1000, timezone.utc).isoformat(),
                      "data": {"droppedPath": "/downloads/completed/Movie.mkv"}}
        self.arr.imported_history.return_value = {"abc123": [self.event]}
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr})
        self.addCleanup(lambda: self.guard.store.db.close())

    def request(self):
        """Authorize and request API deletion, leaving physical files in place."""
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_called_once_with("abc123")
        self.qbit.torrents.return_value = []

    def test_legacy_rejected_leftovers_without_bad_payload_are_retained(self):
        self.request()
        # Simulate old rejection evidence for readable ordinary video. The
        # saved decision and identity manifest alone cannot authorize unlink.
        with self.guard.store.db:
            self.guard.store.db.execute("UPDATE pending SET kind='reject' WHERE hash='abc123'")
        self.guard.poll(now=self.now + 121)
        self.assertTrue(self.path.is_file())
        self.assertTrue(self.sidecar.is_file())
        self.assertTrue(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute("SELECT count(*) FROM deletion_audit").fetchone()[0], 0)

    def test_manifest_committed_before_api_call_and_all_files_verified(self):
        def delete(_hash):
            evidence = self.guard.store.evidence("abc123")
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence[1], self.now)
            paths = {item["path"] for item in evidence[0]["items"]}
            self.assertIn("Movie.nfo", paths)
            self.assertIn("Movie.mkv.!qB", paths)
            self.path.unlink()
            self.sidecar.unlink()
        self.qbit.delete_with_data.side_effect = delete
        self.request()
        with self.assertLogs("torrent-guard", level="INFO") as logs:
            self.guard.poll(now=self.now + 5)
        self.assertFalse(self.guard.store.entries())
        self.assertTrue(any("independently verified absent" in line for line in logs.output))
        self.assertEqual(self.guard.store.db.execute("SELECT count(*) FROM deletion_audit").fetchone()[0], 1)

    def test_leftovers_remain_pending_during_settle_then_are_removed(self):
        self.request()
        with self.assertLogs("torrent-guard", level="WARNING") as logs:
            self.guard.poll(now=self.now + 5)
        self.assertTrue(any("waiting for qBittorrent deletion settle period until" in line for line in logs.output))
        self.assertFalse(any("cleanup disabled" in line for line in logs.output))
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())
        self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(self.guard.store.entries())
        self.arr.blocklist_and_remove.assert_not_called()

    def test_imported_copy_and_hardlink_survive_local_cleanup(self):
        hardlink = self.library / "Movie.mkv"
        hardlink.hardlink_to(self.path)
        copied = self.library / "copy.mkv"
        copied.write_bytes(self.path.read_bytes())
        self.request()
        self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())
        self.assertEqual(hardlink.read_bytes(), b"real test payload")
        self.assertEqual(copied.read_bytes(), b"real test payload")

    def test_disabled_local_cleanup_keeps_evidence_and_files(self):
        self.guard.config = replace(self.cfg, cleanup_leftovers=False)
        self.request()
        with self.assertLogs("torrent-guard", level="WARNING") as logs:
            self.guard.poll(now=self.now + 1000)
        self.assertTrue(any("automatic leftover cleanup disabled (CLEANUP_LEFTOVERS=false)" in line for line in logs.output))
        self.assertTrue(any("compose.cleanup.yaml" in line for line in logs.output))
        self.assertTrue(self.path.exists())
        self.assertIsNotNone(self.guard.store.evidence("abc123"))
        self.assertTrue(self.guard.store.entries())

    def test_replaced_file_is_preserved(self):
        self.request()
        old = self.completed / "original-kept"
        self.path.rename(old)
        self.path.write_bytes(b"different useful file")
        self.guard.poll(now=self.now + 120)
        self.assertEqual(self.path.read_bytes(), b"different useful file")
        self.assertTrue(self.guard.store.entries())

    def test_file_appearing_after_snapshot_is_preserved(self):
        self.request()
        unexpected = self.completed / "Movie.mkv.!qB"
        unexpected.write_bytes(b"new data")
        self.guard.poll(now=self.now + 120)
        self.assertEqual(unexpected.read_bytes(), b"new data")
        self.assertTrue(self.guard.store.entries())

    def test_symlink_replacement_cannot_delete_library(self):
        self.request()
        library_file = self.library / "protected.mkv"
        library_file.write_bytes(b"library")
        self.path.unlink()
        self.path.symlink_to(library_file)
        self.guard.poll(now=self.now + 120)
        self.assertEqual(library_file.read_bytes(), b"library")
        self.assertTrue(self.path.is_symlink())
        self.assertTrue(self.guard.store.entries())

    def test_missing_and_replaced_root_never_count_as_verified_absence(self):
        self.request()
        moved = self.root / "moved"
        self.completed.rename(moved)
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.guard.store.entries())
        self.completed.mkdir()
        self.guard.poll(now=self.now + 125)
        self.assertTrue(self.guard.store.entries())
        self.assertTrue((moved / "Movie.mkv").exists())

    def test_write_mapping_mismatch_preserves_data(self):
        self.request()
        self.guard.verifier.config = replace(self.cfg, cleanup_write_root=str(self.library))
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())

    def test_permissions_and_api_errors_do_not_clear_journal(self):
        self.request()
        with patch.object(self.guard.verifier, "observe", side_effect=PermissionError("unreadable mount")):
            self.guard.poll(now=self.now + 120)
        self.assertTrue(self.guard.store.entries())
        self.arr.imported_history.side_effect = OSError("Arr offline")
        self.guard.poll(now=self.now + 125)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())

    def test_other_torrent_ownership_blocks_local_removal(self):
        self.request()
        self.qbit.torrents.return_value = [{**self.torrent, "hash": "other", "category": "manual"}]
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())

    def queued_torrent(self):
        """A real queue state with explicit zero lifetime and completed data."""
        return {**self.torrent, "hash": "queued", "category": "manual", "name": "Not started",
                "state": "queuedDL", "progress": 0, "downloaded": 0, "completed": 0,
                "completion_on": -1, "content_path": "/downloads/completed/Not started"}

    def test_zero_data_queued_torrent_without_files_does_not_block_cleanup(self):
        self.request()
        self.qbit.torrents.return_value = [self.queued_torrent()]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.guard.store.entries())
        self.assertFalse(self.guard.store.cleanup_retries())
        self.qbit.stop.assert_not_called()
        self.qbit.delete_with_data.assert_called_once_with("abc123")
        self.assertFalse((self.completed / "Not started").exists())

    def test_unstarted_magnet_without_content_path_does_not_block_cleanup(self):
        self.request()
        self.qbit.torrents.return_value = [{**self.queued_torrent(), "content_path": ""}]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())

    def assert_metadata_wait_allows_unrelated_cleanup(self, state):
        """An empty zero-data magnet must not freeze unrelated saved cleanup."""
        self.request()
        self.qbit.torrents.return_value = [{**self.queued_torrent(), "state": state, "content_path": ""}]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.guard.store.entries())
        self.qbit.stop.assert_not_called()
        self.qbit.delete_with_data.assert_called_once_with("abc123")
        self.arr.blocklist_and_remove.assert_not_called()

    def test_zero_data_metadata_download_allows_unrelated_cleanup(self):
        self.assert_metadata_wait_allows_unrelated_cleanup("metaDL")

    def test_zero_data_forced_metadata_download_allows_unrelated_cleanup(self):
        self.assert_metadata_wait_allows_unrelated_cleanup("forcedMetaDL")

    def test_metadata_wait_exception_preserves_safety_checks(self):
        self.request()
        for state in ("metaDL", "forcedMetaDL"):
            waiting = {**self.queued_torrent(), "state": state, "content_path": ""}
            invalid = [{**waiting, field: value} for field, value in (
                ("downloaded", 1), ("completed", 1), ("progress", 0.1),
                ("completion_on", self.now), ("downloaded", None),
            )]
            for field in ("progress", "downloaded", "completed", "completion_on"):
                invalid.append({key: value for key, value in waiting.items() if key != field})
            self.qbit.files.return_value = []
            for torrent in invalid:
                with self.subTest(state=state, torrent=torrent):
                    self.assertFalse(is_unstarted_download(torrent))
                    self.qbit.torrents.return_value = [torrent]
                    with self.assertRaisesRegex(ValueError, "file metadata is unavailable"):
                        self.guard._verify_deletion("abc123", "RADARR", "imported", self.now + 120)
                    self.assertTrue(self.path.exists())
            self.qbit.torrents.return_value = [{**waiting, "content_path": self.torrent["content_path"]}]
            with self.assertRaisesRegex(ValueError, "reserves recorded download path"):
                self.guard._verify_deletion("abc123", "RADARR", "imported", self.now + 120)
            self.qbit.torrents.return_value = [waiting]
            self.guard._defer_filesystem("queued", "unresolved I/O", self.now)
            with self.assertRaisesRegex(ValueError, "file metadata is unavailable"):
                self.guard._verify_deletion("abc123", "RADARR", "imported", self.now + 120)
            self.guard._clear_filesystem("queued")
            self.qbit.files.return_value = self.files
            with self.assertRaisesRegex(ValueError, "another torrent owns"):
                self.guard._verify_deletion("abc123", "RADARR", "imported", self.now + 120)
            self.assertTrue(self.path.exists())

    def test_queued_known_file_list_still_protects_overlapping_files(self):
        self.request()
        self.qbit.torrents.return_value = [self.queued_torrent()]
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())

    def test_queued_content_path_still_protects_existing_download(self):
        self.request()
        self.qbit.torrents.return_value = [{**self.queued_torrent(), "content_path": self.torrent["content_path"]}]
        self.qbit.files.return_value = []
        with self.assertLogs("torrent-guard", level="WARNING") as logs:
            self.guard.poll(now=self.now + 120)
        self.assertTrue(any("metadata/queued torrent reserves recorded download path" in line for line in logs.output))
        self.assertTrue(self.path.exists())

    def test_queued_with_unresolved_filesystem_hold_still_blocks_cleanup(self):
        self.request()
        self.guard._defer_filesystem("queued", "unresolved access error", self.now)
        self.qbit.torrents.return_value = [self.queued_torrent()]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.path.exists())
        self.assertIn("queued", self.guard.filesystem_retries)

    def test_unstarted_queue_requires_explicit_consistent_counters(self):
        queued = self.queued_torrent()
        self.assertTrue(is_unstarted_download(queued))
        for key, value in (("state", "downloading"), ("state", "queuedUP"), ("progress", 0.1),
                           ("downloaded", 1), ("completed", 1), ("completion_on", self.now),
                           ("downloaded", None), ("completed", "0"), ("progress", False),
                           ("progress", float("nan"))):
            with self.subTest(key=key, value=value):
                self.assertFalse(is_unstarted_download({**queued, key: value}))
        for key in ("downloaded", "completed", "progress", "completion_on"):
            without = dict(queued)
            del without[key]
            self.assertFalse(is_unstarted_download(without))

    def test_started_download_is_rechecked_after_queue_wait(self):
        queued = self.queued_torrent()
        self.qbit.torrents.return_value = [queued]
        self.qbit.files.return_value = []
        self.assertEqual(self.guard._ownership_paths({}), set())
        self.qbit.torrents.return_value = [{**queued, "state": "downloading", "downloaded": 1}]
        with self.assertRaisesRegex(ValueError, "complete qBittorrent file metadata is unavailable"):
            self.guard._ownership_paths({})

    def test_queued_file_api_failure_is_not_treated_as_empty_metadata(self):
        self.request()
        self.qbit.torrents.return_value = [self.queued_torrent()]
        self.qbit.files.side_effect = ValueError("invalid API response")
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())

    def test_owned_queued_torrent_waits_without_filesystem_validation_or_rejection(self):
        self.qbit.torrents.return_value = [{**self.queued_torrent(), "category": "movies"}]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now)
        self.assertFalse(self.guard.store.entries())
        self.assertFalse(self.guard.filesystem_retries)
        self.qbit.stop.assert_not_called()
        self.qbit.delete_with_data.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()

    def test_missing_ownership_metadata_names_both_downloads_and_backs_off(self):
        self.request()
        blocker = {**self.torrent, "hash": "other-secret-hash", "category": "manual",
                   "name": "Waiting for metadata", "content_path": "/downloads/completed/Waiting"}
        self.qbit.torrents.return_value = [blocker]
        self.qbit.files.return_value = []
        with self.assertLogs("torrent-guard", level="WARNING") as logs:
            self.guard.poll(now=self.now + 120)
        output = "\n".join(logs.output)
        self.assertIn("name='Movie'", output)
        self.assertIn("/downloads/completed/Movie.mkv", output)
        self.assertIn("ownership check blocked by name='Waiting for metadata'", output)
        self.assertIn("/downloads/completed/Waiting", output)
        self.assertIn("retry_in=60s", output)
        self.assertNotIn("abc123", output)
        self.assertNotIn("other-secret-hash", output)
        count = self.qbit.files.call_count
        self.guard.poll(now=self.now + 125)
        self.assertEqual(self.qbit.files.call_count, count)
        self.assertTrue(self.path.exists())
        with self.assertLogs("torrent-guard", level="WARNING") as logs:
            self.guard.poll(now=self.now + 180)
        self.assertTrue(any("retry_in=120s" in line for line in logs.output))
        # Removal resumes automatically when ownership can be established.
        self.qbit.torrents.return_value = []
        self.guard.poll(now=self.now + 300)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.guard.store.cleanup_retries())

    def test_cleanup_backoff_survives_restart_and_caps_at_fifteen_minutes(self):
        self.request()
        self.qbit.torrents.return_value = [{**self.torrent, "hash": "other", "category": "manual"}]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now + 120)
        self.guard.store.db.close()
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr})
        self.qbit.files.reset_mock()
        self.guard.poll(now=self.now + 125)
        self.qbit.files.assert_not_called()
        timestamp = self.now + 180
        for expected_delay in (120, 240, 480, 900, 900):
            self.guard.poll(now=timestamp)
            retry = self.guard.store.cleanup_retries()["abc123"]
            self.assertEqual(retry[1] - timestamp, expected_delay)
            timestamp = retry[1]
        self.assertTrue(self.path.exists())

    def test_shared_failed_ownership_scan_does_not_repeat_for_each_leftover(self):
        self.request()
        evidence = self.guard.store.evidence("abc123")[0]
        self.guard.store.add("second", "RADARR", "Second movie", "test", kind="imported")
        self.guard.store.save_manifest("second", evidence)
        self.guard.store.requested("second", self.now)
        self.guard.store.set_phase("second", "verify")
        self.qbit.torrents.return_value = [{**self.torrent, "hash": "other", "category": "manual"}]
        self.qbit.files.return_value = []
        self.qbit.files.reset_mock()
        self.guard.poll(now=self.now + 120)
        self.qbit.files.assert_called_once_with("other")
        self.assertEqual(len(self.guard.store.cleanup_retries()), 2)
        self.assertTrue(self.path.exists())

    def test_saved_paths_appear_in_cleaned_log_after_restart_without_hash(self):
        self.request()
        self.guard.store.db.close()
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr})
        with self.assertLogs("torrent-guard", level="INFO") as logs:
            self.guard.poll(now=self.now + 120)
        output = "\n".join(logs.output)
        self.assertIn("CLEANED [RADARR]", output)
        self.assertIn("name='Movie'", output)
        self.assertIn("/downloads/completed/Movie.mkv", output)
        self.assertNotIn("abc123", output)

    def test_blocked_leftover_does_not_prevent_another_absence_verification(self):
        self.request()
        manifest = self.guard.store.evidence("abc123")[0]
        for item in manifest["items"]:
            item["path"] = "already-removed/" + item["path"]
        self.guard.store.add("gone", "RADARR", "Already removed", "test", kind="imported")
        self.guard.store.save_manifest("gone", manifest)
        self.guard.store.requested("gone", self.now)
        self.guard.store.set_phase("gone", "verify")
        self.qbit.torrents.return_value = [{**self.torrent, "hash": "other", "category": "manual"}]
        self.qbit.files.return_value = []
        self.guard.poll(now=self.now + 120)
        self.assertEqual(self.guard.store.hashes(), {"abc123"})
        self.assertTrue(self.path.exists())

    def test_cleanup_retry_does_not_modify_existing_journal_in_dry_run(self):
        self.request()
        self.guard._defer_cleanup("abc123", "test failure", self.now)
        before = self.guard.store.cleanup_retries()
        self.guard.config = replace(self.cfg, dry_run=True)
        self.guard.poll(now=self.now + 1000)
        self.assertEqual(self.guard.store.cleanup_retries(), before)
        self.assertTrue(self.path.exists())

    def test_reused_hash_never_enters_local_cleanup(self):
        self.request()
        self.qbit.torrents.return_value = [{**self.torrent, "added_on": self.now + 10}]
        self.guard.poll(now=self.now + 120)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.qbit.delete_with_data.call_count, 1)

    def test_restart_retains_deadline_and_manifest(self):
        self.request()
        self.guard.store.db.close()
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr})
        self.guard.poll(now=self.now + 60)
        self.assertTrue(self.path.exists())
        self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.guard.store.entries())

    def test_nfs_unsupported_rename_uses_guarded_fallback_and_finishes(self):
        self.request()
        with patch("deletion.rename_exclusive", side_effect=OSError(errno.EINVAL, "Invalid argument")), self.assertLogs("torrent-guard", level="WARNING") as logs:
            self.guard.poll(now=self.now + 120)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(self.guard.store.entries())
        self.assertTrue(any("CLEANUP NFS compatibility" in line for line in logs.output))

    def test_nfs_fallback_interrupted_unlink_resumes_from_saved_quarantine(self):
        self.request()
        with patch("deletion.rename_exclusive", side_effect=OSError(errno.EINVAL, "unsupported flag")), patch("deletion.os.unlink", side_effect=OSError(errno.EIO, "interrupted unlink")):
            self.guard.poll(now=self.now + 120)
        manifest = self.guard.store.evidence("abc123")[0]
        moved = next(item for item in manifest["items"] if item.get("quarantine"))
        self.assertTrue((self.completed / moved["quarantine"] / "payload").is_file())
        self.guard.store.db.close()
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr})
        with patch("deletion.rename_exclusive", side_effect=OSError(errno.EINVAL, "unsupported flag")):
            self.guard.poll(now=self.now + 150)
        self.assertFalse(self.guard.store.entries())
        self.assertFalse(self.path.exists())

    def test_nfs_fallback_captures_replaced_source_without_unlinking_it(self):
        self.request()
        rename = os.rename

        def replace_before_rename(source, target, **kwargs):
            replacement = self.completed / "replacement"
            replacement.write_bytes(b"changed during NFS rename")
            os.replace(replacement, self.path)
            rename(source, target, **kwargs)

        with patch("deletion.rename_exclusive", side_effect=OSError(errno.EINVAL, "unsupported flag")), patch("deletion.os.rename", side_effect=replace_before_rename):
            self.guard.poll(now=self.now + 120)
        manifest = self.guard.store.evidence("abc123")[0]
        moved = next(item for item in manifest["items"] if item.get("quarantine"))
        self.assertEqual((self.completed / moved["quarantine"] / "payload").read_bytes(), b"changed during NFS rename")
        self.assertTrue(self.guard.store.entries())

    def test_crash_after_quarantine_resumes_without_losing_data(self):
        self.request()
        with patch("deletion.os.unlink", side_effect=OSError("simulated interrupted unlink")):
            self.guard.poll(now=self.now + 120)
        evidence = self.guard.store.evidence("abc123")[0]
        item = next(item for item in evidence["items"] if item.get("quarantine"))
        self.assertTrue((self.completed / item["quarantine"] / "payload").exists())
        self.guard.poll(now=self.now + 150)
        self.assertFalse(self.guard.store.entries())

    def test_race_captures_changed_object_without_deleting_it(self):
        from deletion import rename_exclusive
        self.request()
        def swap(source_fd, source, target_fd, target):
            replacement = self.completed / "replacement"
            replacement.write_bytes(b"replacement during rename")
            os.replace(replacement, self.path)
            rename_exclusive(source_fd, source, target_fd, target)
        with patch("deletion.rename_exclusive", side_effect=swap):
            self.guard.poll(now=self.now + 120)
        manifest = self.guard.store.evidence("abc123")[0]
        item = next(item for item in manifest["items"] if item.get("quarantine"))
        self.assertEqual((self.completed / item["quarantine"] / "payload").read_bytes(), b"replacement during rename")
        self.assertTrue(self.guard.store.entries())

    def test_traversal_cannot_reach_library_or_trigger_api_deletion(self):
        self.files.append({"name": "../library/protected.mkv", "priority": 0})
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_nested_tracked_file_is_removed_without_recursive_directory_deletion(self):
        folder = self.completed / "Movie"
        folder.mkdir()
        self.path.rename(folder / "Movie.mkv")
        self.files[0]["name"] = "Movie/Movie.mkv"
        self.torrent["content_path"] = "/downloads/completed/Movie"
        self.request()
        self.guard.poll(now=self.now + 120)
        self.assertFalse((folder / "Movie.mkv").exists())
        self.assertTrue(folder.is_dir())
        self.assertFalse(self.guard.store.entries())

    def test_path_changes_before_request_prevent_api_deletion(self):
        self.qbit.torrents.side_effect = [[self.torrent], [{**self.torrent, "save_path": "/downloads/incomplete"}]]
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_changed_paths_on_retry_preserve_original_manifest(self):
        self.guard.poll(now=self.now)
        before = self.guard.store.evidence("abc123")[0]
        self.qbit.torrents.return_value = [{**self.torrent, "download_path": "/downloads/incomplete"}]
        self.guard.poll(now=self.now + 5)
        self.assertEqual(self.qbit.delete_with_data.call_count, 1)
        self.assertEqual(self.guard.store.evidence("abc123")[0], before)

    def test_changed_file_list_on_retry_stops_api_deletion(self):
        self.guard.poll(now=self.now)
        self.files.append({"name": "new.nfo", "priority": 0})
        self.guard.poll(now=self.now + 5)
        self.assertEqual(self.qbit.delete_with_data.call_count, 1)

    def test_api_failure_after_dispatch_recovers_verification_after_restart(self):
        def ambiguous(_hash):
            self.path.unlink()
            self.sidecar.unlink()
            raise OSError("connection lost after dispatch")
        self.qbit.delete_with_data.side_effect = ambiguous
        self.guard.poll(now=self.now)
        self.assertEqual(self.guard.store.entries()[0][-2], "delete")
        self.qbit.torrents.return_value = []
        self.guard.store.db.close()
        self.guard = Guard(self.cfg, self.qbit, {"RADARR": self.arr})
        self.guard.poll(now=self.now + 30)
        self.assertFalse(self.guard.store.entries())

    def test_parent_symlink_prevents_snapshot_and_api_deletion(self):
        (self.completed / "alias").symlink_to(self.library, target_is_directory=True)
        self.files.append({"name": "alias/other.nfo", "priority": 0})
        self.guard.poll(now=self.now)
        self.qbit.delete_with_data.assert_not_called()

    def test_dry_run_never_snapshots_or_mutates_local_files(self):
        self.guard.config = replace(self.cfg, dry_run=True)
        with patch.object(self.guard.verifier, "snapshot") as snapshot:
            self.guard.poll(now=self.now)
        snapshot.assert_not_called()
        self.qbit.delete_with_data.assert_not_called()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.guard.store.entries())

    def test_snapshot_excludes_sensitive_api_fields(self):
        manifest = self.guard.verifier.snapshot({**self.torrent, "magnet_uri": "private-tracker-secret"}, self.files)
        self.assertNotIn("private-tracker-secret", json.dumps(manifest))

    def test_incomplete_suffix_is_verified_but_not_locally_removed(self):
        temporary = self.incomplete / "bad.exe.!qB"
        temporary.write_bytes(b"partial")
        manifest = self.guard.verifier.snapshot({"save_path": "/downloads/incomplete"}, [{"name": "bad.exe"}])
        remaining = self.guard.verifier.remaining(manifest)
        self.assertEqual(len(remaining), 1)
        with self.assertRaisesRegex(ValueError, "completed-download root"):
            self.guard.verifier.remove(manifest, remaining[0], Mock())
        self.assertTrue(temporary.exists())

    def test_additive_migration_retains_legacy_record_without_fake_evidence(self):
        path = self.base / "legacy.sqlite3"
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE pending(hash TEXT PRIMARY KEY, app TEXT, name TEXT, reason TEXT, queue_id INTEGER, phase TEXT)")
            database.execute("INSERT INTO pending VALUES ('old','RADARR','Movie','import',NULL,'verify')")
        store = PendingStore(str(path))
        self.addCleanup(store.db.close)
        self.assertEqual(store.entries()[0][0], "old")
        self.assertIsNone(store.evidence("old"))


class QuarantineRenameTests(unittest.TestCase):
    """Exercise compatibility boundaries using real private directories/files."""

    def setUp(self):
        from deletion import rename_to_quarantine
        self.rename = rename_to_quarantine
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'original'
        self.source.write_bytes(b'keep payload')
        self.quarantine = self.root / '.torrent-guard-test'
        self.quarantine.mkdir(mode=0o700)
        self.source_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.target_fd = os.open(self.quarantine, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.source_fd)
        self.addCleanup(os.close, self.target_fd)

    def test_supported_rename_keeps_atomic_no_replace(self):
        self.assertFalse(self.rename(self.source_fd, 'original', self.target_fd))
        self.assertFalse(self.source.exists())
        self.assertEqual((self.quarantine / 'payload').read_bytes(), b'keep payload')

    def test_unsupported_operation_codes_use_private_rename(self):
        for code in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            with self.subTest(code=code), patch('deletion.rename_exclusive', side_effect=OSError(code, 'unsupported')):
                self.assertTrue(self.rename(self.source_fd, 'original', self.target_fd))
                os.rename(self.quarantine / 'payload', self.source)

    def test_operational_errors_never_fall_back(self):
        for code in (errno.EACCES, errno.EPERM, errno.ESTALE, errno.EIO, errno.EXDEV, errno.EEXIST):
            with self.subTest(code=code), patch('deletion.rename_exclusive', side_effect=OSError(code, 'failure')), patch('deletion.os.rename') as rename:
                with self.assertRaises(OSError):
                    self.rename(self.source_fd, 'original', self.target_fd)
                rename.assert_not_called()
                self.assertTrue(self.source.exists())

    def test_existing_payload_or_symlink_is_never_overwritten(self):
        payload = self.quarantine / 'payload'
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if symlink:
                    payload.symlink_to(self.root / 'missing-target')
                else:
                    payload.write_bytes(b'existing quarantine data')
                with patch('deletion.rename_exclusive', side_effect=OSError(errno.EINVAL, 'unsupported')), patch('deletion.os.rename') as rename:
                    with self.assertRaises(FileExistsError):
                        self.rename(self.source_fd, 'original', self.target_fd)
                    rename.assert_not_called()
                self.assertTrue(self.source.exists())
                payload.unlink()

    def test_wrong_owner_or_shared_quarantine_is_retained(self):
        with patch('deletion.rename_exclusive', side_effect=OSError(errno.EINVAL, 'unsupported')), patch('deletion.os.geteuid', return_value=os.geteuid() + 1), patch('deletion.os.rename') as rename:
            with self.assertRaises(ValueError):
                self.rename(self.source_fd, 'original', self.target_fd)
            rename.assert_not_called()
        self.quarantine.chmod(0o750)
        with patch('deletion.rename_exclusive', side_effect=OSError(errno.EINVAL, 'unsupported')), patch('deletion.os.rename') as rename:
            with self.assertRaises(ValueError):
                self.rename(self.source_fd, 'original', self.target_fd)
            rename.assert_not_called()
        self.assertTrue(self.source.exists())
