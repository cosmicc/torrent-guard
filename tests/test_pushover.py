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
                                    ('maintenance.snapraid_status', lambda config_path='': 'SnapRAID status:\nNo errors.')):
            context = patch(target, replacement)
            context.start()
            self.addCleanup(context.stop)

    def test_daily_start_and_finish_follow_confirmed_stop_and_restore(self):
        self.controller.tick(self.at(8, 2))
        self.assertFalse(self.alerts)
        self.controller.tick(self.at(8, 2, 1))
        self.assertEqual(len(self.alerts), 2)
        self.assertIn('sync starting', self.alerts[0][0])
        self.assertEqual(self.alerts[0][2], [False, False])
        self.assertEqual(self.alerts[1][2], [True, True])
        self.assertIn('No errors.', self.alerts[1][1])
        self.controller = maintenance.RemoteCoordinator(self.omv, {**self.connection, 'pushover': {
            'app_token': 'a' * 30, 'user_key': 'b' * 30}})
        self.controller.tick(self.at(8, 3))
        self.assertEqual(len(self.alerts), 2)

    def test_monday_transition_keeps_apps_stopped_and_final_has_status(self):
        self.controller.tick(self.at(9, 2, 1))
        self.assertEqual(len(self.alerts), 3)
        self.assertIn('scrub starting', self.alerts[1][0])
        self.assertEqual(self.alerts[1][2], [False, False])
        self.assertEqual(self.alerts[2][2], [True, True])
        self.assertIn('weekly scrub', self.alerts[2][1])
        self.assertIn('No errors.', self.alerts[2][1])

    def test_delivery_failure_does_not_hold_successful_jobs_or_restoration(self):
        with patch('maintenance.send_pushover', return_value=False) as send:
            self.controller.tick(self.at(9, 2, 1))
        self.assertEqual(send.call_count, 3)
        self.assertFalse(self.controller.state['active'])
        self.assertTrue(all(row['State']['Running'] for row in self.rows.values()))

    def test_failed_job_alerts_once_and_never_reports_success(self):
        self.returncode = 1
        for _ in range(2):
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(8, 2, 1))
        # First failure and subsequent interrupted-job warning are distinct.
        count = len(self.alerts)
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 2, 1))
        self.assertEqual(len(self.alerts), count)
        self.assertTrue(all('maintenance complete' not in alert[0] for alert in self.alerts))
        self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))

    def test_lost_finish_ack_does_not_announce_completion_until_confirmed(self):
        self.lose_finish = True
        with self.assertRaises(maintenance.MaintenanceError):
            self.controller.tick(self.at(8, 2, 1))
        self.assertFalse(any('maintenance complete' in alert[0] for alert in self.alerts))
        self.controller.tick(self.at(8, 2, 2))
        self.assertEqual(sum('maintenance complete' in alert[0] for alert in self.alerts), 1)
        self.assertEqual(len(self.jobs), 1)

    def test_enabling_alerts_does_not_emit_a_historical_completion(self):
        self.controller.notification_keys = ('', '')
        self.controller.tick(self.at(8, 2, 1))
        self.controller.notification_keys = ('a'*30, 'b'*30)
        self.controller.tick(self.at(8, 3))
        self.assertFalse(self.alerts)

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
            self.assertEqual(kwargs['timeout'], 20)
            kwargs['stdout'].write(b'x'*9000 + b'\nNo errors.\n')
            return subprocess.CompletedProcess(command, 2)
        with patch('maintenance.subprocess.run', side_effect=status):
            text = maintenance.snapraid_status()
        self.assertIn('exit 2', text)
        self.assertTrue(text.endswith('No errors.'))
        self.assertLess(len(text), 750)
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('snapraid', 20)):
            with patch('maintenance.subprocess.run', side_effect=error):
                self.assertIn('unavailable', maintenance.snapraid_status())

    def test_optional_setup_uses_hidden_readers_and_validates_pairs(self):
        ask, secret, output = Mock(return_value='y'), Mock(side_effect=['a'*30, 'b'*30]), Mock()
        self.assertEqual(prompt_pushover(ask, secret, output), {'app_token': 'a'*30, 'user_key': 'b'*30})
        self.assertEqual(secret.call_count, 2)
        self.assertNotIn('a'*30, str(output.call_args_list))
        self.assertEqual(prompt_pushover(Mock(return_value=''), secret, output), {})
        with self.assertRaises(InstallError):
            prompt_pushover(ask, Mock(return_value=''), output)
