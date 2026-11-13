"""Create missing maintenance setup files; verify existing files without repair.

These root-only installers never enable jobs, stop containers, reload sshd,
change existing permissions, or overwrite files. They intentionally refuse a
different installed helper version: upgrading that file is an operator action.
"""
import argparse
import base64
import binascii
from dataclasses import dataclass
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Callable

# The installers run from a checkout, not inside the Guard container.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance import ssh_command


USER = "guard-maintenance"
HELPER = "/usr/local/sbin/torrent-guard-maintenance"
FORCED = 'restrict,command="/usr/bin/sudo -n ' + HELPER + ' agent \\"$SSH_ORIGINAL_COMMAND\\"" '
SUDO_RULE = USER + " ALL=(root) NOPASSWD: " + HELPER + " agent *\n"
SOURCE = Path(__file__).resolve().parents[1] / "maintenance.py"


class InstallError(RuntimeError):
    """Existing configuration is unsafe/incompatible, or a prerequisite failed."""


class PendingConnection(InstallError):
    """Local OMV setup is ready but the remote Docker agent is not reachable."""


@dataclass(frozen=True)
class Paths:
    """Fixed application-owned locations; a different root is used only by tests."""

    root: Path = Path("/")

    def path(self, name: str) -> Path:
        """Map a fixed absolute location to its host or isolated fixture root."""
        return self.root / name.lstrip("/")

    @property
    def key(self) -> Path:
        """Dedicated OMV key, separate from default administrator identities."""
        return self.path("/root/.ssh/torrent-guard-maintenance")

    @property
    def hosts(self) -> Path:
        """Dedicated pinned host keys; never use the normal known_hosts file."""
        return self.path("/root/.ssh/torrent-guard-maintenance-known-hosts")

    @property
    def config(self) -> Path:
        """OMV's existing runtime connection configuration."""
        return self.path("/etc/torrent-guard-maintenance.json")


