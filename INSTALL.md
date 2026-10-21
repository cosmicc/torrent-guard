# Torrent Guard installation and operations

[![Tests](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml/badge.svg)](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml)

**v0.4** · Python 3.12 · Docker Compose · qBittorrent + Sonarr/Radarr · `ffprobe` video checks

Torrent Guard is a background service that examines new and completed movie/TV torrents and cleans up imported releases. It uses **qBittorrent's Web API** and **Sonarr/Radarr's v3 APIs**; no qBittorrent “Run external program on torrent completion” hook is required.

## What it does

1. **On metadata arrival:** Inspect every selected file in fresh torrents. Reject dangerous extensions (`.exe`, `.scr`, `.bat`, `.ps1`, `.js`, `.lnk`, etc.), even when another file looks like a video; reject releases that lack a video file (configurable). A rejected torrent is stopped, its release is blocklisted using the matching Arr queue item, then qBittorrent is instructed to **delete the torrent and all qBittorrent-tracked payloads** with `deleteFiles=true`.
2. **When complete:** Reinspect eligible movie/TV torrents **regardless of their initial age**. Validate downloaded videos using `ffprobe` against the actual media files via a read-only Docker bind mount. Invalid videos are handled as bad releases (stop, blocklist, remove and delete torrent data). Valid releases are left alone until Arr confirms import.
3. **After successful Arr import + 10 minutes:** Require a matching Sonarr/Radarr `downloadFolderImported` history event for the **same infohash**, dated after this torrent was added and at least 10 minutes ago. For multi-video downloads, require a matching import record for every selected video file. Then instruct qBittorrent to delete the torrent and its tracked downloaded files (but **never** blocklist a successful release).
4. **Audit every decision:** Timestamped accepted/rejected/cleanup messages, warnings and errors go to both Docker logs and `/state/torrent-guard.log` with rotation. A SQLite journal survives restarts and retries unfinished deletes.

## Incomplete and completed directories (v0.3)

**Leave qBittorrent's “Append .!qB extension to incomplete files” ON.** qBittorrent normally returns *original torrent filenames* from `/api/v2/torrents/files` even while the physical payload is called `Movie.mkv.!qB`. Guard also handles a filename that happens to include that suffix: `Movie.mkv.!qB` is recognized as video, `Fake.exe.!qB` is recognized as a risky executable. These **early** checks need metadata only, not file access.

Your configured qBittorrent directory layout is:

```text
/downloads/
├── incomplete/     # downloading files, often with .!qB suffix
└── complete/       # final renamed files after qBittorrent finishes moving
```

Guard waits for **both** qBittorrent completion and the final file under `QBIT_COMPLETE_DIR`. It does not inspect media contents in `incomplete/`, does not interpret a transiently missing file as a bad torrent, and does not run ffprobe on `.!qB` data. A reported `content_path` still inside `incomplete/` triggers a **deferred validation**. `download_path` may retain the configured incomplete path after completion and is therefore not used as a completion signal. A missing or unreadable completed file is deferred and retried, never automatically blocklisted based on the missing path alone.

`QBIT_COMPLETE_DIR` and `QBIT_INCOMPLETE_DIR` are **qBittorrent container paths**; `DOWNLOADS_HOST_PATH` is the **host** parent path and **must mount both** locations into Guard as `/downloads:ro`. If qBittorrent uses category subfolders, final `save_path` must still fall under `/downloads/complete`.

No external run-on-completion script or qBittorrent setting change is required.

Torrent eligibility is based on your configured qBittorrent **categories** and/or optional tags such as `movies` and `tv`. The category and tag must not disagree on whether Sonarr or Radarr owns the torrent. Other torrents are skipped. No Docker socket access and no writable media mounts are required.

## Install on the media-manager Docker host

```bash
# Create or use the project directory
git clone https://github.com/cosmicc/torrent-guard.git
cd torrent-guard
cp .env.example .env
chmod 600 .env
nano .env
```

Set the qBittorrent host/credentials and each *Arr API URL/key/category. Common Docker host-published port values are 8080, 8989 and 7878. `host.docker.internal` is mapped by Compose to the Linux Docker host gateway. If services only have private Docker ports, instead join the guard container to their external Docker network and use service names.

**Important: set the download path mapping accurately.** Example:

