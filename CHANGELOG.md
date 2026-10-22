# Changelog

Release dates use MM.DD.YYYY. Historical entries were migrated from the release
notes recorded in Git. Versions through 0.7 were prepared on 10.20.2026.

## 1.0 - 10.21.2026

### Added

- One-hour observed metadata-download timeout for Sonarr/Radarr torrents, with
  persistent timers, Arr blocklisting before removal, and zero-payload audit evidence.
- Read-only SSH log viewer with event blocks, indented details, terminal-width
  wrapping, rotation following and optional container execution.
- Recognize zero-data `queuedDL`, `metaDL` and `forcedMetaDL` downloads using
  explicit zero progress, lifetime downloaded bytes and completed bytes.

### Changed

- Start the metadata timer when Guard first observes metadata downloading;
  preserve it across restarts and reset it on observed state exit, disappearance,
  torrent replacement or ownership change. Queued downloads do not time out.
- Use bright dark-background ANSI colors, replacing blue activity with cyan.
- Wait normally for zero-data queues and metadata downloads with empty file
  metadata; their download directories do not need to exist yet.
- Upgrade adds the SQLite `metadata_wait` table without replacing existing
  journals or file evidence. No new environment settings are required. Rebuild
  and recreate the image, retaining any cleanup override and existing `/state`.

### Fixed

- Zero-data queued and metadata torrents no longer block unrelated leftover
  cleanup. Known files/content paths, unresolved filesystem holds, prior data,
  missing counters and API failures retain normal ownership protections.
- Reject timed-out zero-payload magnets without inventing filenames or claiming
  filesystem deletion. Missing Arr queue matches and unconfirmed blocklisting
  still prevent removal; recovered metadata or changed data retain the torrent.
- Make long audit messages readable over SSH without discarding details or
  embedding color codes in saved logs.

## 0.9 - 10.21.2026

### Added

- Persistent automatic cleanup retries at 60, 120, 240, 480, then 900 seconds,
  with the next retry time and attempt count in diagnostics.

### Changed

- Download logs use names and recorded paths instead of infohash identifiers,
  including after restart or removal from qBittorrent.
- Share failed ownership checks within a poll to avoid repeatedly querying the
  same missing metadata for every leftover. Successful checks remain fresh.

### Fixed

- Identify the specific live torrent blocking leftover cleanup when its file
  metadata is unavailable, alongside the affected leftover's saved paths.
- Prevent cleanup API and safety-check failures from retrying every poll;
  restart preserves deadlines and successful verification clears retry state.
- Omit query strings from HTTP error endpoints and include the exact target
  path when a local removal fails.

## 0.8 - 10.21.2026

### Added

- Startup log reports whether automatic leftover cleanup is enabled, its settle
  interval, and its writable completed-root alias.

### Changed

- Pending disk verification distinguishes disabled cleanup, the deletion settle
  deadline, and paths still present after an automatic cleanup attempt.

### Fixed

- Replace the ambiguous “waiting for settle/cleanup or manual review” warning
  with the actual blocker and instructions to enable the cleanup override.
- Correct host/container path examples to `/mnt/downloads/complete` and
  `/downloads/complete`; clarify the separate `/cleanup/completed` write alias.

## 0.7 - 10.20.2026

### Added

- Persistent filesystem-error holds and per-torrent retry backoff of 30, 60,
  120, 240, then 300 seconds, including across daemon restarts.
- Regression tests for stale handles during resolution/stat/open, ffprobe I/O
  failures, incomplete-torrent retention, recovery, and dry-run journal safety.

### Changed

- Check actual stat/open/read access and preflight tracked files before destructive
  operations. Completion-state changes cannot bypass unresolved filesystem errors.
- Add a filesystem_retry SQLite table automatically without discarding existing
  state. No new environment settings are required.

### Fixed

- Treat ESTALE and unresolved filesystem access failures as DEFER validation,
  never as proof of invalid media.
