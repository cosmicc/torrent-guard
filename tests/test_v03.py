"""v0.3: qBittorrent incomplete/complete directory and .!qB regression tests."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from guard import Config, MediaValidator, classify, effective_extension, normalized_name, video_files, is_complete


class IncompleteFilenameTests(unittest.TestCase):
    def test_qbit_api_original_extensions_still_work(self):
        self.assertEqual(effective_extension("Movies/Movie.mkv"), ".mkv")
        self.assertIn("risky", classify([{"name": "Fake.exe"}]))

    def test_appended_incomplete_suffix_does_not_hide_exe(self):
        for name in ("Film.exe.!qB", "Film.SCR.!QB", "Dir/setup.msi.!qb", "bad.bat.!qB"):
            with self.subTest(name=name):
                self.assertIn("risky", classify([{"name": name}]))

    def test_appended_incomplete_suffix_does_not_hide_video(self):
        for name in ("Film.mkv.!qB", "Shows\\Episode.MP4.!QB"):
            with self.subTest(name=name):
                self.assertIsNone(classify([{"name": name}]))
                self.assertEqual(len(video_files([{"name": name}])), 1)

    def test_suffix_does_not_sanitize_double_extension(self):
        self.assertIn("risky", classify([{"name": "Film.mkv.exe.!qB"}]))
        self.assertIn("no recognized", classify([{"name": "file.!qB"}]))


class PathAndFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        self.root = Path(self.ctx.name)
        self.root.joinpath("complete").mkdir()
        self.root.joinpath("incomplete").mkdir()
        self.cfg = Config("http://qbit", "u", "p", {}, {}, qbit_download_root="/downloads",
                          guard_download_root=str(self.root), qbit_complete_dir="/downloads/complete",
                          qbit_incomplete_dir="/downloads/incomplete")
        self.validator = MediaValidator(self.cfg)
        self.file = {"name": "Film.mkv", "size": 50, "progress": 1.0, "priority": 1}
        self.torrent = {"save_path": "/downloads/complete", "content_path": "/downloads/complete/Film.mkv"}
        self.video_path = self.root / "complete" / "Film.mkv"

    def tearDown(self):
        self.ctx.cleanup()

    def test_mapping_uses_complete_directory_not_incomplete(self):
        self.assertEqual(self.validator.resolve(self.torrent, self.file), self.video_path)

    def test_completed_but_still_in_incomplete_path_defers(self):
        self.video_path.write_bytes(b"valid media placeholder")
        t = {**self.torrent, "content_path": "/downloads/incomplete/Film.mkv.!qB"}
        with patch("guard.subprocess.run") as proc:
            decision, reason = self.validator.inspect(t, [self.file])
        self.assertEqual(decision, "defer")
        self.assertIn("incomplete directory", reason)
        proc.assert_not_called()

    def test_other_content_path_outside_complete_defers(self):
        self.video_path.write_bytes(b"valid media placeholder")
        t = {**self.torrent, "content_path": "/downloads/other/Film.mkv"}
        with patch("guard.subprocess.run") as probe:
            decision, reason = self.validator.inspect(t, [self.file])
        self.assertEqual(decision, "defer")
        self.assertIn("outside final directory", reason)
        probe.assert_not_called()

    def test_ffprobe_disabled_still_waits_for_file_in_complete(self):
        cfg = Config(**{**self.cfg.__dict__, "verify_video_content": False})
        validator = MediaValidator(cfg)
        result, reason = validator.inspect(self.torrent, [self.file])
        self.assertEqual(result, "defer")
        self.video_path.write_bytes(b"data")
        result, reason = validator.inspect(self.torrent, [self.file])
        self.assertEqual(result, "accept")
        self.assertIn("ffprobe DISABLED", reason)

    def test_torrent_save_path_in_incomplete_defers(self):
        t = {**self.torrent, "save_path": "/downloads/incomplete", "content_path": ""}
        self.root.joinpath("incomplete/Film.mkv").write_bytes(b"not finished")
        result, msg = self.validator.inspect(t, [self.file])
        self.assertEqual(result, "defer")
        self.assertIn("not under configured final directory", msg)

    def test_final_path_not_yet_available_defers_even_if_incomplete_exists(self):
        self.root.joinpath("incomplete/Film.mkv.!qB").write_bytes(b"still copying")
        with patch("guard.subprocess.run") as proc:
            result, msg = self.validator.inspect(self.torrent, [self.file])
        self.assertEqual(result, "defer")
        self.assertIn("not readable", msg)
        proc.assert_not_called()

    def test_api_reports_temporary_suffix_after_progress_complete_defers(self):
        self.video_path.write_bytes(b"valid")
        with patch("guard.subprocess.run") as proc:
            result, msg = self.validator.inspect(self.torrent, [{**self.file, "name": "Film.mkv.!qB"}])
        self.assertEqual(result, "defer")
        self.assertIn("waiting for rename/move", msg)
        proc.assert_not_called()

    def test_video_verified_after_move_and_rename(self):
        self.root.joinpath("incomplete/Film.mkv.!qB").write_bytes(b"half")
        self.video_path.write_bytes(b"full file data")
        probe = Mock(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}), stderr="")
        with patch("guard.subprocess.run", return_value=probe) as proc:
            result, msg = self.validator.inspect(self.torrent, [self.file])
        self.assertEqual(result, "accept")
        proc.assert_called_once()
        self.assertEqual(proc.call_args.args[0][-1], str(self.video_path))

    def test_moving_state_never_counts_as_complete(self):
        t = {"progress": 1, "amount_left": 0, "completion_on": 100, "state": "moving"}
        self.assertFalse(is_complete(t))
        self.assertTrue(is_complete({**t, "state": "uploading"}))

    def test_nested_category_save_path(self):
        t = {"save_path": "/downloads/complete/radarr", "content_path": ""}
        self.assertEqual(self.validator.resolve(t, self.file), self.root / "complete/radarr/Film.mkv")

    def test_symlink_in_complete_escape_defers(self):
        link = self.root / "complete" / "Film.mkv"
        link.symlink_to("/etc/passwd")
        result, msg = self.validator.inspect(self.torrent, [self.file])
        self.assertEqual(result, "defer")
        self.assertIn("escapes read-only mount", msg)

    def test_symlink_into_incomplete_directory_defers(self):
        target = self.root / "incomplete" / "Film.mkv"
        target.write_bytes(b"unfinished video")
        self.video_path.symlink_to(target)
        with patch("guard.subprocess.run") as probe:
            result, msg = self.validator.inspect(self.torrent, [self.file])
        self.assertEqual(result, "defer")
        self.assertIn("escapes configured completed directory", msg)
        probe.assert_not_called()

    def test_unfinished_selected_video_defers_without_blocklisting(self):
        for changes in ({"progress": 0.5}, {"size": 0}):
            with self.subTest(changes=changes), patch("guard.subprocess.run") as probe:
                result, _ = self.validator.inspect(self.torrent, [{**self.file, **changes}])
                self.assertEqual(result, "defer")
                probe.assert_not_called()


class EnvPathTests(unittest.TestCase):
    def valid_env(self):
        return {"QBIT_URL": "http://localhost:8080", "QBIT_USERNAME": "u", "QBIT_PASSWORD": "p",
                "RADARR_URL": "http://localhost:7878", "RADARR_API_KEY": "secret", "RADARR_CATEGORY": "radarr",
                "RADARR_TAG": "movies", "QBIT_DOWNLOAD_ROOT": "/downloads"}

    def test_default_complete_incomplete_env(self):
        with patch.dict(os.environ, self.valid_env(), clear=True):
            c = Config.from_env()
        self.assertEqual(c.qbit_complete_dir, "/downloads/complete")
        self.assertEqual(c.qbit_incomplete_dir, "/downloads/incomplete")

    def test_custom_qbit_root_and_dirs(self):
        vars = {**self.valid_env(), "QBIT_DOWNLOAD_ROOT": "/data", "QBIT_COMPLETE_DIR": "/data/finished", "QBIT_INCOMPLETE_DIR": "/data/partial"}
        with patch.dict(os.environ, vars, clear=True):
            c = Config.from_env()
        self.assertEqual((c.qbit_complete_dir, c.qbit_incomplete_dir), ("/data/finished", "/data/partial"))

    def test_reject_invalid_paths(self):
        for extras in ({"QBIT_COMPLETE_DIR": "/etc"},
                       {"QBIT_COMPLETE_DIR": "/downloads/incomplete"},
                       {"QBIT_COMPLETE_DIR": "/downloads/../etc"},
                       {"QBIT_INCOMPLETE_DIR": "/downloads"}):
            with self.subTest(extras=extras):
                with patch.dict(os.environ, {**self.valid_env(), **extras}, clear=True):
                    with self.assertRaises(ValueError):
                        Config.from_env()


if __name__ == "__main__":
    unittest.main()
