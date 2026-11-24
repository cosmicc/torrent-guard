"""Synthetic guarded scripts and disk data; no host UUIDs, serials or credentials."""
from scripts.guarded_adapter import ORIGINAL_LIMITS


def guarded_script() -> str:
    """Represent the adapter's recognized shell contract using disposable test jobs."""
    return '''#!/usr/bin/env bash
set -Eeuo pipefail
MODE="${1:-}"
CONF=/etc/snapraid.conf
case "$MODE" in preflight|diff|sync|scrub) ;; *) exit 64 ;; esac
MAX_ADDED=250
MAX_REMOVED=50
MAX_UPDATED=100
MAX_MOVED=100
MAX_COPIED=250
MAX_RESTORED=50
LAST_DETAIL=/unused-fixture
say() { printf '%s\\n' "$*"; }
fail() { say "ERROR: $*"; exit 1; }
check_mount() { :; }
preflight() {
  [[ "${TEST_MISSING:-0}" == 0 ]] || fail "fixture is NOT a mount point"
}
summary() { [[ "$1" == equal ]] && printf '%s\\n' "${TEST_EQUAL:-10000}"; }
state_status() { :; }
scan_diff() {
  DIFF_STATE=equal
  DIFF_ADDED=${TEST_ADDED:-0}
  DIFF_REMOVED=${TEST_REMOVED:-0}
  DIFF_UPDATED=${TEST_UPDATED:-0}
  DIFF_MOVED=${TEST_MOVED:-0}
  DIFF_COPIED=${TEST_COPIED:-0}
  DIFF_RESTORED=${TEST_RESTORED:-0}
  DIFF_RELOCATED=${TEST_RELOCATED:-0}
  if (( DIFF_ADDED + DIFF_REMOVED + DIFF_UPDATED + DIFF_MOVED + DIFF_COPIED + DIFF_RESTORED + DIFF_RELOCATED > 0 )); then
    DIFF_STATE=diff
  fi
}
run_snapraid() { say "MUTATING OPERATION $*"; }
''' + ORIGINAL_LIMITS + '''
preflight
case "$MODE" in
  preflight) say "PASS: preflight" ;;
  diff) state_status; scan_diff; fail "Differences or unfinished sync found" ;;
  sync)
    state_status
    scan_diff
    check_thresholds
    preflight
    run_snapraid sync sync
    ;;
  scrub) say "MUTATING OPERATION scrub" ;;
esac
'''


def ata_data(serial: str = 'fixture-drive-000001', **counters) -> dict:
    """Create a healthy ATA payload; overrides emulate raw SMART sector counters."""
    return {'serial_number': serial, 'device': {'protocol': 'ATA'},
            'smart_status': {'passed': True}, 'temperature': {'current': 30},
            'power_on_time': {'hours': 100},
            'ata_smart_attributes': {'table': [
                {'id': number, 'raw': {'value': counters.get(key, 0)}}
                for key, number in (('reallocated', 5), ('pending', 197), ('uncorrectable', 198), ('crc', 199))]}}
