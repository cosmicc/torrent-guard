#!/usr/bin/env python3
"""Torrent Guard: early malware-file rejection, completed-media validation and Arr-aware cleanup.

Uses the qBittorrent/Arr APIs. ffprobe reads downloads through a read-only mount;
qBittorrent is the primary deletion mechanism (deleteFiles=true). Optional
completed-download cleanup uses persisted file identities and a scoped write bind.
"""
from __future__ import annotations

import argparse
import errno
import http.cookiejar
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request

from deletion import FileVerifier, metadata_manifest, validate_metadata_manifest, zero_payload

LOG = logging.getLogger("torrent-guard")
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")
VERSION = "1.3"
METADATA_TIMEOUT_SECONDS = 3600
METADATA_STATES = frozenset({"metaDL", "forcedMetaDL"})
HEARTBEAT_FILE = Path("/tmp/torrent-guard-heartbeat")
DEFAULT_VIDEO = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".divx", ".flv"})
DEFAULT_RISKY = frozenset({".exe", ".scr", ".com", ".bat", ".cmd", ".msi", ".msp", ".msix", ".ps1", ".psm1", ".vbs", ".vbe", ".vb", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".pif", ".cpl", ".jar", ".apk", ".appx", ".reg", ".scf", ".sh", ".py"})


def display_time(epoch: float) -> str:
    """Render an instant in Eastern Time with its DST-aware name and UTC offset."""
    return datetime.fromtimestamp(epoch, DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z%z")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


def boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def api_url(value: str, name: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{name} must be a valid http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name}: do not put credentials, query or fragment in URL")
    return value


@dataclass(frozen=True)
class ArrInstance:
    url: str
    api_key: str
    category: str
    tag: str = ""


@dataclass(frozen=True)
class Config:
    qbit_url: str
    qbit_username: str
    qbit_password: str
    instances: dict[str, ArrInstance]
    categories: dict[str, str]
    dry_run: bool = True
    reject_no_video: bool = True
    poll_seconds: int = 5
    max_age_hours: float = 24.0
    state_db: str = "/state/torrent-guard.sqlite3"
    tags: dict[str, str] = field(default_factory=dict)
    verify_video_content: bool = True
    qbit_download_root: str = "/downloads"
    guard_download_root: str = "/downloads"
    qbit_complete_dir: str = ""  # blank only for legacy/test configurations
    qbit_incomplete_dir: str = ""  # API may show a temporary content_path here
    import_grace_minutes: int = 10
    import_check_seconds: int = 60
    history_lookback_days: int = 30
    completed_settle_seconds: int = 60
    ffprobe_timeout_seconds: int = 30
    cleanup_leftovers: bool = False
    cleanup_write_root: str = "/cleanup/completed"
    delete_settle_seconds: int = 120

    @classmethod
    def from_env(cls) -> "Config":
        instances, categories, tags = {}, {}, {}
        for app in ("SONARR", "RADARR"):
            url = os.getenv(f"{app}_URL", "").strip()
            key = os.getenv(f"{app}_API_KEY", "").strip()
            category = os.getenv(f"{app}_CATEGORY", "").strip()
            tag = os.getenv(f"{app}_TAG", "").strip().casefold()
            if any((url, key, category, tag)) and not all((url, key, category)):
                raise ValueError(f"Configure all {app}_URL, _API_KEY and _CATEGORY, or none; _TAG optional")
            if url:
                if category in categories or (tag and tag in tags):
                    raise ValueError(f"Duplicate qBittorrent category or tag for {app}")
                instances[app] = ArrInstance(api_url(url, f"{app}_URL"), key, category, tag)
                categories[category] = app
                if tag:
                    tags[tag] = app
        if not instances:
            raise ValueError("Configure Sonarr and/or Radarr")
        poll = int(os.getenv("POLL_SECONDS", "5"))
        age = float(os.getenv("MAX_AGE_HOURS", "24"))
        grace = int(os.getenv("IMPORT_GRACE_MINUTES", "10"))
        check = int(os.getenv("IMPORT_CHECK_SECONDS", "60"))
        history = int(os.getenv("HISTORY_LOOKBACK_DAYS", "30"))
        settle = int(os.getenv("COMPLETED_SETTLE_SECONDS", "60"))
        timeout = int(os.getenv("FFPROBE_TIMEOUT_SECONDS", "30"))
        delete_settle = int(os.getenv("DELETE_SETTLE_SECONDS", "120"))
        if delete_settle < 30:
            raise ValueError("DELETE_SETTLE_SECONDS must be at least 30")
        if poll < 2 or not 0 < age < float("inf") or grace < 10 or check < 10 or history < 1 or settle < 0 or timeout < 1:
            raise ValueError("Invalid time settings: poll>=2, age>0, grace>=10m, check>=10s, lookback>=1d, settle>=0, timeout>=1s")
        qroot = os.getenv("QBIT_DOWNLOAD_ROOT", "/downloads").rstrip("/")
        groot = os.getenv("GUARD_DOWNLOAD_ROOT", "/downloads").rstrip("/")
        if not qroot.startswith("/") or not groot.startswith("/") or qroot == "" or groot == "" or ".." in PurePosixPath(qroot).parts or ".." in PurePosixPath(groot).parts:
            raise ValueError("QBIT_DOWNLOAD_ROOT and GUARD_DOWNLOAD_ROOT must be absolute directories without '..'")
        def location(name: str, default: str) -> str:
            value = os.getenv(name, default).strip().rstrip("/")
            path = PurePosixPath(value)
            if not value or not path.is_absolute() or not path.is_relative_to(PurePosixPath(qroot)) or ".." in path.parts or value == qroot:
                raise ValueError(f"{name} must be a subdirectory of QBIT_DOWNLOAD_ROOT: {qroot}")
            return str(path)
        complete_dir = location("QBIT_COMPLETE_DIR", qroot + "/complete")
        incomplete_dir = location("QBIT_INCOMPLETE_DIR", qroot + "/incomplete")
        if PurePosixPath(complete_dir).is_relative_to(PurePosixPath(incomplete_dir)) or PurePosixPath(incomplete_dir).is_relative_to(PurePosixPath(complete_dir)):
            raise ValueError("QBIT_COMPLETE_DIR and QBIT_INCOMPLETE_DIR must not overlap")
        return cls(
            qbit_url=api_url(required("QBIT_URL"), "QBIT_URL"),
            qbit_username=required("QBIT_USERNAME"), qbit_password=required("QBIT_PASSWORD"),
            instances=instances, categories=categories, tags=tags,
            dry_run=boolean("DRY_RUN", True), reject_no_video=boolean("REJECT_NO_VIDEO", True),
            poll_seconds=poll, max_age_hours=age,
            state_db=os.getenv("STATE_DB", "/state/torrent-guard.sqlite3"),
            verify_video_content=boolean("VERIFY_VIDEO_CONTENT", True),
            qbit_download_root=qroot, guard_download_root=groot,
            qbit_complete_dir=complete_dir, qbit_incomplete_dir=incomplete_dir,
            import_grace_minutes=grace, import_check_seconds=check,
            history_lookback_days=history, completed_settle_seconds=settle,
            ffprobe_timeout_seconds=timeout,
            cleanup_leftovers=boolean("CLEANUP_LEFTOVERS", False),
            delete_settle_seconds=delete_settle,
        )


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, url: str, *, fields=None, headers=None, include_status=False):
        data = urllib.parse.urlencode(fields).encode("utf-8") if fields is not None else None
        req = urllib.request.Request(url, method=method, data=data, headers={"User-Agent": f"TorrentGuard/{VERSION}", **(headers or {})})
        try:
            with self.opener.open(req, timeout=12) as response:
                status = response.status
                payload = response.read()
        except urllib.error.HTTPError as exc:
            snippet = exc.read(250).decode("utf-8", "replace")
            endpoint = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
            raise ApiError(f"HTTP {exc.code} calling {method} {endpoint}: {snippet}", exc.code) from None
        stripped = payload.strip()
        if stripped.startswith((b"{", b"[")):
            result = json.loads(payload)
        else:
            result = payload.decode("utf-8", "replace")
        return (status, result) if include_status else result


