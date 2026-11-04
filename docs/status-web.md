# Web status maintenance

Read `AGENTS.md` first. This focused workflow covers `status_report.py`,
`status_web.py`, `web/`, Docker packaging and `tests/test_status.py`. Operator
setup is in [INSTALL.md](../INSTALL.md#web-status-page).

## Isolation and data

- Keep Guard polling and its SQLite logging handler on the main thread. HTTP
  threads only read a cached snapshot and the private heartbeat marker. They
  never poll APIs, write state, log through the main SQLite handler or run probes.
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
  retains fifty emitted decisions, not a synthetic torrent processing ledger.
  Preserve v1.6 tables/events and handle an older history lacking this table.
  Retain seven days of metric events and twenty issues per severity. Counts are
  exact rolling emitted-event totals, overlap and include dry-run candidates;
  they are not unique downloads, imported episodes or reclaimed disk space.
- Label partial history and recording failures; do not backfill guesses from
  rotated logs. Show import snapshot timestamps, journal phases and retry
  deadlines. Persisted counters exclude dry-run-only observations.

## Filesystem boundaries

- Map locations relative to the trusted qBittorrent root. Open every filesystem
  component without symlink following. Read checks only list/check space.
- Never write-test downloads/incomplete or libraries. State is writable by
  design. Test the completed cleanup alias only when enabled and device/inode
  matched against the mapped read-only completed root.
- Use random exclusive mode-0600 test files, descriptor-relative write/fsync/
  readback/remove and identity checks before removal. Never unlink replacements.
  As with cleanup, same-UID/root processes must not race directory/probe changes.
  Timeouts may leave only the probe's own file; never sweep to recover it.

## HTTP and browser boundaries

- Serve only packaged `/`, `/app.css`, `/app.js`, `/icon.svg` and `/api/status`
  through fixed routes. No arbitrary file serving, management actions, writable
  endpoints, CORS grants, credentials in URLs or third-party assets.
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
  separate from daemon liveness. Refresh only reads the cache.

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
