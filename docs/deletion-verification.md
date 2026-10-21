# Download deletion verification and leftover cleanup

Available starting with v0.6. Installations on v0.5 or earlier must upgrade
before using the settings or Compose override below.

Guard saves file evidence before requesting qBittorrent `deleteFiles=true`.
It reports `CLEANED` only after the torrent is absent and every recorded file
path is independently confirmed absent. The operation stays in SQLite while
files remain, a mount is unavailable, or verification encounters an error.

## Evidence and scope

`deletion.py` handles filesystem checks. The SQLite `deletion_evidence` table
stores each operation's immutable target list, file identities, root identities,
original torrent/import evidence, last delete-request time, and quarantine
progress. Successful verification moves the evidence into `deletion_audit` in
the same transaction that clears pending work.

The target list comes from all qBittorrent files, including unselected files and
non-video payloads. Original names and potential `.!qB` names are checked under
reported final/temporary save locations. No torrent-title guesses, recursive
sweeps, wildcard deletion, or arbitrary library paths are used. The manifest
excludes magnet URIs and unnecessary API fields that can contain tracker secrets.

Missing metadata, unsafe paths, symlinks, special files, changed root identities,
and permission errors retain pending work. The same rules apply after restart.
Missing intermediate download directories can establish that their recorded file
names no longer exist, but the configured root itself must remain available
with the identity saved before deletion.

Independent verification concerns recorded download names. It does not prove
that arbitrary untracked files or empty torrent directories have been removed.
Imported library copies and hardlinks remain intact when a download name is
removed. Logs count original/incomplete path variants, not just video files.

## Optional completed-download write access

Standard Compose keeps `/downloads` read-only. Enable `compose.cleanup.yaml`
only when automatic removal of verified completed-download leftovers is wanted.
The override adds a single writable bind at `/cleanup/completed` and sets
`CLEANUP_LEFTOVERS=true`. It never mounts a library or the whole download parent
writable. The bind source must already exist; Compose must not create a missing
host directory and silently use it as the download location.

For the operator's confirmed mapping, merge these nonsecret values into the
existing host `.env` without replacing credentials or other settings:

```dotenv
DOWNLOADS_HOST_PATH=/mnt/downloads
QBIT_DOWNLOAD_ROOT=/downloads
QBIT_COMPLETE_DIR=/downloads/complete
CLEANUP_COMPLETE_HOST_PATH=/mnt/downloads/complete
DELETE_SETTLE_SECONDS=120
DRY_RUN=true
```

Retain the actual configured `QBIT_INCOMPLETE_DIR`. Do not replace it based on
this completed-directory example. Both the repository default and this operator's directory use `complete`.
`CLEANUP_COMPLETE_HOST_PATH` is a host bind source; `/cleanup/completed` is the
separate internal write alias, independent of the host directory's final name.

Deploy the override explicitly:

```bash
docker compose -f compose.yaml -f compose.cleanup.yaml up -d --build --force-recreate
docker compose -f compose.yaml -f compose.cleanup.yaml logs -f --tail=100 torrent-guard
```

After validating decisions and permissions with disposable downloads, set
`DRY_RUN=false` and recreate with the same two Compose files. Dry-run does not
request deletion, capture/update operation evidence, quarantine, or remove files
while polling. Schema initialization remains part of application startup.

UID/GID 65534 must have the directory permissions needed to rename a recorded
file, create a private quarantine directory beside it, and unlink the quarantined
file. Grant access only to intended download directories using appropriate group
permissions or ACLs. Do not use root, a Docker socket, extra capabilities, a
writable root filesystem, or world-writable download permissions as a shortcut.
Permission failures are reported and remain pending.

## Local cleanup rules

The default `DELETE_SETTLE_SECONDS=120` (minimum 30) allows qBittorrent to finish
its own deletion. The deadline is measured from the last persisted delete
request, including retries. Once the torrent is absent, the deadline does not
reset on polling or restart.