- Recheck access after failed ffprobe validation; defer reported I/O errors and
  malformed probe responses while still rejecting readable, genuinely invalid media.

## 0.6 - 10.20.2026

### Added

- Persistent per-file deletion manifests and independent checks for every saved
  download name before reporting cleanup complete, with an audit of verified work.
- Opt-in automatic removal of unchanged completed-download leftovers using a
  dedicated writable bind, current import/ownership checks and private quarantine.
  Verified payloads are permanently unlinked immediately after the final identity
  check; they are not sent to a recycle bin.
- Filesystem tests for replacement races, restarts, mounts, permissions,
  unselected files and protection of imported library copies/hardlinks.

### Changed

- Keep cleanup pending when files remain or verification is uncertain. Allow
  qBittorrent 120 seconds by default before optional local fallback.
- Add evidence/audit SQLite tables without discarding existing records. Legacy
  missing-torrent records without manifests require manual review. Existing
  orphans from already-cleared v0.5 records are not adopted automatically.
- Deployment: standard downloads remain read-only. Enabling completed-download
  fallback requires compose.cleanup.yaml and CLEANUP_COMPLETE_HOST_PATH matching
  QBIT_COMPLETE_DIR. No writable library or download-parent mount is permitted.

### Fixed

- Do not treat torrent disappearance alone as proof that the files were deleted.

## 0.5 - 10.20.2026

### Added

- INFO-level import-wait reasons that distinguish missing import evidence,
  filename mismatches, partial imports, history lookback, and confirmed imports
  still within the cleanup grace period. Log reasons when they change.
- Recurring `CHECK import` summaries show completed candidates and matching
  history hashes, retaining the default 60-second check interval.

### Changed

- Recognize successful import evidence immediately, while retaining the
  configured post-import grace period before deleting tracked downloads.
- Completed-video acceptance now says history is being checked rather than
  suggesting import is missing before the history check runs.
- Upgrade: preserve `.env` and `/state`, rebuild and recreate the container,
  and review `CHECK import`/`WAIT import` in dry-run mode before enabling deletion.
  No new settings or database migration are required.

### Fixed

- Recognize numeric `downloadFolderImported` history event `3` from Radarr and
  Sonarr. Numeric `5` is no longer accepted as import evidence; it represents
  episode deletion in Sonarr.

## 0.4 - 10.20.2026

### Added

- Classic color-coded live Docker logs, approved light/dark palette samples,
  and `LOG_COLOR=always|auto|never`. Persistent audit logs remain plain text.
- Docker healthcheck based on a completed-poll heartbeat, configurable with
  `HEALTHCHECK_MAX_AGE_SECONDS` (default 300), plus periodic processing status logs.
- Cleanup safety regression tests covering restarts, retries, changed media,
  partial imports, missing Arr queue entries, dry runs, and unconfirmed blocklisting.
- Release metadata validation and extraction of the current version's changelog
  entry for GitHub Releases.

### Changed

- Use two-part version numbers beginning with `0.4`; preserve historical versions.
- Revalidate completion, current selected media, import history, and the import
  grace interval before every imported-download deletion attempt, including retries.
- Keep imported cleanup separate from rejection processing, including older
  journal entries in rejection phases. Successful imports never call Arr blocklisting.
- Replace `RELEASE_NOTES.md` with this changelog; releases use only their matching
  version entry and are explicitly marked Latest. Manual release dispatch must run from `main`.
- Move detailed setup, upgrades, configuration, and troubleshooting into `INSTALL.md`.
- Upgrade: keep the existing `.env` and `/state` journal; no schema migration or
  new required configuration is needed. Rebuild and recreate the container with
  `DRY_RUN=true`, review decisions on the media server, then enable cleanup.

### Fixed

- Preserve rejected downloads when Arr returns HTTP 404 without confirmed blocklisting.
- Preserve downloads during upload-side rechecks, absent completion metadata,
  unfinished selected videos, and symlink escapes from the completed directory.
- Reject truncated or malformed Arr history pagination instead of treating it as
  complete import evidence.
