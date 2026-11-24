"""Automatic scrub scheduling uses complete evidence and restart-safe session plans."""
import unittest
from unittest.mock import Mock, patch

import maintenance
import test_maintenance_remote as fixtures


def report(percent: int, *, complete: bool = True) -> maintenance.SnapraidStatus:
    """Build a complete status fixture without any production array data."""
    return maintenance.SnapraidStatus(
        text=f"SnapRAID status report:\nNo sync is in progress.\n{percent}% of the array is not scrubbed.\nNo error detected.",
        complete=complete)


class CoverageParsingTests(unittest.TestCase):
    """Partial, contradictory and unhealthy output cannot authorize extra work."""

    def test_current_legacy_and_zero_coverage_formats(self):
        for percent in (0, 5, 20, 21, 84, 100):
            self.assertEqual(report(percent).unscrubbed_percent(), percent)
            legacy = report(percent).text.replace(f'{percent}%', f'The {percent}%')
            self.assertEqual(maintenance.SnapraidStatus(text=legacy, complete=True).unscrubbed_percent(), percent)
        for line in ('The full array was scrubbed at least one time.',
                     'The entire array has been scrubbed at least once.'):
            text = report(0).text.replace('0% of the array is not scrubbed.', line)
            self.assertEqual(maintenance.SnapraidStatus(text=text, complete=True).unscrubbed_percent(), 0)

    def test_incomplete_unhealthy_ambiguous_or_out_of_range_is_unknown(self):
        good = report(84)
        cases = [report(5, complete=False), report(101),
                 maintenance.SnapraidStatus(text=good.text.replace('No error detected.', ''), complete=True),
                 maintenance.SnapraidStatus(text=good.text.replace('No sync is in progress.', 'You have a sync in progress at 99%.'), complete=True),
                 maintenance.SnapraidStatus(text=good.text + '\nDANGER! bad blocks', complete=True),
                 maintenance.SnapraidStatus(text=good.text + '\n5% of the array is not scrubbed.', complete=True),
                 maintenance.SnapraidStatus(text=good.text + '\nThe full array was scrubbed at least one time.', complete=True)]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertIsNone(evidence.unscrubbed_percent())

    def test_timed_out_nonzero_or_truncated_process_output_is_not_policy_evidence(self):
        def process(command, **kwargs):
            kwargs['stdout'].write(report(84).text.encode())
            return Mock(returncode=1)
        with patch('maintenance.subprocess.run', side_effect=process):
            self.assertIsNone(maintenance.read_snapraid_status().unscrubbed_percent())
        def partial(command, **kwargs):
            kwargs['stdout'].write(report(5).text.encode())
            raise maintenance.subprocess.TimeoutExpired(command, 120)
        with patch('maintenance.subprocess.run', side_effect=partial):
            evidence = maintenance.read_snapraid_status()
        self.assertIsNone(evidence.unscrubbed_percent())
        self.assertIn('partial report', evidence.summary())
        def oversized(command, **kwargs):
            kwargs['stdout'].write(b'x'*65537 + b'\n' + report(84).text.encode())
            return Mock(returncode=0)
        with patch('maintenance.subprocess.run', side_effect=oversized):
            self.assertIsNone(maintenance.read_snapraid_status().unscrubbed_percent())


