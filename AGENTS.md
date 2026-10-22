# AGENTS.md — Torrent Guard

This is the root-level coding-agent guide for Codex and maintainers. Read alongside the current code, README.md, compose.yaml and test suite before changing behavior. The code at HEAD is authoritative if anything here is outdated.

## Purpose and deployment

Torrent Guard is a long-running Python 3.12 daemon deployed via Docker Compose on a media-management host running qBittorrent, Sonarr and Radarr. Sonarr/Radarr select torrent releases; qBittorrent downloads them. The guard discovers fake releases with dangerous file extensions, confirms finished torrents contain actual video, asks the responsible Arr instance to blocklist bad releases, and removes bad downloads with qBittorrent's deleteFiles=true. Valid completed torrents should be removed with data only after Sonarr/Radarr has demonstrably imported every video file and the import grace interval has elapsed.

The highest priority is preventing deletion of useful, unimported media. API outages, ambiguous ownership, missing/moving files and incomplete import history must not be treated as proof of a bad torrent or a completed import. Missing metadata alone is inconclusive; v1.0 adds the explicit observed one-hour zero-payload timeout described below.

There is no web service, exposed port, qBittorrent completion hook, Docker socket access or external database server. Standard Compose uses a read-only download mount. An explicit cleanup override can additionally mount only the completed-download directory writable. Library mounts must never be writable or targeted. The app polls qBittorrent and Arr APIs and records cleanup operations in a local SQLite journal.

## Project structure

- guard.py — API, polling and journal orchestration:
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
  - ConsoleFormatter: safe bright ANSI console palette; plain rotating audit files.
  - write_heartbeat, healthcheck: atomic completed-poll marker and read-only Docker check.
- tests/test_guard.py — metadata classification, API, queue, journal and deletion failures.
- tests/test_v02.py — completed-media validation and import-history cleanup.
- tests/test_v03.py — incomplete/complete directories, .!qB, path containment and moves.
- tests/test_v031.py — qBittorrent 5.2+ login compatibility.
- deletion.py — saved file manifests, independent absence checks, and guarded completed-download quarantine/unlink operations.
- tests/test_deletion.py — actual temporary-file and SQLite tests for verification, leftover removal, library protection and restart/race handling.
- tests/test_nfs.py — stale-handle deferral, bounded backoff, persistent access holds and completion-independent retention.
- Dockerfile — Alpine Python 3.12, ffmpeg/ffprobe, non-root UID/GID 65534.
- compose.yaml — container with read-only root, no Linux capabilities, no new privileges, temporary /tmp, persistent /state and read-only /downloads.
- compose.cleanup.yaml — explicit opt-in writable completed-download bind; never mount the download parent or library writable.
- .env.example — nonsecret examples; real .env MUST stay untracked.
- .gitignore and .dockerignore — ignore credentials, DBs, logs, caches and build artifacts.
- README.md — concise user-facing overview and getting started.
- INSTALL.md — detailed setup, configuration, upgrades and troubleshooting.
- CHANGELOG.md, VERSION — version history/version; update together for application releases. Do not maintain RELEASE_NOTES.md or a separate user changelog.
- scripts/release_notes.py — validate version consistency and extract the current changelog entry for release automation.
- tests/test_cleanup_safety.py, tests/test_release.py — cleanup retry authorization and release metadata regression tests.
- scripts/watch_logs.py — read-only SSH event blocks, width-aware wrapping, bright dark-shell palette, file rotation and partial-write handling; shipped as /app/watch_logs.py.
- tests/test_watch_logs.py — viewer rendering, control escaping and actual-file follow/rotation checks.
- tests/test_metadata_timeout.py — persisted observation deadlines, Arr-first zero-payload rejection, dry-run/restart safety and metadata-recovery races.
- tests/test_monitoring.py — palette isolation, terminal-control escaping and completed-poll health checks.
- docs/log-palette.svg — approved bright log palette with light/dark reference samples. Green success, bright cyan activity, yellow waiting/warnings, red errors/rejections, purple dry run; exact ANSI shades follow the terminal theme.
- docs/deletion-verification.md — required reading before modifying file verification, cleanup permissions, journal manifests, quarantine recovery or deletion safety. Contains v0.6 deployment and validation details. Verified leftovers are permanently unlinked; quarantine is only a temporary identity-check step unless safety checks fail.
- docs/filesystem-retries.md — required reading for v0.7 unresolved filesystem errors, NFS ESTALE handling, ffprobe I/O interpretation, retry persistence and recovery.
- .github/workflows/test.yml — Python unit tests and syntax checks on push/PR.
- .github/workflows/release.yml — validates tests and publishes a stable vVERSION GitHub Release explicitly marked Latest when VERSION changes on main or via dispatch.
- LICENSE — preserve license and honor it for dependencies.

