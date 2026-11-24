"""Operator guidance shown before every interactive host-setup question.

Keep the source of each value here so media, OMV and legacy installer prompts
stay consistent. Guidance must never include a user's credentials or an
unverified network-scanned fingerprint as an answer to copy.
"""
from collections.abc import Callable


PROMPT_HELP = {
    "SNAPRAID_CONFIG": (
        "On OMV, inspect your existing /usr/local/sbin/snapraid-guarded-sync script for the "
        "SnapRAID -c/--conf configuration path. OMV commonly keeps array configuration files "
        "under /etc/snapraid/. Enter the absolute file path used by your scripts, without "
        "quotes or command options, or Enter if snapraid status already works with its default "
        "configuration. This affects read-only status summaries only; guarded jobs remain unchanged."
    ),
    "PUSHOVER_ENABLE": (
        "Optional: send sync/diff, scrub, disk SMART and maintenance failure notifications through "
        "Pushover. Enter y to configure keys, or Enter to disable. On media, keys are Docker .env "
        "variables; on OMV, enter the same keys in its private coordinator configuration. "
        "OMV sends notifications and needs outbound HTTPS; delivery failures never stop maintenance."
    ),
    "PUSHOVER_APP_TOKEN": (
        "Sign in at https://pushover.net, create/select your Torrent Guard application under "
        "Your Applications, and copy its API Token/Key. Paste the 30-character token only. "
        "Input is hidden; use the same token on media and OMV."
    ),
    "PUSHOVER_USER_KEY": (
        "Sign in at https://pushover.net and copy Your User Key from the dashboard. "
        "Paste the 30-character user key only, with no quotes or spaces. Input is hidden. "
        "Use the same key on media and OMV; notifications follow your Pushover device settings."
    ),
    "MEDIA_HOST": (
        "On the media Docker server, run hostname -I and choose its LAN IP, or use a hostname "
        "that OMV can resolve. Enter the address of the server running sonarr/radarr, without "
        "http://, https:// or a port. Replace example text such as DOCKER_HOST with that address."
    ),
    "SSH_PORT": (
        "Use the SSH port you use for normal administrator logins to the media Docker server. "
        "This is the SSH service port, not a Docker, Nginx or status-page port. "
        "Press Enter for 22 if you have not changed it."
    ),
    "HOST_FINGERPRINT": (
        "On the MEDIA DOCKER SERVER, use its local console or an already trusted administrator "
        "SSH session and run:\n"
        "  sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256\n"
        "Copy only the SHA256:... value from that output and paste it here on OMV, without "
        "the key size, comment or (ED25519). This verifies the media server's identity."
    ),
    "ADMIN_USERNAME": (
        "Enter the account name you normally use to SSH into the MEDIA DOCKER SERVER, "
        "which must be allowed to run sudo there. In an existing session on that server, "
        "whoami shows your login name. This is not the Sonarr/Radarr Web UI username or "
        "the dedicated guard-maintenance account. It is used once to enroll OMV's public key."
    ),
    "OMV_PUBLIC_KEY": (
        "For this legacy maintenance-only installer, on OMV run:\n"
        "  sudo cat /root/.ssh/torrent-guard-maintenance.pub\n"
        "Copy the single ssh-ed25519 public-key line. Paste only that line, without Markdown "
        "code-block markers. Use the .pub file; the private key stays on OMV. "
        "The master install.py workflow handles this transfer automatically."
    ),
    "QBIT_URL": (
        "Use qBittorrent's Web UI address and published port on the media Docker host, including "
        "http:// or https:// and any URL base. The address must be reachable from the Guard "
        "container. For the same host, the suggested host.docker.internal address uses its "
        "published Web UI port; localhost would refer to Guard itself."
    ),
    "QBIT_USERNAME": (
        "Use the username you normally enter when signing into qBittorrent's Web UI. "
        "You can check its configured username in qBittorrent's Web UI authentication settings."
    ),
    "QBIT_PASSWORD": (
        "Use the password you normally enter when signing into qBittorrent's Web UI. "
        "Input is hidden. If it is unknown, reset it in qBittorrent's Web UI authentication "
        "settings using your existing administrator access."
    ),
    "DOWNLOADS_HOST_PATH": (
        "On the MEDIA DOCKER SERVER, find the existing host folder containing both complete/ "
        "and incomplete/. Check qBittorrent's Docker bind mounts if needed: use the host/source "
        "side of its download mapping, not its container path or the final media library. "
        "Enter an absolute path; the download storage must already be mounted."
    ),
    "QBIT_DOWNLOAD_ROOT": (
        "Check the container/destination side of qBittorrent's Docker download mount. "
        "Enter the absolute path qBittorrent sees that contains both complete/ and incomplete/ "
        "(commonly /downloads). This can differ from the media server's host/source path."
    ),
    "QBIT_COMPLETE_DIR": (
        "In qBittorrent's download/save-path settings, find the completed-download destination, "
        "including any category-specific paths. Enter its completed root as qBittorrent sees "
        "it inside its container, under the download root above; do not use the Arr media library."
    ),
    "QBIT_INCOMPLETE_DIR": (
        "In qBittorrent's download settings, find the folder used to keep incomplete torrents. "
        "Enter that directory as qBittorrent sees it inside its container, under the download "
        "root above. Press Enter if the suggested incomplete/ path matches your settings."
    ),
    "ENABLE_REMOVAL": (
        "Choose whether Guard may stop, blocklist and delete eligible torrents now. "
        "Enter y to enable real removal, or press Enter for a dry run while you check the "
        "paths and decisions in Guard's status page/logs. Change DRY_RUN in .env later "
        "and rerun the media installer when ready."
    ),
    "ENABLE_CLEANUP": (
        "Choose whether Guard may automatically delete eligible leftover/orphan download "
        "files using the completed-download cleanup and the four dedicated complete/incomplete "
        "tv/movies folders. Enter y to enable the scoped writable mounts, or press Enter to "
        "leave local cleanup disabled. Guard's dry-run setting still controls actual deletion."
    ),
    "UNINSTALL": (
        "Review the removal plan above. Enter y to remove this host's dedicated maintenance "
        "setup, or press Enter to leave it unchanged. Guard's main database, logs and media "
        "are preserved. Disable the coordinator job and finish maintenance before removal."
    ),
}

for _app in ("SONARR", "RADARR"):
    _title = _app.title()
    PROMPT_HELP.update({
        _app + "_URL": (
            f"Use the address and published port you use to open {_title} on the media Docker "
            "server, including http:// or https:// and any configured URL base. Guard must be "
            "able to reach it from its container; host.docker.internal uses host-published ports."
        ),
        _app + "_API_KEY": (
            f"Open {_title}'s Web UI, go to Settings > General, and copy its API Key. "
            f"Paste the {_title} key here, not qBittorrent's password. Input is hidden."
        ),
        _app + "_CATEGORY": (
            f"In {_title}'s Web UI, open Settings > Download Clients, edit its qBittorrent "
            "client and copy the Category value exactly. It must match the category assigned "
            "to that app's torrents in qBittorrent; it is separate from torrent tags."
        ),
    })


def ask_with_help(
    setting: str,
    prompt: str,
    *,
    read: Callable[[str], str],
    output: Callable[[str], object],
) -> str:
    """Explain the input source before reading; never print the supplied answer."""
    output("\n" + PROMPT_HELP[setting])
    return read(prompt)
