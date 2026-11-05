"""Restricted status HTTP server with archive-only bookkeeping requests.

Nginx provides TLS and authentication. A source-address allowlist restricts the
backend independently of forwarded headers. HTTP handlers only serve packaged
assets, cached observations and current bookkeeping. The sole POST route queues
archive requests; handlers never initiate diagnostics, cleanup or active-record edits.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name("web")
REFRESH_SECONDS = 60
STALE_SECONDS = 180


@dataclass(frozen=True)
class WebSettings:
    """Validate the backend listener and trusted proxy networks at startup."""

    enabled: bool
    port: int
    allowed_clients: tuple

    @classmethod
    def from_env(cls) -> "WebSettings":
        """Do not infer trusted clients from Host or X-Forwarded-For."""
        from guard import boolean

        port = int(os.getenv("STATUS_PORT", "8080"))
        if not 1024 <= port <= 65535:
            raise ValueError("STATUS_PORT must be between 1024 and 65535")
        networks = tuple(ipaddress.ip_network(value.strip(), strict=False)
                         for value in os.getenv("STATUS_ALLOWED_CLIENTS", "127.0.0.1/32,::1/128").split(",") if value.strip())
        if not networks or len(networks) > 16 or any(network.prefixlen == 0 for network in networks):
            raise ValueError("STATUS_ALLOWED_CLIENTS requires 1–16 specific proxy IPs/CIDRs; unrestricted networks are forbidden")
        return cls(boolean("STATUS_ENABLED", True), port, networks)


def polling_status() -> dict:
    """Read liveness without advancing it, including when collection is stalled."""
    from guard import HEARTBEAT_FILE, healthcheck

    try:
        age = time.monotonic() - float(HEARTBEAT_FILE.read_text(encoding="ascii"))
        if not math.isfinite(age) or age < 0:
            raise ValueError("invalid heartbeat")
        return {"healthy": healthcheck() == 0, "age_seconds": round(age)}
    except (OSError, ValueError):
        return {"healthy": False, "age_seconds": None}


class StatusCache:
    """Publish atomic snapshots collected independently of Guard's poll loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.snapshot = None
        self.error = None

    def collect_once(self) -> None:
        """Bound an entire diagnostic pass, preserving older observations on failure."""
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("status_report.py")), "--collect"],
                                    capture_output=True, text=True, timeout=120, check=True)
            if len(result.stdout) > 2 * 1024 * 1024:
                raise ValueError("oversized snapshot")
            snapshot = json.loads(result.stdout)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("collected_at"), (int, float)):
                raise ValueError("invalid snapshot")
            with self.lock:
                self.snapshot, self.error = snapshot, None
        except (OSError, ValueError, subprocess.SubprocessError):
            # No logging here: the daemon's SQLite logging handler belongs to
            # its polling thread. Publish this monitoring failure on the page.
            with self.lock:
                self.error = "Diagnostic collection unavailable; retrying automatically"

    def run(self) -> None:
        """Collect at most once per minute regardless of browser/request count."""
        while not self.stop.is_set():
            self.collect_once()
            self.stop.wait(REFRESH_SECONDS)

    def read(self) -> dict:
        """Copy bounded data, then add current heartbeat and cache freshness."""
        with self.lock:
            payload = dict(self.snapshot) if self.snapshot else {}
            payload["collection_error"] = self.error
        at = payload.get("collected_at")
        age = time.time() - at if at is not None else None
        payload.update(ready=at is not None, stale=age is None or age < 0 or age > STALE_SECONDS,
                       cache_age_seconds=max(0, round(age)) if age is not None else None,
                       poll=polling_status(), served_at=time.time())
        return payload


