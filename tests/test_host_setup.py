"""Two-host setup and upgrade regressions; privileged operations are simulated."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.app_install import AppInstall, dotenv_value, read_values, save_env
from scripts.enroll_maintenance_key import enroll
from scripts.maintenance_install import FORCED
from scripts.host_setup import MODEL, SCHEDULE, UpgradeInstaller, configure_omv_schedule, install_omv, update_checkout
from scripts.maintenance_install import HELPER, SOURCE, InstallError
import test_maintenance_install as fixtures


class ApplicationSetupTests(unittest.TestCase):
    """Use real env files and fake Docker commands without starting services."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.state = self.root / 'saved-state'
        self.state.mkdir()
        self.values = read_values((Path(__file__).parents[1] / '.env.example').read_text())
        self.values.update(QBIT_PASSWORD='literal$secret', SONARR_API_KEY='sonarr-fixture', RADARR_API_KEY='radarr-fixture',
                           DOWNLOADS_HOST_PATH=str(self.downloads), CLEANUP_COMPLETE_HOST_PATH=str(self.downloads / 'complete'),
                           STATE_HOST_PATH=str(self.state))
        (self.root / '.env.example').write_text(''.join(k+'='+v+'\n' for k,v in self.values.items()))
        self.run = Mock(side_effect=self.command)
        self.ask = Mock(side_effect=AssertionError('Unneeded prompt'))
        self.app = AppInstall(self.root, self.run, self.ask, self.ask, Mock())
        self.container = {'Config': {'Env': [k+'='+v for k,v in self.values.items()], 'User': '1200:1201',
                                    'Labels': {'com.docker.compose.project.config_files': '/old/compose.yaml,/old/compose.cleanup.yaml,/old/compose.orphans.yaml'}},
                          'Mounts': [{'Type': 'bind', 'Source': str(self.state), 'Destination': '/state'}],
                          'HostConfig': {'PortBindings': {'8080/tcp': [{'HostIp': '192.0.2.2', 'HostPort': '8089'}]}}}
        self.present = True

    def command(self, args, **kwargs):
        if args[:3] == ['docker', 'container', 'ls']:
            return 'torrent-guard\n' if self.present else ''
        if args[:3] == ['docker', 'container', 'inspect']:
            return json.dumps([self.container])
        if 'config' in args:
            return json.dumps({'services': {'torrent-guard': {'environment': self.values}}})
        return ''

    def saved_env(self, **extra):
        values = {**self.values, **extra}
        text = '# Keep my comments and credentials\n' + ''.join(k+'='+dotenv_value(v)+'\n' for k,v in values.items())
        (self.root / '.env').write_text(text)
        return text

    def test_existing_env_and_profiles_survive_upgrade_without_prompts(self):
        before = self.saved_env(COMPOSE_FILE='compose.yaml:compose.cleanup.yaml:compose.orphans.yaml')
        sentinel = self.state / 'torrent-guard.sqlite3'
        sentinel.write_bytes(b'existing journal')
        self.app.deploy()
        self.assertEqual((self.root / '.env').read_text(), before)
        self.assertEqual(sentinel.read_bytes(), b'existing journal')
        self.ask.assert_not_called()
        commands = [call.args[0] for call in self.run.call_args_list]
        build = next(i for i,c in enumerate(commands) if 'build' in c)
        up = next(i for i,c in enumerate(commands) if 'up' in c)
        self.assertLess(build, up)
        self.assertIn('--wait', commands[up])
        self.assertEqual(commands[up][-1], 'torrent-guard')
        for name in ('compose.cleanup.yaml', 'compose.orphans.yaml'):
            self.assertIn(str(self.root / name), commands[up])
        self.assertFalse(any('down' in c for c in commands))

    def test_new_clone_recovers_state_ids_port_credentials_and_overrides(self):
        self.app.deploy()
        values = read_values((self.root / '.env').read_text())
        self.assertEqual(values['STATE_HOST_PATH'], str(self.state))
        self.assertEqual(values['GUARD_UID'], '1200')
        self.assertEqual(values['GUARD_GID'], '1201')
        self.assertEqual(values['STATUS_BIND_ADDRESS'], '192.0.2.2')
        self.assertEqual(values['QBIT_PASSWORD'], 'literal$secret')
        self.assertIn('compose.orphans.yaml', values['COMPOSE_FILE'])
        self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)
        self.ask.assert_not_called()

    def test_legacy_env_without_state_or_profile_adds_only_bookkeeping(self):
        before = self.saved_env()
        before = ''.join(line for line in before.splitlines(keepends=True) if not line.startswith('STATE_HOST_PATH='))
        (self.root / '.env').write_text(before)
        self.app.deploy()
        after = (self.root / '.env').read_text()
        for line in before.splitlines():
            self.assertIn(line, after.splitlines())
        self.assertEqual(read_values(after)['STATE_HOST_PATH'], str(self.state))

    def test_custom_override_and_state_switch_refuse_before_build(self):
        for extra in ({'COMPOSE_FILE': 'compose.yaml:custom.yaml'}, {'STATE_HOST_PATH': str(self.downloads)}):
            self.saved_env(**extra)
            self.run.reset_mock()
            with self.assertRaises(InstallError):
                self.app.deploy()
            self.assertFalse(any('build' in call.args[0] for call in self.run.call_args_list))

    def test_missing_download_mount_is_not_created(self):
        missing = self.root / 'not-mounted'
        self.saved_env(DOWNLOADS_HOST_PATH=str(missing))
        with self.assertRaisesRegex(InstallError, 'mount is unavailable'):
            self.app.deploy()
        self.assertFalse(missing.exists())

    def test_named_state_volume_is_not_silently_replaced(self):
        self.container['Mounts'][0]['Type'] = 'volume'
        with self.assertRaisesRegex(InstallError, 'bind mount'):
            self.app.configure(self.container)
        self.assertFalse((self.root / '.env').exists())

    def test_root_guard_identity_is_refused(self):
        self.saved_env(GUARD_UID='0')
        with self.assertRaisesRegex(InstallError, 'non-root'):
            self.app.deploy()
        self.assertFalse(any('build' in c.args[0] for c in self.run.call_args_list))

    def test_first_install_retains_dry_run_unless_authorized(self):
        self.present = False
        self.ask.side_effect = ['', '']
        self.app.deploy()
        values = read_values((self.root / '.env').read_text())
        self.assertEqual(values['DRY_RUN'], 'true')
        self.assertEqual(values['ORPHAN_CLEANUP'], 'false')
        self.assertEqual(self.ask.call_count, 2)

    def test_new_credential_quote_round_trip_and_atomic_conflict(self):
        value = 'dollar$HOME${VALUE}"back\\slash\nline'
        self.assertEqual(read_values('QBIT_PASSWORD='+dotenv_value(value))['QBIT_PASSWORD'], value)
        path = self.root / '.env'
        path.write_text('existing secret')
        with self.assertRaisesRegex(InstallError, 'changed during'):
            save_env(path, 'replacement', 'stale text', (os.getuid(), os.getgid()))
        self.assertEqual(path.read_text(), 'existing secret')


