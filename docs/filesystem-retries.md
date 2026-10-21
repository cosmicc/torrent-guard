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
contains `retry_in` and the attempt number and is emitted only on an attempted
retry, rather than every five-second poll.

SQLite's additive `filesystem_retry` table records the hash, attempt count,
next retry time and reason. Restarting the daemon reloads the hold. Changing a
torrent to incomplete, missing files or another state does not clear it. Guard
rechecks actual access when the deadline arrives; complete torrents also rerun
media inspection. Only successful recovery checks clear the hold. An absent
torrent's saved-path deletion verification must establish recovery instead.

The backoff also applies to other inconclusive validation deferrals, such as
unavailable media, to avoid busy retry loops. No environment settings change.
Keep the existing state database; do not delete retry rows to force cleanup.

Dry-run keeps newly observed retry state in memory and does not change persisted
retry rows while polling. Additive schema initialization still occurs during
startup, consistent with the existing journal initialization workflow.

## Operator checks

Inspect `DEFER validation` and the associated download name, paths and reason. Check the NFS server,
mount availability, host/container mappings, and UID 65534 access. Diagnose the
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