| Path setting | Value | Meaning |
|---|---|---|
| `DOWNLOADS_HOST_PATH` | `/srv/media/downloads` | Actual host directory storing qBittorrent downloads; mounted into Guard read-only |
| `QBIT_DOWNLOAD_ROOT` | `/downloads` | The **same** root as seen **inside qBittorrent** (or on host if not containerized) |
| `GUARD_DOWNLOAD_ROOT` | `/downloads` | Path **inside Torrent Guard**, provided by the Compose mount |

If qBittorrent sees `/data/torrents` while the host sees `/srv/media/downloads`, use `QBIT_DOWNLOAD_ROOT=/data/torrents` and `DOWNLOADS_HOST_PATH=/srv/media/downloads`. The file paths are mapped relatively; e.g. qBittorrent save path `/data/torrents/complete/movies` with file `Film.mkv` maps to Guard `/downloads/complete/movies/Film.mkv`. Set `QBIT_COMPLETE_DIR=/data/torrents/complete` and `QBIT_INCOMPLETE_DIR=/data/torrents/incomplete` when the qBittorrent root differs. The container runs as UID/GID 65534; this user must be able to traverse directories and **read** the video files. **No write permission** is necessary.

By default Compose **requires** `DOWNLOADS_HOST_PATH`: if omitted, it refuses deployment rather than running incomplete verification. Read-only bind mounting the location does not grant Torrent Guard the ability to delete local media; deletion is carried out by the qBittorrent API.

Prepare persistent state on the Docker host. The directory itself must be
writable for SQLite and log rotation; changing log-file permissions alone is
insufficient:

```bash
sudo install -d -o 65534 -g 65534 -m 750 /config/torrent-guard
```

Start in report-only mode:

```bash
docker compose up -d --build
docker compose logs -f torrent-guard
```

In dry-run mode (`DRY_RUN=true`) no torrent is stopped, blocklisted or deleted. Review the `ACCEPT metadata`, `ACCEPT completed`, `REJECT candidate`, `DRY RUN`, `DEFER validation`, `WAIT import` (DEBUG) and ERROR messages. Verify the actual media paths are readable and that Arr import-history matches work.

When ready to allow cleanup, change `.env` to `DRY_RUN=false` and recreate:

```bash
docker compose up -d --force-recreate
docker compose logs -f torrent-guard
```

View persistent logs:

```bash
docker compose exec torrent-guard tail -n 100 /state/torrent-guard.log
```

### Upgrade to v0.4

```bash
cd torrent-guard
git pull --ff-only
# Compare .env.example to your existing .env; DO NOT overwrite real credentials.
# Add DOWNLOADS_HOST_PATH, QBIT_DOWNLOAD_ROOT, QBIT_COMPLETE_DIR,
# and QBIT_INCOMPLETE_DIR before restarting. Keep DRY_RUN=true while verifying.
docker compose up -d --build --force-recreate
```

No new required environment settings or database schema changes are needed for v0.4.
Optional `LOG_COLOR=always` enables the classic console palette by default;
`HEALTHCHECK_MAX_AGE_SECONDS=300` sets the completed-poll freshness limit.
Existing journal records are retained. Imported cleanup now rechecks current
media and Arr history on every deletion attempt, including after a restart;
older imported records in rejection phases are safely routed to imported cleanup.
Arr HTTP 404 during rejection retains the torrent for manual review because
blocklisting is unconfirmed.

