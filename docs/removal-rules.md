# Removal protection and timeout workflow (v1.9)

Read AGENTS.md, deletion-verification.md and filesystem-retries.md first. This
workflow covers `save_tagged`, `positive_total_seeders`, `stall_conditions`,
metadata timers, pending authorization, `removal_reference` and their tests.
Operator setup is in [INSTALL.md](../INSTALL.md#stalled-download-timeouts).

## Protection precedence

1. An exact comma-separated **save** tag, ignoring case and surrounding space,
   prohibits every Guard stop, rejection/blocklist/replacement, client deletion
   and payload cleanup while that torrent remains tagged in qBittorrent. It
   overrides completed/imported cleanup, bad-file rejection and every timer.
   Check before processing and again before each stop, blocklist and deletion.
2. Existing file-ownership checks protect saved torrents from local cleanup.
   No permanent ignore list is created: removing the tag permits fresh ordinary
   authorization; removing the torrent manually permits ordinary orphan cleanup
   after its separate 15-minute absence observation. Missing file metadata/access
   remains inconclusive. Never derive permanent exemptions from old manifest tags.
3. More than zero total swarm seeders protects **incomplete** downloads from
   shorter timeout rules, including metadata failure. Wait for seeders to connect.
   Only the thirty-day zero-active-seeder exception below 80% can override this.
   It does not override ordinary bad-release rejection or imported completion
   cleanup; only save supplies that absolute exemption.
4. At 80% or more, incomplete downloads are retained against all timeout rules.
   Completed/imported cleanup and dangerous/no-media rejection remain in effect
   unless save is present. Use the higher of API progress and completed/total
   bytes; missing/invalid completion/size/byte/progress facts retain data.

## Conditions and independent observations

Timeouts require unambiguous Arr ownership and an explicit `movies` or `tv` tag.
Unknown seed counts cannot establish zero. Paused/stopped/queued/checking/moving
states cannot initiate a timer; only journaled Guard-owned stops for the same
incarnation may resume pending work. `downloading`, `forcedDL` and `stalledDL`
are the normal incomplete states. Metadata states use explicit zero-payload proof.

- **Zero total swarm seeders (`num_complete == 0`):** below 15% waits three hours;
  15% to under 50% waits three days; 50% to under 80% waits seven days. Preserve
  continuous zero-total history across percentage changes and peer progress.
  Recalculate the required wait for the current band before every action.
  Returning total seeders ends this condition and suppresses the shorter rules.
- **Inactive stall:** below 15%, stalled with zero speed and zero connected
  active seeders (`num_seeds == 0`), waits three hours. Known zero total seeders
  is also required, because positive totals protect shorter timeouts. Progress,
  state/speed recovery or connected/total-seeder recovery ends this observation.
- **No connected active seeders:** below 80%, thirty observed days continuously
  reporting `num_seeds == 0` permits rejection regardless of total seeders.
  First observation starts this clock; torrent added date/age is never substituted.
  Connected seeders or unknown/invalid active counts end it. Peer progress and
  total-seeder changes alone do not reset it. It applies even if peers supply data.
- **Metadata:** more than ten observed minutes with explicit zero payload and
  no file metadata can fail unless save or positive total seeders protects it.
  Metadata with positive totals and zero active seeders can fail only under the
  thirty-day observation. Its version-2 evidence still has no filesystem targets.

`stalled_wait` JSON keeps independent observations and selects the earliest live
qualified deadline. Policy 4 signatures reset legacy age/seeder authorizations
without deleting journals, audits or valid Guard-stop provenance. Reset condition
observations on incarnation/owner/size/path/tag changes, absence or clock rollback.
Restarts/API outages preserve last known observations; observed recovery resets
only its own condition. Save clears active timer eligibility. Dry-run timer
updates remain in memory; initialization is additive and existing state survives.

## Destructive boundary checks

`kind=stalled` handles payload-bearing timeout failures. Before stop, Arr
blocklist and client deletion, refresh current save tags, percentage, selected
condition, file evidence/access and ownership. Every rejected incomplete torrent
requests replacement with `skipRedownload=false`, `removeFromClient=false` before
Guard sends qBittorrent `deleteFiles=true`. Missing queue matches/Arr errors retain
work. Reaching 80%, a positive total-seeder recovery, a connected active seeder,
or entry into a longer-wait band cancels the respective premature deletion.

Long-wait metadata failures retain `kind=metadata`, using version-2 zero-target
manifests with an added `timeout` authorization. Persist this proof before
queuing, and never silently downgrade it to a stale ten-minute rejection on
retry. `_stop_metadata` checkpoints long-wait Guard-stop provenance only after
fresh authorization; `_metadata_removal_ready` checks it again before actions.
Recovered metadata, payload, connected seeders, save tags or changed incarnation
retain the torrent. This evidence never authorizes local file unlink.

Saved timeout reasons or old manifests are never fresh deletion permission.
The original evidence-based local cleanup and independent orphan policy keep
their own ownership, identity, mount and quarantine checks. A live saved torrent
must remain protected regardless of which cleanup path discovers its files.

## Reference and validation

The dashboard Removal rules panel uses pure `removal_reference()` data built from
runtime constants. List save first, positive-total protection second, then all
bands, 80% retention, the thirty-day zero-active condition, metadata, bad-release,
completed/imported and optional orphan policies. Note that save ends on client/
tag removal and that positive-total protection does not exempt good-import
cleanup or bad-release rejection. Reads never call APIs, probe or change state.

Use real temporary SQLite/files plus mocked services. Test save across every
pending kind/phase and tags appearing between checks; total vs active seeders;
exact percentage/time boundaries; observed thirty-day reset/restart/dry run;
metadata zero-target long-wait recovery; failed replacement retaining data;
positive totals still allowing import/bad-release cleanup; and save protection
ending after manual client removal. Assert order and absence of destructive calls.
Run root checks and desktop/mobile policy-panel validation with disposable data.
These tests do not validate production APIs, permissions, NFS or actual deletion.
