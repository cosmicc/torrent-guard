#!/usr/bin/env python3
"""OMV SnapRAID coordinator, restricted Docker-host agent and Guard status.

Install this root-owned command on both hosts; never mount Docker's socket in
Guard. OMV runs jobs only after SSH confirms the Docker-host hold. The daemon
only calls read_maintenance(). Each host preserves its own durable journal.
"""
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("America/New_York")
CONTAINERS = ("sonarr", "radarr")
JOBS = {"sync": "/usr/local/sbin/snapraid-guarded-sync",
        "scrub": "/usr/local/sbin/snapraid-guarded-scrub"}
DEFAULT_STATUS = Path("/config/torrent-guard/maintenance.json")
REMOTE_CONFIG = Path("/etc/torrent-guard-maintenance.json")
AGENT_DIRECTORY = Path("/var/lib/torrent-guard-maintenance-agent")
AGENT_CONFIG = Path("/etc/torrent-guard-maintenance-agent.json")


def read_maintenance(state_db: str | Path) -> dict:
    """Read bounded host-published status; an unreadable marker suspends Guard."""
    path = Path(state_db).parent / "maintenance.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not a regular file")
            raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("oversized maintenance status")
            data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("schema") != 1
                or type(data.get("active")) is not bool
                or not isinstance(data.get("detail"), str)
                or len(data["detail"]) > 1000):
            raise ValueError("invalid maintenance status")
        return {"active": data["active"], "detail": data["detail"],
                "updated_at": data.get("updated_at"), "timezone": "America/New_York"}
    except FileNotFoundError:
        return {"active": False, "detail": "Maintenance coordinator not installed"}
    except (OSError, ValueError, RecursionError):
        return {"active": True, "detail": "Maintenance status unreadable; processing suspended for safety"}


def atomic_json(path: Path, data: dict, mode: int) -> None:
    """Publish durable JSON using an exclusive temporary file and atomic replace."""
    # Anchor writes to the opened directory; never follow a destination symlink.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = ".maintenance-" + os.urandom(16).hex()
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


class MaintenanceError(RuntimeError):
    """An unsafe or failed operation that must keep maintenance active."""


