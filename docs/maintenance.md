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
- Sundays, keep both apps stopped between scripts and run
  `/usr/local/sbin/snapraid-guarded-scrub` on **OMV** at or after 04:30.
- Restore original policies and start only previously running apps once all jobs
  succeed. Weekdays can finish after sync; Sundays finish after scrub. 06:00 is
  an expected end time, never a forced restart deadline.

The coordinator catches up missed ticks during 02:00–06:00, including the spring
DST jump. An active session continues through overruns and across dates. It does
not start a wholly missed new session after 06:00. Long syncs delay scrub until
sync finishes. Failed/interrupted jobs hold maintenance for operator review.

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

Use the installers below for steps 1–3. Run them from an updated Torrent Guard
checkout on each host; keep `maintenance.py`, the `scripts/` directory and its
shared `maintenance_install.py` together. Requires root/sudo, Python 3.10+ and
timezone data. OMV needs `ssh`, `ssh-keygen` and `ssh-keyscan`. The Docker host
needs its already-running SSH server, Docker CLI, `ssh-keygen`, `useradd`,
`getent` and `visudo`. Install missing tools using your host's package manager;
the installers do not upgrade packages or change service settings.

Existing files are **never overwritten or repaired**. Completed repeat runs
verify helper contents, ownership/permissions, account restrictions, keys,
pinned host keys and connection settings without regenerating keys, changing
permissions or prompting for information already configured. Equivalent JSON
formatting and public-key comments are preserved. Missing files can be created
to resume an incomplete installation. A conflicting helper version, account,
key, pin, unsafe path, permission or configuration stops with a diagnostic.
Review such differences manually; rerunning does not bypass them.

Maintenance uses its own `guard-maintenance` account, OMV private key and
known-hosts file. Neither install edits `/etc/ssh/sshd_config`, SSH service
settings, normal `authorized_keys`, `known_hosts`, default identities or client
configuration. It never restarts SSH, starts/stops containers, runs SnapRAID or
schedules work. Maintenance clients ignore normal SSH client configuration,
SSH agents and shared connection sockets. Normal administrator SSH sessions and
connections continue using their existing settings.

## 1. Prepare OMV

First obtain the Docker server's Ed25519 host-key fingerprint through your
existing trusted maintenance connection or console on the **Docker host**:

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

From the Torrent Guard checkout on **OMV**, run:

```bash
sudo python3 scripts/install_maintenance_omv.py
```

On a fresh install, it asks only for the Docker host address and the trusted
SHA256 fingerprint. SSH port defaults to 22; supply `--ssh-port PORT` for a
custom port. It installs the helper, creates a dedicated key only if absent,
prints its **public** key, pins the verified Docker host key and writes the OMV
connection file. It never accepts an unverified scanned host key.

The first connection check normally remains pending until step 2 installs that
public key on Docker. The OMV installer leaves its completed local work intact
and exits **2** with instructions to finish Docker setup and rerun. Copy the
printed `ssh-ed25519 ...` public key; never transfer the private key.

Optional first-install arguments avoid prompts:

```bash
sudo python3 scripts/install_maintenance_omv.py \
  --docker-host DOCKER_HOST --ssh-port 22 --host-fingerprint SHA256:TRUSTED_FINGERPRINT
```

Connection configuration remains `/etc/torrent-guard-maintenance.json`. OMV's
private key is `/root/.ssh/torrent-guard-maintenance`; its separate host pins
are `/root/.ssh/torrent-guard-maintenance-known-hosts`. Existing configuration
with different connection settings is preserved and reported, not replaced.

## 2. Install on the media Docker host

From its Torrent Guard checkout on the **Docker host**, run:

```bash
sudo python3 scripts/install_maintenance_docker.py
```

Paste OMV's maintenance **public** key when requested. That is the only required
first-install question. The installer reads Docker's existing `/state` bind for
`torrent-guard`, installs the helper and creates only the missing dedicated
account, restricted `authorized_keys`, sudo rule, agent configuration and private
recovery directory. It does not change Docker mounts or permissions on Guard's
state directory. `sonarr`, `radarr` and `torrent-guard` must already exist.

