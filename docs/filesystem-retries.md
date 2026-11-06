# Filesystem recovery and NFS stale handles

Available starting with v0.7. Installations on v0.6 or earlier must upgrade
to receive persistent filesystem-error holds and retry backoff.

An unresolved filesystem error is not evidence of bad media or proof that a
download was deleted. Guard retains the torrent and journal while errors remain,
including when qBittorrent changes its progress/completion state.

## Validation and destructive-call boundaries

Media validation preserves `errno.ESTALE` from path resolution, stat, open and
read operations. File readability is checked using actual stat/open/read calls;
`os.access()` or a suppressed `exists()` failure cannot establish accessibility.
The result is `DEFER validation`, with `ESTALE` identified where available.

ffprobe failures trigger another file-access check. If the file became
inaccessible, its probe result is inconclusive. Reported stale handles, I/O
errors, lost connections, permission problems and timeouts also defer processing,
even if a subsequent small read succeeds. Malformed stream metadata is
inconclusive. Readable media with a genuine invalid-stream result can still be
rejected; this change does not disable content verification.

Tracked file paths are checked before metadata-based rejection and before
pending stop, blocklist or API-deletion calls. This includes incomplete torrents;
these access checks never run ffprobe on incomplete files. File-manifest checks
continue to gate local leftover removal. Errors preserve pending work instead of
allowing a later metadata decision to bypass the filesystem problem.

## Backoff and restart behavior

The first unresolved check schedules a retry after 30 seconds. Repeated failures
use 60, 120, 240, then at most 300 seconds. The daemon continues polling other
torrents and does not sleep inside a torrent's validation path. The warning
contains `retry_in`, the attempt number and (since v1.3) Eastern `retry_at` (EST/EDT with numeric offset) and is emitted only on an attempted
retry, rather than every five-second poll.

SQLite's additive `filesystem_retry` table records the hash, attempt count,
next retry time and reason. Restarting the daemon reloads the hold. Changing a
torrent to incomplete, missing files or another state does not clear it. Guard
rechecks actual access when the deadline arrives; complete torrents also rerun
media inspection. Only successful recovery checks clear the hold. An absent
torrent's saved-path deletion verification must establish recovery instead.

The separate [record-retirement workflow](record-retirement.md) now archives
missing-client records after independently verifying all saved names absent.
It can also accept an explicit archive-only **Forget record** action when legacy
evidence is unverifiable. These outcomes retire bookkeeping rather than assert
filesystem recovery, successful import, or deletion. Existing files remain intact.

The backoff also applies to other inconclusive validation deferrals, such as
unavailable media, to avoid busy retry loops. No environment settings change.
Keep the existing state database; do not delete retry rows to force cleanup.

Ordinary dry-run retries remain memory-only. Captured path observations and
audit bookkeeping may persist; verified retirement or explicit archive-only
requests may clear active retry rows after preserving them in the archive.
They do not stop torrents, blocklist or delete payloads. Additive schema
initialization remains part of application startup.

## Operator checks

Inspect `DEFER validation` and the associated download name, paths and reason. Check the NFS server,
mount availability, host/container mappings, and access for the configured
runtime UID (default 1000). Diagnose the
storage failure instead of making downloads writable or bypassing import checks.
When access returns, the next scheduled retry logs recovery and resumes ordinary
classification/import verification. Normal import grace and ownership rules
still apply.

Backoff begins after the operating system returns an error. It cannot interrupt
a kernel syscall blocked on an unresponsive hard NFS mount. A stalled poll can
make the existing completed-poll Docker healthcheck unhealthy; that status does
not itself prove deletion failure or automatically restart the container.

Tests use temporary files plus injected ESTALE/I/O failures. They verify
deferral, suppressed destructive calls, restart persistence, backoff deadlines,
recovery and dry-run behavior. They do not simulate an actual NFS server outage.

The v1.3 retry timestamp reflects the later active filesystem/cleanup deadline;
both holds must expire before processing resumes. NFS EINVAL during protected
quarantine rename may indicate an unsupported flag rather than unavailable
files. See [NFS rename compatibility](deletion-verification.md#nfs-rename-compatibility-v13)
for the guarded fallback and its concurrency requirements. Other filesystem
errors continue to retain files.

Confirmed client/file absence can now finish a saved delete request or retire
other stale work during the once-per-minute retirement pass, without waiting
old backoff. Early verification is strictly read-only. Current per-record
retirement details identify the reason an absent-client hold still cannot retire.
Write/delete probe failures concern cleanup permission, not permission to archive
bookkeeping; archives write only `/state` after fresh client-absence validation.
