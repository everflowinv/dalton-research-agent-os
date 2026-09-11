"""Q1: the three things a person does with the quality loop, end to end.

Against a real state directory with a real published deliverable, because the
CLI's job is to find the document, score it and write the record, and a test
that stubs the finding tests nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.analyst_journal import AnalystJournalAuthority
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.research_quality_cli import main
from dalton_core.research_quality_rubrics import rubric
from dalton_core.research_quality_score import QualityScoreAuthority
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
AUTOMATION = "automation:coverage-mission"
OWNER = "human:lumos"
REVENUE = ("Accenture plc reported Revenues of USD 18742125000 for 2025-09-01..2025-11-30, "
           "up 5.95% year over year from USD 17689545000 in the comparable quarter.")


class CliHarness(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        store = DaltonStore(str(self.state / "core.sqlite"))
        try:
            fixtures = bootstrap_method_authorities(store)
            missions = CoverageMissionAuthority(store)
            params = mission_params(fixtures)
            ref = params.pop("mission_ref")
            params["autonomy"] = {**params["autonomy"],
                                  "may_write": list(params["autonomy"]["may_write"]) + ["deliverable"]}
            mission = missions.create_mission(ref, **params)
            self.mission_ref = ref
            self.playbook_record = fixtures["playbook"]
            self.claim_ref = self.write_claim(store)
            authority = MissionDeliverableAuthority(store)
            record = authority.publish(
                kind="initial_screen", subject_ref=ACN, mission=mission,
                playbook=fixtures["playbook"],
                template_ref="playbook:deliverable_templates.initial_screen",
                sections=self.sections(fixtures),
                summary="Accenture 的收入增速仍在中个位数区间。", actor_ref=AUTOMATION,
            )
            self.deliverable = record["id"]
            self.deliverable_ref = record["deliverable_ref"]
            self.deliverable_hash = record["content_hash"]
        finally:
            store.close()


    def sections(self, fixtures):
        """All eight templated sections, because a screen missing six is a
        different test than the one these are for."""

        titles = fixtures["playbook"]["deliverable_templates"]["initial_screen"]
        written = {
            titles[3]: ("该季报显示季度收入为 USD 18742125000，同比增长 5.95%，增速仍在中个位数区间；"
                        "关键 driver 是 AI 相关转型支出能否比它替掉的传统咨询收入来得快。",
                        [{"text": REVENUE, "claim_version_ref": self.claim_ref,
                          "period": "2025-09-01..2025-11-30"}]),
            titles[7]: ("应盯住新签订单与利用率两条序列；两条目前都不在账本里。", []),
        }
        sections = []
        for title in titles:
            body, numbers = written.get(title, ("", []))
            sections.append({
                "title": title, "body": body, "claim_refs": [self.claim_ref] if body else [],
                "numbers": numbers,
                "gaps": [] if body else ["这一节的证据还没有进入账本"],
            })
        return sections

    def write_claim(self, store):
        claim = {
            "schema_version": "0.2", "id": "claim-version:" + "7" * 60,
            "claim_ref": "claim:test:1", "version": 1, "subject_ref": ACN,
            "metric_or_aspect": "quarterly_revenue_yoy_growth",
            "period": "2025-09-01..2025-11-30", "basis": "official-filing-xbrl",
            "normalized_statement": REVENUE, "claim_kind": "quantitative", "value": "5.95",
            "unit": "percent", "currency": None, "scale": "one", "producer_execution_refs": [],
            "semantic_review_ref": None, "semantic_review_hash": None,
            "candidate_origin_ref": None, "candidate_origin_hash": None,
            "actor_ref": "system:research-auto-commit", "prior_version_ref": None,
            "created_at": "2026-09-07T19:36:01+00:00",
        }
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        with store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1, json.dumps(claim, sort_keys=True),
                 claim["content_hash"], claim["created_at"]),
            )
        return claim["id"]

    def run_cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def scored(self, *extra):
        code, out, _ = self.run_cli("score", "--state-dir", str(self.state),
                                    "--rubric", "initial_screen",
                                    "--target", self.deliverable, *extra)
        return code, json.loads(out)


class ScoreCommandTests(CliHarness):
    def test_scoring_runs_the_deterministic_layer_and_records_it(self):
        code, summary = self.scored()
        self.assertEqual(code, 0)
        self.assertTrue(summary["deterministic"]["passed"])
        self.assertEqual(summary["rubric_hash"], rubric("initial_screen").content_hash)
        self.assertEqual(summary["recorded"]["status"], "fresh")
        self.assertIsNone(summary["judge"])
        self.assertIsNone(summary["verifier"])
        self.assertFalse(summary["verified"])

    def test_explicit_verifier_config_wires_a_second_bounded_model(self):
        judge_path = self.state / "judge.json"
        verifier_path = self.state / "verifier.json"
        judge_path.write_text(json.dumps({"purpose_call_budgets": {
            "quality": {"max_cost_usd": 0.21}}}))
        verifier_path.write_text(json.dumps({"purpose_call_budgets": {
            "quality_verifier": {"max_cost_usd": 0.09}}}))
        made = []

        class Model:
            def __init__(self, config, **kwargs):
                made.append((config, kwargs))

        scored = {
            "deterministic": {"passed": True, "checks": []},
            "judge": {"status": "scored", "scores": [], "summary": {},
                      "model": {"route_decision_ref": "route:judge"}},
            "verifier": {"status": "verified", "verdict": "pass", "findings": [],
                         "model": {"route_decision_ref": "route:verifier",
                                   "purpose": "quality_verifier"}},
        }
        with patch("dalton_core.cockpit_model.CockpitModel", Model), \
                patch("dalton_core.research_quality_cli.score_artefact",
                      return_value=scored) as call:
            code, summary = self.scored(
                "--dry-run", "--model-config", str(judge_path),
                "--verifier-model-config", str(verifier_path))
        self.assertEqual(code, 0)
        self.assertEqual([row[1]["max_cost_usd"] for row in made], [0.21, 0.09])
        self.assertIs(call.call_args.kwargs["model"].__class__, Model)
        self.assertIs(call.call_args.kwargs["verifier_model"].__class__, Model)
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["verifier"]["model"]["route_decision_ref"],
                         "route:verifier")

    def test_a_verifier_config_without_a_judge_config_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_cli("score", "--state-dir", str(self.state),
                         "--rubric", "initial_screen", "--target", self.deliverable,
                         "--verifier-model-config", str(self.state / "verifier.json"))

    def test_a_malformed_verifier_config_fails_without_a_model_call(self):
        judge_path = self.state / "judge.json"
        verifier_path = self.state / "verifier.json"
        judge_path.write_text("{}")
        verifier_path.write_text("[]")
        with patch("dalton_core.cockpit_model.CockpitModel") as model:
            code, _, error = self.run_cli(
                "score", "--state-dir", str(self.state), "--rubric", "initial_screen",
                "--target", self.deliverable, "--model-config", str(judge_path),
                "--verifier-model-config", str(verifier_path))
        self.assertEqual(code, 1)
        self.assertIn("quality_verifier model configuration is invalid", error)
        self.assertEqual(model.return_value.call.call_count, 0)

    def test_the_claim_refs_check_runs_for_real_against_the_core(self):
        _, summary = self.scored()
        check = next(item for item in summary["deterministic"]["checks"]
                     if item["check"] == "claim_refs_resolve")
        self.assertEqual(check["status"], "pass")

    def test_a_deliverable_ref_resolves_to_its_latest_version(self):
        code, out, _ = self.run_cli("score", "--state-dir", str(self.state),
                                    "--rubric", "initial_screen", "--target", self.deliverable_ref)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["target_ref"], self.deliverable)

    def test_scoring_twice_is_a_duplicate(self):
        self.scored()
        _, summary = self.scored()
        self.assertEqual(summary["recorded"]["status"], "duplicate")

    def test_a_dry_run_records_nothing(self):
        code, summary = self.scored("--dry-run")
        self.assertEqual(code, 0)
        self.assertIsNone(summary["recorded"])
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        self.assertEqual(QualityScoreAuthority(store).scores_for(self.deliverable), [])

    def test_a_failing_check_is_reported_and_still_recorded(self):
        # Publishing a document with residual citation wreckage is possible --
        # the deliverable authority does not look for it, which is how the live
        # screens got theirs -- so the score has to be the thing that says so.
        store = DaltonStore(str(self.state / "core.sqlite"))
        try:
            mission = CoverageMissionAuthority(store).active_mission(self.mission_ref)
            record = MissionDeliverableAuthority(store).publish(
                kind="initial_screen", subject_ref=ACN, mission=mission,
                playbook=self.playbook_record,
                template_ref="playbook:deliverable_templates.initial_screen",
                sections=[{"title": "S7 数据跟踪",
                           "body": "首先是收入：、、（同一季度数据重复）显示季度收入为 USD 18742125000。",
                           "claim_refs": [self.claim_ref],
                           "numbers": [{"text": REVENUE, "claim_version_ref": self.claim_ref,
                                        "period": "2025-09-01..2025-11-30"}],
                           "gaps": []}],
                summary="带残句的一版。", actor_ref=AUTOMATION)
        finally:
            store.close()
        code, out, _ = self.run_cli("score", "--state-dir", str(self.state),
                                    "--rubric", "initial_screen", "--target", record["id"])
        summary = json.loads(out)
        self.assertEqual(code, 2)
        self.assertFalse(summary["deterministic"]["passed"])
        self.assertEqual(summary["recorded"]["status"], "fresh")
        residual = next(item for item in summary["deterministic"]["checks"]
                        if item["check"] == "residual_citation_artefacts")
        self.assertEqual(residual["status"], "fail")

    def test_an_unknown_target_is_a_failure_with_a_reason(self):
        code, _, err = self.run_cli("score", "--state-dir", str(self.state),
                                    "--rubric", "initial_screen", "--target", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no deliverable found", err)


class JournalCommandTests(CliHarness):
    def test_adding_feedback_finds_the_targets_hash_by_itself(self):
        code, out, _ = self.run_cli(
            "journal", "add", "--state-dir", str(self.state), "--target", self.deliverable,
            "--verdict", "needs_more_evidence", "--actor-ref", OWNER,
            "--note", "缺 bookings，S7 无法验证订单动能", "--score", "2")
        self.assertEqual(code, 0)
        entry = json.loads(out)["entry"]
        self.assertEqual(entry["target_hash"], self.deliverable_hash)
        self.assertEqual(entry["company_ref"], ACN)
        self.assertEqual(entry["score_override"],
                         {"rubric_ref": "rubric:initial-screen", "overall": 2})

    def test_feedback_can_be_read_back_per_company_in_prompt_shape(self):
        self.run_cli("journal", "add", "--state-dir", str(self.state), "--target", self.deliverable,
                     "--verdict", "revise", "--actor-ref", OWNER, "--note", "重写 S7")
        code, out, _ = self.run_cli("journal", "show", "--state-dir", str(self.state),
                                    "--company-ref", ACN)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertIn("重写 S7", payload["context"]["text"])

    def test_an_automation_principal_is_refused_with_a_reason(self):
        code, _, err = self.run_cli(
            "journal", "add", "--state-dir", str(self.state), "--target", self.deliverable,
            "--verdict", "read", "--actor-ref", AUTOMATION)
        self.assertEqual(code, 1)
        self.assertIn("human:", err)

    def test_the_entry_is_in_the_core_where_a_drafter_could_read_it(self):
        self.run_cli("journal", "add", "--state-dir", str(self.state), "--target", self.deliverable,
                     "--verdict", "useful", "--actor-ref", OWNER)
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        entries = AnalystJournalAuthority(store).for_company(ACN)
        self.assertEqual([entry["verdict"] for entry in entries], ["useful"])


class GoldenCommandTests(unittest.TestCase):
    def test_the_golden_runner_prints_a_row_per_case_and_agrees_with_the_set(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["golden", "run"])
        printed = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("live-acn-v2", printed)
        self.assertIn("honest-unknown", printed)
        self.assertIn("good-first-version", printed)
        self.assertNotIn("DIFF", printed)

    def test_one_rubric_can_be_run_on_its_own(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["golden", "run", "--rubric", "ask_answer"])
        printed = out.getvalue()
        self.assertIn("honest-unknown", printed)
        self.assertNotIn("live-acn-v2", printed)


if __name__ == "__main__":
    unittest.main()