class ScheduleTests(unittest.TestCase):
    """Model OMV's native configuration database, including interrupted updates."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installer = Mock()
        self.installer.paths.path.side_effect = lambda value: self.root / value.lstrip('/')
        self.receipt = self.installer.paths.path('/var/lib/torrent-guard-maintenance/setup.json')
        self.receipt.parent.mkdir(parents=True)
        self.jobs = [{'uuid': 'sync', 'enable': True, 'command': '/usr/local/sbin/snapraid-guarded-sync'},
                     {'uuid': 'scrub', 'enable': True, 'command': '/usr/local/sbin/snapraid-guarded-scrub'},
                     {'uuid': 'unrelated', 'enable': True, 'command': 'echo keep unchanged'}]
        self.execute = Mock(side_effect=self.command)

    def command(self, args, **kwargs):
        if args[:2] == ['omv-confdbadm', 'read']:
            return '{}' if '--defaults' in args else json.dumps(self.jobs)
        if args[:2] == ['omv-confdbadm', 'update']:
            value = json.loads(kwargs['data'])
            self.jobs = [job for job in self.jobs if job['uuid'] != value['uuid']] + [value]
        return ''

    def test_schedule_replacement_is_scoped_and_rerun_keeps_originals(self):
        original = json.loads(json.dumps(self.jobs))
        configure_omv_schedule(self.installer, self.execute)
        saved = self.receipt.read_text()
        by_id = {job['uuid']: job for job in self.jobs}
        self.assertFalse(by_id['sync']['enable'])
        self.assertFalse(by_id['scrub']['enable'])
        self.assertEqual(by_id['unrelated'], original[2])
        coordinator = [job for job in self.jobs if job['uuid'] not in {'sync', 'scrub', 'unrelated'}][0]
        for key,value in SCHEDULE.items():
            self.assertEqual(coordinator[key], value)
        self.execute.reset_mock()
        configure_omv_schedule(self.installer, self.execute)
        self.assertEqual(self.receipt.read_text(), saved)
        self.assertFalse(any(c.args[0][1:2] == ['update'] for c in self.execute.call_args_list))
        self.assertTrue(json.loads(saved)['original_jobs'][0]['enable'])

    def test_custom_wrapped_job_and_duplicate_coordinators_refuse_no_writes(self):
        for commands in (['sh /usr/local/sbin/snapraid-guarded-sync'], [HELPER+' tick', HELPER+' tick']):
            self.jobs = [{'uuid': str(i), 'enable': True, 'command': command} for i,command in enumerate(commands)]
            with self.assertRaises(InstallError):
                configure_omv_schedule(self.installer, self.execute)
            self.assertFalse(self.receipt.exists())
            self.assertFalse(any(c.args[0][1:2] == ['update'] for c in self.execute.call_args_list))

    def test_native_deploy_failure_keeps_receipt_and_rerun_recovers(self):
        original = self.command
        def fail(args, **kwargs):
            if args[0] == 'omv-salt':
                raise InstallError('deployment failed')
            return original(args, **kwargs)
        with self.assertRaises(InstallError):
            configure_omv_schedule(self.installer, fail)
        self.assertTrue(self.receipt.exists())
        saved = self.receipt.read_text()
        configure_omv_schedule(self.installer, self.execute)
        self.assertEqual(self.receipt.read_text(), saved)
        self.assertTrue(any(job['enable'] and job['command'].endswith(' tick') for job in self.jobs))


class BootstrapTests(fixtures.InstallerFixture):
    """Reuse real key generation and temporary host files for SSH setup."""

    def setUp(self):
        super().setUp()
        self.installer = UpgradeInstaller(self.paths, self.ask, self.output)
        self.installer.required = Mock()
        self.installer.run = self.command

    def prepare(self):
        self.ask.side_effect = ['docker.test', '', fixtures.fingerprint(fixtures.sample_key(7)), 'admin']

    def test_single_omv_pass_bootstraps_and_future_upgrade_needs_no_admin(self):
        self.prepare()
        enrolled = False
        calls = []
        def execute(args, **kwargs):
            nonlocal enrolled
            calls.append(args)
            if args[-1] == 'check '+'0'*32:
                if not enrolled:
                    raise InstallError('No dedicated key yet')
                return json.dumps({'ok': True, 'active': False, 'session': '0'*32})
            self.assertEqual(args[0], 'ssh')
            self.assertIn('-tt', args)
            self.assertIn('admin@docker.test', args)
            self.assertNotIn('-i', args)
            self.assertIn('-oControlPath=none', args)
            self.assertIn('-oStrictHostKeyChecking=yes', args)
            self.assertIn('enroll', args[-1])
            enrolled = True
            return ''
        is_file = Path.is_file
        with patch('scripts.host_setup.os.access', return_value=True), patch('scripts.host_setup.Path.is_file', autospec=True, side_effect=lambda path: True if path.name in ('snapraid-guarded-sync', 'snapraid-guarded-scrub') else is_file(path)), patch('scripts.host_setup.configure_omv_schedule') as schedule:
            install_omv(self.installer, execute)
            before = self.snapshot(self.root)
            self.ask.reset_mock(side_effect=True)
            self.ask.side_effect = AssertionError('No questions on upgrade')
            install_omv(self.installer, execute)
        self.assertEqual(self.snapshot(self.root), before)
        self.ask.assert_not_called()
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(sum('-tt' in args for args in calls), 1)
        for name,value in self.normal.items():
            self.assertEqual(self.snapshot(self.paths.key.parent)[name], value)

    def test_active_remote_maintenance_never_prompts_for_admin_or_schedules(self):
        self.prepare()
        execute = Mock(return_value=json.dumps({'ok': True, 'active': True, 'session': '0'*32}))
        is_file = Path.is_file
        with patch('scripts.host_setup.os.access', return_value=True), patch('scripts.host_setup.Path.is_file', autospec=True, side_effect=lambda path: True if path.name in ('snapraid-guarded-sync', 'snapraid-guarded-scrub') else is_file(path)), patch('scripts.host_setup.configure_omv_schedule') as schedule:
            with self.assertRaisesRegex(InstallError, 'active'):
                install_omv(self.installer, execute)
        self.assertEqual(self.ask.call_count, 3)
        schedule.assert_not_called()

    def test_owned_helper_upgrade_preserves_recovery_data(self):
        self.installer.helper()
        helper = self.paths.path(HELPER)
        helper.write_bytes(b'# older revision\n'+SOURCE.read_bytes())
        normal = self.snapshot(self.paths.key.parent)
        self.installer.helper()
        self.assertEqual(helper.read_bytes(), SOURCE.read_bytes())
        self.assertEqual(self.snapshot(self.paths.key.parent), normal)
        before = helper.stat().st_mtime_ns
        self.installer.helper()
        self.assertEqual(helper.stat().st_mtime_ns, before)

    def test_unrecognized_helper_is_preserved(self):
        target = self.paths.path(HELPER)
        target.parent.mkdir(parents=True)
        target.write_text('# unrelated program\n')
        with self.assertRaisesRegex(InstallError, 'unrelated'):
            self.installer.helper()
        self.assertEqual(target.read_text(), '# unrelated program\n')

    def test_media_first_prepares_without_public_key_question(self):
        self.installer.docker(None, None, prepare_key=True)
        self.ask.assert_not_called()
        self.assertFalse(self.paths.path('/var/lib/guard-maintenance-ssh/.ssh/authorized_keys').exists())
        self.assertTrue(self.paths.path('/etc/sudoers.d/torrent-guard-maintenance').is_file())


class CheckoutTests(unittest.TestCase):
    """Automatic upgrades never discard tracked edits or switch branches."""

    def test_dirty_or_non_main_refuses_before_pull(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            for results in ([' M guard.py'], ['', 'dev']):
                execute = Mock(side_effect=results)
                with self.assertRaises(InstallError):
                    update_checkout(root, execute)
                self.assertFalse(any('pull' in c.args[0] for c in execute.call_args_list))

    def test_fast_forward_detects_new_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            execute = Mock(side_effect=['', 'main', 'old-sha', '', 'new-sha'])
            self.assertTrue(update_checkout(root, execute))
            self.assertTrue(any(c.args[0][-2:] == ['pull', '--ff-only'] for c in execute.call_args_list))


class EnrollmentTests(unittest.TestCase):
    """Only the dedicated public-key file is eligible for bootstrap writes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'service'
        (self.home / '.ssh').mkdir(parents=True)
        self.authorized = self.home / '.ssh/authorized_keys'
        self.key = fixtures.sample_key()

    def invoke(self, key):
        real_path = Path
        real_lstat = Path.lstat
        def owner(path):
            info = real_lstat(path)
            from types import SimpleNamespace
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_dev=info.st_dev,
                                   st_ino=info.st_ino, st_mtime_ns=info.st_mtime_ns)
        with patch('scripts.enroll_maintenance_key.os.geteuid', return_value=0), patch('scripts.enroll_maintenance_key.Path', side_effect=lambda value: self.home if str(value) == '/var/lib/guard-maintenance-ssh' else real_path(value)), patch.object(Path, 'lstat', owner), patch('builtins.print'):
            enroll(key)

    def test_enroll_repeat_and_legacy_same_key(self):
        self.invoke(self.key)
        self.assertEqual(self.authorized.read_text(), FORCED+self.key+'\n')
        mtime = self.authorized.stat().st_mtime_ns
        self.invoke(self.key)
        self.assertEqual(self.authorized.stat().st_mtime_ns, mtime)
        self.authorized.write_text(self.key+' legacy comment\n')
        self.invoke(self.key)
        self.assertEqual(self.authorized.read_text(), FORCED+self.key+'\n')

    def test_other_keys_and_symlinks_are_not_overwritten(self):
        self.authorized.write_text(FORCED+fixtures.sample_key(2)+'\n')
        before = self.authorized.read_bytes()
        with self.assertRaisesRegex(ValueError, 'different dedicated key'):
            self.invoke(self.key)
        self.assertEqual(self.authorized.read_bytes(), before)
        outside = self.home / 'normal-admin-key'
        outside.write_text('normal administrator key')
        self.authorized.unlink()
        self.authorized.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.invoke(self.key)
        self.assertEqual(outside.read_text(), 'normal administrator key')

    def test_private_or_multikey_input_is_refused(self):
        for key in ('-----BEGIN OPENSSH PRIVATE KEY-----', self.key+'\n'+self.key):
            with self.assertRaises(ValueError):
                self.invoke(key)
        self.assertFalse(self.authorized.exists())