## Runtime flow

1. Poll qBittorrent GET /api/v2/torrents/info?filter=all every POLL_SECONDS (default 5 seconds).
2. Route a torrent to Sonarr or Radarr using the exact configured qBittorrent category and/or tv/movies tag. Conflicting ownership is skipped.
3. Fresh torrents (MAX_AGE_HOURS defaults to 24) are inspected using GET /api/v2/torrents/files?hash=HASH. Wait when metadata is unavailable. Only selected files (priority not zero) are considered. Flag dangerous extensions (.exe, .msi, .ps1, .bat, .scr, etc.) even if video is also present. REJECT_NO_VIDEO optionally blocks releases containing no recognized video extension. Filenames alone do not prove a valid movie.
4. For bad releases, create a durable rejection in PendingStore. Processing must first stop qBittorrent, obtain a matching Arr queue item by infohash, call Arr rejection/blocklisting while keeping the torrent in qBittorrent, then explicitly ask qBittorrent POST /api/v2/torrents/delete with hashes=HASH and deleteFiles=true. Verify the torrent record disappears. Keep the download if its Arr blocklist outcome cannot be confirmed; never let Arr remove the torrent first.
5. For completed torrents, respect progress, amount_left, completion_on, moving/checking states and the completed settle delay. Only inspect files after they are in the configured final completed directory. Confirm all selected video files using ffprobe readable video stream metadata. If paths are moving, absent, unreadable, or unsafe, DEFER rather than reject. A completed release with no video or an actual invalid ffprobe result can be rejected.
6. Valid video remains in qBittorrent awaiting matching Arr history. GET /api/v3/history paginated with page/pageSize/sortKey=date/sortDirection=descending. Match downloadFolderImported (string or numeric 3, never numeric 5) and downloadId to the same torrent infohash, require import occurred after added_on, and require the import age >= IMPORT_GRACE_MINUTES (minimum 10; current operator log showed 30). Check history immediately after media validation and repeat at IMPORT_CHECK_SECONDS; the cleanup grace does not delay history checking. Report missing/partial/mismatched evidence and confirmed-import grace separately at INFO when the reason changes. Grace deadlines are UTC. Multiple selected video files require import evidence for all of them. An absent queue item or a completed qBittorrent download alone is NEVER proof of successful Arr import.
7. Once all import and retention criteria hold, delete the qBittorrent torrent plus its tracked data without blocklisting the successfully imported release. The Arr-managed main movie/TV library must remain intact; imported hardlinked or copied files should not be removed.
8. Log ACCEPT metadata/completed, REJECT candidate, DEFER validation, WAIT import, DRY RUN, PENDING, CLEANUP, DELETE, CLEANED, warnings and errors. Pending journal phases must be restart-safe and idempotent.

### Metadata timeout (v1.0)

Track owned `metaDL`/`forcedMetaDL` torrents independently of `MAX_AGE_HOURS` in
`metadata_wait(hash, added_on, app, first_seen)`. Fail only after more than 3600
seconds since first observation, preserving time across restarts/API outages;
reset on observed state exit/absence, ownership/incarnation change or clock
rollback. Switching between metadata states keeps the same timer. Dry-run
updates only the in-memory copy and must not mutate timer rows during polling.

Pending `kind=metadata` uses rejection phases (match/arr/delete/verify), never
import cleanup. Empty-file removal requires explicit zero-payload counters and
no filesystem hold. Save validated v2 `metadata_only` evidence before stopping;
recheck empty files, zero counters, state and incarnation on pending retries and
before deletion. Stopped states allow recovery after Guard stops a failed magnet.
Arr must confirm blocklisting before qBittorrent deletion. Recovered metadata,
prior payload, missing counters/API failures retain the torrent for review.
A v2 zero-payload audit has no filesystem targets and is never accepted by the
ordinary file verifier or local unlink path. Its CLEANED label reports client
absence only, explicitly without claiming filesystem absence. Preserve existing
v1 file evidence, journals and audits. See INSTALL.md and the deletion guide.

