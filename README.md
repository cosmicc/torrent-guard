# Torrent Guard

[![Tests](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml/badge.svg)](https://github.com/cosmicc/torrent-guard/actions/workflows/test.yml)

**Version 0.1**. A lightweight Python (standard-library only) and Docker Compose service that detects suspicious movie/TV torrents downloaded by Sonarr or Radarr via qBittorrent.

It examines **torrent file metadata** rather than executing any file. A suspicious release can be stopped, blocklisted in the relevant Arr app, removed from qBittorrent **with all qBittorrent-tracked downloaded data**, and retried by Sonarr/Radarr according to those apps' settings.

## Detection and cleanup

- Poll qBittorrent every 5 seconds by default, waiting until magnet metadata is available.
- Inspect every filename in the torrent for `.exe`, `.scr`, `.msi`, `.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, `.lnk`, `.jar`, and other risky extensions. Case-insensitive; a valid `.mkv` alongside an `.exe` is **still rejected**.
- Optionally reject torrents with **no recognized video file** (enabled by default). This deliberately rejects archives that have not yet been unpacked. Disable with `REJECT_NO_VIDEO=false` if you deliberately download RAR-based releases.
- Only consider torrents assigned to the exact qBittorrent categories specified for Sonarr/Radarr, and only inspect newly added torrents by default (24h). Do not use these categories for manual/non-media torrents.
- In active mode: **stop torrent → find matching Arr queue item → blocklist without letting Arr delete the client entry → call qBittorrent `/api/v2/torrents/delete` with `deleteFiles=true` → verify torrent disappears**. State is journaled in SQLite and survives restarts.
- In dry-run mode (the default), log the detection and take **no destructive action**.

**Deletion scope:** The `deleteFiles=true` flag is the qBittorrent-supported mechanism to delete the torrent **and its associated tracked download files**. Torrent Guard does not need filesystem access. qBittorrent controls exactly which files it removes; orphaned/untracked files and files copied/imported elsewhere cannot be guaranteed to be deleted. The guard verifies that the torrent is gone, not that every pathname has vanished from disk. If the Arr queue entry is missing, the guard stops and retains the torrent, avoiding untracked/unblocklisted deletion.

## Docker Compose (media-manager server)

Requirements: Docker Engine + Compose plugin, qBittorrent WebUI/API, Sonarr and/or Radarr, and host-published ports reachable from the guard container. **No download directory or Docker socket mounts.**

```bash
git clone https://github.com/cosmicc/torrent-guard.git
cd torrent-guard
cp .env.example .env
chmod 600 .env
nano .env
# Edit qBittorrent URL/login; Arr URLs/keys and exact download categories.
docker compose up -d --build
docker compose logs -f torrent-guard
```

The bundled Compose file uses `host.docker.internal:host-gateway` to connect from a Linux container to **host-published** ports on the same Docker server. If the apps only expose container-private ports, join a shared external Docker network and use their DNS names instead. There are no inbound listening ports for Torrent Guard.

Keep `DRY_RUN=true` until you've reviewed detection logs. To enable deletion, edit `.env` to `DRY_RUN=false`, then:

```bash
docker compose up -d --force-recreate
docker compose logs -f torrent-guard
```

Stop/update:

```bash
docker compose down              # retains the named SQLite state volume
git pull --ff-only
docker compose up -d --build
```

**Do not run `docker compose down -v`** unless you intentionally want to erase the pending-cleanup journal. The volume is writable by the unprivileged container user and is the only persisted data.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD` | qBittorrent HTTP API URL and credentials | required |
| `SONARR_URL`, `SONARR_API_KEY`, `SONARR_CATEGORY` | Sonarr URL/key and **exact** qBittorrent category | group optional |
| `RADARR_URL`, `RADARR_API_KEY`, `RADARR_CATEGORY` | Radarr URL/key and **exact** qBittorrent category | group optional |
| `DRY_RUN` | Report-only if true; active deletion if false | `true` |
| `POLL_SECONDS` | Check interval (min 2) | `5` |
| `MAX_AGE_HOURS` | Only **newly inspect** torrents no older than this (pending cleanup continues regardless) | `24` |
| `REJECT_NO_VIDEO` | Reject metadata listing no video file | `true` |
| `STATE_DB` | SQLite journal path | `/state/torrent-guard.sqlite3` |
| `LOG_LEVEL` | `INFO`, `DEBUG`, etc. | `INFO` |

If you use only one Arr app, remove **all three** variables for the unused app. You must have at least one configured app and distinct categories.

Recognized video extensions: `.mkv`, `.mp4`, `.avi`, `.m4v`, `.mov`, `.wmv`, `.webm`, `.mpg`, `.mpeg`, `.ts`, `.m2ts`, `.vob`, `.divx`, `.flv`.

## Safety and limitations

- Extension matching is not actual content validation, antivirus, or an executable sandbox. A malicious file renamed `.mkv` or hidden in `.zip` may escape detection.
- This is **polling**, not a pre-download block. qBittorrent may transfer data before metadata is inspected; this cannot guarantee zero downloaded bytes.
- Video-free archives, bundled helper scripts, and legitimate executables will be rejected by design. The guard is for movie/TV categories only.
- Arr **blocklisting and search again is dependent on Arr tracking/failed-download settings**. A replacement is not guaranteed.
- In case of an Arr outage, unmatched queue entry, permission error, or ambiguous state, pending state remains for retry/manual intervention; review `docker compose logs`.
- To prevent secrets leaking to GitHub, `.env`, secrets, state databases, and logs are ignored by `.gitignore` and `.dockerignore`. **Never commit production secrets**.
- Keep the API ports private to trusted networks; enable TLS if access crosses untrusted links.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile guard.py
# Optionally validate Compose on a Docker host after creating .env:
docker compose config
```

The tests mock both API clients; they do **not** validate compatibility with every qBittorrent/Sonarr/Radarr version or confirm actual filesystem deletion. Start in dry-run mode first.

## Files

```text
.
├── .dockerignore
├── .env.example
├── .gitignore
├── .github/workflows/release.yml
├── .github/workflows/test.yml
├── Dockerfile
├── compose.yaml
├── guard.py
├── README.md
├── RELEASE_NOTES.md
├── VERSION
└── tests/test_guard.py
```
