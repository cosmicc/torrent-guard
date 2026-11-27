# Continuous OMV array accessibility monitoring

The master OMV installer enables an independent check **every 60 seconds** for
all data and parity paths in the guarded SnapRAID configuration. It uses the
existing OMV Pushover keys. There are no new credentials or interactive questions.
Upgrade both host helpers with the normal [installer](../INSTALL.md), outside
active maintenance; rebuilding Guard alone does not install this host feature.

The dedicated root cron file `/etc/cron.d/torrent-guard-array-watch` runs:

```bash
/usr/local/sbin/torrent-guard-maintenance watch-array
```

This job is separate from OMV's coordinated sync/scrub job. Disabling that job
for mergerfs rebalancing leaves drive monitoring active. Long sync/scrub jobs
and maintenance review holds also do not block the monitor. It only observes
and alerts: it does not stop/start apps, launch sync/scrub, modify parity or media,
or clear maintenance holds. Existing guarded checks still govern those actions.

## What is checked

- Read the same configuration selected by the trusted guarded script; reject a
  conflicting explicit `snapraid_config` in OMV's private configuration.
- Include every configured data drive and parity path, including split parity
  paths. Explicit absolute paths and ordinary quoted paths are supported;
  unsupported/ambiguous configuration raises a monitoring alert.
- Before enrolling new paths, require the existing guarded mount/UUID preflight.
  Keep the expected filesystem UUID and mount target in the private inventory.
- Reject missing mounts, root-filesystem fallback, changed identities and
  read-only filesystems. Verify directory device identity before reading it.
- Check kernel block-device presence and available online state, including
  partition parents and mapper/MD backing devices. Then read each configured
  directory without creating files, accessing raw disks or running SMART tests.

Each invocation gives observations a **20-second overall deadline**. A failed or
timed-out probe reports accessibility as unknown, never recovered. A killed worker
stuck in uninterruptible kernel I/O retains the monitor lock until it exits, so
later cron ticks cannot pile up more disk workers. Inspect OMV kernel/storage
logs if the monitor reports that a previous probe is still running.

Detection normally occurs on the next minute's check, followed by probe and
Pushover delivery time. This checks accessibility, not every sector, file integrity
or continuous SMART health. Cached reads and failures invisible to the kernel
may not be detected until a real I/O error or device-state change becomes visible.

## Alerts and recovery

An inaccessible data/parity role triggers **high-priority Pushover** immediately
after the check. An unavailable configuration, enrollment or timed-out probe
uses a distinct monitoring-unavailable alert. Several affected drives are batched
within Pushover's message limit; alerts identify logical array roles. Reasons and
the private drive inventory are recorded on OMV:

```bash
sudo cat /var/lib/torrent-guard-array-watch/state.json
```

The directory is mode 0700 and the journal/lock are mode 0600. These are separate
from both maintenance journals, Guard's database and SMART history. A fresh install
needs no migration; an unreadable or corrupt existing monitor journal is retained
for review rather than reset. Configuration/journal failures that prevent safely
recording an alert are logged locally without sending an unrecorded notification.

One alert attempt is persisted before sending per outage, surviving cron restarts.
A confirmed successful check sends one normal-priority recovery notification and
allows a later outage to alert again. Incomplete checks do not clear drive outages.
Recovery does not certify media integrity or release maintenance review holds.
Delivery remains best effort: failed/uncertain requests are logged, not retried
automatically. With Pushover disabled, checks and incident logs continue; enabling
valid keys reports any still-current unannounced outage.

Known filesystem UUID changes require operator review. For an intentional drive
replacement at an unchanged path, first verify the replacement UUID and update
the guarded script's expected identity. With the watcher temporarily disabled
and no probe running, preserve a copy of its journal and update only that role's
recorded UUID/mount to the independently verified values. Never reset maintenance
journals or SMART history to silence an alert. Newly configured paths are enrolled
only after guarded preflight and all accessibility checks succeed.

To temporarily disable the watcher itself, comment out its single `* * * * * root`
line in `/etc/cron.d/torrent-guard-array-watch`; uncomment it to resume. Restore
the original file before installer reruns or uninstall: modified cron definitions
are preserved and refused for review. There is no need to disable it merely to
pause scheduled sync/scrub. Installation/removal refuses a busy monitor lock;
retry after its short check finishes. Maintenance-only OMV uninstall removes
the recognized watcher cron file and private watcher state, preserving SMART
history, script backups and all array data.
