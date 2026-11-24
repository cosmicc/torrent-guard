"""Pre-sync gating and SMART review holds across the real two-host journal flow."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from array_fixtures import ata_data
import maintenance
from torrent_guard_array import DIFF_KEYS, smart_sample
from test_scrub_catchup import report
import test_maintenance_remote as fixtures


class ArrayMaintenanceTests(fixtures.RemoteMaintenanceTests):
    """No real array/Docker calls; use the restricted agent and durable temp journals."""

    def setUp(self):
        super().setUp()
        self.good_precheck = deepcopy(self.precheck)
        self.good_health = deepcopy(self.health)
        self.controller.notification_keys = ('a'*30, 'b'*30)
        context = patch('maintenance.send_pushover', return_value=True)
        self.send = context.start()
        self.addCleanup(context.stop)

    def reload(self):
        self.controller = maintenance.RemoteCoordinator(self.omv, {**self.connection,
            'pushover': {'app_token': 'a'*30, 'user_key': 'b'*30}})

    def critical_health(self):
        self.health['samples'] = [smart_sample(ata_data(pending=1), ['data'], 0)]
        self.health['critical'] = True

    def test_gate_runs_only_after_stop_ack_and_before_start_alert_or_sync(self):
        events = []
        def gate(*args):
            self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))
            self.assertFalse(self.jobs)
            events.append('gate')
            return self.precheck
        def health(*args):
            self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))
            self.assertEqual(self.controller.state['jobs']['sync'], 'complete')
            events.append('health')
            return self.health
        def notify(token, user, title, message, **kwargs):
            if 'sync starting' in title:
                self.assertEqual(events, ['gate'])
                self.assertFalse(self.jobs)
                self.assertIn('equal=10000', message)
            if title == 'SnapRAID disk SMART report':
                self.assertTrue(all(row['State']['Running'] for row in self.rows.values()))
                self.assertIn('pending=0', message)
            return True
        self.send.side_effect = notify
        with patch('maintenance.pre_sync_check', side_effect=gate) as check, patch('maintenance.check_smart_health', side_effect=health):
            self.controller.tick(self.at(10, 2))
            check.assert_not_called()
            self.controller.tick(self.at(10, 2, 1))
        self.assertEqual(events, ['gate', 'health'])
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.controller.state['active'])

    def test_blocked_diff_alert_replaces_start_and_latches_until_operator_retry(self):
        self.precheck = {'passed': False, 'counts': {key: 51 if key == 'removed' else 10000 if key == 'equal' else 0 for key in DIFF_KEYS},
                         'reason': 'removed=51 exceeds limit 50'}
        with patch('maintenance.pre_sync_check', side_effect=lambda *args: self.precheck) as gate, patch('maintenance.check_smart_health') as health:
            for _ in range(2):
                with self.assertRaises(maintenance.MaintenanceError):
                    self.controller.tick(self.at(10, 2, 1))
            self.assertEqual(gate.call_count, 1)
            health.assert_not_called()
        self.assertFalse(self.jobs)
        self.assertEqual(self.controller.state['jobs']['sync'], 'failed')
        self.assertTrue(self.controller.state['active'])
        self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('BLOCKED', self.send.call_args.args[2])
        self.assertIn('removed=51', self.send.call_args.args[3])
        self.assertEqual(self.send.call_args.kwargs['priority'], 1)
        self.controller.authorize_retry()
        self.precheck = deepcopy(self.good_precheck)
        self.controller.tick(self.at(10, 3))
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.controller.state['active'])

    def test_missing_drive_block_reports_counts_unavailable_without_start_alert(self):
        self.precheck = {'passed': False, 'counts': None, 'reason': 'Required array filesystem is not mounted.'}
        with self.assertRaises(maintenance.ArrayHold):
            self.controller.tick(self.at(9, 2, 1))
        self.assertFalse(self.jobs)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('counts: unavailable', self.send.call_args.args[3])
        self.assertNotIn('sync starting', self.send.call_args.args[2])

    def test_critical_smart_keeps_apps_stopped_and_only_rechecks_after_explicit_retry(self):
        self.critical_health()
        with patch('maintenance.check_smart_health', side_effect=lambda *args: self.health) as health:
            with self.assertRaises(maintenance.ArrayHold):
                self.controller.tick(self.at(10, 2, 1))
            self.assertEqual(health.call_count, 1)
            self.assertTrue(self.controller.state['smart_review_required'])
            self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))
            self.assertTrue(all(phase == 'complete' for phase in self.controller.state['jobs'].values()))
            count = self.send.call_count
            self.reload()
            with self.assertRaises(maintenance.ArrayHold):
                self.controller.tick(self.at(11, 3))
            self.assertEqual(health.call_count, 1)
            self.assertEqual(self.send.call_count, count)
            self.controller.authorize_retry()
            self.health = deepcopy(self.good_health)
            self.controller.tick(self.at(11, 3))
            self.assertEqual(health.call_count, 2)
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.controller.state['active'])
        self.assertTrue(all(row['State']['Running'] for row in self.rows.values()))
        self.assertEqual(self.controller.state['smart_attempts'], 2)

    def test_failed_sync_also_reports_smart_without_replacing_original_error(self):
        self.returncode = 23
        self.critical_health()
        with self.assertRaisesRegex(maintenance.MaintenanceError, 'status 23'):
            self.controller.tick(self.at(10, 2, 1))
        self.assertTrue(self.controller.state['smart_review_required'])
        self.assertEqual(self.controller.state['jobs']['sync'], 'failed')
        self.assertEqual(self.controller.state['job_failure']['exit_code'], 23)
        reports = [call for call in self.send.call_args_list if 'CRITICAL DISK HEALTH' in call.args[2]]
        self.assertTrue(reports)
        self.assertIn('script exited with status 23', reports[0].args[3])
        self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))

    def test_monday_smart_runs_after_both_jobs_without_repeating_them_on_recovery(self):
        self.critical_health()
        with self.assertRaises(maintenance.ArrayHold):
            self.controller.tick(self.at(9, 2, 1))
        self.assertEqual(self.jobs, [self.paths['sync'], self.paths['scrub']])
        self.controller.authorize_retry()
        self.health = deepcopy(self.good_health)
        self.controller.tick(self.at(10, 7))
        self.assertEqual(len(self.jobs), 2)
        self.assertFalse(self.controller.state['active'])

    def test_incomplete_smart_does_not_claim_failing_or_healthy_and_requires_review(self):
        self.health.update(complete=False, errors=['SMART data unavailable for parity'])
        with self.assertRaises(maintenance.ArrayHold):
            self.controller.tick(self.at(10, 2, 1))
        self.assertTrue(self.controller.state['smart_review_required'])
        self.assertIn('CHECK INCOMPLETE', self.send.call_args.args[2])
        self.assertNotIn('CRITICAL DISK HEALTH', self.send.call_args.args[2])
        self.assertIn('unavailable for parity', self.send.call_args.args[3])

    def test_private_history_error_latches_incomplete_diagnostics_safely(self):
        original = maintenance.private_directory
        def private(path):
            if path == self.controller.array_directory:
                raise OSError('private-path-must-not-be-sent')
            return original(path)
        with patch('maintenance.private_directory', side_effect=private):
            with self.assertRaises(maintenance.ArrayHold):
                self.controller.tick(self.at(10, 2, 1))
        self.assertTrue(self.controller.state['smart_review_required'])
        self.assertNotIn('private-path-must-not-be-sent', str(self.send.call_args_list))

    def test_pushover_failure_never_changes_good_health_or_critical_hold(self):
        self.send.return_value = False
        self.controller.tick(self.at(10, 2, 1))
        self.assertFalse(self.controller.state['active'])
        self.critical_health()
        with self.assertRaises(maintenance.ArrayHold):
            self.controller.tick(self.at(11, 2, 1))
        self.assertTrue(self.controller.state['active'])

    def test_lost_finish_ack_does_not_recheck_smart_or_repeat_jobs(self):
        self.lose_finish = True
        with patch('maintenance.check_smart_health', side_effect=lambda *args: self.health) as health:
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(10, 2, 1))
            self.reload()
            self.controller.tick(self.at(11, 7))
            self.assertEqual(health.call_count, 1)
        self.assertEqual(len(self.jobs), 1)
        reports = [call for call in self.send.call_args_list if call.args[2] == 'SnapRAID disk SMART report']
        self.assertEqual(len(reports), 1)
        self.assertFalse(self.controller.state['active'])

    def test_multiple_smart_messages_stay_bounded_and_critical(self):
        self.health['samples'] = [smart_sample(ata_data(serial=f'fixture-{i}', pending=1, crc=100), [f'data{i}'], 0) for i in range(20)]
        self.health['critical'] = True
        with self.assertRaises(maintenance.ArrayHold):
            self.controller.tick(self.at(10, 2, 1))
        reports = [call for call in self.send.call_args_list if 'CRITICAL DISK HEALTH' in call.args[2]]
        self.assertGreater(len(reports), 1)
        self.assertTrue(all(len(call.args[3]) <= 1024 for call in reports))
        self.assertTrue(all(call.kwargs['priority'] == 1 for call in reports))
        combined = '\n'.join(call.args[3] for call in reports)
        for i in range(20):
            self.assertIn(f'data{i} ', combined)

    def test_coverage_and_smart_follow_the_guarded_array_and_preserve_hysteresis(self):
        self.precheck['config_path'] = '/etc/snapraid.conf'
        with patch('maintenance.read_snapraid_status', side_effect=[report(84), report(70)]) as read, patch('maintenance.check_smart_health', return_value=self.good_health) as health:
            self.controller.tick(self.at(10, 2, 1))
        self.assertTrue(all(call.args[0] == '/etc/snapraid.conf' for call in read.call_args_list))
        self.assertEqual(health.call_args.args[0], '/etc/snapraid.conf')
        self.assertTrue(self.controller.state['scrub_policy']['active'])
        self.reload()
        with patch('maintenance.read_snapraid_status', side_effect=[report(10), report(5)]):
            self.controller.tick(self.at(11, 2, 1))
        self.assertIn('scrub', self.controller.state['jobs'])
        self.assertFalse(self.controller.state['scrub_policy']['active'])

    def test_limits_configuration_is_bounded_and_percent_is_frozen_per_session(self):
        for key, value in (('diff_max_changed_percent', 0), ('diff_max_changed_percent', True),
                           ('pre_sync_timeout_seconds', 1), ('smart_timeout_seconds', 121)):
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.RemoteCoordinator(self.omv, {**self.connection, key: value})
        self.controller.tick(self.at(10, 2))
        self.reload()
        self.controller.diff_percent = 1
        with patch('maintenance.pre_sync_check', return_value=self.good_precheck) as check:
            self.controller.tick(self.at(10, 2, 1))
        check.assert_called_once_with(5, 900, '')
