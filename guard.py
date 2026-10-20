#!/usr/bin/env python3
"""Reject suspicious Sonarr/Radarr torrents using qBittorrent file metadata.

Standard library only; no filesystem access to downloaded media is required.
This is an extension-based filter, not an antivirus engine.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("torrent-guard")

DEFAULT_VIDEO = frozenset({
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".divx", ".flv",
})
DEFAULT_RISKY = frozenset({
    ".exe", ".scr", ".com", ".bat", ".cmd", ".msi", ".msp",
    ".msix", ".ps1", ".psm1", ".vbs", ".vbe", ".vb", ".js",
    ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".pif", ".cpl",
    ".jar", ".apk", ".appx", ".reg", ".scf", ".sh", ".py",
})


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

    @classmethod
    def from_env(cls) -> "Config":
        instances: dict[str, ArrInstance] = {}
        categories: dict[str, str] = {}
        for app in ("SONARR", "RADARR"):
            url = os.getenv(f"{app}_URL", "").strip()
            key = os.getenv(f"{app}_API_KEY", "").strip()
            category = os.getenv(f"{app}_CATEGORY", "").strip()
            if any((url, key, category)) and not all((url, key, category)):
                raise ValueError(f"Configure all {app}_URL, _API_KEY and _CATEGORY, or none")
            if url:
                if category in categories:
                    raise ValueError(f"Duplicate qBittorrent category: {category}")
                instances[app] = ArrInstance(api_url(url, f"{app}_URL"), key, category)
                categories[category] = app
        if not instances:
            raise ValueError("Configure Sonarr and/or Radarr")
        poll = int(os.getenv("POLL_SECONDS", "5"))
        age = float(os.getenv("MAX_AGE_HOURS", "24"))
        if poll < 2 or age <= 0 or age != age:
            raise ValueError("POLL_SECONDS must be >= 2 and MAX_AGE_HOURS must be positive")
        return cls(
            qbit_url=api_url(required("QBIT_URL"), "QBIT_URL"),
            qbit_username=required("QBIT_USERNAME"),
            qbit_password=required("QBIT_PASSWORD"),
            instances=instances,
            categories=categories,
            dry_run=boolean("DRY_RUN", True),
            reject_no_video=boolean("REJECT_NO_VIDEO", True),
            poll_seconds=poll,
            max_age_hours=age,
            state_db=required("STATE_DB") if "STATE_DB" in os.environ else "/state/torrent-guard.sqlite3",
        )


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method: str, url: str, *, fields=None, headers=None):
        data = urllib.parse.urlencode(fields).encode("utf-8") if fields is not None else None
        req = urllib.request.Request(
            url,
            method=method,
            data=data,
            headers={"User-Agent": "TorrentGuard/0.1", **(headers or {})},
        )
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
        result = self.http.request(
            "POST", self.config.qbit_url + "/api/v2/auth/login",
            fields={"username": self.config.qbit_username, "password": self.config.qbit_password},
            headers={"Referer": self.config.qbit_url + "/"},
        )
        if result.strip() != "Ok.":
            raise ApiError(f"qBittorrent refused login: {result!r}")
        LOG.info("Authenticated to qBittorrent")

    def request(self, method: str, path: str, *, fields=None, query=None):
        url = self.config.qbit_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        def issue():
            return self.http.request(
                method, url, fields=fields,
                headers={"Referer": self.config.qbit_url + "/"},
            )
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
            # qBittorrent 4.x calls this API 'pause'.
            self.request("POST", "/api/v2/torrents/pause", fields={"hashes": torrent_hash})

    def delete_with_data(self, torrent_hash: str):
        """Ask qBittorrent to remove both the torrent and its tracked data."""
        return self.request(
            "POST", "/api/v2/torrents/delete",
            fields={"hashes": torrent_hash, "deleteFiles": "true"},
        )


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
            data = self.http.request(
                "GET", self.instance.url + "/api/v3/queue?" + query,
                headers=self.headers,
            )
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ApiError("Unexpected Arr queue response")
            rows = data["records"]
            records.extend(rows)
            if not rows or len(records) >= int(data.get("totalRecords", len(records))):
                return {
                    str(row["downloadId"]).lower(): int(row["id"])
                    for row in records
                    if row.get("downloadId") and row.get("id") is not None
                }
        raise ApiError("Arr queue exceeds 10,000 records; refusing incomplete map")

    def blocklist_and_remove(self, queue_id: int):
        query = urllib.parse.urlencode({
            # Torrent Guard explicitly deletes with qBittorrent deleteFiles=true.
            # Letting Arr remove it first could leave downloaded data behind.
            "removeFromClient": "false",
            "blocklist": "true",
            "skipRedownload": "false",
        })
        self.http.request(
            "DELETE", self.instance.url + f"/api/v3/queue/{queue_id}?" + query,
            headers=self.headers,
        )


def classify(files: list[dict], *, reject_no_video: bool = True) -> str | None:
    """Classify a qBittorrent torrent file-list. Empty list means metadata pending."""
    if not files:
        return None
    risky: list[str] = []
    video_found = False
    for file in files:
        name = str(file.get("name", ""))
        ext = PurePosixPath(name.replace("\\", "/")).suffix.lower()
        if ext in DEFAULT_RISKY:
            risky.append(name)
        if ext in DEFAULT_VIDEO:
            video_found = True
    if risky:
        return "risky executable/script/shortcut: " + ", ".join(risky[:4])
    if reject_no_video and not video_found:
        return "no recognized video file (.mkv, .mp4, etc.)"
    return None


class PendingStore:
    """Small durable SQLite journal; container restarts cannot lose pending removals."""

    def __init__(self, filename: str):
        if filename != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        self.db = sqlite3.connect(filename)
        self.db.execute("""CREATE TABLE IF NOT EXISTS pending (
            hash TEXT PRIMARY KEY, app TEXT NOT NULL, name TEXT NOT NULL,
            reason TEXT NOT NULL, queue_id INTEGER, phase TEXT NOT NULL
        )""")
        self.db.commit()

    def add(self, torrent_hash: str, app: str, name: str, reason: str):
        self.db.execute(
            "INSERT OR IGNORE INTO pending (hash,app,name,reason,queue_id,phase) VALUES (?,?,?,?,NULL,'match')",
            (torrent_hash, app, name, reason),
        )
        self.db.commit()

    def set_phase(self, torrent_hash: str, phase: str, queue_id: int | None = None):
        self.db.execute(
            "UPDATE pending SET phase=?, queue_id=COALESCE(?,queue_id) WHERE hash=?",
            (phase, queue_id, torrent_hash),
        )
        self.db.commit()

    def remove(self, torrent_hash: str):
        self.db.execute("DELETE FROM pending WHERE hash=?", (torrent_hash,))
        self.db.commit()

    def entries(self):
        return self.db.execute(
            "SELECT hash,app,name,reason,queue_id,phase FROM pending ORDER BY rowid"
        ).fetchall()

    def hashes(self):
        return {row[0] for row in self.db.execute("SELECT hash FROM pending")}


class Guard:
    def __init__(self, config: Config, qbit=None, arr_clients=None, store=None):
        self.config = config
        self.qbit = qbit if qbit is not None else Qbit(config)
        self.arr_clients = arr_clients if arr_clients is not None else {
            name: ArrApi(instance) for name, instance in config.instances.items()
        }
        self.store = store if store is not None else PendingStore(config.state_db)
        self.good: set[str] = set()
        self.logged_dry: set[str] = set()
        self.warned_missing: set[str] = set()

    def poll(self, now: float | None = None):
        now = time.time() if now is None else now
        torrents = self.qbit.torrents()
        if not isinstance(torrents, list):
            raise ApiError("Unexpected qBittorrent torrent-list response")
        by_hash = {str(t.get("hash", "")).lower(): t for t in torrents if t.get("hash")}
        pending = self.store.hashes()
        eligible = {}
        for torrent_hash, torrent in by_hash.items():
            app = self.config.categories.get(torrent.get("category"))
            age = now - int(torrent.get("added_on") or 0)
            if app and 0 <= age <= self.config.max_age_hours * 3600:
                eligible[torrent_hash] = (app, str(torrent.get("name", torrent_hash)))

        self.good.intersection_update(eligible)
        self.logged_dry.intersection_update(eligible)
        for torrent_hash, (app, name) in eligible.items():
            if torrent_hash in self.good or torrent_hash in pending:
                continue
            try:
                files = self.qbit.files(torrent_hash)
                if not isinstance(files, list):
                    raise ApiError("Unexpected qBittorrent file-list response")
                if not files:
                    continue  # Magnet metadata not available yet.
                reason = classify(files, reject_no_video=self.config.reject_no_video)
                if not reason:
                    self.good.add(torrent_hash)
                    continue
                if torrent_hash not in self.logged_dry:
                    LOG.warning("SUSPICIOUS [%s] %s [%s]: %s", app, name, torrent_hash[:12], reason)
                if self.config.dry_run:
                    if torrent_hash not in self.logged_dry:
                        LOG.warning("DRY RUN: would stop, blocklist and delete torrent + its files: %s", name)
                    self.logged_dry.add(torrent_hash)
                else:
                    self.store.add(torrent_hash, app, name, reason)
                    pending.add(torrent_hash)
            except (ApiError, urllib.error.URLError, ValueError, OSError) as exc:
                LOG.error("Failed inspecting %s: %s", name, exc)

        if self.config.dry_run:
            return

        # Pending entries are durable and remain actionable after age cutoff/restart.
        queue_cache: dict[str, dict[str, int]] = {}
        for torrent_hash, app, name, reason, queue_id, phase in self.store.entries():
            try:
                torrent = by_hash.get(torrent_hash)
                # Never delete files for a hash whose current ownership has changed.
                if torrent and self.config.categories.get(torrent.get("category")) != app:
                    LOG.error("PENDING %s changed category; manual review required: %s", app, name)
                    continue
                if phase == "match":
                    if torrent is None:
                        LOG.error("PENDING torrent disappeared before cleanup; cannot verify its files were deleted: %s", name)
                        continue
                    self.qbit.stop(torrent_hash)
                    if app not in queue_cache:
                        queue_cache[app] = self.arr_clients[app].queue_by_hash()
                    queue_id = queue_cache[app].get(torrent_hash)
                    if queue_id is None:
                        if torrent_hash not in self.warned_missing:
                            LOG.warning("Not yet in %s queue (stopped; will retry): %s", app, name)
                            self.warned_missing.add(torrent_hash)
                        continue
                    self.store.set_phase(torrent_hash, "arr", queue_id)
                    phase = "arr"
                if phase == "arr":
                    # Record failure in Arr first, but tell Arr NOT to delete from
                    # qBittorrent: only our deleteFiles=true request should do so.
                    try:
                        self.arr_clients[app].blocklist_and_remove(queue_id)
                    except ApiError as exc:
                        if exc.status != 404:
                            raise
                        LOG.warning("Queue ID %s already missing in %s; proceeding with data cleanup for %s", queue_id, app, name)
                    self.store.set_phase(torrent_hash, "delete")
                    phase = "delete"
                if phase == "delete":
                    if torrent is None:
                        LOG.error("PENDING torrent disappeared before explicit deleteFiles=true; manual file cleanup may be needed: %s", name)
                        continue
                    self.qbit.stop(torrent_hash)
                    self.qbit.delete_with_data(torrent_hash)
                    LOG.warning("DELETE REQUESTED [%s] torrent + downloaded data: %s", app, name)
                    self.store.set_phase(torrent_hash, "verify")
                    # Verify disappearance next poll, not via an assumed synchronous delete.
                    continue
                if phase == "verify":
                    if torrent is not None:
                        self.qbit.stop(torrent_hash)
                        self.qbit.delete_with_data(torrent_hash)
                        LOG.warning("Deletion not yet confirmed; retrying deleteFiles=true: %s", name)
                        continue
                    self.store.remove(torrent_hash)
                    self.warned_missing.discard(torrent_hash)
                    LOG.warning("REJECTED [%s] %s: torrent absent after deleteFiles=true; %s", app, name, reason)
            except (ApiError, urllib.error.URLError, ValueError, OSError, sqlite3.Error) as exc:
                LOG.error("Cleanup failed for %s (journal retained for retry): %s", name, exc)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_env()
    LOG.info(
        "Starting: categories=%r poll=%ds max_age=%.1fh dry_run=%s reject_no_video=%s state=%s",
        config.categories, config.poll_seconds, config.max_age_hours,
        config.dry_run, config.reject_no_video, config.state_db,
    )
    guard = None
    while True:
        try:
            if guard is None:
                guard = Guard(config)
            guard.poll()
        except (ApiError, urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            LOG.error("Poll failed; will retry: %s", exc)
            # Re-authenticate on the next poll after a qBittorrent-wide failure.
            guard = None
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