class Coordinator:
    """Run daily sync and Sunday scrub without overlapping media imports."""

    def __init__(self, directory: Path, status_file: Path):
        self.directory = directory
        self.path = directory / "state.json"
        self.status_file = status_file
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None
        if self.state is not None and (
                not isinstance(self.state, dict) or self.state.get("schema") != 1
                or type(self.state.get("active")) is not bool
                or not isinstance(self.state.get("jobs"), dict)
                or not isinstance(self.state.get("containers"), dict)
                or set(self.state["containers"]) != (set() if self.state.get("role") == "omv" else set(CONTAINERS))
                or not isinstance(self.state.get("date"), str)
                or not isinstance(self.state.get("detail"), str)
                or (self.state.get("role") != "docker" and "sync" not in self.state["jobs"])
                or not set(self.state["jobs"]).issubset(JOBS)
                or any(phase not in ("pending", "running", "complete", "failed")
                       for phase in self.state["jobs"].values())):
            raise ValueError("Invalid maintenance journal; preserve it for operator recovery")

    def save(self) -> None:
        """Save private recovery intent before publishing browser-readable status."""
        atomic_json(self.path, self.state, 0o600)
        atomic_json(self.status_file, {
            "schema": 1, "active": self.state["active"],
            "detail": self.state["detail"],
            "updated_at": time.time(),
        }, 0o644)

    def docker(self, *arguments: str) -> str:
        """Use fixed Docker argv without shell expansion or exposing API secrets."""
        try:
            result = subprocess.run(["docker", *arguments], check=True, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150)
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise MaintenanceError("Docker operation failed: " + arguments[0]) from exc

    def inspect(self, name: str) -> dict:
        """Require one known container and explicit running/paused status."""
        try:
            rows = json.loads(self.docker("container", "inspect", name))
            row, = rows
            if (not isinstance(row["Id"], str) or not row["Id"]
                    or any(type(row["State"][key]) is not bool
                           for key in ("Running", "Paused", "Restarting"))):
                raise ValueError("invalid container state")
            if row["State"]["Paused"] or row["State"]["Restarting"]:
                raise MaintenanceError(name + " is paused or restarting; operator review required")
            return row
        except (ValueError, KeyError, TypeError) as exc:
            raise MaintenanceError("Cannot establish container state: " + name) from exc

    def prepare(self, now: datetime, session: str | None = None) -> None:
        """Record original identities, running state and restart policies first."""
        containers = {}
        for name in CONTAINERS:
            row = self.inspect(name)
            policy = row["HostConfig"]["RestartPolicy"]
            policy_name = policy["Name"] or "no"
            if policy_name not in ("no", "always", "unless-stopped", "on-failure"):
                raise MaintenanceError("Unsupported restart policy: " + name)
            count = policy.get("MaximumRetryCount", 0)
            if type(count) is not int or count < 0:
                raise MaintenanceError("Invalid restart policy: " + name)
            containers[name] = {"id": row["Id"], "running": row["State"]["Running"],
                                "policy": policy_name + (":" + str(count) if policy_name == "on-failure" and count else "")}
        self.state = {"schema": 1, "date": now.date().isoformat(), "active": True,
                      "containers": containers, "jobs": {"sync": "pending"},
                      "detail": "Preparing maintenance; stopping Sonarr and Radarr"}
        if now.weekday() == 6:
            self.state["jobs"]["scrub"] = "pending"
        if session is not None:
            # Only OMV tracks job execution; the agent owns container recovery.
            self.state.update(role="docker", session=session, jobs={})
        self.save()

    def stop_writers(self) -> None:
        """Disable automatic restarts and verify each saved container is stopped."""
        for name, saved in self.state["containers"].items():
            row = self.inspect(name)
            if row["Id"] != saved["id"]:
                raise MaintenanceError(name + " was replaced during maintenance")
            # Persisted original policies make this restart-safe. Disable restart
            # before stopping so a Docker daemon restart cannot resume imports.
            self.docker("update", "--restart=no", saved["id"])
            if row["State"]["Running"]:
                self.docker("stop", "--timeout=120", saved["id"])
            if self.inspect(name)["State"]["Running"]:
                raise MaintenanceError(name + " did not stop")

    def run_job(self, job: str) -> None:
        """Run the existing script synchronously; failed/interrupted jobs hold apps."""
        path = Path(JOBS[job])
        details = path.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != 0
                or details.st_mode & 0o022 or not os.access(path, os.X_OK)):
            raise MaintenanceError("Job must be an executable root-owned, non-writable regular file: " + str(path))
        self.state["jobs"][job] = "running"
        self.state["detail"] = "Running SnapRAID " + job
        self.save()
        # Flush prior writes before SnapRAID begins. Its existing guarded script
        # retains responsibility for its own deletion thresholds and sync rules.
        os.sync()
        try:
            result = subprocess.run([str(path)], check=False)
        except OSError as exc:
            self.state["jobs"][job] = "failed"
            raise MaintenanceError("Could not execute SnapRAID " + job) from exc
        self.state["jobs"][job] = "complete" if result.returncode == 0 else "failed"
        self.save()
        if result.returncode:
            raise MaintenanceError(f"SnapRAID {job} exited with status {result.returncode}; review job logs before retrying")

    def restore(self) -> None:
        """Restore policies and start only previously running, unchanged containers."""
        for name, saved in self.state["containers"].items():
            if self.inspect(name)["Id"] != saved["id"]:
                raise MaintenanceError(name + " was replaced; cannot restore automatically")
        self.state["detail"] = "SnapRAID jobs finished; restoring Sonarr and Radarr"
        self.save()
        for name, saved in self.state["containers"].items():
            self.docker("update", "--restart=" + saved["policy"], saved["id"])
            if saved["running"] and not self.inspect(name)["State"]["Running"]:
                self.docker("start", saved["id"])
            if self.inspect(name)["State"]["Running"] != saved["running"]:
                raise MaintenanceError("Could not restore original running state: " + name)
        self.state.update(active=False, detail="Maintenance complete; original container states restored")
        self.save()

    def tick(self, now: datetime | None = None) -> None:
        """Reconcile one schedule tick; hold through overruns and Sunday job gaps."""
        live_clock = now is None
        now = (now or datetime.now(ZONE)).astimezone(ZONE)
        if self.state:
            # Repair a publication interrupted after its private journal commit.
            # In particular, a completed session must not leave Guard held by
            # an old active marker forever after a transient status write error.
            self.save()
        if not self.state or not self.state["active"]:
            if (now.hour < 2 or now.hour >= 6
                    or self.state and self.state["date"] == now.date().isoformat()):
                return
            self.prepare(now)
        self.stop_writers()
        jobs = self.state["jobs"]
        if any(value in ("running", "failed") for value in jobs.values()):
            raise MaintenanceError("SnapRAID job failed or was interrupted; confirm jobs have stopped before retrying")
        old_date = self.state["date"] != now.date().isoformat()
        if jobs["sync"] == "pending" and (old_date or (now.hour, now.minute) >= (2, 1)):
            self.run_job("sync")
        # Re-read local time after long syncs, including runs past 06:00.
        current = datetime.now(ZONE) if live_clock else now
        if (jobs.get("scrub") == "pending" and jobs["sync"] == "complete"
                and (self.state["date"] != current.date().isoformat()
                     or (current.hour, current.minute) >= (4, 30))):
            self.run_job("scrub")
        if all(value == "complete" for value in jobs.values()):
            self.restore()
        else:
            self.state["detail"] = "Maintenance active; waiting for " + ("Sunday 04:30 scrub" if jobs["sync"] == "complete" else "02:01 sync")
            self.save()