class Qbit:
    def __init__(self, config: Config):
        self.config = config
        self.http = Http()
        self.login()

    def login(self):
        """Support qBittorrent <=5.1 (200/Ok.) and >=5.2 (204/empty).

        Never treat an arbitrary empty HTTP 200 response as success, and verify
        the session against a protected endpoint before proceeding.
        """
        referer = {"Referer": self.config.qbit_url + "/"}
        status, result = self.http.request(
            "POST", self.config.qbit_url + "/api/v2/auth/login",
            fields={"username": self.config.qbit_username, "password": self.config.qbit_password},
            headers=referer, include_status=True,
        )
        valid = (status == 200 and result == "Ok.") or (status == 204 and result == "")
        if not valid:
            raise ApiError(f"qBittorrent refused login: HTTP {status}, response {result!r}", status)
        try:
            version = self.http.request("GET", self.config.qbit_url + "/api/v2/app/version", headers=referer)
        except ApiError as exc:
            raise ApiError(f"qBittorrent login response was accepted, but session verification failed: {exc}", exc.status) from exc
        if not isinstance(version, str) or not version.strip() or not version.lstrip().startswith("v"):
            raise ApiError(f"qBittorrent login response was accepted, but version verification returned {version!r}")
        LOG.info("Authenticated to qBittorrent (%s; login HTTP %s)", version.strip(), status)

    def request(self, method: str, path: str, *, fields=None, query=None):
        url = self.config.qbit_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        def issue():
            return self.http.request(method, url, fields=fields, headers={"Referer": self.config.qbit_url + "/"})
        try:
            return issue()
        except ApiError as exc:
            if exc.status != 403:
                raise
            self.login()
            return issue()

    def torrents(self):
        return self.request("GET", "/api/v2/torrents/info", query={"filter": "all"})

    def files(self, torrent_hash: str):
        return self.request("GET", "/api/v2/torrents/files", query={"hash": torrent_hash})

    def stop(self, torrent_hash: str):
        try:
            self.request("POST", "/api/v2/torrents/stop", fields={"hashes": torrent_hash})
        except ApiError as exc:
            if exc.status != 404:
                raise
            self.request("POST", "/api/v2/torrents/pause", fields={"hashes": torrent_hash})

    def delete_with_data(self, torrent_hash: str):
        """qBittorrent deletes all of its associated download data, including partial files."""
        return self.request("POST", "/api/v2/torrents/delete", fields={"hashes": torrent_hash, "deleteFiles": "true"})


class ArrApi:
    def __init__(self, instance: ArrInstance, http: Http | None = None):
        self.instance = instance
        self.http = http or Http()

    @property
    def headers(self):
        return {"X-Api-Key": self.instance.api_key}

    def queue_by_hash(self) -> dict[str, int]:
        records = []
        for page in range(1, 52):
            query = urllib.parse.urlencode({"page": page, "pageSize": 200})
            data = self.http.request("GET", self.instance.url + "/api/v3/queue?" + query, headers=self.headers)
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ApiError("Unexpected Arr queue response")
            rows = data["records"]
            records.extend(rows)
            if not rows or len(records) >= int(data.get("totalRecords", len(records))):
                return {str(row["downloadId"]).lower(): int(row["id"]) for row in records if row.get("downloadId") and row.get("id") is not None}
        raise ApiError("Arr queue exceeds 10,000 records; refusing incomplete map")

    def blocklist_and_remove(self, queue_id: int):
        query = urllib.parse.urlencode({"removeFromClient": "false", "blocklist": "true", "skipRedownload": "false"})
        self.http.request("DELETE", self.instance.url + f"/api/v3/queue/{queue_id}?" + query, headers=self.headers)

    def imported_history(self, cutoff: float) -> dict[str, list[dict]]:
        """Fetch paged history in descending order; never claim import if history is incomplete."""
        imports: dict[str, list[dict]] = {}
        seen = 0
        for page in range(1, 52):
            query = urllib.parse.urlencode({"page": page, "pageSize": 200, "sortKey": "date", "sortDirection": "descending"})
            data = self.http.request("GET", self.instance.url + "/api/v3/history?" + query, headers=self.headers)
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ApiError("Unexpected Arr history response")
            rows = data["records"]
            total = data.get("totalRecords")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ApiError("Arr history response has invalid totalRecords")
            seen += len(rows)
            if seen > total or (seen < total and len(rows) != 200):
                raise ApiError("Arr history pagination is incomplete or changed; retrying later")
            for row in rows:
                date = parse_date(row.get("date"))
                if date is None:
                    raise ApiError("Arr history event has invalid date; cannot safely verify import")
                if date < cutoff:
                    continue
                event = str(row.get("eventType", "")).casefold()
                # Both Arr enums use 3 for DownloadFolderImported. Sonarr's 5
                # means EpisodeFileDeleted and must never authorize cleanup.
                if event not in ("downloadfolderimported", "3"):
                    continue
                download_id = str(row.get("downloadId") or "").lower()
                if download_id:
                    imports.setdefault(download_id, []).append(row)
            if seen == total:
                return imports
            # API sorted by date; page entirely older than cutoff.
            if parse_date(rows[-1].get("date")) < cutoff:
                return imports
        raise ApiError("Arr history exceeds 10,000 records in lookup window; refusing incomplete import verification")


def parse_date(raw) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.timestamp()
    except ValueError:
        return None


def selected_files(files: list[dict]) -> list[dict]:
    return [f for f in files if int(f.get("priority", 1)) != 0]


def normalized_name(name: str) -> str:
    """Account for qBittorrent's optional .!qB *on-disk* incomplete suffix.

    The Web API normally returns the original torrent filename without .!qB,
    but stripping the suffix defensively also handles API/adapter variations.
    This is for metadata decisions only; do not treat incomplete disk data as media.
    """
    clean = name.replace("\\", "/")
    return clean[:-4] if clean.casefold().endswith(".!qb") else clean


def effective_extension(name: str) -> str:
    return PurePosixPath(normalized_name(name)).suffix.casefold()


def video_files(files: list[dict]) -> list[dict]:
    return [f for f in selected_files(files) if effective_extension(str(f.get("name", ""))) in DEFAULT_VIDEO]


def classify(files: list[dict], *, reject_no_video: bool = True) -> str | None:
    """Empty file list means torrent metadata pending; ignore unchecked files."""
    if not files:
        return None
    active = selected_files(files)
    if not active:
        return "no selected files for download"
    risky = [str(f.get("name", "")) for f in active if effective_extension(str(f.get("name", ""))) in DEFAULT_RISKY]
    if risky:
        return "risky executable/script/shortcut: " + ", ".join(risky[:4])
    if reject_no_video and not video_files(files):
        return "no recognized video file (.mkv, .mp4, etc.)"
    return None


def torrent_app(torrent: dict, config: Config) -> str | None:
    """Support qBittorrent categories and/or explicitly configured tags; reject ambiguity."""
    cat = config.categories.get(torrent.get("category"))
    tagged = {config.tags[tag.strip().casefold()] for tag in str(torrent.get("tags") or "").split(",") if tag.strip().casefold() in config.tags}
    if len(tagged) > 1 or (cat and tagged and cat not in tagged):
        LOG.warning("Skipping conflicting Arr ownership %s category=%s tags=%s", download_context(torrent), torrent.get("category"), torrent.get("tags"))
        return None
    return cat or (next(iter(tagged)) if tagged else None)


def is_unstarted_download(torrent: dict) -> bool:
    """Recognize explicit zero-data queues and metadata-download waits.

    Missing counters are uncertainty, not zero. In particular, a resumed queue
    can own useful data even when its current-session transfer count is zero.
    """
    if torrent.get("state") not in {"queuedDL", "metaDL", "forcedMetaDL"}:
        return False
    return zero_payload(torrent)


def is_complete(torrent: dict) -> bool:
    """Require explicit completion metadata and no active move or recheck."""
    state = str(torrent.get("state") or "").casefold()
    return (
        float(torrent.get("progress") or 0) >= 0.999999
        and torrent.get("amount_left") is not None
        and int(torrent["amount_left"]) == 0
        and int(torrent.get("completion_on") or 0) > 0
        and bool(state)
        and state not in {"missingfiles", "moving", "error"}
        and not state.startswith("checking")
    )


