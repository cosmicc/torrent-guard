"""One entry point for media-host deployment and OMV maintenance setup/upgrades."""
import argparse
import ast
from contextlib import contextmanager
import fcntl
import getpass
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from maintenance import atomic_json, ssh_command, pushover_keys
from torrent_guard_array import WATCH_CRON, WATCH_DIRECTORY, WATCH_SCHEDULE
from scripts.app_install import AppInstall, prompt_pushover
from scripts.guarded_adapter import install_guarded_adapter
from scripts.maintenance_install import ARRAY_HELPER, ARRAY_SOURCE, FORCED, HELPER, SOURCE, USER, Installer, InstallError, public_key
from scripts.maintenance_uninstall import Uninstaller


ROOT = Path(__file__).resolve().parents[1]
MODEL = "conf.system.cron.job"
TICK = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin " + HELPER + " tick"
SCHEDULE = {"enable": True, "execution": "exactly", "type": "userdefined", "username": "root",
            "minute": "*", "hour": "*", "month": "*", "dayofmonth": "*", "dayofweek": "*",
            "everynminute": False, "everynhour": False, "everyndayofmonth": False, "command": TICK}


def run(command: list[str], *, data: str | None = None, stream: bool = False, timeout: int | None = None, env: dict | None = None) -> str:
    """Execute argv without shell expansion; keep credentials out of error text."""
    try:
        result = subprocess.run(command, input=data, text=True, check=True, timeout=timeout, env=env,
                                stdout=None if stream else subprocess.PIPE,
                                stderr=None if stream else subprocess.PIPE)
        return result.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(command[0] + " failed. Setup may be partially complete; see INSTALL_SUPPLEMENT.md") from exc


def update_checkout(root: Path, execute=run) -> bool:
    """Fast-forward as the checkout owner; never reset edits or change git config."""
    if not (root / ".git").exists():
        return False  # A downloaded source archive is already an explicit revision.
    owner = root.stat().st_uid
    prefix = [] if owner == os.geteuid() else ["sudo", "-H", "-n", "-u", pwd.getpwuid(owner).pw_name, "--"]
    git = prefix + ["git", "-C", str(root)]
    if execute(git + ["status", "--porcelain", "--untracked-files=no"]).strip():
        raise InstallError("Tracked checkout files were edited; preserve/reconcile those edits before upgrading")
    if execute(git + ["branch", "--show-current"]).strip() != "main":
        raise InstallError("Automatic upgrades require the main branch; no branch was changed")
    before = execute(git + ["rev-parse", "HEAD"]).strip()
    execute(git + ["pull", "--ff-only"], stream=True)
    return before != execute(git + ["rev-parse", "HEAD"]).strip()


