"""Host-invoked diagnostics and independent, bounded monitoring history.

Monitoring never authorizes cleanup or changes the deletion journal. The daemon
writes a separate SQLite file; the report opens both databases read-only.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from scripts.watch_logs import safe_text, wrap_text

RETENTION = 7 * 86400

class DiagnosticError(Exception):
    """An intentionally public diagnostic without remote response content."""


def private_text(message: str) -> str:
    """Redact configured credentials before persisting or displaying details."""
    secrets = [value for key, value in os.environ.items()
               if value and (key.endswith("_API_KEY") or key.endswith("_PASSWORD"))]
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, "[redacted]")
    return safe_text(message)


def history_path(state_db: str) -> Path:
    """Keep telemetry beside the configured journal, without altering it."""
    return Path(state_db + ".status.sqlite3")


@contextmanager
def read_database(path: Path):
    """Read an existing database without creating files or taking write locks."""
    database = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        database.execute("PRAGMA query_only=ON")
        yield database
    finally:
        database.close()


class StatusHistory(logging.Handler):
    """Persist event counts, recent issues and last observed poll counters.

    Counts describe emitted events, not unique torrents. Warning/error counts can
    overlap rejection events. Failures are reported without interrupting polling.
    """

    def __init__(self, path: Path):
        super().__init__(logging.INFO)
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.database = sqlite3.connect(path, timeout=1)
        os.chmod(path, 0o600)
        self.database.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (at REAL NOT NULL, metric TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_at ON events(at);
            CREATE TABLE IF NOT EXISTS issues (at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
        """)
        self.database.execute("INSERT OR IGNORE INTO metadata VALUES ('since', ?)", (str(time.time()),))
        self.database.commit()
        self.last_prune = 0.0
        self.last_failure = float("-inf")
        self.reporting_failure = False

    def emit(self, record: logging.LogRecord) -> None:
        """Record allowlisted summaries; never store exception tracebacks."""
        if self.reporting_failure:
            return
        try:
            message = record.getMessage()
            metrics = []
            if message.startswith("CLEANED "):
                metrics.append("cleaned")
            if message.startswith("REJECT candidate "):
                metrics.append("rejected")
            if record.levelno >= logging.ERROR:
                metrics.append("errors")
            elif record.levelno >= logging.WARNING:
                metrics.append("warnings")
            with self.database:
                self.database.executemany("INSERT INTO events VALUES (?, ?)",
                                          [(record.created, metric) for metric in metrics])
                if record.levelno >= logging.WARNING:
                    self.database.execute("INSERT INTO issues VALUES (?, ?, ?)",
                                          (record.created, logging.getLevelName(record.levelno), private_text(message)[:2000]))
                    for level in ("WARNING", "ERROR", "CRITICAL"):
                        self.database.execute("DELETE FROM issues WHERE level=? AND rowid NOT IN "
                                              "(SELECT rowid FROM issues WHERE level=? ORDER BY rowid DESC LIMIT 20)", (level, level))
                if message.startswith("STATUS "):
                    snapshot = dict(re.findall(r"(\w+)=(\d+)", message))
                    self.database.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot', ?)",
                                          (json.dumps({"at": record.created, "counts": snapshot}),))
                if message.startswith("Starting v"):
                    self.database.execute("INSERT OR REPLACE INTO metadata VALUES ('started', ?)", (str(record.created),))
                if self.last_failure != float("-inf"):
                    self.database.execute("INSERT OR REPLACE INTO metadata VALUES ('gap', ?)", (str(self.last_failure),))
                if record.created - self.last_prune >= 60 or record.created < self.last_prune:
                    self.database.execute("DELETE FROM events WHERE at < ?", (record.created - RETENTION,))
                    self.last_prune = record.created
        except (sqlite3.Error, OSError, ValueError):
            if record.created - self.last_failure >= 60:
                self.last_failure = record.created
                self.reporting_failure = True
                try:
                    logging.getLogger("torrent-guard").error("Status history unavailable; monitoring counts may be incomplete")
                finally:
                    self.reporting_failure = False

    def close(self) -> None:
        """Release the independent monitoring connection."""
        self.database.close()
        super().close()


