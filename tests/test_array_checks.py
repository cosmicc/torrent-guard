"""Real private-history files and isolated commands exercise array safety evidence."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from array_fixtures import ata_data
import torrent_guard_array as checks


class CommandTests(unittest.TestCase):
    """Diagnostic subprocesses are bounded and never interpret shell text."""

    def test_real_readonly_process_output_and_timeout(self):
        result = checks.read_command([sys.executable, '-c', 'print("ok")'], 5)
        self.assertTrue(result.complete)
        self.assertEqual(result.output.strip(), 'ok')
        timed = checks.read_command([sys.executable, '-c', 'import time;print("partial",flush=True);time.sleep(10)'], .2)
        self.assertFalse(timed.complete)
        self.assertIn('partial', timed.output)
        self.assertLess(timed.code, 0)

    def test_oversized_invalid_utf8_or_missing_binary_cannot_pass(self):
        result = checks.read_command([sys.executable, '-c', 'print("x"*100)'], 5, limit=10)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.output), 10)
        result = checks.read_command([sys.executable, '-c', 'import os;os.write(1,b"\\xff")'], 5)
        self.assertFalse(result.complete)
        with self.assertRaises(checks.ArrayCheckError):
            checks.read_command(['/nonexistent-fixture-binary'], 5)


class SmartParsingTests(unittest.TestCase):
    """Vendor raw error rates, cable issues and historical flags are distinct."""

    def sample(self, **overrides):
        return checks.smart_sample(ata_data(**overrides), ['data'], 0)

    def test_healthy_metrics_and_missing_fields_are_not_fake_zero(self):
        sample = self.sample()
        self.assertFalse(sample['critical'])
        self.assertEqual(sample['metrics']['pending'], 0)
        data = ata_data()
        data['ata_smart_attributes']['table'] = [{'id': 5, 'raw': {'value': 0}}]
        sample = checks.smart_sample(data, ['parity'], 0)
        self.assertIsNone(sample['metrics']['pending'])
        self.assertTrue(sample['warnings'])
        self.assertIn('n/a', '\n'.join(checks.smart_lines({'samples': [sample], 'errors': []})))

    def test_pending_uncorrectable_current_failure_and_failed_selftest_are_critical(self):
        self.assertTrue(self.sample(pending=1)['critical'])
        self.assertTrue(self.sample(uncorrectable=1)['critical'])
        data = ata_data()
        data['smart_status']['passed'] = False
        self.assertTrue(checks.smart_sample(data, ['data'], 8)['critical'])
        self.assertTrue(checks.smart_sample(ata_data(), ['data'], 16)['critical'])
        data = ata_data()
        data['ata_smart_self_test_log'] = {'standard': {'table': [{'status': {'passed': False}}]}}
        self.assertTrue(checks.smart_sample(data, ['data'], 128)['critical'])

    def test_stable_reallocated_and_crc_are_warnings_not_automatic_drive_failure(self):
        sample = self.sample(reallocated=4, crc=7)
        self.assertFalse(sample['critical'])
        self.assertTrue(any('cable' in reason for reason in sample['warnings']))
        data = ata_data()
        data['ata_smart_attributes']['table'].append({'id': 1, 'raw': {'value': 999999999999}})
        self.assertFalse(checks.smart_sample(data, ['data'], 0)['critical'])
        historical = checks.smart_sample(ata_data(), ['data'], 224)
        self.assertFalse(historical['critical'])
        self.assertTrue(historical['warnings'])

    def test_temperature_thresholds_and_bad_readings(self):
        for temp, critical in ((49, False), (50, False), (60, True)):
            data = ata_data()
            data['temperature']['current'] = temp
            self.assertEqual(bool(checks.smart_sample(data, ['data'], 0)['critical']), critical)
        for code in (-9, 1, 2, 4):
            with self.assertRaises(checks.ArrayCheckError):
                checks.smart_sample(ata_data(), ['data'], code)
        for field in ('serial_number', 'smart_status', 'ata_smart_attributes'):
            data = ata_data()
            data.pop(field)
            with self.assertRaises(checks.ArrayCheckError):
                checks.smart_sample(data, ['data'], 0)

    def test_nvme_and_scsi_use_protocol_specific_counters(self):
        data = {'serial_number': 'fixture-nvme', 'device': {'protocol': 'NVMe'}, 'smart_status': {'passed': True},
                'nvme_smart_health_information_log': {'critical_warning': 1, 'media_errors': 3, 'percentage_used': 105}}
        sample = checks.smart_sample(data, ['parity'], 0)
        self.assertTrue(sample['critical'])
        self.assertEqual(sample['metrics']['media_errors'], 3)
        data = {'serial_number': 'fixture-sas', 'device': {'protocol': 'SCSI'}, 'smart_status': {'passed': True},
                'scsi_grown_defect_list': 2, 'scsi_error_counter_log': {
                    key: {'total_uncorrected_errors': 1} for key in ('read', 'write', 'verify')}}
        sample = checks.smart_sample(data, ['data'], 0)
        self.assertEqual(sample['metrics']['reallocated'], 2)
        self.assertEqual(sample['metrics']['media_errors'], 3)


class HistoryTests(unittest.TestCase):
    """Serial identity prevents device-name changes or replacements from corrupting trends."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'smart.sqlite3'

    def test_growth_is_critical_stable_counts_are_not_and_replacement_is_separate(self):
        initial = checks.smart_sample(ata_data(reallocated=2), ['data'], 0)
        checks.save_samples(self.path, [initial], 'first')
        stable = checks.smart_sample(ata_data(reallocated=2), ['data'], 0)
        checks.save_samples(self.path, [stable], 'second')
        self.assertFalse(stable['critical'])
        rising = checks.smart_sample(ata_data(reallocated=4, crc=3), ['renamed-role'], 0)
        checks.save_samples(self.path, [rising], 'third')
        self.assertEqual(rising['deltas']['reallocated'], 2)
        self.assertTrue(rising['critical'])
        replacement = checks.smart_sample(ata_data(serial='replacement-fixture', reallocated=1), ['data'], 0)
        checks.save_samples(self.path, [replacement], 'fourth')
        self.assertEqual(replacement['deltas'], {})
        self.assertFalse(replacement['critical'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute('SELECT count(*) FROM samples').fetchone()[0], 4)

    def test_counter_reset_warns_and_audit_evidence_is_preserved(self):
        checks.save_samples(self.path, [checks.smart_sample(ata_data(reallocated=9), ['data'], 0)], 'first')
        reset = checks.smart_sample(ata_data(), ['data'], 0)
        checks.save_samples(self.path, [reset], 'second')
        self.assertTrue(any('counter decreased' in item for item in reset['warnings']))

    def test_foreign_database_symlink_and_insecure_mode_are_refused(self):
        with closing(sqlite3.connect(self.path)) as database:
            database.execute('CREATE TABLE unrelated(data TEXT)')
        self.path.chmod(0o600)
        with self.assertRaises(checks.ArrayCheckError):
            checks.save_samples(self.path, [], 'test')
        self.path.unlink()
        self.path.symlink_to(self.root / 'outside')
        with self.assertRaises(checks.ArrayCheckError):
            checks.save_samples(self.path, [], 'test')
        self.path.unlink()
        self.path.write_bytes(b'')
        self.path.chmod(0o644)
        with self.assertRaises(checks.ArrayCheckError):
            checks.save_samples(self.path, [], 'test')


class DeviceTests(unittest.TestCase):
    """Array roles are mapped to physical disks including parity, never guessed by label."""

    def test_mapping_deduplicates_physical_disks_and_keeps_parity(self):
        mapping = checks.array_devices('8:80 /dev/sdf 8:81 /dev/sdf1 sde\n8:64 /dev/sde 8:65 /dev/sde1 parity\n')
        self.assertEqual(mapping['/dev/sdf']['roles'], ['sde'])
        self.assertEqual(mapping['/dev/sde']['roles'], ['parity'])
        both = checks.array_devices('8:0 /dev/sda 8:1 /dev/sda1 data\n8:0 /dev/sda 8:2 /dev/sda2 other\n8:16 /dev/sdb 8:17 /dev/sdb1 parity\n')
        self.assertEqual(both['/dev/sda']['roles'], ['data', 'other'])
        for output in ('', '8:0 /dev/sda 8:1 /dev/sda1 data', 'bad mapping',
                       '8:0 /dev/../secret 8:1 /dev/sda1 parity'):
            with self.assertRaises(checks.ArrayCheckError):
                checks.array_devices(output)

    def test_collection_saves_data_and_parity_and_rejects_hot_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_stat = os.stat
            calls = []
            mapping = '8:0 /dev/sda 8:1 /dev/sda1 data\n8:16 /dev/sdb 8:17 /dev/sdb1 parity\n'
            def device_stat(path, *args, **kwargs):
                if str(path) in ('/dev/sda', '/dev/sdb'):
                    return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 0 if str(path).endswith('sda') else 16))
                return real_stat(path, *args, **kwargs)
            def run(command, *args, **kwargs):
                calls.append(command)
                if command[-1] == 'preflight':
                    return checks.CommandResult(0, '', True)
                if command[-1] == 'devices':
                    return checks.CommandResult(0, mapping, True)
                return checks.CommandResult(0, json.dumps(ata_data(serial='fixture-' + command[-1].split('/')[-1])), True)
            with patch('torrent_guard_array.trusted_guarded'), patch('torrent_guard_array.os.stat', side_effect=device_stat), patch('torrent_guard_array.read_command', side_effect=run):
                report = checks.check_smart_health('', root, 'fixture')
            self.assertTrue(report['complete'])
            self.assertEqual(len(report['samples']), 2)
            self.assertFalse(report['critical'])
            self.assertEqual(sum(command[0] == checks.SMARTCTL for command in calls), 2)
            self.assertFalse(any('-t' in command for command in calls))
            counts = {}
            def swapped(path, *args, **kwargs):
                value = device_stat(path, *args, **kwargs)
                if str(path) == '/dev/sda':
                    counts['sda'] = counts.get('sda', 0) + 1
                    if counts['sda'] > 1:
                        value.st_rdev = os.makedev(8, 32)
                return value
            with patch('torrent_guard_array.trusted_guarded'), patch('torrent_guard_array.os.stat', side_effect=swapped), patch('torrent_guard_array.read_command', side_effect=run):
                report = checks.check_smart_health('', root, 'fixture')
            self.assertFalse(report['complete'])
            self.assertTrue(report['errors'])