- Correct outdated agent guidance and state-directory setup/upgrade instructions.

## 0.3.1 - 10.20.2026

### Added

- Regression tests for qBittorrent login responses and protected-session validation.

### Changed

- Support qBittorrent 5.2+ HTTP 204 empty login responses alongside legacy HTTP
  200 `Ok.` responses, preserving SID and `QBT_SID_<port>` cookie authentication.
- Store the SQLite journal and rotating logs in the host bind mount
  `/config/torrent-guard:/state`. Existing named-volume data must be copied
  explicitly when changing mounts; it is not transferred automatically.

### Fixed

- Resolve repeated `qBittorrent refused login: ''` failures on newer qBittorrent.
- Verify `/api/v2/app/version` after login; reject invalid credentials, arbitrary
  empty HTTP 200 responses, malformed responses, and failed session verification.
- Upgrade: keep existing credentials and settings; rebuild and recreate the
  container. Ensure UID/GID 65534 can write the state directory.

## 0.3 - 10.20.2026

### Added

- Separate final `/downloads/complete` and temporary `/downloads/incomplete`
  directory support through `QBIT_COMPLETE_DIR` and `QBIT_INCOMPLETE_DIR`.
- Regression tests for incomplete suffixes, path mapping, late file availability,
  and incomplete-to-complete moves.

### Changed

- Normalize an optional trailing `.!qB` suffix for metadata classification:
  `.exe.!qB` remains dangerous and `.mkv.!qB` remains a video filename.
- Wait for final renamed, readable files under the configured completed directory
  before ffprobe checks; missing or moving files defer inspection.
- Upgrade: mount the host parent containing both directories, confirm qBittorrent
  path settings, leave “Append .!qB extension to incomplete files” enabled,
  rebuild, and review a dry run. Existing SQLite state remains compatible;
  never erase `/state` or run `docker compose down -v` during upgrades.

### Fixed

- Avoid false rejection of transiently absent media during final moves.

## 0.2 - 10.20.2026

### Added

- Completed-torrent inspection regardless of initial age, with category or
  optional `tv`/`movies` tag ownership and conflict protection.
- ffprobe checks for readable video streams in every selected video using a
  read-only download mount. Invalid media follows rejection/blocklist cleanup.
- Import-history cleanup for the same infohash only after every selected video
  is imported after torrent creation and the import is at least 10 minutes old.
- Rotating persistent audit logs alongside Docker logs, plus expanded offline tests.

### Changed

- Migrate v0.1 SQLite journals in place to support restart-safe imported cleanup.
- Require `DOWNLOADS_HOST_PATH` in Compose, with corresponding
  `QBIT_DOWNLOAD_ROOT` mapping and read permissions for UID 65534.
- Upgrade: merge new settings into the existing `.env`, verify categories and
  tags, rebuild, review dry-run logs, then enable cleanup. Retain existing state.

### Fixed

- Preserve media on inaccessible mounts, missing history, partial multi-video
  imports, and Arr API errors. Keep bad releases stopped when no queue match exists.

## 0.1 - 10.20.2026

### Added

- Initial Docker Compose daemon using qBittorrent and Sonarr/Radarr APIs.
- Early selected-file extension screening, optional no-video rejection, configured
  category scope, and recent-download filtering.
- Stop suspicious torrents, request Arr blocklisting, explicitly delete tracked
  data with qBittorrent `deleteFiles=true`, and verify torrent disappearance.
- Persistent SQLite retry journal, dry-run default, non-root container, automated
  unit tests, and operator documentation; no Docker socket required.

### Changed

- None; initial release.

### Fixed

- None; initial release.

Across all versions, ffprobe is a stream-metadata check, not full decoding,
checksum verification, or malware certification. qBittorrent deletes its tracked
download data; torrent disappearance does not prove removal of orphaned files.
Imported library copies and hardlinks must remain intact. Cleanup can interrupt
seeding; operators must account for tracker requirements.
