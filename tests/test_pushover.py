"""Pushover delivery, maintenance ordering and optional secret setup regressions."""
import subprocess
import unittest
from unittest.mock import Mock, patch
import urllib.parse

import maintenance
from scripts.app_install import prompt_pushover
from scripts.maintenance_install import InstallError
import test_maintenance_remote as remote


class NotificationSequenceTests(remote.RemoteMaintenanceTests):
    """Use real two-host journals with simulated SSH, Docker and Pushover."""

    def setUp(self):
        super().setUp()
        self.controller.notification_keys = ('a' * 30, 'b' * 30)
        self.alerts = []

        def deliver(token, user, title, message):
            self.alerts.append((title, message, [row['State']['Running'] for row in self.rows.values()]))
            return True

        for target, replacement in (('maintenance.send_pushover', deliver),
                                    ('maintenance.snapraid_status', lambda config_path='', timeout_seconds=120: 'SnapRAID status:\nNo errors.')):
            context = patch(target, replacement)
            context.start()
            self.addCleanup(context.stop)

    def test_daily_start_and_finish_follow_confirmed_stop_and_restore(self):
        self.controller.tick(self.at(8, 2))
        self.assertFalse(self.alerts)
        self.controller.tick(self.at(8, 2, 1))
        self.assertEqual(len(self.alerts), 3)
        self.assertIn('sync starting', self.alerts[0][0])
        self.assertEqual(self.alerts[0][2], [False, False])
        self.assertEqual(self.alerts[1][2], [True, True])
        self.assertIn('No error detected.', self.alerts[1][1])
        self.controller = maintenance.RemoteCoordinator(self.omv, {**self.connection, 'pushover': {
            'app_token': 'a' * 30, 'user_key': 'b' * 30}})
        self.controller.tick(self.at(8, 3))
        self.assertEqual(len(self.alerts), 3)

    def test_monday_transition_keeps_apps_stopped_and_final_has_status(self):
        self.controller.tick(self.at(9, 2, 1))
        self.assertEqual(len(self.alerts), 4)
        self.assertIn('scrub starting', self.alerts[1][0])
        self.assertEqual(self.alerts[1][2], [False, False])
        self.assertEqual(self.alerts[2][2], [True, True])
        self.assertIn('weekly scrub', self.alerts[2][1])
        self.assertIn('No error detected.', self.alerts[2][1])

    def test_delivery_failure_does_not_hold_successful_jobs_or_restoration(self):
        with patch('maintenance.send_pushover', return_value=False) as send:
            self.controller.tick(self.at(9, 2, 1))
        self.assertEqual(send.call_count, 4)
        self.assertFalse(self.controller.state['active'])
        self.assertTrue(all(row['State']['Running'] for row in self.rows.values()))

    def test_failed_job_alerts_once_and_never_reports_success(self):
        self.returncode = 1
        for _ in range(2):
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(8, 2, 1))
        # Later interrupted-job ticks preserve/deduplicate the original failure.
        count = len(self.alerts)
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 2, 1))
        self.assertEqual(len(self.alerts), count)
        self.assertTrue(all('maintenance complete' not in alert[0] for alert in self.alerts))
        self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))

    def test_failure_alert_has_code_session_live_apps_recovery_and_status(self):
        self.returncode = 23
        with patch('maintenance.snapraid_status', return_value='SnapRAID status:\n' + 'x'*900 + '\nSUMMARY: array needs attention.'):
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(8, 2, 1))
        failures = [alert for alert in self.alerts if 'needs attention' in alert[0]]
        self.assertEqual(len(failures), 1)
        message = failures[0][1]
        self.assertIn('2026-11-08', message)
        self.assertIn(self.controller.state['session'], message)
        self.assertIn('sync=failed', message)
        self.assertIn('exited with status 23', message)
        self.assertIn('Sonarr: stopped; Radarr: stopped', message)
        self.assertIn('upgrade blocked', message)
        self.assertIn('retry --confirm-jobs-stopped', message)
        self.assertIn('no SnapRAID/guarded jobs are running', message)
        self.assertIn('SUMMARY: array needs attention.', message)
        self.assertLessEqual(len(message), 1024)
        saved = self.controller.state['job_failure'].copy()
        self.controller = maintenance.RemoteCoordinator(self.omv, {**self.connection, 'pushover': {
            'app_token': 'a'*30, 'user_key': 'b'*30}})
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 3))
        self.assertEqual(self.controller.state['job_failure'], saved)
        self.assertEqual(len([alert for alert in self.alerts if 'needs attention' in alert[0]]), 1)

    def test_status_request_is_read_only_and_checks_container_identity(self):
        self.controller.tick(self.at(8, 2))
        before = (self.agent / 'state.json').read_bytes()
        calls = len(self.calls)
        result = maintenance.agent_request('status ' + self.controller.state['session'], self.agent, self.marker)
        self.assertFalse(result['containers']['sonarr']['running'])
        self.assertEqual((self.agent / 'state.json').read_bytes(), before)
        self.assertTrue(all(call[:2] == ('container', 'inspect') for call in self.calls[calls:]))
        self.rows['sonarr']['Id'] = 'replacement'
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.agent_request('status ' + self.controller.state['session'], self.agent, self.marker)

    def test_unavailable_remote_status_does_not_change_original_failure(self):
        original = self.controller.remote
        def remote_status(action):
            if action == 'status':
                raise maintenance.MaintenanceError('private connection details')
            return original(action)
        self.returncode = 2
        with patch.object(self.controller, 'remote', side_effect=remote_status):
            with self.assertRaisesRegex(maintenance.MaintenanceError, 'status 2'):
                self.controller.tick(self.at(8, 2, 1))
        message = self.alerts[-1][1]
        self.assertIn('current state unknown', message)
        self.assertNotIn('private connection details', message)
        self.assertTrue(self.controller.state['active'])

    def test_launch_failure_preserves_fixed_reason_without_exporting_exception(self):
        process = self.process
        def launch(command, **kwargs):
            if command == [self.paths['sync']]:
                raise OSError('private environment information')
            return process(command, **kwargs)
        with patch('maintenance.subprocess.run', side_effect=launch):
            with self.assertRaisesRegex(maintenance.MaintenanceError, 'Could not execute'):
                self.controller.tick(self.at(8, 2, 1))
        self.assertEqual(self.controller.state['job_failure']['kind'], 'launch')
        self.assertIn('script could not be launched', self.alerts[-1][1])
        self.assertNotIn('private environment information', self.alerts[-1][1])
        self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))

    def test_failure_after_authorized_retry_alerts_again_without_repeating_each_tick(self):
        self.returncode = 3
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 2, 1))
        self.controller.state['jobs']['sync'] = 'pending'  # Model the existing operator retry command.
        self.controller.save()
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 3))
        self.assertEqual(self.controller.state['job_attempts']['sync'], 2)
        self.assertEqual(len([alert for alert in self.alerts if 'needs attention' in alert[0]]), 2)

    def test_legacy_failed_journal_reports_unavailable_code_without_guessing(self):
        self.controller.tick(self.at(8, 2))
        self.controller.state['jobs']['sync'] = 'failed'
        self.controller.save()
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 3))
        self.assertIn('original exit code unavailable', self.alerts[-1][1])
        self.assertIn('Sonarr: stopped', self.alerts[-1][1])

    def test_lost_finish_ack_does_not_announce_completion_until_confirmed(self):
        self.lose_finish = True
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 2, 1))
        self.assertFalse(any('maintenance complete' in alert[0] for alert in self.alerts))
        self.assertIn('Sonarr: running; Radarr: running', self.alerts[-1][1])
        self.controller.tick(self.at(8, 2, 2))
        self.assertEqual(sum('maintenance complete' in alert[0] for alert in self.alerts), 1)
        self.assertEqual(len(self.jobs), 1)

    def test_enabling_alerts_does_not_emit_a_historical_completion(self):
        self.controller.notification_keys = ('', '')
        self.controller.tick(self.at(8, 2, 1))
        self.controller.notification_keys = ('a'*30, 'b'*30)
        self.controller.tick(self.at(8, 3))
        self.assertFalse(self.alerts)

    def test_status_timeout_configuration_is_bounded_and_used_by_alerts(self):
        for value in (10, 120, 300, 600):
            controller = maintenance.RemoteCoordinator(self.omv, {**self.connection,
                'snapraid_status_timeout_seconds': value})
            self.assertEqual(controller.snapraid_status_timeout, value)
        for value in (True, 0, 9, 601, '300', None, float('inf')):
            with self.assertLogs('maintenance'):
                controller = maintenance.RemoteCoordinator(self.omv, {**self.connection,
                    'snapraid_status_timeout_seconds': value})
            self.assertEqual(controller.snapraid_status_timeout, 120)
        self.controller.snapraid_status_timeout = 300
        with patch('maintenance.read_snapraid_status', return_value=self.coverage_report) as status:
            self.controller.tick(self.at(8, 2, 1))
        status.assert_called_once_with('', 300)

    def test_disabled_and_invalid_credentials_do_not_block_jobs(self):
        for settings in ({}, {'app_token': 'bad'}, {'user_key': 123}, 'bad'):
            with self.subTest(settings=settings):
                controller = maintenance.RemoteCoordinator(self.omv, {**self.connection, 'pushover': settings})
                self.assertEqual(controller.notification_keys, ('', ''))


