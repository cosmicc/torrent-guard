# Torrent Guard v0.2

## Added

- Poll completed torrents in configured Sonarr/Radarr categories **or** `tv`/`movies` qBittorrent tags, even when older than the initial 24-hour inspection window.
- On completion, inspect selected video files with `ffprobe` to confirm the presence of a readable video stream, rather than trusting `.mp4`/`.mkv` filename extensions alone. Files are accessed through a **read-only** download-directory mount.
- Reject completed torrents with missing/invalid media, Arr-blocklist their releases, and call qBittorrent's deletion API with `deleteFiles=true` to remove associated downloads.
- After valid completion, query Sonarr/Radarr import **history**. Delete the torrent and its qBittorrent-managed payload only when every selected video file has a matching import record **at least 10 minutes old** and all imports happened after the torrent was added.
- Persist a rotating audit/diagnostic log (`/state/torrent-guard.log`) with accept/reject, cleanups, warnings and errors. Log to Docker stdout as well.
- Existing v0.1 SQLite journal is migrated in place to include accepted-import cleanup tasks and restart-safe deletion retries.
- Expanded tests, maintained dry-run default, and continued unprivileged container deployment.

## Deployment changes

1. Run `git pull --ff-only` and copy new settings from `.env.example` into existing `.env` (**do not overwrite credentials**).
2. Set **`DOWNLOADS_HOST_PATH`** to the host's download location matching **`QBIT_DOWNLOAD_ROOT`** inside qBittorrent; check UID 65534 can read files. The default Compose file now requires this variable, intentionally preventing a silently unverified deployment.
3. Confirm `SONARR_CATEGORY`, `RADARR_CATEGORY`, and optional `SONARR_TAG=tv` / `RADARR_TAG=movies` correspond to your actual qBittorrent values.
4. `docker compose up -d --build`; inspect `docker compose logs -f torrent-guard`; only then set `DRY_RUN=false` and recreate.
5. Existing volumes are retained; do **not** run `docker compose down -v`.

## Safety boundaries

- Existing imported library copies/hardlinks are not deleted by Torrent Guard; qBittorrent determines which tracked torrent payloads to remove. Torrent entry disappearance **does not guarantee** every file was physically deleted.
- On missing Arr history, incomplete multi-video import, inaccessible media mount or Arr API error, cleanup is deferred. A bad release without a matching Arr queue entry is stopped and held rather than silently deleting without blocklisting.
- Cleanup immediately after import may violate private tracker seeding requirements; this release follows the requested import + 10 minute criterion regardless of ratio, provided all checks pass.
- `ffprobe` checks the media header/stream, **not** every frame, full-file integrity or absence of malicious data.
