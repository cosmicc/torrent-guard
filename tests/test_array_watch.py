"""Independent accessibility checks; no production mounts, jobs or HTTPS calls."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import maintenance
import torrent_guard_array as checks


class DriveProbeTests(unittest.TestCase):
    """Exercise real directory reads with synthetic mount identities."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'array.conf'
        self.data, self.parity = self.root / 'data', self.root / 'parity'
        self.data.mkdir()
        self.parity.mkdir()
        self.config.write_text(f'data d1 {self.data}\nparity {self.parity}/snapraid.parity\n')
        self.guarded = self.root / 'guarded'
        self.guarded.write_text(f'#!/bin/bash\nCONF={self.config}\n')
        self.guarded.chmod(0o700)
        self.commands = []
        self.mounts = {str(path): {'target': str(path), 'uuid': path.name + '-uuid',
                                'fstype': 'ext4', 'options': 'rw,relatime',
                                'maj:min': f'{os.major(path.stat().st_dev)}:{os.minor(path.stat().st_dev)}'}
                       for path in (self.data, self.parity)}
        for target, replacement in (('torrent_guard_array.GUARDED', self.guarded),
                                    ('torrent_guard_array.check_kernel_device', Mock()),
                                    ('torrent_guard_array.subprocess.run', self.command)):
            context = patch(target, replacement)
            context.start()
            self.addCleanup(context.stop)

    def command(self, command, **kwargs):
        self.commands.append(command)
        if command == [str(self.guarded), 'preflight']:
            return subprocess.CompletedProcess(command, 0)
        self.assertEqual(command[:3], ['/usr/bin/findmnt', '--json', '--target'])
        self.assertNotIn('shell', kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({'filesystems': [self.mounts[command[3]]]}).encode())

    def test_enrolls_all_drives_then_checks_identity_without_running_preflight_every_minute(self):
        report = checks.probe_array_drives([])
        self.assertTrue(report['complete'])
        self.assertEqual(set(report['healthy']), {'data d1:0', 'parity:0'})
        self.assertEqual(len(report['inventory']), 2)
        self.commands.clear()
        self.assertTrue(checks.probe_array_drives(report['inventory'])['complete'])
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all(command[0] == '/usr/bin/findmnt' for command in self.commands))
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(list(self.parity.iterdir()), [])

    def test_missing_readonly_wrong_uuid_and_wrong_mount_keep_known_inventory(self):
        previous = checks.probe_array_drives([])['inventory']
        original = self.mounts[str(self.data)].copy()
        for fields in ({'target': '/'}, {'uuid': None}, {'uuid': 'replacement'},
                       {'target': '/unexpected'}, {'options': 'ro,relatime'}, {'maj:min': '0:99999'}):
            with self.subTest(fields=fields):
                self.mounts[str(self.data)] = {**original, **fields}
                report = checks.probe_array_drives(previous)
                self.assertFalse(report['complete'])
                self.assertEqual(report['inventory'], previous)
                self.assertEqual(report['issues'][0]['key'], 'data d1:0')
                self.assertEqual(report['healthy'], ['parity:0'])

    def test_unreadable_directory_is_an_outage_and_exceptions_stay_private(self):
        previous = checks.probe_array_drives([])['inventory']
        self.data.rmdir()
        report = checks.probe_array_drives(previous)
        self.assertFalse(report['complete'])
        self.assertEqual(report['issues'][0]['key'], 'data d1:0')
        self.assertNotIn(str(self.root), report['issues'][0]['reason'])
        with patch('torrent_guard_array.os.scandir', side_effect=PermissionError('private path')):
            report = checks.probe_array_drives(previous)
        self.assertFalse(report['complete'])
        self.assertNotIn('private path', str(report))

    def test_failed_enrollment_malformed_config_and_different_array_never_report_recovery(self):
        with patch('torrent_guard_array.subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
            report = checks.probe_array_drives([])
        self.assertFalse(report['complete'])
        self.assertEqual(report['healthy'], [])
        self.assertEqual(report['inventory'], [])
        for text in ('data d1 /array\n', 'data d1 relative\nparity /array/p\n',
                     'data d1 /array\ndata d1 /other\nparity /parity/p\n'):
            self.config.write_text(text)
            self.assertFalse(checks.probe_array_drives([])['complete'])
        other = self.root / 'other.conf'
        other.touch()
        report = checks.probe_array_drives([], str(other))
        self.assertFalse(report['complete'])
        self.assertIn('differs', report['issues'][0]['reason'])

    def test_worker_deadline_and_success_use_real_isolation(self):
        self.assertTrue(checks.bounded_array_probe([], timeout=2)['complete'])
        started = time.monotonic()
        with patch('torrent_guard_array.probe_array_drives', side_effect=lambda *args: time.sleep(30)):
            report = checks.bounded_array_probe([], timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(report['complete'])
        self.assertEqual(report['healthy'], [])
        self.assertEqual(report['issues'][0]['key'], 'monitor')


class KernelDeviceTests(unittest.TestCase):
    """Synthetic sysfs proves removal, partition and mapper dependency checks."""

    def test_offline_and_removed_backing_devices_are_detected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            disk = root / 'disk'
            (disk / 'device').mkdir(parents=True)
            state = disk / 'device/state'
            state.write_text('running\n')
            partition = disk / 'part1'
            partition.mkdir()
            (partition / 'partition').write_text('1')
            (root / '8:1').symlink_to(partition, target_is_directory=True)
            checks.check_kernel_device('8:1', root)
            mapper = root / 'mapper'
            (mapper / 'slaves').mkdir(parents=True)
            (mapper / 'slaves/backing').symlink_to(partition, target_is_directory=True)
            (root / '253:0').symlink_to(mapper, target_is_directory=True)
            checks.check_kernel_device('253:0', root)
            state.write_text('offline\n')
            for number in ('8:1', '253:0'):
                with self.assertRaises(checks.ArrayCheckError):
                    checks.check_kernel_device(number, root)
            (root / '8:1').unlink()
            with self.assertRaises(FileNotFoundError):
                checks.check_kernel_device('8:1', root)


class WatchNotificationTests(unittest.TestCase):
    """Real independent journals and locks; delivery and observations are simulated."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.watch = self.root / 'watch'
        self.settings = {'pushover': {'app_token': 'a' * 30, 'user_key': 'b' * 30}}
        self.issue = {'key': 'data d1:0', 'label': 'data d1', 'reason': 'Filesystem mount is unavailable.'}
        self.report = {'inventory': [], 'issues': [self.issue], 'healthy': [], 'complete': False}
        self.send = Mock(return_value=True)
        for target, replacement in (
                ('maintenance.private_directory', lambda path: path.mkdir(mode=0o700, exist_ok=True)),
                ('maintenance.root_config', lambda path: json.loads(path.read_text())),
                ('maintenance.bounded_array_probe', lambda *args: self.report),
                ('maintenance.send_pushover', self.send)):
            context = patch(target, replacement)
            context.start()
            self.addCleanup(context.stop)

    def tick(self):
        maintenance.watch_array(self.settings, self.watch)

    def test_outage_is_durable_deduplicated_and_recovery_rearms_alert(self):
        def delivery(*args, **kwargs):
            saved = json.loads((self.watch / 'state.json').read_text())
            self.assertTrue(saved['incidents']['data d1:0']['attempted'])
            return False  # Delivery failure must still deduplicate across invocations.

        self.send.side_effect = delivery
        self.tick()
        self.tick()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.kwargs['priority'], 1)
        self.assertIn('data d1', self.send.call_args.args[3])
        self.send.side_effect = None
        self.report = {**self.report, 'issues': [], 'healthy': ['data d1:0'], 'complete': True}
        self.tick()
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('recovered', self.send.call_args.args[2])
        self.assertEqual(self.send.call_args.kwargs['priority'], 0)
        self.report = {**self.report, 'issues': [self.issue], 'healthy': [], 'complete': False}
        self.tick()
        self.assertEqual(self.send.call_count, 3)

    def test_unknown_probe_preserves_drive_outage_without_false_recovery(self):
        self.tick()
        self.report = {**self.report, 'issues': [{'key': 'monitor', 'label': 'Array monitor',
                                                'reason': 'Probe timed out.'}]}
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(len(json.loads((self.watch / 'state.json').read_text())['incidents']), 2)
        self.assertFalse(any('recovered' in call.args[2] for call in self.send.call_args_list))

    def test_maintenance_lock_and_journal_are_untouched(self):
        journal = self.root / 'state.json'
        journal.write_text('{"active":true,"jobs":{"sync":"running"}}')
        original = journal.read_bytes()
        with (self.root / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.tick()
        self.assertEqual(journal.read_bytes(), original)
        self.send.assert_called_once()

    def test_cli_dispatches_watcher_without_opening_coordinator(self):
        config = self.root / 'omv.json'
        config.write_text(json.dumps(self.settings))
        with patch('maintenance.sys.argv', ['maintenance.py', 'watch-array', '--config', str(config)]), \
                patch('maintenance.os.geteuid', return_value=0), \
                patch('maintenance.watch_array') as watch, \
                patch('maintenance.RemoteCoordinator') as coordinator:
            self.assertEqual(maintenance.main(), 0)
        watch.assert_called_once_with(self.settings)
        coordinator.assert_not_called()

    def test_disabled_alerts_keep_observations_and_enabling_alerts_reports_current_outage(self):
        self.settings = {}
        self.tick()
        self.send.assert_not_called()
        self.settings = {'pushover': {'app_token': 'a'*30, 'user_key': 'b'*30}}
        self.tick()
        self.send.assert_called_once()

    def test_busy_monitor_lock_does_not_launch_another_probe(self):
        self.watch.mkdir(mode=0o700)
        with (self.watch / 'lock').open('w') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('maintenance.bounded_array_probe') as probe:
                self.tick()
                probe.assert_not_called()
        self.send.assert_not_called()

    def test_unwritable_or_corrupt_journal_never_sends_unrecorded_alert(self):
        with patch('maintenance.atomic_json', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.tick()
        self.send.assert_not_called()
        path = self.watch / 'state.json'
        path.write_text('{bad')
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(path.read_text(), '{bad')
        self.send.assert_not_called()

    def test_large_outage_is_batched_without_losing_any_drive(self):
        self.report['issues'] = [dict(self.issue, key=f'data d{i}:0', label='data ' + str(i) + 'x'*60)
                                 for i in range(32)]
        self.tick()
        self.assertLessEqual(self.send.call_count, 3)
        messages = '\n'.join(call.args[3] for call in self.send.call_args_list)
        for issue in self.report['issues']:
            self.assertIn(issue['label'], messages)
        self.assertTrue(all(len(call.args[3]) <= 1024 for call in self.send.call_args_list))
