"""Torrent Guard array safety and SMART observation for the privileged OMV helper.

Only fixed read-only commands run here. Private history is kept outside the
array; device identity, incomplete observations and bad health stay explicit.
"""
from dataclasses import dataclass
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time


GUARDED = Path('/usr/local/sbin/snapraid-guarded')
SMARTCTL = '/usr/sbin/smartctl'
DIFF_KEYS = ('equal', 'added', 'removed', 'updated', 'moved', 'copied', 'restored', 'relocated')
APP_ID = 0x54474148
WATCH_CRON = '/etc/cron.d/torrent-guard-array-watch'
WATCH_DIRECTORY = Path('/var/lib/torrent-guard-array-watch')
WATCH_SCHEDULE = ('# Torrent Guard independent array accessibility monitor\n'
                  'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
                  '* * * * * root /usr/local/sbin/torrent-guard-maintenance watch-array\n')


class ArrayCheckError(RuntimeError):
    """A safe operator-facing error without command output or private arguments."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded command output and explicit execution outcome."""

    code: int
    output: str
    complete: bool


def read_command(command: list[str], timeout: float, *, env: dict | None = None,
                 limit: int = 1024 * 1024) -> CommandResult:
    """Bound read-only command time/output; kill its process group on timeout."""
    try:
        with tempfile.TemporaryFile() as output:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=subprocess.STDOUT, env=env, start_new_session=True) as process:
                complete = True
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    complete = False
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    code = process.wait()
            output.seek(0, os.SEEK_END)
            length = output.tell()
            output.seek(0)
            raw = output.read(limit)
        text = raw.decode('utf-8', errors='replace')
        return CommandResult(code, text, complete and length <= limit and '\ufffd' not in text)
    except (OSError, subprocess.SubprocessError) as error:
        raise ArrayCheckError('Read-only array command could not run; check OMV tools and local storage.') from error


def trusted_guarded(path: Path = GUARDED) -> None:
    """Refuse linked, foreign-owned, writable or nonexecutable privileged scripts."""
    info = path.lstat()
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise ArrayCheckError('Guarded script ownership, permissions or type is unsafe.')


def protected_array_paths(expected_config: str = '') -> list[dict]:
    """Read the guarded array's explicit data/parity paths without sourcing shell."""
    trusted_guarded(GUARDED)
    with GUARDED.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ArrayCheckError('Guarded configuration is oversized.')
    assignments = re.findall(r'^CONF=(?:"(/[A-Za-z0-9_./:+-]+)"|(/[A-Za-z0-9_./:+-]+))$',
                             raw.decode('utf-8'), re.MULTILINE)
    if len(assignments) != 1:
        raise ArrayCheckError('Guarded array configuration cannot be identified.')
    config = Path(next(value for value in assignments[0] if value)).resolve(strict=True)
    if expected_config and Path(expected_config).resolve(strict=True) != config:
        raise ArrayCheckError('Configured array differs from the guarded array.')
    info = config.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ArrayCheckError('Array configuration ownership/type/permissions are unsafe.')
    with config.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ArrayCheckError('Array configuration is oversized.')
    paths = []
    for line in raw.decode('utf-8').splitlines():
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if fields[0] in ('data', 'disk'):
            if len(fields) != 3 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', fields[1]):
                raise ArrayCheckError('Array data path is unsupported.')
            label, entries, parity = 'data ' + fields[1], [fields[2]], False
        elif re.fullmatch(r'(?:[2-6]-)?parity', fields[0]):
            if len(fields) != 2:
                raise ArrayCheckError('Array parity path is unsupported.')
            label, entries, parity = fields[0], fields[1].split(','), True
        else:
            continue
        for index, entry in enumerate(entries):
            path = Path(entry)
            if (not path.is_absolute() or '..' in path.parts or len(entry) > 4096
                    or not entry.isprintable() or any(char in entry for char in '*?[]')):
                raise ArrayCheckError('Array path is unsafe or unsupported.')
            paths.append({'key': label + ':' + str(index), 'label': label,
                          'path': str(path.parent if parity else path)})
    if (not 2 <= len(paths) <= 32 or len({row['key'] for row in paths}) != len(paths)
            or not any(row['key'].startswith('data ') for row in paths)
            or not any(row['key'].startswith('parity:') for row in paths)):
        raise ArrayCheckError('Array drive list is incomplete, duplicate or oversized.')
    return paths