It verifies that the dedicated account has password login disabled, no privileged
group membership, and the expected home/shell. New accounts use an unusable
password field so key authentication also works with non-PAM Linux SSH.
Incompatible existing account locks are reported without changing them.
Existing unrestricted keys or incompatible accounts are rejected without
editing them. The forced key may
invoke only the maintenance agent's fixed verbs/session IDs. The account never
joins the Docker group or receives general sudo privileges.

The script validates its dedicated sudo rule and reads the SSH server's effective
public-key policy without changing it. Existing server policies can restrict
new accounts; the final OMV connection check verifies actual access. Any needed
server-policy decision remains an explicit administrator task so existing SSH
access is preserved.

Optional arguments:

```bash
sudo python3 scripts/install_maintenance_docker.py \
  --omv-public-key 'ssh-ed25519 OMV_PUBLIC_KEY_BLOB' \
  --guard-state-dir /actual/guard-state
```

The state-directory override must identify the same directory as Guard's
existing `/state` bind. The installer detects this automatically in normal
setups. Agent configuration is `/etc/torrent-guard-maintenance-agent.json`;
container recovery state is `/var/lib/torrent-guard-maintenance-agent/state.json`.
No existing state or journals are reset.

## 3. Verify from OMV

Rerun on **OMV** after Docker setup:

```bash
sudo python3 scripts/install_maintenance_omv.py
```

It uses the existing settings without questions or file changes and performs
only the read-only remote `check` request. Success verifies SSH authentication,
the restricted command and access to the expected Docker containers. No apps
are stopped and no SnapRAID jobs are run. You can rerun either host installer
at any time to verify its existing installation.

Exit codes: **0** verified; **1** missing prerequisite, conflicting/unsafe setup
or invalid input; **2** OMV files ready but remote verification still pending.
A repeated pending result also covers connectivity, host-key, account-policy,
key or Docker-access problems; resolve them before changing scheduled jobs.
Never disable host-key checking to clear an error.

For inspection of the underlying account/key/sudo rules, see the
[manual setup reference](maintenance-manual.md). If an installed helper differs
from the checkout, review the change and explicitly upgrade it outside active
maintenance, then rerun the installers. They intentionally offer no overwrite
or automatic repair flag.

## 4. Replace the OMV scheduled jobs

In **OMV → Scheduled Jobs**, disable the direct 02:01 sync and Sunday 04:30 scrub
entries. Replace them with **one root job, every minute, every day**:

```bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/local/sbin/torrent-guard-maintenance tick
```

Leave both existing scripts installed on OMV. The coordinator invokes them at
the configured times. Do not leave direct scheduled copies enabled: they bypass
SSH stop confirmation and the coordinator lock. Enable OMV job-failure
notifications. Minute ticks cannot overlap a long-running job.

Rebuild/recreate Guard on the Docker server with your usual Compose overrides.
Its maintenance banner uses the local agent marker. Expected Arr checks and
Guard torrent processing are suspended while active; qBittorrent keeps running.
The polling heartbeat stays live. Missing marker means not installed;
unreadable/malformed existing marker suspends processing for safety.

The host controls stop apps even with Guard `DRY_RUN=true`. Do not run `tick`
manually during the window unless ready for actual stops and SnapRAID work.
On future upgrades, explicitly update the helper on **both hosts** outside active
maintenance after reviewing changes. The installers report version differences
without overwriting them; rebuilding Guard does not update host-installed files.

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
public keys, dedicated host pins, and inactive coordinator journal/lock directory.
Normal administrator identities, known_hosts, authorized_keys, SSH configuration
and services are preserved on both hosts. Original guarded SnapRAID scripts and
existing direct sync/scrub jobs are preserved. Shared parent directories are
kept. Neither uninstaller changes OMV job configuration automatically.

Unexpected files in dedicated directories, shared service groups, or a connection
configuration pointing at normal administrator keys stop removal for review.
There is no force-delete override. Repeat uninstalls tolerate already absent
artifacts. After removing setup on **both hosts**, rerun steps 1–3 for fresh
configuration and a new OMV key. Re-enable the coordinator only after verification.
