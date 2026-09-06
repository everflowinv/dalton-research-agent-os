#!/usr/bin/env python3
"""Isolated synthetic preflight; no broker, credential, live state or model access.

Uses installed dalton_core when PYTHONPATH does not include src. Fixture setup
provisions only temporary synthetic authorities; the preflight never does.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # fixture helpers, never a src import override
import dalton_core
from tests.test_document_extraction_preflight import ExtractionPreflightTests, disk_state


def run(output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    test = ExtractionPreflightTests()
    test.setUp()
    try:
        test.test_pass_is_non_reserving_and_no_core_scheduler_staging_or_disk_writes()
        before = disk_state(test.h.root)
        passed = test.check()
        test.assertEqual(before, disk_state(test.h.root))
        test.assertTrue(passed['local_checks_passed'])
        test.seed_spend(amount=1000000, prior_day=True)
        before_blocked = disk_state(test.h.root)
        blocked = test.check()
        test.assert_blocked(blocked, 'owner_budget_exceeded')
        test.assertEqual(before_blocked, disk_state(test.h.root))
        result = {
            'ok': True, 'fixture_only': True, 'package_path': dalton_core.__file__,
            'real_model_calls': 0, 'broker_connections': 0, 'credential_contents_read': False,
            'preflight_persistent_writes': 0, 'live_state_accessed': False,
            'core_scheduler_staging_dumps_and_total_changes_unchanged': True,
            'authority_db_wal_bytes_modes_mtimes_unchanged': True,
            'sqlite_shm_note': 'Read-only WAL uses volatile SHM read-mark/locking slots, not authority writes.',
            'pass_preview': passed, 'prior_day_unsettled_blocked_preview': blocked,
        }
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        (output / 'result.json').chmod(0o600)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        test.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
