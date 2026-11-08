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

## 1. Install the command on both servers

Requires Python 3.10+ and timezone data on both hosts, OpenSSH client on OMV,
OpenSSH server and Docker CLI on the Docker host, and root administration.
Copy the release's `maintenance.py` to each server, then install it on **both**:

```bash
sudo install -o root -g root -m 0755 maintenance.py /usr/local/sbin/torrent-guard-maintenance
```

On **OMV**:

```bash
sudo install -d -o root -g root -m 0700 /var/lib/torrent-guard-maintenance
sudo install -d -o root -g root -m 0700 /root/.ssh
sudo ssh-keygen -t ed25519 -N '' -f /root/.ssh/torrent-guard-maintenance
sudo cat /root/.ssh/torrent-guard-maintenance.pub
```

Use a new dedicated key, and preserve an existing one when upgrading. Copy only
its public key to the Docker host; its private key stays on OMV.

## 2. Restrict SSH on the Docker host

Create a dedicated account **without Docker group membership**. If it already
exists, preserve it and apply the directory permissions below:

```bash
sudo useradd --create-home --home-dir /var/lib/guard-maintenance-ssh --shell /bin/sh guard-maintenance
sudo chown root:root /var/lib/guard-maintenance-ssh
sudo chmod 0755 /var/lib/guard-maintenance-ssh
sudo install -d -o root -g root -m 0755 /var/lib/guard-maintenance-ssh/.ssh
sudo install -d -o root -g root -m 0700 /var/lib/torrent-guard-maintenance-agent
sudoedit /var/lib/guard-maintenance-ssh/.ssh/authorized_keys
```

Add the OMV public key with this prefix on the **same line** (replace the key
placeholder with the actual `ssh-ed25519 ...` public key):

```text
restrict,command="/usr/bin/sudo -n /usr/local/sbin/torrent-guard-maintenance agent \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 REPLACE_WITH_OMV_PUBLIC_KEY
```

The backslash-escaped quotes are part of the authorized_keys syntax. The forced
shell command passes the entire original request as one quoted argument; it
never evaluates it as shell code. The root agent accepts only fixed verbs and a
32-character hexadecimal session ID. It rejects arbitrary commands, paths,
container names and additional arguments. `restrict` disables forwarding and PTY.
Where practical also use `from="OMV_IP"` to restrict the key's source address.

```bash
sudo chown root:root /var/lib/guard-maintenance-ssh/.ssh/authorized_keys
sudo chmod 0644 /var/lib/guard-maintenance-ssh/.ssh/authorized_keys
sudo visudo -f /etc/sudoers.d/torrent-guard-maintenance
```

Add this single sudo rule, then validate it with `sudo visudo -cf /etc/sudoers.d/torrent-guard-maintenance`:

```text
guard-maintenance ALL=(root) NOPASSWD: /usr/local/sbin/torrent-guard-maintenance agent *
```

Do not grant this account a general shell key, password login, Docker group
membership or unrestricted sudo. Password authentication should remain disabled
for this account; key authentication uses the forced command above. Retain the
root ownership of its home/authorized_keys and of the installed helper.

The agent publishes `/config/torrent-guard/maintenance.json` on the **Docker
host**, through Guard's existing state bind. If that host directory differs,
create a root-owned `/etc/torrent-guard-maintenance-agent.json` containing only:

```json
{"status_file": "/actual/guard-state/maintenance.json"}
```

Never put the marker on a protected NFS media share. Preserve Guard's existing
SQLite data. Container recovery state lives separately on the Docker host at
`/var/lib/torrent-guard-maintenance-agent/state.json`.

## 3. Configure OMV's SSH connection

Verify the Docker host's SSH public-key fingerprint through a trusted console
before accepting it into a dedicated known_hosts file. On the Docker host, list
its Ed25519 fingerprint with `sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.
On OMV, collect the key for your actual host (and custom SSH port if needed):

```bash
sudo ssh-keyscan -H DOCKER_HOST | sudo tee /root/.ssh/torrent-guard-maintenance-known-hosts > /dev/null
sudo ssh-keygen -lf /root/.ssh/torrent-guard-maintenance-known-hosts
```

Compare fingerprints before continuing. A scan alone does not authenticate the
host. Keep strict host-key verification enabled. For non-default ports, scan
with `ssh-keyscan -p PORT -H DOCKER_HOST` and use the same port below.

Create root-owned mode-0600 `/etc/torrent-guard-maintenance.json` on **OMV**:

```json
{
  "host": "DOCKER_HOST",
  "user": "guard-maintenance",
  "port": 22,
  "identity_file": "/root/.ssh/torrent-guard-maintenance",
  "known_hosts_file": "/root/.ssh/torrent-guard-maintenance-known-hosts"
}
```

Replace `DOCKER_HOST` with the Docker server's reachable IP/hostname. Set file
ownership/permissions with `sudo chown root:root /etc/torrent-guard-maintenance.json`
and `sudo chmod 0600 /etc/torrent-guard-maintenance.json`.

Test the forced key and Docker access **without stopping apps or running jobs**
from OMV (adjust `-p` for a custom port):

```bash
sudo ssh -T -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/root/.ssh/torrent-guard-maintenance-known-hosts \
  -i /root/.ssh/torrent-guard-maintenance -p 22 guard-maintenance@DOCKER_HOST \
  'check 00000000000000000000000000000000'
```

Expect JSON with `"ok": true`. A connection/authentication/host-key/Docker error
must be resolved before enabling the schedule.

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
On future upgrades, reinstall `maintenance.py` on **both hosts** outside active
maintenance; rebuilding Guard does not update either installed host command.

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
