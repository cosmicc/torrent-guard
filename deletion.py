"""Persistent download-file evidence and narrowly scoped Linux leftover removal.

All names originate in a saved qBittorrent file list. Read checks never follow
symlinks. Local removal is restricted to the completed root, uses a private
quarantine, and checks identity after the atomic move before unlinking data.
"""
from contextlib import contextmanager
import ctypes
import errno
import logging
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

LOG = logging.getLogger("torrent-guard")


def identity(info: os.stat_result) -> list[int]:
    """Identify the object and its observed data version; rename changes ctime."""
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def parts(name: str) -> tuple[str, ...]:
    """Reject traversal, absolute paths, and ambiguous empty components."""
    normalized = name.replace("\\", "/")
    components = tuple(normalized.split("/"))
    if not normalized or any(value in {"", ".", ".."} for value in components) or "\x00" in normalized:
        raise ValueError("unsafe download filename")
    return components


def zero_payload(torrent: dict) -> bool:
    """Require explicit lifetime/completed zero bytes, never missing counters."""
    for key in ("progress", "downloaded", "completed"):
        value = torrent.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 0:
            return False
    completed_on = torrent.get("completion_on")
    return (isinstance(completed_on, (int, float)) and not isinstance(completed_on, bool)
            and completed_on <= 0)


def metadata_manifest(torrent: dict) -> dict:
    """Record a zero-payload magnet, with no guessed filesystem deletion targets.

    Stopped states support recovery after Guard has stopped a timed-out magnet.
    This manifest is never accepted by the ordinary file-removal verifier.
    """
    added = torrent.get("added_on")
    if (not zero_payload(torrent)
            or torrent.get("state") not in {"metaDL", "forcedMetaDL", "stoppedDL", "pausedDL"}
            or not torrent.get("hash") or isinstance(added, bool)
            or not isinstance(added, (int, float)) or not 0 < added < float("inf")):
        raise ValueError("metadata removal requires explicit zero payload and an unchanged metadata/stopped state")
    fields = ("hash", "added_on", "category", "tags", "state", "progress", "downloaded", "completed",
              "completion_on", "save_path", "download_path", "content_path")
    return {"version": 2, "kind": "metadata_only", "torrent": {key: torrent[key] for key in fields if key in torrent},
            "files": [], "roots": {}, "items": []}


def validate_metadata_manifest(manifest: dict) -> None:
    """Reject malformed zero-payload evidence without treating it as file absence."""
    if (not isinstance(manifest, dict) or manifest.get("version") != 2
            or manifest.get("kind") != "metadata_only" or manifest.get("files") != []
            or manifest.get("items") != [] or manifest.get("roots") != {}
            or not isinstance(manifest.get("torrent"), dict)):
        raise ValueError("invalid metadata-only deletion evidence; manual review required")
    metadata_manifest(manifest["torrent"])


