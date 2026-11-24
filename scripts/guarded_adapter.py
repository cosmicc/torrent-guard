"""Add a narrowly recognized read-only gate without replacing operator array policy.

The shared script's mounts, UUIDs, limits, logs, checks and sync/scrub branches
remain intact. Unknown versions are refused for review, never guessed or reset.
"""
import hashlib
import os
from pathlib import Path
import re
import tempfile

from scripts.maintenance_install import InstallError


MARKER = '# Torrent Guard check-sync adapter v1'
ORIGINAL_LIMITS = '''check_thresholds() {
  (( DIFF_ADDED <= MAX_ADDED )) || fail "Added $DIFF_ADDED exceeds $MAX_ADDED; review manually."
  (( DIFF_REMOVED <= MAX_REMOVED )) || fail "Removed $DIFF_REMOVED exceeds $MAX_REMOVED; review manually."
  (( DIFF_UPDATED <= MAX_UPDATED )) || fail "Updated $DIFF_UPDATED exceeds $MAX_UPDATED; review manually."
  (( DIFF_MOVED <= MAX_MOVED )) || fail "Moved $DIFF_MOVED exceeds $MAX_MOVED; review manually."
  (( DIFF_COPIED <= MAX_COPIED )) || fail "Copied $DIFF_COPIED exceeds $MAX_COPIED; review manually."
  (( DIFF_RESTORED <= MAX_RESTORED )) || fail "Restored $DIFF_RESTORED exceeds $MAX_RESTORED; review manually."
  (( DIFF_RELOCATED <= MAX_COPIED )) || fail "Relocated $DIFF_RELOCATED exceeds $MAX_COPIED; review manually."
}'''
FUNCTIONS = r'''# Torrent Guard check-sync adapter v1
# Structured counts precede threshold failures; original limits remain in force.
tg_diff_report() {
  local variable n key limit separator
  DIFF_EQUAL="$(summary equal "$LAST_DETAIL")"
  for variable in DIFF_EQUAL DIFF_ADDED DIFF_REMOVED DIFF_UPDATED DIFF_MOVED DIFF_COPIED DIFF_RESTORED DIFF_RELOCATED MAX_ADDED MAX_REMOVED MAX_UPDATED MAX_MOVED MAX_COPIED MAX_RESTORED; do
    n="${!variable}"
    [[ "$n" =~ ^(0|[1-9][0-9]{0,11})$ ]] || fail "Invalid numeric diff count/limit."
    printf -v "$variable" '%s' "$((10#$n))"
  done
  limit="${TORRENT_GUARD_DIFF_MAX_PERCENT:-5}"
  [[ "$limit" =~ ^[0-9]{1,3}$ ]] || fail "Invalid changed-file percentage limit."
  limit="$((10#$limit))"
  (( limit >= 1 && limit <= 100 )) || fail "Changed-file percentage must be 1 through 100."
  TG_DIFF_LIMIT="$limit"
  [[ "$CONF" =~ ^/[A-Za-z0-9_./:+-]+$ ]] || fail "Unsupported protected configuration path."
  printf 'TG_DIFF_V1 {"state":"%s","config":"%s","max_percent":%s,"counts":{' "$DIFF_STATE" "$CONF" "$limit"
  separator=''
  for key in equal added removed updated moved copied restored relocated; do
    variable="DIFF_${key^^}"
    printf '%s"%s":%s' "$separator" "$key" "${!variable}"
    separator=','
  done
  printf '},"limits":{'
  separator=''
  for key in added removed updated moved copied restored relocated; do
    variable="MAX_${key^^}"
    [[ "$key" == relocated ]] && variable=MAX_COPIED
    printf '%s"%s":%s' "$separator" "$key" "${!variable}"
    separator=','
  done
  printf '}}\n'
}
tg_percent_guard() {
  local total changed
  total=$((DIFF_EQUAL + DIFF_REMOVED + DIFF_UPDATED + DIFF_MOVED + DIFF_RESTORED + DIFF_RELOCATED))
  changed=$((DIFF_REMOVED + DIFF_UPDATED))
  if (( total > 0 && changed * 100 >= TG_DIFF_LIMIT * total )); then
    fail "Removed/updated file percentage reaches $TG_DIFF_LIMIT%; manual review required."
  fi
}
'''
GATE = '''  check-sync)
    state_status
    scan_diff
    check_thresholds
    say "PASS: pre-sync checks passed; no parity modifications made."
    ;;
'''


def adapt_guarded(text: str) -> str:
    """Produce an idempotent additive patch only for the recognized guarded workflow."""
    if not re.search(r'^CONF=(?:/[A-Za-z0-9_./:+-]+|"/[A-Za-z0-9_./:+-]+")$', text, re.MULTILINE):
        raise InstallError('Shared guarded configuration assignment is unsupported; preserved for review')
    updated_limits = ORIGINAL_LIMITS.replace('check_thresholds() {\n', 'check_thresholds() {\n  tg_diff_report\n')
    updated_limits = updated_limits[:-1] + '  tg_percent_guard\n}'
    if MARKER in text:
        if (text.count(FUNCTIONS) != 1 or text.count(updated_limits) != 1 or text.count(GATE) != 1
                or 'case "$MODE" in preflight|diff|check-sync|sync|scrub) ;; *)' not in text):
            raise InstallError('Installed guarded adapter differs; preserve it for review')
        return text
    required = ('case "$MODE" in preflight|diff|sync|scrub) ;; *)',
                '\npreflight\ncase "$MODE" in\n',
                '  sync)\n    state_status\n    scan_diff\n    check_thresholds\n',
                'check_mount() {', 'scan_diff() {', 'state_status() {',
                '    preflight\n    run_snapraid sync sync\n')
    if text.count(ORIGINAL_LIMITS) != 1 or any(text.count(fragment) != 1 for fragment in required):
        raise InstallError('Shared guarded script is unrecognized; no array checks or limits were changed')
    result = text.replace('preflight|diff|sync|scrub', 'preflight|diff|check-sync|sync|scrub')
    result = result.replace(ORIGINAL_LIMITS, FUNCTIONS + '\n' + updated_limits)
    return result.replace('  sync)\n    state_status', GATE + '  sync)\n    state_status', 1)


def install_guarded_adapter(installer) -> None:
    """Back up exact original bytes privately, syntax-check and atomically extend the script."""
    path = installer.paths.path('/usr/local/sbin/snapraid-guarded')
    installer.directory(path.parent)
    info = installer.info(path)
    if not info.st_mode & 0o111:
        raise InstallError('Shared guarded script must already be executable')
    original = path.read_bytes()
    try:
        updated = adapt_guarded(original.decode('utf-8')).encode()
    except UnicodeError as error:
        raise InstallError('Shared guarded script is not UTF-8; preserved for review') from error
    if updated == original:
        return
    history = installer.paths.path('/var/lib/torrent-guard-maintenance-array')
    installer.directory(history, private=True)
    digest = hashlib.sha256(original).hexdigest()
    installer.file(history / ('guarded-' + digest + '.sh'), original, 0o600, private=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.tg-guarded-', dir=path.parent)
    try:
        os.fchmod(descriptor, info.st_mode & 0o777)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        installer.run(['bash', '-n', temporary])
        installer.info(path)
        if path.read_bytes() != original:
            raise InstallError('Shared guarded script changed during setup; preserved for review')
        os.replace(temporary, path)
        installer.output('Added read-only pre-sync gate and percentage guard; existing limits and mount checks retained.')
    finally:
        Path(temporary).unlink(missing_ok=True)
