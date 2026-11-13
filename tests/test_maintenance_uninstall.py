"""Remove current and manual maintenance setup without touching unrelated data."""
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.maintenance_install import HELPER, USER, InstallError
from scripts.maintenance_uninstall import Uninstaller
import test_maintenance_install as installer_tests
from test_maintenance_install import sample_key


class UninstallTests(installer_tests.InstallerFixture):
    """Reuse isolated host/account fixtures, never real system accounts."""

    def setUp(self):
        super().setUp()
        self.uninstaller = Uninstaller(self.paths, output=Mock())
        self.uninstaller.processes = Mock(return_value=[])
        self.uninstaller.run = self.remove_command
        self.group_exists = False
        self.removal_commands = []
        context = patch("scripts.maintenance_uninstall.grp.getgrnam", side_effect=self.get_group)
        context.start()
        self.addCleanup(context.stop)
        context = patch("scripts.maintenance_uninstall.pwd.getpwall", return_value=[])
        context.start()
        self.addCleanup(context.stop)

    def get_group(self, name):
        if not self.group_exists:
            raise KeyError(name)
        return SimpleNamespace(gr_name=USER, gr_gid=1001, gr_mem=[])

    def remove_command(self, command, **kwargs):
        self.removal_commands.append(command)
        if command == ["userdel", USER]:
            self.created_user = False
        elif command == ["groupdel", USER]:
            self.group_exists = False
        else:
            self.fail("Unexpected removal command " + str(command))
        return ""

    def apply(self, role):
        marker, journals = self.uninstaller.plan(role)
        with self.uninstaller.locks(journals):
            self.uninstaller.remove(role, marker, journals)

    def inactive_journal(self, role):
        private = self.paths.path("/var/lib/torrent-guard-maintenance" + ("-agent" if role == "docker" else ""))
        private.mkdir(parents=True, exist_ok=True)
        (private / "state.json").write_text(json.dumps({"schema": 1, "active": False}))
        (private / "lock").touch()
        return private

    def test_omv_uninstall_then_fresh_install_has_a_new_key(self):
        self.install_omv()
        original = self.paths.key.read_bytes()
        self.inactive_journal("omv")
        sync = self.paths.path("/usr/local/sbin/snapraid-guarded-sync")
        sync.write_text("existing guarded script")
        self.apply("omv")
        self.assertFalse(self.paths.key.exists())
        self.assertFalse(self.paths.hosts.exists())
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.path(HELPER).exists())
        self.assertEqual(sync.read_text(), "existing guarded script")
        self.assertEqual(self.snapshot(self.paths.path("/root/.ssh")), self.normal)
        self.apply("omv")
        self.install_omv()
        self.assertNotEqual(self.paths.key.read_bytes(), original)

    def test_docker_uninstall_keeps_guard_database_and_all_media(self):
        self.installer.docker(sample_key(), None)
        self.group_exists = True
        private = self.inactive_journal("docker")
        state = self.root / "guard-state"
        (state / "torrent-guard.sqlite3").write_bytes(b"PRESERVE DATABASE")
        (state / "torrent-guard.log").write_text("PRESERVE AUDIT")
        (state / "maintenance.json").write_text(json.dumps({"schema": 1, "active": False}))
        self.apply("docker")
        self.assertFalse(self.created_user)
        self.assertFalse(self.group_exists)
        self.assertFalse(private.exists())
        self.assertFalse((state / "maintenance.json").exists())
        self.assertEqual((state / "torrent-guard.sqlite3").read_bytes(), b"PRESERVE DATABASE")
        self.assertEqual((state / "torrent-guard.log").read_text(), "PRESERVE AUDIT")
        self.assertEqual(self.snapshot(self.paths.path("/root/.ssh")), self.normal)
        self.apply("docker")
        self.installer.docker(sample_key(2), None)
        self.assertTrue(self.created_user)

    def test_manual_v21_partial_account_home_and_sudo_setup_are_removed(self):
        # No new-installer receipt or source hash is needed.
        home = self.paths.path("/var/lib/guard-maintenance-ssh")
        (home / ".ssh").mkdir(parents=True)
        for name in (".bashrc", ".profile", ".bash_logout"):
            (home / name).write_text("legacy useradd skeleton")
        (home / ".ssh/authorized_keys").write_text(sample_key() + " manual partial key\n")
        helper = self.paths.path(HELPER)
        helper.parent.mkdir(parents=True)
        helper.write_text("older manually installed helper")
        sudo = self.paths.path("/etc/sudoers.d/torrent-guard-maintenance")
        sudo.parent.mkdir(parents=True)
        sudo.write_text(USER + " ALL=(root) NOPASSWD: " + HELPER + " agent *\n")
        self.created_user = True
        self.group_exists = True
        self.apply("docker")
        self.assertFalse(home.exists())
        self.assertFalse(helper.exists())
        self.assertFalse(sudo.exists())
        self.assertFalse(self.created_user)
        self.installer.docker(sample_key(), None)

    def test_active_or_corrupt_state_blocks_every_removal(self):
        self.install_omv()
        private = self.inactive_journal("omv")
        for value in ('{"schema":1,"active":true}', '{', '{"schema":1,"active":0}'):
            (private / "state.json").write_text(value)
            before = self.snapshot(self.root)
            with self.assertRaises(InstallError):
                self.uninstaller.plan("omv")
            self.assertEqual(self.snapshot(self.root), before)

    def test_live_process_or_enabled_omv_job_blocks_removal(self):
        self.install_omv()
        self.uninstaller.processes.return_value = ["123"]
        with self.assertRaisesRegex(InstallError, "process is running"):
            self.uninstaller.plan("omv")
        self.uninstaller.processes.return_value = []
        cron = self.paths.path("/etc/cron.d/openmediavault-userdefined")
        cron.parent.mkdir(parents=True)
        cron.write_text("* * * * * root /usr/local/sbin/torrent-guard-maintenance tick\n")
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "Disable"):
            self.uninstaller.plan("omv")
        self.assertEqual(self.snapshot(self.root), before)
        cron.write_text("# * * * * * root /usr/local/sbin/torrent-guard-maintenance tick\n")
        self.apply("omv")
        self.assertTrue(cron.exists())

    def test_held_lock_and_changed_target_block_removal(self):
        self.install_omv()
        private = self.inactive_journal("omv")
        marker, journals = self.uninstaller.plan("omv")
        with (private / "lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(InstallError, "busy"):
                with self.uninstaller.locks(journals):
                    pass
        helper = self.paths.path(HELPER)
        helper.unlink()
        helper.write_text("replaced file")
        helper.chmod(0o700)
        with self.assertRaisesRegex(InstallError, "changed"):
            self.uninstaller.remove("omv", marker, journals)
        self.assertTrue(self.paths.key.exists())

    def test_alpine_cron_alias_is_checked_without_modifying_it(self):
        self.install_omv()
        cron = self.paths.path("/etc/crontabs/root")
        cron.parent.mkdir(parents=True)
        cron.write_text("* * * * * /usr/local/sbin/torrent-guard-maintenance tick\n")
        alias = self.paths.path("/var/spool/cron/crontabs")
        alias.parent.mkdir(parents=True)
        alias.symlink_to(cron.parent)
        with self.assertRaisesRegex(InstallError, "Disable"):
            self.uninstaller.plan("omv")
        self.assertTrue(alias.is_symlink())
        self.assertTrue(self.paths.key.exists())

    def test_unknown_directory_contents_and_shared_group_are_preserved(self):
        self.installer.docker(sample_key(), None)
        home = self.paths.path("/var/lib/guard-maintenance-ssh")
        unknown = home / "personal-file"
        unknown.write_text("DO NOT DELETE")
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "Unexpected files"):
            self.uninstaller.plan("docker")
        self.assertEqual(self.snapshot(self.root), before)
        unknown.unlink()
        self.group_exists = True
        with patch("scripts.maintenance_uninstall.pwd.getpwall", return_value=[SimpleNamespace(pw_name="normal-admin", pw_gid=1001)]):
            with self.assertRaisesRegex(InstallError, "shared"):
                self.uninstaller.plan("docker")
        self.assertTrue(self.created_user)

    def test_custom_config_cannot_delete_normal_ssh_identities(self):
        self.install_omv()
        config = json.loads(self.paths.config.read_text())
        config["identity_file"] = str(self.paths.key.parent / "id_ed25519")
        self.paths.config.write_text(json.dumps(config))
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(InstallError, "outside dedicated"):
            self.uninstaller.plan("omv")
        self.assertEqual(self.snapshot(self.root), before)

    def test_removing_dedicated_key_symlink_does_not_follow_it(self):
        self.install_omv()
        self.paths.key.unlink()
        target = self.paths.key.parent / "id_ed25519"
        self.paths.key.symlink_to(target)
        self.apply("omv")
        self.assertFalse(self.paths.key.is_symlink())
        self.assertEqual(target.read_text(), "normal private identity\n")

    def test_directory_symlink_escape_and_unrelated_sudo_rules_are_rejected(self):
        self.installer.docker(sample_key(), None)
        ssh = self.paths.path("/var/lib/guard-maintenance-ssh/.ssh")
        (ssh / "authorized_keys").unlink()
        ssh.rmdir()
        ssh.symlink_to(self.paths.key.parent)
        before = self.snapshot(self.paths.key.parent)
        with self.assertRaises(InstallError):
            self.uninstaller.plan("docker")
        self.assertEqual(self.snapshot(self.paths.key.parent), before)
        ssh.unlink()
        ssh.mkdir()
        sudo = self.paths.path("/etc/sudoers.d/torrent-guard-maintenance")
        sudo.chmod(0o600)
        sudo.write_text("normal-admin ALL=(ALL) ALL\n")
        with self.assertRaisesRegex(InstallError, "unrelated rules"):
            self.uninstaller.plan("docker")
        self.assertTrue(self.created_user)


if __name__ == "__main__":
    unittest.main()