### Cleanup authorization and retries (v0.4)

Scheduled import checks log `CHECK import` at INFO with candidate and matched-hash
counts. These counts are diagnostic only; full per-video evidence and grace still
govern deletion. Keep the default 60-second check interval. Completed-video
acceptance must not claim that an import is missing before checking history.

Imported journal records bypass Arr queue matching and blocklisting in every
phase, including legacy `match`/`arr` records. Before each deletion attempt,
including `verify` retries after restart, recheck completion metadata, settle
delay, current selected files, media readability/validity, history lookback,
all-video import evidence, and import grace. Missing/ambiguous evidence retains
the download. A missing torrent clears an imported record only in `verify`,
after a recorded delete request and independent absence verification of all saved
download names on unchanged mapped roots. Rejection HTTP 404 is not proof of blocklisting;
retain the download and journal for manual review. Keep tests asserting both
the order and absence of destructive calls. The previous v0.3.1 warning that
all imported records went through blocklisting was incorrect: new imported
records already started at `delete`; v0.4 adds explicit isolation for every phase.

## Download directories and file-name suffixes

User's qBittorrent setup:

    /downloads/incomplete/  -- actively downloading files; often *.mkv.!qB
    /downloads/complete/   -- operator's corrected final location

The host final directory is `/mnt/downloads/complete`; the operator sets
`QBIT_COMPLETE_DIR=/downloads/complete` in the host `.env`. Repository defaults
use the same final-directory name; never silently rewrite an
existing installation's environment or infer the final directory from its title.

qBittorrent's Append .!qB extension to incomplete files should remain enabled. The qBittorrent API generally exposes original filenames; normalized_name also strips an optional trailing .!qB for metadata classification. Thus fake.exe.!qB remains detectable as executable. NEVER ffprobe an incomplete .!qB file; wait for qBittorrent to finish, rename and move it.

DOWNLOADS_HOST_PATH is the host parent containing BOTH complete and incomplete. Compose mounts it read-only at /downloads within Torrent Guard. QBIT_DOWNLOAD_ROOT, QBIT_COMPLETE_DIR and QBIT_INCOMPLETE_DIR describe the paths seen by qBittorrent; GUARD_DOWNLOAD_ROOT describes the mount path inside Torrent Guard. These can differ when services use different internal path mappings.

Always calculate mapped paths relative to the trusted qBittorrent root. Reject unsafe path traversal, symlink escapes and directories outside the expected completed root. Distinguish save_path/content_path from download_path: download_path may still refer to the configured temporary directory after completion. Missing completed files during a move are a defer/retry condition.

## Network, API and authentication

QBIT_URL, SONARR_URL and RADARR_URL must be addresses reachable FROM THE GUARD CONTAINER, not merely host browser addresses. Linux Docker Compose maps host.docker.internal to the host gateway; Docker service names such as radarr:7878 work only when containers share a network. Use the actual published ports and any configured URL base.

Qbit login sends POST /api/v2/auth/login and maintains a cookie jar. Older qBittorrent releases respond HTTP 200 with Ok.; qBittorrent 5.2+ can respond HTTP 204 with an empty body. The v0.3.1 implementation accepts both, verifies the authenticated session with GET /api/v2/app/version, and uses the cookie jar for SID/QBT_SID_* cookies. Never treat HTTP 200 with arbitrary empty content as authentication success. Do not print passwords or API keys.

Sonarr/Radarr use X-Api-Key header with /api/v3 paths. History pagination parameters are valid for Radarr and Sonarr. Use UTC timestamps; do not assume API history is complete if pages fail or are truncated. A missing, ambiguous or unmatchable history record means preserve the torrent.

### Observed issue: HTTP 400 Invalid Hostname from Radarr history

An earlier operator report described successful qBittorrent authentication and ffprobe followed by HTTP 400 at a Radarr history request returning an HTML page starting Bad Request - Invalid Hostname. This may indicate Windows HTTP.sys or an upstream gateway rather than Radarr; the source must be verified. Verify RADARR_URL points to the right service, IP, port, hostname and HTTP Host binding; look for a reverse proxy, IIS/HTTP.sys, WAF or environment HTTP_PROXY/HTTPS_PROXY settings used by urllib. Run GET /api/v3/system/status from INSIDE the guard container with X-Api-Key, inspect HTTP status/Server/Content-Type, and compare direct/no-proxy versus configured path. If history returns an error, preserve downloads. Do not bypass API errors by assuming an import or deleting anyway.