class UpgradeInstaller(Installer):
    """Upgrade app-owned code while retaining the low-level installers' strict mode."""

    def helper(self) -> None:
        """Validate the complete bundle before upgrading the sidecar, then entry point."""
        bundle = ((ARRAY_HELPER, ARRAY_SOURCE, 0o644, {"pre_sync_check", "check_smart_health"}),
                  (HELPER, SOURCE, 0o755, {"Coordinator", "read_maintenance"}))
        for target, source, mode, required in bundle:
            path = self.paths.path(target)
            self.directory(path.parent)
            if not self.existing(path):
                continue
            self.info(path)
            if path.read_bytes() == source.read_bytes():
                continue
            try:
                definitions = {node.name for node in ast.parse(path.read_bytes()).body
                               if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
            except (ValueError, SyntaxError) as error:
                raise InstallError("Installed helper is unrecognized; preserve it for review") from error
            if not required.issubset(definitions):
                raise InstallError("Refusing to replace an unrelated file at the helper path")
        for target, source, mode, _required in bundle:
            path, content = self.paths.path(target), source.read_bytes()
            if not self.existing(path):
                self.file(path, content, mode)
                continue
            if path.read_bytes() == content:
                continue
            descriptor, temporary = tempfile.mkstemp(prefix=".tg-upgrade-", dir=path.parent)
            try:
                os.fchmod(descriptor, mode)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                self.output("Upgraded installed maintenance helper code")
            finally:
                Path(temporary).unlink(missing_ok=True)

    def docker(self, supplied_key: str | None, state_dir: str | None, *, prepare_key: bool = False) -> None:
        """Migrate only the known service home from documented manual installs."""
        try:
            account = pwd.getpwnam(USER)
        except KeyError:
            account = None
        home = self.paths.path("/var/lib/guard-maintenance-ssh")
        if account is not None:
            if account.pw_uid == 0 or account.pw_dir != str(home) or account.pw_shell != "/bin/sh":
                raise InstallError("Unexpected maintenance account; administrator review is required")
            groups = {group.gr_name for group in grp.getgrall() if USER in group.gr_mem or group.gr_gid == account.pw_gid}
            if groups & {"root", "docker", "sudo", "wheel", "admin", "lxd", "incus-admin"}:
                raise InstallError("Maintenance account has privileged groups; preserved for review")
            for path in (home, home / ".ssh"):
                if path.exists():
                    info = path.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, account.pw_uid):
                        raise InstallError("Unexpected service-home ownership/type; preserved for review")
                    os.chown(path, 0, 0)
                    os.chmod(path, 0o755)
            authorized = home / ".ssh/authorized_keys"
            if authorized.exists():
                info = authorized.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, account.pw_uid):
                    raise InstallError("Unexpected dedicated key file; preserved for review")
                lines = [line for line in authorized.read_text().splitlines() if line.strip() and not line.startswith("#")]
                if len(lines) == 1 and lines[0].startswith("ssh-ed25519 "):
                    content = FORCED + public_key(lines[0]) + "\n"
                    # A bare key from a partial manual install is the same
                    # credential with tighter restrictions, not a key rotation.
                    from scripts.app_install import save_env
                    save_env(authorized, content, authorized.read_text(), (0, 0))
                os.chown(authorized, 0, 0)
                os.chmod(authorized, 0o644)
        super().docker(supplied_key, state_dir, prepare_key=prepare_key)

    def account(self) -> bool:
        """Keep password login disabled while fixing a known non-PAM ! lock."""
        hard_locked = super().account()
        if hard_locked:
            effective = self.run(["sshd", "-T", "-C", f"user={USER},host=localhost,addr=127.0.0.1"])
            if "usepam no" in effective.splitlines():
                self.run(["usermod", "--password", "*", USER])
                return False
        return hard_locked


