"""Persistent status history, isolated diagnostics and restricted read-only HTTP."""
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from guard import ApiError, ArrInstance, Config, PendingStore, main
from status_report import DiagnosticError, StatusHistory, check_drive, collect_status, enable_history, history_data, history_path, probe_drive
from status_web import StatusCache, StatusServer, WebSettings, polling_status, start_status_server


class HistoryTests(unittest.TestCase):
    """History survives restarts, rotation and the additive v1.6 upgrade."""

    def test_windows_restart_retention_and_exact_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            now = 2000000
            with patch("status_report.time.time", return_value=now - 90000):
                handler = StatusHistory(path)
            messages = [(now - 8 * 86400, "CLEANED old", logging.INFO),
                        (now - 20 * 3600, "CLEANED day", logging.INFO),
                        (now - 10 * 3600, "REJECT candidate movie", logging.WARNING),
                        (now - 6 * 3600 - .01, "CLEANED before", logging.INFO),
                        (now - 6 * 3600, "CLEANED boundary", logging.INFO),
                        (now, "API failed", logging.ERROR),
                        (now + 1, "CLEANED future", logging.INFO)]
            for at, message, level in messages:
                handler.handle(logging.makeLogRecord({"msg": message, "levelno": level, "created": at}))
            handler.close()
            reopened = StatusHistory(path)
            data = history_data(path, now)
            self.assertEqual(data["windows"]["6"]["cleaned"], 1)
            self.assertEqual(data["windows"]["12"]["cleaned"], 2)
            self.assertEqual(data["windows"]["24"]["cleaned"], 3)
            self.assertEqual(data["windows"]["12"]["rejected"], 1)
            self.assertEqual(data["windows"]["12"]["warnings"], 1)
            self.assertEqual(data["windows"]["6"]["errors"], 1)
            self.assertFalse(data["windows"]["24"]["partial"])
            self.assertEqual(reopened.database.execute("SELECT count(*) FROM events WHERE at < ?", (now - 7 * 86400,)).fetchone()[0], 0)
            reopened.close()

    def test_partial_snapshot_activity_redaction_and_independent_issue_limits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RADARR_API_KEY": "secret-token"}):
            path = Path(directory) / "history.sqlite3"
            now = 2000000
            with patch("status_report.time.time", return_value=now):
                handler = StatusHistory(path)
            messages = [("bad secret-token \x1b[2J", logging.ERROR),
                        ("ACCEPT completed [RADARR] name='Movie' paths='/downloads/complete/Movie.mkv'", logging.INFO),
                        ("STATUS pending_deletion=2 import_grace=1", logging.INFO)]
            for message, level in messages:
                handler.handle(logging.makeLogRecord({"msg": message, "levelno": level, "created": now}))
            for index in range(30):
                handler.handle(logging.makeLogRecord({"msg": f"warning {index}", "levelno": logging.WARNING, "created": now}))
            handler.close()
            data = history_data(path, now)
            self.assertTrue(all(window["partial"] for window in data["windows"].values()))
            self.assertEqual(data["snapshot"]["counts"]["pending_deletion"], "2")
            self.assertEqual(data["activity"][0]["name"], "Movie")
            self.assertEqual(data["activity"][0]["decision"], "Validated")
            self.assertIn("[redacted]", json.dumps(data))
            self.assertNotIn("secret-token", json.dumps(data))
            self.assertNotIn("\x1b", json.dumps(data))
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute("SELECT count(*) FROM issues").fetchone()[0], 21)

    def test_old_history_upgrade_preserves_existing_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            handler = StatusHistory(path)
            handler.database.execute("DROP TABLE activity")
            handler.database.execute("INSERT INTO events VALUES (?, 'cleaned')", (time.time(),))
            handler.database.commit()
            handler.close()
            self.assertEqual(history_data(path, time.time())["activity"], [])
            reopened = StatusHistory(path)
            self.assertEqual(history_data(path, time.time())["windows"]["6"]["cleaned"], 1)
            reopened.close()

    def test_history_failure_does_not_interrupt_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = StatusHistory(Path(directory) / "history.sqlite3")
            handler.database.close()
            with self.assertLogs("torrent-guard", level="ERROR") as logs:
                handler.emit(logging.makeLogRecord({"msg": "CLEANED example", "levelno": logging.INFO}))
            self.assertIn("monitoring counts may be incomplete", logs.output[0])
            handler.close()

    def test_warning_output_threshold_still_collects_info_statistics(self):
        logger = logging.getLogger("torrent-guard")
        handlers, level = logger.handlers[:], logger.level
        logger.handlers.clear()
        console = Mock(spec=logging.Handler, level=logging.WARNING)
        logger.addHandler(console)
        logger.setLevel(logging.WARNING)
        try:
            with tempfile.TemporaryDirectory() as directory:
                journal = str(Path(directory) / "journal.sqlite3")
                enable_history(journal)
                logger.info("CLEANED example")
                logger.info("STATUS pending_deletion=0")
                console.handle.assert_not_called()
                self.assertEqual(history_data(history_path(journal), time.time())["windows"]["6"]["cleaned"], 1)
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers[:] = handlers
            logger.setLevel(level)


