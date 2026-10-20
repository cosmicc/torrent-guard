#!/usr/bin/env python3
"""Torrent Guard: early malware-file rejection, completed-media validation and Arr-aware cleanup.

Uses the qBittorrent/Arr APIs. ffprobe reads downloads through a read-only mount;
only qBittorrent can delete torrent payloads (deleteFiles=true).
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("torrent-guard")
VERSION = "0.2"
DEFAULT_VIDEO = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".divx", ".flv"})
DEFAULT_RISKY = frozenset({".exe", ".scr", ".com", ".bat", ".cmd", ".msi", ".msp", ".msix", ".ps1", ".psm1", ".vbs", ".vbe", ".vb", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".pif", ".cpl", ".jar", ".apk", ".appx", ".reg", ".scf", ".sh", ".py"})


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
    import_grace_minutes: int = 10
    import_check_seconds: int = 60
    history_lookback_days: int = 30
    completed_settle_seconds: int = 60
    ffprobe_timeout_seconds: int = 30

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
        if poll < 2 or not 0 < age < float("inf") or grace < 10 or check < 10 or history < 1 or settle < 0 or timeout < 1:
            raise ValueError("Invalid time settings: poll>=2, age>0, grace>=10m, check>=10s, lookback>=1d, settle>=0, timeout>=1s")
        qroot = os.getenv("QBIT_DOWNLOAD_ROOT", "/downloads").rstrip("/")
        groot = os.getenv("GUARD_DOWNLOAD_ROOT", "/downloads").rstrip("/")
        if not qroot.startswith("/") or not groot.startswith("/") or qroot == "" or groot == "":
            raise ValueError("QBIT_DOWNLOAD_ROOT and GUARD_DOWNLOAD_ROOT must be absolute directories")
        return cls(
            qbit_url=api_url(required("QBIT_URL"), "QBIT_URL"),
            qbit_username=required("QBIT_USERNAME"), qbit_password=required("QBIT_PASSWORD"),
            instances=instances, categories=categories, tags=tags,
            dry_run=boolean("DRY_RUN", True), reject_no_video=boolean("REJECT_NO_VIDEO", True),
            poll_seconds=poll, max_age_hours=age,
            state_db=os.getenv("STATE_DB", "/state/torrent-guard.sqlite3"),
            verify_video_content=boolean("VERIFY_VIDEO_CONTENT", True),
            qbit_download_root=qroot, guard_download_root=groot,
            import_grace_minutes=grace, import_check_seconds=check,
            history_lookback_days=history, completed_settle_seconds=settle,
            ffprobe_timeout_seconds=timeout,
        )


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, url: str, *, fields=None, headers=None):
        data = urllib.parse.urlencode(fields).encode("utf-8") if fields is not None else None
        req = urllib.request.Request(url, method=method, data=data, headers={"User-Agent": "TorrentGuard/0.2", **(headers or {})})
        try:
            with self.opener.open(req, timeout=12) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            snippet = exc.read(250).decode("utf-8", "replace")
            raise ApiError(f"HTTP {exc.code} calling {method} {url}: {snippet}", exc.code) from exc
        stripped = payload.strip()
        if stripped.startswith((b"{", b"[")):
            return json.loads(payload)
        return payload.decode("utf-8", "replace")


class Qbit:
    def __init__(self, config: Config):
        self.config = config
        self.http = Http()
        self.login()

    def login(self):
        result = self.http.request("POST", self.config.qbit_url + "/api/v2/auth/login", fields={"username": self.config.qbit_username, "password": self.config.qbit_password}, headers={"Referer": self.config.qbit_url + "/"})
        if result.strip() != "Ok.":
            raise ApiError(f"qBittorrent refused login: {result!r}")
        LOG.info("Authenticated to qBittorrent")

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
        for page in range(1, 52):
            query = urllib.parse.urlencode({"page": page, "pageSize": 200, "sortKey": "date", "sortDirection": "descending"})
            data = self.http.request("GET", self.instance.url + "/api/v3/history?" + query, headers=self.headers)
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ApiError("Unexpected Arr history response")
            rows = data["records"]
            for row in rows:
                date = parse_date(row.get("date"))
                if date is None:
                    raise ApiError("Arr history event has invalid date; cannot safely verify import")
                if date < cutoff:
                    continue
                event = str(row.get("eventType", "")).casefold()
                if event not in ("downloadfolderimported", "5"):
                    continue
                download_id = str(row.get("downloadId") or "").lower()
                if download_id:
                    imports.setdefault(download_id, []).append(row)
            if not rows or len(rows) < 200 or len(rows) >= int(data.get("totalRecords", page * 200)):
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


def video_files(files: list[dict]) -> list[dict]:
    return [f for f in selected_files(files) if PurePosixPath(str(f.get("name", "")).replace("\\", "/")).suffix.casefold() in DEFAULT_VIDEO]


def classify(files: list[dict], *, reject_no_video: bool = True) -> str | None:
    """Empty file list means torrent metadata pending; ignore unchecked files."""
    if not files:
        return None
    active = selected_files(files)
    if not active:
        return "no selected files for download"
    risky = [str(f.get("name", "")) for f in active if PurePosixPath(str(f.get("name", "")).replace("\\", "/")).suffix.casefold() in DEFAULT_RISKY]
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
        LOG.warning("Skipping conflicting Arr ownership hash=%s category=%s tags=%s", torrent.get("hash"), torrent.get("category"), torrent.get("tags"))
        return None
    return cat or (next(iter(tagged)) if tagged else None)


def is_complete(torrent: dict) -> bool:
    return float(torrent.get("progress") or 0) >= 0.999999 and int(torrent.get("amount_left") or 0) == 0 and int(torrent.get("completion_on") or 0) > 0 and str(torrent.get("state") or "").casefold() not in {"missingfiles", "moving", "checkingdl", "checkingresumedata"}


class MediaValidator:
    """ffprobe selected video payloads; reads files only from an explicitly mapped root."""
    def __init__(self, config: Config):
        self.config = config

    def resolve(self, torrent: dict, file: dict) -> Path:
        root = Path(self.config.qbit_download_root)
        save = Path(str(torrent.get("save_path") or ""))
        relative = PurePosixPath(str(file.get("name") or "").replace("\\", "/"))
        if not save.is_absolute() or not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe or missing download path")
        try:
            save_rel = save.relative_to(root)
        except ValueError as exc:
            raise ValueError("qBittorrent save_path is outside QBIT_DOWNLOAD_ROOT; check mapping") from exc
        mounted_root = Path(self.config.guard_download_root).resolve()
        candidate = (mounted_root / save_rel / str(relative)).resolve()
        if not candidate.is_relative_to(mounted_root):
            raise ValueError("download file path escapes read-only mount")
        return candidate

    def inspect(self, torrent: dict, files: list[dict]) -> tuple[str, str]:
        videos = video_files(files)
        if not videos:
            return "reject", "completed torrent has no selected video files"
        if not self.config.verify_video_content:
            return "accept", f"extension/metadata-only check: {len(videos)} video filename(s) (ffprobe DISABLED)"
        for video in videos:
            name = str(video.get("name"))
            if int(video.get("size") or 0) <= 0 or float(video.get("progress", 0)) < 0.999999:
                return "reject", f"video has zero size or is not fully downloaded: {name}"
            try:
                path = self.resolve(torrent, video)
            except ValueError as exc:
                return "defer", f"media path mapping needs attention for {name}: {exc}"
            if not path.is_file() or not os.access(path, os.R_OK):
                return "defer", f"video not readable at {path}; verify read-only bind mount, permissions, and qBittorrent root mapping"
            try:
                p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", str(path)], capture_output=True, text=True, timeout=self.config.ffprobe_timeout_seconds, check=False)
            except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError) as exc:
                return "defer", f"could not run ffprobe for {name}: {exc}"
            try:
                streams = json.loads(p.stdout or "{}").get("streams", [])
            except ValueError:
                streams = []
            if p.returncode != 0 or not any(s.get("codec_type") == "video" and int(s.get("width") or 0) > 0 and int(s.get("height") or 0) > 0 for s in streams):
                return "reject", f"ffprobe found no readable video stream: {name} ({p.stderr.strip()[:180]})"
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
        if stamp is None or stamp < added or now - stamp < grace_minutes * 60:
            continue
        data = e.get("data") or {}
        source = str(data.get("droppedPath") or data.get("sourcePath") or "").replace("\\", "/").casefold()
        matchable.append((source, stamp))
    if not matchable:
        return False, "no matching import event older than grace period since torrent was added"
    # Single-video releases can use a history event without a source path.
    if len(videos) == 1:
        filename = PurePosixPath(str(videos[0].get("name")).replace("\\", "/")).name.casefold()
        if any(not src or PurePosixPath(src).name == filename for src, _ in matchable):
            return True, "verified single-video Arr import"
        return False, "import history points to a different video filename"
    imported_files = {PurePosixPath(src).name for src, _ in matchable if src}
    wanted = [PurePosixPath(str(f.get("name")).replace("\\", "/")).name.casefold() for f in videos]
    if len(set(wanted)) != len(wanted):
        return False, "duplicate video basenames; cannot safely match all imports"
    if all(name in imported_files for name in wanted):
        return True, f"verified import history for all {len(wanted)} video files"
    return False, f"only {len(imported_files.intersection(wanted))}/{len(wanted)} video files confirmed imported"


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
        self.db.commit()

    def add(self, torrent_hash: str, app: str, name: str, reason: str, kind: str = "reject"):
        if kind not in {"reject", "imported"}:
            raise ValueError("invalid journal kind")
        self.db.execute("INSERT OR IGNORE INTO pending (hash,app,name,reason,queue_id,phase,kind) VALUES (?,?,?,?,NULL,?,?)", (torrent_hash, app, name, reason, "match" if kind == "reject" else "delete", kind))
        self.db.commit()

    def set_phase(self, torrent_hash: str, phase: str, queue_id: int | None = None):
        self.db.execute("UPDATE pending SET phase=?,queue_id=COALESCE(?,queue_id) WHERE hash=?", (phase, queue_id, torrent_hash))
        self.db.commit()

    def remove(self, torrent_hash: str):
        self.db.execute("DELETE FROM pending WHERE hash=?", (torrent_hash,))
        self.db.commit()

    def entries(self):
        return self.db.execute("SELECT hash,app,name,reason,queue_id,phase,kind FROM pending ORDER BY rowid").fetchall()

    def hashes(self):
        return {row[0] for row in self.db.execute("SELECT hash FROM pending")}


class Guard:
    def __init__(self, config: Config, qbit=None, arr_clients=None, store=None, validator=None):
        self.config = config
        self.qbit = qbit if qbit is not None else Qbit(config)
        self.arr_clients = arr_clients if arr_clients is not None else {name: ArrApi(inst) for name, inst in config.instances.items()}
        self.store = store if store is not None else PendingStore(config.state_db)
        self.validator = validator if validator is not None else MediaValidator(config)
        self.metadata_passed: set[str] = set()
        self.completed_passed: set[str] = set()
        self.logged_dry: set[str] = set()
        self.logged_import_dry: set[str] = set()
        self.warned_missing: set[str] = set()
        self.warned_defer: dict[str, str] = {}
        self.last_import_check: float = 0.0

    def poll(self, now: float | None = None):
        now = time.time() if now is None else now
        torrents = self.qbit.torrents()
        if not isinstance(torrents, list):
            raise ApiError("Unexpected qBittorrent torrent-list response")
        by_hash = {str(t.get("hash", "")).lower(): t for t in torrents if t.get("hash")}
        pending = self.store.hashes()
        self.metadata_passed.intersection_update(by_hash)
        self.completed_passed.intersection_update(by_hash)
        self.logged_dry.intersection_update(by_hash)
        self.logged_import_dry.intersection_update(by_hash)
        self.warned_defer = {h: msg for h, msg in self.warned_defer.items() if h in by_hash}
        candidates: dict[str, tuple[str, str, dict, list[dict]]] = {}
        for h, torrent in by_hash.items():
            app = torrent_app(torrent, self.config)
            if not app or h in pending:
                continue
            name = str(torrent.get("name") or h)
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
                    LOG.debug("WAIT metadata [%s] %s hash=%s", app, name, h)
                    continue
                reason = classify(files, reject_no_video=self.config.reject_no_video)
                if reason:
                    self._reject(h, app, name, reason, torrent)
                    continue
                if h not in self.metadata_passed:
                    self.metadata_passed.add(h)
                    LOG.info("ACCEPT metadata [%s] name=%r hash=%s category=%r tags=%r selected_video=%d", app, name, h, torrent.get("category"), torrent.get("tags"), len(video_files(files)))
                if need_complete:
                    result, detail = self.validator.inspect(torrent, files)
                    if result == "reject":
                        self._reject(h, app, name, "completed payload invalid: " + detail, torrent)
                        continue
                    if result == "defer":
                        if self.warned_defer.get(h) != detail:
                            LOG.warning("DEFER validation [%s] name=%r hash=%s reason=%s", app, name, h, detail)
                            self.warned_defer[h] = detail
                        continue
                    self.completed_passed.add(h)
                    LOG.info("ACCEPT completed [%s] name=%r hash=%s: %s; waiting for confirmed Arr import", app, name, h, detail)
                if complete and h in self.completed_passed:
                    candidates[h] = (app, name, torrent, files)
            except (ApiError, urllib.error.URLError, ValueError, OSError, sqlite3.Error) as exc:
                LOG.error("Inspection failed [%s] name=%r hash=%s: %s", app, name, h, exc)

        if candidates and (not self.last_import_check or now - self.last_import_check >= self.config.import_check_seconds):
            self.last_import_check = now
            since = now - self.config.history_lookback_days * 86400
            for app in {v[0] for v in candidates.values()}:
                try:
                    imports = self.arr_clients[app].imported_history(since)
                except (ApiError, ValueError, OSError, urllib.error.URLError) as exc:
                    LOG.error("Import history failed [%s]; preserving downloads: %s", app, exc)
                    continue
                for h, (own_app, name, torrent, files) in candidates.items():
                    if own_app != app or h in pending:
                        continue
                    if int(torrent.get("added_on") or 0) < since:
                        LOG.debug("Skipping import cleanup beyond history lookback: %s", name)
                        continue
                    yes, reason = import_confirmed(torrent, files, imports.get(h, []), now, self.config.import_grace_minutes)
                    if not yes:
                        LOG.debug("WAIT import [%s] name=%r hash=%s: %s", app, name, h, reason)
                        continue
                    if self.config.dry_run:
                        if h not in self.logged_import_dry:
                            LOG.info("DRY RUN: ACCEPT imported [%s] name=%r hash=%s: %s; would delete torrent + qBit data", app, name, h, reason)
                            self.logged_import_dry.add(h)
                    else:
                        LOG.info("CLEANUP scheduled [%s] name=%r hash=%s: imported at least %dm ago; %s", app, name, h, self.config.import_grace_minutes, reason)
                        self.store.add(h, app, name, reason, kind="imported")
                        pending.add(h)
        if not self.config.dry_run:
            self._process_pending(by_hash)

    def _reject(self, h: str, app: str, name: str, reason: str, torrent: dict):
        if h not in self.logged_dry:
            LOG.warning("REJECT candidate [%s] name=%r hash=%s category=%r tags=%r reason=%s", app, name, h, torrent.get("category"), torrent.get("tags"), reason)
        if self.config.dry_run:
            if h not in self.logged_dry:
                LOG.warning("DRY RUN: would stop, Arr-blocklist and delete torrent + tracked files: %s", name)
            self.logged_dry.add(h)
        else:
            self.store.add(h, app, name, reason, kind="reject")

    def _process_pending(self, by_hash: dict[str, dict]):
        queue_cache: dict[str, dict[str, int]] = {}
        for h, app, name, reason, queue_id, phase, kind in self.store.entries():
            try:
                torrent = by_hash.get(h)
                if torrent and torrent_app(torrent, self.config) != app:
                    LOG.error("PENDING ownership changed; manual review required [%s] %s hash=%s", app, name, h)
                    continue
                if phase == "match":
                    if torrent is None:
                        LOG.error("PENDING disappeared before cleanup; cannot prove files were deleted: %s", name)
                        continue
                    self.qbit.stop(h)
                    if app not in queue_cache:
                        queue_cache[app] = self.arr_clients[app].queue_by_hash()
                    queue_id = queue_cache[app].get(h)
                    if queue_id is None:
                        if h not in self.warned_missing:
                            LOG.warning("PENDING no %s queue match yet (stopped; will retry): %s", app, name)
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
                        LOG.warning("Arr queue ID already gone; confirm blocklist manually [%s] name=%s", app, name)
                    self.store.set_phase(h, "delete")
                    phase = "delete"
                if phase == "delete":
                    if torrent is None:
                        LOG.error("PENDING torrent missing before deleteFiles=true: manual file verification needed [%s] %s", app, name)
                        continue
                    if kind == "reject":
                        self.qbit.stop(h)
                    self.qbit.delete_with_data(h)
                    self.store.set_phase(h, "verify")
                    LOG.warning("DELETE requested [%s] kind=%s name=%r hash=%s deleteFiles=true", app, kind, name, h)
                    continue
                if phase == "verify":
                    if torrent is not None:
                        self.qbit.delete_with_data(h)
                        LOG.warning("PENDING deletion still visible; retrying [%s] %s hash=%s", app, name, h)
                        continue
                    self.store.remove(h)
                    self.warned_missing.discard(h)
                    LOG.info("CLEANED [%s] kind=%s name=%r hash=%s qBittorrent entry absent after deleteFiles=true; reason=%s (disk deletion not independently verified)", app, kind, name, h, reason)
            except (ApiError, urllib.error.URLError, ValueError, OSError, sqlite3.Error) as exc:
                LOG.error("Cleanup failed [%s] kind=%s name=%r hash=%s phase=%s (will retry): %s", app, kind, name, h, phase, exc)


def setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    LOG.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOG.addHandler(console)
    logfile = os.getenv("LOG_FILE", "/state/torrent-guard.log").strip()
    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        handler = RotatingFileHandler(logfile, maxBytes=int(os.getenv("LOG_MAX_BYTES", "10485760")), backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")), encoding="utf-8")
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def main():
    setup_logging()
    config = Config.from_env()
    LOG.info("Starting v%s categories=%r tags=%r poll=%ds dry_run=%s verify_media=%s import_grace=%dm state=%s", VERSION, config.categories, config.tags, config.poll_seconds, config.dry_run, config.verify_video_content, config.import_grace_minutes, config.state_db)
    guard = None
    while True:
        try:
            if guard is None:
                guard = Guard(config)
            guard.poll()
        except (ApiError, urllib.error.URLError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            LOG.exception("Poll failed; will retry: %s", exc)
            guard = None
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