def root_config(path: Path) -> dict:
    """Read an operator-owned configuration without accepting writable links."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise MaintenanceError("Configuration must be a root-owned, non-writable regular file: " + str(path))
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise MaintenanceError("Configuration must be a JSON object")
    return data


def private_directory(path: Path) -> None:
    """Require private root-owned journals on either host."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise MaintenanceError("State directory must be root-owned, mode 0700, without symlinks")


class RemoteCoordinator(Coordinator):
    """Schedule OMV jobs while controlling only a restricted remote agent."""

    def __init__(self, directory: Path, connection: dict):
        super().__init__(directory, DEFAULT_STATUS)
        if self.state and self.state.get("role") != "omv":
            raise MaintenanceError("This is not an OMV journal; do not move Docker-host recovery state here")
        self.connection = connection
        host, user = connection.get("host", ""), connection.get("user", "guard-maintenance")
        port = connection.get("port", 22)
        if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", host)
                or not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user)
                or type(port) is not int or not 1 <= port <= 65535):
            raise MaintenanceError("Configure a valid Docker host, SSH user and port")
        for name in ("identity_file", "known_hosts_file"):
            value = connection.get(name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise MaintenanceError("Configure an absolute SSH " + name)

    def save(self) -> None:
        """OMV stores job phases locally; only the remote agent publishes status."""
        atomic_json(self.path, self.state, 0o600)

    def remote(self, action: str) -> dict:
        """Require strict host-key verification and an acknowledged matching session."""
        config = self.connection
        session = self.state["session"]
        command = ["ssh", "-T", "-oBatchMode=yes", "-oIdentitiesOnly=yes", "-oStrictHostKeyChecking=yes",
                   "-oUserKnownHostsFile=" + config["known_hosts_file"], "-oConnectTimeout=10",
                   "-oServerAliveInterval=5", "-oServerAliveCountMax=3", "-i", config["identity_file"],
                   "-p", str(config.get("port", 22)), "--",
                   config.get("user", "guard-maintenance") + "@" + config["host"], action + " " + session]
        try:
            result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=360)
            data = json.loads(result.stdout)
            if (not isinstance(data, dict) or data.get("session") != session
                    or type(data.get("active")) is not bool or data.get("ok") is not True
                    or data["active"] != (action != "finish")):
                raise ValueError("invalid agent acknowledgement")
            return data
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise MaintenanceError("Docker-host SSH confirmation failed for " + action + "; SnapRAID/startup withheld") from exc

    def prepare(self, now: datetime) -> None:
        """Persist OMV session before asking the Docker host to stop containers."""
        self.state = {"schema": 1, "role": "omv", "date": now.date().isoformat(), "active": True,
                      "session": uuid.uuid4().hex, "containers": {}, "jobs": {"sync": "pending"},
                      "detail": "Waiting for Docker-host stop confirmation"}
        if now.weekday() == 6:
            self.state["jobs"]["scrub"] = "pending"
        self.save()

    def stop_writers(self) -> None:
        """Never let a failed SSH request become authorization to run SnapRAID."""
        self.remote("begin")

    def run_job(self, job: str) -> None:
        """Reconfirm the remote hold immediately before executing an OMV job."""
        self.remote(job)
        super().run_job(job)

    def restore(self) -> None:
        """Finish idempotently after every scheduled OMV job succeeds."""
        self.remote("finish")
        self.state.update(active=False, detail="Maintenance complete; Docker host confirmed restoration")
        self.save()

    def tick(self, now: datetime | None = None) -> None:
        """Retry a lost finish acknowledgement without re-stopping restored apps."""
        if self.state and self.state["active"] and all(phase == "complete" for phase in self.state["jobs"].values()):
            self.restore()
            return
        super().tick(now)
        if self.state and self.state["active"]:
            self.remote("hold")


