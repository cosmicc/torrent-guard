"""Stale NFS handles never authorize rejection or bypass filesystem retention."""
from dataclasses import replace
from datetime import datetime, timezone
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import ArrInstance, Config, Guard, MediaValidator


def stale():
    """Use the same errno raised by a stale network-filesystem handle."""
    return OSError(errno.ESTALE, "Stale file handle")


class StaleValidationTests(unittest.TestCase):
    def setUp(self):
        context = tempfile.TemporaryDirectory()
        self.addCleanup(context.cleanup)
        self.root = Path(context.name)
        self.path = self.root / "Movie.mkv"
        self.path.write_bytes(b"downloaded payload")
        self.config = Config("http://qb", "u", "p", {}, {}, guard_download_root=str(self.root))
        self.validator = MediaValidator(self.config)
        self.torrent = {"save_path": "/downloads"}
        self.files = [{"name": "Movie.mkv", "size": 20, "progress": 1}]
        self.invalid = Mock(returncode=1, stdout="", stderr="Invalid data found when processing input")

    def test_estale_from_resolution_stat_and_open_always_defers(self):
        for operation in ("guard.Path.resolve", "guard.Path.stat", "guard.Path.open"):
            with self.subTest(operation=operation), patch(operation, side_effect=stale()), patch("guard.subprocess.run") as probe:
                decision, detail = self.validator.inspect(self.torrent, self.files)
                self.assertEqual(decision, "defer")
                self.assertIn("ESTALE", detail)
                probe.assert_not_called()

    def test_ffprobe_failure_with_lost_file_access_is_inconclusive(self):
        with patch.object(self.validator, "readable", side_effect=[None, stale()]), patch("guard.subprocess.run", return_value=self.invalid):
            decision, detail = self.validator.inspect(self.torrent, self.files)
        self.assertEqual(decision, "defer")
        self.assertIn("ESTALE", detail)

    def test_estale_during_file_read_defers_before_ffprobe(self):
        with patch("guard.Path.open") as opened, patch("guard.subprocess.run") as probe:
            opened.return_value.__enter__.return_value.read.side_effect = stale()
            decision, detail = self.validator.inspect(self.torrent, self.files)
        self.assertEqual(decision, "defer")
        self.assertIn("ESTALE", detail)
        probe.assert_not_called()

    def test_ffprobe_io_error_is_inconclusive_even_if_access_recovers(self):
        for message in ("Stale file handle", "Input/output error", "Permission denied", "Connection timed out"):
            with self.subTest(message=message), patch("guard.subprocess.run", return_value=Mock(returncode=1, stdout="", stderr=message)):
                self.assertEqual(self.validator.inspect(self.torrent, self.files)[0], "defer")

    def test_actual_invalid_media_with_readable_file_still_rejects(self):
        with patch("guard.subprocess.run", return_value=self.invalid):
            self.assertEqual(self.validator.inspect(self.torrent, self.files)[0], "reject")

    def test_unusable_probe_json_is_not_invalid_media_evidence(self):
        with patch("guard.subprocess.run", return_value=Mock(returncode=0, stdout="not JSON", stderr="")):
            self.assertEqual(self.validator.inspect(self.torrent, self.files)[0], "defer")


