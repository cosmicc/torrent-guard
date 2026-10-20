# Torrent Guard v0.1

Initial release of **Torrent Guard**, a Dockerized safety filter for Sonarr/Radarr torrents downloaded through qBittorrent.

- Inspect torrent contents by file extension after metadata arrives, including `.exe` and script payloads.
- Optional rejection when no recognized video file is present.
- Restrict processing to configured Sonarr/Radarr qBittorrent categories and recently added torrents.
- Stop suspicious torrents, request Arr blocklisting, explicitly delete the qBittorrent torrent with `deleteFiles=true`, and verify its disappearance.
- Persist pending cleanup in SQLite so the process can retry after a restart.
- Start in `DRY_RUN=true` mode; no downloaded-media mounts or Docker socket needed.
- Docker Compose deployment, documentation, and automated unit tests.

**Caution:** qBittorrent's `deleteFiles=true` instructs qBittorrent to remove the torrent's tracked download data, not arbitrary files outside the torrent. Deletion from the torrent list does not independently prove filesystem cleanup. Test in dry-run mode before enabling destructive actions.
