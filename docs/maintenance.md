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
- At or after 02:01, run `/usr/local/sbin/snapraid-guarded-sync` on **OMV**, with
  no arguments, only after the Docker host confirms the hold and flushes NFS writes.
- On Mondays, run `/usr/local/sbin/snapraid-guarded-scrub` on **OMV** immediately
  after the daily sync succeeds, in the same coordinator invocation. Keep both
  apps stopped throughout sync and scrub, with fresh remote hold/NFS-flush
  confirmation before each script. There is no separate scrub start time.
- Restore original policies and start only previously running apps once all jobs
  succeed: Monday sessions finish after scrub; every other day, including Sunday,
  finishes after sync. 06:00 is an expected end time, never a forced restart deadline.

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
The helper is installed outside Guard; rebuilding the container alone does not
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

New sessions follow the Monday schedule. Existing journal job lists remain
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

Fix the underlying problem and verify on OMV that **no SnapRAID or guarded-script
process remains running**. Only then authorize a failed/interrupted-job retry:

```bash
sudo /usr/local/sbin/torrent-guard-maintenance retry --confirm-jobs-stopped
```

The next tick retries only failed/interrupted phases; completed jobs remain
complete. SSH failures before job launch recover automatically once connectivity
returns. Never delete either journal or rewrite container IDs to bypass safety.

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

This removes the helper, agent configuration, inactive maintenance marker,
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

OMV removal includes the helper, connection configuration, dedicated private and
public keys, dedicated host pins, and inactive coordinator journal/lock/setup-receipt directory.
Normal administrator identities, known_hosts, authorized_keys, SSH configuration
and services are preserved on both hosts. Original guarded SnapRAID scripts and
existing direct sync/scrub jobs are preserved. Shared parent directories are
kept. Neither uninstaller changes OMV job configuration automatically.

Unexpected files in dedicated directories, shared service groups, or a connection
configuration pointing at normal administrator keys stop removal for review.
There is no force-delete override. Repeat uninstalls tolerate already absent
artifacts. After removing setup on **both hosts**, run the master installer on media and then OMV for fresh
configuration and a new OMV key. OMV setup reuses/enables a matching disabled coordinator job.
