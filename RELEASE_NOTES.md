# Torrent Guard v0.3

## Highlights

- Support qBittorrent `/downloads/incomplete` and `/downloads/complete` workflows without disabling the `.!qB` incomplete-file extension.
- Normalize an optional trailing `. !qB` style suffix (actual literal `.!qB`) for **early metadata-only** file classification; `.exe.!qB` is rejected, `.mkv.!qB` is recognized as video.
- Only validate completed media against the configured **final** save directory. If qBittorrent is still moving the torrent, reports content in `incomplete/`, or a completed file is not present/readable yet, postpone inspection rather than reject it.
- Keep v0.2 safeguards: ffprobe validation, Arr blocklist before bad-torrent deletion, qBittorrent `deleteFiles=true`, and import-history-verified cleanup at least 10 minutes after import.
- Adds regression tests for incomplete suffixes, incomplete-to-complete relocation, path mapping and late file availability.

## Upgrade

1. `git pull --ff-only`
2. In `.env` confirm `DOWNLOADS_HOST_PATH` mounts the **host parent folder** containing both `incomplete/` and `complete/`.
3. Set `QBIT_COMPLETE_DIR=/downloads/complete` and `QBIT_INCOMPLETE_DIR=/downloads/incomplete` (paths **inside qBittorrent**) if they differ from the defaults; root is `QBIT_DOWNLOAD_ROOT=/downloads`.
4. Leave the qBittorrent `Append .!qB extension to incomplete files` setting **enabled**.
5. `docker compose up -d --build`; use `docker compose logs -f torrent-guard` to inspect decisions.
6. Leave `DRY_RUN=true` for the first pass, then explicitly enable cleanup if the reported file paths and decisions look correct.

The state SQLite journal and `.env` settings from v0.2 are backward-compatible. **Never** run `docker compose down -v` during an upgrade. Deleting a torrent with `deleteFiles=true` tells qBittorrent to remove its tracked data; that does not independently certify removal of orphaned files.
