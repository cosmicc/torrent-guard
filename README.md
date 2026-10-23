# Torrent Guard

[![Tests](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml/badge.svg)](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml)

**v1.3** · Python 3.12 · Docker Compose · qBittorrent + Sonarr/Radarr

Torrent Guard monitors movie and TV downloads through qBittorrent and the
Sonarr/Radarr APIs. It screens selected filenames for dangerous extensions,
checks completed videos with ffprobe, and cleans up tracked download files
after Arr confirms that every selected video was imported and the import
grace interval has elapsed (10 minutes by default).

- Bad releases are stopped and blocklisted through their owning Arr instance
  before qBittorrent is asked to delete their tracked data.
- Successful imports are never blocklisted. Pending deletion attempts recheck
  completion, media, and import evidence; uncertainty preserves the download.
- Runs without exposed ports, root privileges, Docker socket access, or a
  writable media mount. Persistent SQLite state and rotating logs support retries.
- Color-coded live logs and a Docker healthcheck show ongoing polling activity.
- Metadata downloads observed for more than one hour are failed through Arr
  blocklisting before removal; timers survive restarts and reset on state exit.
- Starts with `DRY_RUN=true`: no torrent stop, blocklist, deletion, or pending
  operation changes during polling.

## Getting started

Guard defaults to UID/GID `1000:1000` to match host download ownership. Set
`GUARD_UID`/`GUARD_GID` in `.env` for other non-root IDs. Upgrading from 1.1
requires migrating existing state ownership while stopped; see
[permissions and migration](INSTALL.md#container-user-and-permissions).

1. Follow [installation and configuration](INSTALL.md) to create `.env`, prepare
   the persistent state directory, and map both download directories read-only.
2. Start with `docker compose up -d --build` and inspect
   `docker compose logs -f torrent-guard` in dry-run mode.
3. After validating paths and Arr import evidence, set `DRY_RUN=false` and run
   `docker compose up -d --force-recreate`.

Keep qBittorrent's “Append .!qB extension to incomplete files” enabled. No
completion hook is required. Imported library files are not cleanup targets;
qBittorrent is the primary deletion mechanism. Guard verifies that the torrent
and all saved download file paths are absent. ffprobe checks video stream
metadata, not full-file integrity or malware safety.

See [setup, upgrades, and troubleshooting](INSTALL.md),
[version history](CHANGELOG.md), and [development guidance](AGENTS.md).
This daemon uses a single changelog; no separate user changelog is maintained.

**v0.6:** independent deletion verification retains unresolved
operations. Optional [completed-download leftover cleanup](docs/deletion-verification.md)
uses a narrowly scoped writable bind and saved file identities. Verified leftovers
are permanently deleted after the final identity check.

Automatic leftover removal requires starting with both Compose files; plain
`docker compose up` runs verification with read-only downloads. See the
[cleanup deployment commands](docs/deletion-verification.md#optional-completed-download-write-access).

**v0.7 filesystem protection:** stale NFS handles and other inconclusive access
failures preserve downloads and retry with backoff. See
[filesystem recovery](docs/filesystem-retries.md).

## Monitor

For readable, colored event blocks in a PuTTY/SSH terminal, run from the project
checkout on the Docker host (requires Python 3):

```bash
python3 scripts/watch_logs.py
```

The viewer follows `/config/torrent-guard/torrent-guard.log`, handles rotation,
and fits details to your terminal width. Press Ctrl+C to exit. See
[SSH monitoring options](INSTALL.md#ssh-event-viewer) for custom paths and use
without host Python.

```bash
docker compose logs -f --tail=100 torrent-guard
docker compose ps
```

Live logs use green for success, bright cyan for activity, yellow for waiting/warnings,
red for errors/rejections, and purple for dry runs. An INFO `STATUS` line shows
numeric pending-work counts every 60 seconds after completed polls. `HEARTBEAT`
is available at DEBUG. The rotating audit file stays plain text.
Set `LOG_COLOR=never` to disable colors.

Arr import checks run immediately after video validation, then every 60 seconds
by default. `CHECK import` shows active checks; `WAIT import` explains missing
evidence or a confirmed import still within its cleanup grace period.

Download logs identify names and paths. Cleanup failures retry automatically
with increasing delays up to 15 minutes, preserved across restarts. Ownership
Log timestamps and deadlines use Eastern Time with automatic EST/EDT changes
and explicit offsets. Filesystem warnings include Eastern `retry_at`; cleanup and filesystem holds must
both expire before retrying. NFS cleanup supports a guarded private-quarantine
rename when the filesystem rejects the no-replace flag. Ownership
warnings identify the live torrent whose missing metadata blocks removal;
see [cleanup troubleshooting](docs/deletion-verification.md#cleanup-retries-and-missing-metadata).
Zero-data queued or metadata downloads can wait without directories or file
metadata and do not block unrelated cleanup; known overlapping paths stay protected.

The Docker healthcheck reports unhealthy when no poll has finished for 300
seconds, with startup grace and retries. Increase `HEALTHCHECK_MAX_AGE_SECONDS`
for slow or large media batches. Health measures recent polling, including dry
runs and idle polls; it does not certify every Arr API or file deletion.
Docker does not automatically restart unhealthy containers.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile guard.py deletion.py scripts/release_notes.py scripts/watch_logs.py
python3 scripts/release_notes.py
```

Tests use mocked services and ffprobe. Review a dry run on the actual media
server before enabling deletion. Release preparation validates matching versions
in `VERSION`, `guard.py`, `compose.yaml`, and `CHANGELOG.md`. New versions use
two parts (`0.4`, `0.5`, etc.); historical release numbers are preserved. Publishing a version
change on `main` triggers the GitHub release workflow.
