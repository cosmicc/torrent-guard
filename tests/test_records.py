"""Real-file/SQLite tests for absence retirement and archive-only UI actions."""
from dataclasses import replace
import http.client
import ipaddress
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from guard import ApiError, ArrInstance, Config, Guard
from records import enqueue, view
from status_web import StatusCache, StatusServer


class RetirementTests(unittest.TestCase):
    """Retirement cannot stop torrents, remove files or fabricate cleanup success."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / 'downloads'
        self.completed = self.root / 'completed'
        self.incomplete = self.root / 'incomplete'
        (self.completed / 'tv').mkdir(parents=True)
        (self.completed / 'movies').mkdir()
        self.incomplete.mkdir()
        self.path = self.completed / 'tv' / 'Episode.mkv'
        self.path.write_bytes(b'video')
        self.now = 1800000000
        self.h = 'a' * 40
        self.torrent = {'hash': self.h, 'name': 'Episode', 'category': 'tv', 'save_path': '/downloads/completed/tv',
                        'download_path': '/downloads/incomplete', 'added_on': self.now - 3600,
                        'completion_on': 0, 'state': 'stalledDL', 'progress': .1}
        self.files = [{'name': 'Episode.mkv', 'size': 5, 'progress': .1}]
        self.config = Config('http://qb', 'u', 'p', {'SONARR': ArrInstance('http://arr', 'dummy', 'tv')}, {'tv': 'SONARR'},
                             dry_run=True, state_db=str(self.base / 'state.sqlite3'), guard_download_root=str(self.root),
                             qbit_complete_dir='/downloads/completed', qbit_incomplete_dir='/downloads/incomplete')
        self.qbit, self.arr = Mock(), Mock()
        self.qbit.torrents.return_value = []
        self.qbit.files.return_value = self.files
        self.guard = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(self.guard.store.db.close)

    def pending(self, evidence=True):
        self.guard.store.add(self.h, 'SONARR', 'Episode', 'manual review required')
        if evidence:
            manifest = self.guard.verifier.snapshot(self.torrent, self.files)
            self.guard.store.save_manifest(self.h, manifest)
            return manifest

    def assert_no_deletion(self):
        self.qbit.stop.assert_not_called()
        self.qbit.delete_with_data.assert_not_called()
        self.assertFalse(self.arr.mock_calls)

    def test_all_pending_phases_retire_absent_records_even_in_dry_run(self):
        self.pending()
        self.path.unlink()
        self.guard.store.set_filesystem_retry(self.h, 2, self.now + 300, 'old filesystem error')
        self.guard.filesystem_retries = self.guard.store.filesystem_retries()
        with patch.object(self.guard.verifier, 'remove') as unlink:
            self.guard.poll(self.now)
        self.assertEqual(self.guard.store.entries(), [])
        self.assertEqual(self.guard.filesystem_retries, {})
        self.assertEqual(self.guard.store.filesystem_retries(), {})
        archived = self.guard.store.db.execute('SELECT disposition,evidence FROM record_archive').fetchone()
        self.assertEqual(archived[0], 'verified_absent')
        self.assertTrue(json.loads(archived[1])['deletion_evidence'])
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM deletion_audit').fetchone()[0], 0)
        unlink.assert_not_called()
        self.assert_no_deletion()

    def test_retirement_before_saved_delete_request_works_in_each_phase_and_mode(self):
        self.path.unlink()
        for index, (dry_run, phase) in enumerate((dry, phase) for dry in (True, False)
                                                for phase in ('match', 'arr', 'delete', 'verify')):
            with self.subTest(dry_run=dry_run, phase=phase):
                self.guard.config = replace(self.config, dry_run=dry_run)
                self.pending()
                self.guard.store.set_phase(self.h, phase)
                self.guard.poll(self.now + index * 61)
                self.assertEqual(self.guard.store.entries(), [])
                self.assert_no_deletion()
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM record_archive').fetchone()[0], 8)

    def test_existing_incomplete_or_quarantine_payload_prevents_retirement(self):
        manifest = self.pending()
        self.path.unlink()
        incomplete = self.incomplete / 'Episode.mkv.!qB'
        incomplete.write_bytes(b'partial')
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        incomplete.unlink()
        quarantine = self.completed / 'tv' / '.saved-quarantine'
        quarantine.mkdir()
        payload = quarantine / 'payload'
        payload.write_bytes(b'captured')
        target = next(item for item in manifest['items']
                      if item['root'] == self.config.qbit_complete_dir and item['path'] == 'tv/Episode.mkv')
        target['quarantine'] = 'tv/.saved-quarantine'
        self.guard.store.save_manifest(self.h, manifest)
        self.guard.poll(self.now + 61)
        self.assertTrue(self.guard.store.entries())
        payload.unlink()
        self.guard.poll(self.now + 122)
        self.assertEqual(self.guard.store.entries(), [])
        self.assert_no_deletion()

    def test_missing_or_replaced_mount_is_not_absence(self):
        self.pending()
        self.path.unlink()
        moved = self.root / 'detached'
        self.completed.rename(moved)
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        self.completed.mkdir()
        self.guard.poll(self.now + 61)
        self.assertTrue(self.guard.store.entries())
        self.assert_no_deletion()

    def test_client_reappearance_before_commit_preserves_record(self):
        self.pending()
        self.path.unlink()
        self.qbit.torrents.side_effect = [[], [self.torrent]]
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        self.assert_no_deletion()

    def test_missing_or_malformed_evidence_stays_for_explicit_review(self):
        self.pending(evidence=False)
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        self.guard.store.save_manifest(self.h, {'version': 1, 'items': []})
        self.guard.poll(self.now + 61)
        self.assertTrue(self.guard.store.entries())
        self.assert_no_deletion()

    def test_zero_payload_retirement_does_not_claim_filesystem_absence(self):
        from deletion import metadata_manifest
        torrent = {**self.torrent, 'state': 'metaDL', 'progress': 0, 'downloaded': 0, 'completed': 0}
        self.guard.store.add(self.h, 'SONARR', 'Failed magnet', 'timeout', kind='metadata')
        self.guard.store.save_manifest(self.h, metadata_manifest(torrent))
        with patch.object(self.guard.verifier, 'remaining', side_effect=AssertionError('metadata has no filesystem targets')), self.assertLogs('torrent-guard', level='INFO') as logs:
            self.guard.poll(self.now)
        self.assertEqual(self.guard.store.entries(), [])
        self.assertTrue(any('no filesystem targets' in message for message in logs.output))
        self.assert_no_deletion()

    def test_malformed_torrent_in_saved_manifest_is_inconclusive(self):
        manifest = self.pending()
        self.path.unlink()
        manifest['torrent'] = None
        self.guard.store.save_manifest(self.h, manifest)
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        self.assert_no_deletion()

    def test_symlink_is_not_absence(self):
        self.pending()
        self.path.unlink()
        self.path.symlink_to(self.base / 'missing-target')
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        self.assert_no_deletion()

    def test_future_orphan_hold_captures_paths_and_retires_without_pending_record(self):
        self.guard.retirement_files[self.h] = (self.torrent, self.files)
        self.guard._defer_filesystem(self.h, 'read error', self.now)
        self.path.unlink()
        self.guard.poll(self.now + 1)
        self.assertNotIn(self.h, self.guard.filesystem_retries)
        self.assertEqual(self.guard.store.db.execute('SELECT name,disposition FROM record_archive').fetchone(), ('Episode', 'verified_absent'))
        self.assert_no_deletion()

    def test_observed_move_keeps_both_tv_and_movies_locations(self):
        self.guard.retirement_files[self.h] = (self.torrent, self.files)
        self.guard._defer_filesystem(self.h, 'read error', self.now)
        moved = self.completed / 'movies' / 'Episode.mkv'
        moved.write_bytes(b'new payload')
        new_torrent = {**self.torrent, 'save_path': '/downloads/completed/movies'}
        self.guard.retirement_files[self.h] = (new_torrent, self.files)
        self.guard._defer_filesystem(self.h, 'read error after move', self.now + 1)
        moved.unlink()
        self.guard.poll(self.now + 2)
        self.assertIn(self.h, self.guard.filesystem_retries)
        self.path.unlink()
        self.guard.poll(self.now + 63)
        self.assertNotIn(self.h, self.guard.filesystem_retries)
        self.assert_no_deletion()

    def test_failed_capture_cannot_reuse_older_observation_as_complete_proof(self):
        self.guard.retirement_files[self.h] = (self.torrent, self.files)
        self.guard._defer_filesystem(self.h, 'read error', self.now)
        with patch.object(self.guard.verifier, 'snapshot', side_effect=OSError('mount unavailable')):
            self.guard._defer_filesystem(self.h, 'mount error', self.now + 1)
        self.path.unlink()
        self.guard.poll(self.now + 2)
        self.assertIn(self.h, self.guard.filesystem_retries)
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM record_archive').fetchone()[0], 0)
        self.assert_no_deletion()

    def test_manual_archive_preserves_present_files_and_raw_legacy_evidence(self):
        self.pending(evidence=False)
        action = view(Path(self.config.state_db))['records'][0]
        request_id = enqueue(Path(self.config.state_db), self.h, action['revision'], self.now)
        self.guard.poll(self.now)
        self.assertEqual(self.path.read_bytes(), b'video')
        self.assertEqual(self.guard.store.entries(), [])
        self.assertEqual(self.guard.store.db.execute('SELECT disposition FROM record_archive').fetchone()[0], 'operator_dismissed')
        self.assertEqual(self.guard.store.db.execute('SELECT state FROM record_requests WHERE id=?', (request_id,)).fetchone()[0], 'completed')
        self.assert_no_deletion()

    def test_archived_record_is_not_recreated_from_earlier_poll_inventory(self):
        self.pending(evidence=False)
        self.path.unlink()
        completed = {**self.torrent, 'state': 'uploading', 'progress': 1,
                     'amount_left': 0, 'completion_on': self.now - 300}
        record = view(Path(self.config.state_db))['records'][0]
        enqueue(Path(self.config.state_db), self.h, record['revision'], self.now)
        # The first inventory is stale by the time archival confirms absence.
        self.qbit.torrents.side_effect = [[completed], []]
        self.guard.poll(self.now)
        self.assertEqual(view(Path(self.config.state_db))['records'], [])
        self.assertNotIn(self.h, self.guard.filesystem_retries)
        self.assert_no_deletion()

    def test_absent_verify_record_finishes_without_waiting_old_backoff(self):
        self.guard.config = replace(self.config, dry_run=False)
        self.pending()
        self.path.unlink()
        self.guard.store.set_phase(self.h, 'verify')
        self.guard.store.requested(self.h, self.now - 300)
        self.guard.store.set_cleanup_retry(self.h, 4, self.now + 900, 'old backoff')
        self.guard.cleanup_retries = self.guard.store.cleanup_retries()
        self.guard.store.set_filesystem_retry(self.h, 4, self.now + 300, 'old access hold')
        self.guard.filesystem_retries = self.guard.store.filesystem_retries()
        with patch.object(self.guard.verifier, 'remove') as unlink:
            self.guard.poll(self.now)
        self.assertEqual(self.guard.store.entries(), [])
        self.assertEqual(view(Path(self.config.state_db))['records'], [])
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM deletion_audit').fetchone()[0], 1)
        unlink.assert_not_called()
        self.assert_no_deletion()

    def test_early_verification_race_never_falls_through_to_payload_cleanup(self):
        self.guard.config = replace(self.config, dry_run=False, cleanup_leftovers=True)
        manifest = self.pending()
        self.guard.store.set_phase(self.h, 'verify')
        self.guard.store.requested(self.h, self.now - 300)
        self.guard.store.set_cleanup_retry(self.h, 4, self.now + 900, 'old backoff')
        self.guard.cleanup_retries = self.guard.store.cleanup_retries()
        with patch.object(self.guard.verifier, 'remaining', side_effect=[[], manifest['items']]), patch.object(self.guard.verifier, 'remove') as unlink:
            self.guard.poll(self.now)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.guard.store.entries())
        self.assertIn('reappeared', view(Path(self.config.state_db))['records'][0]['retirement_check']['detail'])
        unlink.assert_not_called()
        self.assert_no_deletion()

    def test_retirement_check_explains_missing_evidence_and_does_not_stale_confirmation(self):
        self.pending(evidence=False)
        before = view(Path(self.config.state_db))['records'][0]
        self.guard.poll(self.now)
        after = view(Path(self.config.state_db))['records'][0]
        self.assertEqual(before['revision'], after['revision'])
        self.assertIn('no saved file paths', after['retirement_check']['detail'])
        enqueue(Path(self.config.state_db), self.h, before['revision'], self.now)
        self.guard.poll(self.now + 1)
        self.assertEqual(view(Path(self.config.state_db))['records'], [])
        self.assert_no_deletion()

    def test_retirement_check_explains_present_file_and_bad_mount(self):
        self.pending()
        self.guard.poll(self.now)
        self.assertIn('saved files remain', view(Path(self.config.state_db))['records'][0]['retirement_check']['detail'])
        self.completed.rename(self.root / 'detached')
        self.guard.poll(self.now + 61)
        self.assertIn('could not be verified', view(Path(self.config.state_db))['records'][0]['retirement_check']['detail'])
        self.assert_no_deletion()

    def test_manual_archive_refuses_live_or_changed_record(self):
        self.pending(evidence=False)
        action = view(Path(self.config.state_db))['records'][0]
        request_id = enqueue(Path(self.config.state_db), self.h, action['revision'], self.now)
        self.qbit.torrents.return_value = [self.torrent]
        self.guard.poll(self.now)
        self.assertTrue(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute('SELECT state FROM record_requests WHERE id=?', (request_id,)).fetchone()[0], 'refused')
        # A changed phase must not be archived by a stale confirmation dialog.
        request_id = enqueue(Path(self.config.state_db), self.h, action['revision'], self.now + 1)
        self.guard.store.set_phase(self.h, 'arr')
        self.qbit.torrents.return_value = []
        self.guard.poll(self.now + 2)
        self.assertTrue(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute('SELECT state FROM record_requests WHERE id=?', (request_id,)).fetchone()[0], 'refused')
        self.assert_no_deletion()

    def test_client_outage_keeps_durable_request_queued_across_restart(self):
        self.pending(evidence=False)
        action = view(Path(self.config.state_db))['records'][0]
        request_id = enqueue(Path(self.config.state_db), self.h, action['revision'], self.now)
        self.qbit.torrents.side_effect = [[], ApiError('offline')]
        with self.assertRaises(ApiError):
            self.guard.poll(self.now)
        self.assertEqual(self.guard.store.db.execute('SELECT state FROM record_requests WHERE id=?', (request_id,)).fetchone()[0], 'queued')
        self.qbit.torrents.side_effect = None
        self.qbit.torrents.return_value = []
        restarted = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(restarted.store.db.close)
        restarted.poll(self.now + 5)
        self.assertEqual(restarted.store.entries(), [])
        self.assert_no_deletion()

    def test_explicit_archive_preserves_malformed_json_for_audit(self):
        self.pending(evidence=False)
        with self.guard.store.db:
            self.guard.store.db.execute('INSERT INTO deletion_evidence VALUES (?, ?, NULL)', (self.h, 'invalid-json'))
        action = view(Path(self.config.state_db))['records'][0]
        enqueue(Path(self.config.state_db), self.h, action['revision'], self.now)
        self.guard.poll(self.now)
        archived = json.loads(self.guard.store.db.execute('SELECT evidence FROM record_archive').fetchone()[0])
        self.assertEqual(archived['deletion_evidence'][0][1], 'invalid-json')
        self.assertEqual(self.path.read_bytes(), b'video')
        self.assert_no_deletion()

    def test_archive_failure_rolls_back_audit_and_active_row_removal_together(self):
        from records import archive
        self.pending(evidence=False)
        self.guard.store.db.executescript("CREATE TRIGGER fail_retirement BEFORE DELETE ON pending BEGIN SELECT RAISE(ABORT, 'test interruption'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            archive(self.guard.store.db, self.h, self.now, 'operator_dismissed')
        self.assertTrue(self.guard.store.entries())
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM record_archive').fetchone()[0], 0)

    def test_bounded_queue_remains_idempotent_when_full(self):
        first = None
        for index in range(32):
            h = f'{index:040x}'
            self.guard.store.add(h, 'SONARR', 'Queued record', 'review')
            record = next(row for row in view(Path(self.config.state_db))['records'] if row['id'] == h)
            request_id = enqueue(Path(self.config.state_db), h, record['revision'], self.now)
            if first is None:
                first = (record, request_id)
        record, request_id = first
        self.assertEqual(enqueue(Path(self.config.state_db), record['id'], record['revision'], self.now), request_id)
        self.pending(evidence=False)
        record = next(row for row in view(Path(self.config.state_db))['records'] if row['id'] == self.h)
        with self.assertRaisesRegex(ValueError, 'queue is full'):
            enqueue(Path(self.config.state_db), self.h, record['revision'], self.now)


class ActionHTTPTests(unittest.TestCase):
    """Only same-origin, token-protected JSON requests can queue archival."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        from guard import PendingStore
        self.path = Path(self.directory.name) / 'state.sqlite3'
        store = PendingStore(str(self.path))
        store.add('b' * 40, 'RADARR', '<img onerror=bad>', 'review')
        store.db.close()
        self.record = view(self.path)['records'][0]
        self.server = StatusServer(('127.0.0.1', 0), StatusCache(), (ipaddress.ip_network('127.0.0.1/32'),), self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, payload=None, **overrides):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': self.server.csrf_token,
                   'Origin': f'http://127.0.0.1:{self.server.server_port}'}
        headers.update(overrides)
        body = json.dumps(payload if payload is not None else {'record_id': self.record['id'], 'revision': self.record['revision']})
        try:
            connection.request('POST', '/api/forget', body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_valid_request_only_queues_and_is_idempotent(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first[0], 202)
        self.assertEqual(json.loads(first[1])['request_id'], json.loads(second[1])['request_id'])
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute('SELECT count(*) FROM pending').fetchone()[0], 1)
            self.assertEqual(database.execute('SELECT count(*) FROM record_archive').fetchone()[0], 0)
            self.assertEqual(database.execute('SELECT count(*) FROM record_requests').fetchone()[0], 1)

    def test_configured_public_origin_allows_tls_proxy_backend_host_mismatch(self):
        self.server.public_origin = 'https://guard.example'
        self.assertEqual(self.request(Origin='https://guard.example:443', Host='guard-backend:8080')[0], 202)
        for origin in ('https://attacker.example', 'http://guard.example', 'https://guard.example:8443', 'null'):
            self.assertEqual(self.request(Origin=origin, Host='guard-backend:8080')[0], 403)
        self.assertEqual(self.request(Origin='https://guard.example', Host='guard-backend:8080', **{'X-CSRF-Token': 'wrong'})[0], 403)

    def test_origin_and_token_failures_have_distinct_actionable_errors(self):
        code, message = self.request(Origin='https://guard.example', Host='guard-backend:8080')
        self.assertEqual(code, 403)
        self.assertIn(b'STATUS_PUBLIC_ORIGIN', message)
        code, message = self.request(**{'X-CSRF-Token': 'wrong'})
        self.assertEqual(code, 403)
        self.assertIn(b'token expired', message)

    def test_source_origin_token_and_schema_rejections(self):
        for headers in ({'Origin': 'https://attacker.invalid'}, {'X-CSRF-Token': ''}, {'X-CSRF-Token': 'wrong'}):
            self.assertEqual(self.request(**headers)[0], 403)
        self.assertEqual(self.request(**{'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request({'record_id': '../state', 'revision': 'x' * 64})[0], 409)
        self.assertEqual(self.request({'record_id': self.record['id'], 'revision': self.record['revision'], 'padding': 'x' * 2000})[0], 409)
        self.server.allowed_clients = (ipaddress.ip_network('192.0.2.1/32'),)
        self.assertEqual(self.request(**{'X-Forwarded-For': '192.0.2.1'})[0], 403)
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute('SELECT count(*) FROM record_requests').fetchone()[0], 0)

    def test_status_counters_are_current_after_operator_archival(self):
        from records import archive
        with sqlite3.connect(self.path) as database:
            archive(database, self.record['id'], 1800000000, 'operator_dismissed', self.record['revision'])
        self.server.cache.snapshot = {'collected_at': 1800000000,
                                      'journal': {'available': True, 'counts': {'pending': 99}}}
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET', '/api/status')
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(payload['journal']['counts']['pending'], 0)
        self.assertEqual(payload['record_actions']['records'], [])
        self.assertEqual(payload['record_actions']['archived'][0]['disposition'], 'operator_dismissed')
