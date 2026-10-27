# Torrent Guard installation and operations

[![Tests](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml/badge.svg)](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml)

**v1.3** · Python 3.12 · Docker Compose · qBittorrent + Sonarr/Radarr · `ffprobe` video checks

Torrent Guard is a background service that examines new and completed movie/TV torrents and cleans up imported releases. It uses **qBittorrent's Web API** and **Sonarr/Radarr's v3 APIs**; no qBittorrent “Run external program on torrent completion” hook is required.

## What it does

1. **On metadata arrival:** Inspect every selected file in fresh torrents. Reject dangerous extensions (`.exe`, `.scr`, `.bat`, `.ps1`, `.js`, `.lnk`, etc.), even when another file looks like a video; reject releases that lack a video file (configurable). A rejected torrent is stopped, its release is blocklisted using the matching Arr queue item, then qBittorrent is instructed to **delete the torrent and all qBittorrent-tracked payloads** with `deleteFiles=true`.
2. **When complete:** Reinspect eligible movie/TV torrents **regardless of their initial age**. Validate downloaded videos using `ffprobe` against the actual media files via a read-only Docker bind mount. Invalid videos are handled as bad releases (stop, blocklist, remove and delete torrent data). Valid releases are left alone until Arr confirms import.
3. **After successful Arr import + 15 minutes:** Require a matching Sonarr/Radarr `downloadFolderImported` history event for the **same infohash**, dated after this torrent was added and at least 15 minutes ago by default. For multi-video downloads, require a matching import record for every selected video file. Then instruct qBittorrent to delete the torrent and its tracked downloaded files (but **never** blocklist a successful release).
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

If qBittorrent sees `/data/torrents` while the host sees `/srv/media/downloads`, use `QBIT_DOWNLOAD_ROOT=/data/torrents` and `DOWNLOADS_HOST_PATH=/srv/media/downloads`. The file paths are mapped relatively; e.g. qBittorrent save path `/data/torrents/complete/movies` with file `Film.mkv` maps to Guard `/downloads/complete/movies/Film.mkv`. Set `QBIT_COMPLETE_DIR=/data/torrents/complete` and `QBIT_INCOMPLETE_DIR=/data/torrents/incomplete` when the qBittorrent root differs. The container defaults to UID/GID `1000:1000`, configurable with `GUARD_UID` and `GUARD_GID`. This user must be able to traverse directories and **read** the video files. Local write permission is needed only for the explicit completed-download cleanup override.

By default Compose **requires** `DOWNLOADS_HOST_PATH`: if omitted, it refuses deployment rather than running incomplete verification. Read-only bind mounting the location does not grant Torrent Guard the ability to delete local media; deletion is carried out by the qBittorrent API.

Prepare persistent state on the Docker host. The directory itself must be
writable for SQLite and log rotation; changing log-file permissions alone is
insufficient:

```bash
sudo install -d -o 1000 -g 1000 -m 750 /config/torrent-guard
```