class DriveTests(unittest.TestCase):
    """Only exclusively created probe files may be removed during diagnostics."""

    def test_real_read_and_write_checks_preserve_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            payload = path / "movie.mkv"
            payload.write_bytes(b"media")
            self.assertFalse(probe_drive(path)["write_tested"])
            data = probe_drive(path, True, path)
            self.assertTrue(data["write_tested"])
            self.assertGreater(data["total_bytes"], 0)
            self.assertEqual(list(path.iterdir()), [payload])
            self.assertEqual(payload.read_bytes(), b"media")
            self.assertTrue(check_drive(path)["readable"])

    def test_mismatched_alias_and_symlinks_never_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete, wrong = root / "complete", root / "wrong"
            complete.mkdir()
            wrong.mkdir()
            link = root / "link"
            link.symlink_to(complete, target_is_directory=True)
            for path in (wrong, link):
                with self.assertRaises((DiagnosticError, ValueError, OSError)):
                    probe_drive(path, True, complete)
            self.assertEqual(list(complete.iterdir()), [])
            self.assertEqual(list(wrong.iterdir()), [])

    def test_failed_probe_removes_only_its_own_file(self):
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
                probe = next(root.glob(".torrent-guard-status-*"))
                probe.unlink()
                probe.write_bytes(b"replacement")
            with patch("status_report.os.fsync", side_effect=replace_probe):
                with self.assertRaisesRegex(DiagnosticError, "replaced"):
                    probe_drive(root, write=True)
            self.assertEqual(next(root.iterdir()).read_bytes(), b"replacement")