@contextmanager
def idle_setup(role: str):
    """Refuse upgrades during observed maintenance and serialize local setup runs."""
    checks = Uninstaller()
    if checks.processes():
        raise InstallError("Maintenance/SnapRAID is running; rerun this installer after it finishes")
    private = Path("/var/lib/torrent-guard-maintenance" + ("-agent" if role == "media" else ""))
    state = checks.json_data(private / "state.json")
    if state is not None and (state.get("schema") != 1 or state.get("active") is not False):
        raise InstallError("Maintenance is active or needs recovery; complete it before upgrading")
    if role == "media":
        config = checks.json_data(Path("/etc/torrent-guard-maintenance-agent.json"))
        marker = checks.json_data(Path(config["status_file"]) if config else Path("/config/torrent-guard/maintenance.json"))
        if marker is not None and (marker.get("schema") != 1 or marker.get("active") is not False):
            raise InstallError("Guard still has an active maintenance hold; recover it before upgrading")
    lock_path = Path("/run/lock/torrent-guard-setup-" + role + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("Another installer is running") from exc
        # The runtime uses the same journal lock. Keep a scheduler invocation
        # from starting between the inactivity check and helper replacement.
        installer = Installer()
        installer.directory(private, private=True)
        if not os.path.lexists(private / "lock"):
            try:
                installer.file(private / "lock", b"", 0o600, private=True)
            except FileExistsError:
                pass  # A runtime invocation won publication; lock it below.
        with checks.locks([private / "state.json"]):
            state = checks.json_data(private / "state.json")
            if state is not None and (state.get("schema") != 1 or state.get("active") is not False):
                raise InstallError("Maintenance started during setup; rerun after it finishes")
            yield


def configure_omv_schedule(installer: Installer, execute=run) -> None:
    """Create through OMV's new-object ID, then migrate only the exact jobs.

    OMV assigns the durable UUID when its defaults' new-object identifier is
    submitted. Supplying an invented UUID asks OMV to update a missing record.
    Create disabled and read back the actual object before touching direct jobs.
    """
    installer.required(("omv-confdbadm", "omv-salt"))

    def call(stage: str, command: list[str], **kwargs) -> str:
        """Identify the failed operation without exposing cron payloads/secrets."""
        installer.output("OMV schedule: " + stage)
        try:
            return execute(command, **kwargs)
        except InstallError as exc:
            raise InstallError(
                "OMV schedule setup failed while " + stage
                + ". Check OMV Scheduled Jobs; setup may be partially complete. "
                "Original job definitions are retained in setup.json when migration has begun."
            ) from exc

    def read_jobs(stage: str) -> list[dict]:
        """Validate database reads before selecting or updating any job."""
        jobs = json.loads(call(stage, ["omv-confdbadm", "read", MODEL]))
        if (not isinstance(jobs, list) or any(not isinstance(job, dict)
                or not isinstance(job.get("uuid"), str) or not isinstance(job.get("command"), str)
                for job in jobs) or len({job["uuid"] for job in jobs}) != len(jobs)):
            raise InstallError("Unexpected OMV scheduled-job data; no further schedule changes were made")
        return jobs

    jobs = read_jobs("reading scheduled jobs")
    receipt = installer.paths.path("/var/lib/torrent-guard-maintenance/setup.json")
    saved = json.loads(receipt.read_text()) if receipt.exists() else {"schema": 1, "original_jobs": []}
    if (not isinstance(saved, dict) or saved.get("schema") != 1
            or not isinstance(saved.get("original_jobs"), list)
            or any(not isinstance(job, dict) or not isinstance(job.get("uuid"), str)
                   for job in saved["original_jobs"])):
        raise InstallError("Invalid schedule recovery record; preserve it for review")
    coordinated, direct = [], []
    for job in jobs:
        command = job["command"].strip()
        if not any(program in command for program in (HELPER, "snapraid-guarded-sync", "snapraid-guarded-scrub")):
            continue
        tokens = shlex.split(command)
        while tokens and tokens[0].startswith("PATH="):
            tokens.pop(0)
        if tokens == [HELPER, "tick"]:
            coordinated.append(job)
        elif command in ("/usr/local/sbin/snapraid-guarded-sync", "/usr/local/sbin/snapraid-guarded-scrub"):
            direct.append(job)
        elif any(Path(token).name in ("snapraid-guarded-sync", "snapraid-guarded-scrub") for token in tokens):
            raise InstallError("A guarded SnapRAID job has custom arguments/wrapping; review it in the supplement before automatic replacement")
    if len(coordinated) > 1:
        raise InstallError("Duplicate coordinator jobs exist; review the duplicates before upgrading")
    if any(type(job.get("enable")) is not bool for job in direct + coordinated):
        raise InstallError("Unsupported OMV job enable state; no schedule was changed")
    defaults = coordinated[0] if coordinated else json.loads(call(
        "reading new-job defaults", ["omv-confdbadm", "read", "--defaults", MODEL]))
    if not isinstance(defaults, dict) or not isinstance(defaults.get("uuid"), str) or not defaults["uuid"]:
        raise InstallError("Could not read OMV's new-job identifier; no schedule was changed")
    if not coordinated and defaults["uuid"] in {job["uuid"] for job in jobs}:
        raise InstallError("OMV new-job defaults identify an existing job; no schedule was changed")
    desired = {**defaults, **SCHEDULE}
    if not coordinated:
        desired.update(comment="Torrent Guard coordinated SnapRAID maintenance", sendemail=True)
    originals = {job["uuid"]: job for job in saved["original_jobs"]}
    for job in direct:
        originals.setdefault(job["uuid"], job)
    saved.update(original_jobs=list(originals.values()),
                 coordinator_uuid=coordinated[0]["uuid"] if coordinated else None)
    # Save original states before the first write. Interrupted creation can be
    # recovered by finding the unique disabled coordinator on the next run.
    from maintenance import atomic_json
    atomic_json(receipt, saved, 0o600)
    current = coordinated[0] if coordinated else None
    if current is None:
        disabled = {**desired, "enable": False}
        call("creating a disabled coordinator job", ["omv-confdbadm", "update", MODEL, "-"], data=json.dumps(disabled))
        old_ids = {job["uuid"] for job in jobs}
        created = [job for job in read_jobs("verifying the new disabled coordinator")
                   if job["uuid"] not in old_ids and job["uuid"] != defaults["uuid"]
                   and all(job.get(key) == value for key, value in disabled.items() if key != "uuid")]
        if len(created) != 1:
            raise InstallError("Could not identify one new disabled OMV coordinator; direct jobs were left unchanged")
        current = created[0]
        desired["uuid"] = current["uuid"]
    saved["coordinator_uuid"] = desired["uuid"]
    atomic_json(receipt, saved, 0o600)
    for job in direct:
        if job["enable"]:
            call("disabling the direct guarded-script job " + job["uuid"],
                 ["omv-confdbadm", "update", MODEL, "-"], data=json.dumps({**job, "enable": False}))
    if desired != current:
        call("enabling the verified coordinator job", ["omv-confdbadm", "update", MODEL, "-"], data=json.dumps(desired))
    # Redeploy on rerun to finish interrupted migrations using OMV's generated
    # configuration. Never hand-edit its generated cron files.
    call("deploying OMV cron configuration", ["omv-salt", "deploy", "run", "cron"], stream=True)
    by_id = {job["uuid"]: job for job in read_jobs("verifying the deployed schedule")}
    if any(by_id.get(desired["uuid"], {}).get(key) != value for key, value in desired.items()) or any(by_id.get(job["uuid"], {}).get("enable") is not False for job in direct):
        raise InstallError("OMV schedule verification failed; rerun after inspecting OMV jobs")
    installer.output("OMV schedule is configured: daily 02:01 sync; Monday scrub plus automatic daily catch-up (>20% to <=5% by default); coordinated from 02:00 Eastern.")


def install_omv(installer: UpgradeInstaller, execute=run) -> None:
    """Generate/keep its key, enroll once using admin SSH, then activate scheduling."""
    installer.required(("ssh", "ssh-keygen", "ssh-keyscan", "omv-confdbadm", "omv-salt", "bash", "smartctl", "findmnt"))
    for script in ("/usr/local/sbin/snapraid-guarded-sync", "/usr/local/sbin/snapraid-guarded-scrub"):
        if not Path(script).is_file() or not os.access(script, os.X_OK):
            raise InstallError("The existing guarded SnapRAID script is required: " + script)
    installer.directory(installer.paths.path("/var/lib/torrent-guard-maintenance"), private=True)
    key = installer.key_pair()
    path = installer.paths.config
    if path.exists():
        installer.info(path, private=True)
        config = json.loads(path.read_text())
    else:
        host = installer.question("MEDIA_HOST", "Media Docker host IP/hostname: ").strip()
        port = installer.question("SSH_PORT", "Media Docker host SSH port [22]: ").strip() or "22"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", host) or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise InstallError("Invalid media host or SSH port")
        config = {"host": host, "user": USER, "port": int(port), "identity_file": str(installer.paths.key),
                  "known_hosts_file": str(installer.paths.hosts)}
    if (config.get("user") != USER or config.get("identity_file") != str(installer.paths.key)
            or config.get("known_hosts_file") != str(installer.paths.hosts)
            or not isinstance(config.get("host"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", config["host"])
            or type(config.get("port")) is not int or not 1 <= config["port"] <= 65535):
        raise InstallError("Custom SSH settings were preserved; review the supplement before changing them")
    installer.host_key(config, None)
    installer.json_file(path, config)

    def check():
        """Authenticate and require a session-matched read-only acknowledgement."""
        result = json.loads(execute(ssh_command(config, "check " + "0" * 32), timeout=60))
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("session") != "0" * 32 or type(result.get("active")) is not bool):
            raise ValueError("Remote maintenance check was not confirmed")
        return result

    try:
        result = check()
    except (InstallError, ValueError):
        admin = installer.question("ADMIN_USERNAME", "One-time administrator SSH username on the media host: ").strip()
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", admin):
            raise InstallError("Invalid administrator SSH username")
        admin_config = {**config, "user": admin}
        command = ssh_command(admin_config, "")[:-1]
        command = [value for value in command if value not in ("-oBatchMode=yes", "-oIdentitiesOnly=yes", "-oIdentityAgent=none")]
        identity = command.index("-i")
        del command[identity:identity + 2]  # Administrator's normal identity/password, not the maintenance key.
        # -tt lets SSH/sudo read administrator authentication directly from the
        # terminal. No password is captured by Python or saved in configuration.
        command[command.index("-T")] = "-tt"
        payload = (ROOT / "scripts/enroll_maintenance_key.py").read_text()
        command.append(shlex.join(["sudo", "-H", "python3", "-c", payload, key]))
        installer.output(
            "Authenticate to the media host once. SSH asks for that administrator account's "
            "password or key passphrase; sudo may ask for its sudo password on the media host. "
            "Enter these directly at the SSH/sudo prompts. Passwords are not saved."
        )
        execute(command, stream=True)
        result = check()
    if result["active"]:
        raise InstallError("Remote maintenance is active; rerun after it finishes")
    if "pushover" not in config:
        settings = prompt_pushover(installer.ask, getpass.getpass, installer.output)
        if settings:
            selected = installer.question("SNAPRAID_CONFIG", "SnapRAID configuration file [default used by snapraid status]: ").strip()
            if selected:
                if not Path(selected).is_absolute():
                    raise InstallError("SnapRAID configuration must be an absolute path")
                installer.info(Path(selected))
                config["snapraid_config"] = selected
        config["pushover"] = settings
        atomic_json(path, config, 0o600)
    try:
        pushover_keys(config["pushover"])
    except (ValueError, AttributeError) as exc:
        raise InstallError("Invalid Pushover configuration; check the private OMV configuration") from exc
    install_guarded_adapter(installer)
    installer.helper()
    # This is our own cron file, never OMV's generated schedule. Its independent
    # lock lets checks continue while tick is paused or executing a long job.
    installer.directory(installer.paths.path(str(WATCH_DIRECTORY)), private=True)
    watch_lock = installer.paths.path(str(WATCH_DIRECTORY)) / 'lock'
    if not installer.existing(watch_lock):
        installer.file(watch_lock, b'', 0o600, private=True)
    else:
        installer.info(watch_lock, private=True)
    installer.file(installer.paths.path(WATCH_CRON), WATCH_SCHEDULE.encode(), 0o644)
    configure_omv_schedule(installer, execute)
    installer.output("OMV installation/upgrade complete. No manual key copying or return installer pass is needed.")


def main() -> int:
    """Run one supported role; default upgrades fast-forward the existing checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("media", "omv"))
    parser.add_argument("--no-update", action="store_true", help="use this checkout revision without fetching (offline/testing)")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo/root")
    try:
        with idle_setup(args.role):
            if not args.no_update and update_checkout(ROOT):
                # Release the setup lock before restarting with the new modules.
                rerun = True
            else:
                rerun = False
                installer = UpgradeInstaller()
                if args.role == "media":
                    installer.required(("docker", "ssh-keygen", "sshd", "visudo", "useradd", "getent"))
                    run(["docker", "compose", "version"])
                    state = AppInstall(ROOT, run).deploy()
                    installer.docker(None, str(state), prepare_key=True)
                    installer.output("Media host installation/upgrade complete. Next run the installer on OMV.")
                else:
                    install_omv(installer)
        if rerun:
            os.execv(sys.executable, [sys.executable, str(ROOT / "install.py"), args.role, "--no-update"])
    except (InstallError, OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        print("Setup stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0
