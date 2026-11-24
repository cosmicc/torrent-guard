# Array pre-sync protection and SMART health

These checks run on **OMV**, outside the Guard container. Guard receives no raw
block devices, SMART access, Docker socket or SSH credentials. Upgrade both host
helpers using the [normal installers](../INSTALL.md), outside active maintenance.
OMV additionally needs Bash and `smartmontools` (`/usr/sbin/smartctl`); install a
missing package through your normal OMV/Debian administration before rerunning
setup. The installer does not install packages or run sync/scrub during setup.

## Before sync

After the Docker host acknowledges that Sonarr/Radarr are stopped and NFS writes
are flushed, the coordinator runs the shared guarded script's new **read-only
`check-sync` mode**. It uses the same mount/UUID, filesystem type, configuration,
content/parity metadata, exclusions, free-log-space and saved-parity checks as
the operator's existing workflow. Missing/wrong disks, invalid/incomplete diff
reports, timeouts and failed checks withhold both sync and scrub.

The coordinator consumes a bounded structured count/limit report and requires an
explicit successful gate exit. SnapRAID diff's ordinary differences are handled
by the existing wrapper; a raw nonzero exit is never assumed to mean either
permission to sync or a successful check. Array configuration is taken from the
guarded report for subsequent status, coverage and physical SMART discovery.
A conflicting explicit `snapraid_config` setting blocks the gate.

All existing operator limits remain in force. For the recognized original
script, those limits are:

| Change | Existing maximum |
|---|---:|
| Added | 250 |
| Removed | 50 |
| Updated | 100 |
| Moved | 100 |
| Copied | 250 |
| Restored | 50 |
| Relocated | Same as copied, 250 |

The added percentage rule blocks when **removed + updated reaches 5%** of the
existing protected file count. The denominator is equal + removed + updated +
moved + restored + relocated. Added/copied files are excluded from this percentage
but retain their existing individual limits. For example, 50 removals from 1,000
existing files blocks at exactly 5%, even though 50 is within the original
absolute removal limit. Existing thresholds are read from the live script,
never replaced by repository defaults.

Passing checks add equal/added/removed/updated/moved/copied/restored/relocated
counts to the sync-start Pushover message. A blocked check sends **CRITICAL:
SnapRAID sync/scrub BLOCKED**, its safe reason and counts instead of sync-start.
When an earlier mount/status check prevents a diff, counts say unavailable.
Raw script output, UUID mount paths and arbitrary exception text stay on OMV.

