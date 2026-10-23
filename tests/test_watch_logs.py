"""Regression coverage for read-only SSH presentation and real file following."""
import io
from pathlib import Path
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

from scripts.watch_logs import LogFollower, color_code, main, render_line


class RenderingTests(unittest.TestCase):
    """Retain diagnostic content and prevent log text controlling terminals."""

    def test_eastern_zone_and_offset_remain_in_event_headers(self):
        for zone in ("EST-0500", "EDT-0400"):
            with self.subTest(zone=zone):
                rendered = render_line(f"2026-11-01 01:30:00 {zone} WARNING torrent-guard DEFER validation name='Movie' retry_in=60s", 100, False)
                self.assertIn(f"2026-11-01 01:30:00 {zone}  WARNING  DEFER validation", rendered)
                self.assertIn("Download: 'Movie'", rendered)

    def test_event_block_wraps_long_names_paths_and_retains_details(self):
        path = "/downloads/complete/" + "long-directory/" * 12 + "movie.mkv"
        line = ("2026-10-21 14:32:08,123 WARNING torrent-guard "
                f"PENDING disk verification name='Example Movie' paths='{path}': "
                "automatic cleanup disabled; retry_in=60s")
        result = render_line(line, 60, False)
        self.assertIn("Download: 'Example Movie'", result)
        self.assertIn("WARNING  PENDING disk verification", result)
        self.assertTrue(all(len(row) <= 60 for row in result.splitlines()))
        # Wrapping must retain every path character, even with no word boundaries.
        flattened = "".join(row.strip() for row in result.splitlines())
        self.assertIn(path, flattened)
        self.assertIn("retry_in=60s", result)

    def test_palette_matches_daemon_and_colors_only_headings(self):
        for message, level, code in [
            ("ACCEPT completed", "INFO", "92"),
            ("CLEANED", "INFO", "92"),
            ("HEARTBEAT polling active", "INFO", "96"),
            ("WAIT import", "INFO", "93"),
            ("Something failed", "ERROR", "91"),
            ("REJECT candidate", "WARNING", "91"),
            ("DRY RUN: would delete", "WARNING", "95"),
        ]:
            with self.subTest(message=message):
                self.assertEqual(color_code(message, level), code)
                line = f"2026-10-21 14:32:08,123 {level} torrent-guard {message} name='Movie' paths='/data/movie'"
                rendered = render_line(line, 80, True)
                self.assertIn(f"\033[{code}m", rendered)
                detail = rendered[rendered.index("  Download:"):]
                self.assertNotIn("\033", detail)

    def test_unknown_lines_controls_and_tracebacks_remain_safe(self):
        rendered = render_line("Traceback: external \033[2J\r\x9b31m\u202e title", 80, False)
        for char in ("\033", "\r", "\x9b", "\u202e"):
            self.assertNotIn(char, rendered)
        self.assertIn("Traceback:", rendered)
        self.assertIn("\\x1b", rendered)

    def test_unicode_and_escaped_quoted_name(self):
        line = "2026-10-21 14:32:08,123 INFO torrent-guard ACCEPT completed name=\"A 'movie'\" paths='" + "電影" * 30 + "'"
        result = render_line(line, 40, False)
        self.assertIn('Download: "A \'movie\'"', result)
        for row in result.splitlines():
            self.assertLessEqual(sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in row), 40)

    def test_snapshot_command_and_plain_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.log"
            path.write_text("2026-10-21 14:32:08,123 INFO torrent-guard HEARTBEAT polling active\n")
            output = io.StringIO()
            with patch("sys.argv", ["watch_logs.py", "--file", str(path), "--once"]), patch("sys.stdout", output):
                self.assertEqual(main(), 0)
            self.assertIn("HEARTBEAT", output.getvalue())
            self.assertNotIn("\033", output.getvalue())


class FollowingTests(unittest.TestCase):
    """Exercise rotation, late creation, truncation and partial writes on disk."""

    def test_tail_partial_writes_rotation_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.log"
            path.write_bytes(b"older\nrecent\npar")
            follower = LogFollower(path, 2)
            try:
                self.assertEqual(follower.read_lines(), ["recent"])
                with path.open("ab") as stream:
                    stream.write(b"tial\n")
                self.assertEqual(follower.read_lines(), ["partial"])
                path.rename(path.with_suffix(".log.1"))
                self.assertEqual(follower.read_lines(), [])
                path.write_bytes(b"replacement\n")
                self.assertEqual(follower.read_lines(), [])  # drain old descriptor
                self.assertEqual(follower.read_lines(), ["replacement"])
                path.write_bytes(b"new\n")
                self.assertEqual(follower.read_lines(), ["new"])
                self.assertEqual(follower.read_lines(), [])
            finally:
                follower.close()
            self.assertEqual(path.read_bytes(), b"new\n")

    def test_missing_file_and_tail_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.log"
            follower = LogFollower(path, 0)
            try:
                with self.assertRaises(FileNotFoundError):
                    follower.read_lines()
                path.write_bytes(b"old\n")
                self.assertEqual(follower.read_lines(), [])
                with path.open("ab") as stream:
                    stream.write(b"fresh\n")
                self.assertEqual(follower.read_lines(), ["fresh"])
            finally:
                follower.close()
