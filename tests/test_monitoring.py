"""Console color isolation and completed-poll Docker healthcheck regression tests."""
import io
import logging
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from guard import ConsoleFormatter, LOG, display_time, healthcheck, import_confirmed, main, setup_logging, write_heartbeat


class EasternTimeTests(unittest.TestCase):
    """Display Eastern offsets accurately without changing elapsed-time safety."""

    def test_standard_daylight_and_repeated_fall_hour(self):
        cases = [
            ("2026-01-15T12:00:00+00:00", "2026-01-15 07:00:00 EST-0500"),
            ("2026-07-15T12:00:00+00:00", "2026-07-15 08:00:00 EDT-0400"),
            ("2026-11-01T05:30:00+00:00", "2026-11-01 01:30:00 EDT-0400"),
            ("2026-11-01T06:30:00+00:00", "2026-11-01 01:30:00 EST-0500"),
            ("2026-03-08T06:59:59+00:00", "2026-03-08 01:59:59 EST-0500"),
            ("2026-03-08T07:00:00+00:00", "2026-03-08 03:00:00 EDT-0400"),
        ]
        for stamp, expected in cases:
            with self.subTest(stamp=stamp):
                epoch = datetime.fromisoformat(stamp).timestamp()
                self.assertEqual(display_time(epoch), expected)
                record = logging.LogRecord("torrent-guard", logging.INFO, "", 0, "STATUS pending_deletion=0", (), None)
                record.created = epoch
                self.assertTrue(ConsoleFormatter(False).format(record).startswith(expected + " INFO"))

    def test_import_grace_spans_dst_without_early_authorization(self):
        imported = datetime(2026, 11, 1, 5, 55, tzinfo=timezone.utc).timestamp()
        torrent = {"added_on": imported - 3600}
        files = [{"name": "Movie.mkv"}]
        events = [{"date": "2026-11-01T05:55:00Z", "data": {}}]
        confirmed, reason = import_confirmed(torrent, files, events, imported + 599, 10)
        self.assertFalse(confirmed)
        self.assertIn("2026-11-01 01:05:00 EST-0500", reason)
        self.assertTrue(import_confirmed(torrent, files, events, imported + 600, 10)[0])


class ColorLoggingTests(unittest.TestCase):
    """Keep decision colors on the console, with safe plain audit output."""

    def test_bright_palette_decision_mapping(self):
        cases = [("ACCEPT completed", logging.INFO, "92"),
                 ("CLEANED movie", logging.INFO, "92"),
                 ("HEARTBEAT polling active", logging.INFO, "96"),
                 ("WAIT import", logging.DEBUG, "93"),
                 ("PENDING stopped", logging.WARNING, "93"),
                 ("REJECT candidate", logging.WARNING, "91"),
                 ("DRY RUN: would delete", logging.WARNING, "95"),
                 ("Import history failed", logging.ERROR, "91")]
        for message, level, code in cases:
            with self.subTest(message=message):
                record = logging.makeLogRecord({"msg": message, "levelno": level})
                colored = ConsoleFormatter(True).format(record)
                plain = ConsoleFormatter(False).format(record)
                self.assertTrue(colored.startswith(f"\033[{code}m"))
                self.assertTrue(colored.endswith("\033[0m"))
                self.assertNotIn("\033", plain)
                self.assertIn(message, plain)

    def test_external_control_codes_cannot_control_terminal(self):
        record = logging.makeLogRecord({"msg": "Title \033[2J\x9b31m", "levelno": logging.INFO})
        result = ConsoleFormatter(False).format(record)
        self.assertNotIn("\033", result)
        self.assertNotIn("\x9b", result)
        self.assertIn("\\x1b", result)

    def test_color_modes_follow_terminal_only_in_auto(self):
        for mode, terminal, colored in (("always", False, True), ("auto", False, False),
                                        ("auto", True, True), ("never", True, False)):
            with self.subTest(mode=mode, terminal=terminal):
                handlers, level = LOG.handlers[:], LOG.level
                LOG.handlers.clear()
                stream = io.StringIO()
                try:
                    with patch.dict(os.environ, {"LOG_FILE": "", "LOG_COLOR": mode}), patch("sys.stderr", stream), patch.object(stream, "isatty", return_value=terminal):
                        setup_logging()
                        LOG.info("HEARTBEAT polling active")
                    self.assertEqual("\033[96m" in stream.getvalue(), colored)
                finally:
                    for handler in LOG.handlers:
                        handler.close()
                    LOG.handlers[:] = handlers
                    LOG.setLevel(level)

    def test_file_stays_plain_when_console_colors_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "audit.log"
            handlers, level = LOG.handlers[:], LOG.level
            LOG.handlers.clear()
            stream = io.StringIO()
            try:
                with patch.dict(os.environ, {"LOG_FILE": str(filename), "LOG_COLOR": "always"}), patch("sys.stderr", stream):
                    setup_logging()
                    LOG.info("ACCEPT completed")
                self.assertIn("\033[92m", stream.getvalue())
                self.assertNotIn("\033", filename.read_text())
                self.assertIn("ACCEPT completed", filename.read_text())
            finally:
                for handler in LOG.handlers:
                    handler.close()
                LOG.handlers[:] = handlers
                LOG.setLevel(level)