class MediaValidator:
    """ffprobe selected video payloads; reads files only from an explicitly mapped root."""
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def readable(path: Path) -> None:
        """Exercise stat/open/read, preserving ESTALE and other filesystem errors."""
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EIO, "media path is not a regular readable file", str(path))
        with path.open("rb") as stream:
            stream.read(65536)

    def check_filesystem(self, torrent: dict, files: list[dict]) -> None:
        """Check mapped tracked paths without ffprobe, including incomplete torrents."""
        verifier = FileVerifier(self.config)
        manifest = verifier.snapshot(torrent, files)
        for item in manifest["items"]:
            if item["identity"] is not None:
                with verifier.parent(verifier.roots[item["root"]], item["path"],
                                     manifest["roots"][item["root"]]["identity"]) as (directory, name):
                    if directory is None:
                        raise FileNotFoundError("tracked file moved during access check")
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                            raise OSError(errno.EIO, "tracked path changed during access check")
                        os.read(descriptor, 65536)
                    finally:
                        os.close(descriptor)

    @staticmethod
    def unavailable(name: str, error) -> tuple[str, str]:
        """Turn access uncertainty into an explicit deferral, never bad-media evidence."""
        code = errno.errorcode.get(getattr(error, "errno", None), "I/O")
        return "defer", f"filesystem/validation unavailable ({code}) for {name}: {error}"

    def resolve(self, torrent: dict, file: dict) -> Path:
        root = Path(self.config.qbit_download_root)
        save = Path(str(torrent.get("save_path") or ""))
        relative = PurePosixPath(str(file.get("name") or "").replace("\\", "/"))
        if not save.is_absolute() or not relative.parts or relative.is_absolute() or ".." in relative.parts or ".." in PurePosixPath(str(save)).parts:
            raise ValueError("unsafe or missing download path")
        if self.config.qbit_complete_dir and not PurePosixPath(str(save)).is_relative_to(PurePosixPath(self.config.qbit_complete_dir)):
            raise ValueError(f"completed save_path {save} is not under configured final directory {self.config.qbit_complete_dir}; waiting for move")
        try:
            save_rel = save.relative_to(root)
        except ValueError as exc:
            raise ValueError("qBittorrent save_path is outside QBIT_DOWNLOAD_ROOT; check mapping") from exc
        mounted_root = Path(self.config.guard_download_root).resolve()
        candidate = (mounted_root / save_rel / str(relative)).resolve()
        if not candidate.is_relative_to(mounted_root):
            raise ValueError("download file path escapes read-only mount")
        if self.config.qbit_complete_dir:
            final_relative = Path(self.config.qbit_complete_dir).relative_to(root)
            if not candidate.is_relative_to(mounted_root / final_relative):
                raise ValueError("download file path escapes configured completed directory")
        return candidate

    def inspect(self, torrent: dict, files: list[dict]) -> tuple[str, str]:
        # qBittorrent's download_path is normally *configured* as the temporary
        # location even after completion, so only check the actual content_path.
        content_path = str(torrent.get("content_path") or "")
        if self.config.qbit_incomplete_dir and content_path and PurePosixPath(content_path).is_relative_to(PurePosixPath(self.config.qbit_incomplete_dir)):
            return "defer", f"qBittorrent content_path still under incomplete directory: {content_path}; waiting for final move"
        if self.config.qbit_complete_dir and content_path and PurePosixPath(content_path).is_absolute() and not PurePosixPath(content_path).is_relative_to(PurePosixPath(self.config.qbit_complete_dir)):
            return "defer", f"qBittorrent content_path is outside final directory: {content_path}; waiting for final move"
        videos = video_files(files)
        if not videos:
            return "reject", "completed torrent has no selected video files"
        for video in videos:
            name = str(video.get("name"))
            if name.casefold().endswith(".!qb"):
                return "defer", f"incomplete .!qB suffix still reported for {name}; waiting for rename/move"
            if int(video.get("size") or 0) <= 0 or float(video.get("progress", 0)) < 0.999999:
                return "defer", f"video has zero size or is not fully downloaded: {name}"
            try:
                path = self.resolve(torrent, video)
            except ValueError as exc:
                return "defer", f"media path mapping needs attention for {name}: {exc}"
            except OSError as exc:
                return self.unavailable(name, exc)
            try:
                self.readable(path)
            except OSError as exc:
                decision, detail = self.unavailable(name, exc)
                return decision, f"video not readable at {path}; {detail}"
            if not self.config.verify_video_content:
                continue  # still require final, existing, readable video files
            try:
                p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", str(path)], capture_output=True, text=True, timeout=self.config.ffprobe_timeout_seconds, check=False)
            except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError) as exc:
                return self.unavailable(name, exc)
            try:
                payload = json.loads(p.stdout or "{}")
                streams = payload.get("streams", []) if isinstance(payload, dict) else None
                if not isinstance(streams, list) or any(not isinstance(stream, dict) for stream in streams):
                    raise ValueError("unexpected ffprobe stream metadata")
                valid_stream = any(s.get("codec_type") == "video" and int(s.get("width") or 0) > 0 and int(s.get("height") or 0) > 0 for s in streams)
            except (ValueError, TypeError) as exc:
                return self.unavailable(name, exc)
            if p.returncode != 0 or not valid_stream:
                try:
                    # The mount can fail while ffprobe is running. Access errors
                    # and reported I/O failures cannot establish invalid media.
                    self.readable(path)
                except OSError as exc:
                    return self.unavailable(name, exc)
                io_errors = ("stale file handle", "input/output error", "i/o error", "permission denied",
                             "no such file", "connection timed out", "connection reset", "resource temporarily unavailable")
                if any(message in p.stderr.casefold() for message in io_errors):
                    return self.unavailable(name, p.stderr.strip()[:180])
                return "reject", f"ffprobe found no readable video stream: {name} ({p.stderr.strip()[:180]})"
        if not self.config.verify_video_content:
            return "accept", f"metadata-only: {len(videos)} final video file(s) present/readable (ffprobe DISABLED)"
        return "accept", f"ffprobe verified {len(videos)} complete video file(s)"


def import_confirmed(torrent: dict, files: list[dict], events: list[dict], now: float, grace_minutes: int) -> tuple[bool, str]:
    """Conservatively require each selected video to have a matching Arr import event."""
    videos = video_files(files)
    if not videos:
        return False, "no video to match"
    added = int(torrent.get("added_on") or 0)
    matchable = []
    for e in events:
        stamp = parse_date(e.get("date"))
        if stamp is None or stamp < added or stamp > now:
            continue
        data = e.get("data") or {}
        source = str(data.get("droppedPath") or data.get("sourcePath") or "").replace("\\", "/").casefold()
        matchable.append((source, stamp))
    if not matchable:
        return False, "no matching import event since torrent was added"
    wanted = [PurePosixPath(str(f.get("name")).replace("\\", "/")).name.casefold() for f in videos]
    if len(set(wanted)) != len(wanted):
        return False, "duplicate video basenames; cannot safely match all imports"
    imported_at = []
    for filename in wanted:
        # Single-video releases can use a same-hash import without a source path.
        stamps = [stamp for src, stamp in matchable
                  if (not src and len(videos) == 1) or PurePosixPath(src).name == filename]
        if stamps:
            imported_at.append(min(stamps))
    if len(imported_at) != len(wanted):
        if len(videos) == 1:
            return False, "import history points to a different video filename"
        return False, f"only {len(imported_at)}/{len(wanted)} video files confirmed imported"
    cleanup_after = max(imported_at) + grace_minutes * 60
    if now < cleanup_after:
        eligible = display_time(cleanup_after)
        return False, f"import confirmed for all {len(wanted)} video file(s); cleanup grace until {eligible}"
    return True, f"verified import history for all {len(wanted)} video files"


