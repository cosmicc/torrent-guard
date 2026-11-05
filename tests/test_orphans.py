"""Real-file and SQLite coverage of the v1.9 independent orphan policy."""
from dataclasses import replace
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import ApiError, ArrInstance, Config, Guard, is_complete


class OrphanTests(unittest.TestCase):
    """Client uncertainty or changing files must never authorize orphan removal."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'downloads'
        for kind in ('complete', 'incomplete'):
            for media in ('tv', 'movies'):
                (self.root / kind / media).mkdir(parents=True)
        self.config = Config('http://qb', 'u', 'p', {'SONARR': ArrInstance('http://arr', 'dummy', 'tv')},
                             {'tv': 'SONARR'}, dry_run=False, orphan_cleanup=True,
                             state_db=str(Path(self.temp.name) / 'state.sqlite3'),
                             guard_download_root=str(self.root), orphan_write_root=str(self.root),
                             qbit_complete_dir='/downloads/complete', qbit_incomplete_dir='/downloads/incomplete')
        self.qbit, self.arr = Mock(), Mock()
        self.qbit.torrents.return_value = []
        self.guard = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(self.guard.store.db.close)
        self.cleaner = self.guard.orphan_cleaner
        self.path = self.root / 'incomplete/tv/Show/episode.mkv.!qB'
        self.path.parent.mkdir()
        self.path.write_bytes(b'partial')
        self.now = 1800000000

    def tick(self, start=0, end=900):
        for seconds in range(start, end + 1, 60):
            self.cleaner.poll(self.now + seconds)

    def torrent(self, state='downloading', **values):
        return {'hash': 'a' * 40, 'name': 'Show', 'category': 'unrelated', 'state': state,
                'save_path': '/downloads/incomplete/tv', 'download_path': '/downloads/incomplete/tv',
                'content_path': '/downloads/incomplete/tv/Show', **values}

    def test_all_four_scopes_delete_only_after_full_absence_and_keep_library_hardlink(self):
        targets = []
        for kind in ('complete', 'incomplete'):
            for media in ('tv', 'movies'):
                path = self.root / kind / media / 'extra.nfo'
                path.write_bytes(b'data')
                targets.append(path)
        library = Path(self.temp.name) / 'library.mkv'
        import os
        os.link(self.path, library)
        outside = self.root / 'incomplete/other.mkv'
        outside.write_bytes(b'outside')
        self.tick(end=840)
        self.assertTrue(all(path.exists() for path in targets + [self.path]))
        self.cleaner.poll(self.now + 900)
        self.assertTrue(all(not path.exists() for path in targets + [self.path]))
        self.assertEqual(library.read_bytes(), b'partial')
        self.assertTrue(outside.exists())
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM orphan_audit').fetchone()[0], 5)
        self.qbit.delete_with_data.assert_not_called()
        self.assertFalse(self.arr.mock_calls)

    def test_every_client_state_and_unrelated_category_protects_entire_content_directory(self):
        self.qbit.files.return_value = [{'name': 'Show/known.mkv'}]
        for state in ('downloading', 'queuedDL', 'pausedDL', 'stoppedDL', 'uploading', 'stalledUP', 'forcedUP'):
            self.qbit.torrents.return_value = [self.torrent(state)]
            self.tick()
            self.assertTrue(self.path.exists(), state)
            self.assertEqual(self.cleaner.rows, {})
            self.cleaner.last_scan = None

    def test_save_tag_protects_live_files_but_does_not_permanently_ignore_orphans(self):
        self.qbit.files.return_value = [{'name': 'Show/episode.mkv'}]
        self.qbit.torrents.return_value = [self.torrent('stoppedDL', tags='save')]
        self.tick()
        self.assertTrue(self.path.exists())
        self.qbit.torrents.return_value = []
        self.tick(start=960, end=1800)
        self.assertTrue(self.path.exists())
        self.cleaner.poll(self.now + 1860)
        self.assertFalse(self.path.exists())

    def test_empty_zero_byte_metadata_preserves_known_content_path(self):
        self.qbit.files.return_value = []
        self.qbit.torrents.return_value = [self.torrent('metaDL', progress=0, downloaded=0, completed=0, completion_on=0)]
        self.tick()
        self.assertTrue(self.path.exists())

    def test_payload_change_resets_wait(self):
        self.tick(end=840)
        self.path.write_bytes(b'changed')
        self.cleaner.poll(self.now + 900)
        self.assertTrue(self.path.exists())
        self.tick(start=960, end=1800)
        self.assertFalse(self.path.exists())

    def test_reappearance_resets_wait(self):
        self.tick(end=840)
        self.qbit.files.return_value = [{'name': 'Show/episode.mkv'}]
        self.qbit.torrents.return_value = [self.torrent('pausedDL')]
        self.cleaner.poll(self.now + 900)
        self.qbit.torrents.return_value = []
        self.tick(start=960, end=1800)
        self.assertTrue(self.path.exists())
        self.cleaner.poll(self.now + 1860)
        self.assertFalse(self.path.exists())

    def test_missing_metadata_malformed_inventory_and_api_failures_preserve_files(self):
        for value in (None, [{}], [self.torrent()]):
            self.qbit.torrents.return_value = value
            self.qbit.files.return_value = []
            self.cleaner.last_scan = None
            try:
                self.cleaner.poll(self.now)
            except ApiError:
                self.cleaner.invalidate(self.now)
            self.assertTrue(self.path.exists())
        self.qbit.torrents.side_effect = ApiError('offline')
        self.cleaner.last_scan = None
        with self.assertRaises(ApiError):
            self.cleaner.poll(self.now + 900)
        self.assertTrue(self.path.exists())

    def test_failed_scan_or_long_poll_gap_requires_new_wait(self):
        self.tick(end=840)
        with patch.object(self.cleaner, 'discover', side_effect=OSError(errno.ESTALE, 'stale')):
            self.cleaner.poll(self.now + 900)
        self.tick(start=960, end=1740)
        self.assertTrue(self.path.exists())
        self.cleaner.poll(self.now + 2000)
        self.assertTrue(self.path.exists())

    def test_fresh_ownership_immediately_before_delete_blocks_removal(self):
        self.tick(end=840)
        with patch.object(self.cleaner, 'protected', side_effect=[set(), {Path('/downloads/incomplete/tv/Show')} ]):
            self.cleaner.poll(self.now + 900)
        self.assertTrue(self.path.exists())

    def test_dry_run_does_not_write_candidate_rows_or_delete(self):
        config = replace(self.config, dry_run=True)
        guard = Guard(config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(guard.store.db.close)
        for seconds in range(0, 961, 60):
            guard.orphan_cleaner.poll(self.now + seconds)
        self.assertTrue(self.path.exists())
        self.assertEqual(guard.store.db.execute('SELECT count(*) FROM orphan_candidates').fetchone()[0], 0)
        self.assertEqual(guard.store.db.execute('SELECT count(*) FROM orphan_audit').fetchone()[0], 0)

    def test_restart_keeps_observed_timer_but_long_downtime_resets(self):
        self.tick(end=840)
        guard = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(guard.store.db.close)
        guard.orphan_cleaner.poll(self.now + 900)
        self.assertFalse(self.path.exists())

    def test_symlinks_missing_scopes_and_mismatched_write_bind_preserve_files(self):
        link = self.root / 'complete/tv/escape'
        link.symlink_to(self.path)
        self.tick()
        self.assertTrue(self.path.exists())
        link.unlink()
        self.cleaner.guard.config = replace(self.config, orphan_write_root=str(self.root / 'wrong'))
        self.tick(start=960, end=1860)
        self.assertTrue(self.path.exists())

    def test_old_quarantines_do_not_exhaust_the_discovery_limit(self):
        for number in range(5):
            (self.path.parent / f'.torrent-guard-old-{number}').mkdir()
        with patch('orphans.MAX_FILES', 3):
            self.tick()
        self.assertFalse(self.path.exists())

    def test_private_quarantine_is_not_discovered(self):
        quarantine = self.path.parent / '.torrent-guard-foreign'
        quarantine.mkdir()
        payload = quarantine / 'payload'
        payload.write_bytes(b'private')
        self.tick()
        self.assertEqual(payload.read_bytes(), b'private')

    def test_quarantine_recovery_survives_restart(self):
        self.tick(end=840)
        from deletion import rename_to_quarantine
        import os
        def capture_then_interrupt(source_fd, name, target_fd):
            rename_to_quarantine(source_fd, name, target_fd)
            raise OSError('interrupted after rename')
        with patch('deletion.rename_to_quarantine', side_effect=capture_then_interrupt):
            self.cleaner.poll(self.now + 900)
        self.assertFalse(self.path.exists())
        payloads = list(self.path.parent.glob('.torrent-guard-*/payload'))
        self.assertEqual(len(payloads), 1)
        guard = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(guard.store.db.close)
        for seconds in range(960, 1801, 60):
            guard.orphan_cleaner.poll(self.now + seconds)
        self.assertFalse(payloads[0].exists())

    def test_clock_rollback_and_long_restart_gap_reset_timer(self):
        self.tick(end=840)
        guard = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(guard.store.db.close)
        guard.orphan_cleaner.poll(self.now + 1200)
        self.assertTrue(self.path.exists())
        guard.orphan_cleaner.poll(self.now - 60)
        self.assertTrue(self.path.exists())
        row = next(iter(guard.orphan_cleaner.rows.values()))
        self.assertEqual(row[0], self.now - 60)

    def test_outage_on_first_poll_after_restart_invalidates_persisted_timer(self):
        self.tick(end=840)
        guard = Guard(self.config, self.qbit, {'SONARR': self.arr})
        self.addCleanup(guard.store.db.close)
        self.qbit.torrents.side_effect = ApiError('offline')
        with self.assertRaises(ApiError):
            guard.poll(self.now + 850)
        self.qbit.torrents.side_effect = None
        guard.orphan_cleaner.poll(self.now + 900)
        self.assertTrue(self.path.exists())
        self.assertEqual(next(iter(guard.orphan_cleaner.rows.values()))[0], self.now + 850)

    def test_new_owner_between_inventory_reads_defers_entire_scan(self):
        self.tick(end=840)
        self.qbit.torrents.side_effect = [[], [self.torrent('uploading')]]
        self.cleaner.poll(self.now + 900)
        self.assertTrue(self.path.exists())
        self.assertEqual(next(iter(self.cleaner.rows.values()))[0], self.now + 900)

    def test_missing_scope_and_symlink_directory_prevent_scan(self):
        scope = self.root / 'complete/movies'
        scope.rmdir()
        self.tick()
        self.assertTrue(self.path.exists())
        scope.symlink_to(self.root / 'incomplete/tv', target_is_directory=True)
        self.tick(start=960, end=1860)
        self.assertTrue(self.path.exists())

    def test_replaced_file_after_final_ownership_check_is_preserved(self):
        self.tick(end=840)
        original = self.cleaner.protected
        calls = 0
        def change_file():
            nonlocal calls
            calls += 1
            reserved = original()
            if calls == 2:
                self.path.write_bytes(b'new content')
            return reserved
        with patch.object(self.cleaner, 'protected', side_effect=change_file):
            self.cleaner.poll(self.now + 900)
        self.assertEqual(self.path.read_bytes(), b'new content')
        self.assertEqual(self.guard.store.db.execute('SELECT count(*) FROM orphan_audit').fetchone()[0], 0)

    def test_one_cleanup_permission_failure_does_not_starve_other_files(self):
        good = self.root / 'complete/tv/good.mkv'
        good.write_bytes(b'good')
        self.tick(end=840)
        from deletion import FileVerifier
        original = FileVerifier.remove
        def remove(verifier, manifest, item, persist):
            if item['path'].endswith('episode.mkv.!qB'):
                raise PermissionError('denied')
            return original(verifier, manifest, item, persist)
        with patch.object(FileVerifier, 'remove', remove):
            self.cleaner.poll(self.now + 900)
        self.assertTrue(self.path.exists())
        self.assertFalse(good.exists())

    def test_disabled_policy_never_scans_or_deletes(self):
        self.guard.config = replace(self.config, orphan_cleanup=False)
        self.tick()
        self.assertTrue(self.path.exists())
        self.qbit.torrents.assert_not_called()

    def test_seeding_states_are_complete_but_moving_and_checking_are_not(self):
        torrent = dict(progress=1, amount_left=0, completion_on=self.now - 3600)
        for state in ('uploading', 'stalledUP', 'forcedUP', 'queuedUP', 'pausedUP', 'stoppedUP'):
            self.assertTrue(is_complete({**torrent, 'state': state}))
        for state in ('moving', 'checkingUP', 'missingFiles', 'error'):
            self.assertFalse(is_complete({**torrent, 'state': state}))
