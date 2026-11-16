"""Prompt guidance must help operators without exposing secrets or bypassing trust."""
from pathlib import Path
import unittest
from unittest.mock import Mock

from scripts.app_install import AppInstall
from scripts.install_prompts import PROMPT_HELP, ask_with_help
from scripts.maintenance_install import InstallError, fingerprint
import test_maintenance_install as fixtures


class PromptPrivacyTests(unittest.TestCase):
    """Explain inputs before reading them, retaining hidden credential entry."""

    def test_guidance_is_displayed_before_input_and_does_not_echo_answer(self):
        events = []
        answer = 'fixture-secret$that-must-not-be-printed'
        def read(prompt):
            events.append(('read', prompt))
            return answer
        def output(text):
            events.append(('output', text))
        result = ask_with_help('QBIT_PASSWORD', 'Password: ', read=read, output=output)
        self.assertEqual(result, answer)
        self.assertEqual([event[0] for event in events], ['output', 'read'])
        self.assertIn("qBittorrent's Web UI", events[0][1])
        self.assertNotIn(answer, repr(events))

    def test_hidden_settings_use_secret_reader_without_displaying_a_default(self):
        for setting in ('QBIT_PASSWORD', 'SONARR_API_KEY', 'RADARR_API_KEY'):
            with self.subTest(setting=setting):
                ask, output = Mock(), Mock()
                secret = Mock(return_value='fixture-private-value')
                app = AppInstall(Path('/unused'), Mock(), ask, secret, output)
                self.assertEqual(app.prompt('Credential', 'private-default', setting=setting, hidden=True), 'fixture-private-value')
                ask.assert_not_called()
                secret.assert_called_once_with('Credential: ')
                displayed = str(output.call_args_list)
                self.assertIn(PROMPT_HELP[setting], output.call_args.args[0])
                self.assertNotIn('fixture-private-value', displayed)
                self.assertNotIn('private-default', displayed)

    def test_unregistered_question_refuses_before_reading(self):
        read, output = Mock(), Mock()
        with self.assertRaises(KeyError):
            ask_with_help('UNREGISTERED', 'Value: ', read=read, output=output)
        read.assert_not_called()
        output.assert_not_called()


class FingerprintHelpTests(fixtures.InstallerFixture):
    """Exercise the real host-key pin path with public-only key fixtures."""

    def config(self):
        return {'host': 'docker.test', 'port': 22}

    def test_trusted_media_host_command_and_copy_format_precede_question(self):
        actual = fingerprint(fixtures.sample_key(7))
        def answer(prompt):
            rendered = '\n'.join(call.args[0] for call in self.output.call_args_list)
            self.assertIn('MEDIA DOCKER SERVER', rendered)
            self.assertIn('sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256', rendered)
            self.assertIn('already trusted administrator SSH session', rendered)
            self.assertIn('Copy only the SHA256:... value', rendered)
            # Never offer the unverified network scan as the answer to copy.
            self.assertNotIn(actual, rendered)
            return actual
        self.ask.side_effect = answer
        self.installer.host_key(self.config(), None)
        self.assertTrue(self.paths.hosts.exists())

    def test_guidance_keeps_fingerprint_mismatch_refusal_and_existing_pin_reuse(self):
        self.ask.return_value = fingerprint(fixtures.sample_key(2))
        self.ask.side_effect = None
        with self.assertRaisesRegex(InstallError, 'fingerprint mismatch'):
            self.installer.host_key(self.config(), None)
        self.assertFalse(self.paths.hosts.exists())
        self.ask.return_value = fingerprint(fixtures.sample_key(7))
        self.installer.host_key(self.config(), None)
        self.ask.reset_mock()
        before = self.paths.hosts.read_bytes()
        self.installer.host_key(self.config(), None)
        self.ask.assert_not_called()
        self.assertEqual(self.paths.hosts.read_bytes(), before)

    def test_legacy_public_key_prompt_identifies_omv_pub_file_and_copy_format(self):
        self.ask.side_effect = [fixtures.sample_key()]
        self.installer.docker(None, None)
        rendered = '\n'.join(call.args[0] for call in self.output.call_args_list)
        self.assertIn('sudo cat /root/.ssh/torrent-guard-maintenance.pub', rendered)
        self.assertIn('without Markdown code-block markers', rendered)
        self.assertIn('private key stays on OMV', rendered)
