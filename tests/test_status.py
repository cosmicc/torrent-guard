"""One-shot status safety, persistent event windows and real filesystem probes."""
import io
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import ApiError, ArrInstance, Config, PendingStore, main
from status_report import DiagnosticError, Report, StatusHistory, check_drive, enable_history, history_path, probe_drive, run_status, show_history


class HistoryTests(unittest.TestCase):
    """Keep exact event windows across restarts, independent of rotating logs."""

    def test_windows_restart_retention_and_recent_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            now = 2000000
            with patch("status_report.time.time", return_value=now - 90000):
                handler = StatusHistory(path)
            messages = [(now - 8 * 86400, "CLEANED old", logging.INFO),
                        (now - 20 * 3600, "CLEANED day", logging.INFO),
                        (now - 10 * 3600, "REJECT candidate movie", logging.WARNING),
                        (now - 3600, "CLEANED recent", logging.INFO),
                        (now, "API failed", logging.ERROR)]
            for at, message, level in messages:
                record = logging.makeLogRecord({"msg": message, "levelno": level, "created": at})
                handler.handle(record)
            handler.close()
            reopened = StatusHistory(path)
            output = io.StringIO()
            with patch("sys.stdout", output):
                show_history(Report(False, 100), path, now)
            text = output.getvalue()
            rows = [row.split() for row in text.splitlines() if row.startswith((" 6h", "12h", "24h"))]
            self.assertEqual(rows, [["6h", "1", "0", "0", "1"], ["12h", "1", "1", "1", "1"], ["24h", "2", "1", "1", "1"]])
            self.assertIn("API failed", text)
            self.assertNotIn("partial", text)
            self.assertEqual(reopened.database.execute("SELECT count(*) FROM events WHERE at < ?", (now - 7 * 86400,)).fetchone()[0], 0)
            reopened.close()

    def test_partial_history_snapshot_redaction_and_issue_limits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RADARR_API_KEY": "secret-token"}):
            path = Path(directory) / "history.sqlite3"
            now = 2000000
            with patch("status_report.time.time", return_value=now):
                handler = StatusHistory(path)
            handler.handle(logging.makeLogRecord({"msg": "bad secret-token \x1b[2J", "levelno": logging.ERROR, "created": now}))
            for index in range(30):
                handler.handle(logging.makeLogRecord({"msg": f"warning {index}", "levelno": logging.WARNING, "created": now}))
            handler.handle(logging.makeLogRecord({"msg": "STATUS pending_deletion=2 import_grace=1", "levelno": logging.INFO, "created": now}))
            handler.close()
            output = io.StringIO()
            with patch("sys.stdout", output):
                show_history(Report(False, 90), path, now)
            text = output.getvalue()
            self.assertEqual(text.count("partial"), 3)
            self.assertIn("pending_deletion: 2", text)
            self.assertIn("import_grace: 1", text)
            self.assertIn("[redacted]", text)
            self.assertNotIn("secret-token", text)
            self.assertNotIn("\x1b", text)
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute("SELECT count(*) FROM issues").fetchone()[0], 21)

    def test_history_failure_does_not_interrupt_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = StatusHistory(Path(directory) / "history.sqlite3")
            handler.database.close()
            with self.assertLogs("torrent-guard", level="ERROR") as logs:
                handler.emit(logging.makeLogRecord({"msg": "CLEANED example", "levelno": logging.INFO}))
            self.assertIn("monitoring counts may be incomplete", logs.output[0])
            handler.close()

    def test_exact_rolling_boundary_and_future_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            now = 2000000
            with patch("status_report.time.time", return_value=now - 90000):
                handler = StatusHistory(path)
            for at in (now - 6 * 3600 - 0.01, now - 6 * 3600, now + 1):
                handler.handle(logging.makeLogRecord({"msg": "CLEANED test", "levelno": logging.INFO, "created": at}))
            handler.close()
            output = io.StringIO()
            with patch("sys.stdout", output):
                show_history(Report(False, 100), path, now)
            row = next(line for line in output.getvalue().splitlines() if line.startswith(" 6h"))
            self.assertEqual(row.split(), ["6h", "1", "0", "0", "0"])

    def test_warning_output_threshold_still_collects_info_statistics(self):
        logger = logging.getLogger("torrent-guard")
        handlers, level = logger.handlers[:], logger.level
        logger.handlers.clear()
        stream = io.StringIO()
        console = logging.StreamHandler(stream)
        logger.addHandler(console)
        logger.setLevel(logging.WARNING)
        try:
            with tempfile.TemporaryDirectory() as directory:
                journal = str(Path(directory) / "journal.sqlite3")
                enable_history(journal)
                logger.info("CLEANED example")
                logger.info("STATUS pending_deletion=0")
                self.assertEqual(stream.getvalue(), "")
                with sqlite3.connect(history_path(journal)) as database:
                    self.assertEqual(database.execute("SELECT count(*) FROM events WHERE metric='cleaned'").fetchone()[0], 1)
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers[:] = handlers
            logger.setLevel(level)


