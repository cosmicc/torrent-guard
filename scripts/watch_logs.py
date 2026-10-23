#!/usr/bin/env python3
"""Read-only, dependency-free SSH event viewer for Torrent Guard audit logs."""
from __future__ import annotations

import argparse
from collections import deque
import os
from pathlib import Path
import re
import shutil
import sys
import time
import unicodedata

RECORD = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?: (?:EST|EDT)[+-]\d{4})?)(?:,\d+)? "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) torrent-guard (.*)$"
)
# Only quoted repr strings emitted by download_context are extracted. Never eval logs.
NAME = re.compile(r"\bname=(?P<value>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")")


def safe_text(value: str) -> str:
    """Escape external controls, including ANSI escapes and bidi overrides."""
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def color_code(message: str, level: str) -> str:
    """Match the daemon's bright palette for dark terminals."""
    if level in {"ERROR", "CRITICAL"} or message.startswith("REJECT"):
        return "91"
    if message.startswith("DRY RUN"):
        return "95"
    if level == "WARNING" or message.startswith(("WAIT", "DEFER", "PENDING")):
        return "93"
    if message.startswith(("ACCEPT", "CLEANED")):
        return "92"
    return "96"


def cell_width(char: str) -> int:
    """Estimate terminal cell width using standard-library Unicode metadata."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def wrap_text(value: str, width: int, indent: str = "  ") -> list[str]:
    """Wrap without discarding text, accounting for wide Unicode characters."""
    capacity = max(1, width - len(indent))
    lines, current, cells = [], "", 0
    for char in safe_text(value):
        size = cell_width(char)
        if current and cells + size > capacity:
            # Prefer a word boundary, but split long paths rather than clipping them.
            boundary = current.rfind(" ")
            if boundary > capacity // 2:
                lines.append(indent + current[:boundary])
                current = current[boundary + 1:]
                cells = sum(cell_width(char) for char in current)
            else:
                lines.append(indent + current)
                current, cells = "", 0
        current += char
        cells += size
    lines.append(indent + current)
    return lines


def render_line(line: str, width: int, color: bool) -> str:
    """Render one audit line; preserve unknown lines and traceback details."""
    match = RECORD.match(line)
    if not match:
        return "\n".join(wrap_text(line, width)) + "\n"
    stamp, level, message = match.groups()
    name = NAME.search(message)
    if name:
        heading = message[:name.start()].strip()
        fields = ["Download: " + name["value"], "Details: " + message[name.end():].lstrip()]
    else:
        heading, separator, detail = message.partition("; ")
        fields = ["Details: " + detail] if separator else []
    header = f"{stamp}  {level}  {heading}"
    rows = wrap_text(header, width, "")
    if color:
        rows = [f"\033[{color_code(message, level)}m{row}\033[0m" for row in rows]
    for field in fields:
        rows.extend(wrap_text(field, width))
    return "\n" + "\n".join(rows) + "\n"


class LogFollower:
    """Follow a filename read-only through rename rotation and observed truncation.

    Keep incomplete lines buffered so a writer's partial write is not presented as
    a separate event. Reopen replacements only after draining the old descriptor.
    """

    def __init__(self, path: Path, tail: int):
        """Remember the target and history limit without opening or writing it."""
        self.path = path
        self.tail = tail
        self.stream = None
        self.initial = True
        self.partial = b""

    def close(self) -> None:
        """Release the read-only file descriptor."""
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def read_lines(self) -> list[str]:
        """Return available complete lines; missing paths are retried next call."""
        if self.stream is None:
            self.stream = self.path.open("rb")
            if self.initial:
                recent = list(deque(self.stream, maxlen=self.tail))
                self.initial = False
                data = b"".join(recent)
            else:
                data = self.stream.read()
        else:
            if os.fstat(self.stream.fileno()).st_size < self.stream.tell():
                self.stream.seek(0)
                self.partial = b""
            data = self.stream.read()
        parts = (self.partial + data).split(b"\n")
        self.partial = parts.pop()
        lines = [part.decode("utf-8", errors="replace") for part in parts]
        try:
            status = self.path.stat()
        except FileNotFoundError:
            return lines
        opened = os.fstat(self.stream.fileno())
        if (status.st_dev, status.st_ino) != (opened.st_dev, opened.st_ino):
            if self.partial:
                lines.append(self.partial.decode("utf-8", errors="replace"))
                self.partial = b""
            self.close()
        return lines


def main() -> int:
    """Follow the audit file until Ctrl+C, without API access or journal writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path("/config/torrent-guard/torrent-guard.log"))
    parser.add_argument("--tail", type=int, default=40, help="initial recent lines (default: 40)")
    parser.add_argument("--width", type=int, help="override automatic terminal width")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--once", action="store_true", help="show recent events and exit")
    args = parser.parse_args()
    if args.tail < 0 or (args.width is not None and args.width < 20):
        parser.error("tail must be nonnegative; width must be at least 20")
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
    follower = LogFollower(args.file, args.tail)
    previous_error = None
    try:
        while True:
            try:
                lines = follower.read_lines()
                previous_error = None
            except OSError as exc:
                if str(exc) != previous_error:
                    print("Waiting for log: " + safe_text(str(exc)), file=sys.stderr, flush=True)
                    previous_error = str(exc)
                if args.once:
                    return 1
                time.sleep(0.5)
                continue
            width = args.width or max(20, shutil.get_terminal_size((100, 24)).columns - 1)
            for line in lines:
                print(render_line(line, width, color), end="", flush=True)
            if args.once:
                if follower.partial:
                    print(render_line(follower.partial.decode("utf-8", errors="replace"), width, color), end="")
                return 0
            time.sleep(0.5)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    finally:
        follower.close()


if __name__ == "__main__":
    sys.exit(main())
