"""Install/upgrade the Guard container while preserving host data and settings."""
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET

from maintenance import pushover_keys
from scripts.install_prompts import ask_with_help
from scripts.maintenance_install import InstallError


PROFILE_FILES = ("compose.yaml", "compose.cleanup.yaml", "compose.orphans.yaml")


def dotenv_value(value: str) -> str:
    """Quote new dotenv values without letting Compose expand password dollars."""
    return json.dumps(value, ensure_ascii=False).replace("$", "$$")


def read_values(text: str) -> dict:
    """Read simple bookkeeping/default values without evaluating shell text."""
    result = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$", line)
        if match:
            value = match[2].strip()
            if value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value).replace("$$", "$")
                except ValueError:
                    value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            result[match[1]] = value
    return result


def save_env(path: Path, content: str, expected: str | None, owner: tuple[int, int]) -> None:
    """Atomically persist setup changes without truncating saved credentials."""
    descriptor, temporary = tempfile.mkstemp(prefix=".tg-env-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, *owner)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is None:
            os.link(temporary, path)
        else:
            if path.is_symlink() or path.read_text() != expected:
                raise InstallError("The .env changed during setup; preserved for review")
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prompt_pushover(ask, secret, output) -> dict:
    """Offer optional notifications and read both credentials with hidden input."""
    enabled = ask_with_help("PUSHOVER_ENABLE", "Enable Pushover maintenance notifications [y/N]: ",
                            read=ask, output=output).strip().lower() in {"y", "yes"}
    if not enabled:
        return {}
    settings = {
        "app_token": ask_with_help("PUSHOVER_APP_TOKEN", "Pushover application token: ", read=secret, output=output).strip(),
        "user_key": ask_with_help("PUSHOVER_USER_KEY", "Pushover user key: ", read=secret, output=output).strip(),
    }
    try:
        token, user = pushover_keys(settings)
        if not token or not user:
            raise ValueError("Both Pushover credentials are required when enabled")
    except ValueError as exc:
        raise InstallError(str(exc)) from exc
    return settings


class AppInstall:
    """Prepare existing media infrastructure and deploy only Torrent Guard."""

    def __init__(self, root: Path, run, ask=input, secret=getpass.getpass, output=print):
        self.root, self.run, self.ask, self.secret, self.output = root, run, ask, secret, output

    def inspect(self, name: str, optional: bool = False) -> dict | None:
        """Capture Docker metadata privately; never print Config.Env or API keys."""
        if optional:
            names = self.run(["docker", "container", "ls", "-a", "--format", "{{.Names}}"])
            if name not in names.splitlines():
                return None
        rows = json.loads(self.run(["docker", "container", "inspect", name]))
        if not isinstance(rows, list) or len(rows) != 1:
            raise InstallError("Could not inspect container " + name)
        return rows[0]

    @staticmethod
    def missing(value: str | None) -> bool:
        """Recognize only absent/default placeholders, not a real saved setting."""
        return value is None or value in ("", "REPLACE_ME") or value.startswith("/EDIT/")

    def prompt(self, label: str, default: str = "", *, setting: str, hidden: bool = False) -> str:
        """Explain where to find missing settings and preserve hidden input."""
        if hidden:
            value = ask_with_help(setting, label + ": ", read=self.secret, output=self.output)
        else:
            value = ask_with_help(
                setting, label + (f" [{default}]" if default else "") + ": ",
                read=self.ask, output=self.output,
            ).strip()
        value = value or default
        if not value:
            raise InstallError(label + " is required")
        return value

    def defaults(self, container: dict | None) -> dict:
        """Recover allowlisted runtime settings when cloning into a new directory."""
        result = read_values((self.root / ".env.example").read_text())
        if container:
            for field in container.get("Config", {}).get("Env", []):
                key, separator, value = field.partition("=")
                if separator and (key in result or key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")):
                    result[key] = value
            mounts = [row for row in container.get("Mounts", []) if row.get("Destination") == "/state"]
            if len(mounts) != 1 or mounts[0].get("Type") != "bind":
                raise InstallError("Existing Guard state must remain on its current bind mount")
            result["STATE_HOST_PATH"] = mounts[0]["Source"]
            identity = container.get("Config", {}).get("User", "")
            if re.fullmatch(r"[0-9]+:[0-9]+", identity):
                result["GUARD_UID"], result["GUARD_GID"] = identity.split(":")
            ports = container.get("HostConfig", {}).get("PortBindings", {})
            status_port = result.get("STATUS_PORT", "8080") + "/tcp"
            bindings = ports.get(status_port) or []
            if len(bindings) == 1:
                result["STATUS_BIND_ADDRESS"] = bindings[0]["HostIp"] or "0.0.0.0"
                result["STATUS_HOST_PORT"] = bindings[0]["HostPort"]
        return result

    def arr_defaults(self, name: str, container: dict) -> tuple[str | None, str]:
        """Read the existing Arr API key/URL base without modifying its config."""
        key, base = None, ""
        mounts = [row for row in container.get("Mounts", []) if row.get("Destination") == "/config" and row.get("Type") == "bind"]
        if len(mounts) == 1:
            config = Path(mounts[0]["Source"]) / "config.xml"
            if config.is_file() and config.stat().st_size < 1024 * 1024:
                try:
                    document = ET.fromstring(config.read_bytes())
                    key, base = document.findtext("ApiKey"), document.findtext("UrlBase") or ""
                except ET.ParseError:
                    pass  # Ask rather than infer credentials from malformed XML.
        port = "8989" if name == "sonarr" else "7878"
        published = container.get("NetworkSettings", {}).get("Ports", {}).get(port + "/tcp") or []
        if published:
            port = published[0]["HostPort"]
        return key, "http://host.docker.internal:" + port + ("/" + base.strip("/") if base else "")

    def configure(self, running: dict | None) -> dict:
        """Reuse an existing .env byte-for-byte unless filling missing values."""
        path = self.root / ".env"
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise InstallError("The checkout .env must be a regular file")
        existing = path.read_text() if path.exists() else None
        values = self.defaults(running)
        saved = read_values(existing) if existing is not None else {}
        values.update(saved)
        changes = {}
        keys = ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")
        runtime_keys = {field.partition("=")[0] for field in (running or {}).get("Config", {}).get("Env", [])}
        if not all(key in saved or key in runtime_keys for key in keys):
            settings = prompt_pushover(self.ask, self.secret, self.output)
            for key, name in zip(keys, ("app_token", "user_key")):
                # Preserve an already supplied credential while filling its pair.
                if self.missing(values.get(key)):
                    values[key] = settings.get(name, "")
                    changes[key] = values[key]
        try:
            pushover_keys({"app_token": values.get(keys[0], ""), "user_key": values.get(keys[1], "")})
        except ValueError as exc:
            raise InstallError(str(exc)) from exc
        if self.missing(values.get("QBIT_PASSWORD")):
            values["QBIT_URL"] = self.prompt("qBittorrent API URL reachable from Guard", values.get("QBIT_URL", ""), setting="QBIT_URL")
            values["QBIT_USERNAME"] = self.prompt("qBittorrent username", values.get("QBIT_USERNAME", "admin"), setting="QBIT_USERNAME")
            values["QBIT_PASSWORD"] = self.prompt("qBittorrent password", setting="QBIT_PASSWORD", hidden=True)
            changes.update({key: values[key] for key in ("QBIT_URL", "QBIT_USERNAME", "QBIT_PASSWORD")})
        for app in ("SONARR", "RADARR"):
            if self.missing(values.get(app + "_API_KEY")):
                container = self.inspect(app.lower())
                key, url = self.arr_defaults(app.lower(), container)
                values[app + "_URL"] = url if key else self.prompt(app.title() + " API URL", values.get(app + "_URL", url), setting=app + "_URL")
                values[app + "_API_KEY"] = key or self.prompt(app.title() + " API key", setting=app + "_API_KEY", hidden=True)
                values[app + "_CATEGORY"] = self.prompt(app.title() + " qBittorrent category", values.get(app + "_CATEGORY", ""), setting=app + "_CATEGORY")
                changes.update({app + suffix: values[app + suffix] for suffix in ("_URL", "_API_KEY", "_CATEGORY")})
        if self.missing(values.get("DOWNLOADS_HOST_PATH")):
            values["DOWNLOADS_HOST_PATH"] = self.prompt("Host download folder containing complete/ and incomplete/", setting="DOWNLOADS_HOST_PATH")
            values["QBIT_DOWNLOAD_ROOT"] = self.prompt("Download root seen inside qBittorrent", values["QBIT_DOWNLOAD_ROOT"], setting="QBIT_DOWNLOAD_ROOT")
            values["QBIT_COMPLETE_DIR"] = self.prompt("Completed directory inside qBittorrent", values["QBIT_DOWNLOAD_ROOT"] + "/complete", setting="QBIT_COMPLETE_DIR")
            values["QBIT_INCOMPLETE_DIR"] = self.prompt("Incomplete directory inside qBittorrent", values["QBIT_DOWNLOAD_ROOT"] + "/incomplete", setting="QBIT_INCOMPLETE_DIR")
            changes.update({key: values[key] for key in ("DOWNLOADS_HOST_PATH", "QBIT_DOWNLOAD_ROOT", "QBIT_COMPLETE_DIR", "QBIT_INCOMPLETE_DIR")})
        if self.missing(values.get("CLEANUP_COMPLETE_HOST_PATH")):
            relative = PurePosixPath(values["QBIT_COMPLETE_DIR"]).relative_to(values["QBIT_DOWNLOAD_ROOT"])
            values["CLEANUP_COMPLETE_HOST_PATH"] = str(Path(values["DOWNLOADS_HOST_PATH"]) / str(relative))
            changes["CLEANUP_COMPLETE_HOST_PATH"] = values["CLEANUP_COMPLETE_HOST_PATH"]
        if existing is None and running is None:
            live = ask_with_help("ENABLE_REMOVAL", "Enable real torrent removal now? Otherwise start in dry run [y/N]: ", read=self.ask, output=self.output).strip().lower() in {"y", "yes"}
            cleanup = ask_with_help("ENABLE_CLEANUP", "Enable automatic download-folder leftover/orphan cleanup [y/N]: ", read=self.ask, output=self.output).strip().lower() in {"y", "yes"}
            values.update(DRY_RUN="false" if live else "true", CLEANUP_LEFTOVERS=str(cleanup).lower(), ORPHAN_CLEANUP=str(cleanup).lower())
        if running:
            state = next(row["Source"] for row in running["Mounts"] if row.get("Destination") == "/state")
            if "STATE_HOST_PATH" in saved and not os.path.samefile(saved["STATE_HOST_PATH"], state):
                raise InstallError("Saved STATE_HOST_PATH differs from the existing Guard state; no deployment performed")
            if "STATE_HOST_PATH" not in saved:
                values["STATE_HOST_PATH"] = state
                changes["STATE_HOST_PATH"] = state
        if existing is None:
            content = "# Configured by Torrent Guard's installer. Keep this file private.\n" + "".join(key + "=" + dotenv_value(value) + "\n" for key, value in values.items())
            owner = self.root.stat()
            save_env(path, content, None, (owner.st_uid, owner.st_gid))
        elif changes:
            # Replace only keys that were absent/placeholders; preserve all other
            # lines, comments, credentials and settings exactly as supplied.
            lines = existing.splitlines(keepends=True)
            remaining = dict(changes)
            for index, line in enumerate(lines):
                match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line)
                if match and match[1] in remaining:
                    lines[index] = match[1] + "=" + dotenv_value(remaining.pop(match[1])) + "\n"
            content = "".join(lines)
            content += "\n" + "".join(key + "=" + dotenv_value(value) + "\n" for key, value in remaining.items())
            owner = path.stat()
            save_env(path, content, existing, (owner.st_uid, owner.st_gid))
        os.chmod(path, 0o600)
        return values

    def deploy(self) -> Path:
        """Build first, then recreate only Guard with its existing cleanup profiles."""
        running = self.inspect("torrent-guard", optional=True)
        values = self.configure(running)
        for key in ("GUARD_UID", "GUARD_GID"):
            if not values.get(key, "1000").isdigit() or int(values.get(key, "1000")) <= 0:
                raise InstallError("Guard must use positive non-root numeric GUARD_UID/GUARD_GID")
        selected = values.get("COMPOSE_FILE", "")
        if selected:
            files = selected.split(":")
            if any(name not in PROFILE_FILES for name in files) or "compose.yaml" not in files:
                raise InstallError("Custom COMPOSE_FILE preserved; review its overrides in INSTALL_SUPPLEMENT.md")
        elif running:
            configured = running.get("Config", {}).get("Labels", {}).get("com.docker.compose.project.config_files", "")
            files = [Path(name).name for name in configured.split(",") if name]
            if any(name not in PROFILE_FILES for name in files):
                raise InstallError("Custom running Compose overrides preserved; see INSTALL_SUPPLEMENT.md before upgrading")
            if not files:
                files = ["compose.yaml"] + [name for key, name in (("CLEANUP_LEFTOVERS", "compose.cleanup.yaml"), ("ORPHAN_CLEANUP", "compose.orphans.yaml")) if values.get(key) == "true"]
        else:
            files = ["compose.yaml"] + [name for key, name in (("CLEANUP_LEFTOVERS", "compose.cleanup.yaml"), ("ORPHAN_CLEANUP", "compose.orphans.yaml")) if values.get(key) == "true"]
        state = Path(values.get("STATE_HOST_PATH", "/config/torrent-guard"))
        if not state.is_absolute() or len(state.parts) < 3 or state.is_symlink():
            raise InstallError("State bind must be a dedicated absolute directory")
        if not state.exists():
            state.mkdir(parents=True, mode=0o750)
            os.chown(state, int(values.get("GUARD_UID", "1000")), int(values.get("GUARD_GID", "1000")))
        if not Path(values["DOWNLOADS_HOST_PATH"]).is_dir():
            raise InstallError("The download mount is unavailable; no replacement directory was created")
        args = ["docker", "compose", "--project-directory", str(self.root)]
        for name in files:
            args.extend(["-f", str(self.root / name)])
        configuration = json.loads(self.run(args + ["config", "--format", "json"]))
        service = configuration["services"]["torrent-guard"]
        # Shell variables have higher Compose precedence than .env. Refuse a
        # hidden state/identity override instead of starting on a different DB.
        user = service.get("user", values.get("GUARD_UID", "1000") + ":" + values.get("GUARD_GID", "1000"))
        if user != values.get("GUARD_UID", "1000") + ":" + values.get("GUARD_GID", "1000"):
            raise InstallError("Compose runtime IDs differ from saved settings; remove conflicting shell overrides")
        for volume in service.get("volumes", []):
            if volume.get("type") == "bind" and not Path(volume["source"]).is_dir():
                raise InstallError("A configured bind folder is unavailable; mount it before setup: " + volume["source"])
            if volume.get("target") == "/state" and (volume.get("type") != "bind" or not os.path.samefile(volume["source"], state)):
                raise InstallError("Compose state differs from the saved journal bind; remove conflicting shell overrides")
        environment = service["environment"]
        self.run([sys.executable, "-c", "import sys;sys.path.insert(0,sys.argv[1]);from guard import Config;Config.from_env()", str(self.root)],
                 env={**os.environ, **{key: str(value) for key, value in environment.items() if value is not None}})
        # Test only a newly-created private probe, under Guard's actual IDs.
        # No existing journal/log ownership is changed during an upgrade.
        probe = ("import os,sys,tempfile;os.setgroups([]);os.setgid(int(sys.argv[2]));"
                 "os.setuid(int(sys.argv[1]));fd,p=tempfile.mkstemp(prefix='.tg-install-',dir=sys.argv[3]);"
                 "os.write(fd,b'permission probe');os.fsync(fd);os.close(fd);os.unlink(p)")
        self.run([sys.executable, "-c", probe, values.get("GUARD_UID", "1000"), values.get("GUARD_GID", "1000"), str(state)])
        # These inherited mount selections must remain explicit on subsequent
        # upgrades. Add bookkeeping only; never rewrite saved credentials.
        if not selected:
            env_path = self.root / ".env"
            old = env_path.read_text()
            owner = env_path.stat()
            save_env(env_path, old + "\nCOMPOSE_FILE=" + ":".join(files) + "\n", old, (owner.st_uid, owner.st_gid))
        self.output("Building and upgrading Torrent Guard; existing state and credentials are preserved.")
        self.run(args + ["build", "torrent-guard"], stream=True)
        self.run(args + ["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120", "torrent-guard"], stream=True)
        self.output("Torrent Guard deployed. Its authenticated status proxy uses the existing settings.")
        return state
