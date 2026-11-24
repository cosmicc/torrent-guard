# Install or upgrade Torrent Guard

Run the installer on **both servers, media host first**. It installs or upgrades
Torrent Guard and its SnapRAID maintenance integration. Future upgrades use the
same installer commands; existing settings, keys and journals are preserved.

Before starting: both hosts need Git, Python 3.10+ and root/sudo access.
The media host needs Docker Compose, an existing SSH server, and the existing
`sonarr` and `radarr` containers. OMV needs your working NFS mounts and executable
`/usr/local/sbin/snapraid-guarded-sync` and `snapraid-guarded-scrub` scripts.
[Extra prerequisites and nonstandard setups](INSTALL_SUPPLEMENT.md#unified-installer-reference).

## 1. Media Docker server

```bash
git clone https://github.com/cosmicc/torrent-guard.git
cd torrent-guard
sudo python3 install.py media
```

If already cloned, use that directory instead. The installer updates the checkout,
reuses your `.env` (or recovers an existing Guard container's settings), builds and
recreates Guard with the existing cleanup overrides, and prepares restricted SSH.
On first installation it asks only for missing settings and cleanup choices.
**Keep the displayed host fingerprint for the OMV step.**

## 2. OMV server

```bash
git clone https://github.com/cosmicc/torrent-guard.git
cd torrent-guard
sudo python3 install.py omv
```

First setup asks for the media host address, SSH port, its trusted fingerprint,
and a one-time administrator SSH login if key enrollment is needed. SSH/sudo may
ask for your password; it is never saved. No manual key copying is required.
Normal administrator SSH settings and connections remain unchanged. Each prompt
explains where to find its answer. For the fingerprint, run
`sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256` in a trusted
media-host session and paste only its `SHA256:...` value into the OMV prompt.

The installer verifies restricted access, disables the two direct guarded-script
jobs, and enables one coordinated OMV job. Sonarr/Radarr stop at 2am Eastern
(with daylight saving time). Daily sync starts at 2:01am, followed by weekly
Monday scrub. Automatic catch-up adds daily scrubs above 20% unscrubbed and
returns to weekly at 5% or less. Apps stay stopped through planned jobs and
resume after success. Existing guarded scrub limits remain in effect.
qBittorrent keeps running.
**No further scheduling or installer pass is needed.**

## Future upgrades

In the existing checkout, run `sudo python3 install.py media` on the media host,
then `sudo python3 install.py omv` on OMV. Each command fetches current `main`
and upgrades that host. For an older checkout without `install.py`, run
`git pull --ff-only` once first. Do upgrades outside active maintenance.

Afterward, check your existing status page. A fresh installation needs its
[authenticated Nginx proxy](INSTALL_SUPPLEMENT.md#web-status-page) configured once
if you want browser access. If you chose dry run, enable real cleanup when ready
by setting `DRY_RUN=false` in `.env` and rerunning the media installer.

For custom layouts, recovery, manual commands or uninstalling, use the
[installation supplement](INSTALL_SUPPLEMENT.md) and [maintenance reference](docs/maintenance.md).
