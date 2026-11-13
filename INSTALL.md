# Torrent Guard installation and operations

For daily sync and Sunday scrub on a separate OMV/NFS server with Sonarr/Radarr
stopped on the Docker server, see [OMV/SnapRAID maintenance](docs/maintenance.md).
Restricted SSH coordinates the hosts; one OMV job replaces the two direct
SnapRAID scheduled jobs. qBittorrent remains running.
Use the [OMV and Docker-host installers](docs/maintenance.md#1-prepare-omv)
for setup steps 1–3; reruns verify existing configuration without overwriting it.

[![Tests](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml/badge.svg)](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml)

**v2.2** · Python 3.12 · Docker Compose · qBittorrent + Sonarr/Radarr · `ffprobe` video checks

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

| Condition | Removal policy |
| --- | --- |
| Exact `save` tag, ignoring case | Exempt from every Guard stop/rejection/replacement/deletion, including completed/imported and bad releases |
| Incomplete download with positive total seeders | Wait for seeders; exempt from shorter timeouts, subject only to the 30-day zero-active exception below 80% |
| Below 15%, zero total swarm seeders | 3 observed hours |
| 15% to under 50%, zero total swarm seeders | 3 observed days |
| 50% to under 80%, zero total swarm seeders | 7 observed days |
| 80% or more, still incomplete | Exempt from all incomplete-download timeouts |
| Below 15%, stalled, zero speed and zero active/total seeders | 3 observed hours of stalling |
| Below 80%, zero connected active seeders, any total count | 30 observed days without active seeders; reset when one connects |
| Metadata, explicit zero payload and no file metadata | More than 10 observed minutes unless save or positive total seeders protects it |

**Save always wins.** It protects while the torrent remains tagged in
qBittorrent. Removing the tag permits fresh normal authorization. If the torrent
is manually removed, its files can qualify for normal orphan cleanup after the
separate absence wait; no permanent ignore list is created. A pending cleanup
cannot bypass save, even after a restart or when the tag appears between actions.

Total seeders means the reported swarm count (`num_complete`); active seeders
means connected seeders (`num_seeds`). Positive totals protect shorter incomplete
timeouts even when none are connected. The 30-day exception measures observed
zero **active** seeders, never torrent age/added date. An old torrent is not
immediately purged on upgrade. Connected seeders or missing/invalid active counts
reset this clock. Peer progress and total-seeder changes alone do not. Metadata
with positive totals also waits for connections; after the long observation,
zero-payload client removal still cannot claim filesystem absence.

Zero-total history survives peer progress and percentage-band changes. Recompute
the required wait from the current percentage before every stop, blocklist and
delete request. Use the higher of API progress and completed/total bytes so stale
lower progress cannot bypass 80% retention. Unknown counts are not zero.
Pauses/stops/queues/checking/moving cannot initiate a timeout; only Guard's own
journaled stop for the same incarnation can resume pending work. Timers survive
restarts/API outages and reset on observed recovery, owner/incarnation/size/path/
tag changes, disappearance or clock rollback. Dry-run timers stay in memory.

Positive total seeders and 80% retention apply to incomplete-download timeouts.
Completed/seeding import cleanup and dangerous/no-media rejection remain enabled
unless save is present. Rejected incomplete torrents request Arr replacement
before client deletion. The dashboard's **Removal rules → View all rules and
exceptions** lists every policy and exemption without initiating actions.

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

### Web status page

Version 1.7 replaces the `--status` CLI with one page served by the existing
Torrent Guard container. No additional Nginx/frontend container or JavaScript
build is needed. Open the dedicated hostname configured on your main Nginx
server. Nginx must supply TLS and authentication for **the whole site**, including
`/api/status` and static assets: download names and operational details are private.
The backend has no login of its own and must not be exposed to the public internet.

For your Nginx server on another machine, merge these settings into the existing
host `.env` (replace the example addresses with your actual private addresses):

```dotenv
STATUS_ENABLED=true
STATUS_PORT=8080
STATUS_BIND_ADDRESS=192.0.2.20
STATUS_HOST_PORT=8088
STATUS_ALLOWED_CLIENTS=192.0.2.10/32
```

Here `192.0.2.20` represents the Docker host and `192.0.2.10` the connecting Nginx
server. These are documentation addresses, not usable deployment defaults.
`STATUS_BIND_ADDRESS` and `STATUS_HOST_PORT` control Docker's host publication;
`STATUS_PORT` controls the internal listener. Bind to a specific private host
address and permit only Nginx in the host/network firewall. Account for Docker's
published-port rules (for example, the `DOCKER-USER` chain); a generic UFW rule
alone may not restrict Docker forwarding.

`STATUS_ALLOWED_CLIENTS` is a comma-separated list of up to sixteen exact IPs or
CIDRs, matched to the actual TCP peer, never `X-Forwarded-For`. Unrestricted `/0`
networks are rejected. The defaults bind only `127.0.0.1:8088` on the host and
allow loopback clients inside the container. Remote access requires the explicit
settings above. Docker NAT may change the observed source for local connections;
allow the actual trusted proxy address rather than widening access to a whole LAN.
Set `STATUS_ENABLED=false` to disable the listener and collector.

Inside your existing authenticated TLS Nginx server block, proxy the whole
hostname to the Docker host's private published port:

```nginx
location / {
    # Preserve your existing authentication/auth_request protection here.
    proxy_pass http://192.0.2.20:8088;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_read_timeout 15s;
    proxy_buffering off;
}
```

Use your real Docker host address instead of the example. Keep TLS certificate
and authentication configuration on your existing Nginx server. If the hop
between Nginx and Guard crosses an untrusted network, use a private VPN/tunnel;
the lightweight backend speaks HTTP. Test the Nginx configuration and reload it
through your normal management process. No access to that server is required
for Guard's build, and this repository does not change your main Nginx setup.

Pull v1.8, then rebuild/recreate using the same Compose files already in use:

```bash
git pull
docker compose up -d --build --force-recreate
# If completed-download cleanup is already enabled, use both files instead:
# docker compose -f compose.yaml -f compose.cleanup.yaml up -d --build --force-recreate
```

Keep `.env`, `/config/torrent-guard`, all journal evidence and the existing
`<STATE_DB>.status.sqlite3` history file. The additive upgrade creates only a
bounded recent-activity table in the monitoring database. Deletion tables are
not migrated or reset. The application healthcheck and SSH log viewer remain.

The page shows live/dry-run mode, polling heartbeat/uptime, API connections,
managed torrent states, directory access/free space, recent validation/import/
cleanup decisions, pending deletion names/phases/reasons, retry holds/deadlines,
and recent warnings/errors. A refresh button and ten-second browser updates read
a cached snapshot. Diagnostics run once per minute **after** the previous pass
finishes, independently of polling. Each drive probe has a ten-second timeout;
the complete collection has a 120-second limit. Collection failure preserves
old results with an explicit warning; observations older than three minutes are
marked stale. The heartbeat is read independently and never refreshed by HTTP
or background diagnostics. A responsive page is not proof of healthy polling.

State and the identity-matched completed cleanup alias (when enabled) receive
exclusive mode-0600 temporary-file create/write/fsync/read/remove tests. Downloads
and incomplete directories are checked read-only. Write tests run independently
of dry-run mode and only on the collection schedule, never per browser request.
An interrupted probe can leave its own `.torrent-guard-status-*` file; never
sweep download files to resolve it. Kernel-level NFS stalls may delay process
termination beyond userspace timeouts. Directory checks do not certify every
media file, the underlying mount source, or library access. API system status
checks do not prove Arr history endpoints are available.

Rolling **6, 12 and 24 hour** totals are overlapping emitted-event counts, not
unique downloads. Rejection candidates include dry-run observations and may
repeat after restarts; they also count as warnings. Errors are separate from
warnings. `CLEANED` includes zero-payload client removals and is not reclaimed
disk space. History starts with v1.6 (or first startup if newer); short windows
are marked partial, old logs are not backfilled, and downtime is not observed.
The separate history retains seven days of event counts, twenty issues per
severity and up to 100 recent decisions across log rotation/restarts. Recent decisions
begin with v1.7. Ten decisions per page, up to four warnings/four errors from
the last 24 hours, and fifty pending records are displayed, with up to
twenty-five holds per kind. Import counters show the timestamp of their last
completed-poll snapshot; persisted timers exclude dry-run-only observations.

All external text is escaped/redacted, shown as text rather than HTML, and served
with no-store and restrictive browser headers. The page cannot stop torrents or
delete files. Its only POST route queues archive-only bookkeeping requests;
status and static assets use fixed GET/HEAD routes. The proxy allowlist is an additional restriction,
not a replacement for Nginx authentication. New colors follow the approved
[navy/slate and cyan palette](docs/status-palette.svg). Display times use Eastern
Time with EST/EDT; hover timestamps for their full zone/offset.

### Record recovery

Stale bookkeeping is retired automatically when a successful qBittorrent
inventory confirms the torrent is gone and independent checks confirm that
**every saved name** is absent. This includes files under the completed `tv`
and `movies` subdirectories, incomplete/`.!qB` names, and saved quarantine payloads.
The configured completed directory remains their shared parent. No directory
sweeps or title-based file guesses are used. All saved manifests/observations
must agree that the files are absent on unchanged, accessible mapped roots.
Missing/replaced mounts, permission errors, malformed evidence and client API
outages do not prove absence. Routine automatic retirement checks run at most
once per minute; ordinary successful-deletion verification retains its existing
audit and retry flow. In dry run, verified retirement also clears stale records.

For legacy holds without trustworthy saved paths, open **Record recovery**,
expand the active records and select **Forget record**. Read the confirmation,
then choose **Archive record**. The request is durable and the polling thread
performs a fresh qBittorrent inventory check before archiving. A torrent still
present in qBittorrent is refused. Requests wait through client outages and
survive container restarts. Changes to a record's phase or saved evidence require
refreshing and reviewing it again. A timeout in the browser can occur after a
request was queued; refresh to see its result before retrying.

**Forget record does not inspect, remove or certify absence of files.** It stops
retries for the selected missing-client record and preserves its raw evidence
for audit, even when files may remain or the mount cannot be read. This is the
fallback for records the app cannot safely verify automatically. It is not a
replacement for Arr import verification or a way to mark files as successfully
cleaned. Existing files are retained. If the torrent is added again later, normal
screening/import processing resumes; archival does not create a permanent ignore.

These bookkeeping actions work with `DRY_RUN=true`, while torrent stops, Arr
blocklisting, cleanup scheduling and payload deletion remain disabled. The
additive `record_observations`, `record_archive` and `record_requests` tables live
in the existing state SQLite database. Original active rows are archived in one
transaction before removal; historical `deletion_audit` rows are preserved.
Captured filesystem path observations survive restarts, including dry run, and
are rechecked rather than trusted as cleanup authorization. Legacy records may
lack observations and need a one-time explicit archive. Keep the state directory
and back it up through the normal stopped-container procedure; never erase it
to clear the display. No new mounts or environment variables are required.

The page shows current archive requests/results, up to 100 active recovery
records and the ten latest archives. Active bookkeeping is read directly so
completed actions clear pending/hold counters without waiting for the slower
diagnostic cache. Import counts still show their last-poll timestamp. Historical
warnings remain in **Warnings & errors** after their active record is retired;
they are not a claim that the work is still pending. Archives use distinct
`verified_absent` and `operator_dismissed` outcomes and never increment `CLEANED`
statistics. The queue is bounded to 32 outstanding requests and up to eight are
handled per successful poll. Nginx must preserve the browser Host/Origin and
custom request headers, keep authentication on `/api/forget`, and allow POST
to that route. Origin/token checks are additional protection, not authentication.

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
python3 -m py_compile guard.py deletion.py records.py status_report.py status_web.py scripts/release_notes.py scripts/watch_logs.py
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

## Automatic orphan cleanup (v1.9)

For unattended removal in `complete/tv`, `complete/movies`, `incomplete/tv` and
`incomplete/movies`, follow the [orphan cleanup deployment guide](docs/orphan-cleanup.md#deployment).
Use `compose.orphans.yaml` on each recreation. This is separate from the older
completed-root fallback; it also removes untracked files without import evidence
once every qBittorrent torrent has left them unowned for 15 observed minutes.
No Forget record action is needed. Keep the existing state directory and credentials.

Forget record results refresh every two seconds while queued. A queued result
means the polling thread has not yet confirmed client absence; inspect polling
health if it persists. A refused result explains whether the torrent is still
present or the record changed. Errors remain visible after the dialog closes.
Historical warning entries are retained even after the active record is archived.
When proxying on a nonstandard public port, preserve the complete Host header
with `proxy_set_header Host $http_host;` so same-origin checks match the browser's
Origin. Do not bypass origin/token checks or trust forwarded headers instead.

### v1.9 completion and timeout clarification

Seeding (`uploading`, `stalledUP`, `forcedUP`) uses the same completed cleanup
path, with no added seed-time or ratio requirement. Once every selected video
has confirmed Sonarr/Radarr import and the ordinary import grace has elapsed,
Guard requests torrent deletion with its data. No-media releases still use
rejection/blocklisting with `skipRedownload=false` to permit replacement, then
explicit qBittorrent deletion with data; the default `REJECT_NO_VIDEO=true`
keeps this screening enabled.

The save exemption applies to every Guard removal action. Positive total seeders
protect shorter incomplete timeouts, while thirty observed zero-active-seeder
days below 80% may trigger replacement/deletion. Policy 4 starts fresh condition
observations for legacy age/seeder evidence, preserving journals, audits and
valid Guard-stop provenance. Preserve the state directory; no environment or
manual database edits are needed. Recreate with the same Compose overrides.

## Archive origin and cleanup permissions

An **origin mismatch** rejects Forget record before it enters the queue. It is
separate from download filesystem permissions. Your external Nginx must preserve
the browser's Host, including any port:

```nginx
proxy_set_header Host $http_host;
```

For TLS/Host-rewriting proxies, v2.0 and later can instead
use this exact external browser origin in the existing host `.env`:

```dotenv
STATUS_PUBLIC_ORIGIN=https://guard.example.invalid
```

Replace the example with the scheme, hostname and optional port from your address
bar. Do not include credentials, a path, query or fragment. Recreate Guard after
changing environment settings. The source-IP allowlist and action token still
apply; forwarded headers do not supply trusted origins. A token error instead
requires refreshing the page. Before this fix, v1.9 reports the combined message
`Origin or action token invalid`; forwarding Host correctly also works there.

The older `compose.cleanup.yaml` grants only the completed-directory fallback.
To enable the four-folder orphan cleanup, preserve your existing overrides and
add the orphan override (the four host folders must already exist):

```bash
docker compose -f compose.yaml -f compose.cleanup.yaml -f compose.orphans.yaml up -d --build --force-recreate
```

The parent download mapping stays read-only. v2.0 and later perform
full read/write/delete tests through the four scoped writable aliases, matching
each to its read-only mapped folder. Only an exclusively created random probe
file is removed; media is never a permission-test target. A failed/missing mount
is reported explicitly. Keep the existing non-root UID/GID and `/state`; do not
use root, world-writable permissions, broad writable parent/library binds, or
journal deletion as a workaround. A successful probe tests its own file and does
not guarantee that every existing media file has identical ownership/ACLs.

Record recovery shows the last automatic retirement check: client still present,
files still present, inaccessible/changed mapping, or missing legacy evidence.
Confirmed-absent records retire automatically; unknown evidence can be explicitly
archived after fresh client absence. Historical warnings remain historical.
v2.0 also prevents an old same-poll snapshot from recreating an
archived hold and finishes already-requested deletion verification without waiting
out stale retry deadlines when client and every saved path are confirmed absent.

## Recent decisions and warning history (v2.0)

Recent downloads retains up to 100 emitted decisions, with ten rows per page.
Use First, Backward, Forward and Last to navigate; auto-refresh keeps the current
page when it remains valid. Repeated decisions for one torrent can occupy several
entries. Older versions retained only 50 decisions, so upgrading does not restore
already-pruned entries; the longer list fills as new decisions arrive.

Warnings and errors displays only events from the last 24 hours, including the
exact cutoff and excluding future-dated records. The browser also ages cached
entries out. Existing 6/12/24-hour statistics and audit logs are unchanged; empty
or unavailable history remains distinct from a successful service check.