class FilesystemRetentionTests(unittest.TestCase):
    def setUp(self):
        context = tempfile.TemporaryDirectory()
        self.addCleanup(context.cleanup)
        root = Path(context.name)
        (root / "completed").mkdir()
        (root / "incomplete").mkdir()
        (root / "completed/Movie.mkv").write_bytes(b"video")
        instance = ArrInstance("http://arr", "dummy", "movies")
        self.config = Config("http://qb", "u", "p", {"RADARR": instance}, {"movies": "RADARR"},
                             dry_run=False, state_db=str(root / "state.sqlite3"), verify_video_content=False,
                             guard_download_root=str(root), qbit_complete_dir="/downloads/completed",
                             qbit_incomplete_dir="/downloads/incomplete")
        self.now = 1_800_000_000
        self.torrent = {"hash": "abc123", "category": "movies", "name": "Movie", "added_on": self.now - 3600,
                        "completion_on": self.now - 1800, "state": "uploading", "progress": 1,
                        "amount_left": 0, "save_path": "/downloads/completed"}
        self.files = [{"name": "Movie.mkv", "progress": 1, "size": 5}]
        self.qbit, self.arr = Mock(), Mock()
        self.qbit.torrents.return_value = [self.torrent]
        self.qbit.files.return_value = self.files
        self.arr.imported_history.return_value = {"abc123": [{
            "date": datetime.fromtimestamp(self.now - 1000, timezone.utc).isoformat(),
            "data": {"droppedPath": "/downloads/completed/Movie.mkv"}}]}
        self.guard = Guard(self.config, self.qbit, {"RADARR": self.arr})
        self.addCleanup(lambda: self.guard.store.db.close())

    def assert_retained(self):
        self.qbit.stop.assert_not_called()
        self.qbit.delete_with_data.assert_not_called()
        self.arr.blocklist_and_remove.assert_not_called()

    def test_exponential_backoff_and_logging_do_not_repeat_every_poll(self):
        with patch.object(self.guard.validator, "readable", side_effect=stale()) as access:
            with self.assertLogs("torrent-guard", level="WARNING") as logs:
                for seconds in (0, 5, 10, 25, 30, 35, 85, 90, 100, 210, 450, 750):
                    self.guard.poll(now=self.now + seconds)
            self.assertEqual(access.call_count, 6)
        delays = [30, 60, 120, 240, 300, 300]
        deferred = [line for line in logs.output if "DEFER validation" in line]
        self.assertEqual(len(deferred), len(delays))
        for line, delay in zip(deferred, delays):
            self.assertIn(f"retry_in={delay}s", line)
        self.assert_retained()

    def test_restart_and_incomplete_state_do_not_bypass_hold(self):
        with patch.object(self.guard.validator, "readable", side_effect=stale()):
            self.guard.poll(now=self.now)
        self.guard.store.db.close()
        self.guard = Guard(self.config, self.qbit, {"RADARR": self.arr})
        self.torrent.update(progress=0.5, state="downloading", amount_left=100)
        self.qbit.files.return_value = [{"name": "Fake.exe"}]
        self.guard.poll(now=self.now + 5)
        with patch.object(self.guard.validator, "check_filesystem", side_effect=stale()):
            self.guard.poll(now=self.now + 30)
        self.assert_retained()
        self.assertIn("abc123", self.guard.store.filesystem_retries())

    def test_incomplete_metadata_rejection_defers_before_any_mutation(self):
        self.torrent.update(progress=0.5, state="downloading", amount_left=100)
        self.qbit.files.return_value = [{"name": "Fake.exe"}]
        with patch.object(self.guard.validator, "check_filesystem", side_effect=stale()):
            with self.assertLogs("torrent-guard", level="WARNING") as logs:
                self.guard.poll(now=self.now)
        self.assert_retained()
        self.assertFalse(self.guard.store.entries())
        self.assertFalse(any("REJECT" in line for line in logs.output))

    def test_pending_rejection_is_held_before_stop_or_blocklist(self):
        self.guard.store.add("abc123", "RADARR", "Movie", "risky payload")
        with patch.object(self.guard.validator, "check_filesystem", side_effect=stale()):
            self.guard.poll(now=self.now)
            self.guard.poll(now=self.now + 5)
        self.assert_retained()
        self.assertEqual(self.guard.store.entries()[0][-2], "match")

    def test_actual_recovery_clears_hold_and_allows_confirmed_import_cleanup(self):
        with patch.object(self.guard.validator, "readable", side_effect=stale()):
            self.guard.poll(now=self.now)
        self.guard.poll(now=self.now + 5)
        self.assert_retained()
        self.guard.poll(now=self.now + 30)
        self.qbit.delete_with_data.assert_called_once_with("abc123")
        self.assertFalse(self.guard.store.filesystem_retries())

    def test_dry_run_keeps_retry_state_in_memory_without_journal_mutation(self):
        self.guard.config = replace(self.config, dry_run=True)
        with patch.object(self.guard.validator, "readable", side_effect=stale()):
            self.guard.poll(now=self.now)
        self.assertIn("abc123", self.guard.filesystem_retries)
        self.assertFalse(self.guard.store.filesystem_retries())
        self.assert_retained()
