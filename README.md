# Torrent Guard

[![Tests](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml/badge.svg)](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml)

**v0.8** · Python 3.12 · Docker Compose · qBittorrent + Sonarr/Radarr

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
- Starts with `DRY_RUN=true`: no torrent stop, blocklist, deletion, or pending
  operation changes during polling.

## Getting started

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

```bash
docker compose logs -f --tail=100 torrent-guard
docker compose ps
```

Live logs use green for success, blue for activity, yellow for waiting/warnings,
red for errors/rejections, and purple for dry runs. A `HEARTBEAT` line appears
once a minute while polls finish. The rotating audit file stays plain text.
Set `LOG_COLOR=never` to disable colors.

Arr import checks run immediately after video validation, then every 60 seconds
by default. `CHECK import` shows active checks; `WAIT import` explains missing
evidence or a confirmed import still within its cleanup grace period.

The Docker healthcheck reports unhealthy when no poll has finished for 300
seconds, with startup grace and retries. Increase `HEALTHCHECK_MAX_AGE_SECONDS`
for slow or large media batches. Health measures recent polling, including dry
runs and idle polls; it does not certify every Arr API or file deletion.
Docker does not automatically restart unhealthy containers.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile guard.py deletion.py scripts/release_notes.py
python3 scripts/release_notes.py
```

Tests use mocked services and ffprobe. Review a dry run on the actual media
server before enabling deletion. Release preparation validates matching versions
in `VERSION`, `guard.py`, `compose.yaml`, and `CHANGELOG.md`. New versions use
two parts (`0.4`, `0.5`, etc.); historical release numbers are preserved. Publishing a version
change on `main` triggers the GitHub release workflow.
