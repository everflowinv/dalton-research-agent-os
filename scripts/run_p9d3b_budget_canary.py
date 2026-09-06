#!/usr/bin/env python3
"""Offline budget/broker conformance: real local socket, synthetic provider only.

Never reads live config, approved source material or real credentials. The
fixture uses a known synthetic HMAC key and an isolated SQLite budget authority.
It cannot make an external model call. Keep distinct from a paid model canary.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_document_extraction_admission import BrokerAdmissionTests


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    suite = BrokerAdmissionTests()
    suite.setUp()
    try:
        suite.test_real_adapter_socket_protocol_with_synthetic_provider_and_budget()
        budget = suite.b.connection
        admissions = [dict(row) for row in budget.execute(
            'SELECT admission_id,policy_version_id,day,work_order_ref,attempt_number,phase,route_decision_ref,reserved_micros,content_hash FROM thesis_impact_day_admissions')]
        settlements = [dict(row) for row in budget.execute(
            'SELECT settlement_id,admission_id,actual_micros,usage_entry_ref,content_hash FROM thesis_impact_day_settlements')]
        bindings = [json.loads(row[0]) for row in budget.execute('SELECT record_json FROM model_mission_budget_bindings')]
        costs = [dict(row) for row in suite.h.h.core.connection.execute(
            'SELECT cost_entry_id,usage_entry_ref,amount_micros,currency,cost_status,content_hash FROM observability_cost_entries')]
        result = {'ok': True, 'transport': 'real_local_unix_socket', 'provider': 'synthetic_fixture',
                  'paid_provider_calls': 0, 'external_network_calls': 0, 'live_writes': 0,
                  'synthetic_broker_calls': 1, 'duplicate_second_call': True,
                  'admissions': admissions, 'settlements': settlements, 'mission_bindings': bindings,
                  'core_cost_entries': costs, 'real_model_canary': {'status': 'not_attempted',
                      'max_calls_if_separately_admitted': 1, 'max_total_usd_if_separately_admitted': 0.05,
                      'actual_calls': 0, 'actual_usd': 0,
                      'blocker': 'No approved extraction configuration/shared live budget binding verified; no planner permission reused.'}}
        path=output/'result.json'
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return result
    finally:
        suite.doCleanups()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.output.resolve())