class StatusServer(ThreadingHTTPServer):
    """Limit concurrent clients and connection lifetimes behind the trusted proxy."""

    daemon_threads = True
    request_queue_size = 16
    allow_reuse_address = True

    def __init__(self, address: tuple, cache: StatusCache, allowed_clients: tuple, state_db: Path | None = None):
        self.cache = cache
        self.allowed_clients = allowed_clients
        self.slots = threading.BoundedSemaphore(8)
        self.state_db = state_db
        self.csrf_token = secrets.token_urlsafe(32)
        self.assets = {route: (ASSETS / filename).read_bytes() for route, filename in
                       (("/", "index.html"), ("/app.css", "app.css"), ("/app.js", "app.js"), ("/icon.svg", "icon.svg"))}
        super().__init__(address, StatusHandler)

    def process_request(self, request, client_address) -> None:
        """Reject excess connections without allocating unbounded threads."""
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        """Always release the concurrency slot after disconnects/timeouts."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address) -> None:
        """Client transport failures do not print private paths or stack traces."""


class StatusHandler(BaseHTTPRequestHandler):
    """Serve fixed status routes and a protected, queue-only archival action."""

    server_version = "TorrentGuard"
    sys_version = ""

    def setup(self) -> None:
        """Keep idle/slow connections from holding a handler indefinitely."""
        self.request.settimeout(5)
        super().setup()

    def log_message(self, format, *args) -> None:
        """Avoid request logs containing client-controlled paths or credentials."""

    def send_error(self, code, message=None, explain=None) -> None:
        """Never echo client-supplied URLs or method text into error responses."""
        self.respond(code, "text/plain; charset=utf-8", b"Request unavailable\n")

    def respond(self, code: int, content_type: str, body: bytes) -> None:
        """Apply no-store and self-contained browser policies to every response."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        """Use identical access checks and headers for proxy HEAD requests."""
        self.do_GET()

    def do_GET(self) -> None:
        """Read cached diagnostics/current bookkeeping, never trigger probes."""
        address = ipaddress.ip_address(self.client_address[0])
        if not any(address in network for network in self.server.allowed_clients):
            self.respond(403, "text/plain; charset=utf-8", b"Proxy address not allowed\n")
            return
        route = urlsplit(self.path).path
        if route == "/api/status":
            payload = self.server.cache.read()
            payload["csrf_token"] = self.server.csrf_token
            from guard import removal_reference
            payload["removal_rules"] = removal_reference()
            if self.server.state_db is not None:
                from records import view
                from status_report import journal_data, private_text
                try:
                    # Fast bookkeeping reads bypass the slower diagnostic cache.
                    def clean(value):
                        if isinstance(value, str):
                            return private_text(value)
                        if isinstance(value, dict):
                            return {key: private_text(item) if key in {"name", "reason", "message"} and isinstance(item, str)
                                    else clean(item) if isinstance(item, (dict, list)) else item for key, item in value.items()}
                        if isinstance(value, list):
                            return [clean(item) for item in value]
                        return value
                    actions = view(self.server.state_db)
                    payload["record_actions"] = clean(actions)
                    # An accepted archive should disappear immediately from
                    # active-work counters, without waiting for API diagnostics.
                    payload["journal"] = journal_data(self.server.state_db,
                        {record["id"]: record["name"] for record in actions["records"]})
                except (OSError, sqlite3.Error):
                    payload["record_actions"] = {"available": False}
                    payload["journal"] = {"available": False, "detail": "Current bookkeeping is unavailable"}
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.respond(200, "application/json; charset=utf-8", body)
        elif route in self.server.assets:
            content_type = {"/": "text/html", "/app.js": "text/javascript", "/app.css": "text/css", "/icon.svg": "image/svg+xml"}[route]
            self.respond(200, content_type + "; charset=utf-8", self.server.assets[route])
        else:
            self.respond(404, "text/plain; charset=utf-8", b"Not found\n")

    def do_POST(self) -> None:
        """Queue archive-only work after source, origin, token and body checks."""
        from records import enqueue

        address = ipaddress.ip_address(self.client_address[0])
        if not any(address in network for network in self.server.allowed_clients):
            self.respond(403, "text/plain", b"Proxy address not allowed\n")
            return
        if self.path != "/api/forget":
            self.respond(404, "text/plain", b"Not found\n")
            return
        try:
            origin = urlsplit(self.headers.get("Origin", ""))
        except ValueError:
            self.respond(403, "text/plain", b"Origin invalid\n")
            return
        host = self.headers.get("Host", "")
        token = self.headers.get("X-CSRF-Token", "")
        if (origin.scheme not in {"http", "https"} or origin.netloc != host
                or origin.path or origin.query or origin.fragment
                or not token.isascii()
                or not secrets.compare_digest(token, self.server.csrf_token)):
            self.respond(403, "text/plain", b"Origin or action token invalid; refresh the page\n")
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" or self.headers.get("Transfer-Encoding"):
            self.respond(415, "text/plain", b"A JSON request is required\n")
            return
        try:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("Invalid request size") from None
            if not 0 < size <= 1024:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(size))
            if (not isinstance(payload, dict) or set(payload) != {"record_id", "revision"}
                    or not isinstance(payload["record_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["record_id"])
                    or not isinstance(payload["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["revision"])):
                raise ValueError("Invalid record identity")
            if self.server.state_db is None:
                self.respond(503, "text/plain", b"Bookkeeping queue unavailable\n")
                return
            request_id = enqueue(self.server.state_db, payload["record_id"], payload["revision"], time.time())
        except (ValueError, UnicodeError) as exc:
            from status_report import private_text
            # Only locally authored validation messages; never echo the body.
            message = str(exc) if not isinstance(exc, (json.JSONDecodeError, UnicodeError)) else "Invalid JSON"
            self.respond(409, "text/plain; charset=utf-8", private_text(message).encode())
            return
        except (OSError, sqlite3.Error):
            self.respond(503, "text/plain", b"Bookkeeping queue unavailable; try again after polling recovers\n")
            return
        self.respond(202, "application/json", json.dumps({"request_id": request_id, "message": "Queued; no files or torrents will be deleted"}).encode())


def start_status_server() -> StatusServer | None:
    """Start bounded web/collector threads without moving Guard polling off-thread."""
    settings = WebSettings.from_env()
    if not settings.enabled:
        return None
    cache = StatusCache()
    server = StatusServer(("0.0.0.0", settings.port), cache, settings.allowed_clients,
                          Path(os.getenv("STATE_DB", "/state/torrent-guard.sqlite3")))
    threading.Thread(target=server.serve_forever, name="status-http", daemon=True).start()
    threading.Thread(target=cache.run, name="status-collector", daemon=True).start()
    return server