class HealthcheckTests(unittest.TestCase):
    """Health depends on recent work completion rather than process existence."""

    def test_recent_idle_poll_is_healthy_then_stale_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HEALTHCHECK_MAX_AGE_SECONDS": "300"}):
            path = Path(directory) / "heartbeat"
            self.assertEqual(healthcheck(path), 1)
            with patch("guard.time.monotonic", return_value=1000):
                write_heartbeat(path)
                self.assertEqual(healthcheck(path), 0)
            with patch("guard.time.monotonic", return_value=1301):
                self.assertEqual(healthcheck(path), 1)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_invalid_or_future_marker_and_invalid_threshold_are_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory, patch("guard.time.monotonic", return_value=1000):
            path = Path(directory) / "heartbeat"
            for stamp in ("nan", "inf", "1001", "broken"):
                with self.subTest(stamp=stamp):
                    path.write_text(stamp)
                    self.assertEqual(healthcheck(path), 1)
            path.write_text("1000")
            for maximum in ("0", "-1", "nan", "inf", "bad"):
                with self.subTest(maximum=maximum), patch.dict(os.environ, {"HEALTHCHECK_MAX_AGE_SECONDS": maximum}):
                    self.assertEqual(healthcheck(path), 1)

    def test_healthcheck_cli_needs_no_credentials_or_logging(self):
        with patch("sys.argv", ["guard.py", "--healthcheck"]), patch("guard.healthcheck", return_value=1), patch("guard.setup_logging") as logging_setup, patch("guard.Config.from_env") as config:
            self.assertEqual(main(), 1)
            config.assert_not_called()
            logging_setup.assert_not_called()

    def test_only_completed_polls_advance_heartbeat(self):
        for error in (None, OSError("qBittorrent unreachable")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "heartbeat"
                marker.write_text("old process marker")
                guard = Mock()
                guard.poll.side_effect = error
                guard.store.hashes.return_value = set()
                config = Mock(poll_seconds=5, dry_run=True)
                with patch("sys.argv", ["guard.py"]), patch("guard.HEARTBEAT_FILE", marker), patch("guard.setup_logging"), patch("guard.Config.from_env", return_value=config), patch("guard.Guard", return_value=guard), patch("guard.write_heartbeat") as heartbeat, patch("guard.time.sleep", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        main()
                self.assertFalse(marker.exists())
                if error is None:
                    heartbeat.assert_called_once_with()
                else:
                    heartbeat.assert_not_called()
                self.assertEqual(guard.log_status.call_count, 1 if error is None else 0)

    def test_status_and_debug_heartbeat_run_every_sixty_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = Mock()
            guard.store.hashes.return_value = set()
            config = Mock(poll_seconds=5, dry_run=True, import_grace_minutes=10,
                          delete_settle_seconds=120)
            with patch("sys.argv", ["guard.py"]), patch("guard.HEARTBEAT_FILE", Path(directory) / "marker"), patch("guard.setup_logging"), patch("guard.Config.from_env", return_value=config), patch("guard.Guard", return_value=guard), patch("guard.write_heartbeat"), patch("guard.time.monotonic", side_effect=[0, 30, 61, 61]), patch("guard.time.sleep", side_effect=[None, None, KeyboardInterrupt]), self.assertLogs("torrent-guard", level="DEBUG") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    main()
            self.assertEqual(guard.log_status.call_count, 2)
            heartbeat = [record for record in logs.records if record.getMessage().startswith("HEARTBEAT")]
            self.assertEqual(len(heartbeat), 2)
            self.assertTrue(all(record.levelno == logging.DEBUG for record in heartbeat))
