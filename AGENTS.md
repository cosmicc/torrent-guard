# AGENTS.md — Torrent Guard

This is the root-level coding-agent guide for Codex and maintainers. Read alongside the current code, README.md, compose.yaml and test suite before changing behavior. The code at HEAD is authoritative if anything here is outdated.

## Purpose and deployment

Torrent Guard is a long-running Python 3.12 daemon deployed via Docker Compose on a media-management host running qBittorrent, Sonarr and Radarr. Sonarr/Radarr select torrent releases; qBittorrent downloads them. The guard discovers fake releases with dangerous file extensions, confirms finished torrents contain actual video, asks the responsible Arr instance to blocklist bad releases, and removes bad downloads with qBittorrent's deleteFiles=true. Valid completed torrents should be removed with data only after Sonarr/Radarr has demonstrably imported every video file and the import grace interval has elapsed.

The highest priority is preventing deletion of useful, unimported media. Missing metadata, API outages, ambiguous ownership, missing/moving files and incomplete import history must not be treated as proof of a bad torrent or a completed import.

There is no web service, exposed port, qBittorrent completion hook, Docker socket access, writable media mount or external database server. The app polls qBittorrent and Arr APIs and records cleanup operations in a local SQLite journal.

## Project structure

- guard.py — entire production service:
  - Config, ArrInstance, required, boolean, api_url: environment/config parsing and validation.
  - Http: urllib requests, form/JSON handling, cookie jar, HTTP errors.
  - Qbit: login, torrent/file listing, stop/pause and deletion with data.
  - ArrApi: matching queue items by infohash, blocklist queue entry, paginated import history.
  - selected_files, normalized_name, effective_extension, video_files, classify: metadata filtering.
  - torrent_app, is_complete: categories/tags, scope and completion.
  - MediaValidator: safe download path mapping and ffprobe verification.
  - import_confirmed: strict import-history evidence before successful-download cleanup.
  - PendingStore: persistent SQLite pending operations and phase updates.
  - Guard.poll, Guard._reject, Guard._process_pending: orchestration and retries.
  - setup_logging, main: rotating logs, continuous polling and error recovery.
- tests/test_guard.py — metadata classification, API, queue, journal and deletion failures.
- tests/test_v02.py — completed-media validation and import-history cleanup.
- tests/test_v03.py — incomplete/complete directories, .!qB, path containment and moves.
- tests/test_v031.py — qBittorrent 5.2+ login compatibility.
- Dockerfile — Alpine Python 3.12, ffmpeg/ffprobe, non-root UID/GID 65534.
- compose.yaml — container with read-only root, no Linux capabilities, no new privileges, temporary /tmp, persistent /state and read-only /downloads.
- .env.example — nonsecret examples; real .env MUST stay untracked.
- .gitignore and .dockerignore — ignore credentials, DBs, logs, caches and build artifacts.
- README.md — operator manual and configuration reference.
- RELEASE_NOTES.md, VERSION — release notes/version; update together for application releases.
- .github/workflows/test.yml — Python unit tests and syntax checks on push/PR.
- .github/workflows/release.yml — validates tests and publishes a vVERSION GitHub Release when VERSION changes on main or via dispatch.
- LICENSE — preserve license and honor it for dependencies.

## Runtime flow

1. Poll qBittorrent GET /api/v2/torrents/info?filter=all every POLL_SECONDS (default 5 seconds).
2. Route a torrent to Sonarr or Radarr using the exact configured qBittorrent category and/or tv/movies tag. Conflicting ownership is skipped.
3. Fresh torrents (MAX_AGE_HOURS defaults to 24) are inspected using GET /api/v2/torrents/files?hash=HASH. Wait when metadata is unavailable. Only selected files (priority not zero) are considered. Flag dangerous extensions (.exe, .msi, .ps1, .bat, .scr, etc.) even if video is also present. REJECT_NO_VIDEO optionally blocks releases containing no recognized video extension. Filenames alone do not prove a valid movie.
4. For bad releases, create a durable rejection in PendingStore. Processing must first stop qBittorrent, obtain a matching Arr queue item by infohash, call Arr rejection/blocklisting while keeping the torrent in qBittorrent, then explicitly ask qBittorrent POST /api/v2/torrents/delete with hashes=HASH and deleteFiles=true. Verify the torrent record disappears. Keep the download if its Arr blocklist outcome cannot be confirmed; never let Arr remove the torrent first.
5. For completed torrents, respect progress, amount_left, completion_on, moving/checking states and the completed settle delay. Only inspect files after they are in the configured final completed directory. Confirm all selected video files using ffprobe readable video stream metadata. If paths are moving, absent, unreadable, or unsafe, DEFER rather than reject. A completed release with no video or an actual invalid ffprobe result can be rejected.
6. Valid video remains in qBittorrent awaiting matching Arr history. GET /api/v3/history paginated with page/pageSize/sortKey=date/sortDirection=descending. Match downloadFolderImported event type and downloadId to the same torrent infohash, require import occurred after added_on, and require the import age >= IMPORT_GRACE_MINUTES (minimum 10; current operator log showed 30). Multiple selected video files require import evidence for all of them. An absent queue item or a completed qBittorrent download alone is NEVER proof of successful Arr import.
7. Once all import and retention criteria hold, delete the qBittorrent torrent plus its tracked data without blocklisting the successfully imported release. The Arr-managed main movie/TV library must remain intact; imported hardlinked or copied files should not be removed.
8. Log ACCEPT metadata/completed, REJECT candidate, DEFER validation, WAIT import, DRY RUN, PENDING, CLEANUP, DELETE, CLEANED, warnings and errors. Pending journal phases must be restart-safe and idempotent.