class CollectionTests(unittest.TestCase):
    """Service failures remain isolated and collectors cannot delete downloads."""

    def test_connection_failure_preserves_other_results_and_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.sqlite3"
            store = PendingStore(str(journal))
            store.add("hash", "RADARR", "Movie", "reason")
            store.set_filesystem_retry("hash", 1, 2000000, "waiting for access")
            store.db.close()
            before = journal.read_bytes()
            handler = StatusHistory(history_path(str(journal)))
            handler.close()
            config = Config("http://qbit", "user", "password", {"RADARR": ArrInstance("http://arr", "token", "movies")}, {"movies": "RADARR"},
                            state_db=str(journal), qbit_complete_dir="/downloads/complete", qbit_incomplete_dir="/downloads/incomplete")
            client = Mock()
            client.torrents.return_value = [{"hash": "hash", "name": "Movie", "category": "movies", "state": "downloading"}]
            with patch("guard.Config.from_env", return_value=config), patch("guard.Qbit", return_value=client), patch("guard.Http") as http, patch("status_report.check_drive", return_value={"readable": True}) as drives:
                http.return_value.request.side_effect = ApiError("secret-token response body", 401)
                data = collect_status()
            self.assertEqual(data["services"][0]["managed"], 1)
            self.assertFalse(data["services"][1]["configured"])
            self.assertEqual(data["services"][2]["detail"], "HTTP 401")
            self.assertEqual(data["journal"]["counts"]["pending"], 1)
            self.assertEqual(data["journal"]["holds"][0]["name"], "Movie")
            self.assertNotIn("secret-token", json.dumps(data))
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual([call.args[1] for call in drives.call_args_list], [False, False, False, True])
            client.stop.assert_not_called()
            client.delete_with_data.assert_not_called()
            http.return_value.request.assert_called_once_with("GET", "http://arr/api/v3/system/status", headers={"X-Api-Key": "token"})

    def test_missing_history_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                history_data(path, time.time())
            self.assertFalse(path.exists())

    def test_collection_timeout_preserves_old_snapshot_and_staleness(self):
        cache = StatusCache()
        cache.snapshot = {"collected_at": time.time() - 200, "version": "1.7"}
        with patch("status_web.subprocess.run", side_effect=subprocess.TimeoutExpired("collector", 120)):
            cache.collect_once()
        with patch("status_web.polling_status", return_value={"healthy": False}):
            data = cache.read()
        self.assertTrue(data["stale"])
        self.assertTrue(data["ready"])
        self.assertIn("unavailable", data["collection_error"])
        self.assertEqual(data["version"], "1.7")

    def test_polling_liveness_is_read_only_and_rejects_invalid_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat"
            with patch("guard.HEARTBEAT_FILE", path):
                self.assertFalse(polling_status()["healthy"])
                for value in ("nan", "inf", "-inf", "broken"):
                    path.write_text(value)
                    self.assertIsNone(polling_status()["age_seconds"])
                    self.assertEqual(path.read_text(), value)


class HTTPTests(unittest.TestCase):
    """Real HTTP exercises explicit routes, security headers and proxy restrictions."""

    def setUp(self):
        self.cache = StatusCache()
        self.server = StatusServer(("127.0.0.1", 0), self.cache, (ipaddress.ip_network("127.0.0.1/32"),))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_routes_headers_and_no_request_triggered_collection(self):
        with patch.object(self.cache, "collect_once") as collect:
            for route in ("/", "/app.css", "/app.js", "/icon.svg", "/api/status"):
                code, headers, body = self.request(route)
                self.assertEqual(code, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("Python", headers["Server"])
                self.assertTrue(body)
            collect.assert_not_called()
        self.assertFalse(json.loads(self.request("/api/status")[2])["ready"])
        self.assertEqual(self.request("/api/status", "HEAD")[2], b"")

    def test_forwarded_headers_cannot_bypass_source_allowlist(self):
        self.server.allowed_clients = (ipaddress.ip_network("192.0.2.1/32"),)
        for path in ("/", "/api/status", "/app.js"):
            self.assertEqual(self.request(path, headers={"X-Forwarded-For": "192.0.2.1"})[0], 403)

    def test_no_mutations_or_filesystem_routes(self):
        for path in ("/.env", "/../guard.py", "/%2e%2e/guard.py", "/delete", "/state/torrent-guard.sqlite3"):
            self.assertEqual(self.request(path)[0], 404)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request("/api/status", method)[0], 501)

    def test_invalid_settings_and_disabled_server(self):
        for raw in ("", "0.0.0.0/0", "::/0", "not-an-ip"):
            with patch.dict(os.environ, {"STATUS_ALLOWED_CLIENTS": raw}):
                with self.assertRaises(ValueError):
                    WebSettings.from_env()
        with patch.dict(os.environ, {"STATUS_ENABLED": "false", "STATUS_ALLOWED_CLIENTS": "127.0.0.1/32"}):
            self.assertIsNone(start_status_server())

    def test_cli_removed_healthcheck_still_needs_no_web_or_credentials(self):
        with patch("sys.argv", ["guard.py", "--healthcheck"]), patch("guard.healthcheck", return_value=0), patch("guard.Config.from_env") as config, patch("status_web.start_status_server") as start:
            self.assertEqual(main(), 0)
            config.assert_not_called()
            start.assert_not_called()