## Security, storage, persistence and permissions

The current compose.yaml maps:

    /config/torrent-guard:/state
    host download root:/downloads:ro

The host /config/torrent-guard tree stores the SQLite journal and rotating audit logs: /state/torrent-guard.sqlite3, /state/torrent-guard.log and rotated backups. The container is UID/GID 65534:65534; its /state directory must permit file creation, renaming and writing. Example host setup:

    sudo install -d -o 65534 -g 65534 -m 750 /config/torrent-guard

Permissions on individual log files alone are insufficient: log rotation and SQLite need write permission on the directory. Check ownership, ACLs, volume RW flag, NFS UID mapping, SELinux and filesystem mount options before reducing security. Never use world-writable state directories as the normal fix.

The main download bind mount remains read-only. qBittorrent `deleteFiles=true`
is the primary deletion mechanism. Optional local fallback uses only the completed
root supplied by compose.cleanup.yaml, saved file evidence, a 120-second settle
interval, current import authorization and fresh client ownership checks. Read
docs/deletion-verification.md before changes. Never sweep directories, use rmtree,
follow symlinks, delete unknown/replaced files, or grant write access to a library.
Persist manifests before API deletion and quarantine paths before rename. Only
report CLEANED after all recorded names are independently absent. Missing mounts,
permission errors and legacy missing-torrent records without manifests remain
pending. Preserve deletion_evidence/deletion_audit tables during upgrades.

Keep .env (qBittorrent password, Arr keys), state DB and logs outside version control. Logs contain download titles and paths; do not copy API keys into AGENTS, tests or issue templates. Retain restricted container privileges. Do not require Docker socket or root. Do not delete /state, use docker compose down -v, or auto-reinitialize existing journals during upgrades. When migrating a named Docker volume to a bind mount, data is not copied automatically.

## Configuration and operational controls

Key variables: QBIT_URL/USERNAME/PASSWORD, SONARR_URL/API_KEY/CATEGORY/TAG, RADARR_URL/API_KEY/CATEGORY/TAG, DOWNLOADS_HOST_PATH, QBIT_DOWNLOAD_ROOT, GUARD_DOWNLOAD_ROOT, QBIT_COMPLETE_DIR, QBIT_INCOMPLETE_DIR, VERIFY_VIDEO_CONTENT, COMPLETED_SETTLE_SECONDS, FFPROBE_TIMEOUT_SECONDS, POLL_SECONDS, MAX_AGE_HOURS, REJECT_NO_VIDEO, IMPORT_GRACE_MINUTES, IMPORT_CHECK_SECONDS, HISTORY_LOOKBACK_DAYS, DRY_RUN, STATE_DB, LOG_FILE, LOG_LEVEL, LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_COLOR and HEALTHCHECK_MAX_AGE_SECONDS.

### Monitoring contract

Additional cleanup controls are `CLEANUP_LEFTOVERS` (default false),
`DELETE_SETTLE_SECONDS` (default 120, minimum 30), and the Compose-only
`CLEANUP_COMPLETE_HOST_PATH`. `compose.cleanup.yaml` explicitly enables fallback.
Its read/write alias must identify the same completed root as the read-only
mapping; the application checks device/inode identity before writing.
Log effective cleanup settings at startup. Pending-file warnings must identify
whether automatic cleanup is disabled, its settle deadline, or files remain
after an attempt. Never recommend deleting the journal to retry leftovers;
recreating with the explicit cleanup override resumes existing evidence.

Cleanup API/safety-check exceptions and repeated visible-client deletion attempts
use additive `cleanup_retry` state: 60/120/240/480/900-second capped automatic
backoff, restored after restart and cleared with successful deletion verification.
Keep filesystem holds independent; neither deadline bypasses the other. Dry-run
polling must not mutate cleanup retries. Cache failed ownership checks only within
one poll; refresh successful ownership checks before each cleanup operation.
Operator logs identify download names and actual API/saved paths, not infohashes.
Use saved manifests and journal names after the client forgets a torrent; never
guess deletion targets from titles. When another torrent's metadata blocks
cleanup, include its name/location as well as the cleanup target. HTTP error
endpoints omit query strings. See the deletion guide for retry troubleshooting.