### Critical existing code defect — fix before enabling imported-torrent cleanup

At v0.3.1, Guard._process_pending currently sends BOTH journal kinds (reject and imported) through ArrApi.blocklist_and_remove after a queue match. This may blocklist legitimate, already-imported media, contrary to intended behavior, and it may leave imported cleanup stuck if its queue item has already disappeared. Separate rejection and successful-import cleanup paths. Add tests asserting Arr blocklist is NEVER invoked for successfully imported torrents, including an already-missing Arr queue entry, and that safety conditions still retain data when verification is ambiguous. **Do not assume existing unit tests already cover this.** Recommend DRY_RUN=true until this is fixed and validated.

## Download directories and file-name suffixes

User's qBittorrent setup:

    /downloads/incomplete/  -- actively downloading files; often *.mkv.!qB
    /downloads/complete/    -- after completion and final move

qBittorrent's Append .!qB extension to incomplete files should remain enabled. The qBittorrent API generally exposes original filenames; normalized_name also strips an optional trailing .!qB for metadata classification. Thus fake.exe.!qB remains detectable as executable. NEVER ffprobe an incomplete .!qB file; wait for qBittorrent to finish, rename and move it.

DOWNLOADS_HOST_PATH is the host parent containing BOTH complete and incomplete. Compose mounts it read-only at /downloads within Torrent Guard. QBIT_DOWNLOAD_ROOT, QBIT_COMPLETE_DIR and QBIT_INCOMPLETE_DIR describe the paths seen by qBittorrent; GUARD_DOWNLOAD_ROOT describes the mount path inside Torrent Guard. These can differ when services use different internal path mappings.

Always calculate mapped paths relative to the trusted qBittorrent root. Reject unsafe path traversal, symlink escapes and directories outside the expected completed root. Distinguish save_path/content_path from download_path: download_path may still refer to the configured temporary directory after completion. Missing completed files during a move are a defer/retry condition.

## Network, API and authentication

QBIT_URL, SONARR_URL and RADARR_URL must be addresses reachable FROM THE GUARD CONTAINER, not merely host browser addresses. Linux Docker Compose maps host.docker.internal to the host gateway; Docker service names such as radarr:7878 work only when containers share a network. Use the actual published ports and any configured URL base.

Qbit login sends POST /api/v2/auth/login and maintains a cookie jar. Older qBittorrent releases respond HTTP 200 with Ok.; qBittorrent 5.2+ can respond HTTP 204 with an empty body. The v0.3.1 implementation accepts both, verifies the authenticated session with GET /api/v2/app/version, and uses the cookie jar for SID/QBT_SID_* cookies. Never treat HTTP 200 with arbitrary empty content as authentication success. Do not print passwords or API keys.

Sonarr/Radarr use X-Api-Key header with /api/v3 paths. History pagination parameters are valid for Radarr and Sonarr. Use UTC timestamps; do not assume API history is complete if pages fail or are truncated. A missing, ambiguous or unmatchable history record means preserve the torrent.

### Observed issue: HTTP 400 Invalid Hostname from Radarr history

On Oct 21, 2026, the guard logged successful qBittorrent v5.3.0 authentication and successful ffprobe, then an HTTP 400 at a Radarr history request to port 7878 returning an HTML page starting Bad Request - Invalid Hostname. That HTML is characteristic of Windows HTTP.sys or an upstream gateway, not a normal Radarr JSON API response. Verify RADARR_URL points to the right service, IP, port, hostname and HTTP Host binding; look for a reverse proxy, IIS/HTTP.sys, WAF or environment HTTP_PROXY/HTTPS_PROXY settings used by urllib. Run GET /api/v3/system/status from INSIDE the guard container with X-Api-Key, inspect HTTP status/Server/Content-Type, and compare direct/no-proxy versus configured path. If history returns an error, preserve downloads. Do not bypass API errors by assuming an import or deleting anyway.

