"""Validate release metadata and extract the current CHANGELOG.md entry.

Run from any directory. Output is suitable for gh release --notes-file.
This command never publishes a release or changes repository files.
"""
import ast
from datetime import datetime
from pathlib import Path
import re


def extract_notes(changelog: str, version: str) -> str:
    """Return one dated version entry, rejecting missing or duplicate entries."""
    headings = list(re.finditer(r"^## (\d+\.\d+(?:\.\d+)?) - (\d{2}\.\d{2}\.\d{4})$",
                                changelog, re.MULTILINE))
    matches = [index for index, heading in enumerate(headings) if heading[1] == version]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG.md must contain exactly one entry for {version}")
    index = matches[0]
    heading = headings[index]
    datetime.strptime(heading[2], "%m.%d.%Y")
    end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
    notes = changelog[heading.start():end].strip()
    for section in ("Added", "Changed", "Fixed"):
        if f"### {section}\n" not in notes:
            raise ValueError(f"Version {version} is missing the {section} section")
    return notes + "\n"


def release_notes(root: Path) -> str:
    """Check version consistency before preparing publishable release text."""
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+", version):
        raise ValueError("VERSION must contain a numeric major.minor version (for example, 0.4)")
    module = ast.parse((root / "guard.py").read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in module.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if versions != [version]:
        raise ValueError("guard.py VERSION does not match VERSION")
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    if re.findall(r"^\s+image: torrent-guard:(\S+)\s*$", compose, re.MULTILINE) != [version]:
        raise ValueError("compose.yaml image tag does not match VERSION")
    return extract_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)


if __name__ == "__main__":
    print(release_notes(Path(__file__).resolve().parents[1]), end="")
