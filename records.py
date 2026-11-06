"""Archive stale bookkeeping without granting any permission to delete payloads.

HTTP handlers may enqueue requests, but only the polling thread can retire a
record after checking qBittorrent. Archives retain all original evidence. Automatic
retirement additionally requires independent absence checks of every saved name.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

ACTIVE_TABLES = ("pending", "deletion_evidence", "filesystem_retry", "cleanup_retry",
                 "metadata_wait", "stalled_wait", "record_observations", "record_checks")


def initialize(database: sqlite3.Connection) -> None:
    """Add bookkeeping tables without rebuilding existing journals or audits."""
    database.executescript("""
        CREATE TABLE IF NOT EXISTS record_checks (hash TEXT PRIMARY KEY, checked_at REAL NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS record_observations (hash TEXT PRIMARY KEY, manifest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS record_archive (
            id INTEGER PRIMARY KEY, hash TEXT NOT NULL, name TEXT NOT NULL,
            retired_at REAL NOT NULL, disposition TEXT NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS record_requests (
            id TEXT PRIMARY KEY, hash TEXT NOT NULL, revision TEXT NOT NULL,
            created_at REAL NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL);
    """)


def snapshot(database: sqlite3.Connection, h: str) -> dict:
    """Preserve raw rows, including legacy/corrupt evidence that cannot be parsed."""
    return {table: database.execute(f"SELECT * FROM {table} WHERE hash=?", (h,)).fetchall()
            for table in ACTIVE_TABLES}


def revision(record: dict) -> str:
    """Bind a browser action to the record incarnation/evidence it displayed.

    Retry deadlines and attempt counts naturally advance while the page is open;
    they do not change what record is being archived. Payload/journal changes do.
    """
    stable = {table: rows for table, rows in record.items() if table not in {"filesystem_retry", "cleanup_retry", "record_checks"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def label(record: dict) -> tuple[str, str]:
    """Recover display context without guessing paths from a torrent title."""
    if record["pending"]:
        return record["pending"][0][2], record["pending"][0][3]
    if record["record_observations"]:
        try:
            observed = json.loads(record["record_observations"][0][1])
            return observed.get("record_name", "Retained download"), observed.get("record_reason", "")
        except (ValueError, AttributeError):
            pass
    return "Retained download", next((record[table][0][3] for table in
                                     ("filesystem_retry", "cleanup_retry") if record[table]), "")


def remember_paths(database: sqlite3.Connection, h: str, manifest: dict | None,
                   torrent: dict, reason: str) -> None:
    """Keep all previously observed names; never narrow absence checks on a move."""
    row = database.execute("SELECT manifest FROM record_observations WHERE hash=?", (h,)).fetchone()
    previous = {}
    malformed = False
    if row:
        try:
            previous = json.loads(row[0])
            if not isinstance(previous, dict):
                raise ValueError("invalid observation")
        except ValueError:
            previous, malformed = {"uninterpreted_evidence": row[0]}, True
    observed = dict(manifest) if manifest is not None else dict(previous)
    observed["unverifiable"] = manifest is None or malformed or bool(previous.get("unverifiable"))
    if manifest is not None and previous.get("version") == 1:
        old_roots, new_roots = previous.get("roots", {}), manifest.get("roots", {})
        old_items, new_items = previous.get("items", []), manifest.get("items", [])
        if not isinstance(old_roots, dict) or not isinstance(old_items, list):
            observed["unverifiable"] = True
            observed["uninterpreted_evidence"] = row[0]
        else:
            if any(new_roots[root] != old_roots[root] for root in old_roots.keys() & new_roots.keys()):
                observed["unverifiable"] = True
            old_torrent = previous.get("torrent", {})
            if not isinstance(old_torrent, dict) or old_torrent.get("added_on") != manifest["torrent"].get("added_on"):
                observed["unverifiable"] = True
            observed["roots"] = {**old_roots, **new_roots}
            # Full item dictionaries retain old identities/quarantine names for
            # audit. Only the absence verifier consumes these combined targets.
            items = {json.dumps(item, sort_keys=True): item for item in old_items + new_items}
            observed["items"] = list(items.values())
    if previous.get("uninterpreted_evidence") is not None:
        observed["uninterpreted_evidence"] = previous["uninterpreted_evidence"]
    observed["record_name"] = str(torrent.get("name") or "Retained download")
    observed["record_reason"] = reason
    observed.setdefault("torrent", {"hash": h})
    with database:
        database.execute("INSERT OR REPLACE INTO record_observations VALUES (?,?)", (h, json.dumps(observed)))


def archive(database: sqlite3.Connection, h: str, now: float, disposition: str,
            expected_revision: str | None = None) -> str:
    """Atomically retain evidence before removing active work; never unlink files."""
    if disposition not in {"verified_absent", "operator_dismissed"}:
        raise ValueError("Invalid archive disposition")
    with database:
        database.execute("BEGIN IMMEDIATE")
        record = snapshot(database, h)
        if not any(record[table] for table in ("pending", "filesystem_retry", "cleanup_retry", "record_observations")):
            return "Record is already inactive"
        if expected_revision is not None and revision(record) != expected_revision:
            raise ValueError("Record changed; refresh and review it again")
        name, _ = label(record)
        database.execute("INSERT INTO record_archive(hash,name,retired_at,disposition,evidence) VALUES (?,?,?,?,?)",
                         (h, name, now, disposition, json.dumps(record)))
        for table in ACTIVE_TABLES:
            database.execute(f"DELETE FROM {table} WHERE hash=?", (h,))
    return "Record archived; no files or torrents were deleted"


def connect(path: Path, writable: bool = False) -> sqlite3.Connection:
    """Open an existing journal only; web requests must not initialize it."""
    return sqlite3.connect(path.absolute().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=1)


def enqueue(path: Path, h: str, expected_revision: str, now: float) -> str:
    """Persist a bounded request queue, without modifying any active record."""
    database = connect(path, True)
    try:
        with database:
            database.execute("BEGIN IMMEDIATE")
            current = snapshot(database, h)
            if not any(current[table] for table in ("pending", "filesystem_retry", "cleanup_retry", "record_observations")):
                raise ValueError("Record is already inactive; refresh the page")
            if revision(current) != expected_revision:
                raise ValueError("Record changed; refresh and review it again")
            existing = database.execute("SELECT id FROM record_requests WHERE hash=? AND revision=? AND state='queued'", (h, expected_revision)).fetchone()
            if existing:
                return existing[0]
            if database.execute("SELECT count(*) FROM record_requests WHERE state='queued'").fetchone()[0] >= 32:
                raise ValueError("Request queue is full; try again after polling recovers")
            request_id = secrets.token_hex(16)
            database.execute("INSERT INTO record_requests VALUES (?,?,?,?,'queued',?)",
                             (request_id, h, expected_revision, now, "Waiting for a successful qBittorrent poll"))
            database.execute("DELETE FROM record_requests WHERE state!='queued' AND id NOT IN "
                             "(SELECT id FROM record_requests WHERE state!='queued' ORDER BY created_at DESC LIMIT 20)")
            return request_id
    finally:
        database.close()


def checked(database: sqlite3.Connection, h: str, now: float, detail: str) -> None:
    """Persist why a read-only absence check retained work, without changing its revision."""
    with database:
        database.execute("INSERT OR REPLACE INTO record_checks VALUES (?,?,?)", (h, now, detail[:2000]))


def view(path: Path) -> dict:
    """Expose current actionable records/results using a short read transaction."""
    database = connect(path)
    try:
        database.execute("PRAGMA query_only=ON")
        database.execute("BEGIN")
        records = {}
        hashes = database.execute("SELECT hash FROM pending UNION SELECT hash FROM filesystem_retry UNION SELECT hash FROM cleanup_retry UNION SELECT hash FROM record_observations LIMIT 100").fetchall()
        for (h,) in hashes:
            current = snapshot(database, h)
            name, reason = label(current)
            check = current["record_checks"]
            records[h] = {"id": h, "revision": revision(current), "name": name, "reason": reason,
                          "retirement_check": {"at": check[0][1], "detail": check[0][2]} if check else None}
        requests = [{"id": request_id, "record_id": h, "state": state, "message": message, "name": name}
                    for request_id, h, state, message, name in database.execute(
                        "SELECT r.id,r.hash,r.state,r.message,coalesce(p.name,"
                        "(SELECT name FROM record_archive a WHERE a.hash=r.hash ORDER BY a.id DESC LIMIT 1),'Retained download') "
                        "FROM record_requests r LEFT JOIN pending p ON p.hash=r.hash ORDER BY r.created_at DESC LIMIT 52")]
        archived = [{"name": name, "at": at, "disposition": disposition}
                    for name, at, disposition in database.execute("SELECT name,retired_at,disposition FROM record_archive ORDER BY id DESC LIMIT 10")]
        return {"available": True, "records": list(records.values()), "requests": requests, "archived": archived}
    finally:
        database.close()