def check_kernel_device(number: str, sysfs: Path = Path('/sys/dev/block')) -> None:
    """Detect removed/offline block devices even when directory data is cached.

    Follow partition parents and device-mapper/MD slaves using kernel-owned
    sysfs only. No raw disk reads or spin-up commands are issued.
    """
    if not re.fullmatch(r'\d{1,4}:\d{1,7}', number):
        raise ArrayCheckError('Block device number is invalid.')
    pending = [sysfs / number]
    seen = set()
    while pending:
        path = pending.pop().resolve(strict=True)
        if (path / 'partition').exists():
            path = path.parent
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > 64:
            raise ArrayCheckError('Block device topology is oversized or ambiguous.')
        state = path / 'device/state'
        if state.exists() and state.read_text().strip() not in ('running', 'live'):
            raise ArrayCheckError('Underlying block device is offline or unavailable.')
        slaves = path / 'slaves'
        if slaves.is_dir():
            pending.extend(slaves.iterdir())


def probe_array_drives(previous: list[dict], expected_config: str = '') -> dict:
    """Observe mounts and directory reads; enrollment also requires guarded UUID checks.

    Called only in an isolated, deadline-limited child. No media files are opened
    for writing, no SMART tests run, and results never authorize maintenance.
    """
    issues, healthy, inventory = [], [], []
    try:
        paths = protected_array_paths(expected_config)
        saved = {row['key']: row for row in previous}
        layout = [{key: row[key] for key in ('key', 'label', 'path')} for row in previous]
        enrolling = paths != layout
        if enrolling:
            # Never enroll the root filesystem or a replacement disk as healthy
            # merely because findmnt can resolve an existing directory.
            result = subprocess.run([str(GUARDED), 'preflight'], check=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode:
                raise ArrayCheckError('Guarded mount/UUID checks failed; drive inventory cannot be enrolled.')
        for row in paths:
            try:
                result = subprocess.run(['/usr/bin/findmnt', '--json', '--target', row['path'],
                                         '--output', 'TARGET,UUID,FSTYPE,OPTIONS,MAJ:MIN'], check=False,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
                if result.returncode or len(result.stdout) > 65536:
                    raise ArrayCheckError('Filesystem mount is unavailable.')
                mount, = json.loads(result.stdout)['filesystems']
                target, identity = mount['target'], mount['uuid']
                if (not isinstance(target, str) or not Path(target).is_absolute() or target == '/'
                        or not isinstance(identity, str) or not 1 <= len(identity) <= 128
                        or not identity.isprintable() or not mount.get('fstype')):
                    raise ArrayCheckError('Expected array filesystem is missing or cannot be identified.')
                if 'ro' in mount.get('options', '').split(','):
                    raise ArrayCheckError('Array filesystem is read-only.')
                check_kernel_device(mount['maj:min'])
                old = saved.get(row['key'])
                if (old and old['path'] == row['path']
                        and (old['uuid'] != identity or old['target'] != target)):
                    raise ArrayCheckError('Array filesystem identity or mount changed.')
                descriptor = os.open(row['path'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    major, minor = map(int, mount['maj:min'].split(':'))
                    if os.fstat(descriptor).st_dev != os.makedev(major, minor):
                        raise ArrayCheckError('Array mount changed during the directory check.')
                    with os.scandir(descriptor) as entries:
                        next(entries, None)  # Force an actual directory read, even for an empty drive.
                finally:
                    os.close(descriptor)
                inventory.append({**row, 'uuid': identity, 'target': target})
                healthy.append(row['key'])
            except ArrayCheckError as error:
                issues.append({**row, 'reason': str(error)})
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                issues.append({**row, 'reason': 'Drive directory or mount information could not be read.'})
        # An incomplete reading never shrinks or replaces the known drive list.
        return {'inventory': inventory if not issues else previous, 'issues': issues,
                'healthy': healthy, 'complete': not issues}
    except (ArrayCheckError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, ArrayCheckError) else 'Array configuration could not be read.'
        return {'inventory': previous, 'issues': [{'key': 'monitor', 'label': 'Array monitor', 'reason': reason}],
                'healthy': [], 'complete': False}


def bounded_array_probe(previous: list[dict], expected_config: str = '', timeout: float = 20) -> dict:
    """Keep hung disk reads off the cron parent and prevent overlapping probes.

    The child inherits the watcher's flock descriptor. If a kernel disk read
    remains uninterruptible after SIGKILL, it retains that lock, preventing each
    cron invocation from creating another stuck worker. Never wait indefinitely
    for such a child; report unknown accessibility rather than healthy drives.
    """
    with tempfile.TemporaryFile() as output:
        child = os.fork()
        if child == 0:
            try:
                os.setsid()
                result = probe_array_drives(previous, expected_config)
                output.write(json.dumps(result).encode())
                output.flush()
                os._exit(0)
            except Exception:
                # Last-resort worker boundary: never export private exceptions.
                os._exit(1)
        deadline = time.monotonic() + timeout
        while True:
            ended, status = os.waitpid(child, os.WNOHANG)
            if ended:
                if status == 0:
                    output.seek(0)
                    try:
                        result = json.loads(output.read(262145))
                        if (isinstance(result, dict) and set(result) == {'inventory', 'issues', 'healthy', 'complete'}
                                and type(result['complete']) is bool):
                            return result
                    except ValueError:
                        pass
                break
            if time.monotonic() >= deadline:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child, os.WNOHANG)
                break
            time.sleep(0.05)
    return {'inventory': previous, 'issues': [{'key': 'monitor', 'label': 'Array monitor',
            'reason': 'Drive accessibility probe failed or exceeded its 20-second deadline; inspect OMV.'}],
            'healthy': [], 'complete': False}


def diff_summary(counts: dict | None) -> str:
    """Format only validated numeric counts for a notification."""
    if not counts:
        return 'Diff counts: unavailable.'
    return 'Diff: ' + ', '.join(f'{key}={counts[key]}' for key in DIFF_KEYS) + '.'


def pre_sync_check(max_percent: int = 5, timeout: int = 900, expected_config: str = '') -> dict:
    """Use the guarded script's read-only gate; only an explicit pass authorizes sync."""
    counts = None
    reason = 'Guarded preflight/diff failed; inspect OMV guarded logs.'
    try:
        trusted_guarded(GUARDED)
        result = read_command([str(GUARDED), 'check-sync'], timeout,
                              env={**os.environ, 'TORRENT_GUARD_DIFF_MAX_PERCENT': str(max_percent)})
        reports = [line[len('TG_DIFF_V1 '):] for line in result.output.splitlines() if line.startswith('TG_DIFF_V1 ')]
        if len(reports) == 1:
            data = json.loads(reports[0])
            if (not isinstance(data, dict) or set(data) != {'counts', 'limits', 'state', 'max_percent', 'config'}
                    or data['state'] not in ('equal', 'diff', 'unsynced')
                    or type(data['max_percent']) is not int
                    or data['max_percent'] != max_percent
                    or not isinstance(data['counts'], dict) or set(data['counts']) != set(DIFF_KEYS)
                    or not isinstance(data['limits'], dict) or set(data['limits']) != set(DIFF_KEYS) - {'equal'}
                    or any(type(value) is not int or not 0 <= value < 10**12
                           for value in [*data['counts'].values(), *data['limits'].values()])):
                raise ValueError('invalid gate report')
            config = data['config']
            if (not isinstance(config, str) or len(config) > 4096
                    or not re.fullmatch(r'/[A-Za-z0-9_./:+-]+', config) or '..' in Path(config).parts):
                raise ValueError('invalid protected configuration path')
            counts = data['counts']
            changes = sum(counts[key] for key in DIFF_KEYS if key != 'equal')
            if (data['state'] == 'equal' and changes) or (data['state'] == 'diff' and not changes):
                counts = None
                raise ValueError('contradictory diff state/counts')
            total = sum(counts[key] for key in ('equal', 'removed', 'updated', 'moved', 'restored', 'relocated'))
            destructive = counts['removed'] + counts['updated']
            failures = [f'{key}={counts[key]} exceeds limit {value}' for key, value in data['limits'].items()
                        if counts[key] > value]
            if total and destructive * 100 >= max_percent * total:
                failures.append(f'removed+updated={destructive} of {total} existing files reaches {max_percent}%')
            if failures:
                reason = '; '.join(failures)
            elif expected_config and os.path.realpath(expected_config) != os.path.realpath(config):
                reason = 'Configured status/SMART array differs from the guarded sync configuration.'
            elif result.complete and result.code == 0:
                return {'passed': True, 'counts': counts, 'reason': 'All guarded checks passed.', 'config_path': config}
        if not result.complete:
            reason = 'Guarded preflight/diff timed out or exceeded its output limit.'
        elif counts is None:
            # Translate known failures, keeping UUIDs, mount paths and arbitrary
            # script output private. Unknown wrappers always fail closed.
            mappings = (('NOT a mount point', 'A required array filesystem is not mounted; root fallback rejected.'),
                        ('Missing expected directory:', 'A required array mount directory is missing.'),
                        ('Incorrect filesystem on', 'An array filesystem UUID does not match the expected drive.'),
                        ('Unexpected filesystem type', 'An array filesystem has an unexpected type.'),
                        ('BAD blocks', 'SnapRAID reports bad blocks; review required.'),
                        ('Missing/empty content metadata', 'Array content metadata is missing or empty.'),
                        ('Parity or parity content file missing', 'Parity data or metadata is missing or empty.'),
                        ('Config mismatch', 'Array configuration differs from the protected disk layout.'),
                        ('Less than 1 GiB', 'Insufficient free space for guarded operation logs.'))
            reason = next((message for marker, message in mappings if marker in result.output), reason)
    except (ArrayCheckError, OSError, ValueError, TypeError, KeyError):
        reason = 'Guarded preflight/diff is unavailable or its report is invalid; run OMV setup and inspect logs.'
    return {'passed': False, 'counts': counts, 'reason': reason}


def array_devices(output: str) -> dict[str, dict]:
    """Map logical roles to the physical array devices, including parity, without guessing."""
    devices = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 5:
            raise ArrayCheckError('Array device mapping is incomplete or unrecognized.')
        low_id, device, high_id, partition, role = fields
        if (not re.fullmatch(r'\d+:\d+', low_id) or not re.fullmatch(r'\d+:\d+', high_id)
                or not re.fullmatch(r'/dev/[A-Za-z0-9_./:+-]+', device)
                or '..' in Path(device).parts
                or not re.fullmatch(r'/dev/[A-Za-z0-9_./:+-]+', partition) or '..' in Path(partition).parts
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', role)):
            raise ArrayCheckError('Array device mapping contains unsafe or unsupported values.')
        major, minor = map(int, low_id.split(':'))
        high_major, high_minor = map(int, high_id.split(':'))
        if not (0 <= major <= 4095 and 0 <= minor <= 1048575
                and 0 <= high_major <= 4095 and 0 <= high_minor <= 1048575):
            raise ArrayCheckError('Array device number is outside Linux device limits.')
        expected = os.makedev(major, minor)
        if device in devices and devices[device]['rdev'] != expected:
            raise ArrayCheckError('Array device identity is ambiguous.')
        row = devices.setdefault(device, {'rdev': expected, 'roles': []})
        if role not in row['roles']:
            row['roles'].append(role)
    if not devices or len(devices) > 32 or not any('parity' in row['roles'] for row in devices.values()):
        raise ArrayCheckError('Array mapping is empty, oversized or missing parity.')
    return devices


def numeric(value) -> int | None:
    """Accept nonnegative integral SMART counters, never silently substitute zero."""
    return value if type(value) is int and value >= 0 else None


def smart_sample(data: dict, roles: list[str], code: int) -> dict:
    """Normalize ATA/SAS/NVMe health with stable identity and vendor-safe metrics."""
    if not isinstance(data, dict) or type(code) is not int or not 0 <= code <= 255 or code & 7:
        raise ArrayCheckError('SMART data is unavailable or the device command failed.')
    serial = data.get('serial_number')
    if not isinstance(serial, str) or not 1 <= len(serial) <= 128 or not serial.isprintable():
        raise ArrayCheckError('SMART stable drive identity is unavailable.')
    protocol = data.get('device', {}).get('protocol')
    passed = data.get('smart_status', {}).get('passed')
    if protocol not in ('ATA', 'SCSI', 'NVMe') or type(passed) is not bool:
        raise ArrayCheckError('SMART health/protocol is unavailable or unsupported.')
    identity = hashlib.sha256((protocol + ':' + serial).encode()).hexdigest()
    metrics = {'temperature': numeric(data.get('temperature', {}).get('current')),
               'hours': numeric(data.get('power_on_time', {}).get('hours'))}
    critical, warnings = [], []
    if not passed or code & 24:
        critical.append('SMART FAILING or current prefail threshold exceeded')
    if code & 224:
        warnings.append('SMART historical/error/self-test log flags present')
    if protocol == 'ATA':
        table = data.get('ata_smart_attributes', {}).get('table')
        if not isinstance(table, list) or not table:
            raise ArrayCheckError('ATA SMART attribute table is unavailable.')
        attrs = {row.get('id'): row for row in table if isinstance(row, dict)}
        for key, number in (('reallocated', 5), ('pending', 197), ('uncorrectable', 198), ('crc', 199)):
            metrics[key] = numeric(attrs.get(number, {}).get('raw', {}).get('value'))
        for key in ('pending', 'uncorrectable'):
            if metrics[key]:
                critical.append(f'{key} sectors={metrics[key]}')
        if metrics['reallocated']:
            warnings.append(f'reallocated sectors={metrics["reallocated"]}')
        if metrics['crc']:
            warnings.append(f'interface CRC errors={metrics["crc"]} (check cable/controller)')
        tests = data.get('ata_smart_self_test_log', {}).get('standard', {}).get('table', [])
        if isinstance(tests, list) and tests and isinstance(tests[0], dict):
            if tests[0].get('status', {}).get('passed') is False:
                critical.append('latest SMART self-test failed')
        if any(metrics[key] is None for key in ('reallocated', 'pending', 'uncorrectable')):
            warnings.append('some sector counters unavailable')
    elif protocol == 'SCSI':
        metrics['reallocated'] = numeric(data.get('scsi_grown_defect_list'))
        errors = data.get('scsi_error_counter_log', {})
        values = [numeric(errors.get(key, {}).get('total_uncorrected_errors')) for key in ('read', 'write', 'verify')]
        metrics['media_errors'] = sum(values) if all(value is not None for value in values) else None
        if metrics['reallocated']:
            warnings.append(f'grown defect count={metrics["reallocated"]}')
        if metrics['reallocated'] is None or metrics['media_errors'] is None:
            warnings.append('some SCSI counters unavailable')
    else:
        nvme = data.get('nvme_smart_health_information_log', {})
        metrics['media_errors'] = numeric(nvme.get('media_errors'))
        metrics['wear_percent'] = numeric(nvme.get('percentage_used'))
        if numeric(nvme.get('critical_warning')) is None or metrics['media_errors'] is None:
            warnings.append('some NVMe counters unavailable')
        if numeric(nvme.get('critical_warning')):
            critical.append('NVMe critical warning is active')
        if metrics['wear_percent'] is not None and metrics['wear_percent'] >= 100:
            warnings.append('rated NVMe endurance reached')
    if metrics.get('media_errors'):
        warnings.append(f'media errors={metrics["media_errors"]}')
    temperature = metrics['temperature']
    if temperature is not None and temperature >= 60:
        critical.append(f'temperature={temperature}C (critical threshold 60C)')
    elif temperature is not None and temperature >= 50:
        warnings.append(f'temperature={temperature}C (warning threshold 50C)')
    return {'identity': identity, 'roles': roles, 'serial_suffix': serial[-6:], 'protocol': protocol,
            'metrics': metrics, 'critical': critical, 'warnings': warnings, 'deltas': {}}


def save_samples(path: Path, samples: list[dict], session: str) -> None:
    """Persist private identity-keyed history and compare counters over 180 days."""
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        created = True
    except FileExistsError:
        pass
    info = path.lstat()
    parent = path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077):
        raise ArrayCheckError('SMART history ownership/type/permissions are unsafe.')
    now = time.time()
    with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
        try:
            connection.execute('PRAGMA trusted_schema=OFF')
            app_id = connection.execute('PRAGMA application_id').fetchone()[0]
            if not created and app_id != APP_ID:
                raise ArrayCheckError('SMART history database is unrecognized; preserve it for review.')
            connection.execute(f'PRAGMA application_id={APP_ID}')
            connection.execute('CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, at REAL NOT NULL, identity TEXT NOT NULL, session TEXT NOT NULL, data TEXT NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS samples_identity ON samples(identity, id)')
            for sample in samples:
                previous = connection.execute('SELECT data FROM samples WHERE identity=? ORDER BY id DESC LIMIT 1', (sample['identity'],)).fetchone()
                if previous:
                    old = json.loads(previous[0])['metrics']
                    if not isinstance(old, dict) or any(value is not None and numeric(value) is None for value in old.values()):
                        raise ArrayCheckError('SMART history counter data is invalid; preserve it for review.')
                    for key, value in sample['metrics'].items():
                        if value is None or numeric(old.get(key)) is None:
                            continue
                        delta = value - old[key]
                        sample['deltas'][key] = delta
                        if key in ('reallocated', 'media_errors') and delta > 0:
                            sample['critical'].append(f'{key} increased by {delta}')
                        elif key == 'crc' and delta > 0:
                            sample['warnings'].append(f'interface CRC errors increased by {delta}; check cabling')
                        elif key not in ('temperature', 'hours') and delta < 0:
                            sample['warnings'].append(f'{key} counter decreased; prior evidence retained')
                connection.execute('INSERT INTO samples(at, identity, session, data) VALUES(?,?,?,?)',
                                   (now, sample['identity'], session, json.dumps(sample)))
            connection.execute('DELETE FROM samples WHERE at < ?', (now - 180 * 86400,))
        except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
            raise ArrayCheckError('SMART history could not be read/written; check private OMV state.') from error


def check_smart_health(config_path: str, directory: Path, session: str, timeout: int = 30) -> dict:
    """Read all physical array disks without tests; incomplete/critical health holds apps."""
    samples, errors = [], []
    deadline = time.monotonic() + 300

    def budget(limit: int) -> float:
        """Apply the same total subprocess deadline to every discovery/read/check."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArrayCheckError('SMART collection deadline exceeded')
        return min(limit, remaining)

    try:
        trusted_guarded(GUARDED)
        preflight = read_command([str(GUARDED), 'preflight'], budget(120))
        if not preflight.complete or preflight.code:
            raise ArrayCheckError('Array mount/UUID preflight failed before SMART collection.')
        command = ['/usr/bin/snapraid'] + (['-c', config_path] if config_path else []) + ['devices']
        devices = read_command(command, budget(120))
        if not devices.complete or devices.code:
            raise ArrayCheckError('SnapRAID physical device mapping is unavailable.')
        mapping = array_devices(devices.output)
        for device, entry in mapping.items():
            label = ','.join(entry['roles'])
            try:
                info = os.stat(device)
                if not stat.S_ISBLK(info.st_mode) or info.st_rdev != entry['rdev']:
                    raise ArrayCheckError('physical device identity changed')
                result = read_command([SMARTCTL, '-a', '-j', device], budget(timeout))
                if not result.complete:
                    raise ArrayCheckError('SMART reading timed out or is incomplete')
                sample = smart_sample(json.loads(result.output), entry['roles'], result.code)
                after = os.stat(device)
                if not stat.S_ISBLK(after.st_mode) or after.st_rdev != info.st_rdev:
                    raise ArrayCheckError('physical device identity changed during SMART reading')
                sample['device'] = device
                samples.append(sample)
            except ArrayCheckError as error:
                errors.append(label + ' (' + device + '): ' + str(error))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                errors.append(label + ' (' + device + '): SMART JSON/device observation is unavailable or invalid')
        if len({sample['identity'] for sample in samples}) != len(samples):
            raise ArrayCheckError('Physical SMART identities are duplicated; mapping requires review.')
        save_samples(directory / 'smart.sqlite3', samples, session)
        postflight = read_command([str(GUARDED), 'preflight'], budget(120))
        if not postflight.complete or postflight.code:
            errors.append('Post-SMART mount/UUID preflight failed')
    except ArrayCheckError as error:
        errors.append(str(error))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        errors.append('Array SMART/history check incomplete; inspect OMV tools, guarded preflight and private state.')
    return {'samples': samples, 'errors': errors, 'critical': any(sample['critical'] for sample in samples),
            'complete': not errors, 'at': time.time()}


def smart_lines(report: dict) -> list[str]:
    """Build compact device metrics; unsupported fields are n/a, never fake zeros."""
    lines = []
    for sample in sorted(report['samples'], key=lambda row: not bool(row['critical'])):
        state = 'CRITICAL' if sample['critical'] else ('WARN' if sample['warnings'] else 'OK')
        metrics, deltas = sample['metrics'], sample['deltas']
        fields = []
        for key, short in (('temperature', 'C'), ('hours', 'hours'), ('reallocated', 'realloc'),
                           ('pending', 'pending'), ('uncorrectable', 'uncorr'), ('crc', 'CRC'),
                           ('media_errors', 'media'), ('wear_percent', 'wear%')):
            if key not in metrics:
                continue
            value = metrics[key]
            delta = deltas.get(key)
            suffix = f'({delta:+d})' if delta else ''
            fields.append(f'{short}={value if value is not None else "n/a"}{suffix}')
        label = ','.join(sample['roles'])[:120]
        if sample.get('device'):
            label += ' (' + sample['device'] + ')'
        lines.append(f'{label} [{sample["serial_suffix"]}] {state}: ' + ' '.join(fields))
        for reason in sample['critical'] + sample['warnings']:
            lines.append(label + ': ' + reason)
    lines.extend(report['errors'])
    return lines or ['No physical disk health readings available.']
