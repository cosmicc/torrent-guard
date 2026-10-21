# Changelog

Release dates use MM.DD.YYYY. Historical entries were migrated from the release
notes recorded in Git. All versions below were prepared on 10.20.2026.

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
