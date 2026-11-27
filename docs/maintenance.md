# SnapRAID maintenance across two servers

Torrent Guard v2.1 supports **OMV on a separate server from Docker**. Sonarr,
Radarr and Guard run on the Docker host and access OMV media shares over NFS.
Downloads are outside SnapRAID, so qBittorrent can continue downloading.

- **OMV coordinator:** schedules and runs the existing guarded SnapRAID scripts.
- **Docker-host agent:** accepts restricted SSH requests from OMV, stops/restores
  only `sonarr` and `radarr`, flushes pending NFS writes and publishes Guard status.
- **Guard container:** reads its local maintenance marker and suspends torrent
  processing. It receives neither SSH credentials nor Docker socket access.

The SSH host address is operator configuration, never a production default.

## Schedule

- Local Eastern time (`America/New_York`), following EST/EDT automatically.
- At 02:00 daily, save original container identities, running states and restart
  policies, disable automatic restarts, stop both apps and verify they stopped.
- At or after 02:01, authorize the read-only array/diff gate and then run
  `/usr/local/sbin/snapraid-guarded-sync` on **OMV**, with
  no arguments, only after the Docker host confirms the hold and flushes NFS writes.
- On Mondays, run `/usr/local/sbin/snapraid-guarded-scrub` on **OMV** immediately
  after the daily sync succeeds, in the same coordinator invocation. Keep both
  apps stopped throughout sync and scrub, with fresh remote hold/NFS-flush
  confirmation before each script. There is no separate scrub start time.
- Automatic catch-up can add one scrub after a successful sync on other days,
  using the coverage rules below. Keep both apps stopped through every planned job.
- Collect array SMART health before restoration; critical/incomplete results
  latch an operator-review hold. See [array protection and SMART](array-safety.md).
- Restore original policies and start only previously running apps once all jobs
  and required health checks succeed. 06:00 is an expected end time, never a forced restart deadline.

The coordinator catches up missed ticks during 02:00–06:00, including the spring
DST jump. An active session continues through overruns and across dates. It does
not start a wholly missed new session after 06:00. The saved start date determines
whether scrub is part of the session: a Monday sync finishing Tuesday still runs
its scrub immediately afterward. Failed/interrupted jobs hold maintenance for
operator review; completed sync is preserved when retrying a failed scrub.

The guarded scripts must run synchronously and return zero only after work has
finished. They must not background SnapRAID and exit early. Keep their existing
thresholds/checks; both scripts must be executable, root-owned regular files
without group/other write permission. SSH failure or invalid acknowledgements
withhold SnapRAID execution. Lost finish acknowledgements are retried without
re-running completed jobs or stopping already restored apps again.

Other NFS/SMB clients, library writers, automatic image updaters and administrative
Docker commands require separate coordination. This controls these two apps,
not every NFS client. Guard state and download storage must remain outside the
protected array. Keep updates/recreation outside active maintenance.

## Pausing scheduled routines

For array maintenance such as mergerfs rebalancing, disable **Torrent Guard
coordinated SnapRAID maintenance** in OMV Scheduled Jobs and apply the change.
This prevents future coordinator launches; it does not stop an already running
job. Before touching the array, confirm the coordinator is inactive with
`sudo /usr/local/sbin/torrent-guard-maintenance status` and verify no sync/scrub
or guarded job is running. Let active maintenance finish; preserve both journals.
Leave the old direct guarded-script jobs disabled. Re-enable only the coordinator
when ready; during 02:00–06:00 Eastern it may catch up immediately. The master
OMV installer enables this job, so do not run setup merely to pause it.

The independent [array drive watcher](array-monitoring.md) stays active every
60 seconds while this job is disabled and throughout long maintenance jobs.
Its dedicated `/etc/cron.d/torrent-guard-array-watch` is separate from OMV's
generated coordinator schedule. It alerts without controlling jobs or apps.

## Automatic scrub catch-up

Scrub scheduling is fully automatic, with Monday as the weekly baseline. After
successful daily sync, a fresh complete healthy `snapraid status` report drives
these defaults:

- **Above 20% unscrubbed:** start daily catch-up, including a scrub in this session.
- **At 5% or less:** end catch-up and return to weekly Monday scrubs.
- **Between those thresholds:** retain the saved mode across restarts and days.

