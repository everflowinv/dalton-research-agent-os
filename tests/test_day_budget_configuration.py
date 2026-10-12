import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import day_budget_configuration as configuration
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore, ThesisImpactDayBudgetExceeded
from tests.test_thesis_impact_budget import admit


class DayBudgetConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "state" / "dalton-core"
        self.state.mkdir(parents=True)
        self.db = self.state / "thesis-impact-budget.sqlite"
        with ThesisImpactBudgetStore(self.db) as budget:
            budget.register_policy(policy_version_id="budget:1", day_cap_micros=1_000_000)
            admit(budget, work="already-reserved", reserved=800_000, policy="budget:1")
        self.models = [self.state / name for name in
                       ("initial-screen-model-config.json", "document-extraction-model-config.json")]
        for path in self.models:
            path.write_text(json.dumps({"budget_db": str(self.db), "budget_policy_ref": "budget:1"}))
        self.service = self.root / "config" / "service.json"
        self.service.parent.mkdir()
        self.service.write_text(json.dumps({"core_db": str(self.state / "core.sqlite"),
            "thesis_impact": {"config": {"budget_db": str(self.db), "budget_policy_version_id": "budget:1"}},
            "bounded_planner": {"config": {"budget_db": str(self.db), "budget_policy_ref": "budget:1"}}}))

    def test_sync_preserves_spend_updates_all_bindings_and_retries_idempotently(self):
        result = configuration.synchronize_day_budget_policy(self.state, cap_usd=2)
        ref = result["policy_version_id"]
        for path in self.models:
            self.assertEqual(json.loads(path.read_text())["budget_policy_ref"], ref)
        service = json.loads(self.service.read_text())
        self.assertEqual(service["thesis_impact"]["config"]["budget_policy_version_id"], ref)
        self.assertEqual(service["bounded_planner"]["config"]["budget_policy_ref"], ref)
        again = configuration.synchronize_day_budget_policy(self.state, cap_usd=2)
        self.assertEqual(again["policy_version_id"], ref)
        with ThesisImpactBudgetStore(self.db) as budget:
            self.assertEqual(budget.policy("budget:1")["day_cap_micros"], 1_000_000)
            # The earlier 0.8 reservation still counts under the new cap.
            with self.assertRaises(ThesisImpactDayBudgetExceeded):
                admit(budget, work="too-much", reserved=1_300_000, policy=ref)
            admitted = admit(budget, work="fits-new-cap", reserved=1_100_000, policy=ref)
            self.assertEqual(admitted["status"], "fresh")

    def test_partial_file_failure_restores_bindings_and_retry_reuses_policy(self):
        originals = {path: path.read_bytes() for path in [*self.models, self.service]}
        original_write = configuration._write
        writes = 0
        def fail_once(path, data):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("simulated write failure")
            return original_write(path, data)
        with patch.object(configuration, "_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                configuration.synchronize_day_budget_policy(self.state, cap_usd=2)
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
        result = configuration.synchronize_day_budget_policy(self.state, cap_usd=2)
        with ThesisImpactBudgetStore(self.db) as budget:
            self.assertEqual(budget.connection.execute(
                "SELECT count(*) FROM thesis_impact_budget_policies").fetchone()[0], 2)
            self.assertEqual(budget.policy(result["policy_version_id"])["day_cap_micros"], 2_000_000)

    def test_invalid_cost_and_cross_environment_service_do_not_mutate(self):
        before = self.models[0].read_bytes()
        for cost in [float("nan"), float("inf"), -1, True]:
            with self.assertRaises(ValueError):
                configuration.synchronize_day_budget_policy(self.state, cap_usd=cost)
        wrong = self.root / "wrong.json"
        wrong.write_text(json.dumps({"core_db": str(self.root / "other" / "core.sqlite")}))
        with self.assertRaises(ValueError):
            configuration.synchronize_day_budget_policy(self.state, wrong, cap_usd=2)
        self.assertEqual(self.models[0].read_bytes(), before)

    def test_dry_run_closed_ledger_creates_no_sidecars_locks_or_receipts(self):
        def files():
            return {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file()}
        for empty_wal in (False, True):
            with self.subTest(empty_wal=empty_wal):
                if empty_wal:
                    Path(str(self.db) + '-wal').write_bytes(b'')
                before = files()
                result = configuration.synchronize_day_budget_policy(
                    self.state, cap_usd=2, dry_run=True)
                self.assertEqual(result['status'], 'planned')
                self.assertEqual(files(), before)

    def test_dry_run_reads_live_wal_policy_without_using_checkpoint_snapshot(self):
        with ThesisImpactBudgetStore(self.db) as budget:
            budget.register_policy(policy_version_id='budget:live',
                                   day_cap_micros=2_000_000, prior_version_id='budget:1')
            for path in self.models:
                value = json.loads(path.read_text())
                value['budget_policy_ref'] = 'budget:live'
                path.write_text(json.dumps(value))
            with patch('dalton_core.budget_policy_snapshot.read_checkpointed_budget_policy',
                       side_effect=AssertionError('must read live WAL')):
                result = configuration.synchronize_day_budget_policy(
                    self.state, cap_usd=3, dry_run=True)
            self.assertEqual(result['status'], 'planned')

    def test_dry_run_refuses_missing_policy_and_incomplete_nonempty_wal(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetConflict
        value = json.loads(self.models[0].read_text())
        value['budget_policy_ref'] = 'budget:missing'
        self.models[0].write_text(json.dumps(value))
        with self.assertRaises(ThesisImpactBudgetConflict):
            configuration.synchronize_day_budget_policy(self.state, cap_usd=2, dry_run=True)
        Path(str(self.db) + '-wal').write_bytes(b'uncheckpointed-pages')
        with self.assertRaisesRegex(sqlite3.OperationalError, 'nonempty WAL'):
            configuration.synchronize_day_budget_policy(self.state, cap_usd=2, dry_run=True)


if __name__ == "__main__":
    unittest.main()
