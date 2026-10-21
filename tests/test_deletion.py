"""Real temporary-file tests for deletion evidence and bounded leftover cleanup."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import ArrInstance, Config, Guard, PendingStore


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
        self.guard.poll(now=self.now + 5)
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
        self.guard.poll(now=self.now + 1000)
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

    def test_crash_after_quarantine_resumes_without_losing_data(self):
        self.request()
        with patch("deletion.os.unlink", side_effect=OSError("simulated interrupted unlink")):
            self.guard.poll(now=self.now + 120)
        evidence = self.guard.store.evidence("abc123")[0]
        item = next(item for item in evidence["items"] if item.get("quarantine"))
        self.assertTrue((self.completed / item["quarantine"] / "payload").exists())
        self.guard.poll(now=self.now + 125)
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
        self.guard.poll(now=self.now + 5)
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