An empty file list from a zero-data `queuedDL`, `metaDL` or `forcedMetaDL` torrent is an expected wait:
require explicit numeric zero `progress`, `downloaded` and `completed`, and a
nonpositive `completion_on`. Do not require its directories to exist. During
ownership scans only this narrow case can skip unavailable metadata, and never
while that torrent has an unresolved filesystem hold. Keep its reported content
path reserved against overlapping cleanup, and always check a known file list.
Missing counters, malformed/API failures and previously downloaded data remain
inconclusive. Refresh state each scan; starting or resuming ends this exception.

`LOG_COLOR=always` is the default for live Docker logs; `auto` requires a TTY
and `never` disables colors. Keep the persistent audit file plain text, escape
external terminal control characters in both outputs, and preserve readable
decision labels without color. Use the approved palette linked above: bright ANSI 92/96/93/91/95 for success/activity/waiting/errors/dry runs. The SSH viewer colors headings only, sanitizes external controls and preserves details without API or journal access. Keep it standard-library-only and read-only; document host and container commands in INSTALL.md.

The daemon writes `/tmp/torrent-guard-heartbeat` atomically using monotonic time
only after `Guard.poll()` returns, including idle and dry-run polls. Clear the
marker at startup. Never refresh it from a background timer or healthcheck:
that would conceal stalled processing. Log progress at INFO at most once per
60 seconds after completed polls. The Dockerfile invokes
`python /app/guard.py --healthcheck` every 30 seconds (5-second timeout,
60-second startup grace, 3 retries). The CLI must bypass credentials and logging
initialization and remain read-only. Freshness defaults to 300 seconds via
`HEALTHCHECK_MAX_AGE_SECONDS`; document tuning for long polls. Handled Arr or
per-torrent failures may coexist with healthy polling; health is not dependency
readiness or proof of deletion. Unhealthy status alone does not trigger a Docker
restart. See INSTALL.md for monitoring commands and operational limits.

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
- Run: python3 -m py_compile guard.py deletion.py scripts/release_notes.py scripts/watch_logs.py
- Run: python3 scripts/release_notes.py (validates version consistency and changelog structure without publishing).
- Run: docker compose config using safe dummy .env values when appropriate; NEVER commit secrets.
- Update README, INSTALL.md and .env.example as appropriate when operator-facing flags or behavior change. Document state migrations and safety impact.
- For app releases, update guard.py VERSION, VERSION file, compose.yaml image tag and CHANGELOG.md together. New versions use only two parts (`0.4`, `0.5`, etc.). Changelog headings use `## X.Y - MM.DD.YYYY` without brackets and each version has Added, Changed and Fixed sections. Preserve historical version spellings such as `0.3.1`; do not add new third-level versions. The GitHub release workflow extracts only that version's changelog entry and triggers on VERSION changes to main; manual dispatch also requires main. Docs-only commits normally need no version bump.
- API tests use mocks; deletion tests also use actual temporary files and SQLite. Never claim they verify live qBittorrent/Radarr/Sonarr behavior or production data deletion. Review a dry run and disposable-download test on the actual media server after a destructive-flow change.

## Non-goals and immutable safety rules

Do not delete main library files; do not add filesystem write access to libraries or the download parent; do not bypass the opt-in completed-root cleanup restrictions; do not disable or silently bypass import verification; do not blocklist good imports; do not manage torrents belonging to unrelated qBittorrent categories; do not claim checksum, full decoding or malware safety from ffprobe; do not silently swallow operational errors.

Conservative retention is preferable to irreversible data loss.

Filesystem failures take precedence over metadata classification and pending
cleanup. Persist unresolved holds in filesystem_retry and invalidate cached
completed-media acceptance. Retry at 30/60/120/240/300 seconds without sleeping
inside the poll loop; restart or completion-state changes must not bypass a hold.
Clear it only after actual filesystem checks recover. Dry-run stores new retry
state only in memory and must not mutate existing retry rows during polling.
Use DEFER validation for ESTALE and other unresolved resolution/stat/open/read
failures. ffprobe I/O failures and malformed output are inconclusive; a failed
probe permits rejection only when access remains readable and the result does
not report an I/O problem. Check all tracked paths before destructive calls,
including incomplete metadata-based rejections and pending retries. See the
filesystem recovery guide for testing and operational limitations.
