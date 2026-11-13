"""Remove only recognized maintenance artifacts after a fail-closed preflight.

No manifests from the new installer are required: the documented manual v2.1
paths are included. There is no recursive removal, account-home removal flag,
automatic job cancellation, container restart, or SSH configuration rewrite.
"""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
from typing import Callable

if __package__:
    from .maintenance_install import HELPER, USER, InstallError, Installer, Paths
else:
    from maintenance_install import HELPER, USER, InstallError, Installer, Paths


class Uninstaller:
    """Plan explicit removals, preserving unrelated state and unexpected files."""

    def __init__(self, paths: Paths = Paths(), output: Callable = print):
        self.paths, self.output = paths, output
        self.run = Installer(paths, output=output).run
        self.files: dict[Path, tuple] = {}
        self.directories: list[Path] = []
        self.account = None
        self.group = None

    @contextmanager
    def parent(self, path: Path):
        """Open each ancestor without symlink following and anchor unlink to it."""
        relative = path.relative_to(self.paths.root)
        descriptor = os.open(self.paths.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            yield descriptor
        finally:
            os.close(descriptor)

    def details(self, path: Path):
        """Return a no-follow stat; missing artifacts make repeat removal harmless."""
        try:
            with self.parent(path) as descriptor:
                return os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def contents(self, path: Path) -> bytes:
        """Read bounded regular files without following links or special devices."""
        with self.parent(path) as descriptor:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InstallError("Expected a regular maintenance file: " + str(path))
                data = stream.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise InstallError("Maintenance file is unexpectedly large: " + str(path))
                return data

    def json_data(self, path: Path) -> dict | None:
        """Inconclusive recovery state must never be treated as inactive."""
        if self.details(path) is None:
            return None
        try:
            value = json.loads(self.contents(path))
        except (ValueError, OSError) as exc:
            raise InstallError("Cannot establish maintenance state/configuration: " + str(path)) from exc
        if not isinstance(value, dict):
            raise InstallError("Invalid maintenance JSON; preserve for review: " + str(path))
        return value

    def add_file(self, path: Path) -> None:
        """Record a leaf's identity; links are removed themselves, never followed."""
        details = self.details(path)
        if details is None:
            return
        if not (stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)):
            raise InstallError("Unexpected maintenance file type: " + str(path))
        self.files[path] = (details.st_dev, details.st_ino, details.st_mode)

    def directory(self, path: Path, allowed: set[str]) -> None:
        """Plan only explicit children; unknown content blocks removal before writes."""
        details = self.details(path)
        if details is None:
            return
        if not stat.S_ISDIR(details.st_mode):
            raise InstallError("Maintenance directory is a link or unexpected type: " + str(path))
        with self.parent(path) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                unexpected = set(os.listdir(descriptor)) - allowed
            finally:
                os.close(descriptor)
        if unexpected:
            raise InstallError("Unexpected files in " + str(path) + "; review manually before uninstalling")
        self.directories.append(path)

    def processes(self) -> list[str]:
        """Reject running helpers/SnapRAID without killing processes or guessing age."""
        found = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit() or int(process.name) == os.getpid():
                continue
            try:
                args = (process / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            names = [Path(os.fsdecode(arg)).name for arg in args if arg]
            if any(name in ("torrent-guard-maintenance", "snapraid", "snapraid-guarded-sync", "snapraid-guarded-scrub") for name in names):
                found.append(process.name)
            elif "maintenance.py" in names and any(command in names for command in ("tick", "agent", "retry")):
                found.append(process.name)
        return found

    def schedules(self) -> list[Path]:
        """Find enabled coordinator cron entries; OMV owns their removal through its UI."""
        paths = [self.paths.path("/etc/crontab"), self.paths.path("/var/spool/cron/crontabs/root"),
                 self.paths.path("/var/spool/cron/root"), self.paths.path("/etc/crontabs/root")]
        directory = self.paths.path("/etc/cron.d")
        if self.details(directory) is not None:
            paths.extend(directory.iterdir())
        enabled = []
        for path in paths:
            # Cron locations have standard symlink aliases on Alpine. Resolve
            # them for read-only inspection only; removal paths never do this.
            path = path.resolve()
            path.relative_to(self.paths.root)
            details = self.details(path)
            if details is None or not stat.S_ISREG(details.st_mode):
                continue
            for line in self.contents(path).decode(errors="replace").splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"(?:/usr/local/sbin/)?torrent-guard-maintenance\s+tick(?:\s|$)", line):
                    enabled.append(path)
                    break
        return enabled

    def idle(self, marker: Path | None, journals: list[Path]) -> None:
        """Active or unreadable holds block all removal, including key deletion."""
        if self.processes():
            raise InstallError("A maintenance/SnapRAID process is running; finish or recover it before uninstalling")
        if self.schedules():
            raise InstallError("Disable the coordinator's OMV/cron job first; no scheduled jobs were changed")
        for path in journals + ([marker] if marker else []):
            state = self.json_data(path)
            if state is not None and (state.get("schema") != 1 or state.get("active") is not False):
                raise InstallError("Maintenance is active or inconclusive: " + str(path) + "; complete recovery first")

    def account_plan(self) -> None:
        """Remove only the dedicated service identity, including a manual create-home."""
        home = self.paths.path("/var/lib/guard-maintenance-ssh")
        try:
            account = pwd.getpwnam(USER)
        except KeyError:
            account = None
        if account is not None:
            if account.pw_uid == 0 or account.pw_dir != str(home):
                raise InstallError("Maintenance account has an unexpected UID/home; no account was changed")
            self.account = account
        try:
            group = grp.getgrnam(USER)
        except KeyError:
            group = None
        if group is not None:
            if group.gr_gid == 0 or any(member != USER for member in group.gr_mem) or any(
                    user.pw_name != USER and user.pw_gid == group.gr_gid for user in pwd.getpwall()):
                raise InstallError("Maintenance group is shared with another account; review before uninstalling")
            self.group = group
        # useradd --create-home from the manual v2.1 guide may copy these files.
        skeleton = {".bashrc", ".bash_logout", ".profile", ".bash_profile", ".bash_history", ".hushlogin"}
        self.directory(home, skeleton | {".ssh"})
        for name in skeleton:
            self.add_file(home / name)
        ssh = home / ".ssh"
        self.directory(ssh, {"authorized_keys", "authorized_keys2"})
        self.add_file(ssh / "authorized_keys")
        self.add_file(ssh / "authorized_keys2")
        # Remove only this service user's mail spool; never remove a shared mail
        # directory, and permit only the two standard system mail aliases.
        mail_bases = {self.paths.path("/var/mail"), self.paths.path("/var/spool/mail")}
        for base in mail_bases:
            if os.path.lexists(base) and base.is_symlink():
                target = base.resolve()
                if target not in mail_bases:
                    raise InstallError("Unexpected mail directory alias; service mail left unchanged")
                base = target
            self.add_file(base / USER)

    def plan(self, role: str, state_dir: str | None = None) -> tuple[Path | None, list[Path]]:
        """Build the complete fixed-scope removal plan before deleting anything."""
        self.files.clear()
        self.directories.clear()
        self.account = self.group = None
        marker = None
        private = self.paths.path("/var/lib/torrent-guard-maintenance" + ("-agent" if role == "docker" else ""))
        journals = [private / "state.json"]
        if role == "docker":
            config = self.paths.path("/etc/torrent-guard-maintenance-agent.json")
            data = self.json_data(config)
            if data is not None and (set(data) != {"status_file"} or not isinstance(data["status_file"], str)):
                raise InstallError("Unrecognized agent configuration; review it before uninstalling")
            saved = Path(data["status_file"]) if data else self.paths.path("/config/torrent-guard/maintenance.json")
            marker = Path(state_dir) / "maintenance.json" if state_dir else saved
            if not marker.is_absolute() or marker.name != "maintenance.json" or data and marker != saved:
                raise InstallError("Status marker must match the configured Guard state path")
            marker.relative_to(self.paths.root)
            self.add_file(marker)
            self.add_file(config)
            self.account_plan()
            sudo_file = self.paths.path("/etc/sudoers.d/torrent-guard-maintenance")
            if self.details(sudo_file) is not None:
                for line in self.contents(sudo_file).decode().splitlines():
                    if line.strip() and not line.lstrip().startswith("#") and not (
                            line.split()[0] == USER and HELPER + " agent" in line):
                        raise InstallError("Dedicated sudo file contains unrelated rules; left unchanged")
            self.add_file(sudo_file)
        else:
            config = self.paths.config
            data = self.json_data(config)
            key, pins = self.paths.key, self.paths.hosts
            if data:
                for name, default in (("identity_file", key), ("known_hosts_file", pins)):
                    value = data.get(name, str(default))
                    if not isinstance(value, str):
                        raise InstallError("Unrecognized OMV key configuration")
                    target = Path(value)
                    if not target.is_absolute() or target.parent != self.paths.key.parent or not target.name.startswith("torrent-guard-maintenance"):
                        raise InstallError("OMV configuration points outside dedicated maintenance SSH files; normal keys preserved")
                    if name == "identity_file":
                        key = target
                    else:
                        pins = target
            for path in {self.paths.key, Path(str(self.paths.key) + ".pub"), self.paths.hosts,
                         key, Path(str(key) + ".pub"), pins, config}:
                self.add_file(path)
        self.add_file(self.paths.path(HELPER))
        self.directory(private, {"state.json", "lock"})
        self.add_file(private / "state.json")
        self.add_file(private / "lock")
        self.idle(marker, journals)
        return marker, journals

    @contextmanager
    def locks(self, journals: list[Path]):
        """Hold existing journal locks throughout final checks and deletion."""
        with ExitStack() as stack:
            for path in journals:
                lock = path.parent / "lock"
                if self.details(lock) is None:
                    continue
                with self.parent(lock) as descriptor:
                    fd = os.open(lock.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                stream = stack.enter_context(os.fdopen(fd, "rb"))
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InstallError("Unexpected journal lock type; removal withheld")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise InstallError("Maintenance journal is busy; removal withheld") from exc
            yield

    def remove(self, role: str, marker: Path | None, journals: list[Path]) -> None:
        """Apply the identity-checked plan without broad unlink or home deletion."""
        self.idle(marker, journals)
        # Revoke the dedicated entry point before removing its configuration.
        first = self.paths.path(HELPER)
        ordered = sorted(self.files, key=lambda path: (path != first, str(path)))
        for path in ordered:
            details = self.details(path)
            if details is None:
                continue
            if (details.st_dev, details.st_ino, details.st_mode) != self.files[path]:
                raise InstallError("Removal target changed; left for review: " + str(path))
            with self.parent(path) as descriptor:
                os.unlink(path.name, dir_fd=descriptor)
            self.output("Removed: " + str(path))
        if role == "docker" and self.account is not None:
            # Never use userdel --remove: only the exact planned home paths may
            # be removed. A live service-account process makes userdel refuse.
            self.run(["userdel", USER])
        if role == "docker" and self.group is not None:
            try:
                remaining = grp.getgrnam(USER)
            except KeyError:
                remaining = None  # Some userdel implementations remove its private group.
            if remaining is not None:
                if remaining.gr_gid != self.group.gr_gid or remaining.gr_mem:
                    raise InstallError("Maintenance group changed; left for review")
                self.run(["groupdel", USER])
        for path in sorted(set(self.directories), key=lambda value: len(value.parts), reverse=True):
            with self.parent(path) as descriptor:
                os.rmdir(path.name, dir_fd=descriptor)
            self.output("Removed empty directory: " + str(path))


def main(role: str) -> int:
    """Preview/confirm maintenance-only removal; repeat calls tolerate missing setup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the displayed maintenance-only removal plan")
    parser.add_argument("--dry-run", action="store_true", help="show the checked plan without removing anything")
    if role == "docker":
        parser.add_argument("--guard-state-dir", help="custom Guard state bind if no agent configuration was written")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run this host uninstaller with sudo/root")
    uninstaller = Uninstaller()
    try:
        marker, journals = uninstaller.plan(role, getattr(args, "guard_state_dir", None))
        if not uninstaller.files and not uninstaller.directories and uninstaller.account is None and uninstaller.group is None:
            print("No maintenance installation remains")
            return 0
        print("Maintenance-only removal plan:")
        for path in sorted(uninstaller.files):
            print("  " + str(path))
        for path in sorted(set(uninstaller.directories)):
            print("  empty directory: " + str(path))
        if uninstaller.account is not None:
            print("  dedicated account: " + USER)
        if uninstaller.group is not None:
            print("  dedicated group: " + USER)
        if args.dry_run:
            return 0
        if not args.yes and input("Remove this maintenance setup? [y/N] ").strip().lower() != "y":
            print("Nothing removed")
            return 0
        with uninstaller.locks(journals):
            uninstaller.remove(role, marker, journals)
        print("Maintenance setup removed; the host installer can create a fresh setup")
        return 0
    except (InstallError, OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        print("Uninstall stopped: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit("Use uninstall_maintenance_omv.py or uninstall_maintenance_docker.py")
