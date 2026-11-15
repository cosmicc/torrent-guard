"""One-time administrator SSH bootstrap; only a dedicated public key is accepted.

The OMV installer sends this audited source to Python over an authenticated
administrator SSH session. It is never available through the restricted key's
forced-command agent, and never receives or stores an administrator password.
"""
import base64
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile


def enroll(value: str) -> None:
    """Create the dedicated key file or tighten an identical legacy bare key."""
    if os.geteuid() != 0:
        raise ValueError("Administrator sudo/root is required for key enrollment")
    fields = value.split()
    if len(fields) != 2 or fields[0] != "ssh-ed25519":
        raise ValueError("Expected one Ed25519 public key")
    decoded = base64.b64decode(fields[1], validate=True)
    prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    if len(decoded) != len(prefix) + 32 or not decoded.startswith(prefix):
        raise ValueError("Invalid public key")
    home = Path("/var/lib/guard-maintenance-ssh")
    for folder in (home, home / ".ssh"):
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Run the media installer first; the dedicated SSH home is not ready")
    path = home / ".ssh/authorized_keys"
    previous = None
    command = 'restrict,command="/usr/bin/sudo -n /usr/local/sbin/torrent-guard-maintenance agent \\"$SSH_ORIGINAL_COMMAND\\"" '
    desired = command + value + "\n"
    if os.path.lexists(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Existing dedicated authorized_keys needs administrator review")
        previous = (info.st_dev, info.st_ino, info.st_mtime_ns)
        lines = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
        if len(lines) == 1 and lines[0].startswith(command) and lines[0][len(command):].split()[:2] == fields:
            print("Dedicated maintenance key already enrolled")
            return
        # A manually pasted bare copy of the same public key can be restricted
        # safely. Never replace a different key or discard multiple keys.
        if len(lines) != 1 or lines[0].split()[:2] != fields:
            raise ValueError("A different dedicated key exists; review/reset maintenance setup before replacing it")
    descriptor, name = tempfile.mkstemp(prefix=".tg-enroll-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        if previous is not None:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns) != previous:
                raise ValueError("The dedicated key changed during enrollment; left unchanged")
            os.replace(name, path)
        else:
            os.link(name, path)
        print("Dedicated maintenance public key enrolled")
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("One public key argument is required")
        enroll(sys.argv[1])
    except (OSError, ValueError) as exc:
        print("Key enrollment stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