After this delay, local removal requires all of the following:

- Explicit opt-in and a matching writable completed-root bind.
- Current matching Arr import evidence and elapsed grace for imported cleanup;
  successful imports are never blocklisted. Rejections must already have passed
  the existing Arr blocklist phase.
- A fresh qBittorrent inventory with no same-hash reappearance and no other
  torrent claiming a recorded path. Missing or ambiguous client metadata defers
  local removal, including metadata for unrelated torrents.
- The same configured read/write root identity and the original regular file's
  device, inode, size, and nanosecond modification time.

Automatic removal is limited to the completed root. Incomplete leftovers are
verified and reported but require qBittorrent or manual remediation; the app is
not given writable incomplete-directory access. Newly appearing files, replaced
files, and changed payloads are retained for review.

The file is moved with Linux `renameat2(RENAME_NOREPLACE)` into a private
`.torrent-guard-<random>` directory beside it. The destination is checkpointed
before the move. Guard verifies the captured object's identity before unlinking
it permanently, then independently checks absence again. This happens immediately
after the final identity check; there is no recycle bin or normal file-retention
period inside quarantine. A changed object captured during a
race is preserved in quarantine and reported for manual recovery. If renameat2
is unavailable or unsupported by the filesystem, local cleanup fails closed.

Quarantine progress survives restarts. Empty quarantine directories are retained;
Guard does not recursively delete directories. A source path reappearing alongside
its quarantine payload stops automatic removal and preserves both objects.
Inspect saved `quarantine` paths in `deletion_evidence` when recovery is required.
Never erase the journal or blindly delete quarantine contents to force cleanup.

Directory components are opened without following symlinks. Parent attachment
is checked via `/proc/self/fd` before destructive operations. Host administrators
must not concurrently relocate or rewrite cleanup directories while local
cleanup is running. These checks are not a guarantee against an adversarial
host process racing directory moves or modifying files as the same UID/root.

## Migration and existing leftovers

The migration adds evidence/audit tables and preserves existing pending rows.
A legacy pending record can obtain a snapshot while its torrent still exists.
A missing-torrent record without saved evidence requires manual review and is
never silently marked clean. Retry attempts stop if the torrent creation time
or tracked filename list changes after the snapshot.

The reported Greenland journal entry was already removed by v0.5. Neither its
file identity nor a full tracked-file list can be reconstructed from the log or
torrent title. This implementation does not automatically adopt that orphan or
scan `/mnt/downloads/complete` for likely matches. Review that specific leftover
manually after checking its imported library copy. New operations retain the
necessary evidence before qBittorrent forgets the torrent.

## Validation and operation

`tests/test_deletion.py` uses actual temporary files and SQLite with mocked remote
APIs. It covers remaining files, delayed cleanup, copies/hardlinks, changed files,
symlinks, unavailable/replaced roots, mismatched mounts, API/permission failures,
other torrent ownership, restart recovery, quarantine interruption, replacement
races, unselected files, incomplete suffixes, and dry-run behavior. These tests
cannot prove the production server's API behavior, bind mounts, or permissions.

`PENDING disk verification` reports unresolved checks with retained evidence;
`LEFTOVER cleanup attempted` records local attempts. `CLEANED` requires both
client absence and independent recorded-file absence. Persistent errors repeat
at most once a minute unless the reason changes. The ordinary healthcheck still
measures completed polling, not whether all cleanups succeed.

If only plain `docker compose up` was used, automatic leftover removal is
disabled by default. Enable the override using the commands above; a container
restart alone does not add the write mount or reload its environment. Existing
pending evidence is retained and retried after recreation. Do not erase the
database or delete/re-add torrents to make Guard retry. The startup `Cleanup
settings` log shows the active flag and settle interval. Starting with v0.8, diagnostics
explicitly distinguish disabled cleanup, a settle deadline, and files still
present after an attempted removal; v0.7 used a generic warning for all three.
