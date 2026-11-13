# Web status maintenance

Read `AGENTS.md` first. This focused workflow covers `status_report.py`,
`status_web.py`, `web/`, Docker packaging and `tests/test_status.py`. Operator
setup is in [INSTALL.md](../INSTALL.md#web-status-page).

## Isolation and data

- Read `maintenance.json` beside STATE_DB for current host-maintenance status.
  The HTTP path only reads this bounded marker; it never controls Docker or jobs.
  Suppress expected Arr checks while active, show the host detail using safe
  text rendering and reuse the approved palette. Missing means not installed;
  malformed/unreadable existing markers keep Guard processing suspended.

- Keep Guard polling and its SQLite logging handler on the main thread. HTTP
  reads combine cached diagnostics, current read-only bookkeeping and heartbeat.
  The sole POST route may insert a bounded archive request through its own SQLite
  connection. HTTP threads never edit active work, poll APIs, delete files, log
  through the main SQLite handler or run probes. See [retirement](record-retirement.md).
- `StatusCache` runs diagnostics in a subprocess with a 120-second limit and
  waits sixty seconds after completion. Each drive probe has a ten-second limit.
  Preserve old results after collection failures; show their age and mark them
  stale after 180 seconds. Never let monitoring refresh the polling heartbeat.
- Open existing journal/history with SQLite `mode=ro`, `query_only` and bounded
  busy waits, never `immutable`. Do not construct Guard/PendingStore from a
  diagnostic. Cache failures per section without hiding other results.
- Only qBittorrent authentication uses POST. Other checks use GET; never invoke
  stop, blocklist, deletion, ffprobe or full Arr history scans from monitoring.
- Keep history at `<STATE_DB>.status.sqlite3`. The additive `activity` table
  retains up to 100 emitted decisions, not a synthetic torrent processing ledger.
  Preserve v1.6 tables/events and handle an older history lacking this table.
  Retain seven days of metric events and 1,000 issue events per severity. Counts are
  exact rolling emitted-event totals, overlap and include dry-run candidates;
  they are not unique downloads, imported episodes or reclaimed disk space.
- Label partial history and recording failures; do not backfill guesses from
  rotated logs. Show import snapshot timestamps, journal phases and retry
  deadlines. Persisted counters exclude dry-run-only observations.

## Filesystem boundaries

- Map locations relative to the trusted qBittorrent root. Open every filesystem
  component without symlink following. Read checks only list/check space.
- Never write-test the parent download mapping or libraries. State is writable
  by design. Test all four complete/incomplete TV/movie writable cleanup aliases
  after device/inode matching to their mapped read-only folders; missing mounts
  and write/delete failures must be visible. Keep the legacy completed alias
  check when that fallback is enabled.
- Use random exclusive mode-0600 test files, descriptor-relative write/fsync/
  readback/remove and identity checks before removal. Never unlink replacements.
  As with cleanup, same-UID/root processes must not race directory/probe changes.
  Timeouts may leave only the probe's own file; never sweep to recover it.

## HTTP and browser boundaries

- Serve only packaged `/`, `/app.css`, `/app.js`, `/icon.svg` and `/api/status`
  through GET/HEAD routes. POST `/api/forget` queues archive-only bookkeeping;
  it must validate the actual peer, the exact configured STATUS_PUBLIC_ORIGIN
  (or Origin/Host when unset), per-process action token,
  JSON content type, 1-KiB body limit and record revision. No CORS grants,
  destructive actions, arbitrary files, URL credentials or third-party assets.
- Nginx owns authentication/TLS for the entire hostname. The app checks actual
  connecting IPs/CIDRs, never proxy-provided identity or X-Forwarded-For. Defaults
  publish host loopback and allow only loopback; remote proxies require explicit
  private binding/allowlist and Docker-aware firewall configuration. Reject `/0`.
- Bound concurrency to eight handlers, the accept queue and connection timeouts.
  Suppress client-controlled request logs and transport tracebacks. Keep all
  responses no-store, nosniff, frame-denied and under a self-only CSP.
- Redact configured credentials before persistence/display. Browser rendering
  must use textContent and DOM creation, never innerHTML for remote text. Keep
  stale, unavailable, unconfigured and zero states distinct. HTTP freshness is
  separate from daemon liveness. Refresh only reads cached diagnostics/current bookkeeping.
- Do not redact opaque record IDs/revisions into unusable protocol values;
  redact human names, reasons and messages. Keep request results and archives
  distinct from deletion success. Current active counters may be read directly
  from SQLite; diagnostics must still not run on HTTP requests.

## Design and validation

See [design validation](status-design.md) for the concept/browser comparison.
Use [status-palette.svg](status-palette.svg) and the layout reference
[status-concept.png](status-concept.png). The concept contains example data;
never ship those values as live defaults. Tokens: background `#0b1220`, surface
`#121d2e`, border `#26364a`, text `#e8eff7`, secondary `#a7b7ca`, accent `#50d8ed`,
healthy `#63d69a`, waiting `#f6c76b`, error `#fb8792`, dry-run `#d1a5f5`.

Use plain HTML/CSS/JavaScript, uniform gutters, system fonts, readable tables
and responsive stacking. Keep all dates Eastern with an explicit offset in full
timestamps. Missing metrics are dashes, not invented zeros. No browser action
may change torrent state.

Run the root checks plus real HTTP tests, no-probe-on-request checks, proxy
spoofing/path/action rejection, additive history and real temporary-file tests.
Check browser console, CSP, desktop/mobile overflow, range buttons, refresh,
expandable details, initial/unavailable/stale states and malicious text as text.
Use disposable API fixtures for Docker/browser tests; never contact production
or claim these tests validate its API/import/mount configuration.

## v1.9 recovery feedback

Show the configured orphan policy separately from completed evidence fallback.
Archive requests refresh status every two seconds while queued and display errors
in view after closing the dialog. Forget record remains archive-only; it never
triggers the independent orphan policy. Preserve Host including its port at the
proxy; retain the strict Origin/Host and action-token checks.

Include `ORPHAN removed` in recent activity as an orphan file removal, without
counting individual orphan files as imported-torrent cleanup metrics.

## Removal policy reference

The expandable Removal rules panel displays a pure `removal_reference()` payload
from `guard.py`, generated from the same percentage/duration constants that govern
runtime checks. List the absolute live save-tag exemption and positive-total-seeder protection
first. Include all zero-total-seeder bands, 80% incomplete retention,
30-day zero-active-seeder observation, inactive stalls, metadata waits, dangerous/no-media
rejection, completed/seeding imports and optional orphan cleanup. Clearly state
pause/queue, tag/ownership and 80% exceptions, replacement-first ordering and dry
run. No HTTP request may evaluate torrents, probe drives, or mutate cleanup state
to generate this reference. It remains available before diagnostics finish.
Use the approved palette/spacing, semantic table headers and keyboard-operable
details disclosure. Verify desktop/mobile wrapping, opening/closing the panel,
refresh preserving its state, policy values and safe text rendering in Chromium.

The thirty-day rule is based on observed zero active connected seeders, resetting
on a connection; never label it as torrent age. Save overrides completed/imported
and dangerous/no-media cleanup too. Positive totals only protect incomplete
shorter timeouts. Explain that save protection ends when the tag/client is removed,
so it does not create a permanent orphan ignore. Keep these facts from the pure
runtime reference, using the existing palette and wrapping on desktop/mobile.

## Archive origin and hold diagnostics

`STATUS_PUBLIC_ORIGIN` is an optional exact scheme/host/port origin, validated at
startup; it permits TLS/Host-rewriting proxies without trusting forwarded headers
or disabling source-IP/token validation. Reject credentials, paths, wildcards,
queries/fragments and invalid ports. Normalize hostname case/default ports using
browser origin semantics. Split origin/configuration errors from expired-token
errors; a known 4xx rejection was never queued and must not imply uncertain success.

`record_checks` provides current retirement details alongside the original reason.
Redact human detail values, preserve opaque record/revision tokens and read only
current bookkeeping on HTTP requests. Probes stay in the isolated background
collector. Report four-folder capability failures with recreation/UID/mount
troubleshooting; no request, modal or refresh may initiate a permission probe.
Confirm the exact probe name is absent after unlink and never unlink a replacement.

## Paginated monitoring (v2.0)

Retain and return at most 100 recent decisions; render ten per page with First,
Backward, Forward and Last controls. Navigation is browser-only over cached
history, makes no API calls, and preserves the page number through refresh.
Clamp the page when history shrinks; empty/unavailable/single-page states disable
unavailable controls. Use semantic navigation, accessible names/status, keyboard
focus and the approved responsive spacing/palette. No schema reset or historical
backfill is required; previously pruned rows cannot be recovered.

Query warning/error issues only within the inclusive last-24-hour window and
exclude future timestamps. Preserve the per-severity visibility cap and the
independent rolling metrics/audit retention. Refilter cached issue timestamps in
the browser so an aging/stale snapshot cannot display out-of-window records.
Test exact time boundaries, future rows, retention/restart ordering, all four
controls, page preservation/clamping, zero/one/partial/ten-page history, safe text
rendering, desktop/mobile overflow and console/CSP health.

## Warning groups (v2.2)

Warnings/errors have independent ten-card pagination with the same four controls
as recent activity. Keep valid page selection across refresh and clamp it after
expiry/shrink. Backend and browser both enforce last-24-hours and exclude future
events. SQL groups exact messages by severity with first/last timestamps and
occurrence counts. Return the latest 100 groups of warnings and 100 of errors;
retention is bounded to 1,000 events per severity. Older details pruned by prior
versions cannot be recovered. Rolling metric counters still count emitted
events, not unique problems or files; grouping never erases those counters.

Cards separate type, file/download, reason, action, retry/attempt and recovery ID
using the approved cyan/amber/green/purple palette and safe DOM text. Do not show
full filesystem paths on warning cards. Keep exact paths in private audit logs
and saved evidence. The reported orphan-quarantine warnings are real safety
holds, not normal cleanup; repeated attempts on the same retained file can
produce many warnings. Never weaken saved identity checks to silence them.

Test grouping/counts, retained limits, expiry/future filtering, privacy redaction,
reported filenames with spaces, all navigation boundaries, refresh/shrink,
malicious strings as text, desktop/mobile overflow and clean browser console.