class CatchupTests(fixtures.RemoteMaintenanceTests):
    """Run real journals/agent holds while replacing only remote APIs and status."""

    def reload(self, **settings):
        self.controller = maintenance.RemoteCoordinator(self.omv, {**self.connection, **settings})

    def run_with_reports(self, day, *percentages):
        values = [value if isinstance(value, maintenance.SnapraidStatus) else report(value) for value in percentages]
        with patch('maintenance.read_snapraid_status', side_effect=values) as read:
            self.controller.tick(self.at(day, 2, 1))
        self.assertEqual(read.call_count, len(values))

    def test_hysteresis_survives_restart_and_returns_to_weekly(self):
        self.run_with_reports(10, 84, 76)
        self.assertTrue(self.controller.state['scrub_policy']['active'])
        self.assertEqual(self.controller.state['scrub_reason'], 'catchup')
        self.assertFalse(self.controller.state['active'])
        self.assertTrue(all(row['State']['Running'] for row in self.rows.values()))
        self.reload()
        self.run_with_reports(11, 10, 5)
        self.assertFalse(self.controller.state['scrub_policy']['active'])
        self.assertIn('returning to weekly', self.controller.state['scrub_policy_detail'])
        self.reload()
        self.run_with_reports(12, 15)
        self.assertEqual(self.controller.state['jobs'], {'sync': 'complete'})
        self.reload()
        self.run_with_reports(13, 20)
        self.assertEqual(self.controller.state['jobs'], {'sync': 'complete'})
        self.reload()
        self.run_with_reports(14, 21, 4)
        self.assertEqual(self.controller.state['jobs']['scrub'], 'complete')
        self.assertFalse(self.controller.state['scrub_policy']['active'])

    def test_weekly_monday_remains_when_catchup_inactive(self):
        self.run_with_reports(9, 10, 2)
        self.assertEqual(self.jobs, [self.paths['sync'], self.paths['scrub']])
        self.assertNotEqual(self.controller.state.get('scrub_reason'), 'catchup')
        self.assertFalse(self.controller.state['scrub_policy']['active'])

    def test_unknown_coverage_never_starts_extra_scrub_or_certifies_caught_up(self):
        self.run_with_reports(10, 84, 60)
        self.reload()
        self.run_with_reports(11, report(0, complete=False))
        self.assertTrue(self.controller.state['scrub_policy']['active'])
        self.assertEqual(self.controller.state['jobs'], {'sync': 'complete'})
        self.assertFalse(self.controller.state['active'])
        self.assertTrue(self.controller.state['scrub_status_notice'])
        self.reload()
        self.run_with_reports(12, 10, 5)
        self.assertFalse(self.controller.state['scrub_policy']['active'])
        self.assertFalse(self.controller.state['scrub_policy']['status_problem'])

    def test_bad_status_preserves_baseline_weekly_scrub_and_warns_once(self):
        self.run_with_reports(9, report(84, complete=False), report(84, complete=False))
        self.assertEqual(self.controller.state['jobs']['scrub'], 'complete')
        self.assertTrue(self.controller.state['scrub_status_notice'])
        self.reload()
        self.run_with_reports(10, report(84, complete=False))
        self.assertFalse(self.controller.state['scrub_status_notice'])
        self.assertEqual(self.controller.state['jobs'], {'sync': 'complete'})

    def test_one_extra_scrub_per_day_and_no_redecision_on_idle_ticks(self):
        self.run_with_reports(10, 84, 80)
        with patch('maintenance.read_snapraid_status') as read:
            self.controller.tick(self.at(10, 4))
        read.assert_not_called()
        self.assertEqual(len(self.jobs), 2)

    def test_failed_sync_never_checks_coverage_or_schedules_extra_work(self):
        self.returncode = 23
        with patch('maintenance.read_snapraid_status') as read:
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(10, 2, 1))
        read.assert_not_called()
        self.assertEqual(self.controller.state['jobs'], {'sync': 'failed'})
        self.assertTrue(self.controller.state['active'])
        self.assertFalse(any(row['State']['Running'] for row in self.rows.values()))

    def test_old_active_session_plan_is_not_rewritten_on_upgrade(self):
        self.controller.tick(self.at(10, 2))
        for key in ('scrub_policy', 'scrub_policy_pending', 'scrub_thresholds'):
            self.controller.state.pop(key)
        self.controller.save()
        self.reload()
        with patch('maintenance.read_snapraid_status') as read:
            self.controller.tick(self.at(10, 2, 1))
        read.assert_not_called()
        self.assertEqual(self.jobs, [self.paths['sync']])
        self.assertFalse(self.controller.state['active'])

    def test_crash_after_sync_resumes_pending_policy_without_sync_repeat(self):
        with patch.object(self.controller, 'plan_scrub', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.controller.tick(self.at(10, 2, 1))
        self.assertTrue(self.controller.state['scrub_policy_pending'])
        self.assertEqual(self.jobs, [self.paths['sync']])
        self.reload()
        self.run_with_reports(11, 84, 50)
        self.assertEqual(self.jobs, [self.paths['sync'], self.paths['scrub']])

    def test_frozen_extra_scrub_survives_restart_and_configuration_change(self):
        original = self.controller.run_job
        def interrupt(job):
            if job == 'scrub':
                raise maintenance.MaintenanceError('interrupted before launch')
            original(job)
        with patch.object(self.controller, 'run_job', side_effect=interrupt), patch('maintenance.read_snapraid_status', return_value=report(84)):
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(10, 2, 1))
        self.assertEqual(self.controller.state['jobs']['scrub'], 'pending')
        self.reload(scrub_catchup_start_percent=95)
        self.run_with_reports(11, 4)
        self.assertEqual(self.jobs, [self.paths['sync'], self.paths['scrub']])
        self.assertEqual(self.controller.state['scrub_thresholds']['start'], 20)

    def test_crash_after_scrub_resumes_observation_without_reexecuting_jobs(self):
        with patch.object(self.controller, 'observe_scrub', side_effect=OSError('interrupted')), patch('maintenance.read_snapraid_status', return_value=report(84)):
            with self.assertRaises(OSError):
                self.controller.tick(self.at(10, 2, 1))
        self.assertTrue(self.controller.state['scrub_result_pending'])
        self.reload()
        self.run_with_reports(11, 5)
        self.assertFalse(self.controller.state['scrub_policy']['active'])
        self.assertEqual(len(self.jobs), 2)

    def test_lost_finish_ack_retries_only_restoration_not_status_or_jobs(self):
        self.lose_finish = True
        with patch('maintenance.read_snapraid_status', side_effect=[report(84), report(5)]):
            with self.assertRaises(maintenance.MaintenanceError):
                self.controller.tick(self.at(10, 2, 1))
        stops = sum(call[0] == 'stop' for call in self.calls)
        self.reload()
        with patch('maintenance.read_snapraid_status') as read:
            self.controller.tick(self.at(11, 3))
        read.assert_not_called()
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(sum(call[0] == 'stop' for call in self.calls), stops)

    def test_three_stalled_runs_alert_once_and_actual_progress_resets_incident(self):
        for day in (10, 11, 12):
            self.run_with_reports(day, 84, 84)
            if day < 12:
                self.reload()
        self.assertEqual(self.controller.state['scrub_policy']['stalled_runs'], 3)
        self.assertTrue(self.controller.state['scrub_stalled_notice'])
        self.assertTrue(self.controller.state['scrub_policy']['active'])
        self.reload()
        self.run_with_reports(13, 84, 84)
        self.assertFalse(self.controller.state.get('scrub_stalled_notice', False))
        self.reload()
        self.run_with_reports(14, 84, 70)
        self.assertEqual(self.controller.state['scrub_policy']['stalled_runs'], 0)
        self.assertFalse(self.controller.state['scrub_policy']['stalled_alerted'])

    def test_policy_threshold_settings_require_valid_integers(self):
        for start, stop in ((20, 5), (80, 10), (100, 0)):
            self.reload(scrub_catchup_start_percent=start, scrub_catchup_stop_percent=stop)
            self.assertEqual(self.controller.scrub_thresholds, {'start': start, 'stop': stop})
        for start, stop in ((True, 5), (20, 20), (5, 20), (101, 0), (20, -1), ('20', 5)):
            with self.assertRaises(maintenance.MaintenanceError):
                self.reload(scrub_catchup_start_percent=start, scrub_catchup_stop_percent=stop)

    def test_different_configured_array_starts_with_fresh_catchup_state(self):
        self.run_with_reports(10, 84, 60)
        self.reload(snapraid_config='/etc/snapraid/other.conf')
        self.run_with_reports(11, 10)
        self.assertFalse(self.controller.state['scrub_policy']['active'])
        self.assertEqual(self.controller.state['jobs'], {'sync': 'complete'})

    def test_notifications_report_start_progress_return_to_weekly_and_stall(self):
        self.controller.notification_keys = ('a'*30, 'b'*30)
        with patch('maintenance.send_pushover', return_value=True) as send:
            self.run_with_reports(10, 84, 5)
        messages = [call.args[3] for call in send.call_args_list]
        self.assertTrue(any('Daily catch-up started: 84%' in message for message in messages))
        self.assertTrue(any('84% -> 5%' in message and 'returning to weekly' in message for message in messages))
        self.assertEqual(len(messages), 3)
