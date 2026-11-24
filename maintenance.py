#!/usr/bin/env python3
"""OMV SnapRAID coordinator, restricted Docker-host agent and Guard status.

Install this root-owned command on both hosts; never mount Docker's socket in
Guard. OMV runs jobs only after SSH confirms the Docker-host hold. The daemon
only calls read_maintenance(). Each host preserves its own durable journal.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime
import errno
import fcntl
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("America/New_York")
CONTAINERS = ("sonarr", "radarr")
SCRUB_WEEKDAY = 0  # Monday in datetime.weekday(); applies to newly prepared sessions.
JOBS = {"sync": "/usr/local/sbin/snapraid-guarded-sync",
        "scrub": "/usr/local/sbin/snapraid-guarded-scrub"}
DEFAULT_STATUS = Path("/config/torrent-guard/maintenance.json")
REMOTE_CONFIG = Path("/etc/torrent-guard-maintenance.json")
AGENT_DIRECTORY = Path("/var/lib/torrent-guard-maintenance-agent")
AGENT_CONFIG = Path("/etc/torrent-guard-maintenance-agent.json")


LOG = logging.getLogger(__name__)


def pushover_keys(settings: dict) -> tuple[str, str]:
    """Validate an optional credential pair without including secrets in errors."""
    token, user = settings.get("app_token", ""), settings.get("user_key", "")
    if not token and not user:
        return "", ""
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{30}", value)
           for value in (token, user)):
        raise ValueError("Pushover requires both a 30-character application token and user key")
    return token, user


def send_pushover(token: str, user: str, title: str, message: str) -> bool:
    """Send one bounded HTTPS request; never redirect credentials or log replies."""
    connection = http.client.HTTPSConnection("api.pushover.net", timeout=10)
    try:
        body = urllib.parse.urlencode({"token": token, "user": user,
                                       "title": title[:250], "message": message[:1024]})
        connection.request("POST", "/1/messages.json", body,
                           {"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096 or json.loads(raw).get("status") != 1:
            raise ValueError("Pushover did not acknowledge the message")
        return True
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        # API responses and exception text may contain credentials. Keep the
        # warning actionable without echoing either or changing maintenance.
        LOG.warning("Pushover notification failed; check credentials, quota and OMV HTTPS connectivity")
        return False
    finally:
        connection.close()


@dataclass(frozen=True)
class SnapraidStatus:
    """Bounded status evidence; displayable partial output never authorizes work."""

    text: str = ""
    label: str = "SnapRAID status"
    complete: bool = False

    def summary(self) -> str:
        """Return the bounded operator summary used by notifications."""
        return self.label + ":\n" + (self.text[-700:] or "No output returned.")

    def unscrubbed_percent(self) -> int | None:
        """Accept one unambiguous coverage result from a complete healthy report."""
        if not self.complete:
            return None
        lines = self.text.splitlines()
        if ("No sync is in progress." not in lines or "No error detected." not in lines
                or any(re.search(r"DANGER!|NOT fully synced|sync in progress at", line) for line in lines)):
            return None
        values = []
        for line in lines:
            match = re.fullmatch(r"(?:The )?(\d{1,3})% of the array is not scrubbed\.", line)
            if match:
                values.append(int(match[1]))
            elif line in ("The full array was scrubbed at least one time.",
                          "The entire array has been scrubbed at least once."):
                values.append(0)
        return values[0] if len(values) == 1 and 0 <= values[0] <= 100 else None


def read_snapraid_status(config_path: str = "", timeout_seconds: int = 120) -> SnapraidStatus:
    """Collect bounded evidence, distinguishing process success from partial output."""
    try:
        with tempfile.TemporaryFile() as output:
            command = ["/usr/bin/snapraid"] + (["-c", config_path] if config_path else []) + ["status"]
            complete = False
            try:
                result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                        timeout=timeout_seconds, check=False)
                complete = result.returncode == 0
                label = "SnapRAID status" + (f" (exit {result.returncode})" if result.returncode else "")
            except subprocess.TimeoutExpired:
                label = f"SnapRAID status timed out after {timeout_seconds} seconds (partial report)"
            except OSError as error:
                reason = {errno.ENOENT: "executable or required loader missing",
                          errno.EACCES: "execution permission denied"}.get(error.errno, f"OS error {error.errno}")
                return SnapraidStatus(label="SnapRAID status could not start: " + reason + "; check /usr/bin/snapraid on OMV.")
            output.seek(0, os.SEEK_END)
            length = output.tell()
            output.seek(max(0, length - 65536))
            raw = output.read(65536)
        # Never parse a truncated/invalid/control-bearing report as policy input.
        decoded = raw.decode("utf-8", errors="replace")
        text = "".join(char for char in decoded if char in "\n\t" or char.isprintable()).strip()
        complete = complete and length <= 65536 and text == decoded.strip() and "\ufffd" not in text
        return SnapraidStatus(text=text, label=label, complete=complete)
    except OSError as error:
        return SnapraidStatus(label=f"SnapRAID status output unavailable (OS error {error.errno}); check OMV temporary storage.")


def snapraid_status(config_path: str = "", timeout_seconds: int = 120) -> str:
    """Return a display summary; automated policy uses the structured evidence."""
    return read_snapraid_status(config_path, timeout_seconds).summary()


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
    """Run saved maintenance jobs while preventing overlapping imports."""

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
        if now.weekday() == SCRUB_WEEKDAY:
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
        self.job_start(job)
        # A new execution supersedes its old failure without erasing completed
        # job phases. Only fixed labels/codes are stored, never script output.
        self.state.pop("job_failure", None)
        attempts = self.state.setdefault("job_attempts", {})
        attempts[job] = attempts.get(job, 0) + 1
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
            self.state["job_failure"] = {"job": job, "kind": "launch", "at": time.time()}
            self.save()
            raise MaintenanceError("Could not execute SnapRAID " + job) from exc
        self.state["jobs"][job] = "complete" if result.returncode == 0 else "failed"
        if result.returncode:
            self.state["job_failure"] = {"job": job, "kind": "exit", "exit_code": result.returncode,
                                         "at": time.time()}
        if result.returncode == 0:
            self.job_completed(job)
        self.save()
        if result.returncode:
            raise MaintenanceError(f"SnapRAID {job} exited with status {result.returncode}; review job logs before retrying")

    def job_start(self, job: str) -> None:
        """Hook for OMV notifications after job validation and hold confirmation."""

    def job_completed(self, job: str) -> None:
        """Hook to journal post-job observation intent before committing success."""

    def plan_scrub(self) -> None:
        """Hook to extend a new OMV session only after its sync succeeds."""

    def observe_scrub(self) -> None:
        """Hook to process saved successful-scrub observation intent."""

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
        """Run the saved job plan sequentially, retaining the hold through overruns."""
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
        if jobs["sync"] == "complete":
            self.plan_scrub()
        # The persisted plan belongs to the session's start date. A Monday
        # scrub follows successful sync in this same tick, even after an overrun
        # into another day. Existing journal plans remain intact across upgrades.
        if jobs.get("scrub") == "pending" and jobs["sync"] == "complete":
            self.run_job("scrub")
        self.observe_scrub()
        if all(value == "complete" for value in jobs.values()):
            self.restore()
        else:
            self.state["detail"] = "Maintenance active; waiting for 02:01 sync"
            self.save()


def root_config(path: Path) -> dict:
    """Read an operator-owned configuration without accepting writable links."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise MaintenanceError("Configuration must be a root-owned, non-writable regular file: " + str(path))
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise MaintenanceError("Configuration must be a JSON object")
    if data.get("pushover") and info.st_mode & 0o077:
        raise MaintenanceError("Pushover configuration must be private (mode 0600)")
    return data