class DeliveryTests(unittest.TestCase):
    """No live keys, network calls or SnapRAID work in notification tests."""

    def test_https_body_and_acknowledgement_are_bounded(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"status":1}'
        with patch('maintenance.http.client.HTTPSConnection', return_value=connection) as factory:
            self.assertTrue(maintenance.send_pushover('a'*30, 'b'*30, 'Title', 'x'*2000))
        factory.assert_called_once_with('api.pushover.net', timeout=10)
        args = connection.request.call_args.args
        self.assertEqual(args[:2], ('POST', '/1/messages.json'))
        values = urllib.parse.parse_qs(args[2])
        self.assertEqual(len(values['message'][0]), 1024)
        self.assertNotIn('token=', args[1])
        response.read.assert_called_once_with(4097)
        connection.close.assert_called_once()

    def test_errors_redact_response_and_never_follow_redirects(self):
        for status, raw in ((302, b'private-token'), (200, b'{"status":0,"errors":["private-token"]}'),
                            (200, b'[]'), (200, b'bad-json'), (200, b'x'*4097)):
            connection = Mock()
            connection.getresponse.return_value.status = status
            connection.getresponse.return_value.read.return_value = raw
            with patch('maintenance.http.client.HTTPSConnection', return_value=connection), self.assertLogs('maintenance') as logs:
                self.assertFalse(maintenance.send_pushover('private-token', 'user', 'Title', 'Message'))
            self.assertNotIn('private-token', str(logs.output))
            self.assertEqual(connection.request.call_count, 1)
        with patch('maintenance.http.client.HTTPSConnection') as factory, self.assertLogs('maintenance'):
            factory.return_value.request.side_effect = TimeoutError('private-token')
            self.assertFalse(maintenance.send_pushover('token', 'user', 'Title', 'Message'))

    def test_custom_status_configuration_uses_argv_without_shell(self):
        def status(command, **kwargs):
            self.assertEqual(command, ['/usr/bin/snapraid', '-c', '/etc/snapraid/array.conf', 'status'])
            self.assertNotIn('shell', kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch('maintenance.subprocess.run', side_effect=status):
            self.assertIn('No output', maintenance.snapraid_status('/etc/snapraid/array.conf'))

    def test_status_output_timeout_and_nonzero_are_informational(self):
        def status(command, **kwargs):
            self.assertEqual(command, ['/usr/bin/snapraid', 'status'])
            self.assertEqual(kwargs['timeout'], 120)
            kwargs['stdout'].write(b'x'*9000 + b'\nNo errors.\n')
            return subprocess.CompletedProcess(command, 2)
        with patch('maintenance.subprocess.run', side_effect=status):
            text = maintenance.snapraid_status()
        self.assertIn('exit 2', text)
        self.assertTrue(text.endswith('No errors.'))
        self.assertLess(len(text), 750)
        for error, expected in ((FileNotFoundError(2, 'private-path'), 'could not start'),
                                (PermissionError(13, 'private-path'), 'permission denied')):
            with patch('maintenance.subprocess.run', side_effect=error):
                text = maintenance.snapraid_status()
                self.assertIn(expected, text)
                self.assertNotIn('private-path', text)


    def test_timeout_preserves_partial_report_and_identifies_actual_limit(self):
        def slow_status(command, **kwargs):
            self.assertEqual(kwargs['timeout'], 300)
            kwargs['stdout'].write(b'Loading state...\nDisk report still loading.\n')
            raise subprocess.TimeoutExpired(command, 300)
        with patch('maintenance.subprocess.run', side_effect=slow_status):
            text = maintenance.snapraid_status(timeout_seconds=300)
        self.assertIn('timed out after 300 seconds (partial report)', text)
        self.assertIn('Disk report still loading.', text)
        self.assertNotIn('could not start', text)

    def test_temporary_storage_error_is_distinct_from_launch_or_timeout(self):
        with patch('maintenance.tempfile.TemporaryFile', side_effect=OSError(28, 'private-path')):
            text = maintenance.snapraid_status()
        self.assertIn('OS error 28', text)
        self.assertIn('temporary storage', text)
        self.assertNotIn('private-path', text)

    def test_optional_setup_uses_hidden_readers_and_validates_pairs(self):
        ask, secret, output = Mock(return_value='y'), Mock(side_effect=['a'*30, 'b'*30]), Mock()
        self.assertEqual(prompt_pushover(ask, secret, output), {'app_token': 'a'*30, 'user_key': 'b'*30})
        self.assertEqual(secret.call_count, 2)
        self.assertNotIn('a'*30, str(output.call_args_list))
        self.assertEqual(prompt_pushover(Mock(return_value=''), secret, output), {})
        with self.assertRaises(InstallError):
            prompt_pushover(ask, Mock(return_value=''), output)
