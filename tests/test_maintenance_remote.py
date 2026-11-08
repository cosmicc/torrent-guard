"""Two-host maintenance protocol tests; Docker/SSH/jobs are isolated fixtures."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from maintenance import Coordinator, MaintenanceError, RemoteCoordinator, ZONE, agent_request, main


class RemoteMaintenanceTests(unittest.TestCase):
    """OMV owns scheduling/jobs; the separate agent owns container recovery."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.omv, self.agent = self.root / "omv", self.root / "docker-host"
        self.omv.mkdir()
        self.agent.mkdir()
        self.marker = self.agent / "maintenance.json"
        self.rows = {name: {"Id": name + "-id", "State": {"Running": True, "Paused": False, "Restarting": False},
                            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}}}
                     for name in ("sonarr", "radarr")}
        self.connection = {"host": "docker.example.invalid", "user": "guard-maintenance",
                           "identity_file": "/root/.ssh/guard", "known_hosts_file": "/root/.ssh/known_hosts"}
        self.calls, self.ssh, self.jobs = [], [], []
        self.returncode = 0
        self.fail_ssh = False
        self.lose_finish = False
        self.session_reply = None
        self.paths = {job: str(self.omv / job) for job in ("sync", "scrub")}
        for path in self.paths.values():
            Path(path).write_text("#!/bin/sh\nexit 0\n")
            Path(path).chmod(0o700)
        original_lstat = Path.lstat

        def root_fixture(path):
            info = original_lstat(path)
            return Mock(st_mode=info.st_mode, st_uid=0) if str(path) in self.paths.values() else info

        for target, value in (("maintenance.JOBS", self.paths), ("maintenance.Path.lstat", root_fixture),
                              ("maintenance.os.sync", Mock()),
                              ("maintenance.private_directory", lambda path: path.mkdir(exist_ok=True))):
            context = patch(target, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(Coordinator, "docker", lambda _controller, *args: self.docker(*args))
        context.start()
        self.addCleanup(context.stop)
        context = patch("maintenance.subprocess.run", side_effect=self.process)
        context.start()
        self.addCleanup(context.stop)
        self.controller = RemoteCoordinator(self.omv, self.connection)

    def docker(self, *args):
        self.calls.append(args)
        if args[:2] == ("container", "inspect"):
            return json.dumps([self.rows[args[2]]])
        if args[0] in ("start", "stop"):
            self.rows[args[-1].removesuffix("-id")]["State"]["Running"] = args[0] == "start"
        return ""

    def process(self, command, **kwargs):
        if command[0] != "ssh":
            self.jobs.append(command[0])
            self.assertFalse(any(row["State"]["Running"] for row in self.rows.values()))
            self.assertTrue(json.loads(self.marker.read_text())["active"])
            return subprocess.CompletedProcess(command, self.returncode)
        self.ssh.append(command)
        if self.fail_ssh:
            raise subprocess.CalledProcessError(255, command, stderr="private transport detail")
        result = agent_request(command[-1], self.agent, self.marker)
        if command[-1].startswith("finish ") and self.lose_finish:
            self.lose_finish = False
            raise subprocess.TimeoutExpired(command, 360)
        if self.session_reply is not None:
            result.update(self.session_reply)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result))

    def at(self, day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)

    def test_two_hosts_sunday_gap_jobs_and_independent_journals(self):
        self.controller.tick(self.at(8, 2))
        self.assertFalse(self.jobs)
        self.assertFalse((self.omv / "maintenance.json").exists())
        self.assertEqual(json.loads((self.agent / "state.json").read_text())["role"], "docker")
        self.assertEqual(json.loads((self.omv / "state.json").read_text())["role"], "omv")
        self.controller.tick(self.at(8, 2, 1))
        self.assertEqual(self.jobs, [self.paths["sync"]])
        self.controller = RemoteCoordinator(self.omv, self.connection)
        self.controller.tick(self.at(8, 4, 29))
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(any(row["State"]["Running"] for row in self.rows.values()))
        self.controller.tick(self.at(8, 4, 30))
        self.assertEqual(self.jobs, [self.paths["sync"], self.paths["scrub"]])
        self.assertFalse(self.controller.state["active"])
        self.assertFalse(json.loads(self.marker.read_text())["active"])
        self.assertTrue(all(row["State"]["Running"] for row in self.rows.values()))

    def test_ssh_failure_withholds_jobs_then_recovers_without_new_session(self):
        self.fail_ssh = True
        with self.assertRaises(MaintenanceError):
            self.controller.tick(self.at(9, 2, 1))
        session = self.controller.state["session"]
        self.assertFalse(self.jobs)
        self.fail_ssh = False
        self.controller = RemoteCoordinator(self.omv, self.connection)
        self.controller.tick(self.at(9, 2, 2))
        self.assertEqual(self.controller.state["session"], session)
        self.assertEqual(self.jobs, [self.paths["sync"]])

    def test_lost_finish_reply_retries_finish_without_restopping_or_rerunning_jobs(self):
        self.lose_finish = True
        with self.assertRaises(MaintenanceError):
            self.controller.tick(self.at(9, 2, 1))
        self.assertTrue(self.controller.state["active"])
        self.assertFalse(json.loads(self.marker.read_text())["active"])
        calls = len(self.calls)
        self.controller = RemoteCoordinator(self.omv, self.connection)
        self.controller.tick(self.at(9, 8))
        self.assertFalse(self.controller.state["active"])
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(self.calls), calls)
        self.assertTrue(self.ssh[-1][-1].startswith("finish "))

    def test_failed_omv_job_never_requests_finish(self):
        self.returncode = 1
        with self.assertRaises(MaintenanceError):
            self.controller.tick(self.at(9, 2, 1))
        with self.assertRaises(MaintenanceError):
            self.controller.tick(self.at(9, 9))
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(any(command[-1].startswith("finish ") for command in self.ssh))
        self.assertTrue(json.loads(self.marker.read_text())["active"])

    def test_invalid_acknowledgements_never_authorize_snapraid(self):
        for reply in ({"session": "0" * 32}, {"ok": False}, {"active": False}, {"active": 1}):
            self.session_reply = reply
            with self.assertRaises(MaintenanceError):
                self.controller.tick(self.at(9, 2, 1))
        self.assertFalse(self.jobs)

    def test_strict_ssh_options_and_no_docker_host_in_source_defaults(self):
        self.controller.tick(self.at(9, 2))
        command = self.ssh[0]
        self.assertIn("-oStrictHostKeyChecking=yes", command)
        self.assertIn("-oIdentitiesOnly=yes", command)
        self.assertIn("-oBatchMode=yes", command)
        self.assertIn("-oUserKnownHostsFile=/root/.ssh/known_hosts", command)
        self.assertIn("guard-maintenance@docker.example.invalid", command)

    def test_agent_rejects_injection_extra_arguments_and_arbitrary_sessions(self):
        for value in ("", "begin", "begin abc", "rm -rf /", "finish " + "a" * 32 + "; id",
                      "begin " + "a" * 32 + " --state-dir /tmp", "begin $(id)", "begin " + "A" * 32):
            with self.assertRaises((MaintenanceError, ValueError)):
                agent_request(value, self.agent, self.marker)
        self.assertFalse(self.calls)
        self.controller.tick(self.at(9, 2))
        with self.assertRaises(MaintenanceError):
            agent_request("finish " + "f" * 32, self.agent, self.marker)
        self.assertTrue(json.loads(self.marker.read_text())["active"])

    def test_completed_agent_session_cannot_be_reopened(self):
        self.controller.tick(self.at(9, 2, 1))
        session = self.controller.state["session"]
        with self.assertRaises(MaintenanceError):
            agent_request("begin " + session, self.agent, self.marker)
        self.assertTrue(all(row["State"]["Running"] for row in self.rows.values()))

    def test_connection_configuration_rejects_option_injection_and_relative_keys(self):
        for fields in ({"host": "-oProxyCommand=id"}, {"user": "root;id"}, {"port": True},
                       {"port": 0}, {"identity_file": "relative"}, {"known_hosts_file": ""}):
            with self.assertRaises(MaintenanceError):
                RemoteCoordinator(self.omv, {**self.connection, **fields})

    def test_read_only_check_does_not_stop_containers_or_create_session(self):
        result = agent_request("check " + "0" * 32, self.agent, self.marker)
        self.assertTrue(result["ok"])
        self.assertFalse(result["active"])
        self.assertFalse((self.agent / "state.json").exists())
        self.assertFalse(self.marker.exists())
        self.assertTrue(all(call[:2] == ("container", "inspect") for call in self.calls))

    def test_forced_agent_cannot_accept_extra_root_configuration_arguments(self):
        with patch("maintenance.os.geteuid", return_value=0), \
                patch("maintenance.sys.argv", ["cmd", "agent", "begin " + "a" * 32, "--state-dir", "/tmp"]), \
                patch("maintenance.sys.stderr"):
            self.assertEqual(main(), 1)
        self.assertFalse(self.calls)

    def test_failed_agent_publishes_hold_but_never_restores(self):
        self.controller.tick(self.at(8, 2))
        result = agent_request("failed " + self.controller.state["session"], self.agent, self.marker)
        self.assertTrue(result["active"])
        self.assertIn("review OMV", json.loads(self.marker.read_text())["detail"])
        self.assertFalse(any(call[0] == "start" for call in self.calls))


if __name__ == "__main__":
    unittest.main()
