# Web status design and validation

The layout reference is [status-concept.png](status-concept.png); the approved
colors are [status-palette.svg](status-palette.svg). Both are project assets.
The concept was created with the built-in image generation tool from this brief:

> Single-page Torrent Guard operational dashboard, no navigation or separate
> frontend service. Dark navy background, slate surfaces, cyan accents, green
> health, amber waiting, red errors. Uniform spacing and system typography.
> Header and refresh control; polling strip; qBittorrent/Sonarr/Radarr/storage
> summaries; rolling 6/12/24-hour totals; recent download table; pending work;
> warnings/errors; drive access/free space. Plain HTML/CSS implementation with
> no third-party assets. Example values are design fixtures only.

The implementation was compared visually with the concept at a 1536×1024
desktop viewport and checked at 390×844 mobile. The Browser plugin was not
available, so Playwright used the installed Chromium browser. Both the concept
and rendered screenshots were inspected with `view_image`.

Comparison points: navy/slate/cyan tokens, system type hierarchy, consistent
panel gutters/borders, four service summaries, activity/pending column balance,
tabular recent-download decisions, warning/drive sections and responsive
stacking. The browser version retains the concept's visual structure while
adding actual operational details and explicit coverage/freshness notes.
Production strings differ deliberately from the fixture's abbreviated copy:
connection check times, partial history, settings, safe read-only checks,
persisted timers and retry eligibility are stated precisely. Error count zero
is neutral. Additional drive and pending rows extend the single page vertically;
there are no extra routes or management controls.

Validated interactions: refresh, all three statistics windows, expandable
download details, first-snapshot loading, unavailable history, stale cached
data, lost HTTP connection, automatic retry and hostile text rendered inert.
Desktop/mobile have no page overflow. There were no application console errors
or CSP violations; an intentional aborted-request test produced only the
expected browser network error. UI data came exclusively from disposable
fixtures. Temporary screenshots/browser fixtures are not release assets.
