"""Release preparation must fail before publishing inconsistent metadata."""
from pathlib import Path
import tempfile
import unittest

from scripts.release_notes import extract_notes, release_notes


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.entry = "## 0.4 - 10.20.2026\n\n### Added\n\n- Feature.\n\n### Changed\n\n- Change.\n\n### Fixed\n\n- Fix.\n"

    def test_extracts_only_requested_version(self):
        old = self.entry.replace("0.4", "0.3.1")
        self.assertEqual(extract_notes("# Changelog\n\n" + self.entry + "\n" + old, "0.4"), self.entry)

    def test_missing_duplicate_or_incomplete_entries_fail(self):
        for contents in ("", self.entry + self.entry, self.entry.replace("### Fixed", "### Other")):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                extract_notes(contents, "0.4")

    def test_inconsistent_versions_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, contents in {"VERSION": "0.4\n", "guard.py": 'VERSION = "0.4"\n',
                                       "compose.yaml": "    image: torrent-guard:0.4\n",
                                       "CHANGELOG.md": self.entry}.items():
                (root / filename).write_text(contents, encoding="utf-8")
            self.assertEqual(release_notes(root), self.entry)
            (root / "compose.yaml").write_text("    image: torrent-guard:0.3.1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "image tag"):
                release_notes(root)

    def test_repository_release_metadata_is_consistent(self):
        self.assertIn("## 0.4 -", release_notes(Path(__file__).resolve().parents[1]))

    def test_new_three_part_versions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("0.4.1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "major.minor version"):
                release_notes(root)
