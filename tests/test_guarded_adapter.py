"""Execute the additive shell gate against synthetic, non-mutating array fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from array_fixtures import guarded_script
from scripts.guarded_adapter import adapt_guarded, install_guarded_adapter
from scripts.maintenance_install import InstallError
import test_maintenance_install as host
import torrent_guard_array as checks


@unittest.skipUnless(shutil.which('bash'), 'The OMV guarded shell adapter requires host Bash')
class ShellGateTests(unittest.TestCase):
    """No test invokes real SnapRAID or has access to production disks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'guarded'
        self.original = guarded_script()
        self.path.write_text(adapt_guarded(self.original))
        self.path.chmod(0o700)

    def run_gate(self, **values):
        environment = {**os.environ, **{key: str(value) for key, value in values.items()}}
        return subprocess.run(['bash', str(self.path), 'check-sync'], text=True, capture_output=True, env=environment)

    def test_adaptation_is_idempotent_and_preserves_original_policy_values(self):
        adapted = self.path.read_text()
        self.assertEqual(adapt_guarded(adapted), adapted)
        for line in ('MAX_ADDED=250', 'MAX_REMOVED=50', 'MAX_UPDATED=100', 'MAX_MOVED=100'):
            self.assertIn(line, adapted)
        self.assertIn('    preflight\n    run_snapraid sync sync\n', adapted)
        subprocess.run(['bash', '-n', str(self.path)], check=True)
        for text in (self.original.replace('DIFF_REMOVED <= MAX_REMOVED', 'DIFF_REMOVED < MAX_REMOVED'),
                     adapted.replace('tg_percent_guard\n}', '\n}')):
            with self.assertRaises(InstallError):
                adapt_guarded(text)

    def test_normal_changes_pass_without_running_sync_or_scrub(self):
        result = self.run_gate(TEST_ADDED=12, TEST_UPDATED=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('MUTATING OPERATION', result.stdout)
        data = json.loads(next(line[len('TG_DIFF_V1 '):] for line in result.stdout.splitlines() if line.startswith('TG_DIFF_V1 ')))
        self.assertEqual(data['counts']['added'], 12)
        self.assertEqual(data['counts']['updated'], 3)
        self.assertEqual(data['limits']['removed'], 50)
        self.assertEqual(data['max_percent'], 5)

    def test_existing_limits_and_exact_percentage_boundary_block_before_mutation(self):
        for setting, value in (('TEST_ADDED', 251), ('TEST_REMOVED', 51), ('TEST_UPDATED', 101),
                               ('TEST_MOVED', 101), ('TEST_COPIED', 251), ('TEST_RESTORED', 51), ('TEST_RELOCATED', 251)):
            result = self.run_gate(**{setting: value})
            self.assertNotEqual(result.returncode, 0, setting)
            self.assertIn('TG_DIFF_V1 ', result.stdout)
            self.assertNotIn('MUTATING OPERATION', result.stdout)
        exact = self.run_gate(TEST_REMOVED=50, TEST_EQUAL=950)
        self.assertNotEqual(exact.returncode, 0)
        self.assertIn('reaches 5%', exact.stdout)
        under = self.run_gate(TEST_REMOVED=49, TEST_EQUAL=951)
        self.assertEqual(under.returncode, 0, under.stdout + under.stderr)

    def test_final_sync_repeats_percentage_check(self):
        result = subprocess.run(['bash', str(self.path), 'sync'], capture_output=True, text=True,
                                env={**os.environ, 'TEST_REMOVED': '50', 'TEST_EQUAL': '950'})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('MUTATING OPERATION', result.stdout)

    def test_missing_mount_and_malformed_numeric_values_never_pass(self):
        for values in ({'TEST_MISSING': 1}, {'TEST_UPDATED': 'oops'}, {'TEST_UPDATED': '9999999999999'},
                       {'TORRENT_GUARD_DIFF_MAX_PERCENT': 0}, {'TORRENT_GUARD_DIFF_MAX_PERCENT': '5;id'}):
            result = self.run_gate(**values)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('MUTATING OPERATION', result.stdout)

    def test_python_gate_reports_counts_and_maps_failures_without_exposing_output(self):
        with patch('torrent_guard_array.GUARDED', self.path), patch.dict(os.environ, TEST_UPDATED='3'):
            passed = checks.pre_sync_check()
        self.assertTrue(passed['passed'])
        self.assertEqual(passed['counts']['updated'], 3)
        with patch('torrent_guard_array.GUARDED', self.path), patch.dict(os.environ, TEST_REMOVED='51'):
            blocked = checks.pre_sync_check()
        self.assertFalse(blocked['passed'])
        self.assertIn('removed=51 exceeds limit 50', blocked['reason'])
        with patch('torrent_guard_array.GUARDED', self.path), patch.dict(os.environ, TEST_MISSING='1'):
            missing = checks.pre_sync_check()
        self.assertFalse(missing['passed'])
        self.assertIsNone(missing['counts'])
        self.assertIn('not mounted', missing['reason'])
        self.assertNotIn('fixture', missing['reason'])

    def test_duplicate_partial_invalid_reports_and_exit_errors_fail_closed(self):
        valid = self.run_gate().stdout
        data = valid + valid
        for result in (checks.CommandResult(0, data, True), checks.CommandResult(0, valid, False),
                       checks.CommandResult(1, valid, True), checks.CommandResult(0, 'TG_DIFF_V1 {}', True)):
            with patch('torrent_guard_array.GUARDED', self.path), patch('torrent_guard_array.read_command', return_value=result):
                self.assertFalse(checks.pre_sync_check()['passed'])

    def test_wrong_array_configuration_is_blocked_before_sync(self):
        with patch('torrent_guard_array.GUARDED', self.path):
            result = checks.pre_sync_check(expected_config='/etc/other-array.conf')
        self.assertFalse(result['passed'])
        self.assertIn('differs from', result['reason'])
        self.assertIsNotNone(result['counts'])
        with patch('torrent_guard_array.GUARDED', self.path):
            result = checks.pre_sync_check(expected_config='/etc/snapraid.conf')
        self.assertTrue(result['passed'])
        self.assertEqual(result['config_path'], '/etc/snapraid.conf')

    def test_unsafe_script_or_parent_is_refused(self):
        self.path.chmod(0o777)
        with patch('torrent_guard_array.GUARDED', self.path):
            self.assertFalse(checks.pre_sync_check()['passed'])
        self.path.chmod(0o700)
        self.root.chmod(0o777)
        with patch('torrent_guard_array.GUARDED', self.path):
            self.assertFalse(checks.pre_sync_check()['passed'])
        self.root.chmod(0o700)


@unittest.skipUnless(shutil.which('bash'), 'The OMV guarded shell adapter requires host Bash')
class AdapterInstallTests(host.InstallerFixture):
    """Real temporary files prove exact backups and non-overwriting repeat setup."""

    def setUp(self):
        super().setUp()
        self.path = self.paths.path('/usr/local/sbin/snapraid-guarded')
        self.path.parent.mkdir(parents=True)
        self.path.write_text(guarded_script())
        self.path.chmod(0o750)
        self.installer.run = self.real_run

    def test_install_saves_original_bytes_and_second_run_preserves_all_metadata(self):
        original = self.path.read_bytes()
        install_guarded_adapter(self.installer)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o750)
        backups = list(self.paths.path('/var/lib/torrent-guard-maintenance-array').glob('guarded-*.sh'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        before = self.snapshot(self.root)
        install_guarded_adapter(self.installer)
        self.assertEqual(self.snapshot(self.root), before)

    def test_unknown_shared_script_is_untouched(self):
        self.path.write_text('#!/bin/bash\necho unrelated\n')
        before = self.snapshot(self.root)
        with self.assertRaises(InstallError):
            install_guarded_adapter(self.installer)
        self.assertEqual(self.snapshot(self.root), before)