class DriveTests(unittest.TestCase):
    """Write tests only touch unique probe files inside authorized directories."""

    def test_real_write_read_remove_and_readonly_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            payload = path / "movie.mkv"
            payload.write_bytes(b"media")
            self.assertIn("not tested", probe_drive(path))
            self.assertIn("create/write/fsync/read/remove OK", probe_drive(path, True, path))
            self.assertEqual(list(path.iterdir()), [payload])
            self.assertEqual(payload.read_bytes(), b"media")
            self.assertIn("read/list OK", check_drive(path))

    def test_mismatched_cleanup_alias_and_symlink_never_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete, wrong = root / "complete", root / "wrong"
            complete.mkdir()
            wrong.mkdir()
            link = root / "link"
            link.symlink_to(complete, target_is_directory=True)
            for path, expected in ((wrong, complete), (link, complete)):
                with self.assertRaises((DiagnosticError, ValueError, OSError)):
                    probe_drive(path, True, expected)
            self.assertEqual(list(complete.iterdir()), [])
            self.assertEqual(list(wrong.iterdir()), [])

    def test_failed_write_removes_only_its_test_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "video.mkv"
            payload.write_bytes(b"video")
            with patch("status_report.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    probe_drive(root, write=True)
            self.assertEqual(list(root.iterdir()), [payload])

    def test_replaced_probe_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def replace_probe(descriptor):
                probe = next(root.glob('.torrent-guard-status-*'))
                probe.unlink()
                probe.write_bytes(b'replacement')
            with patch("status_report.os.fsync", side_effect=replace_probe):
                with self.assertRaisesRegex(DiagnosticError, "replaced"):
                    probe_drive(root, write=True)
            remaining = list(root.iterdir())
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].read_bytes(), b'replacement')


class StatusTests(unittest.TestCase):
    """Diagnostics cannot enter daemon polling, deletion or journal migration."""

    def test_cli_status_bypasses_logging_heartbeat_and_guard(self):
        with patch("sys.argv", ["guard.py", "--status", "--color", "never"]), patch("status_report.run_status", return_value=0) as status, patch("guard.setup_logging") as logs, patch("guard.Guard") as daemon, patch("guard.HEARTBEAT_FILE") as marker:
            self.assertEqual(main(), 0)
            status.assert_called_once_with("never", None)
            logs.assert_not_called()
            daemon.assert_not_called()
            marker.unlink.assert_not_called()

    def test_service_failure_preserves_other_results_and_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "journal.sqlite3"
            store = PendingStore(str(database))
            store.add("hash", "RADARR", "Movie", "reason")
            store.db.close()
            before = database.read_bytes()
            handler = StatusHistory(history_path(str(database)))
            handler.close()
            config = Config("http://qbit", "user", "password", {"RADARR": ArrInstance("http://arr", "token", "movies")}, {"movies": "RADARR"},
                            state_db=str(database), qbit_complete_dir="/downloads/complete", qbit_incomplete_dir="/downloads/incomplete")
            client = Mock()
            client.torrents.return_value = [{"hash": "a", "category": "movies", "state": "downloading"}]
            output = io.StringIO()
            with patch("guard.Config.from_env", return_value=config), patch("guard.Qbit", return_value=client), patch("guard.Http") as http, patch("status_report.check_drive", return_value="read/list OK") as drives, patch("guard.healthcheck", return_value=1), patch("sys.stdout", output):
                http.return_value.request.side_effect = ApiError("secret-token response body", 401)
                self.assertEqual(run_status("never", 80), 1)
            text = output.getvalue()
            self.assertIn("managed=1", text)
            self.assertIn("SONARR: not configured", text)
            self.assertIn("RADARR: HTTP 401", text)
            self.assertIn("pending=1", text)
            self.assertIn("6h partial", text)
            self.assertNotIn("secret-token", text)
            self.assertEqual(database.read_bytes(), before)
            client.stop.assert_not_called()
            client.delete_with_data.assert_not_called()
            self.assertEqual([call.kwargs.get("write", False) for call in drives.call_args_list], [False, False, False, True])
            http.return_value.request.assert_called_once_with("GET", "http://arr/api/v3/system/status", headers={"X-Api-Key": "token"})

    def test_missing_databases_are_not_created_and_report_keeps_running(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            report = Report(False, 80)
            with patch("sys.stdout", io.StringIO()):
                report.check("history", lambda: show_history(report, path, 1000))
            self.assertTrue(report.failed)
            self.assertFalse(path.exists())

    def test_success_with_enabled_cleanup_and_both_arr_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.sqlite3"
            store = PendingStore(str(journal))
            store.db.close()
            handler = StatusHistory(history_path(str(journal)))
            handler.close()
            config = Config("http://qbit", "user", "password",
                            {app: ArrInstance("http://" + app.lower(), "key", app.lower()) for app in ("SONARR", "RADARR")},
                            {"sonarr": "SONARR", "radarr": "RADARR"}, state_db=str(journal),
                            cleanup_leftovers=True, qbit_complete_dir="/downloads/complete", qbit_incomplete_dir="/downloads/incomplete")
            client = Mock()
            client.torrents.return_value = []
            marker = Mock()
            marker.read_text.return_value = "100"
            with patch("guard.Config.from_env", return_value=config), patch("guard.Qbit", return_value=client), patch("guard.Http") as http, patch("guard.HEARTBEAT_FILE", marker), patch("guard.healthcheck", return_value=0), patch("status_report.time.monotonic", return_value=101), patch("status_report.check_drive", return_value="OK") as drives, patch("sys.stdout", io.StringIO()):
                http.return_value.request.return_value = {"version": "1.2.3"}
                self.assertEqual(run_status("never", 80), 0)
            self.assertEqual(http.return_value.request.call_count, 2)
            self.assertEqual(drives.call_args_list[-1].kwargs, {"write": True, "match": Path("/downloads/complete")})
            self.assertEqual(drives.call_args_list[-1].args, (Path("/cleanup/completed"),))
            client.stop.assert_not_called()
            client.delete_with_data.assert_not_called()

    def test_terminal_controls_escaping_and_width(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            Report(True, 30).line("x\x1b[2J " + "long/path/" * 20, "96")
        text = output.getvalue()
        self.assertNotIn("\x1b[2J", text)
        self.assertIn("\x1b[96m", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line.removeprefix("\x1b[96m").removesuffix("\x1b[0m")), 30)