def public_key(value: str) -> str:
    """Accept exactly one real Ed25519 public key; discard its optional comment."""
    parts = value.strip().split()
    if len(value.splitlines()) != 1 or len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise InstallError("Supply one ssh-ed25519 PUBLIC key line, never a private key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InstallError("Invalid public key encoding") from exc
    prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    if len(blob) != len(prefix) + 32 or not blob.startswith(prefix):
        raise InstallError("Invalid Ed25519 public key")
    return "ssh-ed25519 " + parts[1]


def fingerprint(key: str) -> str:
    """Use OpenSSH's SHA256 fingerprint representation for trusted pinning."""
    blob = base64.b64decode(public_key(key).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


class Installer:
    """Perform scoped installation with exclusive creation and read-only reruns."""

    def __init__(self, paths: Paths = Paths(), ask: Callable = input, output: Callable = print):
        self.paths, self.ask, self.output = paths, ask, output

    def run(self, command: list[str], *, data: str | None = None, timeout: int = 30) -> str:
        """Run trusted argv without a shell or printing potentially sensitive output."""
        try:
            result = subprocess.run(command, input=data, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, check=True)
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError(command[0] + " failed; no existing configuration was replaced") from exc

    def required(self, commands: tuple[str, ...]) -> None:
        """Require tools already installed; never upgrade packages or SSH services."""
        missing = [command for command in commands if not shutil.which(command)]
        if missing:
            raise InstallError("Install these prerequisites first: " + ", ".join(missing))

    def info(self, path: Path, *, directory: bool = False, private: bool = False):
        """Reject symlinks, unexpected ownership and writable privileged targets."""
        details = path.lstat()
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (not kind(details.st_mode) or details.st_uid != 0 or details.st_mode & 0o022
                or private and details.st_mode & 0o077):
            raise InstallError(f"Unsafe ownership/type/permissions: {path}; left unchanged")
        return details

    def directory(self, path: Path, mode: int = 0o700, *, private: bool = False) -> None:
        """Create only missing directories, validating each existing component."""
        if path == self.paths.root:
            return
        self.directory(path.parent, 0o755)
        try:
            path.mkdir(mode=mode)
            self.output("Created directory: " + str(path))
        except FileExistsError:
            pass
        self.info(path, directory=True, private=private)

    def existing(self, path: Path) -> bool:
        """Count broken symlinks as existing, so they can never be overwritten."""
        return os.path.lexists(path)

    def file(self, path: Path, content: bytes, mode: int, *, private: bool = False) -> None:
        """Verify an existing file or publish a new complete file without replacement."""
        if self.existing(path):
            details = self.info(path, private=private)
            if path.read_bytes() != content or mode & 0o111 and not details.st_mode & 0o111:
                raise InstallError(f"Existing file differs: {path}; left unchanged. Review/update it manually")
            return
        self.directory(path.parent, 0o755)
        descriptor, temporary = tempfile.mkstemp(prefix=".tg-install-", dir=path.parent)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # link(), unlike replace(), cannot overwrite a file created by a
            # simultaneous installer. Root-controlled parents are validated.
            os.link(temporary, path, follow_symlinks=False)
            self.output("Created file: " + str(path))
        except FileExistsError as exc:
            raise InstallError(f"Target appeared during installation: {path}; rerun to verify") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)

    def json_file(self, path: Path, content: dict) -> None:
        """Preserve formatting of an equivalent existing JSON configuration."""
        if self.existing(path):
            self.info(path, private=True)
            try:
                if json.loads(path.read_text()) == content:
                    return
            except ValueError:
                pass
            raise InstallError(f"Existing JSON differs: {path}; left unchanged")
        self.file(path, (json.dumps(content, indent=2) + "\n").encode(), 0o600, private=True)

    def helper(self) -> None:
        """Never replace an older helper or modify either host's recovery journal."""
        self.file(self.paths.path(HELPER), SOURCE.read_bytes(), 0o755)

    def key_pair(self) -> str:
        """Generate a dedicated pair only if neither file exists; verify existing pairs."""
        key = self.paths.key
        pub = Path(str(key) + ".pub")
        self.directory(key.parent)
        if not self.existing(key):
            if self.existing(pub):
                raise InstallError("Public key exists without private key; preserved for investigation")
            # Generate off-target, then exclusively publish both files. A crash
            # never causes regeneration over a previously published private key.
            with tempfile.TemporaryDirectory(prefix=".tg-key-", dir=key.parent) as directory:
                temporary = Path(directory) / "identity"
                self.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "torrent-guard-maintenance", "-f", str(temporary)])
                self.file(key, temporary.read_bytes(), 0o600, private=True)
                self.file(pub, temporary.with_suffix(".pub").read_bytes(), 0o644)
        self.info(key, private=True)
        # -P '' fails promptly for an encrypted key; never prompt for an
        # administrator passphrase or print private key contents.
        derived = public_key(self.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)]).strip())
        if not self.existing(pub):
            self.file(pub, (derived + "\n").encode(), 0o644)
        self.info(pub)
        if public_key(pub.read_text().strip()) != derived:
            raise InstallError("Dedicated public/private key mismatch; neither file changed")
        return derived

    def host_key(self, config: dict, expected: str | None) -> None:
        """Keep existing pins, or pin only a fingerprint verified through a trusted console."""
        path = self.paths.hosts
        host, port = config["host"], config["port"]
        lookup = host if port == 22 else f"[{host}]:{port}"
        if self.existing(path):
            self.info(path)
            text = self.run(["ssh-keygen", "-F", lookup, "-f", str(path)])
        else:
            text = self.run(["ssh-keyscan", "-T", "5", "-t", "ed25519", "-p", str(port), host])
        keys = set()
        for line in text.splitlines():
            if line and not line.startswith("#"):
                fields = line.split()
                if len(fields) >= 3:
                    keys.add(public_key(" ".join(fields[1:3])))
        if len(keys) != 1:
            raise InstallError("Could not establish one Ed25519 host key; existing pins left unchanged")
        key = keys.pop()
        actual = fingerprint(key)
        if not self.existing(path):
            expected = expected or self.ask("Docker host SHA256 fingerprint from its trusted console: ").strip()
        if expected is not None and expected.strip() != actual:
            raise InstallError("Docker host fingerprint mismatch; host key was not changed")
        if not self.existing(path):
            self.file(path, (lookup + " " + key + "\n").encode(), 0o644)

    def omv(self, host: str | None, port: int | None, expected: str | None) -> None:
        """Prepare OMV, show its public key, and perform only the remote check verb."""
        self.required(("ssh", "ssh-keygen", "ssh-keyscan"))
        if self.existing(self.paths.path(HELPER)):
            self.helper()
        if self.existing(self.paths.config):
            self.info(self.paths.config, private=True)
            config = json.loads(self.paths.config.read_text())
            if not isinstance(config, dict):
                raise InstallError("Existing OMV configuration is not a JSON object")
            if host is not None and config.get("host") != host or port is not None and config.get("port") != port:
                raise InstallError("Requested host/port differs from existing configuration; unchanged")
        else:
            config = {"host": host if host is not None else self.ask("Docker host IP or hostname: ").strip(), "user": USER,
                      "port": port if port is not None else 22, "identity_file": str(self.paths.key), "known_hosts_file": str(self.paths.hosts)}
        if (not isinstance(config.get("host"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", config["host"])
                or type(config.get("port")) is not int or not 1 <= config["port"] <= 65535
                or config.get("user") != USER or config.get("identity_file") != str(self.paths.key)
                or config.get("known_hosts_file") != str(self.paths.hosts)):
            raise InstallError("OMV configuration must use the dedicated maintenance account/key/known-hosts file")
        self.helper()
        self.directory(self.paths.path("/var/lib/torrent-guard-maintenance"), private=True)
        key = self.key_pair()
        self.output("OMV PUBLIC key for the Docker installer:\n" + key)
        self.host_key(config, expected)
        self.json_file(self.paths.config, config)
        try:
            result = json.loads(self.run(ssh_command(config, "check " + "0" * 32), timeout=60))
            if (not isinstance(result, dict) or result.get("ok") is not True
                    or result.get("session") != "0" * 32 or type(result.get("active")) is not bool):
                raise InstallError("Invalid maintenance check acknowledgement")
        except (InstallError, ValueError) as exc:
            raise PendingConnection("OMV files are ready. Complete Docker-host setup, then rerun this installer to verify SSH. No jobs were run") from exc
        self.output("Verified OMV SSH connection and Docker access; no containers stopped or jobs run")

    def account(self) -> bool:
        """Create/verify the dedicated account; return whether it has a ! lock."""
        home = self.paths.path("/var/lib/guard-maintenance-ssh")
        try:
            account = pwd.getpwnam(USER)
        except KeyError:
            # Linux sshd without PAM rejects "!" account locks even for keys.
            # "*" is an unusable password (no password can authenticate) while
            # allowing the dedicated forced public key without changing sshd.
            self.run(["useradd", "--no-create-home", "--user-group", "--home-dir", str(home),
                      "--shell", "/bin/sh", "--password", "*", USER])
            account = pwd.getpwnam(USER)
        if account.pw_uid == 0 or account.pw_dir != str(home) or account.pw_shell != "/bin/sh":
            raise InstallError("Existing maintenance account differs; left unchanged")
        groups = {group.gr_name for group in grp.getgrall() if USER in group.gr_mem or group.gr_gid == account.pw_gid}
        if groups & {"root", "docker", "sudo", "wheel", "admin", "lxd", "incus-admin"}:
            raise InstallError("Maintenance account has privileged groups; left unchanged")
        shadow = self.run(["getent", "shadow", USER]).strip().split(":")
        if len(shadow) < 2 or not shadow[1].startswith(("!", "*")):
            raise InstallError("Maintenance account password is not locked; left unchanged")
        self.directory(home, 0o755)
        self.directory(home / ".ssh", 0o755)
        if any(not self.info(path, directory=True).st_mode & 0o001 for path in (home, home / ".ssh")):
            raise InstallError("Root-owned maintenance home/.ssh must permit the SSH user to traverse them; unchanged")
        return shadow[1].startswith("!")

    def docker(self, supplied_key: str | None, state_dir: str | None) -> None:
        """Install restricted SSH access without modifying sshd or administrator keys."""
        self.required(("docker", "ssh-keygen", "sshd", "visudo", "useradd", "getent"))
        if self.existing(self.paths.path(HELPER)):
            self.helper()
        agent_config = self.paths.path("/etc/torrent-guard-maintenance-agent.json")
        existing_config = None
        if self.existing(agent_config):
            self.info(agent_config, private=True)
            existing_config = json.loads(agent_config.read_text())
            if not isinstance(existing_config, dict) or set(existing_config) != {"status_file"}:
                raise InstallError("Existing agent configuration differs; left unchanged")
        rows = json.loads(self.run(["docker", "container", "inspect", "sonarr", "radarr", "torrent-guard"]))
        if not isinstance(rows, list) or len(rows) != 3:
            raise InstallError("Expected sonarr, radarr and torrent-guard containers")
        guard, = [row for row in rows if row.get("Name") == "/torrent-guard"]
        mounts = [mount for mount in guard.get("Mounts", []) if mount.get("Destination") == "/state" and mount.get("Type") == "bind"]
        if len(mounts) != 1 or mounts[0].get("RW") is not True:
            raise InstallError("Guard needs an existing writable /state bind; no mounts were changed")
        detected = Path(mounts[0]["Source"])
        state = Path(state_dir) if state_dir else detected
        if not state.is_absolute() or not state.is_dir() or state.is_symlink() or not os.path.samefile(state, detected):
            raise InstallError("Guard state directory must match its existing /state bind")
        expected_config = {"status_file": str(state / "maintenance.json")}
        if existing_config is not None and existing_config != expected_config:
            raise InstallError("Existing agent status path differs from Guard mount; left unchanged")
        authorized = self.paths.path("/var/lib/guard-maintenance-ssh/.ssh/authorized_keys")
        if self.existing(authorized):
            self.info(authorized)
            lines = [line for line in authorized.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
            line = lines[0] if len(lines) == 1 else ""
            if line.startswith('from="'):
                restricted = re.fullmatch(r'from="[A-Za-z0-9_.:/,!*?\[\]-]+",(.*)', line)
                line = restricted[1] if restricted else ""
            if not line.startswith(FORCED):
                raise InstallError("Existing dedicated authorized_keys is not the expected restricted key; unchanged")
            if not self.info(authorized).st_mode & 0o004:
                raise InstallError("Root-owned dedicated authorized_keys is not readable by the SSH user; unchanged")
            key = public_key(line[len(FORCED):])
            if supplied_key is not None and public_key(supplied_key) != key:
                raise InstallError("Supplied OMV key differs from existing authorized key; unchanged")
        else:
            key = public_key(supplied_key or self.ask("Paste the OMV maintenance PUBLIC key: ").strip())
        self.helper()
        hard_locked = self.account()
        self.directory(self.paths.path("/var/lib/torrent-guard-maintenance-agent"), private=True)
        self.json_file(agent_config, expected_config)
        if not self.existing(authorized):
            self.file(authorized, (FORCED + key + "\n").encode(), 0o644)
        sudo_file = self.paths.path("/etc/sudoers.d/torrent-guard-maintenance")
        if self.existing(sudo_file):
            self.info(sudo_file)
            rules = [" ".join(line.split()) for line in sudo_file.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
            if rules != [" ".join(SUDO_RULE.split())]:
                raise InstallError("Existing maintenance sudo rule differs; unchanged")
        else:
            self.directory(sudo_file.parent, 0o755)
            with tempfile.TemporaryDirectory(prefix="tg-sudo-") as directory:
                candidate = Path(directory) / "rule"
                candidate.write_text(SUDO_RULE)
                self.run(["visudo", "-cf", str(candidate)])
            self.file(sudo_file, SUDO_RULE.encode(), 0o440)
        self.run(["visudo", "-cf", str(sudo_file)])
        effective = self.run(["sshd", "-T", "-C", f"user={USER},host=localhost,addr=127.0.0.1"])
        settings = dict(line.split(None, 1) for line in effective.splitlines() if " " in line)
        if hard_locked and settings.get("usepam") == "no":
            raise InstallError("Existing account uses a ! lock with non-PAM SSH; public-key login is blocked. Account/sshd left unchanged")
        targets = settings.get("authorizedkeysfile", "").replace("%h", str(authorized.parent.parent)).replace("%u", USER).split()
        if settings.get("pubkeyauthentication") != "yes" or settings.get("forcecommand", "none") != "none" or not any(
                (Path(target) if target.startswith("/") else authorized.parent.parent / target) == authorized for target in targets):
            raise InstallError("Existing sshd policy does not accept this key location; no sshd settings were changed")
        host_public = self.paths.path("/etc/ssh/ssh_host_ed25519_key.pub")
        self.info(host_public)
        self.output("Docker host Ed25519 fingerprint (compare on OMV): " + fingerprint(host_public.read_text().strip()))
        self.output("Docker setup verified. Rerun the OMV installer for its read-only SSH check. No SSH service restart was performed")


def main(role: str) -> int:
    """Ask only for absent setup inputs; keep scheduling and upgrades explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    if role == "omv":
        parser.add_argument("--docker-host")
        parser.add_argument("--ssh-port", type=int)
        parser.add_argument("--host-fingerprint")
    else:
        parser.add_argument("--omv-public-key")
        parser.add_argument("--guard-state-dir")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run this host installer with sudo/root")
    installer = Installer()
    try:
        if role == "omv":
            installer.omv(args.docker_host, args.ssh_port, args.host_fingerprint)
        else:
            installer.docker(args.omv_public_key, args.guard_state_dir)
    except PendingConnection as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (InstallError, OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        print("Installation/verification stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0