A block is saved as a failed sync phase and requires explicit review/retry.
The actual guarded sync repeats its original checks **and** the percentage guard
before updating parity. This protects the final write boundary; the preliminary
report is not permanent authorization. Other clients/writers still require their
own coordination. Diff checks timestamps, size and inode metadata, so changes
that preserve that metadata can escape this tripwire. See the
[SnapRAID diff documentation](https://www.snapraid.it/manual#diff).

## Installer treatment of the shared script

The master OMV installer extends a recognized `/usr/local/sbin/snapraid-guarded`
additively. It retains its private UUIDs, mount paths, exclusions, absolute change
limits, log setup, and existing sync/scrub checks. The read-only gate reuses those
functions, and the percentage check is added to their common threshold function.

Before replacement, exact original bytes are saved as a root-only
`guarded-<sha256>.sh` backup in `/var/lib/torrent-guard-maintenance-array`. The
installer checks Bash syntax, retains the script's permissions and replaces it
atomically. Repeat setup preserves identical bytes/modes/mtimes. Unknown or
modified adapter/control blocks are refused for review; setup never guesses a
patch, resets a script, or overwrites a different backup. This adapter expects
the protected configuration to use a simple absolute path. Runtime counts/limits
must be canonical nonnegative decimal integers with at most 12 digits.

The legacy maintenance-only installers still only create missing helper bundle
files or verify existing files. They do not adapt the shared script. Use the
master installer to complete this feature before enabling a new coordinator.

## After sync/scrub

After all planned jobs finish, OMV checks SMART before asking the Docker host to
restore apps. A script that exits unsuccessfully also triggers a health report;
its original failure and hold remain intact. An unlaunched or interrupted job is
never treated as finished just to start extra diagnostics.

The collector checks guarded mount/UUID preflight, reads **SnapRAID `devices`**,
and runs `smartctl -a -j` once for each physical array disk, including parity.
It never scans unrelated system disks or starts/enables SMART self-tests. Logical
array names are kept separate from current device names: a logical `data5` may
map to `/dev/sdf`. Device type/major/minor are checked before and after reading;
ambiguous mappings, duplicate identities and unverified device changes fail
closed. A final guarded mount/UUID preflight confirms array availability.

SMART reports show the array role, physical device, serial suffix, temperature,
power-on hours, and supported counters: reallocated, pending, offline
uncorrectable, interface CRC, SAS media errors, or NVMe media/endurance metrics.
Unsupported fields show **n/a**, never zero. ATA vendor-specific raw read/seek
error rates are not interpreted as sector counts.

Critical indicators include SMART FAILING/current prefail thresholds, pending
or offline-uncorrectable sectors, a latest failed ATA self-test, active NVMe
critical-warning bits, increasing reallocated/media-error counters, and
**temperature at or above 60°C**. Temperatures from 50°C warn. These thermal
limits are operational defaults, not manufacturer-specific failure predictions.
Stable historical reallocated counts and CRC errors warn; CRC increases identify
a possible cable/controller issue rather than automatically declaring the disk
bad. Historical error/self-test log flags are distinguished from current failure.

Readings and deltas since the previous sample are stored for **180 days** in
root-only `/var/lib/torrent-guard-maintenance-array/smart.sqlite3`. Identity is
based on protocol and serial, so a device-name change keeps its history while a
replacement drive starts separately. Old evidence is retained when counters
reset; a different or unreadable database is never silently reinitialized.
This database is separate from Guard's `/state` and from both maintenance journals.

Pushover sends the important per-drive values in one report, split into numbered
messages if needed for its size limit. **CRITICAL DISK HEALTH: maintenance HELD**
uses high priority (1), as do blocked pre-sync checks and incomplete required
health diagnostics; high priority can bypass Pushover quiet hours. Ordinary
SMART reports and existing routine notifications use normal priority. Incomplete
health is labeled separately and never claims a disk is failing or healthy.

Critical **or incomplete** required health checks keep Sonarr/Radarr stopped and
persist a review hold. Subsequent cron ticks do not repeat the checks or flood
notifications. Pushover delivery failures never bypass a gate/hold or prevent
otherwise authorized restoration. Healthy/warning-only reports permit restoration
only if the jobs also succeeded. SMART indicators cannot guarantee future disk
survival or certify file contents.

## Recovery and configuration

On OMV, inspect the private state without running work:

```bash
sudo /usr/local/sbin/torrent-guard-maintenance status
```

Fix the underlying problem and verify no SnapRAID/guarded jobs remain running.
Then explicitly authorize recovery:

```bash
sudo /usr/local/sbin/torrent-guard-maintenance retry --confirm-jobs-stopped
```

A blocked pre-sync attempt reruns the gate. A SMART hold after completed jobs
rechecks health without repeating sync or scrub. If a job actually failed, only
its failed phase is retried. A still-critical/unreadable health check keeps the
hold. Do not clear journals or remove history to bypass a check. Installer upgrades
remain refused until maintenance has safely finished.

Optional top-level settings in `/etc/torrent-guard-maintenance.json` are:

| Setting | Default | Allowed integers |
|---|---:|---|
| `diff_max_changed_percent` | 5 | 1–100 |
| `pre_sync_timeout_seconds` | 900 | 30–7200 |
| `smart_timeout_seconds` | 30 per drive | 5–120 |

The percentage is frozen per session and passed to both the read-only gate and
actual guarded sync. Standalone/manual guarded sync defaults to 5% unless its
explicit environment sets `TORRENT_GUARD_DIFF_MAX_PERCENT`. SMART collection also
has an overall five-minute budget; command/output timeouts remain incomplete
rather than becoming healthy evidence. A failed-read timeout kills its read-only
process group; ordinary authorized sync/scrub jobs retain their original run-to-
completion/review behavior.

The installed `/usr/local/sbin/torrent-guard-maintenance` and adjacent
`torrent_guard_array.py` form one helper bundle. Upgrade both hosts through the
master installers. Existing active sessions keep their saved plan; these new
checks begin in the first new session after upgrade. Uninstall removes recognized
helper code, but preserves SMART history, original-script backups and the shared
guarded script, including its stronger percentage guard. Original scripts are
never automatically rolled back. Inspect/export those retained records before
any separately authorized manual removal or restoration.