Use your configured numeric IDs in this command if different. Existing
installations must follow the [ownership migration](#container-user-and-permissions)
before starting with the new runtime user.

Start in report-only mode:

```bash
docker compose up -d --build
docker compose logs -f torrent-guard
```

In dry-run mode (`DRY_RUN=true`) no torrent is stopped, blocklisted or deleted. Review the `ACCEPT metadata`, `ACCEPT completed`, `REJECT candidate`, `DRY RUN`, `DEFER validation`, `WAIT import` (INFO when the reason changes) and ERROR messages. Verify the actual media paths are readable and that Arr import-history matches work.

When ready to allow cleanup, change `.env` to `DRY_RUN=false` and recreate:

```bash
docker compose up -d --force-recreate
docker compose logs -f torrent-guard
```

View persistent logs:

```bash
docker compose exec torrent-guard tail -n 100 /state/torrent-guard.log
```

### Upgrade to v0.8

```bash
cd torrent-guard
git pull --ff-only
# Compare .env.example to your existing .env; DO NOT overwrite real credentials.
# Add DOWNLOADS_HOST_PATH, QBIT_DOWNLOAD_ROOT, QBIT_COMPLETE_DIR,
# and QBIT_INCOMPLETE_DIR before restarting. Keep DRY_RUN=true while verifying.
docker compose up -d --build --force-recreate
```

If automatic leftover cleanup is enabled, retain its override when upgrading:

```bash
docker compose -f compose.yaml -f compose.cleanup.yaml up -d --build --force-recreate
```

v1.3 adds guarded NFS quarantine rename compatibility for unsupported
`RENAME_NOREPLACE` errors (such as EINVAL). It checks that the destination is
empty, owner-only and owned by Guard before a standard atomic rename, then
checks the captured file identity before unlink. Other processes running as
Guard's UID or root must not modify its private quarantine during cleanup.
No settings or schema migration are needed; retain the existing state journal
and cleanup override when rebuilding/recreating. See the
[cleanup guide](docs/deletion-verification.md#nfs-rename-compatibility-v13).

v1.1 requires no schema migration or new settings. It revalidates bad-release
evidence on pending retries and immediately before deletion; stale rejection
records with missing or inconclusive evidence remain pending for review. Import
evidence is also refreshed for the exact file list saved before API deletion.
Rebuild and recreate the container, preserving `/state` and any cleanup override.

v1.5 adds `stalled_wait` automatically and preserves all existing journal,
file evidence, audit and retry tables. Retain `/state` and back it up before
upgrading. No new environment variables or writable mounts are needed.
Existing metadata observation timers are retained: an eligible tagged magnet
already observed for more than 10 minutes can fail on the first v1.5 poll.
Stalled-download timers start when v1.5 first observes the qualifying condition;
torrent age alone never causes removal. Rebuild and recreate using your existing
Compose files, including the cleanup override if already enabled.

v1.0 adds `metadata_wait` automatically and preserves existing pending work,
file evidence, audits and retry records. Back up `/state` before upgrading;
do not replace the database. No new environment variables are required. Existing
metadata downloads receive a full hour starting with their first observation by
v1.0; their qBittorrent added time is not used as the timeout start.

v0.8 introduces no new settings or database schema changes relative to v0.7.
No new environment settings are required for read-only independent verification.
v0.7 adds the filesystem_retry table automatically; the evidence/audit tables
introduced in v0.6 and existing pending records are preserved. Back up `/state`
before upgrading. Missing-torrent legacy records
without saved file evidence require manual review. Automatic completed-download
leftover removal requires the explicit cleanup override and host-directory setting
described in [deletion verification](docs/deletion-verification.md).
Import checks remain at 60 seconds by default.
Optional `LOG_COLOR=always` enables the bright console palette by default;
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
1000:1000 and verify the journal is present before starting again. Changing
the mount does **not** copy existing data automatically. Do **not** run
`docker compose down -v` or erase `/state`: it contains the retry journal and audit log.

## Container user and permissions

v1.2 changes the image and Compose default from UID/GID `65534:65534` to
`1000:1000`. Compose reads `GUARD_UID` and `GUARD_GID` from the project `.env`;
these are Docker runtime settings, not Python settings. Set both to the host
numeric owner/group (`stat -c '%u:%g %a %n' /mnt/downloads/complete`). Use non-root
IDs. No automatic chown, privilege escalation or download permission changes occur.

Before upgrading an existing installation, stop Guard, back up its state, and
change ownership of the state directory and its existing contents. For `1000:1000`:

```bash
docker compose stop torrent-guard
sudo cp -a /config/torrent-guard "/config/torrent-guard.backup-$(date +%Y%m%d-%H%M%S)"
sudo chown -R 1000:1000 /config/torrent-guard
sudo chmod 750 /config/torrent-guard
```

Merge these into the existing `.env`, keeping credentials and other settings:

```dotenv
GUARD_UID=1000
GUARD_GID=1000
```

Rebuild and recreate, retaining the cleanup override if already enabled:

```bash
docker compose -f compose.yaml -f compose.cleanup.yaml up -d --build --force-recreate
docker compose exec torrent-guard id
```

For installations using read-only verification only, omit `-f compose.yaml -f
compose.cleanup.yaml`. For custom IDs, substitute them in the ownership commands.
To temporarily retain the pre-v1.2 identity, set both IDs to `65534` and keep the
state ownership consistent. Never delete the journal to resolve permissions.

Local deletion requires write and execute access to the **parent directories**,
not just ownership of a file. It also requires the explicit writable completed-root
bind in `compose.cleanup.yaml`; `/downloads` remains read-only. Check directory
ownership/modes, ACLs, NFS identity mapping and SELinux if access is still denied.
Do not recursively change download/library ownership or use world-writable modes.

Deletes performed through the qBittorrent API run under qBittorrent's own user.
Changing Guard's UID cannot fix qBittorrent's permission errors; its service user
and writable download mount must also permit deleting the tracked downloads.
Guard retains failed operations and retries from its existing journal.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GUARD_UID`, `GUARD_GID` | `1000`, `1000` | Compose-only numeric non-root runtime IDs; match download ownership and migrate state permissions before changing |
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
| `IMPORT_GRACE_MINUTES` | `15` | Minimum age **of confirmed Arr import**, never just torrent completion; minimum allowed 10 |
| `IMPORT_CHECK_SECONDS` | `60` | Arr history poll interval while completed valid downloads are present |
| `HISTORY_LOOKBACK_DAYS` | `30` | Arr history lookup horizon. Older downloads are retained for review |
| `STATE_DB` | `/state/torrent-guard.sqlite3` | Restart-safe pending deletions |
| `LOG_FILE` | `/state/torrent-guard.log` | Timestamped rotating persistent log (empty disables file logging) |
| `LOG_MAX_BYTES` | `10485760` | File rotation size (10 MiB) |
| `LOG_BACKUP_COUNT` | `5` | Number of retained rotated files |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_COLOR` | `always` | Console ANSI colors: `always`, `auto` (terminal only), or `never`; audit files stay plain text |
| `HEALTHCHECK_MAX_AGE_SECONDS` | `300` | Positive finite seconds since the last completed poll; allow for the longest full media-validation batch and `POLL_SECONDS` |
| `CLEANUP_LEFTOVERS` | `false` | Allow guarded local removal of completed leftovers; compose.cleanup.yaml sets this true with the required write bind |
| `DELETE_SETTLE_SECONDS` | `120` | Wait after the last qBittorrent delete request before local fallback; minimum 30 seconds |
| `CLEANUP_COMPLETE_HOST_PATH` | unset | Exact host completed-download directory for the optional cleanup override; must correspond to QBIT_COMPLETE_DIR, never a library or download parent |

Use exactly one category per Arr app; tags are optional and must not conflict. To use only one Arr app, remove all of the other app's variables, including its tag.

Import checks start immediately after completed-video validation, then repeat
every `IMPORT_CHECK_SECONDS` while eligible downloads remain. This interval is
separate from `IMPORT_GRACE_MINUTES`, which delays deletion after the actual
import time. `WAIT import` reports whether import evidence is missing or partial,
filenames do not match, or import is confirmed but cleanup grace is still running.
The grace message gives the earliest cleanup time in Eastern Time, with EST/EDT
and its numeric UTC offset. `CHECK import` reports
the number of completed candidates and hashes with import history on each
successful scheduled history check. Matching history still needs filename,
timestamp, and grace validation before cleanup. `ACCEPT completed` only reports
video validation; it does not mean import is still pending.

## Stalled download timeouts

These three rules require an explicit **`movies` or `tv` qBittorrent tag**
(case-insensitive, comma-separated tags supported), plus unambiguous ownership
under the existing configured categories/tags. A matching category alone is not
enough. This tag requirement applies to timeout rules; filename screening and
successful-import cleanup keep their existing scope.

| Condition continuously observed by Guard | Timeout |
| --- | --- |
| `metaDL` or `forcedMetaDL`, with explicit zero payload and no file metadata | More than 10 minutes |
| `stalledDL`, with zero lifetime downloaded bytes, zero completed bytes and zero progress | One hour |
| `stalledDL`, strictly below 25% complete, no progress, and zero seeders reported in the swarm | Seven days |

The weekly rule uses qBittorrent's `num_complete` swarm count, not just connected
seeders. Unknown/missing/negative swarm counts preserve the download. A connected
seeder also prevents this rule from firing. Total size, progress, byte counters
and zero download speed must be explicit and valid. Swarm information is what
qBittorrent reports; Guard cannot independently prove that no seed exists.

Intentionally stopped, paused, queued, checking, moving and actively downloading
torrents do not start timeout timers. Timers begin with Guard's first observation
of the qualifying condition, survive daemon restarts/API outages, and reset on
observed recovery, disappearance, tag removal, ownership/incarnation change or
clock rollback. Switching between metadata states keeps the same timer. Stalled
timers also reset when downloaded/completed bytes, progress, size, paths or tags
change. Returning swarm seeders reset the weekly timer. No historical stalled
time is inferred from the torrent's age.

Failure uses the owning Sonarr/Radarr queue and blocklist, with replacement
search permitted. Guard stops the torrent, confirms Arr blocklisting, then
requests qBittorrent deletion. Missing queue matches, Arr errors (including 404),
prior filesystem holds, recovered metadata or a recovered stall preserve pending work. The Arr
blocklist controls future release selection; it is not a global ban across every
indexer or manually added download.

Empty-metadata removal requires explicit zero progress, zero lifetime downloaded
and completed bytes, and no completed timestamp. Missing counters or any payload
remain inconclusive and require review. Guard saves a distinct zero-payload
audit record without guessed paths; it never sweeps directories or claims to
verify filesystem absence from this record. If files appear before removal,
Guard retains the torrent for review. Dry-run timers are memory-only and no
stop, blocklist, deletion or polling journal mutation occurs. Timer/rejection
messages are visible in the SSH viewer and normal logs. Stalled removals require
a complete tracked-file list and readable mapped roots. Guard rechecks the live
condition before stopping, blocklisting and deleting. A persisted Guard-owned
stop can resume after restart; an operator pause before authorization cannot
start a timeout. Payload-bearing stalled removals save normal file manifests
and require independent file-absence checks. If qBittorrent leaves files behind,
they remain pending for manual recovery: a past stall does not authorize local
unlink. Guard never gains writable incomplete-directory or library access.

Replacement searches use the existing Arr queue request with
`blocklist=true`, `skipRedownload=false` and `removeFromClient=false`. Arr chooses
any available eligible replacement according to its settings and indexers; a
replacement is not guaranteed. Imported downloads
retain their existing history and grace requirements.

## Live monitoring and Docker health

### SSH event viewer

From the checkout on the Docker host:

```bash
python3 scripts/watch_logs.py
# Custom log location, recent history, or fixed width:
python3 scripts/watch_logs.py --file /config/torrent-guard/torrent-guard.log --tail 20 --width 100
```

Use an account with read access to the audit log; retain its restricted
permissions. No additional Python packages are required. The viewer shows event
blocks with indented download names/details, wraps long paths, preserves all
message text, follows rename rotation, and detects truncation when the file
shrinks below its read position. It waits if the log is temporarily absent.
It reads only the log, never the journal or APIs. Tracebacks and unfamiliar
lines remain visible. `--tail` counts physical log lines, including traceback
lines; use `--tail 0` to watch only new lines. Ctrl+C exits.

With the updated image, host Python is optional:

```bash
docker compose exec torrent-guard python /app/watch_logs.py --file /state/torrent-guard.log
```

Compose exec allocates a terminal by default. Rebuild/recreate once to include
the viewer in an existing image; keep any cleanup override you already use.
The host script works with existing audit logs without a daemon upgrade.
Colors default to automatic terminal detection; use `--color always` to force
or `--color never` to disable. `--once` prints recent events and exits.
Width adapts for each batch after resizing the terminal. Extremely narrow
terminals below 20 columns are unsupported. Bright green success, cyan activity,
yellow waiting/warnings, red errors/rejections, and magenta dry runs are intended
for dark shells; exact shades still depend on PuTTY's ANSI palette settings.
The viewer colors event headings and leaves details in the terminal's normal
foreground. Live daemon output uses the same bright palette; audit files stay
plain text. `LOG_COLOR` controls only daemon console output, independently of
the viewer's `--color` setting. No new environment variables or state migrations
are required. The viewer has no search/pause interface; use terminal scrollback.

### Docker health and raw logs

```bash
docker compose logs -f --tail=100 torrent-guard
docker compose ps
docker inspect --format '{{json .State.Health}}' torrent-guard
# Manual read-only check; exit code 0 means fresh, 1 means missing/stale/invalid.
docker compose exec torrent-guard python /app/guard.py --healthcheck
```

New application log headers and all displayed deadlines use `America/New_York`:
EST (`-0500`) in winter and EDT (`-0400`) during daylight-saving time, for example
`2026-10-22 23:03:05 EDT-0400`. The daemon formats these explicitly, independently
of the host timezone; the image also includes `tzdata` and sets `TZ` accordingly.
The SSH viewer preserves the timezone label and offset. Historical lines keep
their original timestamps. Internal API/epoch times and elapsed retry/grace
intervals are unchanged; no journal migration is needed. Rebuild and recreate
to install timezone data and use the updated viewer.

The bright palette is green for `ACCEPT`/`CLEANED`, bright cyan for routine activity
and `HEARTBEAT`, yellow for waiting/warnings, red for errors/rejections, and
purple for dry runs. Colors use ANSI codes, so exact shades depend on the
terminal theme. See [light and dark palette samples](docs/log-palette.svg).
Use an ANSI-capable terminal for colors; Docker log viewers may render them
differently. Set `LOG_COLOR=never` if a viewer displays raw escape characters.
Persistent `/state/torrent-guard.log` and rotated backups remain plain text;
no second colorized file is created. External control characters are escaped
before console or file output.

An INFO `STATUS` line appears after the first completed poll and at most once
every 60 seconds afterward. It contains only numeric counters:
`pending_deletion`, `verifying_deletion`, `waiting_for_import`, `import_grace`,
`metadata_waits`, `filesystem_holds`, and `cleanup_retries`. Pending deletion
includes every journal phase; verification and retry counts can overlap it.
Waiting for import excludes confirmed imports within grace and scheduled cleanup;
import counts reflect the latest observed evidence, including during API outages.
Metadata waits exclude already-pending timeout removals. Dry-run reports observed
waits and existing journal work without scheduling hypothetical deletions.

`HEARTBEAT polling active` uses DEBUG (`LOG_LEVEL=DEBUG`) at the same interval. The poll loop atomically writes
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
- **Successfully imported downloads:** Only matching Arr **history** authorizes cleanup. A finished torrent alone, disappearance from Arr's queue, a newly added torrent carrying an old reused hash, or a partial Sonarr season-pack import does **not** authorize deletion. The import event must be at least 15 minutes old by default. Every deletion attempt, including pending retries, rechecks completion, selected media, and history. The imported path never queries or changes the Arr queue and never blocklists a successful import.
- **Deletion:** qBittorrent receives `deleteFiles=true`. Guard saves all tracked filenames before the request and verifies their absence on disk before clearing the journal. Remaining files, unknown state, changed roots and permission failures retain pending work. Optional completed-download cleanup permanently removes verified leftovers using saved identities and narrowly scoped write access. Read [deletion verification and deployment](docs/deletion-verification.md), including the `/mnt/downloads/complete` mapping and existing-orphan limitations. Libraries and untracked files are never cleanup targets.
- **Media validity:** `ffprobe` checks for readable video stream metadata; it does **not** decode or checksum every frame, certify all audio/subtitle tracks, or guarantee that a file is malware-free. Failure to access a mounted media file **defers** processing rather than falsely classifying the download as malware. A readable but unexpected media codec may still be rejected if ffprobe cannot identify it.
- **Seeding:** Deleting imported torrents after 15 minutes can interrupt seeding or violate tracker requirements. This is the behavior requested; consider increasing the grace period or maintaining seeding separately for private trackers.
- **Arr history:** On API outages, missing download IDs, mismatched source paths, too-old history, or ambiguous multi-video matches, the program preserves the torrent. It cannot guarantee that *Arr always associates every imported episode with its torrent hash. Check DEBUG logs if valid releases do not clean up.
- **Pending state:** A past v0.1 pending rejection will still be retried after upgrade. Start in dry-run mode only if you want to suspend further pending cleanup while validating settings.

## Tests / maintenance

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile guard.py deletion.py scripts/release_notes.py scripts/watch_logs.py
python3 scripts/release_notes.py
# If Docker is available and .env has correct paths:
docker compose config
```

The tests use mocked qBittorrent/Arr HTTP clients and ffprobe; they don't prove live API compatibility, media mount correctness or physical deletion on your server. Test dry-run first.

Project files: `guard.py`, `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`, `INSTALL.md`, `CHANGELOG.md`, `VERSION`, `.gitignore`, `.dockerignore`, `scripts/`, `tests/`, `.github/workflows/`.

## Troubleshooting

- **Files remain and only `PENDING disk verification` repeats:** plain
  `docker compose up` does not enable local leftover removal. Set
  `CLEANUP_COMPLETE_HOST_PATH` to the exact completed-download host directory and
  recreate with both `compose.yaml` and `compose.cleanup.yaml`. Existing pending
  records retry automatically; retain the journal. v0.8 diagnostics name
  disabled cleanup or show the settle deadline explicitly.

In v0.7, filesystem failures, including NFS `ESTALE`, create a
persistent per-torrent hold. `DEFER validation` includes the retry delay;
30/60/120/240/300-second backoff replaces five-second retry/error loops. A state
change or daemon restart does not permit deletion while access is unresolved.
See [filesystem recovery](docs/filesystem-retries.md). No new settings are needed.

- **State/log permission errors:** verify ownership and directory write permission
  for the configured UID/GID (default 1000:1000), the bind mount's writable flag, ACLs, SELinux, and NFS UID
  mapping. Do not use a world-writable state directory as the fix.
- **Arr HTTP 400 “Invalid Hostname”:** check that the URL, port, URL base, and
  hostname reach the actual Arr service from inside the guard container. Check
  reverse-proxy host bindings and `HTTP_PROXY`/`HTTPS_PROXY` settings. Compare an
  authenticated `/api/v3/system/status` request through the configured route
  with a direct/no-proxy request. Keep API keys out of shared logs. History
  failures retain downloads; they must not be bypassed.
- **Pending cleanup:** inspect journal kind and phase alongside remote API state.
  Cleanup API/safety failures retry automatically with 60/120/240/480/900-second
  backoff preserved across restarts. Logs show download names, paths and the next
  retry time. An ownership-metadata warning also names the blocking live torrent;
  resolve its missing metadata or client error. See
  [cleanup retry troubleshooting](docs/deletion-verification.md#cleanup-retries-and-missing-metadata).
  Empty metadata is expected for `queuedDL`, `metaDL` or `forcedMetaDL` with explicit
  zero progress/downloaded/completed counters and no past completion. It can
  wait without directories and does not block unrelated cleanup. Known path
  overlaps, prior filesystem errors and resumed downloads remain protected.
  Changed ownership, missing media, incomplete imports, and unconfirmed
  blocklisting require review. Never delete the journal to force retries.
- **Changed environment:** recreate the container with
  `docker compose up -d --force-recreate`; a restart does not reload `.env`.

For release history and version-specific upgrade details, see [CHANGELOG.md](CHANGELOG.md).

## Login compatibility (v0.3.1)

qBittorrent 5.2+ returns HTTP `204 No Content` after a successful login instead of the legacy `200 Ok.` response. Torrent Guard accepts either success form **only after** checking `/api/v2/app/version` with the authenticated session. This fixes the repeating `qBittorrent refused login: ''` error on qBittorrent 5.2+ while still rejecting failed credentials and malformed responses. The built-in cookie jar accepts the new `QBT_SID_<port>` cookie name automatically. No password or qBittorrent settings changes are necessary.

### Set the post-import wait to 15 minutes

Existing `.env` values override the default. Set `IMPORT_GRACE_MINUTES=15`
in the existing host `.env` and recreate the container to apply it. Keep the
existing Compose cleanup override if enabled:

```bash
docker compose -f compose.yaml -f compose.cleanup.yaml up -d --build --force-recreate
```

For installations without the cleanup override, use
`docker compose up -d --build --force-recreate`. Confirm `import_grace=15m` in
the startup log. Keep the state directory and journal intact. The wait starts
from the Arr import timestamp for the last selected video, including pending
retries and local leftover cleanup. Bad-release rejection still requires Arr
blocklisting rather than successful import. No database migration is needed.

### Leftovers in the incomplete directory

`manual recovery required` means the torrent is gone but saved paths remain
outside Guard's writable completed root. Guard cannot remove those files through
qBittorrent once the client has forgotten the torrent, and its incomplete mount
remains read-only. Eligible completed files in the same operation can still be
cleaned. The operation stays pending until every recorded path is absent.

- Use the exact paths in `PENDING disk verification`, mapped to the host download
  root. Do not select files by torrent title or use a recursive directory sweep.
- Check qBittorrent's own download-mount permissions and logs: its process is
  responsible for the original `deleteFiles=true` request, independently of Guard.
- Before manual removal, verify that no live torrent owns those paths. For video
  downloads, verify every selected video was imported and the 15-minute grace
  elapsed; for rejected downloads, verify the saved bad payload and Arr rejection.
  If a file was replaced or its ownership is uncertain, preserve it for review.
- Remove only the confirmed leftover download files through the server's normal
  file-management tools. Guard rechecks absence on subsequent eligible polls.
  Keep the journal, filesystem retry rows and any quarantine evidence intact.

### Older NFS errors and unavailable torrent metadata

The attached-error pattern `Invalid argument` during quarantine rename may be
an unsupported NFS rename flag. Version 1.3 includes a guarded compatibility
fallback. Check the `Starting v...` log after rebuilding and recreating the
container; older unzoned timestamps and missing filesystem `retry_at` suggest
older code, but do not identify the deployed version conclusively. Existing
failed operations and saved quarantine directories resume after their persisted
retry deadlines. A remaining EINVAL on current code requires investigating the
actual filesystem operation; do not treat all EINVAL errors as safe to bypass.

An ownership warning naming another torrent means Guard cannot prove files are
unclaimed. Explicitly zero-data queued or metadata torrents can already wait
without blocking unrelated cleanup; missing counters, resumed payload, an API
failure or a filesystem hold keeps that protection. Resolve the named client's
metadata/download issue and let Guard retry. A torrent still under the incomplete
directory must finish its final move before completed-media validation resumes.