## Security, storage, persistence and permissions

The current compose.yaml maps:

    /config/torrent-guard:/state
    host download root:/downloads:ro

The host /config/torrent-guard tree stores the SQLite journal and rotating audit logs: /state/torrent-guard.sqlite3, /state/torrent-guard.log and rotated backups. The container is UID/GID 65534:65534; its /state directory must permit file creation, renaming and writing. Example host setup:

    sudo install -d -o 65534 -g 65534 -m 750 /config/torrent-guard

Permissions on individual log files alone are insufficient: log rotation and SQLite need write permission on the directory. Check ownership, ACLs, volume RW flag, NFS UID mapping, SELinux and filesystem mount options before reducing security. Never use world-writable state directories as the normal fix.

The media bind mount must remain read-only. qBittorrent alone is responsible for deleteFiles=true, including partial files; do not add direct local delete/rmtree calls. Torrent disappearance does not independently certify removal of orphaned/untracked files.

Keep .env (qBittorrent password, Arr keys), state DB and logs outside version control. Logs contain torrent titles/infohashes; do not copy API keys into AGENTS, tests or issue templates. Retain restricted container privileges. Do not require Docker socket or root. Do not delete /state, use docker compose down -v, or auto-reinitialize existing journals during upgrades. When migrating a named Docker volume to a bind mount, data is not copied automatically.

## Configuration and operational controls

Key variables: QBIT_URL/USERNAME/PASSWORD, SONARR_URL/API_KEY/CATEGORY/TAG, RADARR_URL/API_KEY/CATEGORY/TAG, DOWNLOADS_HOST_PATH, QBIT_DOWNLOAD_ROOT, GUARD_DOWNLOAD_ROOT, QBIT_COMPLETE_DIR, QBIT_INCOMPLETE_DIR, VERIFY_VIDEO_CONTENT, COMPLETED_SETTLE_SECONDS, FFPROBE_TIMEOUT_SECONDS, POLL_SECONDS, MAX_AGE_HOURS, REJECT_NO_VIDEO, IMPORT_GRACE_MINUTES, IMPORT_CHECK_SECONDS, HISTORY_LOOKBACK_DAYS, DRY_RUN, STATE_DB, LOG_FILE, LOG_LEVEL, LOG_MAX_BYTES and LOG_BACKUP_COUNT.

DRY_RUN=true means no stop, Arr blocklist, delete or journal mutation; use it during initial validation. Changing environment values requires container recreation, not merely restart:

    docker compose up -d --force-recreate

Rebuild after code changes:

    docker compose up -d --build --force-recreate

Inspect logs:

    docker compose logs -f torrent-guard
    docker compose exec torrent-guard tail -n 100 /state/torrent-guard.log

Treat docker compose config output as possibly containing secrets; sanitize before posting logs. For stuck pending records, inspect journal phases (match, arr, delete, verify) and remote state, not just repeat deletion commands.

## Coding, testing and release requirements

- Read the affected function and relevant tests before edits. Keep changes small and use standard-library Python unless new dependencies are justified.
- Preserve dry-run guarantees, cross-Arr category/tag isolation, defer-vs-reject distinction and explicit deletion with data through the qBittorrent API.
- Test fail-closed behavior on unreachable Arr APIs, incomplete/late move, malformed paths, unknown file types, repeated HTTP errors, journal restarts, and multi-episode partial imports. Assert the ORDER and ABSENCE of destructive calls as well as expected calls.
- Run: python3 -m unittest discover -s tests -v
- Run: python3 -m py_compile guard.py
- Run: docker compose config using safe dummy .env values when appropriate; NEVER commit secrets.
- Update README and .env.example if any operator-facing flag or behavior changes. Document state migrations and safety impact.
- For app releases, update guard.py VERSION, VERSION file, compose.yaml image tag and RELEASE_NOTES.md together. The GitHub release workflow triggers on VERSION changes to main; docs-only commits normally need no version bump.
- Unit tests use mocks. Never claim they verify live qBittorrent/Radarr/Sonarr behavior or physical data deletion. Recommend a dry run and review on the actual media server after a destructive-flow change.

## Non-goals and immutable safety rules

Do not delete main library files; do not add filesystem write access to media; do not disable or silently bypass import verification; do not blocklist good imports; do not manage torrents belonging to unrelated qBittorrent categories; do not claim checksum, full decoding or malware safety from ffprobe; do not silently swallow operational errors.

Conservative retention is preferable to irreversible data loss.