Compose uses the host bind mount `/config/torrent-guard:/state`. If upgrading
from an older `torrent-guard-state` named volume, stop the guard first, back up
the volume, and copy its entire contents (including SQLite sidecar files and
logs) into the host directory while the guard is stopped. Set ownership to
65534:65534 and verify the journal is present before starting again. Changing
the mount does **not** copy existing data automatically. Do **not** run
`docker compose down -v` or erase `/state`: it contains the retry journal and audit log.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD` | required | qBittorrent WebUI API endpoint and login |
| `SONARR_URL`, `SONARR_API_KEY`, `SONARR_CATEGORY` | optional group | Sonarr connection and exact qBittorrent category |
| `RADARR_URL`, `RADARR_API_KEY`, `RADARR_CATEGORY` | optional group | Radarr connection and exact qBittorrent category |
| `SONARR_TAG`, `RADARR_TAG` | `tv`, `movies` in template | Optional matching qBittorrent tag, independently of category |
| `DOWNLOADS_HOST_PATH` | **must set** | Host directory mounted at `/downloads:ro` in Compose |
| `QBIT_DOWNLOAD_ROOT` | `/downloads` | Equivalent download root inside qBittorrent; change when qBittorrent uses another container path |
| `GUARD_DOWNLOAD_ROOT` | `/downloads` | Corresponding path inside Guard; normally unchanged |
| `QBIT_COMPLETE_DIR` | `/downloads/complete` | Final qBittorrent save directory; validation requires complete files here |
| `QBIT_INCOMPLETE_DIR` | `/downloads/incomplete` | Temporary qBittorrent directory, never scanned with ffprobe |
| `VERIFY_VIDEO_CONTENT` | `true` | Use ffprobe on **all selected** video files when a torrent completes |
| `FFPROBE_TIMEOUT_SECONDS` | `30` | Timeout for each ffprobe invocation |
| `COMPLETED_SETTLE_SECONDS` | `60` | Allow completed files to settle before checking video streams |
| `DRY_RUN` | `true` | Log what would be done, without changing any torrents |
| `POLL_SECONDS` | `5` | qBittorrent scan interval (min 2 s) |
| `MAX_AGE_HOURS` | `24` | New torrent early filename inspection age (completed downloads are always eligible) |
| `REJECT_NO_VIDEO` | `true` | Reject a selected file list without recognized video extensions |
| `IMPORT_GRACE_MINUTES` | `10` | Minimum age **of confirmed Arr import**, never just torrent completion; minimum allowed 10 |
| `IMPORT_CHECK_SECONDS` | `60` | Arr history poll interval while completed valid downloads are present |
| `HISTORY_LOOKBACK_DAYS` | `30` | Arr history lookup horizon. Older downloads are retained for review |
| `STATE_DB` | `/state/torrent-guard.sqlite3` | Restart-safe pending deletions |
| `LOG_FILE` | `/state/torrent-guard.log` | Timestamped rotating persistent log (empty disables file logging) |
| `LOG_MAX_BYTES` | `10485760` | File rotation size (10 MiB) |
| `LOG_BACKUP_COUNT` | `5` | Number of retained rotated files |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_COLOR` | `always` | Console ANSI colors: `always`, `auto` (terminal only), or `never`; audit files stay plain text |
| `HEALTHCHECK_MAX_AGE_SECONDS` | `300` | Positive finite seconds since the last completed poll; allow for the longest full media-validation batch and `POLL_SECONDS` |

Use exactly one category per Arr app; tags are optional and must not conflict. To use only one Arr app, remove all of the other app's variables, including its tag.

## Live monitoring and Docker health

```bash
docker compose logs -f --tail=100 torrent-guard
docker compose ps
docker inspect --format '{{json .State.Health}}' torrent-guard
# Manual read-only check; exit code 0 means fresh, 1 means missing/stale/invalid.
docker compose exec torrent-guard python /app/guard.py --healthcheck
```

The classic palette is green for `ACCEPT`/`CLEANED`, blue for routine activity
and `HEARTBEAT`, yellow for waiting/warnings, red for errors/rejections, and
purple for dry runs. Colors use ANSI codes, so exact shades depend on the
terminal theme. See [light and dark palette samples](docs/log-palette.svg).
Use an ANSI-capable terminal for colors; Docker log viewers may render them
differently. Set `LOG_COLOR=never` if a viewer displays raw escape characters.
Persistent `/state/torrent-guard.log` and rotated backups remain plain text;
no second colorized file is created. External control characters are escaped
before console or file output.

A `HEARTBEAT polling active` message appears after the first completed poll
and at most once every 60 seconds afterward. The poll loop atomically writes
a private monotonic-time marker in `/tmp` only after a whole poll returns.
It clears a previous marker on startup. The healthcheck reads this marker
without API calls, credentials, database writes, or extra network exposure.
Idle and dry-run polls count as work; top-level polling/authentication failures
do not refresh the marker. Handled per-torrent and Arr errors still allow a
poll to finish, so consult error logs for dependency failures.

The image checks every 30 seconds, with a 5-second check timeout, 60-second
startup grace, and 3 failed checks before unhealthy status. The default
freshness limit is 300 seconds. A long ffprobe batch can exceed that limit
while still doing useful work; increase `HEALTHCHECK_MAX_AGE_SECONDS` above
the longest expected complete poll and polling interval. Status reports
recent completed processing, not successful imports or physical deletion.
Docker's restart policy does not restart a container solely for being
unhealthy; health status is available for monitoring and alerts.