There is no manual daily/weekly selector. OMV's existing private configuration
optionally accepts top-level `scrub_catchup_start_percent` and
`scrub_catchup_stop_percent`, defaulting to 20 and 5. Both must be integers with
`0 <= stop < start <= 100`. Invalid settings refuse coordinator startup so the
operator can correct them. Thresholds are frozen per session; edits take effect
for the next session. `snapraid_config` must identify the same array used by
the guarded scripts. Changing its configured path resets the saved mode before
fresh coverage is evaluated.

Only a successful, fully captured status report (at most 64 KiB) containing one
unambiguous unscrubbed percentage, no sync in progress and no detected errors
can drive the policy. The supported full-array-scrubbed message counts as zero.
A partial/timed-out, oversized, nonzero, unhealthy or unrecognized report retains
the saved mode, skips extra catch-up authorization for that session and warns
once per incident. The normal Monday scrub remains planned and is still subject
to its own guarded preflight. Recovery clears the status incident. Coverage is
checked even when Pushover is disabled; notifications reuse the fresh report.
Failure paths discard the earlier cache so failure alerts collect fresh status.

At most one scrub runs in a daily session, following successful sync with fresh
remote stop/NFS-flush acknowledgement. The coordinator calls the existing
`/usr/local/sbin/snapraid-guarded-scrub` entry point unchanged. Runtime never
edits scripts or overrides their scrub percentage/age/safety settings. Master
setup adds a recognized pre-sync adapter to the shared guarded script while
preserving its existing policy; see [array protection and SMART](array-safety.md). With
an 8% plan and a 10-day age cutoff, recently synced data may not be eligible;
catch-up has no guaranteed completion date. Sonarr/Radarr remain stopped until
all planned jobs succeed, including overruns. The percentage can also rise
when new data is synced.

After a successful scrub, fresh status records progress and can end catch-up.
Three consecutive successful scrubs with reliable before/after readings but no
percentage decrease warn once per incident; actual percentage improvement resets
it. Catch-up stays active until the stop threshold is met. Missing post-scrub
coverage breaks the no-progress streak without certifying completion. These
warnings never bypass scripts, weaken checks or authorize automatic job retries.
Pushover reports catch-up start, progress and return to weekly in normal
sync/scrub alerts, plus separate unavailable-coverage/no-progress notices after
app restoration. Without keys, operator warnings still appear on OMV.

Catch-up mode and incident counters live in OMV's existing private journal.
Decisions extend the saved job plan atomically, and successful scrub phases
include pending-observation intent. Recovery can finish that observation without
rerunning either job. A lost finish acknowledgement never reopens the hold.
Already active sessions from older releases retain their original job plans;
automation begins with the first new session after both helpers are upgraded.
The existing every-minute coordinator cron entry stays unchanged; do not add
a daily scrub job or re-enable the old direct sync/scrub jobs.

## Pushover notifications

