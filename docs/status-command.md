# Status command maintenance

`AGENTS.md` is the instruction entry point. This focused workflow covers
`status_report.py`, `tests/test_status.py`, Docker packaging and the CLI dispatch
in `guard.py`. Operator instructions are in [INSTALL.md](../INSTALL.md#command-line-status).

- Dispatch `--status` before daemon initialization, logging setup or heartbeat
  removal. Never instantiate Guard or PendingStore from the report. Open existing
  journal/history databases with SQLite `mode=ro` and `query_only`; do not use
  `immutable`, which can hide an active writer's changes. Bound SQLite waits.
- Only qBittorrent authentication may use POST; other status API requests use GET.
  Never call stop, blocklist, delete, ffprobe, full history scans or cleanup.
  Connection errors must not prevent checking the remaining services/drives.
- Download paths are mapped relative to the trusted qBittorrent root. Open all
  filesystem components without symlink following. Read checks exercise directory
  listing and free-space queries, not arbitrary payloads. Never create files on
  the general download mount or in the incomplete directory.
- Temporary write tests use exclusive random names with mode 0600, write/fsync/
  readback/remove, and descriptor-relative operations. State is writable by
  design. The cleanup alias is tested only when enabled and device/inode matched
  against the read-only completed mapping. Check created-file identity before
  removal; never remove replaced files or change mounts/permissions. As with
  existing cleanup, same-UID/root processes must not race directory/probe changes.
- Run drive checks in isolated subprocesses with a ten-second timeout. An
  interrupted probe can leave only its own random test file. Report failures
  without exposing exception bodies, URLs, credentials or raw remote data.
- Keep monitoring in `<STATE_DB>.status.sqlite3`; never modify deletion schemas
  for display purposes. The daemon collects INFO-level events even when output
  verbosity is higher, while preserving console/audit filters. Collection also
  runs in dry-run because it is observational, independent of deletion evidence.
- Retain seven days of events and twenty issues per severity. Persist exact UTC
  epochs, calculate inclusive rolling windows and present Eastern times. Reject
  candidates include dry-run observations and may repeat across restarts. Warnings
  overlap candidates; errors are separate from warnings. CLEANED metadata-only
  events certify client absence without filesystem targets. Never call candidates
  deleted downloads or infer freed bytes/import counts from logs.
- Label partial history from its actual creation time. Do not backfill estimates
  from rotated logs. State downtime and recording gaps honestly. Monitoring write
  failures must not interrupt polling or change deletion authorization; report a
  rate-limited error and mark recovered history incomplete.
- Sanitize terminal controls, redact configured password/API-key values and use
  the approved palette in `docs/log-palette.svg`. Reuse the viewer's wrapping
  helpers; ship their package path as well as the existing standalone viewer.
- Validate real temporary-file and SQLite behavior, restart/window boundaries,
  failure isolation, read-only journal behavior, secret escaping and absence of
  destructive API calls. Run the full root-guide checks before release.