## Important safety behavior

- **Failed downloads:** Arr queue matching is required to blocklist a bad release. If Arr cannot find that queue item or is offline, the torrent stays stopped/pending and **is not deleted** until the blocklist path succeeds. HTTP 404 after a known match also preserves the torrent: absence does not confirm blocklisting. Review Arr's blocklist and the journal manually; do not bypass this check by repeatedly deleting downloads.
- **Successfully imported downloads:** Only matching Arr **history** authorizes cleanup. A finished torrent alone, disappearance from Arr's queue, a newly added torrent carrying an old reused hash, or a partial Sonarr season-pack import does **not** authorize deletion. The import event must be at least 10 minutes old. Every deletion attempt, including pending retries, rechecks completion, selected media, and history. The imported path never queries or changes the Arr queue and never blocklists a successful import.
- **Deletion:** qBittorrent receives the explicit `deleteFiles=true` parameter. It should remove the torrent and its tracked download data, including partial files. Guard verifies **torrent entry disappearance**, not each file's absence on disk. Untracked/orphaned files may require manual cleanup. Files already imported to library paths are not targeted, including hardlinked library copies (which retain their content when the download link is removed).
- **Media validity:** `ffprobe` checks for readable video stream metadata; it does **not** decode or checksum every frame, certify all audio/subtitle tracks, or guarantee that a file is malware-free. Failure to access a mounted media file **defers** processing rather than falsely classifying the download as malware. A readable but unexpected media codec may still be rejected if ffprobe cannot identify it.
- **Seeding:** Deleting imported torrents after 10 minutes can interrupt seeding or violate tracker requirements. This is the behavior requested; consider increasing the grace period or maintaining seeding separately for private trackers.
- **Arr history:** On API outages, missing download IDs, mismatched source paths, too-old history, or ambiguous multi-video matches, the program preserves the torrent. It cannot guarantee that *Arr always associates every imported episode with its torrent hash. Check DEBUG logs if valid releases do not clean up.
- **Pending state:** A past v0.1 pending rejection will still be retried after upgrade. Start in dry-run mode only if you want to suspend further pending cleanup while validating settings.

## Tests / maintenance

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile guard.py scripts/release_notes.py
python3 scripts/release_notes.py
# If Docker is available and .env has correct paths:
docker compose config
```

The tests use mocked qBittorrent/Arr HTTP clients and ffprobe; they don't prove live API compatibility, media mount correctness or physical deletion on your server. Test dry-run first.

Project files: `guard.py`, `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`, `INSTALL.md`, `CHANGELOG.md`, `VERSION`, `.gitignore`, `.dockerignore`, `scripts/`, `tests/`, `.github/workflows/`.

## Troubleshooting

- **State/log permission errors:** verify ownership and directory write permission
  for UID/GID 65534, the bind mount's writable flag, ACLs, SELinux, and NFS UID
  mapping. Do not use a world-writable state directory as the fix.
- **Arr HTTP 400 “Invalid Hostname”:** check that the URL, port, URL base, and
  hostname reach the actual Arr service from inside the guard container. Check
  reverse-proxy host bindings and `HTTP_PROXY`/`HTTPS_PROXY` settings. Compare an
  authenticated `/api/v3/system/status` request through the configured route
  with a direct/no-proxy request. Keep API keys out of shared logs. History
  failures retain downloads; they must not be bypassed.
- **Pending cleanup:** inspect journal kind and phase alongside remote API state.
  Changed ownership, missing media, incomplete imports, and unconfirmed
  blocklisting require review. Never delete the journal to force retries.
- **Changed environment:** recreate the container with
  `docker compose up -d --force-recreate`; a restart does not reload `.env`.

For release history and version-specific upgrade details, see [CHANGELOG.md](CHANGELOG.md).

## Login compatibility (v0.3.1)

qBittorrent 5.2+ returns HTTP `204 No Content` after a successful login instead of the legacy `200 Ok.` response. Torrent Guard accepts either success form **only after** checking `/api/v2/app/version` with the authenticated session. This fixes the repeating `qBittorrent refused login: ''` error on qBittorrent 5.2+ while still rejecting failed credentials and malformed responses. The built-in cookie jar accepts the new `QBT_SID_<port>` cookie name automatically. No password or qBittorrent settings changes are necessary.