Run the normal media and OMV installers outside maintenance to upgrade both
helpers. Setup offers optional notifications once on each host; Enter disables
these alerts. Copy the application's **API Token/Key** and **Your User Key** from
your [Pushover dashboard](https://pushover.net). Both are entered with hidden input.
Use the same pair on both hosts:

- Media Docker `.env`: `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` (both blank
  disables). Compose passes these variables through its existing `env_file`.
- OMV: a `pushover` object with `app_token` and `user_key` in the existing
  root-owned, mode-0600 `/etc/torrent-guard-maintenance.json`. An empty object
  disables notifications. OMV sends alerts; changing Docker variables alone
  does not reconfigure OMV. No keys are transferred over the maintenance protocol.

When enabled, OMV setup also asks for the SnapRAID configuration file used by
your guarded scripts. Enter its absolute path (often under `/etc/snapraid/` on
OMV), or Enter for SnapRAID's default. The optional top-level `snapraid_config`
setting affects only `/usr/bin/snapraid [-c CONFIG] status`, never job commands.

Sync-only sessions send two alerts: sync starting after confirmed app shutdown,
then sync complete after the Docker host confirms restoration. Sessions with a
weekly or catch-up scrub send three: sync starting, sync complete/scrub starting
while apps stay stopped, and sync/scrub complete after confirmed restoration. Completion/transition messages
include a bounded SnapRAID status summary without the scrub-age chart, starting
at “The oldest block was scrubbed…” when available. Partial/error reports retain
available diagnostics. Notification bodies omit the redundant date/time header;
Pushover already displays delivery time. Sync completion (including the scrub-start
transition) shows `Sync Time: 23min 45sec`; scrub completion also shows, for example,
`Scrub Time: 2hrs 23min`. Each duration uses the largest unit and its next smaller
unit (days/hours, hours/minutes or minutes/seconds), discarding smaller units;
jobs under a minute show seconds alone. Timers cover only the synchronous guarded
script execution, excluding preflight, notification delivery, status collection
and restoration. Successful whole-second timings are saved with job completion
in the existing journal and survive coordinator restarts. An explicit retry
replaces only that job's timing; older completed jobs without timing omit it.
No journal migration or reset is needed.

Apps that were already stopped remain
stopped; completion messages describe restoration of their original states.

Failures also alert, preserving the maintenance hold for review. Alerts show
session/date, job phases, the original script exit code (when recorded), current
Sonarr/Radarr state, the upgrade hold and the safe OMV retry instructions. They
include a bounded SnapRAID status summary. A read-only session-matched status
request checks saved container identities; SSH failure, changed containers or an
older agent yields unknown state. Lost finish acknowledgements may leave apps
running, so an active OMV journal alone is never described as proof of shutdown.
The root failure is stored separately from the changing generic journal detail;
raw script output and exceptions are never sent to Pushover. Old journals cannot
recover an exit code they never recorded. Repeated ticks deduplicate the same
job failure; a failed explicitly authorized retry alerts again. Other changed
failure reasons can also alert again. Routine messages use normal Pushover priority/device settings; blocked
pre-sync and critical/incomplete SMART alerts use high priority (1)
without an added timestamp header. Failure session/date identifiers remain
for recovery. Status output is sent to Pushover, so it may include
array names/details. Its collection is read-only and defaults to a 120-second
limit so larger content files have time to load. Optional top-level
`snapraid_status_timeout_seconds` in OMV's private configuration accepts an
integer from 10 to 600; for example, `"snapraid_status_timeout_seconds": 300`.
Existing configurations automatically use the new default after upgrading the
OMV helper. Invalid values warn and fall back to 120 seconds. This setting
changes only notification status collection, not sync/scrub execution.

A timeout explicitly reports its limit and retains a bounded partial report.
Missing executable/loader or execution permission failures identify a launch
problem; temporary-output errors identify local storage. Nonzero status exits
retain their exit code and output. Status problems never change maintenance
success, authorize restoration or trigger a retry of sync/scrub. The notification
may wait up to this limit for its status summary; final app restoration happens
before collecting the final completion summary.

Delivery follows the [Pushover HTTPS API](https://pushover.net/api), with a
10-second timeout, verified TLS, no redirects and no secrets in URLs or logs.
Attempts are recorded in the existing session journal before sending, including
failures; they are not retried automatically. This prevents repeated cron alerts
but means a crash or delivery outage can lose an alert. Do not use notifications
as proof of array health; inspect the coordinator and job logs when needed.
Invalid notification credentials disable alerts with a warning at runtime.

For later key changes/enabling/disabling, edit both the private media `.env` and
OMV JSON outside active maintenance (preserve all SSH settings), then rerun the
media installer to recreate Guard. The OMV coordinator reads its configuration
on each cron invocation. Installer reruns preserve existing notification choices.
Rebuilding Guard alone never upgrades either host's maintenance helper.

## Installer behavior and prerequisites

The normal setup is [INSTALL.md](../INSTALL.md): `sudo python3 install.py media`
on the media Docker host, then `sudo python3 install.py omv` on OMV. Repeat the
same commands for upgrades. The installer fast-forwards `main` without resetting
local edits; archives use their downloaded revision. `--no-update` selects the
current checkout for offline or explicit-revision testing.

Media setup reuses `.env`, existing Guard settings and `/state`, preserves the
standard cleanup overrides, builds Guard, and recreates only Guard. It creates
missing maintenance setup and updates recognized app-owned helper code. It can
migrate the dedicated service home from the manual v2.1 setup to root ownership
and restrict an identical bare service key. A non-PAM `!` password lock is changed
to an unusable `*` password so the forced key can authenticate. No administrator
account, normal SSH configuration or SSH service is changed or restarted.

OMV creates or preserves its dedicated key and pins a console-verified host key.
If the restricted check fails, a one-time administrator SSH/sudo session enrolls
only that public key on the media host. Passwords go directly to SSH/sudo, not
Python or saved configuration. Different existing keys are refused, not rotated.
Subsequent upgrades need no administrator login when restricted access works.
Runtime maintenance ignores normal SSH config, agents and connection sockets.
The one-time administrator session may use normal default identities or a password,
with isolated host pins and multiplexing disabled.

After a session-matched inactive remote check, OMV uses `omv-confdbadm` and
`omv-salt deploy run cron` to disable the exact two direct guarded-script jobs
and create/reuse one root every-minute coordinator job. A new job is created
disabled using OMV’s new-object identifier, then read back with its assigned
UUID before direct jobs are disabled and the coordinator enabled. Unrelated jobs remain
unchanged. Original job definitions are saved in the root-only
`/var/lib/torrent-guard-maintenance/setup.json`; repeat upgrades preserve this
recovery record. Custom wrapped commands and duplicate coordinator jobs require
manual review. A failed deployment can be resumed by rerunning the installer;
verify OMV Scheduled Jobs before leaving an interrupted installation unattended. Failed operations identify
the database read, disabled-job creation, job update or cron deployment stage.
An error may leave partial configuration; the installer does not claim all
settings are unchanged. Repeating setup reuses a verified disabled coordinator.
The new-object behavior follows the [OMV database API](https://github.com/openmediavault/openmediavault/blob/7.x/deb/openmediavault/usr/lib/python3/dist-packages/openmediavault/config/database.py).

Both hosts refuse upgrades during observed maintenance or busy journal locks.
The helper bundle is installed outside Guard; rebuilding the container alone does not
upgrade it. Run both host commands. Required packages, custom Compose layouts,
permissions and recovery details are in the
[installation supplement](../INSTALL_SUPPLEMENT.md#unified-installer-reference).

All interactive setup and uninstall questions show their guidance before asking:
which host/app to use, a command or settings page to locate the value, the exact
copy format and the effect of defaults/choices. This also covers legacy installers.
Passwords and API keys use hidden input. SSH/sudo handle their own authentication;
setup explains which account/password they are asking for before starting them.
The fingerprint prompt prints the media-host console command and asks for only
its SHA256 token; it never offers an unauthenticated scanned token for copying.

## Legacy maintenance-only installers

`scripts/install_maintenance_omv.py` and `scripts/install_maintenance_docker.py`
remain available for advanced/manual setup. They create missing files and verify
existing bytes, modes and modification times without overwriting them. Their
OMV → media → OMV sequence requires manual public-key copying and manual OMV
scheduling; **it is not the recommended installation process**. See the
[manual setup reference](maintenance-manual.md) for account/key/sudo details.
These legacy entry points do not perform application deployment or upgrades.

Guard reads the local agent marker, suspending processing and expected Arr checks
while active. An unreadable/malformed existing marker suspends processing for
safety. The heartbeat remains live. Host controls stop apps even with Guard
`DRY_RUN=true`. Do not run `tick` manually during the window unless ready for
actual stops and SnapRAID work.

## Schedule upgrades

The weekly scrub moved from Sunday at 04:30 to Monday immediately after sync.
The daily sync remains at 02:01 Eastern, with writers stopped from 02:00. Run the
normal master installers on both hosts outside active maintenance to update
both installed helpers. The single every-minute OMV coordinator job stays in
place; no separate scrub job or manual cron-time change is needed.

New sessions follow weekly Monday scheduling plus automatic catch-up. Existing journal job lists remain
intact so an upgrade/recovery preserves already planned and completed work.
A recorded pending scrub follows its successful sync without the old time gap;
completed sessions are not reopened to add or repeat work. Keep both journals.

## Failure and interrupted-job recovery

On **OMV**, inspect journal/job logs without executing work:

```bash
sudo /usr/local/sbin/torrent-guard-maintenance status
```

Nonzero jobs, lost SSH connectivity, ambiguous/replaced containers and interrupted
processes keep the session active. Both hosts' journals must be preserved.
Restart policies remain disabled until a successful finish acknowledgement.
If the finish response is lost after apps already restarted, retry only confirms
that same completed session; it does not restart jobs or reopen the hold.

An installer message saying maintenance is active or needs recovery means a
host journal is still active; it is not permission to delete the journal, clear
the marker or restart apps manually. On media, inspect the read-only agent state
with `sudo cat /var/lib/torrent-guard-maintenance-agent/state.json`. Matching
session IDs on both hosts confirm which session must recover. An old failed
session retains its original job plan, even across a Monday/date boundary.

The `status` command reports phases and recorded failures; it cannot reconstruct
script errors or exit codes missing from older journals. Inspect the guarded
script's own logs/OMV job output to find the underlying cause. A failed sync may
have made partial progress; a zero process count means it stopped, not that the
underlying issue was fixed.

Fix the underlying problem and verify on OMV that **no SnapRAID or guarded-script
process remains running** using `pgrep -af 'snapraid|snapraid-guarded'`. Only then
authorize a failed/interrupted-job retry:

```bash
sudo /usr/local/sbin/torrent-guard-maintenance retry --confirm-jobs-stopped
```

The next tick retries only failed/interrupted phases; completed jobs remain
complete. A SMART review hold rechecks diagnostics after explicit retry without
rerunning completed sync/scrub work. SSH failures before job launch recover automatically once connectivity
returns. Once status shows `active: false` on OMV and the media agent journal
also shows `active: false`, rerun `sudo python3 install.py media` on media, then
`sudo python3 install.py omv` on OMV to upgrade/configure notifications. New
notification code cannot alert for a failure on an older, not-yet-upgraded host.
Never delete either journal or rewrite container IDs to bypass safety.

Repository tests use isolated fixtures and temporary files. Disposable Docker
checks validate stop/restore behavior, not your NFS mounts, script internals,
SSH account policy, array configuration or other clients. Validate those on your
hosts before relying on the maintenance guarantee.

## Uninstall or reset maintenance setup

These uninstallers cover both the installers above and the **manual v2.1 steps
1–3**, including partially completed setup. No installer receipt is required.
They remove only dedicated maintenance artifacts, not Torrent Guard itself.

First disable any every-minute coordinator job in **OMV → Scheduled Jobs**.
Finish/recover any active maintenance and verify that Sonarr/Radarr are back in
their intended states. The uninstallers refuse active/inconclusive journals,
running maintenance/SnapRAID processes, busy locks and detected enabled
coordinator cron entries. They never force jobs to stop or restart containers.

Preview on the **media Docker host**:

```bash
sudo python3 scripts/uninstall_maintenance_docker.py --dry-run
```

Then remove that host's setup (one confirmation, or use `--yes`):

```bash
sudo python3 scripts/uninstall_maintenance_docker.py
```

This removes the recognized helper bundle, agent configuration, inactive maintenance marker,
private agent journal/lock directory, dedicated authorized keys, sudo rule,
`guard-maintenance` account/private group, recognized service-home skeleton
files and service mail spool. If a partial manual install used a custom Guard
state path but never created agent configuration, pass
`--guard-state-dir /actual/guard-state`. The script removes only `maintenance.json`
there; Guard's SQLite databases, logs, credentials and media remain intact.

Next preview/remove on **OMV**:

```bash
sudo python3 scripts/uninstall_maintenance_omv.py --dry-run
sudo python3 scripts/uninstall_maintenance_omv.py
```

OMV removal also removes the recognized independent array watcher cron file and
its separate private incident journal/lock; a modified schedule or busy watcher
blocks removal. SMART history and array data remain preserved.

OMV removal includes the recognized helper bundle, connection configuration, dedicated private and
public keys, dedicated host pins, and inactive coordinator journal/lock/setup-receipt directory.
Normal administrator identities, known_hosts, authorized_keys, SSH configuration
and services are preserved on both hosts. Original guarded SnapRAID scripts and
existing direct sync/scrub jobs are preserved. Shared parent directories are
kept. Neither uninstaller changes OMV job configuration automatically. SMART history
and guarded-script backups in `/var/lib/torrent-guard-maintenance-array` and the
enhanced shared script are preserved; see the [array recovery guide](array-safety.md).

Unexpected files in dedicated directories, shared service groups, or a connection
configuration pointing at normal administrator keys stop removal for review.
There is no force-delete override. Repeat uninstalls tolerate already absent
artifacts. After removing setup on **both hosts**, run the master installer on media and then OMV for fresh
configuration and a new OMV key. OMV setup reuses/enables a matching disabled coordinator job.
