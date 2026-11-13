"""Idempotent installer tests on temporary hosts; no real users/services changed."""
import base64
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.maintenance_install import FORCED, HELPER, USER, InstallError, Installer, Paths, PendingConnection, fingerprint, public_key
from maintenance import ssh_command


def sample_key(byte: int = 1) -> str:
    """Return structurally valid public-only key fixtures, not private credentials."""
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


class InstallerFixture(unittest.TestCase):
    """Fake privileged commands; use real temporary files and ssh-keygen pairs."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = Paths(self.root)
        self.output, self.ask = Mock(), Mock(side_effect=AssertionError("Unexpected question"))
        self.installer = Installer(self.paths, self.ask, self.output)
        self.created_user = False
        self.password_marker = "!"
        self.connected = True
        self.commands = []
        self.ssh_policy = "pubkeyauthentication yes\nauthorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\nforcecommand none\n"
        state = self.root / "guard-state"
        state.mkdir()
        self.rows = [{"Name": "/sonarr"}, {"Name": "/radarr"},
                     {"Name": "/torrent-guard", "Mounts": [{"Type": "bind", "Destination": "/state", "Source": str(state), "RW": True}]}]
        real_lstat = Path.lstat

        def fixture_owner(path):
            details = real_lstat(path)
            # Test host files are created by the unprivileged test runner.
            # Emulate root ownership without weakening production checks.
            return SimpleNamespace(st_uid=0, st_mode=details.st_mode)

        context = patch("scripts.maintenance_install.Path.lstat", fixture_owner)
        context.start()
        self.addCleanup(context.stop)
        self.installer.required = Mock()
        self.real_run = self.installer.run
        self.installer.run = self.command
        for target, replacement in (("scripts.maintenance_install.pwd.getpwnam", self.account),
                                    ("scripts.maintenance_install.grp.getgrall", lambda: [])):
            context = patch(target, replacement)
            context.start()
            self.addCleanup(context.stop)
        host_key = self.paths.path("/etc/ssh/ssh_host_ed25519_key.pub")
        host_key.parent.mkdir(parents=True)
        host_key.write_text(sample_key(7) + " fixture-host\n")
        # Normal administrative connections must remain completely untouched.
        normal = self.paths.path("/root/.ssh")
        normal.mkdir(parents=True, mode=0o700)
        for name, data in (("config", "Host normal\n  ControlPath /tmp/normal-ssh\n"),
                           ("authorized_keys", "normal administrator key\n"),
                           ("known_hosts", "normal host pins\n"), ("id_ed25519", "normal private identity\n")):
            (normal / name).write_text(data)
        self.normal = self.snapshot(normal)

    def snapshot(self, root):
        return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()}

    def account(self, name):
        self.assertEqual(name, USER)
        if not self.created_user:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(self.paths.path("/var/lib/guard-maintenance-ssh")), pw_shell="/bin/sh")

    def command(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "ssh-keygen":
            return self.real_run(command, **kwargs)
        if command[0] == "ssh-keyscan":
            return "docker.test " + sample_key(7) + "\n"
        if command[0] == "ssh":
            if not self.connected:
                raise InstallError("Fixture connection not configured yet")
            self.assertEqual(command[-1], "check " + "0" * 32)
            self.assertIn("-oControlPath=none", command)
            return json.dumps({"ok": True, "active": False, "session": "0" * 32})
        if command[0] == "docker":
            self.assertEqual(command[1:3], ["container", "inspect"])
            return json.dumps(self.rows)
        if command[0] == "useradd":
            self.created_user = True
            self.password_marker = command[command.index("--password") + 1]
            return ""
        if command[0] == "getent":
            return USER + ":" + self.password_marker + ":0:0:99999:7:::\n"
        if command[0] == "sshd":
            self.assertEqual(command[1], "-T")
            return self.ssh_policy
        if command[0] == "visudo":
            self.assertEqual(command[1], "-cf")
            return "parsed OK\n"
        self.fail("Unexpected command " + str(command))

    def install_omv(self):
        self.installer.omv("docker.test", None, fingerprint(sample_key(7)))

class InstallerTests(InstallerFixture):
    """Exercise creation and read-only repeat verification."""

    def test_omv_second_run_has_no_writes_prompts_or_key_regeneration(self):
        self.install_omv()
        before = self.snapshot(self.root)
        self.commands.clear()
        self.installer.omv(None, None, None)
        self.assertEqual(self.snapshot(self.root), before)
        self.ask.assert_not_called()
        self.assertFalse(any(command[0] == "ssh-keyscan" or "-q" in command for command in self.commands))
        for name, value in self.normal.items():
            self.assertEqual(self.snapshot(self.paths.key.parent)[name], value)

    def test_pending_bootstrap_preserves_key_for_second_omv_pass(self):
        self.connected = False
        with self.assertRaises(PendingConnection):
            self.install_omv()
        before = self.snapshot(self.root)
        self.connected = True
        self.installer.omv(None, None, None)
        self.assertEqual(self.snapshot(self.root), before)
        self.ask.assert_not_called()

    def test_docker_second_run_has_no_writes_account_changes_or_questions(self):
        self.installer.docker(sample_key(), None)
        before = self.snapshot(self.root)
        self.commands.clear()
        self.installer.docker(None, None)
        self.assertEqual(self.snapshot(self.root), before)
        self.ask.assert_not_called()
        self.assertFalse(any(command[0] == "useradd" for command in self.commands))
        self.assertEqual(self.snapshot(self.paths.path("/root/.ssh")), self.normal)
        self.assertFalse(any(command[0] in ("systemctl", "service", "usermod", "chown", "chmod") for command in self.commands))

    def test_existing_json_format_and_key_comments_are_preserved(self):
        self.install_omv()
        config = json.loads(self.paths.config.read_text())
        self.paths.config.write_text(json.dumps(config, separators=(",", ":")))
        before = self.snapshot(self.root)
        self.installer.omv(None, None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_helper_mismatch_is_reported_without_overwrite(self):
        self.install_omv()
        helper = self.paths.path(HELPER)
        helper.write_text("existing administrator-managed helper\n")
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "differs"):
            self.installer.omv(None, None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_existing_wrong_permissions_are_reported_without_chmod(self):
        self.install_omv()
        self.paths.key.chmod(0o644)
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "permissions"):
            self.installer.omv(None, None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_different_existing_host_port_and_key_are_preserved(self):
        self.install_omv()
        before = self.snapshot(self.root)
        for host, port in (("other.test", None), (None, 2200)):
            with self.assertRaisesRegex(InstallError, "differs"):
                self.installer.omv(host, port, None)
        self.assertEqual(self.snapshot(self.root), before)
        self.installer.docker(sample_key(), None)
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "differs"):
            self.installer.docker(sample_key(3), None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_untrusted_host_fingerprint_is_never_installed(self):
        with self.assertRaisesRegex(InstallError, "fingerprint mismatch"):
            self.installer.omv("docker.test", None, fingerprint(sample_key(4)))
        self.assertFalse(self.paths.hosts.exists())
        self.assertEqual({name: self.snapshot(self.paths.key.parent)[name] for name in self.normal}, self.normal)

    def test_existing_pin_mismatch_is_not_replaced(self):
        self.install_omv()
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "fingerprint mismatch"):
            self.installer.omv(None, None, fingerprint(sample_key(2)))
        self.assertEqual(self.snapshot(self.root), before)

    def test_normal_account_authorized_keys_are_not_reused(self):
        authorized = self.paths.path("/var/lib/guard-maintenance-ssh/.ssh/authorized_keys")
        authorized.parent.mkdir(parents=True)
        authorized.write_text(sample_key() + " unconfined-key\n")
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "restricted"):
            self.installer.docker(None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_restricted_key_with_existing_from_option_is_preserved(self):
        self.installer.docker(sample_key(), None)
        authorized = self.paths.path("/var/lib/guard-maintenance-ssh/.ssh/authorized_keys")
        authorized.write_text('from="192.0.2.10",' + authorized.read_text())
        before = self.snapshot(self.root)
        self.installer.docker(None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_symlinks_and_public_private_key_mismatch_are_not_repaired(self):
        self.install_omv()
        pub = Path(str(self.paths.key) + ".pub")
        pub.write_text(sample_key(2))
        with self.assertRaisesRegex(InstallError, "mismatch"):
            self.installer.omv(None, None, None)
        helper = self.paths.path(HELPER)
        helper.unlink()
        helper.symlink_to(self.paths.path("/root/.ssh/id_ed25519"))
        with self.assertRaisesRegex(InstallError, "ownership/type"):
            self.installer.helper()
        self.assertTrue(helper.is_symlink())

    def test_only_needed_first_time_questions_are_asked(self):
        self.ask.side_effect = ["docker.test", fingerprint(sample_key(7))]
        self.installer.omv(None, None, None)
        self.assertEqual(self.ask.call_count, 2)
        self.ask.reset_mock(side_effect=True)
        self.ask.side_effect = [sample_key()]
        self.installer.docker(None, None)
        self.assertEqual(self.ask.call_count, 1)

    def test_password_and_privileged_group_mismatches_do_not_modify_account(self):
        self.installer.docker(sample_key(), None)
        before = self.snapshot(self.root)
        with patch("scripts.maintenance_install.grp.getgrall", return_value=[SimpleNamespace(gr_name="docker", gr_mem=[USER], gr_gid=999)]):
            with self.assertRaisesRegex(InstallError, "privileged groups"):
                self.installer.docker(None, None)
        self.assertEqual(self.snapshot(self.root), before)
        original = self.installer.run

        def unlocked(command, **kwargs):
            return USER + ":unlocked-password-marker:\n" if command[:2] == ["getent", "shadow"] else original(command, **kwargs)

        self.installer.run = unlocked
        with self.assertRaisesRegex(InstallError, "password is not locked"):
            self.installer.docker(None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_existing_sshd_policy_is_never_rewritten_or_restarted(self):
        self.ssh_policy = "pubkeyauthentication no\nauthorizedkeysfile /other/path\n"
        with self.assertRaisesRegex(InstallError, "sshd policy"):
            self.installer.docker(sample_key(), None)
        self.assertFalse(self.paths.path("/etc/ssh/sshd_config").exists())
        self.assertFalse(any(command[0] in ("service", "systemctl") for command in self.commands))

    def test_mount_mismatch_never_changes_mounts_or_starts_containers(self):
        self.rows[-1]["Mounts"][0]["Type"] = "volume"
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "bind"):
            self.installer.docker(sample_key(), None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_non_pam_ssh_uses_unusable_password_without_changing_sshd(self):
        self.ssh_policy += "usepam no\n"
        self.installer.docker(sample_key(), None)
        self.assertEqual(self.password_marker, "*")
        self.assertFalse(self.paths.path("/etc/ssh/sshd_config").exists())
        self.password_marker = "!"
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "non-PAM"):
            self.installer.docker(None, None)
        self.assertEqual(self.snapshot(self.root), before)

    def test_invalid_explicit_host_or_port_is_rejected_without_writes_or_questions(self):
        before = self.snapshot(self.root)
        for host, port in (("", None), ("-oProxyCommand=id", None), ("docker.test", 0), ("docker.test", 65536)):
            with self.assertRaises(InstallError):
                self.installer.omv(host, port, None)
        self.assertEqual(self.snapshot(self.root), before)
        self.ask.assert_not_called()


class KeyAndConnectionTests(unittest.TestCase):
    """Reject unsafe key input and isolate maintenance from normal SSH clients."""

    def test_public_key_parsing_never_accepts_private_material_or_multiple_keys(self):
        for value in ("-----BEGIN OPENSSH PRIVATE KEY-----", "ssh-ed25519 invalid", sample_key() + "\n" + sample_key(2), "ssh-rsa AAAA"):
            with self.assertRaises(InstallError):
                public_key(value)
        self.assertEqual(public_key(sample_key() + " harmless comment"), sample_key())

    def test_runtime_and_installer_ssh_are_separate_from_administrator_sessions(self):
        config = {"identity_file": "/dedicated/key", "known_hosts_file": "/dedicated/pins", "host": "docker.test"}
        command = ssh_command(config, "check " + "0" * 32)
        self.assertEqual(command[:4], ["ssh", "-F", "/dev/null", "-T"])
        for flag in ("-oControlPath=none", "-oControlMaster=no", "-oIdentityAgent=none", "-oUpdateHostKeys=no",
                     "-oGlobalKnownHostsFile=/dev/null", "-oStrictHostKeyChecking=yes", "-oForwardAgent=no"):
            self.assertIn(flag, command)
        self.assertIn("-oUserKnownHostsFile=/dedicated/pins", command)

    def test_authorized_key_prefix_matches_documented_forced_command(self):
        self.assertIn('agent \\"$SSH_ORIGINAL_COMMAND\\""', FORCED)
        self.assertTrue(FORCED.startswith('restrict,command="'))


if __name__ == "__main__":
    unittest.main()
