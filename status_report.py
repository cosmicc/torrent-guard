"""Cached web diagnostics and independent, bounded monitoring history.

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
import sqlite3
import subprocess
import sys
import time
import uuid

from scripts.watch_logs import safe_text

RETENTION = 7 * 86400
ACTIVITY_LIMIT = 100
ISSUE_WINDOW_SECONDS = 24 * 3600
ISSUE_EVENT_LIMIT = 1000
ISSUE_GROUP_LIMIT = 100


def filename_only(value: str) -> str:
    """Keep the human filename while omitting filesystem directory prefixes."""
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def compact_paths(message: str) -> str:
    """Shorten quoted and bare filesystem paths without interpreting log literals."""
    quoted = re.compile(r"'([^']*)'|\"([^\"]*)\"")

    def replace(match):
        value = match[1] if match[1] is not None else match[2]
        if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
            return "'" + filename_only(value) + "'"
        return match[0]

    message = quoted.sub(replace, message)
    # Relative quarantine names are diagnostic tokens, not filesystem locations.
    message = re.sub(r"[^\s;'\"]*/(\.torrent-guard-[0-9a-f]+)/payload", r"\1 (payload)", message)
    return re.sub(r"(?<![\w:/])/(?:[^\s;'\"]*/)+([^/\s;'\"]+)", r"\1", message)


def issue_details(message: str) -> dict:
    """Translate known emitted decisions into bounded, safe operator explanations."""
    from scripts.watch_logs import NAME
    name = NAME.search(message)
    download = name["value"][1:-1] if name else ""
    filename = ""
    reason = message
    if message.startswith("ORPHAN deferred ") and "; timer reset:" in message:
        path, reason = message[len("ORPHAN deferred "):].split("; timer reset:", 1)
        filename = filename_only(path)
    else:
        reason_match = re.search(r"\breason=(.*)", message)
        if reason_match:
            reason = reason_match[1]
        elif name:
            reason = message[name.end():]
            reason = re.sub(r"\b(?:paths|save_path|content_path|download_path)=('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", "", reason)
            reason = reason.lstrip(" :;") or message[:name.start()].strip()
    category, title, action = "Other", "Operation needs attention", "Review the details; Guard preserves downloads when checks are inconclusive."
    if "quarantined object changed" in message or "source reappeared after quarantine" in message:
        category, title = "Quarantine", "Quarantined file retained for safety"
        action = "The captured file no longer matches its saved identity. Keep the payload and journal; review the saved evidence before manual recovery."
        reason = reason.split("; recover manually from", 1)[0]
        reason = re.sub(r"(device|inode|size|mtime_ns): saved=\d+, observed=\d+", r"\1 changed", reason).replace("mtime_ns", "modification time")
    elif message.startswith("ORPHAN deferred"):
        category, title = "Orphan cleanup", "Unowned file cleanup deferred"
        action = "Guard retained the file and reset its absence timer. It will retry after another 15 minutes of confirmed client absence."
    elif message.startswith("DEFER validation"):
        category, title = "File access", "File access check deferred"
        action = "Check download mount access and permissions. Guard retains the file and retries automatically."
    elif "conflicting Arr ownership" in message:
        category, title = "Ownership", "Sonarr/Radarr ownership is ambiguous"
        action = "Check the torrent category and tags. Guard skips it until ownership is unambiguous."
    elif message.startswith("REJECT candidate"):
        category, title = "Rejection", "Download rejected by a removal rule"
        action = "Guard must confirm the Arr blocklist/replacement request before removing this download."
    elif message.startswith("PENDING"):
        category, title = "Cleanup", "Cleanup is waiting for verification"
        action = "The operation is still pending. Guard must verify ownership, import evidence and file absence before reporting cleanup complete."
        if "blocklist unconfirmed" in message or "queue match" in message:
            category, title = "Arr", "Arr rejection is not confirmed"
            action = "Check the matching Arr queue item. Guard keeps the download until rejection is confirmed."
    elif message.startswith("Poll failed") or "HTTP " in message:
        category, title = "Service", "A service request failed"
        action = "Check service availability and configured API access. Guard will retry."
    retry = re.search(r"\bretry_in=(\d+)s\b", reason)
    attempt = re.search(r"\battempt=(\d+)\b", reason)
    reason = re.split(r";?\s*retry_in=|;?\s*retry_at=", reason, maxsplit=1)[0].strip(" ;")
    recovery = re.search(r"\.torrent-guard-[0-9a-f]+", message)
    return {"category": category, "title": title, "download": private_text(filename_only(download)) if download else "",
            "filename": private_text(filename), "reason": private_text(compact_paths(reason)), "action": action,
            "retry_seconds": int(retry[1]) if retry else None, "attempt": int(attempt[1]) if attempt else None,
            "recovery": recovery[0] if recovery else ""}

class DiagnosticError(Exception):
    """An intentionally public diagnostic without remote response content."""


def public_error(exc: Exception) -> str:
    """Expose diagnostic types and status codes, never raw API response bodies."""
    if isinstance(exc, DiagnosticError):
        return private_text(str(exc))
    if isinstance(exc, subprocess.TimeoutExpired):
        return "Check timed out; retrying automatically"
    if isinstance(exc, OSError) and exc.errno:
        return os.strerror(exc.errno)
    status = getattr(exc, "status", None)
    return f"HTTP {status}" if status is not None else type(exc).__name__ + "; check private audit logs"


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
            CREATE TABLE IF NOT EXISTS activity (at REAL NOT NULL, decision TEXT NOT NULL, message TEXT NOT NULL);
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
                decision = next((label for prefix, label in (
                    ("ACCEPT completed ", "Validated"), ("CLEANED ", "Cleaned"),
                    ("ORPHAN removed ", "Orphan file removed"),
                    ("DELETE requested ", "Deletion requested"), ("LEFTOVER cleanup attempted ", "Leftover cleanup attempted"),
                    ("RETIRED record:", "Record retired"), ("ARCHIVED record", "Record archived"),
                    ("WAIT import ", "Waiting for import"), ("CLEANUP scheduled ", "Cleanup scheduled"),
                    ("REJECT candidate ", "Rejection candidate"), ("DRY RUN:", "Dry run")
                ) if message.startswith(prefix)), None)
                if decision:
                    self.database.execute("INSERT INTO activity VALUES (?, ?, ?)",
                                          (record.created, decision, private_text(message)[:2000]))
                    self.database.execute("DELETE FROM activity WHERE rowid NOT IN (SELECT rowid FROM activity ORDER BY rowid DESC LIMIT ?)", (ACTIVITY_LIMIT,))
                self.database.executemany("INSERT INTO events VALUES (?, ?)",
                                          [(record.created, metric) for metric in metrics])
                if record.levelno >= logging.WARNING:
                    self.database.execute("INSERT INTO issues VALUES (?, ?, ?)",
                                          (record.created, logging.getLevelName(record.levelno), private_text(message)[:2000]))
                    for level in ("WARNING", "ERROR", "CRITICAL"):
                        self.database.execute("DELETE FROM issues WHERE level=? AND rowid NOT IN "
                                              "(SELECT rowid FROM issues WHERE level=? ORDER BY rowid DESC LIMIT ?)", (level, level, ISSUE_EVENT_LIMIT))
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


def probe_drive(path: Path, write: bool = False, match: Path | None = None) -> dict:
    """Exercise listing/statvfs and optionally a unique create/read/remove test.

    Only our exclusively created test file is removed. Completed write probes also
    require device/inode equality with the configured read-only completed mapping.
    """
    with directory_fd(path) as directory:
        if match is not None:
            with directory_fd(match) as expected:
                actual_info, expected_info = os.fstat(directory), os.fstat(expected)
                if (actual_info.st_dev, actual_info.st_ino) != (expected_info.st_dev, expected_info.st_ino):
                    raise DiagnosticError("cleanup alias does not match its download directory")
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
                    try:
                        os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise DiagnosticError("probe path reappeared after deletion; replacement retained")
                finally:
                    os.close(descriptor)
            writable = "create/write/fsync/read/remove OK"
        return {"readable": True, "write_tested": write, "delete_tested": write,
                "delete_confirmed": write, "free_bytes": free,
                "total_bytes": total, "detail": "Read/list OK; write: " + writable}


def check_drive(path: Path, write: bool = False, match: Path | None = None) -> dict:
    """Isolate slow filesystem probes from the report, with a 10-second limit."""
    result = subprocess.run([sys.executable, str(Path(__file__).absolute()),
                             "--drive", json.dumps([str(path), write, str(match) if match else None])],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        # Child output contains only our fixed diagnostic/OS error descriptions.
        raise DiagnosticError(result.stdout.strip() or "drive probe failed")
    return json.loads(result.stdout)


def history_data(path: Path, now: float) -> dict:
    """Read exact rolling windows and retained events in a consistent snapshot."""
    from scripts.watch_logs import NAME

    with read_database(path) as database:
        database.execute("BEGIN")
        metadata = dict(database.execute("SELECT key, value FROM metadata"))
        since = float(metadata["since"])
        if not math.isfinite(since) or since > now:
            raise DiagnosticError("History start is invalid; counts unavailable")
        windows = {}
        for hours in (6, 12, 24):
            counts = dict(database.execute("SELECT metric, count(*) FROM events WHERE at >= ? AND at <= ? GROUP BY metric",
                                           (now - hours * 3600, now)))
            windows[str(hours)] = {metric: counts.get(metric, 0) for metric in ("cleaned", "rejected", "warnings", "errors")}
            windows[str(hours)]["partial"] = since > now - hours * 3600
        issues = []
        # Preserve visibility of the last errors even during a warning burst.
        for condition in ("level='WARNING'", "level IN ('ERROR','CRITICAL')"):
            for level, message, first, last, count in database.execute(
                    f"SELECT level, message, min(at), max(at), count(*) FROM issues WHERE {condition} AND at >= ? AND at <= ? "
                    "GROUP BY level, message ORDER BY max(at) DESC, max(rowid) DESC LIMIT ?",
                    (now - ISSUE_WINDOW_SECONDS, now, ISSUE_GROUP_LIMIT)):
                details = issue_details(message)
                issues.append({"at": last, "first_at": first, "occurrences": count, "level": level,
                               "message": private_text(compact_paths(message)), **details})
        issues.sort(key=lambda item: item["at"], reverse=True)
        activity = []
        # v1.6 history remains readable before the daemon's additive migration.
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity'").fetchone():
            for at, decision, message in database.execute("SELECT at, decision, message FROM activity ORDER BY rowid DESC LIMIT ?", (ACTIVITY_LIMIT,)):
                name = NAME.search(message)
                activity.append({"at": at, "decision": decision, "name": private_text(name['value'][1:-1] if name else message[:120]),
                                 "detail": private_text(message)})
        return {"available": True, "since": since, "gap": metadata.get("gap"), "windows": windows,
                "started": float(metadata["started"]) if "started" in metadata else None,
                "snapshot": json.loads(metadata["snapshot"]) if "snapshot" in metadata else None,
                "issues": issues, "activity": activity}


def journal_data(path: Path, names: dict) -> dict:
    """Read bounded pending/retry details without constructing or migrating a journal."""
    with read_database(path) as database:
        database.execute("BEGIN")
        counts = {table: database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("pending", "filesystem_retry", "cleanup_retry", "metadata_wait", "stalled_wait")}
        pending = [{"name": private_text(name), "app": app, "phase": phase, "kind": kind, "reason": private_text(reason)}
                   for name, app, phase, kind, reason in database.execute(
                       "SELECT name, app, phase, kind, reason FROM pending ORDER BY rowid LIMIT 50")]
        holds = []
        for table, label in (("filesystem_retry", "Filesystem hold"), ("cleanup_retry", "Cleanup retry")):
            for h, name, attempt, next_at, reason in database.execute(
                    f"SELECT r.hash, p.name, r.attempts, r.next_at, r.reason FROM {table} r LEFT JOIN pending p ON r.hash=p.hash ORDER BY r.next_at LIMIT 25"):
                holds.append({"name": private_text(name or names.get(h) or "Retained download"), "type": label,
                              "attempt": attempt, "next_at": next_at, "reason": private_text(reason)})
        return {"available": True, "counts": counts, "pending": pending, "holds": holds}


def collect_status() -> dict:
    """Collect bounded diagnostics in an isolated process, never on an HTTP request."""
    from guard import ApiError, Config, Http, Qbit, VERSION, torrent_app

    config = Config.from_env()
    from maintenance import read_maintenance
    maintenance = read_maintenance(config.state_db)
    result = {"version": VERSION, "collected_at": time.time(), "services": [], "drives": [],
              "maintenance": maintenance,
              "settings": {"dry_run": config.dry_run, "cleanup_leftovers": config.cleanup_leftovers,
                           "orphan_cleanup": config.orphan_cleanup,
                           "import_grace_minutes": config.import_grace_minutes, "poll_seconds": config.poll_seconds,
                           "verify_video_content": config.verify_video_content, "uid": os.getuid(), "gid": os.getgid()}}
    names = {}
    try:
        client = Qbit(config)
        torrents = client.torrents()
        if not isinstance(torrents, list) or any(not isinstance(row, dict) for row in torrents):
            raise ApiError("Invalid torrent inventory")
        owned = [torrent for torrent in torrents if torrent_app(torrent, config)]
        names = {str(row.get("hash", "")).lower(): str(row.get("name", "Unnamed download")) for row in torrents}
        states = {}
        for row in owned:
            state = private_text(str(row.get("state", "unknown")))
            states[state] = states.get(state, 0) + 1
        result["services"].append({"name": "qBittorrent", "ok": True, "detail": "Authenticated · " + str(len(owned)) + " managed downloads",
                                   "total": len(torrents), "managed": len(owned), "states": states})
    except Exception as exc:
        # Diagnostic boundary: keep independent sections available on failure.
        result["services"].append({"name": "qBittorrent", "ok": False, "detail": public_error(exc)})
    for app in ("SONARR", "RADARR"):
        instance = config.instances.get(app)
        service = {"name": app.title(), "configured": instance is not None, "ok": None, "detail": "Not configured"}
        if instance and maintenance["active"]:
            service.update(detail="Maintenance: Sonarr/Radarr intentionally unavailable")
        elif instance:
            try:
                data = Http().request("GET", instance.url + "/api/v3/system/status", headers={"X-Api-Key": instance.api_key})
                if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"]:
                    raise ApiError("Invalid system status")
                service.update(ok=True, detail="Authenticated · v" + private_text(data["version"]))
            except Exception as exc:
                service.update(ok=False, detail=public_error(exc))
        result["services"].append(service)
    root = Path(config.guard_download_root)
    completed = root / PurePosixPath(config.qbit_complete_dir).relative_to(config.qbit_download_root)
    incomplete = root / PurePosixPath(config.qbit_incomplete_dir).relative_to(config.qbit_download_root)
    drives = [("Downloads mapping / free space", root, False, None),
              ("State", Path(config.state_db).absolute().parent, True, None)]
    if config.cleanup_leftovers:
        drives.append(("Completed legacy cleanup", Path(config.cleanup_write_root), True, completed))
    # Probe the exact four writable aliases used by orphan removal. Keeping the
    # parent mapping read-only does not prevent these scoped write/delete tests.
    # A missing override is a visible failed capability check, never a fake pass.
    orphan_aliases = set()
    for kind, mapped in (("complete", completed), ("incomplete", incomplete)):
        for media in ("tv", "movies"):
            alias = Path(config.orphan_write_root) / kind / media
            orphan_aliases.add(alias)
            drives.append((f"{kind.title()} {media.upper()} cleanup", alias, True, mapped / media))
    for name, path, write, match in drives:
        drive = {"name": name, "path": private_text(str(path)), "ok": False,
                 "required_capability": "read/write/delete" if write else "read/list/space"}
        try:
            drive.update(check_drive(path, write, match), ok=True)
        except Exception as exc:
            drive["detail"] = public_error(exc)
            if path in orphan_aliases:
                drive["detail"] += "; four-folder write/delete check failed. Ensure compose.orphans.yaml is included, recreate the container and check mount/UID permissions."
        result["drives"].append(drive)
    try:
        result["history"] = history_data(history_path(config.state_db), time.time())
    except Exception as exc:
        result["history"] = {"available": False, "detail": public_error(exc)}
    try:
        result["journal"] = journal_data(Path(config.state_db), names)
    except Exception as exc:
        result["journal"] = {"available": False, "detail": public_error(exc)}
    result["collected_at"] = time.time()
    return result


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--collect":
            print(json.dumps(collect_status(), allow_nan=False))
        elif sys.argv[1] == "--drive":
            location, writable, expected = json.loads(sys.argv[2])
            print(json.dumps(probe_drive(Path(location), writable, Path(expected) if expected else None)))
        else:
            raise SystemExit(2)
    except DiagnosticError as exc:
        print(str(exc))
        raise SystemExit(1)
    except OSError as exc:
        print(os.strerror(exc.errno) if exc.errno else "Filesystem operation failed")
        raise SystemExit(1)
    except (ValueError, TypeError, IndexError):
        raise SystemExit(1)
