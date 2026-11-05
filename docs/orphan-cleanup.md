# Automatic orphan cleanup (v1.9)

This is the agent workflow and operator reference for `orphans.py` and
`tests/test_orphans.py`. Read AGENTS.md and the deletion-verification guide first.

## Policy

Orphan cleanup is an independent, explicitly enabled deletion policy. It removes
regular files recursively under **only** these four dedicated download folders:

- `QBIT_COMPLETE_DIR/tv`
- `QBIT_COMPLETE_DIR/movies`
- `QBIT_INCOMPLETE_DIR/tv`
- `QBIT_INCOMPLETE_DIR/movies`

Every file, including sidecars, unselected files and `.!qB` partials, is eligible
once no qBittorrent torrent owns it for 15 continuously observed minutes. This
also covers old leftovers without a Guard record. Import history is **not**
required for orphan files: the operator explicitly accepts deleting these files
even if they were never imported. Keep personal files and libraries outside
these folders. Empty directories are retained.

Seeding uses the same successful-import cleanup as completed torrents, with
no additional seeding retention. Once each selected video has confirmed Arr
import and the ordinary grace has elapsed, Guard removes the torrent with data.
The orphan scanner waits for that client removal before treating remaining
files as unowned. Live torrents of any category/state reserve their content
directory and known complete/incomplete file variants in the meantime. Existing bad-release and
metadata/stalled-download rejection rules remain in force; zero-byte metadata
waits over ten minutes request an Arr replacement before client deletion. Incomplete-download timeouts follow the
[percentage bands and protection rules](../INSTALL.md#stalled-download-timeouts), with
80% retention and intentional pause/queue exclusions. An exact save tag protects
all files still owned by the live tagged torrent; after manual client removal,
ordinary orphan cleanup may proceed. No permanent ignore is stored. Completed-import and
bad-release cleanup remain separate. Each rejected incomplete torrent requests
an Arr replacement before client deletion.

The cleaner checks every minute and refreshes ownership immediately before each
file removal. An API failure, unsafe/missing metadata, scan/access failure,
changed file/root, clock rollback, or observation gap over two minutes prevents
using the old absence interval. Restart preserves recent observations; longer
downtime requires a new wait. An inaccessible individual cleanup target retries
after a new interval without blocking other eligible files. A bounded scan
refuses nesting beyond 32 levels or over 10,000 entries per scope; at most 32
eligible file operations run per scan. These limits are reported, never bypassed.

## Deployment

Preserve `.env` credentials and `/config/torrent-guard`. No journal reset is
needed: `orphan_candidates` and `orphan_audit` are additive SQLite tables.
The application logs `ORPHAN scan`, `ORPHAN deferred` and `ORPHAN removed` with
paths and outcomes. Dry-run logs proposed removal and keeps observations only
in memory; it never changes existing candidate/audit rows or payloads.

All four directories must already exist on the host and be dedicated to these
downloads. For the usual mapping, their host paths are
`$DOWNLOADS_HOST_PATH/complete/{tv,movies}` and
`$DOWNLOADS_HOST_PATH/incomplete/{tv,movies}`. The override assumes those host
names; edit its **source** paths if your configured directory mapping differs.
Never point a source at a library, parent download directory, or unrelated data.

Start with your existing `DRY_RUN=true` setting:

```bash
docker compose -f compose.yaml -f compose.orphans.yaml up -d --build --force-recreate
docker compose -f compose.yaml -f compose.orphans.yaml logs -f --tail=100 torrent-guard
```

After reviewing the dry run and testing disposable downloads, set `DRY_RUN=false`
in the existing `.env` and repeat the recreate command. Once enabled, orphan
cleanup requires no button clicks. Recreate using the override on future upgrades.
The old `compose.cleanup.yaml` may also be included for existing evidence-based
completed-root fallback; it is not required for orphan cleanup.

Plain Compose keeps downloads read-only and orphan cleanup disabled. The orphan
override sets `ORPHAN_CLEANUP=true` and grants write access through four separate
aliases at `/cleanup/orphans/{complete,incomplete}/{tv,movies}`. Do not enable
only the environment flag without these mounts. The main `/downloads` mount
remains read-only. Keep UID/GID 1000 (or your configured non-root identity),
restricted capabilities and root filesystem, and no Docker socket.

## Filesystem and recovery contract

Discovery opens directory components relative to descriptors with no symlink
following. Symlinks/special files or unavailable scopes defer the scan. Never use
`rmtree`, infer filenames from torrent titles, discover private
`.torrent-guard-*` quarantine directories, or broaden the four write scopes.

Each file's path, root device/inode and file device/inode/size/mtime are saved
before deletion. The write alias must match the read root identity. Reuse
`FileVerifier.remove`: checkpoint a private quarantine, atomically rename,
recheck identity and attachment, then unlink only the unchanged regular file.
Its existing NFS fallback and same-UID/root concurrency limitations apply.
Retain changed captured objects for manual recovery. Interrupted captures resume
from the saved candidate only, after renewed ownership/absence checks. Library
hardlinks survive because only the download name is removed.

`orphan_audit` records independently verified absence after the operation; this
is separate from imported-torrent evidence and archive-only bookkeeping.
**Forget record** continues to archive records only. It does not delete files,
authorize orphan removal, or permanently exempt a file. Once orphan files are
absent, ordinary automatic record retirement can finish old work. Historical
warnings remain history and are not a list of current pending records.

Tests must use real temporary files/SQLite and mocked APIs, asserting ownership
for every client state, fresh checks, delays/resets, restart/quarantine recovery,
changed files/mounts, symlinks, library hardlinks, dry run and out-of-scope files.
These checks do not validate live server APIs, permissions, NFS behavior or Arr
replacement availability. Validate a dry run and disposable downloads on the
actual media server before enabling deletion.
