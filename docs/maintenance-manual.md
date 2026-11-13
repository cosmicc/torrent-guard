# Manual maintenance setup reference

The preferred setup is the [non-overwriting host installers](maintenance.md#1-prepare-omv).
This reference is for auditing an existing manual installation and deliberate
operator-managed upgrades. The installers never run these overwrite/repair
commands against existing files.

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
sudo ssh -F /dev/null -T -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o ControlMaster=no -o ControlPath=none -o IdentityAgent=none -o UpdateHostKeys=no \
  -o GlobalKnownHostsFile=/dev/null -o ForwardAgent=no -o ClearAllForwardings=yes \
  -o UserKnownHostsFile=/root/.ssh/torrent-guard-maintenance-known-hosts \
  -i /root/.ssh/torrent-guard-maintenance -p 22 guard-maintenance@DOCKER_HOST \
  'check 00000000000000000000000000000000'
```

Expect JSON with `"ok": true`. A connection/authentication/host-key/Docker error
must be resolved before enabling the schedule.

