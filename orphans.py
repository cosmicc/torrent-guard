"""Observed, journaled orphan cleanup limited to four dedicated media directories.

Client absence authorizes this independent policy even without import history.
Live torrents of every category/state protect their paths. No library, symlink,
private quarantine, or file outside the four configured scopes is discovered.
"""
from dataclasses import replace
import errno
import json
import logging
import os
from pathlib import Path, PurePosixPath
import stat
import urllib.error

from deletion import FileVerifier, identity

LOG = logging.getLogger("torrent-guard")
ABSENCE_SECONDS = 900
SCAN_SECONDS = 60
MAX_FILES = 10000


class OrphanCleaner:
    """Persist absence observations and reuse identity-protected quarantine removal."""

    def __init__(self, guard) -> None:
        self.guard = guard
        self.last_scan = None
        self.rows = None
        # Persist incident notification state separately from deletion evidence.
        # Retrying a safety hold must not emit a fresh warning every 15 minutes.
        self.failures = {}
        guard.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS orphan_candidates (
                path TEXT PRIMARY KEY, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, manifest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orphan_audit (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL,
                removed_at REAL NOT NULL, manifest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orphan_failures (
                path TEXT PRIMARY KEY, reason TEXT NOT NULL);
        """)
        self.failures = dict(guard.store.db.execute("SELECT path, reason FROM orphan_failures"))

    def report_failure(self, path: str, reason: str) -> None:
        """Notify once per changed blocking incident, including across restarts."""
        if self.failures.get(path) == reason:
            LOG.debug("ORPHAN retry retained %s: %s", path, reason)
            return
        self.failures[path] = reason
        if not self.guard.config.dry_run:
            with self.guard.store.db:
                self.guard.store.db.execute("INSERT OR REPLACE INTO orphan_failures VALUES (?,?)", (path, reason))
        LOG.warning("ORPHAN deferred %s; timer reset: %s", path, reason)

    def clear_failures(self, active: set[str]) -> None:
        """Allow a new warning only after recovery or an incident's disappearance."""
        for path in set(self.failures) - active:
            del self.failures[path]
            if not self.guard.config.dry_run:
                with self.guard.store.db:
                    self.guard.store.db.execute("DELETE FROM orphan_failures WHERE path=?", (path,))

    def scopes(self):
        """Map only complete/incomplete tv/movies to four independent write binds."""
        config = self.guard.config
        for kind, base in (("complete", config.qbit_complete_dir),
                           ("incomplete", config.qbit_incomplete_dir)):
            if not base:
                raise ValueError("Orphan cleanup requires both configured download directories")
            for media in ("tv", "movies"):
                qpath = str(PurePosixPath(base) / media)
                scoped = replace(config, qbit_complete_dir=qpath, qbit_incomplete_dir="",
                                 cleanup_leftovers=True,
                                 cleanup_write_root=f"{config.orphan_write_root}/{kind}/{media}")
                yield qpath, FileVerifier(scoped)

    def protected(self) -> set[PurePosixPath]:
        """Refresh all torrent ownership, including unrelated, queued and seeded data."""
        cache = {}
        paths = self.guard._ownership_paths(cache)
        reserved = {PurePosixPath(root) / path for root, path in paths}
        reserved.update(cache["queued_content"])
        # A torrent's content directory protects extras not in its file list.
        live = self.guard.qbit.torrents()
        self.guard._inventory_hashes(live)
        fields = ("hash", "added_on", "save_path", "download_path", "content_path")
        def locations(rows):
            """Compare incarnation and paths across the ownership inventory reads."""
            return {t["hash"].lower(): tuple(t.get(key) for key in fields) for t in rows}
        if locations(live) != locations(cache["inventory"]):
            raise ValueError("Client inventory changed during orphan ownership check")
        for torrent in live:
            content = torrent.get("content_path")
            if content:
                path = PurePosixPath(str(content))
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("Unsafe live torrent content path")
                reserved.add(path)
            # Protect both possible destinations of every known file even if a
            # move changed the torrent between the two inventory reads.
            files = self.guard.qbit.files(torrent["hash"])
            if not isinstance(files, list):
                raise ValueError("File metadata became unavailable during ownership check")
            if not files:
                from guard import is_unstarted_download
                if not is_unstarted_download(torrent):
                    raise ValueError("File metadata disappeared during ownership check")
            if files:
                reserved.update(PurePosixPath(item["root"]) / item["path"]
                                for item in self.guard.verifier.paths(torrent, files))
        return reserved

    @staticmethod
    def claimed(path: PurePosixPath, reserved: set[PurePosixPath]) -> bool:
        """Directory/file overlap is ownership, regardless of torrent state."""
        return any(path.is_relative_to(value) or value.is_relative_to(path) for value in reserved)

    def discover(self, qroot: str, verifier: FileVerifier) -> dict:
        """Walk bounded descriptor-relative directories without following symlinks."""
        root = verifier.roots[qroot]
        relative = str(root.relative_to(verifier.ro)) + "/.orphan-anchor"
        result = {}
        with verifier.parent(verifier.ro, relative) as (descriptor, _):
            if descriptor is None:
                raise ValueError(f"Orphan scope is unavailable: {qroot}")
            info = os.fstat(descriptor)
            anchor = {qroot: {"path": str(root), "identity": [info.st_dev, info.st_ino]}}

            visited = 0

            def walk(parent, prefix, depth):
                """Bound work while visiting only attached ordinary directories."""
                nonlocal visited
                if depth > 32:
                    raise ValueError("Orphan directory nesting exceeds safety limit")
                for name in os.listdir(parent):
                    if name.startswith(".torrent-guard-"):
                        continue  # Journal-only recovery; old empty quarantines do not consume the scan limit.
                    visited += 1
                    if visited > MAX_FILES:
                        raise ValueError("Orphan scan exceeds per-directory entry limit")
                    if "\\" in name:
                        raise ValueError("Ambiguous backslash in orphan filename")
                    path = str(PurePosixPath(prefix) / name)
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # Import/move removed this name after listdir.
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        except FileNotFoundError:
                            continue
                        try:
                            if identity(os.fstat(child))[:2] != identity(info)[:2]:
                                raise ValueError("Directory changed during orphan scan")
                            walk(child, path, depth + 1)
                            if not os.listdir(child):
                                item = {"root": qroot, "path": path,
                                        "identity": identity(os.fstat(child)), "kind": "directory"}
                                result[str(PurePosixPath(qroot) / path)] = {
                                    "version": 1, "roots": anchor, "items": [item]}
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        if len(result) >= MAX_FILES:
                            raise ValueError("Orphan scan exceeds per-directory file limit")
                        item = {"root": qroot, "path": path, "identity": identity(info)}
                        result[str(PurePosixPath(qroot) / path)] = {
                            "version": 1, "roots": anchor, "items": [item]}
                    else:
                        raise ValueError(f"Symlink or special file in orphan scope: {qroot}/{path}")
            walk(descriptor, "", 0)
        return result

    @staticmethod
    def remove_directory(verifier: FileVerifier, manifest: dict, item: dict) -> bool:
        """Remove only a saved, unchanged empty child directory on matching roots.

        This intentionally uses rmdir rather than recursive deletion. A newly
        created file, replacement directory, or attachment change prevents it.
        The caller has already refreshed all live-client ownership reservations.
        """
        if (verifier.config.dry_run or not verifier.config.cleanup_leftovers
                or item["root"] != verifier.config.qbit_complete_dir
                or item.get("kind") != "directory"):
            raise ValueError("Empty-directory cleanup is not authorized")
        root = Path(verifier.config.cleanup_write_root)
        anchor = manifest["roots"][item["root"]]["identity"]
        with verifier.parent(verifier.roots[item["root"]], item["path"], anchor) as (parent, name):
            if parent is not None:
                try:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (not stat.S_ISDIR(current.st_mode) or identity(current) != item["identity"]):
                    return False
        with verifier.parent(root, item["path"], anchor) as (parent, name):
            if parent is None:
                return True
            expected = root / PurePosixPath(item["path"]).parent
            verifier.attached(parent, expected)
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if not stat.S_ISDIR(info.st_mode) or identity(info) != item["identity"]:
                return False
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if identity(os.fstat(child)) != item["identity"] or os.listdir(child):
                    return False
                verifier.attached(child, expected / name)
                verifier.attached(parent, expected)
                if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != item["identity"]:
                    return False
                try:
                    os.rmdir(name, dir_fd=parent)
                except OSError as exc:
                    if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                        return False
                    if exc.errno != errno.ENOENT:
                        raise
            finally:
                os.close(child)
        # Check absence independently through the read mapping, not the alias.
        with verifier.parent(verifier.roots[item["root"]], item["path"], anchor) as (parent, name):
            if parent is not None:
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return True
                raise ValueError("Directory reappeared after cleanup; preserving replacement")
        return True

    @staticmethod
    def remove_empty_quarantine(verifier: FileVerifier, manifest: dict, item: dict) -> None:
        """Retire only this candidate's verified-empty, identity-matched capture."""
        if not item.get("quarantine"):
            return
        if (verifier.config.dry_run or not verifier.config.cleanup_leftovers
                or item["root"] != verifier.config.qbit_complete_dir):
            raise ValueError("Empty-quarantine cleanup is not authorized")
        quarantine = PurePosixPath(item["quarantine"])
        if (not quarantine.name.startswith(".torrent-guard-")
                or quarantine.parent != PurePosixPath(item["path"]).parent):
            raise ValueError("Invalid saved quarantine location; preserving directory")
        root = Path(verifier.config.cleanup_write_root)
        anchor = manifest["roots"][item["root"]]["identity"]
        with verifier.parent(root, item["quarantine"], anchor) as (parent, name):
            if parent is None:
                return
            verifier.attached(parent, root / PurePosixPath(item["quarantine"]).parent)
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or [info.st_dev, info.st_ino] != item["quarantine_identity"]):
                raise ValueError("Quarantine identity/permissions changed; preserving directory")
            os.rmdir(name, dir_fd=parent)  # Refuses even one unexpected child.

    def save(self, path: str | None = None) -> None:
        """Checkpoint all observations; dry-run state is exclusively in memory."""
        if self.guard.config.dry_run:
            return
        if path is not None:
            first, last, manifest = self.rows[path]
            with self.guard.store.db:
                self.guard.store.db.execute("INSERT OR REPLACE INTO orphan_candidates VALUES (?,?,?,?)",
                                            (path, first, last, json.dumps(manifest)))
            return
        with self.guard.store.db:
            self.guard.store.db.execute("DELETE FROM orphan_candidates")
            self.guard.store.db.executemany("INSERT INTO orphan_candidates VALUES (?,?,?,?)",
                [(path, first, last, json.dumps(manifest))
                 for path, (first, last, manifest) in self.rows.items()])

    def invalidate(self, now: float) -> None:
        """Reset eligibility after a failed client observation, preserving captures."""
        if self.rows is None and self.guard.config.orphan_cleanup:
            self.load()
        if self.rows is not None:
            self.rows = {path: (now, now, manifest) for path, (_, _, manifest) in self.rows.items()}
            self.save()

    def load(self) -> None:
        """Read additive journal observations before invalidating or resuming them."""
        self.rows = {path: (first, last, json.loads(manifest)) for path, first, last, manifest
                     in self.guard.store.db.execute("SELECT * FROM orphan_candidates")}

    def poll(self, now: float) -> None:
        """Observe every minute; fresh API/access failures reset absence eligibility."""
        if not self.guard.config.orphan_cleanup:
            return
        if self.last_scan is not None and 0 <= now - self.last_scan < SCAN_SECONDS:
            return
        self.last_scan = now
        if self.rows is None:
            self.load()
        try:
            reserved = self.protected()
            discovered, verifiers = {}, {}
            for qroot, verifier in self.scopes():
                verifiers[qroot] = verifier
                discovered.update(self.discover(qroot, verifier))
            updated = {}
            for path, manifest in discovered.items():
                old = self.rows.get(path)
                if self.claimed(PurePosixPath(path), reserved):
                    continue
                first = now
                if old and old[2] == manifest and 0 <= now - old[1] <= 120 and now >= old[0]:
                    first = old[0]
                updated[path] = (first, now, manifest)
            # Interrupted captures survive discovery and are never adopted by a
            # sweep. Reappearance/ownership resets the timer without losing proof.
            for path, old in self.rows.items():
                if old[2]["items"][0].get("quarantine"):
                    first = old[0] if 0 <= now - old[1] <= 120 and now >= old[0] else now
                    if self.claimed(PurePosixPath(path), reserved):
                        first = now
                    updated[path] = (first, now, old[2])
            self.rows = updated
            self.save()
            removed = 0
            attempted = 0
            for path, (first, last, manifest) in list(self.rows.items()):
                if now - first < ABSENCE_SECONDS or attempted >= 32:
                    continue
                attempted += 1
                item = manifest["items"][0]
                verifier = verifiers[item["root"]]
                targets = [PurePosixPath(path)]
                if item.get("quarantine"):
                    targets.append(PurePosixPath(item["root"]) / item["quarantine"] / "payload")
                fresh_reserved = self.protected()
                if any(self.claimed(target, fresh_reserved) for target in targets):
                    self.rows[path] = (now, now, manifest)
                    self.save(path)
                    continue
                if self.guard.config.dry_run:
                    LOG.info("DRY RUN: ORPHAN would remove %s; absent from qBittorrent for 15 minutes", path)
                    continue
                # FileVerifier matches the write bind's device/inode to the read
                # snapshot and persists quarantine before rename. A replacement
                # captured in a race remains quarantined for manual review.
                try:
                    if item.get("kind") == "directory":
                        if not self.remove_directory(verifier, manifest, item):
                            self.rows[path] = (now, now, manifest)
                            self.save(path)
                            continue
                    else:
                        self.remove_file(verifier, manifest, item, path)
                except (OSError, ValueError) as exc:
                    # One inaccessible/replaced file must not starve other work.
                    self.rows[path] = (now, now, manifest)
                    self.save(path)
                    self.report_failure(path, str(exc))
                    continue
                with self.guard.store.db:
                    self.guard.store.db.execute("INSERT INTO orphan_audit(path,removed_at,manifest) VALUES (?,?,?)",
                                                (path, now, json.dumps(manifest)))
                    self.guard.store.db.execute("DELETE FROM orphan_candidates WHERE path=?", (path,))
                del self.rows[path]
                removed += 1
                LOG.info("ORPHAN removed %s; no qBittorrent ownership for 15 minutes", path)
            self.clear_failures(set(self.rows))
            LOG.debug("ORPHAN scan candidates=%d removed=%d dry_run=%s", len(self.rows), removed, self.guard.config.dry_run)
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            # Preserve capture progress but require a new uninterrupted wait.
            self.rows = {path: (now, now, manifest) for path, (_, _, manifest) in self.rows.items()}
            self.save()
            if str(exc) == "Client inventory changed during orphan ownership check":
                LOG.debug("ORPHAN observation reset during client update: %s", exc)
            else:
                self.report_failure("scan", str(exc))

    def remove_file(self, verifier: FileVerifier, manifest: dict, item: dict, path: str) -> None:
        """Remove a captured file and its own empty recovery directory safely."""
        try:
            verifier.remove(manifest, item, lambda _: self.save(path))
        except FileNotFoundError:
            # Import/removal is normal only after same-root absence checks.
            if verifier.remaining(manifest):
                raise
        if verifier.remaining(manifest):
            raise ValueError(f"Orphan remains after cleanup: {path}")
        self.remove_empty_quarantine(verifier, manifest, item)