def enable_history(state_db: str) -> None:
    """Observe INFO events even when console/audit verbosity is WARNING."""
    logger = logging.getLogger("torrent-guard")
    level = logger.level
    handler = StatusHistory(history_path(state_db))
    for output in logger.handlers:
        output.setLevel(level)
    logger.setLevel(min(level, logging.INFO))
    logger.addHandler(handler)


@contextmanager
def directory_fd(path: Path):
    """Open every absolute path component without following symlinks."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory must be absolute without traversal")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def probe_drive(path: Path, write: bool = False, match: Path | None = None) -> str:
    """Exercise listing/statvfs and optionally a unique create/read/remove test.

    Only our exclusively created test file is removed. Completed write probes also
    require device/inode equality with the configured read-only completed mapping.
    """
    with directory_fd(path) as directory:
        if match is not None:
            with directory_fd(match) as expected:
                actual_info, expected_info = os.fstat(directory), os.fstat(expected)
                if (actual_info.st_dev, actual_info.st_ino) != (expected_info.st_dev, expected_info.st_ino):
                    raise DiagnosticError("cleanup alias does not match completed download directory")
        with os.scandir(directory) as entries:
            next(entries, None)
        usage = os.fstatvfs(directory)
        free = usage.f_bavail * usage.f_frsize
        total = usage.f_blocks * usage.f_frsize
        writable = "not tested (read-only check)"
        if write:
            name = ".torrent-guard-status-" + uuid.uuid4().hex
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                os.write(descriptor, b"Torrent Guard status probe\n")
                os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.read(descriptor, 64) != b"Torrent Guard status probe\n":
                    raise DiagnosticError("write probe readback failed")
            finally:
                try:
                    # Do not remove a replacement introduced by another process.
                    saved, current = os.fstat(descriptor), os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (saved.st_dev, saved.st_ino) != (current.st_dev, current.st_ino):
                        raise DiagnosticError("write probe was replaced; retained for review")
                    os.unlink(name, dir_fd=directory)
                finally:
                    os.close(descriptor)
            writable = "create/write/fsync/read/remove OK"
        return f"read/list OK; free {free / 2**30:.1f} GiB / {total / 2**30:.1f} GiB; write: {writable}"


def check_drive(path: Path, write: bool = False, match: Path | None = None) -> str:
    """Isolate slow filesystem probes from the report, with a 10-second limit."""
    result = subprocess.run([sys.executable, str(Path(__file__).absolute()),
                             json.dumps([str(path), write, str(match) if match else None])],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        # Child output contains only our fixed diagnostic/OS error descriptions.
        raise DiagnosticError(result.stdout.strip() or "drive probe failed")
    return result.stdout.strip()


class Report:
    """Render compact wrapped terminal output with sanitized external text."""

    def __init__(self, color: bool, width: int):
        self.color = color
        self.width = width
        self.failed = False

    def line(self, text: str, code: str = "96") -> None:
        """Wrap before coloring so external terminal controls remain inert."""
        for paragraph in text.split("\n"):
            for row in wrap_text(private_text(paragraph), self.width, ""):
                print(f"\033[{code}m{row}\033[0m" if self.color else row)

    def check(self, label: str, operation) -> None:
        """Keep independent checks running after errors; omit raw API errors."""
        try:
            detail = operation()
            self.line(f"OK    {label}: {detail}", "92")
        except Exception as exc:
            # Diagnostic boundary: one unavailable service must not hide others.
            # Raw HTTP exceptions can contain URLs, response bodies and secrets.
            self.failed = True
            status = getattr(exc, "status", None)
            if isinstance(exc, DiagnosticError):
                detail = str(exc)
            elif isinstance(exc, subprocess.TimeoutExpired):
                detail = "drive check timed out after 10s"
            elif isinstance(exc, OSError) and exc.errno is not None:
                detail = os.strerror(exc.errno)
            else:
                detail = f"HTTP {status}" if status is not None else type(exc).__name__
            self.line(f"FAIL  {label}: {detail}; inspect private audit logs/configuration", "91")


def show_history(report: Report, path: Path, now: float) -> None:
    """Report exact rolling event windows and explicit partial coverage."""
    from guard import display_time

    with read_database(path) as database:
        since_row = database.execute("SELECT value FROM metadata WHERE key='since'").fetchone()
        since = float(since_row[0])
        if not math.isfinite(since) or since > now:
            raise ValueError("invalid history start")
        report.line("\nEvent statistics (overlapping rolling windows)")
        report.line("Window       Cleaned   Reject candidates   Warnings   Errors")
        for hours in (6, 12, 24):
            counts = dict(database.execute("SELECT metric, count(*) FROM events WHERE at >= ? AND at <= ? GROUP BY metric",
                                           (now - hours * 3600, now)))
            partial = " partial" if since > now - hours * 3600 else ""
            code = "91" if counts.get("errors") else "93" if counts.get("warnings") else "96"
            report.line(f"{hours:>2}h{partial:<9} {counts.get('cleaned', 0):>6} {counts.get('rejected', 0):>19}"
                        f" {counts.get('warnings', 0):>10} {counts.get('errors', 0):>8}", code)
        report.line("Recorded since " + display_time(since) + "; daemon downtime is not observed. Counts are events, not unique torrents.", "93")
        gap = database.execute("SELECT value FROM metadata WHERE key='gap'").fetchone()
        if gap:
            report.line("History has a recording failure; counts may be incomplete. Last failure: " + display_time(float(gap[0])), "93")
        started = database.execute("SELECT value FROM metadata WHERE key='started'").fetchone()
        if started:
            report.line("Daemon started: " + display_time(float(started[0])) + f" ({max(0, now - float(started[0])) / 3600:.1f}h ago)")
        snapshot = database.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()
        if snapshot:
            value = json.loads(snapshot[0])
            report.line("\nLast completed-poll snapshot: " + display_time(float(value["at"]))
                        + f" ({max(0, now - float(value['at'])):.0f}s ago; last observed counts)")
            for key, count in value["counts"].items():
                report.line(f"  {key}: {count}")
        else:
            report.line("No completed-poll snapshot recorded yet", "93")
        report.line("\nLatest warnings and errors (up to 5 of each)")
        for levels, title in (("WARNING", "Warnings"), ("ERROR", "Errors")):
            rows = database.execute("SELECT at, level, message FROM issues WHERE level "
                                    + ("= 'WARNING'" if levels == "WARNING" else "IN ('ERROR','CRITICAL')")
                                    + " ORDER BY rowid DESC LIMIT 5").fetchall()
            if not rows:
                report.line(title + ": none retained", "92")
            for at, level, message in rows:
                report.line(f"{display_time(at)} {level}: {message}", "93" if levels == "WARNING" else "91")


def journal_summary(path: Path) -> str:
    """Read persisted pending work in one consistent SQLite snapshot."""
    with read_database(path) as database:
        database.execute("BEGIN")
        counts = []
        for table in ("pending", "filesystem_retry", "cleanup_retry", "metadata_wait", "stalled_wait"):
            count = database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            counts.append(f"{table}={count}")
        phases = database.execute("SELECT phase, count(*) FROM pending GROUP BY phase").fetchall()
        return "; ".join(counts + [f"phase {phase}={count}" for phase, count in phases])


def run_status(color: str = "always", width: int | None = None) -> int:
    """Run one-shot diagnostics without polling, journal writes or cleanup."""
    from guard import ApiError, Config, HEARTBEAT_FILE, Http, Qbit, VERSION, display_time, healthcheck, torrent_app

    report = Report(color == "always" or (color == "auto" and sys.stdout.isatty()),
                    width or max(20, shutil.get_terminal_size((80, 24)).columns))
    now = time.time()
    report.line(f"Torrent Guard v{VERSION} status | {display_time(now)}")
    try:
        config = Config.from_env()
    except (ValueError, OSError):
        report.line("FAIL  Configuration invalid; check required settings in the container environment", "91")
        return 1
    report.line(f"Mode: {'DRY RUN' if config.dry_run else 'LIVE'}; cleanup leftovers: {config.cleanup_leftovers}; "
                f"import grace: {config.import_grace_minutes}m; UID:GID {os.getuid()}:{os.getgid()}",
                "95" if config.dry_run else "96")

    def polling():
        if healthcheck() != 0:
            raise DiagnosticError("no recent completed poll (heartbeat missing, stale or invalid)")
        age = time.monotonic() - float(HEARTBEAT_FILE.read_text())
        return f"last completed poll {age:.0f}s ago"

    report.check("Polling heartbeat", polling)

    def qbit_connection():
        client = Qbit(config)
        torrents = client.torrents()
        if not isinstance(torrents, list) or any(not isinstance(row, dict) for row in torrents):
            raise ApiError("invalid torrent inventory")
        owned = [row for row in torrents if torrent_app(row, config)]
        states = {}
        for row in owned:
            state = str(row.get("state", "unknown"))
            states[state] = states.get(state, 0) + 1
        return f"authenticated; total={len(torrents)}, managed={len(owned)}; " + ", ".join(f"{key}={value}" for key, value in sorted(states.items()))

    report.check("qBittorrent", qbit_connection)
    for app in ("SONARR", "RADARR"):
        instance = config.instances.get(app)
        if instance is None:
            report.line(f"SKIP  {app}: not configured", "93")
            continue

        def arr_connection(instance=instance):
            result = Http().request("GET", instance.url + "/api/v3/system/status", headers={"X-Api-Key": instance.api_key})
            if not isinstance(result, dict) or not isinstance(result.get("version"), str) or not result["version"]:
                raise ApiError("invalid system status")
            return "authenticated; version " + result["version"]

        report.check(app, arr_connection)
    report.line("\nDrive access (directory checks; not a full media-file or mount-source audit)")
    root = Path(config.guard_download_root)
    mapped = {}
    for label, location in (("Completed", config.qbit_complete_dir), ("Incomplete", config.qbit_incomplete_dir)):
        mapped[label] = root / PurePosixPath(location).relative_to(PurePosixPath(config.qbit_download_root))
    for label, path in {"Downloads": root, **mapped}.items():
        report.check(f"{label} {path}", lambda path=path: check_drive(path))
    report.check("State " + str(Path(config.state_db).parent), lambda: check_drive(Path(config.state_db).absolute().parent, write=True))
    if config.cleanup_leftovers:
        report.check("Completed cleanup alias " + config.cleanup_write_root,
                     lambda: check_drive(Path(config.cleanup_write_root), write=True, match=mapped["Completed"]))
    else:
        report.line("SKIP  Completed cleanup write test: cleanup disabled", "93")
    report.check("Persistent journal", lambda: journal_summary(Path(config.state_db)))

    def history():
        show_history(report, history_path(config.state_db), now)
        return "persistent history read"

    report.check("Monitoring history", history)
    report.line("\nStatus checks do not stop, blocklist or delete downloads. API status does not prove import history is available.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    # Private subprocess entry point. A failed probe returns no raw exception or
    # path details; the parent labels the failing drive and keeps checking.
    try:
        location, writable, expected = json.loads(sys.argv[1])
        print(probe_drive(Path(location), writable, Path(expected) if expected else None))
    except DiagnosticError as exc:
        print(str(exc))
        raise SystemExit(1)
    except OSError as exc:
        print(os.strerror(exc.errno) if exc.errno else "filesystem operation failed")
        raise SystemExit(1)
    except (ValueError, TypeError, IndexError):
        raise SystemExit(1)