def private_directory(path: Path) -> None:
    """Require private root-owned journals on either host."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise MaintenanceError("State directory must be root-owned, mode 0700, without symlinks")


class RemoteCoordinator(Coordinator):
    """Schedule OMV sync and automatic scrub catch-up through a restricted agent."""

    def __init__(self, directory: Path, connection: dict):
        super().__init__(directory, DEFAULT_STATUS)
        if self.state and self.state.get("role") != "omv":
            raise MaintenanceError("This is not an OMV journal; do not move Docker-host recovery state here")
        self.connection = connection
        self.status_snapshot: SnapraidStatus | None = None
        settings = connection.get("pushover", {})
        try:
            if not isinstance(settings, dict):
                raise ValueError("Pushover configuration must be an object")
            self.notification_keys = pushover_keys(settings)
        except ValueError:
            LOG.warning("Invalid Pushover credentials; notifications disabled; maintenance continues")
            self.notification_keys = ("", "")
        host, user = connection.get("host", ""), connection.get("user", "guard-maintenance")
        port = connection.get("port", 22)
        if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", host)
                or not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user)
                or type(port) is not int or not 1 <= port <= 65535):
            raise MaintenanceError("Configure a valid Docker host, SSH user and port")
        self.snapraid_config = connection.get("snapraid_config", "")
        if not isinstance(self.snapraid_config, str) or (self.snapraid_config and not Path(self.snapraid_config).is_absolute()):
            LOG.warning("Invalid SnapRAID status configuration; using default configuration")
            self.snapraid_config = ""
        self.snapraid_status_timeout = connection.get("snapraid_status_timeout_seconds", 120)
        if type(self.snapraid_status_timeout) is not int or not 10 <= self.snapraid_status_timeout <= 600:
            LOG.warning("Invalid SnapRAID status timeout; using 120 seconds (allowed: 10-600)")
            self.snapraid_status_timeout = 120
        start = connection.get("scrub_catchup_start_percent", 20)
        stop = connection.get("scrub_catchup_stop_percent", 5)
        if type(start) is not int or type(stop) is not int or not 0 <= stop < start <= 100:
            raise MaintenanceError("Scrub thresholds must be integers with 0 <= stop < start <= 100")
        self.scrub_thresholds = {"start": start, "stop": stop}
        for name in ("identity_file", "known_hosts_file"):
            value = connection.get(name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise MaintenanceError("Configure an absolute SSH " + name)

    def notify(self, event: str, title: str, message: str, *, status: bool = False) -> None:
        """Attempt each event once per durable session without blocking recovery.

        Record intent first: a crash or uncertain HTTP response must not produce
        repeated alerts on every cron tick. Delivery is best effort, not a job
        prerequisite. Notification bookkeeping failures are isolated too.
        """
        token, user = self.notification_keys
        if not token or not self.state:
            return
        events = self.state.setdefault("notification_events", [])
        if event in events:
            return
        events.append(event)
        try:
            self.save()
        except (OSError, ValueError):
            LOG.warning("Could not record Pushover event; notification skipped; maintenance continues")
            return
        stamp = datetime.now(ZONE).strftime("%m.%d.%Y %H:%M %Z%z")
        message = stamp + "\n" + message
        if status:
            summary = (self.status_snapshot.summary() if self.status_snapshot is not None else
                       snapraid_status(self.snapraid_config, self.snapraid_status_timeout))
            budget = max(0, 1024 - len(message) - 2)
            if len(summary) > budget:
                # Retain the status header/exit code and final summary rather
                # than truncating the useful ending behind recovery guidance.
                header, separator, tail = summary.partition("\n")
                if separator and budget > len(header) + 5:
                    summary = header + "\n...\n" + tail[-(budget - len(header) - 5):]
                else:
                    summary = summary[:budget]
            message += "\n\n" + summary
        send_pushover(token, user, title, message)

    def job_start(self, job: str) -> None:
        """Announce sync, or its successful transition into the planned scrub."""
        if job == "sync":
            self.notify("sync_start", "SnapRAID sync starting",
                        "Sonarr and Radarr are stopped. SnapRAID sync is starting.")
        else:
            kind = "Daily catch-up" if self.state.get("scrub_reason") == "catchup" else "Weekly"
            self.notify("scrub_start", "SnapRAID sync complete; scrub starting",
                        f"Sync completed successfully. {kind} scrub is starting. "
                        "Sonarr and Radarr remain stopped.\n" + self.state.get("scrub_policy_detail", ""), status=True)

    def notify_complete(self) -> None:
        """Report completion only after the remote restoration acknowledgement."""
        work = "Sync"
        if "scrub" in self.state["jobs"]:
            work += " and " + ("catch-up" if self.state.get("scrub_reason") == "catchup" else "weekly") + " scrub"
        self.notify("complete", "SnapRAID maintenance complete",
                    work + " completed successfully. Sonarr and Radarr were restored "
                    "to their original running states (previously running apps restarted).\n"
                    + self.state.get("scrub_policy_detail", ""), status=True)
        self.notify_policy_notices()

    def notify_policy_notices(self) -> None:
        """Publish policy issues after restoration without changing job outcomes."""
        if self.state.get("scrub_status_notice"):
            self.notify("scrub_status", "SnapRAID scrub coverage unavailable",
                        "Automatic catch-up could not verify coverage. Saved mode is unchanged; "
                        "no extra scrub was authorized by the failed reading. Weekly scheduling remains. "
                        "Check OMV SnapRAID status and its configured deadline.")
        if self.state.get("scrub_stalled_notice"):
            self.notify("scrub_stalled", "SnapRAID catch-up needs attention",
                        "Three successful scrubs made no percentage decrease. Daily catch-up remains "
                        "active. Check the guarded scrub's 8% plan and 10-day age cutoff, new data, "
                        "and OMV logs. Safety checks remain in effect.")

    def coverage(self) -> int | None:
        """Read fresh full status off the array; retain its display summary in memory."""
        self.status_snapshot = read_snapraid_status(self.snapraid_config, self.snapraid_status_timeout)
        percent = self.status_snapshot.unscrubbed_percent()
        policy = self.state["scrub_policy"]
        problem = percent is None
        new_problem = problem and not policy.get("status_problem", False)
        self.state["scrub_status_notice"] = problem and (new_problem or self.state.get("scrub_status_notice", False))
        if new_problem:
            LOG.warning("Scrub coverage unavailable; automatic catch-up state preserved; weekly plan retained")
        policy["status_problem"] = problem
        return percent

    def update_scrub_mode(self, percent: int) -> str:
        """Apply hysteresis to reliable coverage without changing any live job phases."""
        policy = self.state["scrub_policy"]
        limits = self.state["scrub_thresholds"]
        old = policy["active"]
        if percent > limits["start"]:
            policy["active"] = True
        elif percent <= limits["stop"]:
            policy["active"] = False
        policy["last_percent"] = percent
        if not old and policy["active"]:
            return f"Daily catch-up started: {percent}% unscrubbed (trigger >{limits['start']}%)."
        if old and not policy["active"]:
            policy.update(stalled_runs=0, stalled_alerted=False)
            return f"Catch-up complete: {percent}% unscrubbed; returning to weekly Monday scrubs."
        return f"{percent}% unscrubbed; " + ("daily catch-up continues." if policy["active"] else "weekly Monday scrubs.")

    def plan_scrub(self) -> None:
        """Freeze a new session's extra scrub once, following a successful sync."""
        if not self.state.get("scrub_policy_pending"):
            return  # Preserve plans from older releases and completed decisions.
        percent = self.coverage()
        if percent is not None:
            self.state["scrub_before_percent"] = percent
            self.state["scrub_policy_detail"] = self.update_scrub_mode(percent)
            if self.state["scrub_policy"]["active"]:
                self.state["jobs"].setdefault("scrub", "pending")
                self.state["scrub_reason"] = "catchup"
        else:
            self.state["scrub_policy_detail"] = "Coverage unavailable; saved catch-up mode unchanged."
        self.state["scrub_policy_pending"] = False
        self.save()  # Policy decision and extended plan commit together before launch.

    def job_completed(self, job: str) -> None:
        """Commit observation intent with success so a restart never reruns the scrub."""
        if job == "scrub" and "scrub_policy" in self.state:
            self.state["scrub_result_pending"] = True

    def observe_scrub(self) -> None:
        """Record one post-scrub result, progress and persistent stalled incidents."""
        if not self.state.get("scrub_result_pending"):
            return
        percent = self.coverage()
        policy = self.state["scrub_policy"]
        if percent is not None:
            before = self.state.get("scrub_before_percent")
            detail = self.update_scrub_mode(percent)
            if before is not None and percent < before:
                policy.update(stalled_runs=0, stalled_alerted=False)
            elif before is not None and policy["active"]:
                policy["stalled_runs"] = policy.get("stalled_runs", 0) + 1
                if policy["stalled_runs"] >= 3 and not policy.get("stalled_alerted", False):
                    self.state["scrub_stalled_notice"] = True
                    policy["stalled_alerted"] = True
                    LOG.warning("Daily scrub catch-up made no percentage decrease for three successful runs")
            prefix = f"Scrub coverage: {before}% -> {percent}% unscrubbed. " if before is not None else ""
            self.state["scrub_policy_detail"] = prefix + detail
        else:
            policy["stalled_runs"] = 0
            self.state["scrub_policy_detail"] = "Post-scrub coverage unavailable; saved catch-up mode unchanged."
        self.state["scrub_result_pending"] = False
        self.save()

    def notify_failure(self, error: Exception) -> None:
        """Report bounded, verified facts and preserve the original job failure.

        The read-only status request supplies current app state when supported.
        Old agents, SSH errors and changed identities yield unknown state, never
        a claim that apps are stopped or permission to restart them.
        """
        if not self.notification_keys[0]:
            return
        jobs = self.state["jobs"]
        phases = ", ".join(job + "=" + phase for job, phase in jobs.items())
        failure = self.state.get("job_failure", {})
        reason = "Coordinator could not finish; check OMV maintenance/job logs."
        if isinstance(failure, dict) and failure.get("job") in JOBS and jobs.get(failure["job"]) == "failed":
            job = failure["job"]
            code = failure.get("exit_code")
            if failure.get("kind") == "exit" and type(code) is int:
                reason = f"SnapRAID {job} script exited with status {code}."
            elif failure.get("kind") == "launch":
                reason = f"SnapRAID {job} script could not be launched."
        elif any(phase in ("failed", "running") for phase in jobs.values()):
            reason = "SnapRAID job failed/interrupted; original exit code unavailable."
        # Stable across restart and subsequent generic interrupted-job errors.
        # Other incidents use a private hash, never their raw exception text.
        identity = phases + reason + str(self.state.get("job_attempts", {}))
        if reason.startswith("Coordinator"):
            identity += hashlib.sha256(str(error).encode()).hexdigest()
        event = "failure:" + hashlib.sha256(identity.encode()).hexdigest()
        if event in self.state.get("notification_events", []):
            return
        apps = "Sonarr/Radarr: current state unknown (status unavailable)."
        try:
            data = self.remote("status")
            states = data.get("containers", {})
            labels = []
            for name in CONTAINERS:
                row = states.get(name, {})
                if (isinstance(row, dict) and type(row.get("running")) is bool
                        and row.get("paused") is False and row.get("restarting") is False):
                    label = "running" if row["running"] else "stopped"
                else:
                    label = "unknown"
                labels.append(name.title() + ": " + label)
            apps = "; ".join(labels) + "."
        except (MaintenanceError, OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Notification-only check; the original maintenance error wins.
        message = (f"Session {self.state['date']} / {self.state['session']}\n"
                   + phases + "\n" + reason + "\n" + apps + "\n"
                   "Maintenance hold active; automatic restart withheld; upgrade blocked.\n"
                   "On OMV: inspect job logs and fix the cause. Once no SnapRAID/guarded jobs "
                   "are running, authorize retry:\n"
                   "sudo /usr/local/sbin/torrent-guard-maintenance retry --confirm-jobs-stopped")
        self.notify(event, "SnapRAID maintenance needs attention", message, status=True)

    def save(self) -> None:
        """OMV stores job phases locally; only the remote agent publishes status."""
        atomic_json(self.path, self.state, 0o600)

    def remote(self, action: str) -> dict:
        """Require strict host-key verification and an acknowledged matching session."""
        config = self.connection
        session = self.state["session"]
        command = ssh_command(config, action + " " + session)
        try:
            result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=360)
            data = json.loads(result.stdout)
            if (not isinstance(data, dict) or data.get("session") != session
                    or type(data.get("active")) is not bool or data.get("ok") is not True
                    or (action != "status" and data["active"] != (action != "finish"))):
                raise ValueError("invalid agent acknowledgement")
            return data
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise MaintenanceError("Docker-host SSH confirmation failed for " + action + "; SnapRAID/startup withheld") from exc

    def prepare(self, now: datetime) -> None:
        """Persist OMV session before asking the Docker host to stop containers."""
        policy = (self.state or {}).get("scrub_policy", {"active": False})
        if (not isinstance(policy, dict) or type(policy.get("active")) is not bool
                or type(policy.get("stalled_runs", 0)) is not int or policy.get("stalled_runs", 0) < 0):
            raise MaintenanceError("Invalid scrub policy journal; preserve it for operator recovery")
        if policy.get("config_path", self.snapraid_config) != self.snapraid_config:
            policy = {"active": False}
        policy["config_path"] = self.snapraid_config
        self.state = {"schema": 1, "role": "omv", "date": now.date().isoformat(), "active": True,
                      "session": uuid.uuid4().hex, "containers": {}, "jobs": {"sync": "pending"},
                      "detail": "Waiting for Docker-host stop confirmation",
                      "scrub_policy": policy, "scrub_policy_pending": True,
                      "scrub_thresholds": dict(self.scrub_thresholds)}
        if now.weekday() == SCRUB_WEEKDAY:
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
        self.notify_complete()

    def tick(self, now: datetime | None = None) -> None:
        """Retry a lost finish acknowledgement without re-stopping restored apps."""
        self.status_snapshot = None  # Cached evidence belongs only to this invocation.
        try:
            if self.state and all(phase == "complete" for phase in self.state["jobs"].values()):
                if self.state["active"]:
                    self.plan_scrub()
                    self.observe_scrub()
                    if not all(phase == "complete" for phase in self.state["jobs"].values()):
                        super().tick(now)
                        return
                    self.restore()
                    return
            super().tick(now)
            if self.state and self.state["active"]:
                self.remote("hold")
        except (MaintenanceError, OSError, ValueError, KeyError, TypeError) as exc:
            if self.state and self.state.get("active"):
                self.notify_failure(exc)
            raise