def agent_request(request: str, directory: Path = AGENT_DIRECTORY, status_file: Path = DEFAULT_STATUS) -> dict:
    """Handle only fixed verbs/session IDs, under the Docker host's own lock."""
    words = shlex.split(request)
    if (len(words) != 2 or words[0] not in ("check", "begin", "sync", "scrub", "hold", "failed", "finish", "status")
            or not re.fullmatch(r"[0-9a-f]{32}", words[1])):
        raise MaintenanceError("Invalid restricted maintenance request")
    action, session = words
    private_directory(directory)
    with (directory / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceError("Docker-host maintenance operation still running") from exc
        agent = Coordinator(directory, status_file)
        if action == "check":
            for name in CONTAINERS:
                agent.inspect(name)
            return {"ok": True, "session": session, "active": bool(agent.state and agent.state["active"])}
        if action == "begin" and (not agent.state or not agent.state["active"] and agent.state.get("session") != session):
            agent.prepare(datetime.now(ZONE), session=session)
        if not agent.state or agent.state.get("session") != session or agent.state.get("role") != "docker":
            raise MaintenanceError("Maintenance session mismatch; preserve both hosts' recovery state")
        if action == "finish":
            if agent.state["active"]:
                agent.restore()
            else:
                agent.save()  # Repair a lost/incomplete public status write.
        elif action != "status":
            if not agent.state["active"]:
                raise MaintenanceError("Session already completed; refusing to stop apps again")
            agent.stop_writers()
            # Drain the Docker host's NFS client writes before OMV is allowed
            # to scan parity-protected shares. OMV flushes its side as well.
            os.sync()
            detail = {"begin": "Maintenance active; awaiting OMV SnapRAID jobs",
                      "sync": "Running SnapRAID sync on OMV", "scrub": "Running SnapRAID scrub on OMV",
                      "hold": "Waiting for the next OMV maintenance job; Sonarr/Radarr remain stopped",
                      "failed": "OMV maintenance held after failure; review OMV job logs"}[action]
            agent.state["detail"] = detail
            agent.save()
        return {"ok": True, "session": session, "active": agent.state["active"]}


def main() -> int:
    """Run the OMV scheduler, or the tightly restricted Docker-host SSH agent."""
    if len(sys.argv) >= 2 and sys.argv[1] == "agent":
        if os.geteuid() != 0 or len(sys.argv) != 3:
            print("Restricted agent requires root and one request argument", file=sys.stderr)
            return 1
        try:
            status_file = DEFAULT_STATUS
            if AGENT_CONFIG.exists():
                config = root_config(AGENT_CONFIG)
                if set(config) != {"status_file"} or not isinstance(config["status_file"], str) or not Path(config["status_file"]).is_absolute():
                    raise MaintenanceError("Agent configuration permits only an absolute status_file")
                status_file = Path(config["status_file"])
            print(json.dumps(agent_request(sys.argv[2], status_file=status_file)))
            return 0
        except (MaintenanceError, OSError, ValueError, KeyError, TypeError) as exc:
            print("Maintenance agent error: " + str(exc), file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tick", "status", "retry"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/torrent-guard-maintenance"))
    parser.add_argument("--config", type=Path, default=REMOTE_CONFIG)
    parser.add_argument("--confirm-jobs-stopped", action="store_true",
                        help="operator has verified no SnapRAID/guarded script remains running")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("This OMV coordinator must run as root")
    try:
        private_directory(args.state_dir)
    except (MaintenanceError, OSError) as exc:
        parser.error(str(exc))
    with (args.state_dir / "lock").open("a") as lock:
        if args.command == "status":
            try:
                print(json.dumps(Coordinator(args.state_dir, DEFAULT_STATUS).state, indent=2))
            except (OSError, ValueError) as exc:
                print("Cannot read maintenance journal: " + str(exc), file=sys.stderr)
                return 1
            return 0
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.command != "tick":
                print("Maintenance is still running; retry was not applied", file=sys.stderr)
                return 1
            return 0
        try:
            coordinator = RemoteCoordinator(args.state_dir, root_config(args.config))
        except (MaintenanceError, OSError, ValueError) as exc:
            print("Cannot read maintenance journal; no jobs or restores attempted: " + str(exc), file=sys.stderr)
            return 1
        try:
            if args.command == "retry":
                if not args.confirm_jobs_stopped:
                    parser.error("retry requires --confirm-jobs-stopped after checking actual host processes")
                if not coordinator.state or not coordinator.state["active"]:
                    raise MaintenanceError("No active maintenance to retry")
                for job, phase in coordinator.state["jobs"].items():
                    if phase in ("running", "failed"):
                        coordinator.state["jobs"][job] = "pending"
                coordinator.state["detail"] = "Operator authorized retry; awaiting next schedule tick"
                coordinator.save()
            else:
                coordinator.tick()
        except (MaintenanceError, OSError, ValueError, KeyError, TypeError) as exc:
            if coordinator.state and coordinator.state.get("active"):
                coordinator.state["detail"] = "Maintenance held: " + str(exc)[:800]
                try:
                    coordinator.save()
                    coordinator.remote("failed")
                except (MaintenanceError, OSError, ValueError) as save_error:
                    print("Cannot publish maintenance hold: " + str(save_error), file=sys.stderr)
            print("Maintenance error: " + str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