def download_context(torrent: dict, files: list[dict] | None = None, manifest: dict | None = None) -> str:
    """Describe API or saved evidence for operators without exposing infohashes.

    Paths are descriptive only; filesystem authorization never uses this text.
    Legacy journals may have a title but no recoverable path.
    """
    paths = []
    if manifest:
        paths = [str(PurePosixPath(item.get("root", "")) / item["path"])
                 for item in manifest.get("items", []) if item.get("path")]
    elif files and torrent.get("save_path"):
        paths = [str(PurePosixPath(str(torrent["save_path"])) / str(file["name"]))
                 for file in files if file.get("name")]
    if not paths:
        paths = [str(torrent[key]) for key in ("content_path", "save_path", "download_path") if torrent.get(key)]
    paths = list(dict.fromkeys(paths))
    shown = ", ".join(repr(path) for path in paths[:4]) or "unavailable in saved metadata"
    extra = f" (+{len(paths) - 4} more recorded paths)" if len(paths) > 4 else ""
    return f"name={str(torrent.get('name') or '<unnamed torrent>')!r} paths={shown}{extra}"


class PendingStore:
    """Durable cleanup journal and audit-independent decision state."""
    def __init__(self, filename: str):
        if filename != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        self.db = sqlite3.connect(filename)
        self.db.execute("""CREATE TABLE IF NOT EXISTS pending (
            hash TEXT PRIMARY KEY, app TEXT NOT NULL, name TEXT NOT NULL,
            reason TEXT NOT NULL, queue_id INTEGER, phase TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'reject'
        )""")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(pending)")}
        if "kind" not in columns:
            self.db.execute("ALTER TABLE pending ADD COLUMN kind TEXT NOT NULL DEFAULT 'reject'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS deletion_evidence (
            hash TEXT PRIMARY KEY, manifest TEXT NOT NULL, requested_at REAL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS deletion_audit (
            id INTEGER PRIMARY KEY, hash TEXT NOT NULL, completed_at REAL NOT NULL,
            manifest TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS filesystem_retry (
            hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
            next_at REAL NOT NULL, reason TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS cleanup_retry (
            hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
            next_at REAL NOT NULL, reason TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS metadata_wait (
            hash TEXT PRIMARY KEY, added_on REAL NOT NULL,
            app TEXT NOT NULL, first_seen REAL NOT NULL
        )""")
        self.db.commit()

    def metadata_waits(self) -> dict:
        """Restore observed metadata-wait timers without inferring torrent age."""
        return {row[0]: (row[1], row[2], row[3]) for row in self.db.execute(
            "SELECT hash,added_on,app,first_seen FROM metadata_wait")}

    def save_metadata_waits(self, waits: dict) -> None:
        """Atomically reconcile only the small timer table, preserving all journals."""
        with self.db:
            self.db.execute("DELETE FROM metadata_wait")
            self.db.executemany("INSERT INTO metadata_wait VALUES (?,?,?,?)",
                                [(h, *entry) for h, entry in waits.items()])

    def add(self, torrent_hash: str, app: str, name: str, reason: str, kind: str = "reject"):
        if kind not in {"reject", "imported", "metadata"}:
            raise ValueError("invalid journal kind")
        self.db.execute("INSERT OR IGNORE INTO pending (hash,app,name,reason,queue_id,phase,kind) VALUES (?,?,?,?,NULL,?,?)", (torrent_hash, app, name, reason, "delete" if kind == "imported" else "match", kind))
        self.db.commit()

    def set_phase(self, torrent_hash: str, phase: str, queue_id: int | None = None):
        self.db.execute("UPDATE pending SET phase=?,queue_id=COALESCE(?,queue_id) WHERE hash=?", (phase, queue_id, torrent_hash))
        self.db.commit()

    def remove(self, torrent_hash: str):
        self.db.execute("DELETE FROM pending WHERE hash=?", (torrent_hash,))
        self.db.execute("DELETE FROM deletion_evidence WHERE hash=?", (torrent_hash,))
        self.db.execute("DELETE FROM cleanup_retry WHERE hash=?", (torrent_hash,))
        self.db.commit()

    def evidence(self, torrent_hash: str):
        """Load saved file evidence independently of the pending tuple contract."""
        row = self.db.execute("SELECT manifest,requested_at FROM deletion_evidence WHERE hash=?", (torrent_hash,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def save_manifest(self, torrent_hash: str, manifest: dict):
        """Commit file identities or quarantine progress before destructive calls."""
        self.db.execute("""INSERT INTO deletion_evidence(hash,manifest) VALUES (?,?)
            ON CONFLICT(hash) DO UPDATE SET manifest=excluded.manifest""", (torrent_hash, json.dumps(manifest)))
        self.db.commit()

    def requested(self, torrent_hash: str, now: float):
        """Persist when an API deletion was attempted, including restart recovery."""
        self.db.execute("UPDATE deletion_evidence SET requested_at=? WHERE hash=?", (now, torrent_hash))
        self.db.commit()

    def finish(self, torrent_hash: str, now: float):
        """Atomically archive verified evidence and clear pending work."""
        with self.db:
            self.db.execute("""INSERT INTO deletion_audit(hash,completed_at,manifest)
                SELECT hash,?,manifest FROM deletion_evidence WHERE hash=?""", (now, torrent_hash))
            self.db.execute("DELETE FROM pending WHERE hash=?", (torrent_hash,))
            self.db.execute("DELETE FROM deletion_evidence WHERE hash=?", (torrent_hash,))
            self.db.execute("DELETE FROM cleanup_retry WHERE hash=?", (torrent_hash,))

    def entries(self):
        return self.db.execute("SELECT hash,app,name,reason,queue_id,phase,kind FROM pending ORDER BY rowid").fetchall()

    def hashes(self):
        return {row[0] for row in self.db.execute("SELECT hash FROM pending")}

    def filesystem_retries(self):
        """Load unresolved access holds so a restart cannot bypass retention."""
        return {row[0]: (row[1], row[2], row[3]) for row in self.db.execute(
            "SELECT hash,attempts,next_at,reason FROM filesystem_retry")}

    def set_filesystem_retry(self, h: str, attempt: int, next_at: float, reason: str):
        """Persist bounded backoff independently of cleanup authorization."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO filesystem_retry VALUES (?,?,?,?)", (h, attempt, next_at, reason))

    def clear_filesystem_retry(self, h: str):
        """Release a hold only after an actual filesystem recovery check."""
        with self.db:
            self.db.execute("DELETE FROM filesystem_retry WHERE hash=?", (h,))

    def cleanup_retries(self):
        """Restore cleanup deadlines without changing existing journal evidence."""
        return {row[0]: (row[1], row[2], row[3]) for row in self.db.execute(
            "SELECT hash,attempts,next_at,reason FROM cleanup_retry")}

    def set_cleanup_retry(self, h: str, attempt: int, next_at: float, reason: str):
        """Checkpoint automatic retries so restarts cannot create a busy loop."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cleanup_retry VALUES (?,?,?,?)", (h, attempt, next_at, reason))


class Guard:
    def __init__(self, config: Config, qbit=None, arr_clients=None, store=None, validator=None, verifier=None):
        self.config = config
        self.qbit = qbit if qbit is not None else Qbit(config)
        self.arr_clients = arr_clients if arr_clients is not None else {name: ArrApi(inst) for name, inst in config.instances.items()}
        self.store = store if store is not None else PendingStore(config.state_db)
        self.validator = validator if validator is not None else MediaValidator(config)
        self.verifier = verifier if verifier is not None else FileVerifier(config)
        self.cleanup_messages: dict[str, tuple[str, float]] = {}
        self.log_context: dict[str, str] = {}
        self.cleanup_retries = self.store.cleanup_retries()
        self.filesystem_retries = self.store.filesystem_retries()
        self.metadata_waits = self.store.metadata_waits()
        self.metadata_passed: set[str] = set()
        self.completed_passed: set[str] = set()
        self.logged_dry: set[str] = set()
        self.logged_import_dry: set[str] = set()
        self.warned_missing: set[str] = set()
        self.warned_defer: dict[str, str] = {}
        self.import_wait_reasons: dict[str, str] = {}
        self.import_candidates: set[str] = set()
        self.import_grace_waits: set[str] = set()
        self.last_import_check: float = 0.0

    def poll(self, now: float | None = None):
        now = time.time() if now is None else now
        torrents = self.qbit.torrents()
        if not isinstance(torrents, list):
            raise ApiError("Unexpected qBittorrent torrent-list response")
        by_hash = {str(t.get("hash", "")).lower(): t for t in torrents if t.get("hash")}
        self._track_metadata_waits(by_hash, now)
        pending = self.store.hashes()
        self.log_context = {h: context for h, context in self.log_context.items() if h in by_hash or h in pending}
        self.log_context.update({h: download_context(t) for h, t in by_hash.items()})
        self.metadata_passed.intersection_update(by_hash)
        self.completed_passed.intersection_update(by_hash)
        self.logged_dry.intersection_update(by_hash)
        self.logged_import_dry.intersection_update(by_hash)
        self.warned_defer = {h: msg for h, msg in self.warned_defer.items() if h in by_hash}
        self.import_wait_reasons = {h: msg for h, msg in self.import_wait_reasons.items() if h in by_hash}
        candidates: dict[str, tuple[str, str, dict, list[dict]]] = {}
        for h, torrent in by_hash.items():
            app = torrent_app(torrent, self.config)
            if not app or h in pending:
                continue
            if not self._filesystem_ready(h, torrent, now):
                continue
            name = str(torrent.get("name") or "<unnamed torrent>")
            wait = self.metadata_waits.get(h)
            if wait and now - wait[2] > METADATA_TIMEOUT_SECONDS:
                try:
                    files = self.qbit.files(h)
                    if files != [] or not zero_payload(torrent):
                        raise ValueError("metadata timeout has payload or inconclusive file metadata; preserving download")
                    self._reject(h, app, name, "metadata download exceeded one hour of observed waiting", torrent, kind="metadata")
                except (ApiError, ValueError, OSError) as exc:
                    message = str(exc)
                    if self.warned_defer.get(h) != message:
                        LOG.warning("WAIT metadata timeout %s: %s", self.log_context[h], message)
                        self.warned_defer[h] = message
                continue
            age = now - int(torrent.get("added_on") or 0)
            fresh = 0 <= age <= self.config.max_age_hours * 3600
            complete = is_complete(torrent)
            if not fresh and not complete:
                continue
            try:
                need_metadata = fresh and h not in self.metadata_passed
                need_complete = complete and h not in self.completed_passed and now - int(torrent["completion_on"]) >= self.config.completed_settle_seconds
                need_import = complete and h in self.completed_passed
                if not (need_metadata or need_complete or need_import):
                    continue
                files = self.qbit.files(h)
                if not isinstance(files, list):
                    raise ApiError("Unexpected qBittorrent file-list response")
                if not files:
                    label = "metadata/queue; no payload downloaded" if is_unstarted_download(torrent) else "metadata"
                    LOG.debug("WAIT %s [%s] %s", label, app, self.log_context[h])
                    continue
                self.log_context[h] = download_context(torrent, files)
                reason = classify(files, reject_no_video=self.config.reject_no_video)
                if reason:
                    self.validator.check_filesystem(torrent, files)
                    self._reject(h, app, name, reason, torrent)
                    continue
                if h not in self.metadata_passed:
                    self.metadata_passed.add(h)
                    LOG.info("ACCEPT metadata [%s] %s category=%r tags=%r selected_video=%d", app, self.log_context[h], torrent.get("category"), torrent.get("tags"), len(video_files(files)))
                if need_complete:
                    result, detail = self.validator.inspect(torrent, files)
                    if result == "reject":
                        self._reject(h, app, name, "completed payload invalid: " + detail, torrent)
                        continue
                    if result == "defer":
                        self._defer_filesystem(h, detail, now)
                        continue
                    self.completed_passed.add(h)
                    LOG.info("ACCEPT completed [%s] %s: %s; checking Arr import history", app, self.log_context[h], detail)
                if complete and h in self.completed_passed:
                    candidates[h] = (app, name, torrent, files)
            except OSError as exc:
                self._defer_filesystem(h, str(exc), now)
            except (ApiError, ValueError, sqlite3.Error) as exc:
                LOG.error("Inspection failed [%s] %s: %s", app, self.log_context[h], exc)

        if candidates and (not self.last_import_check or now - self.last_import_check >= self.config.import_check_seconds):
            self.last_import_check = now
            since = now - self.config.history_lookback_days * 86400
            for app in {v[0] for v in candidates.values()}:
                try:
                    imports = self.arr_clients[app].imported_history(since)
                except (ApiError, ValueError, OSError, urllib.error.URLError) as exc:
                    LOG.error("Import history failed [%s]; preserving downloads: %s", app, exc)
                    continue
                matching = [h for h, candidate in candidates.items() if candidate[0] == app]
                LOG.info("CHECK import [%s] completed_downloads=%d matching_history=%d interval=%ds",
                         app, len(matching), sum(bool(imports.get(h)) for h in matching),
                         self.config.import_check_seconds)
                for h, (own_app, name, torrent, files) in candidates.items():
                    if own_app != app or h in pending:
                        continue
                    if int(torrent.get("added_on") or 0) < since:
                        self.import_grace_waits.discard(h)
                        reason = "torrent predates configured history lookback; manual review required"
                        if self.import_wait_reasons.get(h) != reason:
                            LOG.info("WAIT import [%s] %s: %s", app, self.log_context[h], reason)
                            self.import_wait_reasons[h] = reason
                        continue
                    yes, reason = import_confirmed(torrent, files, imports.get(h, []), now, self.config.import_grace_minutes)
                    if not yes:
                        # Separate complete import evidence from missing/partial
                        # evidence without relying on human-readable reason text.
                        if import_confirmed(torrent, files, imports.get(h, []), now, 0)[0]:
                            self.import_grace_waits.add(h)
                        else:
                            self.import_grace_waits.discard(h)
                        if self.import_wait_reasons.get(h) != reason:
                            LOG.info("WAIT import [%s] %s: %s", app, self.log_context[h], reason)
                            self.import_wait_reasons[h] = reason
                        continue
                    self.import_wait_reasons.pop(h, None)
                    self.import_grace_waits.discard(h)
                    if self.config.dry_run:
                        if h not in self.logged_import_dry:
                            LOG.info("DRY RUN: ACCEPT imported [%s] %s: %s; would delete torrent + qBit data", app, self.log_context[h], reason)
                            self.logged_import_dry.add(h)
                    else:
                        LOG.info("CLEANUP scheduled [%s] %s: imported at least %dm ago; %s", app, self.log_context[h], self.config.import_grace_minutes, reason)
                        self.store.add(h, app, name, reason, kind="imported")
                        pending.add(h)
        if not self.config.dry_run:
            self._process_pending(by_hash, now)
        self.import_candidates = set(candidates) - self.store.hashes() - self.logged_import_dry
        self.import_grace_waits.intersection_update(self.import_candidates)

    def log_status(self) -> None:
        """Report numeric work counts from the last completed poll, read-only.

        Pending deletion includes all journal phases; verification and retry
        counts are subsets and may overlap. Import counts are last observed
        evidence, not dependency health or deletion authorization.
        """
        entries = self.store.entries()
        pending = {row[0] for row in entries}
        waiting = self.import_candidates - pending
        grace = self.import_grace_waits & waiting
        LOG.info(
            "STATUS pending_deletion=%d verifying_deletion=%d waiting_for_import=%d "
            "import_grace=%d metadata_waits=%d filesystem_holds=%d cleanup_retries=%d",
            len(pending), sum(row[5] == "verify" for row in entries),
            len(waiting - grace), len(grace), len(set(self.metadata_waits) - pending),
            len(self.filesystem_retries), len(self.cleanup_retries),
        )

    def _rejection_ready(self, h: str, torrent: dict, files: list, now: float) -> bool:
        """Require current rejection evidence; a journal reason alone is insufficient."""
        if not isinstance(files, list) or not files:
            return False
        if classify(files, reject_no_video=self.config.reject_no_video):
            return True
        if (not is_complete(torrent)
                or now - int(torrent["completion_on"]) < self.config.completed_settle_seconds):
            return False
        result, detail = self.validator.inspect(torrent, files)
        if result == "defer":
            self._defer_filesystem(h, detail, now)
        return result == "reject"

    def _track_metadata_waits(self, by_hash: dict, now: float) -> None:
        """Track owned metadata states across polls/restarts; dry run is memory-only.

        Reset on observed state exit, disappearance, owner/incarnation changes or
        clock rollback. API outages provide no evidence of a state transition.
        """
        waits = {}
        for h, torrent in by_hash.items():
            app = torrent_app(torrent, self.config)
            added = torrent.get("added_on")
            if (not app or torrent.get("state") not in METADATA_STATES
                    or isinstance(added, bool) or not isinstance(added, (int, float))
                    or not 0 < added <= now):
                continue
            previous = self.metadata_waits.get(h)
            if previous is None or previous[:2] != (added, app) or now < previous[2]:
                waits[h] = (added, app, now)
                LOG.info("WAIT metadata [%s] %s; one-hour timeout starts now", app, download_context(torrent))
            else:
                waits[h] = previous
        if waits != self.metadata_waits and not self.config.dry_run:
            self.store.save_metadata_waits(waits)
        self.metadata_waits = waits

    def _metadata_removal_ready(self, h: str, torrent: dict, files: list) -> None:
        """Persist empty-payload evidence before stopping/blocklisting a magnet."""
        if files != []:
            raise ValueError("metadata recovered or file list unavailable; preserving download for review")
        manifest = metadata_manifest(torrent)
        saved = self.store.evidence(h)
        if saved is not None:
            validate_metadata_manifest(saved[0])
            if (saved[0]["torrent"].get("added_on") != torrent.get("added_on")
                    or str(saved[0]["torrent"].get("hash", "")).lower() != h
                    or torrent_app(saved[0]["torrent"], self.config) != torrent_app(torrent, self.config)):
                raise ValueError("metadata torrent incarnation changed; preserving download")
        else:
            self.store.save_manifest(h, manifest)

    def _reject(self, h: str, app: str, name: str, reason: str, torrent: dict, kind: str = "reject"):
        if h in self.filesystem_retries:
            return  # An unresolved access failure overrides filename-based rejection.
        if h not in self.logged_dry:
            LOG.warning("REJECT candidate [%s] %s category=%r tags=%r reason=%s", app, self.log_context[h], torrent.get("category"), torrent.get("tags"), reason)
        if self.config.dry_run:
            if h not in self.logged_dry:
                LOG.warning("DRY RUN: would stop, Arr-blocklist and delete torrent + tracked files: %s", self.log_context.get(h, download_context(torrent)))
            self.logged_dry.add(h)
        else:
            self.store.add(h, app, name, reason, kind=kind)

    def _defer_filesystem(self, h: str, reason: str, now: float):
        """Retain the torrent and schedule 30/60/120/240/300-second retries."""
        attempt = self.filesystem_retries.get(h, (0, 0, ""))[0] + 1
        delay = min(300, 30 * 2 ** min(attempt - 1, 4))
        self.filesystem_retries[h] = (attempt, now + delay, reason)
        self.completed_passed.discard(h)
        if not self.config.dry_run:
            self.store.set_filesystem_retry(h, attempt, now + delay, reason)
        # Both independent holds must expire. Report the effective eligibility
        # time, not an earlier filesystem deadline hidden by cleanup backoff.
        next_at = max(now + delay, self.cleanup_retries.get(h, (0, 0, ""))[1])
        retry_at = display_time(next_at)
        LOG.warning("DEFER validation %s reason=%s; retry_in=%ds attempt=%d retry_at=%s",
                    self.log_context.get(h, "download metadata unavailable"), reason,
                    math.ceil(next_at - now), attempt, retry_at)

    def _clear_filesystem(self, h: str):
        """Clear retry state after recovery without changing dry-run journals."""
        self.filesystem_retries.pop(h, None)
        if not self.config.dry_run:
            self.store.clear_filesystem_retry(h)

    def _filesystem_ready(self, h: str, torrent: dict | None, now: float) -> bool:
        """A completion/state change never bypasses an unresolved filesystem hold."""
        retry = self.filesystem_retries.get(h)
        if retry is None:
            return True
        if now < retry[1]:
            return False
        if torrent is None:
            return True  # Saved-path verification below must establish recovery.
        try:
            files = self.qbit.files(h)
            self.validator.check_filesystem(torrent, files)
            if is_complete(torrent):
                result, reason = self.validator.inspect(torrent, files)
                if result == "defer":
                    self._defer_filesystem(h, reason, now)
                    return False
            self._clear_filesystem(h)
            LOG.info("Filesystem access recovered %s; resuming inspection", self.log_context.get(h, "download metadata unavailable"))
            return True
        except (ApiError, ValueError, OSError) as exc:
            self._defer_filesystem(h, str(exc), now)
            return False

    def _import_cleanup_ready(self, torrent: dict, h: str, app: str, now: float,
                              history_cache: dict[str, dict], files: list | None = None) -> bool:
        """Revalidate current evidence before every imported deletion attempt.

        A persisted decision is not permission to delete changed or moved data.
        This path never consults or mutates the Arr queue or blocklist.
        """
        since = now - self.config.history_lookback_days * 86400
        added = int(torrent.get("added_on") or 0)
        if (not is_complete(torrent) or not 0 < added <= now or added < since
                or now - int(torrent["completion_on"]) < self.config.completed_settle_seconds):
            return False
        files = self.qbit.files(h) if files is None else files
        if not isinstance(files, list):
            raise ApiError("Unexpected qBittorrent file-list response")
        if not files or classify(files, reject_no_video=self.config.reject_no_video):
            return False
        result, detail = self.validator.inspect(torrent, files)
        if result != "accept":
            if result == "defer":
                self._defer_filesystem(h, detail, now)
            else:
                LOG.warning("PENDING imported media not ready [%s] %s: %s", app, self.log_context.get(h), detail)
            return False
        if app not in history_cache:
            history_cache[app] = self.arr_clients[app].imported_history(since)
        confirmed, _ = import_confirmed(
            torrent, files, history_cache[app].get(h, []), now,
            self.config.import_grace_minutes,
        )
        return confirmed

    def _request_deletion(self, h: str, torrent: dict, now: float, metadata_only: bool = False,
                          kind: str = "reject"):
        """Persist all tracked file evidence before requesting API deletion."""
        current = self.qbit.torrents()
        if not isinstance(current, list):
            raise ApiError("Cannot refresh torrent before deletion snapshot")
        fresh = next((row for row in current if str(row.get("hash", "")).lower() == h), None)
        fields = ("added_on", "save_path", "download_path", "content_path", "progress", "amount_left", "completion_on", "state", "category", "tags")
        if fresh is None or any(fresh.get(key) != torrent.get(key) for key in fields):
            raise ValueError("torrent moved or changed before deletion; retrying inspection")
        evidence = self.store.evidence(h)
        files = self.qbit.files(h)
        if kind == "imported":
            app = torrent_app(fresh, self.config)
            if not app or not self._import_cleanup_ready(fresh, h, app, now, {}, files=files):
                raise ValueError("current payload lacks verified import evidence; preserving download")
        if not metadata_only and kind == "reject":
            self.validator.check_filesystem(fresh, files)
            if not self._rejection_ready(h, fresh, files, now):
                raise ValueError("current payload is not verified as a bad release; preserving download")
        if metadata_only:
            self._metadata_removal_ready(h, fresh, files)
            manifest = self.store.evidence(h)[0]
        elif evidence is None:
            manifest = self.verifier.snapshot(torrent, files)
            self.store.save_manifest(h, manifest)
        else:
            manifest = evidence[0]
            if torrent.get("added_on") != manifest["torrent"].get("added_on"):
                raise ValueError("torrent incarnation changed; manual review required")
            if any(torrent.get(key) != manifest["torrent"].get(key)
                   for key in ("save_path", "download_path", "content_path")):
                raise ValueError("torrent download paths changed after snapshot; manual review required")
            if not isinstance(files, list) or {f.get("name") for f in files} != {f.get("name") for f in manifest["files"]}:
                raise ValueError("tracked file list changed after deletion snapshot; manual review required")
        self.store.requested(h, now)
        self.qbit.delete_with_data(h)
        self.store.set_phase(h, "verify")

    def _cleanup_status(self, h: str, message: str, now: float, level: int = logging.WARNING):
        """Repeat unresolved status at most once a minute unless the reason changes."""
        previous, stamp = self.cleanup_messages.get(h, ("", 0.0))
        if previous != message or now - stamp >= 60:
            LOG.log(level, "PENDING disk verification %s: %s", self.log_context.get(h, "download metadata unavailable"), message)
            self.cleanup_messages[h] = (message, now)

    def _defer_cleanup(self, h: str, message: str, now: float):
        """Retry unresolved cleanup automatically at 60/120/240/480/900 seconds."""
        attempt = self.cleanup_retries.get(h, (0, 0, ""))[0] + 1
        delay = min(900, 60 * 2 ** min(attempt - 1, 4))
        self.store.set_cleanup_retry(h, attempt, now + delay, message)
        self.cleanup_retries[h] = (attempt, now + delay, message)
        deadline = display_time(now + delay)
        self._cleanup_status(h, f"{message}; retry_in={delay}s attempt={attempt} retry_at={deadline}", now, logging.WARNING)

    def _ownership_paths(self, cache: dict) -> set[tuple[str, str]]:
        """Share failed ownership checks per poll; refresh before each removal.

        Zero-data queued or metadata downloads may lack file metadata. Known
        content paths still reserve their scope, even when the file list is empty.
        All other missing file lists remain inconclusive.
        """
        if "error" in cache:
            error_type, message = cache["error"]
            raise error_type(message)
        live = self.qbit.torrents()
        if not isinstance(live, list):
            raise ApiError("Cannot establish active torrent ownership")
        paths = set()
        queued_content = []
        for torrent in live:
            try:
                torrent_hash = str(torrent.get("hash") or "").lower()
                files = self.qbit.files(torrent_hash)
                if files == [] and is_unstarted_download(torrent) and torrent_hash not in self.filesystem_retries:
                    # An unstarted magnet may not have a content path yet. Do
                    # not infer one from its title or require a directory to exist.
                    if torrent.get("content_path"):
                        content = PurePosixPath(str(torrent["content_path"]))
                        if not content.is_absolute() or ".." in content.parts:
                            raise ValueError("unsafe queued content path")
                        queued_content.append(content)
                    LOG.debug("WAIT queued %s; no data downloaded or file metadata yet", download_context(torrent))
                    continue
                paths.update((item["root"], item["path"]) for item in self.verifier.paths(torrent, files))
            except (ApiError, ValueError, OSError) as exc:
                # Retain the underlying I/O error type for filesystem holds.
                message = f"ownership check blocked by {download_context(torrent)}: {exc}"
                error_type = OSError if isinstance(exc, OSError) else ValueError
                cache["error"] = (error_type, message)
                raise error_type(message) from exc
        cache["hashes"] = {str(t.get("hash", "")).lower() for t in live}
        cache["queued_content"] = queued_content
        return paths

    def _verify_deletion(self, h: str, app: str, kind: str, now: float, ownership_cache: dict | None = None):
        """Confirm disk absence, or remove only authorized unchanged leftovers."""
        evidence = self.store.evidence(h)
        if evidence is None or evidence[1] is None:
            raise ValueError("no saved pre-deletion file evidence; manual review required")
        manifest, requested_at = evidence
        if kind == "metadata":
            validate_metadata_manifest(manifest)
            if str(manifest["torrent"].get("hash", "")).lower() != h:
                raise ValueError("metadata audit hash mismatch; manual review required")
            self.store.finish(h, now)
            self.cleanup_retries.pop(h, None)
            self.cleanup_messages.pop(h, None)
            LOG.info("CLEANED [%s] %s zero-payload metadata torrent absent; no filesystem targets were inferred", app, self.log_context.get(h))
            return True
        remaining = self.verifier.remaining(manifest)
        if remaining and now - requested_at >= self.config.delete_settle_seconds and self.config.cleanup_leftovers:
            # Refresh remote state before local cleanup, including unrelated torrents.
            ownership_cache = {} if ownership_cache is None else ownership_cache
            owned_paths = self._ownership_paths(ownership_cache)
            if h in ownership_cache["hashes"]:
                raise ValueError("torrent reappeared; local removal deferred")
            if kind == "imported":
                since = now - self.config.history_lookback_days * 86400
                original = manifest["torrent"]
                if int(original.get("added_on") or 0) < since:
                    raise ValueError("saved import authorization is outside history lookback")
                history = self.arr_clients[app].imported_history(since)
                yes, reason = import_confirmed(original, manifest["files"], history.get(h, []), now, self.config.import_grace_minutes)
                if not yes:
                    raise ValueError("local removal lacks current import evidence: " + reason)
            elif kind == "reject":
                if not self._rejection_ready(h, manifest["torrent"], manifest["files"], now):
                    raise ValueError("local removal lacks verified bad-release evidence; preserving files")
            wanted = {(item["root"], item["path"]) for item in manifest["items"]}
            for root, relative in wanted:
                target = PurePosixPath(root) / relative
                if any(target.is_relative_to(content) or content.is_relative_to(target)
                       for content in ownership_cache["queued_content"]):
                    raise ValueError(f"metadata/queued torrent reserves recorded download path {str(target)!r}")
            if owned_paths & wanted:
                overlap = sorted(owned_paths & wanted)
                raise ValueError(f"another torrent owns recorded download path {str(PurePosixPath(*overlap[0]))!r}")
            for item in remaining:
                target = str(PurePosixPath(item["root"]) / item["path"])
                try:
                    self.verifier.remove(manifest, item, lambda updated: self.store.save_manifest(h, updated))
                except OSError as exc:
                    raise OSError(exc.errno, exc.strerror or str(exc), target) from exc
                except ValueError as exc:
                    raise ValueError(f"leftover removal failed download={target!r}: {exc}") from exc
                LOG.warning("LEFTOVER cleanup attempted %s download=%r", self.log_context.get(h), str(PurePosixPath(item["root"]) / item["path"]))
            remaining = self.verifier.remaining(manifest)
        if remaining:
            paths = ", ".join(repr(str(PurePosixPath(item.get("root", "")) / item["path"])) for item in remaining[:4])
            if not self.config.cleanup_leftovers:
                blocker = "automatic leftover cleanup disabled (CLEANUP_LEFTOVERS=false); enable compose.cleanup.yaml with the completed-directory write bind"
            elif now - requested_at < self.config.delete_settle_seconds:
                deadline = display_time(requested_at + self.config.delete_settle_seconds)
                blocker = f"waiting for qBittorrent deletion settle period until {deadline}"
            else:
                blocker = "automatic cleanup ran but the filesystem still reports saved paths; review LEFTOVER messages and filesystem errors"
            self._cleanup_status(h, f"{len(remaining)} recorded file(s) remain: {paths}; {blocker}", now)
            self._clear_filesystem(h)
            return False
        self._clear_filesystem(h)
        self.store.finish(h, now)
        self.cleanup_retries.pop(h, None)
        self.warned_missing.discard(h)
        self.cleanup_messages.pop(h, None)
        LOG.info("CLEANED [%s] kind=%s %s torrent absent and all %d recorded file paths independently verified absent", app, kind, self.log_context.get(h), len(manifest["items"]))
        return True

    def _process_pending(self, by_hash: dict[str, dict], now: float):
        """Retry journal operations while keeping import and rejection paths separate."""
        queue_cache: dict[str, dict[str, int]] = {}
        history_cache: dict[str, dict] = {}
        ownership_cache: dict = {}
        for h, app, name, reason, queue_id, phase, kind in self.store.entries():
            if now < self.cleanup_retries.get(h, (0, 0, ""))[1]:
                continue
            self.log_context.setdefault(h, download_context({"name": name}))
            try:
                evidence = self.store.evidence(h)
                if evidence:
                    self.log_context[h] = download_context({**evidence[0].get("torrent", {}), "name": name}, manifest=evidence[0])
                if kind not in {"reject", "imported", "metadata"} or phase not in {"match", "arr", "delete", "verify"}:
                    LOG.error("PENDING invalid journal kind/phase; manual review required %s", self.log_context[h])
                    continue
                torrent = by_hash.get(h)
                if torrent and torrent_app(torrent, self.config) != app:
                    LOG.error("PENDING ownership changed; manual review required [%s] %s", app, self.log_context[h])
                    continue
                if not self._filesystem_ready(h, torrent, now):
                    continue
                if torrent is not None:
                    # Check access before stop/blocklist/API deletion, including
                    # metadata-only rejection of incomplete torrents.
                    current_files = self.qbit.files(h)
                    if kind == "metadata":
                        self._metadata_removal_ready(h, torrent, current_files)
                    else:
                        self.validator.check_filesystem(torrent, current_files)
                        if kind == "reject" and not self._rejection_ready(h, torrent, current_files, now):
                            self._cleanup_status(h, "current payload is not verified as a bad release; preserving download", now)
                            continue
                if kind == "imported":
                    if torrent is not None:
                        if not self._import_cleanup_ready(torrent, h, app, now, history_cache):
                            LOG.warning("PENDING import evidence incomplete; preserving download [%s] %s", app, self.log_context[h])
                            continue
                    # Repair legacy imported phases without ever calling Arr rejection.
                    if phase in {"match", "arr"}:
                        self.store.set_phase(h, "delete")
                        phase = "delete"
                if phase == "match":
                    if torrent is None:
                        LOG.error("PENDING disappeared before cleanup; cannot prove files were deleted: %s", self.log_context[h])
                        continue
                    self.qbit.stop(h)
                    if app not in queue_cache:
                        queue_cache[app] = self.arr_clients[app].queue_by_hash()
                    queue_id = queue_cache[app].get(h)
                    if queue_id is None:
                        if h not in self.warned_missing:
                            LOG.warning("PENDING no %s queue match yet (stopped; will retry): %s", app, self.log_context[h])
                            self.warned_missing.add(h)
                        continue
                    self.store.set_phase(h, "arr", queue_id)
                    phase = "arr"
                if phase == "arr":
                    # Never let Arr remove the torrent first; we explicitly request data deletion.
                    try:
                        self.arr_clients[app].blocklist_and_remove(queue_id)
                    except ApiError as exc:
                        if exc.status != 404:
                            raise
                        LOG.warning("PENDING Arr queue ID gone; blocklist unconfirmed, preserving download; manual review required [%s] %s", app, self.log_context[h])
                        continue
                    self.store.set_phase(h, "delete")
                    phase = "delete"
                if phase == "delete":
                    if torrent is None:
                        evidence = self.store.evidence(h)
                        if evidence is not None and evidence[1] is not None:
                            self.store.set_phase(h, "verify")
                            self._verify_deletion(h, app, kind, now, ownership_cache)
                        else:
                            self._cleanup_status(h, "torrent missing before saved delete request; manual review required", now)
                        continue
                    if kind in {"reject", "metadata"}:
                        self.qbit.stop(h)
                    self._request_deletion(h, torrent, now, metadata_only=kind == "metadata", kind=kind)
                    LOG.warning("DELETE requested [%s] kind=%s %s deleteFiles=true", app, kind, self.log_context[h])
                    continue
                if phase == "verify":
                    if torrent is not None:
                        self._request_deletion(h, torrent, now, metadata_only=kind == "metadata", kind=kind)
                        self._defer_cleanup(h, f"qBittorrent entry still visible [{app}]; deleteFiles=true requested again", now)
                        continue
                    self._verify_deletion(h, app, kind, now, ownership_cache)
            except OSError as exc:
                self._defer_filesystem(h, str(exc), now)
            except (ApiError, ValueError, sqlite3.Error) as exc:
                self._defer_cleanup(h, f"cleanup failed [{app}] kind={kind} phase={phase}: {exc}", now)


class ConsoleFormatter(logging.Formatter):
    """Color decision categories without allowing terminal escapes from API data."""

    def __init__(self, color: bool):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.color = color

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Keep console and audit timestamps Eastern regardless of the host zone."""
        return display_time(record.created)

    def format(self, record: logging.LogRecord) -> str:
        """Keep labels readable without color and sanitize external control codes."""
        rendered = super().format(record)
        rendered = "".join(char if char.isprintable() or char in "\n\t"
                           else ascii(char)[1:-1] for char in rendered)
        if not self.color:
            return rendered
        message = record.getMessage()
        if record.levelno >= logging.ERROR or message.startswith("REJECT"):
            code = "91"  # Bright red: errors and rejected releases.
        elif message.startswith("DRY RUN"):
            code = "95"  # Bright magenta: report-only actions.
        elif record.levelno >= logging.WARNING or message.startswith(("WAIT", "DEFER", "PENDING")):
            code = "93"  # Bright yellow: warnings, waiting, and retained downloads.
        elif message.startswith(("ACCEPT", "CLEANED")):
            code = "92"  # Bright green: accepted media and confirmed cleanup.
        else:
            code = "96"  # Bright cyan: normal activity and processing heartbeat.
        return "\n".join(f"\033[{code}m{line}\033[0m" for line in rendered.split("\n"))


def write_heartbeat(path: Path = HEARTBEAT_FILE) -> None:
    """Atomically record a completed poll using monotonic time, including dry runs."""
    temporary = path.with_suffix(".tmp")
    # /tmp is container-private; exclusive permissions keep the marker local.
    with temporary.open("w", encoding="ascii") as marker:
        os.chmod(temporary, 0o600)
        marker.write(str(time.monotonic()))
    temporary.replace(path)


def healthcheck(path: Path = HEARTBEAT_FILE) -> int:
    """Check recent completed polling without credentials, network calls, or mutations."""
    try:
        maximum_age = float(os.getenv("HEALTHCHECK_MAX_AGE_SECONDS", "300"))
        if not math.isfinite(maximum_age) or maximum_age <= 0:
            raise ValueError("invalid healthcheck age")
        stamp = float(path.read_text(encoding="ascii"))
        age = time.monotonic() - stamp
        if not math.isfinite(age) or not 0 <= age <= maximum_age:
            raise ValueError("stale heartbeat")
    except (OSError, ValueError):
        return 1
    return 0


def setup_logging():
    """Send optional ANSI colors to the console and plain text to rotating files."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    formatter = ConsoleFormatter(False)
    LOG.setLevel(level)
    console = logging.StreamHandler()
    color = os.getenv("LOG_COLOR", "always").strip().lower()
    if color not in {"always", "auto", "never"}:
        raise ValueError("LOG_COLOR must be always, auto or never")
    console.setFormatter(ConsoleFormatter(color == "always" or (color == "auto" and console.stream.isatty())))
    LOG.addHandler(console)
    logfile = os.getenv("LOG_FILE", "/state/torrent-guard.log").strip()
    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        handler = RotatingFileHandler(logfile, maxBytes=int(os.getenv("LOG_MAX_BYTES", "10485760")), backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")), encoding="utf-8")
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def main():
    """Run polling and advance liveness only when a complete poll returns."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--healthcheck", action="store_true", help="check recent completed polling and exit")
    arguments = parser.parse_args()
    if arguments.healthcheck:
        return healthcheck()
    # Never let a marker from an earlier process hide failed initialization.
    HEARTBEAT_FILE.unlink(missing_ok=True)
    setup_logging()
    config = Config.from_env()
    LOG.info("Starting v%s categories=%r tags=%r poll=%ds dry_run=%s verify_media=%s import_grace=%dm complete_dir=%s incomplete_dir=%s incomplete_suffix=.!qB state=%s", VERSION, config.categories, config.tags, config.poll_seconds, config.dry_run, config.verify_video_content, config.import_grace_minutes, config.qbit_complete_dir, config.qbit_incomplete_dir, config.state_db)
    LOG.info("Cleanup settings automatic_leftovers=%s delete_settle_seconds=%d write_root=%s", config.cleanup_leftovers, config.delete_settle_seconds, config.cleanup_write_root)
    guard = None
    last_status = None
    while True:
        try:
            if guard is None:
                guard = Guard(config)
            guard.poll()
            write_heartbeat()
            if last_status is None or time.monotonic() - last_status >= 60:
                LOG.debug("HEARTBEAT polling active dry_run=%s pending=%d", config.dry_run, len(guard.store.hashes()))
                guard.log_status()
                last_status = time.monotonic()
        except (ApiError, urllib.error.URLError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            LOG.exception("Poll failed; will retry: %s", exc)
            guard = None
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