def ssh_command(config: dict, request: str) -> list[str]:
    """Isolate maintenance from normal SSH keys, config, agents and multiplexing."""
    return ["ssh", "-F", "/dev/null", "-T", "-oBatchMode=yes", "-oIdentitiesOnly=yes",
            "-oIdentityAgent=none", "-oControlMaster=no", "-oControlPath=none",
            "-oGlobalKnownHostsFile=/dev/null", "-oUpdateHostKeys=no", "-oForwardAgent=no",
            "-oClearAllForwardings=yes", "-oPermitLocalCommand=no", "-oStrictHostKeyChecking=yes",
            "-oUserKnownHostsFile=" + config["known_hosts_file"], "-oConnectTimeout=10",
            "-oServerAliveInterval=5", "-oServerAliveCountMax=3", "-i", config["identity_file"],
            "-p", str(config.get("port", 22)), "--",
            config.get("user", "guard-maintenance") + "@" + config["host"], request]


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
        result = {"ok": True, "session": session, "active": agent.state["active"]}
        if action == "status":
            states = {}
            for name in CONTAINERS:
                row = agent.inspect(name)
                if row["Id"] != agent.state["containers"][name]["id"]:
                    raise MaintenanceError(name + " identity changed; current maintenance state unverified")
                states[name] = {key.lower(): row["State"][key] for key in ("Running", "Paused", "Restarting")}
            result["containers"] = states
        return result


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
