# Record retirement and archive-only requests

Read `AGENTS.md`, the deletion-verification guide and filesystem-retries guide
before changing this workflow. It covers `records.py`, Guard retirement/capture
methods, `status_web.py`, recovery controls and `tests/test_records.py`.

## Contract

This workflow changes bookkeeping only. Never stop a torrent, query/blocklist an
Arr queue, remove files, authorize later cleanup or emit CLEANED from retirement.
Automatic retirement requires a successful, structurally valid client inventory,
independent absence of every saved ordinary/incomplete/quarantine filename on
unchanged accessible mapped roots, and a freshly repeated client-absence check
immediately before archival. Never infer targets from a title or sweep folders.
Incomplete, malformed, symlinked or inaccessible evidence remains inconclusive.
Zero-payload metadata manifests require their existing explicit validation and
certify only client absence with no inferred filesystem targets.

The live `verify` phase with a saved delete request keeps its deletion audit/logging
flow, but confirmed client/file absence may finish verification before old retry
deadlines. This early verification must pass `allow_cleanup=False` and never unlink. Other stale phases and orphaned holds may retire
under the separate absence contract, including in dry run. Arr history is not
needed to retire confirmed-absent files; it remains mandatory before deleting
existing imported files. Retirement checks run at most once per minute and do
not honor old retry delays when fresh absence checks can establish the outcome.

An operator request may archive unverifiable evidence without checking file
absence, but only after fresh confirmation that the torrent is not in qBittorrent.
This is `operator_dismissed`, never a cleanup success or permanent ignore rule.
Changed evidence/journal phases require another review. On client failure, leave
the request queued and fail the poll; do not loop through more stale inventories.

## Persistence and ownership

`record_observations` stores path manifests captured from actual API file lists
when filesystem validation is deferred. An unsuccessful capture invalidates an
older observation instead of pretending its old targets are complete. Store only
the verifier's allowlisted manifest plus the record name/reason. Do not reuse
these observations as pre-deletion evidence. Capture may persist in dry run;
ordinary dry-run retry/timer updates retain their memory-only behavior.

Rebuild an access-check obligation from surviving observations after restart.
Fresh successful recovery removes the observation. Missing-client observations
can independently establish absence; unknown roots or null/malformed captures
need explicit archive-only review. Legacy holds without manifests cannot be
backfilled from log text or names.

`record_archive` preserves all raw active pending, deletion-evidence, retry,
timer and observation rows before removal in the same SQLite transaction. Keep
historical `deletion_audit` untouched. Clear matching in-memory work after the
commit. Raw legacy evidence must survive even if its JSON cannot be interpreted.
Audit data is not routinely pruned; it remains private operational state.

`record_requests` is a durable queue in the existing journal. HTTP may write only
this queue using a separate existing-database connection, never initialize or
migrate the journal on request. Cap queued requests at 32, process at most eight
per poll, deduplicate identical requests and retain recent results. Main-thread
processing performs actual archival; no web/diagnostic thread may use the main
SQLite connection or modify active deletion state.

Use a stable revision over the displayed record's incarnation/evidence/phase.
Retry timestamps/counts may change without invalidating the confirmation. Keep
archive, request and evidence changes transactional and restart/idempotence safe.
If a request completed before a crash but its result did not, a retry should
report the record already inactive without creating duplicate archives.

## HTTP/UI validation

Nginx owns authentication/TLS and the existing source-IP allowlist still applies.
Require same-origin Host/Origin, an unpredictable process token, JSON and a bounded
body for POST `/api/forget`; do not allow action GETs, cross-origin requests or
forwarded-IP spoofing. Expose current recovery records/results without running
probes. Read active counters directly after archival so the diagnostic cache
cannot make a completed request appear still active. Do not conflate historical
warning entries with outstanding work.

The dialog must clearly say archival stops retries without deleting or certifying
files; Cancel is the initial focus. Keep queued, completed and refused results
visible, handle browser timeouts as uncertain submission, and show recent archive
dispositions explicitly. Keep the approved existing palette and responsive layout.

Test real temporary files/SQLite: all phases, dry-run/live, incomplete and
quarantine leftovers, inaccessible/changed mounts, malformed legacy evidence,
client reappearance/outages, restarts, stale confirmations, queue bounds, archive
atomicity, CSRF/source/body checks, immediate counters and absence of destructive
calls. Browser-test cancel/confirm/result and mobile overflow. Production APIs,
mounts and actual data deletion are outside these tests.

## v1.9 automatic orphan cleanup and action feedback

[Orphan cleanup](orphan-cleanup.md) is a separate polling-thread policy, never
a side effect of Forget record. Once files are gone, ordinary verified retirement
can resolve their stale records. Forget request results refresh every two seconds
while queued; POST errors scroll into view after the dialog closes. Preserve the
complete original Host header at the trusted proxy, including nonstandard ports.

## Reappearing records and missing absence proof

After successful explicit archival, remove its hash from the current poll's old
inventory. A fresh absence check may be newer than that snapshot; reprocessing
it would recreate the just-archived hold. Do not add a permanent ignore: a future
real torrent incarnation may be processed normally.

The additive `record_checks` table stores timestamp/detail for the last read-only
retirement attempt. Archive and remove these rows with active work; exclude them
from confirmation revisions so reporting does not invalidate a dialog/request.
Explain client presence, actual saved files/quarantine payloads, missing legacy
paths and verification errors rather than silently skipping them. These diagnostic
writes are bookkeeping in dry run and never authorize payload deletion.

A reverse-proxy Origin/Host mismatch causes HTTP rejection before enqueueing.
Use exact STATUS_PUBLIC_ORIGIN or preserve original Host at Nginx; retain IP and
action-token checks. An accepted request and completed archival remain distinct.
