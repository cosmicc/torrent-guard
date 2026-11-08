"""Real-file schedule/recovery tests with isolated Docker and SnapRAID fixtures."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import ArrInstance, Config, Guard
from maintenance import Coordinator, MaintenanceError, ZONE, atomic_json, read_maintenance


class MaintenanceTests(unittest.TestCase):
    """No test contacts Docker, Arr, production storage or real SnapRAID."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.status = self.root / "maintenance.json"
        self.controller = Coordinator(self.root, self.status)
        self.rows = {
            name: {"Id": name + "-id", "State": {"Running": True, "Paused": False, "Restarting": False},
                   "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}}}
            for name in ("sonarr", "radarr")}
        self.calls = []
        self.controller.docker = self.docker
        for job in ("sync", "scrub"):
            path = self.root / job
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
        self.addCleanup(patch.stopall)
        patch("maintenance.JOBS", {job: str(self.root / job) for job in ("sync", "scrub")}).start()
        # Temporary fixtures need not be root-owned on developer workstations.
        original_lstat = Path.lstat

        def root_fixture(path):
            info = original_lstat(path)
            if path in (self.root / "sync", self.root / "scrub"):
                return Mock(st_mode=info.st_mode, st_uid=0)
            return info

        patch("maintenance.Path.lstat", root_fixture).start()
        patch("maintenance.os.sync").start()
        self.process = patch("maintenance.subprocess.run", return_value=subprocess.CompletedProcess([], 0)).start()

    def docker(self, *args):
        self.calls.append(args)
        if args[:2] == ("container", "inspect"):
            return json.dumps([self.rows[args[2]]])
        name = args[-1].removesuffix("-id")
        if args[0] in ("start", "stop"):
            self.rows[name]["State"]["Running"] = args[0] == "start"
        return ""

    def at(self, day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)

    def tick(self, day, hour, minute=0):
        self.controller.tick(self.at(day, hour, minute))

    def reload(self):
        self.controller = Coordinator(self.root, self.status)
        self.controller.docker = self.docker

    def test_weekday_stops_at_two_then_runs_sync_and_restores_early(self):
        self.tick(9, 1, 59)
        self.assertFalse(self.calls)
        self.tick(9, 2)
        self.assertTrue(read_maintenance(self.root / "db")["active"])
        self.assertFalse(any(row["State"]["Running"] for row in self.rows.values()))
        self.process.assert_not_called()
        self.tick(9, 2, 1)
        self.process.assert_called_once_with([str(self.root / "sync")], check=False)
        self.assertFalse(self.controller.state["active"])
        self.assertTrue(all(row["State"]["Running"] for row in self.rows.values()))
        self.assertLess(self.calls.index(("update", "--restart=no", "sonarr-id")),
                        self.calls.index(("stop", "--timeout=120", "sonarr-id")))
        self.assertIn(("update", "--restart=unless-stopped", "sonarr-id"), self.calls)
        self.tick(9, 4, 30)
        self.assertEqual(self.process.call_count, 1)

    def test_sunday_holds_between_scripts_and_survives_restart(self):
        self.tick(8, 2, 1)
        self.assertTrue(self.controller.state["active"])
        self.assertEqual(self.controller.state["jobs"], {"sync": "complete", "scrub": "pending"})
        self.reload()
        self.tick(8, 4, 29)
        self.assertEqual(self.process.call_count, 1)
        self.assertFalse(any(row["State"]["Running"] for row in self.rows.values()))
        self.tick(8, 4, 30)
        self.assertEqual(self.process.call_args.args[0], [str(self.root / "scrub")])
        self.assertFalse(self.controller.state["active"])

    def test_overrun_past_six_does_not_expire_maintenance(self):
        self.tick(8, 2, 1)
        self.reload()
        self.tick(8, 8)
        self.assertEqual(self.process.call_count, 2)
        self.assertFalse(self.controller.state["active"])

    def test_live_clock_is_refreshed_after_long_sunday_sync(self):
        clock = Mock()
        clock.now.side_effect = [self.at(8, 2, 1), self.at(8, 7)]
        with patch("maintenance.datetime", clock):
            self.controller.tick()
        self.assertEqual(self.process.call_count, 2)
        self.assertFalse(self.controller.state["active"])

    def test_failed_job_keeps_apps_stopped_and_is_not_automatically_retried(self):
        self.process.return_value = subprocess.CompletedProcess([], 23)
        with self.assertRaises(MaintenanceError):
            self.tick(9, 2, 1)
        self.reload()
        with self.assertRaises(MaintenanceError):
            self.tick(9, 9)
        self.assertEqual(self.process.call_count, 1)
        self.assertTrue(self.controller.state["active"])
        self.assertFalse(any(call[0] == "start" for call in self.calls))

    def test_interrupted_job_is_held_without_starting_another_copy(self):
        self.tick(9, 2)
        self.controller.state["jobs"]["sync"] = "running"
        self.controller.save()
        self.reload()
        with self.assertRaises(MaintenanceError):
            self.tick(9, 7)
        self.process.assert_not_called()

    def test_previously_stopped_container_is_not_started(self):
        self.rows["radarr"]["State"]["Running"] = False
        self.tick(9, 2, 1)
        self.assertNotIn(("start", "radarr-id"), self.calls)
        self.assertFalse(self.rows["radarr"]["State"]["Running"])

    def test_container_replacement_blocks_job_and_restore(self):
        self.tick(9, 2)
        self.rows["sonarr"]["Id"] = "replacement-id"
        with self.assertRaises(MaintenanceError):
            self.tick(9, 2, 1)
        self.process.assert_not_called()
        self.assertTrue(self.controller.state["active"])

    def test_stop_failure_never_runs_job(self):
        self.controller.prepare(self.at(9, 2))

        def fail_stop(*args):
            if args[0] == "stop":
                raise MaintenanceError("Docker unavailable")
            return self.docker(*args)

        self.controller.docker = fail_stop
        with self.assertRaises(MaintenanceError):
            self.tick(9, 2, 1)
        self.process.assert_not_called()
        self.assertTrue(self.controller.state["active"])

    def test_failed_restore_is_retried_without_repeating_successful_job(self):
        def fail_start(*args):
            if args[0] == "start":
                raise MaintenanceError("Docker unavailable")
            return self.docker(*args)

        self.controller.docker = fail_start
        with self.assertRaises(MaintenanceError):
            self.tick(9, 2, 1)
        self.reload()
        self.tick(9, 7)
        self.assertFalse(self.controller.state["active"])
        self.assertEqual(self.process.call_count, 1)

    def test_writable_job_script_is_not_executed(self):
        (self.root / "sync").chmod(0o777)
        with self.assertRaises(MaintenanceError):
            self.tick(9, 2, 1)
        self.process.assert_not_called()
        self.assertTrue(self.controller.state["active"])

    def test_dst_skipped_two_am_is_caught_up_and_fall_back_does_not_duplicate(self):
        spring = datetime(2027, 3, 14, 3, 0, tzinfo=ZONE)
        self.controller.tick(spring)
        self.assertEqual(self.controller.state["jobs"]["sync"], "complete")
        self.assertEqual(spring.utcoffset().total_seconds(), -4 * 3600)
        self.controller.tick(datetime(2027, 3, 14, 4, 30, tzinfo=ZONE))
        self.assertEqual(self.process.call_count, 2)
        self.controller.tick(datetime(2027, 11, 7, 2, 1, tzinfo=ZONE))
        self.assertEqual(self.process.call_count, 3)
        self.controller.tick(datetime(2027, 11, 7, 2, 1, tzinfo=ZONE))
        self.assertEqual(self.process.call_count, 3)

    def test_missing_corrupt_symlink_and_oversized_status(self):
        database = self.root / "db"
        self.assertFalse(read_maintenance(database)["active"])
        for text in ('{', '[]', '{"schema":1,"active":"false"}', ' ' * 9000):
            self.status.write_text(text)
            self.assertTrue(read_maintenance(database)["active"])
        self.status.unlink()
        self.status.symlink_to(self.root / "missing")
        self.assertTrue(read_maintenance(database)["active"])

    def test_atomic_marker_replaces_symlink_without_touching_target(self):
        target = self.root / "keep"
        target.write_text("keep")
        self.status.symlink_to(target)
        atomic_json(self.status, {"schema": 1, "active": True, "detail": "Waiting for Sunday scrub"}, 0o644)
        self.assertEqual(target.read_text(), "keep")
        self.assertTrue(read_maintenance(self.root / "db")["active"])

    def test_guard_suspends_all_poll_work_and_resumes(self):
        config = Config("http://qbit", "user", "password", {}, {}, state_db=str(self.root / "guard.sqlite3"))
        client = Mock()
        client.torrents.return_value = []
        guard = Guard(config, qbit=client)
        self.addCleanup(guard.store.db.close)
        atomic_json(self.status, {"schema": 1, "active": True, "detail": "SnapRAID"}, 0o644)
        guard.poll()
        client.torrents.assert_not_called()
        atomic_json(self.status, {"schema": 1, "active": False, "detail": "Complete"}, 0o644)
        guard.poll()
        client.torrents.assert_called_once()

    def test_completed_session_repairs_stale_active_publication(self):
        self.tick(9, 2, 1)
        atomic_json(self.status, {"schema": 1, "active": True, "detail": "Old marker"}, 0o644)
        self.reload()
        self.tick(9, 7)
        self.assertFalse(read_maintenance(self.root / "db")["active"])
        self.assertEqual(self.process.call_count, 1)

    def test_diagnostics_skip_arr_requests_but_keep_qbit_available(self):
        from status_report import collect_status
        config = Config("http://qbit", "user", "password",
                        {"RADARR": ArrInstance("http://arr", "token", "movies")}, {},
                        state_db=str(self.root / "db"), qbit_complete_dir="/downloads/complete",
                        qbit_incomplete_dir="/downloads/incomplete")
        atomic_json(self.status, {"schema": 1, "active": True, "detail": "SnapRAID"}, 0o644)
        client = Mock()
        client.torrents.return_value = []
        with patch("guard.Config.from_env", return_value=config), patch("guard.Qbit", return_value=client), \
                patch("guard.Http") as http, patch("status_report.check_drive", return_value={}):
            result = collect_status()
        http.return_value.request.assert_not_called()
        client.torrents.assert_called_once()
        self.assertTrue(result["maintenance"]["active"])
        self.assertIsNone(result["services"][2]["ok"])


if __name__ == "__main__":
    unittest.main()