def rename_exclusive(source_fd: int, source: str, target_fd: int, target: str) -> None:
    """Linux renameat2 prevents replacing a pre-existing quarantine payload."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def rename_to_quarantine(source_fd: int, source: str, target_fd: int) -> bool:
    """Atomically capture into a validated private quarantine; report fallback.

    The caller validates directory identity, attachment and quarantine location.
    Some NFS implementations reject RENAME_NOREPLACE. Only unsupported-operation
    errors permit ordinary rename into an empty, owner-only directory. As with
    the surrounding cleanup checks, another process with the same UID or root
    must not modify quarantine during this operation. There is no check/unlink
    fallback against the original download name.
    """
    try:
        rename_exclusive(source_fd, source, target_fd, "payload")
        return False
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS}:
            raise
    info = os.fstat(target_fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("NFS rename fallback requires an owner-only quarantine owned by the runtime user")
    if os.listdir(target_fd):
        raise FileExistsError(errno.EEXIST, "NFS rename fallback requires an empty private quarantine")
    os.rename(source, "payload", src_dir_fd=source_fd, dst_dir_fd=target_fd)
    return True


class FileVerifier:
    """Verify saved download paths and optionally remove unchanged completed files."""

    def __init__(self, config):
        self.config = config
        self.ro = Path(config.guard_download_root)
        self.qroot = PurePosixPath(config.qbit_download_root)
        self.roots = {value: self.ro / str(PurePosixPath(value).relative_to(self.qroot))
                      for value in (config.qbit_complete_dir, config.qbit_incomplete_dir) if value}

    def paths(self, torrent: dict, files: list[dict]) -> list[dict]:
        """Map every tracked file and potential incomplete name without title guesses."""
        if not isinstance(files, list) or not files:
            raise ValueError("complete qBittorrent file metadata is unavailable")
        bases = set()
        for key in ("save_path", "download_path"):
            value = torrent.get(key)
            if value:
                path = PurePosixPath(str(value))
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("unsafe qBittorrent download location")
                bases.add(path)
        if not bases:
            raise ValueError("qBittorrent download locations are missing")
        result = {}
        for file in files:
            name = "/".join(parts(str(file.get("name") or "")))
            names = {name, name[:-4]} if name.casefold().endswith(".!qb") else {name, name + ".!qB"}
            for base in bases:
                root = next((root for root in self.roots if base.is_relative_to(PurePosixPath(root))), None)
                if root is None:
                    raise ValueError("download location is outside configured complete/incomplete roots")
                for variant in names:
                    relative = str((base / variant).relative_to(PurePosixPath(root)))
                    result[root, relative] = {"root": root, "path": relative}
        return list(result.values())

    @contextmanager
    def parent(self, root: Path, relative: str, anchor: list[int] | None = None):
        """Open path components without symlinks; a missing child is known absence."""
        components = parts(relative)
        descriptors = []
        try:
            # Resolve only ancestors of the configured root. The root itself must
            # be a real directory and is checked against its persisted identity.
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if anchor is not None and [info.st_dev, info.st_ino] != anchor:
                raise ValueError("download mount/root identity changed; manual review required")
            for component in components[:-1]:
                try:
                    descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    yield None, components[-1]
                    return
                descriptors.append(descriptor)
            yield descriptor, components[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def observe(self, manifest: dict, item: dict, relative: str | None = None):
        """Return regular-file identity or absence; errors never mean absence."""
        root = self.roots.get(item["root"])
        if root is None or str(root) != manifest["roots"][item["root"]]["path"]:
            raise ValueError("download mapping changed; manual review required")
        with self.parent(root, relative or item["path"], manifest["roots"][item["root"]]["identity"]) as (descriptor, name):
            if descriptor is None:
                return None
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("download path is a symlink or special file; manual review required")
            return identity(info)

    def snapshot(self, torrent: dict, files: list[dict]) -> dict:
        """Capture immutable target names and identities before the API deletion."""
        items = self.paths(torrent, files)
        roots = {}
        for item in items:
            root = self.roots[item["root"]]
            with self.parent(root, item["path"]) as (_descriptor, _name):
                info = root.stat(follow_symlinks=False)
                roots[item["root"]] = {"path": str(root), "identity": [info.st_dev, info.st_ino]}
        torrent_fields = ("hash", "category", "tags", "added_on", "save_path", "download_path", "content_path",
                          "progress", "amount_left", "completion_on", "state")
        # Do not persist magnet URIs or other optional API fields that can contain
        # private tracker credentials. Store only the evidence used by cleanup.
        manifest = {"version": 1, "torrent": {key: torrent[key] for key in torrent_fields if key in torrent},
                    "files": [{key: file[key] for key in ("name", "size", "priority", "progress", "index") if key in file}
                              for file in files], "roots": roots, "items": items}
        for item in items:
            item["identity"] = self.observe(manifest, item)
        return manifest

    def remaining(self, manifest: dict) -> list[dict]:
        """Verify original and recorded quarantine names on a healthy mapped root."""
        if (not isinstance(manifest, dict) or manifest.get("version") != 1
                or not isinstance(manifest.get("items"), list) or not manifest["items"]
                or not isinstance(manifest.get("roots"), dict)):
            raise ValueError("missing or unsupported deletion manifest; manual review required")
        remaining = []
        for item in manifest["items"]:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or item.get("root") not in manifest["roots"]):
                raise ValueError("invalid saved download path; manual review required")
            root = manifest["roots"][item["root"]]
            if (not isinstance(root, dict) or not isinstance(root.get("path"), str)
                    or not isinstance(root.get("identity"), list) or len(root["identity"]) != 2):
                raise ValueError("invalid saved download root; manual review required")
            present = self.observe(manifest, item) is not None
            if item.get("quarantine"):
                present = self.observe(manifest, item, item["quarantine"] + "/payload") is not None or present
            if present:
                remaining.append(item)
        return remaining

    @staticmethod
    def attached(descriptor: int, expected: Path) -> None:
        """Detect parent-directory moves before a quarantine or unlink operation."""
        actual = os.readlink(f"/proc/self/fd/{descriptor}")
        if actual != str(expected):
            raise ValueError("cleanup parent moved; preserving files for manual review")

    def remove(self, manifest: dict, item: dict, persist) -> None:
        """Quarantine then remove only an unchanged, previously recorded regular file.

        Checkpoint the private quarantine before moving data. On identity mismatch
        retain the captured object for recovery; never unlink it automatically.
        Empty quarantine directories are intentionally retained as recovery metadata.
        """
        if self.config.dry_run or not self.config.cleanup_leftovers:
            raise ValueError("local cleanup is disabled")
        if item["root"] != self.config.qbit_complete_dir:
            raise ValueError("local removal is restricted to the completed-download root")
        if item.get("identity") is None:
            raise ValueError("file appeared after snapshot; preserving it")
        root = Path(self.config.cleanup_write_root)
        anchor = manifest["roots"][item["root"]]["identity"]
        with self.parent(root, item["path"], anchor) as (parent, name):
            if parent is None:
                return
            expected = root / PurePosixPath(item["path"]).parent
            self.attached(parent, expected)
            if not item.get("quarantine"):
                current = self.observe(manifest, item)
                if current is None:
                    return
                if current != item["identity"]:
                    raise ValueError("leftover identity changed; preserving it")
                directory = ".torrent-guard-" + uuid.uuid4().hex
                os.mkdir(directory, 0o700, dir_fd=parent)
                quarantine = str(PurePosixPath(item["path"]).parent / directory)
                info = os.stat(directory, dir_fd=parent, follow_symlinks=False)
                item["quarantine"] = quarantine
                item["quarantine_identity"] = [info.st_dev, info.st_ino]
                persist(manifest)
            directory = PurePosixPath(item["quarantine"]).name
            if (str(PurePosixPath(item["quarantine"]).parent) != str(PurePosixPath(item["path"]).parent)
                    or not directory.startswith(".torrent-guard-")):
                raise ValueError("invalid quarantine location")
            quarantine_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                info = os.fstat(quarantine_fd)
                if [info.st_dev, info.st_ino] != item["quarantine_identity"]:
                    raise ValueError("quarantine directory identity changed")
                quarantined = self.observe(manifest, item, item["quarantine"] + "/payload")
                original = self.observe(manifest, item)
                if quarantined is None and original is not None:
                    if original != item["identity"]:
                        raise ValueError("leftover changed before quarantine; preserving it")
                    self.attached(parent, expected)
                    fallback = rename_to_quarantine(parent, name, quarantine_fd)
                    if fallback:
                        LOG.warning("CLEANUP NFS compatibility: protected rename flag unsupported; "
                                    "used atomic rename into private quarantine for download=%r", item["path"])
                    quarantined = identity(os.stat("payload", dir_fd=quarantine_fd, follow_symlinks=False))
                elif quarantined is not None and original is not None:
                    raise ValueError("source reappeared after quarantine; preserving both objects")
                if quarantined is not None:
                    if quarantined != item["identity"]:
                        raise ValueError(f"quarantined object changed; recover manually from {item['quarantine']}/payload")
                    self.attached(parent, expected)
                    self.attached(quarantine_fd, expected / directory)
                    info = os.stat("payload", dir_fd=quarantine_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or identity(info) != item["identity"]:
                        raise ValueError("quarantined object changed before unlink")
                    os.unlink("payload", dir_fd=quarantine_fd)
            finally:
                os.close(quarantine_fd)
