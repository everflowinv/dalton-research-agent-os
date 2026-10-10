"""Batch an: version binding, invariant gating by version, cross-product
conflicts, citation lifecycle and CIK normalisation -- on a synthetic company.

Every rule here is general: the company is Synthetic Widgets Co.
(``tests/synthetic_company.py``) or a synthetic SEC registrant, never a
covered company's fixture.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core import economic_invariants as ei
from dalton_core import product_consistency as pc
from dalton_core.mission_company_cik import (
    NonCanonicalCompanyRef, canonical_spelling, noncanonical_refs_in_text, resolve_alias,
    validate_company_ref,
)
from dalton_core.product_rewrite_flags import (
    HISTORICAL_CITATION_LABEL, affected_sections, citation_display,
)
from dalton_core.store import DaltonStore, content_hash
from tests.synthetic_company import COMPANY

CORE = Path(__file__).resolve().parents[1] / "src" / "dalton_core"
#: schema file -> tables the tests read
SCHEMAS = {
    "forecast_driver_schema.sql": ("forecast_model_versions", "forecast_model_annual_projections",
                                   "forecast_model_filing_proofs"),
    "model_forecast_schema.sql": ("model_forecast_line_versions",),
    "forecast_sensitivity_schema.sql": ("sensitivity_projections",),
    "economic_invariant_schema.sql": ("economic_invariant_verdicts",),
    "company_scenario_schema.sql": ("company_scenario_sets", "company_scenario_versions"),
    "mission_deliverable_schema.sql": ("mission_deliverable_versions",),
    "research_quality_schema.sql": ("research_quality_score_versions",),
    "zero_base_review_schema.sql": ("zero_base_review_versions",),
    "return_attribution_schema.sql": ("holding_period_return_bridges",),
    "deep_insight_gate_schema.sql": ("deep_insight_gate_versions",),
    "company_dossier_schema.sql": ("company_dossier_versions",),
    "debate_map_schema.sql": ("debate_map_versions",),
    "catalyst_calendar_schema.sql": ("catalyst_calendar_versions",),
    "consensus_estimate_schema.sql": ("consensus_estimate_versions",),
    "schema.sql": ("thesis_versions", "claim_versions"),
}
TODAY = "2026-10-09"
MODEL = "forecast-model:" + COMPANY


def _create_statement(text: str, table: str) -> str:
    clean = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
    match = re.search(r'CREATE TABLE (?:IF NOT EXISTS )?"?' + re.escape(table) + r'"?\s*\(', clean)
    if match is None:
        raise AssertionError(f"no CREATE TABLE for {table}")
    depth, index = 0, match.end() - 1
    for index in range(match.end() - 1, len(clean)):
        if clean[index] == "(":
            depth += 1
        elif clean[index] == ")":
            depth -= 1
            if depth == 0:
                break
    return clean[match.start():index + 1].replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1) \
        .replace("IF NOT EXISTS IF NOT EXISTS", "IF NOT EXISTS")


def make_core(connection: sqlite3.Connection) -> sqlite3.Connection:
    for name, tables in SCHEMAS.items():
        text = (CORE / name).read_text(encoding="utf-8")
        for table in tables:
            connection.execute(_create_statement(text, table))
    connection.row_factory = sqlite3.Row
    return connection


class Core:
    """A synthetic Core: helpers that append rows the way the lanes shape them."""

    def __init__(self) -> None:
        self.c = make_core(sqlite3.connect(":memory:"))
        self.clock = 0

    def at(self) -> str:
        self.clock += 1
        return f"2026-10-0{1 + self.clock // 60}T{self.clock % 24:02d}:00:00+00:00"

    def insert(self, table: str, **columns) -> None:
        keys = list(columns)
        self.c.execute(f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
                       [columns[key] for key in keys])

    def model(self, number: int, *, results=None, currency="USD", unit="usd") -> str:
        ref = f"forecast-model-version:synw:{number}"
        record = {"id": ref, "company_ref": COMPANY, "currency": currency, "unit": unit,
                  "results": results or [], "created_at": self.at(), "version": number}
        self.insert("forecast_model_versions", version_id=ref, model_ref=MODEL, version_number=number,
                    prior_version_id=None if number == 1 else f"forecast-model-version:synw:{number - 1}",
                    company_ref=COMPANY, spec_ref="spec", inputs_hash="i", body_hash=f"b{number}",
                    record_json=json.dumps(record), content_hash=f"h{number}", actor_ref="automation:t",
                    created_at=record["created_at"])
        return ref

    def deliverable(self, kind: str, number: int, record: dict) -> str:
        ref = f"mission-deliverable-version:{kind}-synw-{number}"
        record = {"id": ref, "kind": kind, "subject_ref": COMPANY, "created_at": self.at(), **record}
        self.insert("mission_deliverable_versions", version_id=ref, deliverable_ref=f"deliverable:{kind}:synw",
                    version_number=number, prior_version_ref=None, mission_version_ref="m",
                    mission_version_hash="m", playbook_version_ref="p", playbook_version_hash="p", kind=kind,
                    subject_ref=COMPANY, record_json=json.dumps(record), content_hash=ref,
                    actor_ref="automation:t", created_at=record["created_at"])
        return ref

    def thesis(self, number: int, **content) -> str:
        ref = f"thesis-version:synw-{number}"
        self.insert("thesis_versions", version_id=ref, thesis_id="thesis:synw", version_number=number,
                    content_json=json.dumps({"id": ref, **content}), content_hash=ref,
                    authority_kind="human_admission", authority_ref="a", admission_decision_id="a",
                    committed_by="human:x",
                    created_at=self.at())
        return ref

    def claim(self, n: int, text: str = "fact", created_at: str | None = None, **extra) -> str:
        ref = "claim-version:" + content_hash({"n": n})
        self.insert("claim_versions", claim_version_id=ref, claim_ref=f"claim:{n}", version_number=1,
                    claim_json=json.dumps({"id": ref, "normalized_statement": text, **extra}),
                    content_hash=ref, created_at=created_at or self.at())
        return ref


def derived_on_model_v1(core: Core) -> dict[str, str]:
    """One of every derived object, each bound to model v1 (and thesis v1)."""

    model = core.model(1)
    thesis = core.thesis(1, confidence="medium", statement="Widgets demand compounds.")
    refs = {"model": model, "thesis": thesis}
    line = "forecast-line-version:synw:revenue-usd:2026-09-30:1"
    core.insert("model_forecast_line_versions", version_id=line,
                line_ref="forecast-line:synw:revenue-usd:2026-09-30", version_number=1, subject_ref=COMPANY,
                record_json=json.dumps({"id": line, "scenario_version_ref": model, "unit": "usd",
                                        "currency": "USD", "metric_or_aspect": "metric:revenue-usd",
                                        "period": {"end": "2026-09-30"}, "created_at": core.at()}),
                content_hash="l", actor_ref="automation:t", created_at=core.at())
    projection = "sensitivity-projection:synw:1"
    core.insert("sensitivity_projections", projection_id=projection, projection_ref=f"sensitivity-projection:{COMPANY}",
                version_number=1, company_ref=COMPANY, model_version_ref=model, model_version_hash="h1",
                inputs_hash="i", fingerprint="f", body_hash="b",
                record_json=json.dumps({"id": projection, "unit": "usd", "currency": "USD",
                                        "consensus_bridge": {"metrics": []}, "created_at": core.at()}),
                content_hash="s", actor_ref="automation:t", created_at=core.at())
    core.insert("forecast_model_annual_projections", model_version_id=model, company_ref=COMPANY,
                model_content_hash="h1", inputs_hash="i", calendar_hash="c",
                record_json=json.dumps({"projection_ref": "annual-projection:" + model, "model_version_ref": model}),
                content_hash="a", created_at=core.at())
    set_id = "company-scenario-set:synw"
    core.insert("company_scenario_sets", set_id=set_id, company_ref=COMPANY, origin="automation",
                record_json=json.dumps({"id": set_id, "anchors": {"model_version_ref": model}}),
                content_hash="set", actor_ref="automation:t", created_at=core.at())
    scenario = "company-scenario-version:synw-base-1"
    core.insert("company_scenario_versions", version_id=scenario, scenario_ref="company-scenario:synw-base",
                version_number=1, set_ref=set_id, company_ref=COMPANY,
                record_json=json.dumps({"id": scenario, "key": "base", "state": "live",
                                        "model_binding": {"model_version_ref": model}}),
                content_hash="sc", actor_ref="automation:t", created_at=core.at())
    refs["preview"] = core.deliverable("earnings_preview", 1, {
        "summary": f"{COMPANY} 2026-11-05 preview（estimated）｜上游版本 [{model}]｜我们的模型 [{model}]",
        "sections": []})
    verdict = f"economic-invariant-refusal:forecast_model_version:{COMPANY}:1"
    core.insert("economic_invariant_verdicts", verdict_id=verdict,
                verdict_ref=f"economic-invariant-refusal:forecast_model_version:{COMPANY}", version_number=1,
                company_ref=COMPANY, output_kind="forecast_model_version", output_ref=model, status="unavailable",
                rule_ref=ei.RULE_REF, subject_hash="s", failure_count=1,
                record_json=json.dumps({"id": verdict, "output_ref": model, "status": "unavailable",
                                        "results": []}),
                content_hash="v", actor_ref="core:x", created_at=core.at())
    refs["screen"] = core.deliverable("initial_screen", 1, {"sections": []})
    core.insert("research_quality_score_versions", version_id="quality-score-version:synw-1",
                score_ref="quality-score:initial-screen:synw", version_number=1, artefact_kind="initial_screen",
                target_ref=refs["screen"], target_hash="t", subject_ref=COMPANY, rubric_ref="rubric:initial-screen",
                rubric_hash=pc._rubric_hash("rubric:initial-screen") or "r", scorer_version="0.1",
                model_config_fingerprint="f", scoring_identity_hash="s", judge_status=None,
                record_json=json.dumps({"id": "quality-score-version:synw-1"}), content_hash="q",
                actor_ref="automation:t", created_at=core.at())
    core.insert("zero_base_review_versions", version_id="zero-base-review-version:synw-1",
                review_ref="zero-base-review:synw", version_number=1, mission_ref="m", mission_version_ref="m",
                company_ref=COMPANY, trigger="monthly", period_label="2026-09", form_a_view="yes",
                rewritten_line_count=0, stale_debate_count=0, candidate_count=0,
                next_verification_date="2026-10-20", inputs_hash="i",
                record_json=json.dumps({"id": "zero-base-review-version:synw-1", "as_of": "2026-09-14",
                                        "upstream_bindings": pc.upstream_bindings([thesis]),
                                        "answers": {"citations": [thesis],
                                                    "next_verification": {"date": "2026-10-20",
                                                                          "what": "x", "because": "y"}}}),
                content_hash="z", actor_ref="automation:t", created_at=core.at())
    core.insert("holding_period_return_bridges", version_id="holding-period-return-version:synw-1",
                bridge_ref="holding-period-return:synw", version_number=1, company_ref=COMPANY,
                as_of=TODAY, available_leg_count=0, body_hash="b",
                record_json=json.dumps({"inputs": {"forecast_model_version_ref": model}, "legs": []}),
                content_hash="r", actor_ref="automation:t", created_at=core.at())
    return refs


class VersionBindingTests(unittest.TestCase):
    def test_model_v1_to_v2_marks_every_derived_object_stale(self):
        core = Core()
        refs = derived_on_model_v1(core)
        before = pc.evaluate_bindings(core.c, COMPANY, today=TODAY)
        self.assertEqual({item["kind"] for item in before}, set(pc.DERIVED_LABELS))
        self.assertTrue(all(item["binding_status"] == pc.CURRENT for item in before),
                        [(i["kind"], i["binding_status"]) for i in before])
        core.model(2)
        core.thesis(2, confidence="medium", statement="Widgets demand compounds faster.")
        core.deliverable("initial_screen", 2, {"sections": []})
        after = pc.evaluate_bindings(core.c, COMPANY, today=TODAY)
        stale = {item["kind"]: item for item in after}
        self.assertEqual({kind for kind, item in stale.items() if item["binding_status"] == pc.STALE},
                         set(pc.DERIVED_LABELS))
        line = stale["forecast_line"]
        self.assertEqual(line["stale_upstreams"][0]["current_ref"], "forecast-model-version:synw:2")
        self.assertIn("已过期", line["display_note"])
        self.assertIn("第 1 版，当前第 2 版", line["display_note"])
        summary = pc.stale_bindings(core.c, today=TODAY)
        self.assertEqual(summary["stale"], len(pc.DERIVED_LABELS))
        self.assertEqual(summary["by_company"][COMPANY]["stale"], len(pc.DERIVED_LABELS))
        signals = {item["code"]: item for item in pc.redraft_signals(
            core.c, COMPANY, "earnings_preview", version_ref=refs["preview"], today=TODAY)}
        self.assertTrue(signals["stale_binding"]["fired"])

    def test_a_preview_whose_report_date_passed_is_history_not_stale(self):
        core = Core()
        model = core.model(1)
        core.deliverable("earnings_preview", 1, {"summary": f"x 2026-08-01 preview｜我们的模型 [{model}]"})
        core.model(2)
        (item,) = pc.evaluate_bindings(core.c, COMPANY, today=TODAY)
        self.assertEqual(item["binding_status"], pc.HISTORICAL)

    def test_an_object_naming_no_upstream_is_unbound_not_current(self):
        core = Core()
        core.insert("holding_period_return_bridges", version_id="holding-period-return-version:x",
                    bridge_ref="holding-period-return:x", version_number=1, company_ref=COMPANY, as_of=TODAY,
                    available_leg_count=0, body_hash="b", record_json=json.dumps({"inputs": {}, "legs": []}),
                    content_hash="r", actor_ref="automation:t", created_at="2026-10-01")
        (item,) = pc.evaluate_bindings(core.c, COMPANY, today=TODAY)
        self.assertEqual(item["binding_status"], pc.UNBOUND)
        self.assertEqual(item["display_status"], "未记录上游版本")

    def test_upstream_bindings_keep_only_versioned_refs_in_stable_order(self):
        rows = pc.upstream_bindings(["claim-version:" + "a" * 64, "forecast-model-version:x:2",
                                     {"ref": "consensus-estimate-version:c", "role": "consensus"},
                                     "forecast-model-version:x:2", None])
        self.assertEqual([row["ref"] for row in rows],
                         ["consensus-estimate-version:c", "forecast-model-version:x:2"])
        self.assertEqual(rows[0]["kind"], "consensus")


class InvariantGatingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        make_core(self.store.connection)

    def report(self, output_ref: str, *, segment_checked: int = 0) -> ei.InvariantReport:
        return ei.InvariantReport(
            output_kind=ei.FORECAST_MODEL, output_ref=output_ref, company_ref=COMPANY,
            subject_hash=content_hash(output_ref),
            results=(ei.InvariantResult(ei.DIRECTION, ei.PASS, checked=4),
                     ei.InvariantResult(ei.BAND, ei.PASS, checked=0),
                     ei.InvariantResult(ei.SEGMENT_SUM, ei.NOT_APPLICABLE, reason="no breakdown",
                                        checked=segment_checked)))

    def test_publication_needs_the_verdict_for_the_exact_version(self):
        ei.gate(self.store, self.report("forecast-model-version:synw:1"))
        connection = self.store.connection
        self.assertTrue(ei.publication_gate(connection, "forecast-model-version:synw:1")["publishable"])
        newer = ei.publication_gate(connection, "forecast-model-version:synw:2")
        self.assertFalse(newer["publishable"])
        self.assertEqual(newer["status"], "unverified")
        self.assertIn("未验证", newer["label"])

    def test_not_applicable_and_checked_zero_read_uncovered_never_pass(self):
        ei.gate(self.store, self.report("forecast-model-version:synw:1"))
        verdict = ei.verdict_for_output(self.store.connection, "forecast-model-version:synw:1")
        rules = {row["invariant"]: row for row in ei.rule_coverage(verdict)}
        self.assertEqual(rules[ei.DIRECTION]["display_status"], "通过")
        self.assertEqual(rules[ei.BAND]["display_status"], "未覆盖")  # pass with checked=0
        self.assertEqual(rules[ei.SEGMENT_SUM]["display_status"], "未覆盖")
        self.assertEqual(rules[ei.SOLVER_BOUNDS]["display_status"], "未覆盖")  # not run at all
        self.assertFalse(rules[ei.BAND]["applicable"])

    def test_coverage_report_reads_the_current_version_only(self):
        connection = self.store.connection
        core = Core()
        core.c = connection
        core.model(1)
        ei.gate(self.store, self.report("forecast-model-version:synw:1"))
        core.model(2)
        report = ei.coverage_report(connection, COMPANY)[COMPANY][ei.FORECAST_MODEL]
        self.assertEqual(report["output_ref"], "forecast-model-version:synw:2")
        self.assertFalse(report["verdict_for_current"])
        self.assertEqual(report["checked_rules"], 0)
        self.assertEqual({row["display_status"] for row in report["rules"]}, {"未覆盖"})
        ei.gate(self.store, self.report("forecast-model-version:synw:2"))
        report = ei.coverage_report(connection, COMPANY)[COMPANY][ei.FORECAST_MODEL]
        self.assertTrue(report["verdict_for_current"])
        self.assertEqual(report["checked_rules"], 1)
        self.assertEqual(report["total_rules"], len(ei.INVARIANTS))

    def test_the_version_ledger_is_append_only(self):
        ei.gate(self.store, self.report("forecast-model-version:synw:1"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("UPDATE economic_invariant_version_verdicts SET status='unavailable'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO economic_invariant_version_verdicts VALUES('x','r','k','c','available','',"
                "'{}','h','t')")


class PublishedProjectionTests(unittest.TestCase):
    """The real sensitivity authority: each published version has its own verdict."""

    def test_a_published_projection_carries_bindings_and_its_exact_verdict(self):
        from dalton_core.forecast_sensitivity import SensitivityProjectionAuthority, build_projection
        from tests.test_forecast_sensitivity import model as sensitivity_model, store as memory_store

        store = memory_store()
        self.addCleanup(store.close)
        authority = SensitivityProjectionAuthority(store)
        model = sensitivity_model()
        stored = authority.publish(build_projection(model))
        self.assertEqual(stored["status"], "fresh")
        self.assertEqual(stored["upstream_bindings"][0]["ref"], model["id"])
        gate = ei.publication_gate(store.connection, stored["id"])
        self.assertTrue(gate["publishable"], gate)
        again = authority.publish(build_projection(model))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(store.connection.execute(
            "SELECT COUNT(*) FROM economic_invariant_version_verdicts").fetchone()[0], 1)


def _gate(core: Core, classification: str, refs: list[str], answers: list[dict] | None = None,
          number: int = 1) -> dict:
    ref = f"deep-insight-gate-version:synw:{number}"
    record = {"id": ref, "classification": classification, "created_at": core.at(),
              "answers": answers or [{"question_ref": "q1", "confidence": "medium",
                                      "sentences": [{"text": "分类", "refs": refs}]}]}
    core.insert("deep_insight_gate_versions", version_id=ref, gate_ref="deep-insight-gate:synw",
                version_number=number, company_ref=COMPANY, change_reason="drafted", body_hash="b",
                evidence_scope_hash="e", record_json=json.dumps(record), content_hash=ref,
                actor_ref="automation:t", created_at=record["created_at"])
    return record


def _dossier(core: Core, classification: str, refs: list[str], *, variant_refs=(), number: int = 1,
             extra: dict | None = None) -> dict:
    ref = f"company-dossier-version:synw:{number}"
    record = {"id": ref, "created_at": core.at(),
              "industry_classification": {"classification": classification,
                                          "slots": [{"slot_id": "basis", "sentences": [{"refs": refs}]}]},
              "variant_view": {"slots": [{"slot_id": "our_view", "sentences": [{"refs": list(variant_refs)}]}]},
              "sections": [], **(extra or {})}
    core.insert("company_dossier_versions", version_id=ref, dossier_ref="company-dossier:synw",
                version_number=number, company_ref=COMPANY, change_reason="drafted", body_hash="b",
                evidence_scope_hash="e", input_fingerprints_json="{}", record_json=json.dumps(record),
                content_hash=ref, actor_ref="automation:t", created_at=record["created_at"])
    return record


class ConflictTests(unittest.TestCase):
    def kinds(self, core: Core) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for item in pc.detect_conflicts(core.c, today=TODAY):
            out.setdefault(item["conflict_kind"], []).append(item)
        return out

    def test_classification_switch_without_new_evidence_is_high(self):
        core = Core()
        old = core.claim(1)
        _gate(core, "turnaround", [old])
        _dossier(core, "structural_growth", [old])
        (conflict,) = self.kinds(core)["classification"]
        self.assertEqual(conflict["severity"], "high")
        self.assertEqual(conflict["evidence"]["switch_evidence_refs"], [])
        self.assertIn("切换理由不明", conflict["detail"])

    def test_classification_switch_names_the_new_evidence(self):
        core = Core()
        old = core.claim(1)
        _gate(core, "turnaround", [old])
        new = core.claim(2)
        _dossier(core, "structural_growth", [old, new])
        (conflict,) = self.kinds(core)["classification"]
        self.assertEqual(conflict["severity"], "medium")
        self.assertEqual(conflict["evidence"]["switch_evidence_refs"], [new])

    def test_initial_screen_prose_classification_counts(self):
        self.assertEqual(pc.classification_in_prose("判断：公司更接近结构性增长，因为需求迁移。"),
                         "structural_growth")
        self.assertIsNone(pc.classification_in_prose("困境反转型的前提也不成立，AI 转型仍在进行"))
        core = Core()
        _gate(core, "contract_compounder", [core.claim(1)])
        core.deliverable("initial_screen", 1, {"sections": [
            {"title": "S1", "body": "我们把公司归为困境反转：收入下滑后靠自救。", "claim_refs": []}]})
        (conflict,) = self.kinds(core)["classification"]
        self.assertEqual(set(conflict["products"]), {"deep_insight_gate", "initial_screen"})

    def test_variant_view_evidence_reused_as_counter_thesis(self):
        core = Core()
        a, b = core.claim(1), core.claim(2)
        _dossier(core, "structural_growth", [], variant_refs=[a, b])
        core.deliverable("initial_screen", 1, {"sections": [
            {"title": "S4 风险与 Anti-thesis（简版）", "body": "x", "claim_refs": [a, b]}]})
        (conflict,) = self.kinds(core)["variant_counter"]
        self.assertEqual(conflict["evidence"]["shared_evidence_refs"], sorted([a, b]))

    def test_different_consensus_sources_across_products(self):
        core = Core()
        fs = core.claim(1, "Q3 EPS beat the FactSet consensus estimate", metric_or_aspect="EPS vs consensus")
        zk = core.claim(2, "Revenue was above the Zacks consensus", metric_or_aspect="revenue vs consensus")
        core.deliverable("earnings_preview", 1, {"summary": "x 2026-11-05 preview", "sections": [
            {"title": "p", "body": "b", "claim_refs": [fs]}]})
        core.deliverable("event_note", 1, {"sections": [{"title": "e", "body": "b", "claim_refs": [zk]}]})
        (conflict,) = self.kinds(core)["consensus_source"]
        self.assertEqual(sorted(sum(conflict["evidence"]["providers"].values(), [])), ["FactSet", "Zacks"])

    def test_confidence_field_against_its_own_prose(self):
        self.assertEqual(pc.confidence_in_prose("这一判断的置信度为中等偏低"), "medium_low")
        self.assertEqual(pc.confidence_in_prose("remains a low-confidence hypothesis"), "low")
        core = Core()
        _gate(core, "turnaround", [], answers=[{"question_ref": "q2", "confidence": "high",
                                                "sentences": [{"text": "数据很少，置信度低。"}]}])
        core.thesis(1, confidence="high", statement="A low-confidence hypothesis about widgets.")
        conflicts = self.kinds(core)["confidence_prose"]
        self.assertEqual(len(conflicts), 2)

    def test_zero_base_review_against_the_catalyst_calendar(self):
        core = Core()
        calendar = {"entries": [{"event_kind": "earnings", "confidence": "confirmed",
                                 "expected_date": "2026-09-25", "entry_ref": "catalyst-entry:q"}]}
        core.insert("catalyst_calendar_versions", version_id="catalyst-calendar-version:synw-1",
                    calendar_ref="catalyst-calendar:synw", version_number=1, company_ref=COMPANY, entry_count=1,
                    change_reason="x", next_catalyst_date="2026-09-25", record_json=json.dumps(calendar),
                    content_hash="c", actor_ref="automation:t", created_at="2026-09-01T00:00:00+00:00")
        record = {"as_of": "2026-09-14", "answers": {"next_verification": {
            "date": "2026-10-20", "what": "check", "because": "This is not a confirmed earnings date."}}}
        core.insert("zero_base_review_versions", version_id="zero-base-review-version:synw-1",
                    review_ref="zero-base-review:synw", version_number=1, mission_ref="m", mission_version_ref="m",
                    company_ref=COMPANY, trigger="monthly", period_label="2026-09", form_a_view="yes",
                    rewritten_line_count=0, stale_debate_count=0, candidate_count=0,
                    next_verification_date="2026-10-20", inputs_hash="i", record_json=json.dumps(record),
                    content_hash="z", actor_ref="automation:t", created_at="2026-09-14T10:00:00+00:00")
        found = {item["evidence"].get("entry_ref") and item["detail"][:6] for item in
                 self.kinds(core)["zero_base_catalyst"]}
        self.assertEqual(len(self.kinds(core)["zero_base_catalyst"]), 2, found)

    def test_model_operating_loss_against_positive_bridge_earnings(self):
        core = Core()
        cells = [{"period": {"kind": "quarter", "end": end}, "kind": "estimate", "status": "computed",
                  "value": value} for end, value in (("2026-09-30", "-10"), ("2026-12-31", "-20"))]
        core.model(1, results=[{"role": "operating_income", "cells": cells}])
        legs = [{"leg": "expected_consensus", "label": "一致预期", "status": "available",
                 "window": {"start": "2026-07-01", "end": "2026-12-31"}, "eps_end": {"value": "2.5"}}]
        core.insert("holding_period_return_bridges", version_id="holding-period-return-version:synw-1",
                    bridge_ref="holding-period-return:synw", version_number=1, company_ref=COMPANY, as_of=TODAY,
                    available_leg_count=1, body_hash="b", record_json=json.dumps({"inputs": {}, "legs": legs}),
                    content_hash="r", actor_ref="automation:t", created_at="2026-10-02")
        (conflict,) = self.kinds(core)["operating_income_vs_return"]
        self.assertEqual(conflict["severity"], "high")
        self.assertEqual(conflict["evidence"]["model_operating_income"], "-30")

    def test_operating_income_is_derived_when_the_model_has_no_such_line(self):
        def result(role, value):
            return {"role": role, "cells": [{"period": {"kind": "quarter", "end": "2026-09-30"},
                                             "kind": "estimate", "status": "computed", "value": value}]}

        model = {"results": [result("revenue", "100"), result("cost_of_revenue", "70"),
                             result("operating_expense", "40")]}
        self.assertEqual(pc.operating_income_by_period(model), {"2026-09-30": -10})

    def test_forecast_line_unit_and_currency_mismatches(self):
        core = Core()
        core.model(1, currency="USD")
        for n, (unit, currency, metric) in enumerate((("one", "USD", "metric:revenue-usd"),
                                                       ("million", "USD", "metric:revenue-usd"),
                                                       ("one", "EUR", "metric:cash-usd")), start=1):
            ref = f"forecast-line-version:synw:{n}"
            core.insert("model_forecast_line_versions", version_id=ref, line_ref=f"forecast-line:synw:{n}",
                        version_number=1, subject_ref=COMPANY,
                        record_json=json.dumps({"id": ref, "scenario_version_ref": "forecast-model-version:synw:1",
                                                "unit": unit, "currency": currency, "metric_or_aspect": metric}),
                        content_hash=ref, actor_ref="automation:t", created_at="2026-10-02")
        keys = {item["evidence"].get("metric", "") + ":" + item["severity"]
                for item in self.kinds(core)["unit_currency"]}
        self.assertIn("metric:cash-usd:high", keys)       # EUR on a -usd metric, and vs the model
        self.assertIn("metric:revenue-usd:medium", keys)  # one vs million on the same metric


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        make_core(self.store.connection)
        self.authority = pc.ProductConsistencyAuthority(self.store, clock=lambda: "2026-10-09T00:00:00+00:00")

    def test_conflicts_are_recorded_once_and_superseded_when_products_move_on(self):
        core = Core()
        core.c = self.store.connection
        old = "claim-version:" + "1" * 64
        _gate(core, "turnaround", [old])
        _dossier(core, "structural_growth", [old])
        first = self.authority.scan(actor_ref="automation:synw", today=TODAY)
        self.assertEqual(first["fresh"], 1)
        self.assertEqual(self.authority.scan(actor_ref="automation:synw", today=TODAY)["fresh"], 0)
        (open_one,) = pc.recorded_conflicts(self.store.connection)
        self.assertEqual(open_one["state"], "open")
        _dossier(core, "turnaround", [old], number=2)
        result = self.authority.scan(actor_ref="automation:synw", today=TODAY)
        self.assertEqual(result["superseded"], 1)
        self.assertEqual(pc.recorded_conflicts(self.store.connection), [])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM product_consistency_conflicts")

    def test_an_explanation_must_name_evidence(self):
        core = Core()
        core.c = self.store.connection
        _gate(core, "turnaround", [])
        _dossier(core, "structural_growth", [])
        self.authority.scan(actor_ref="automation:synw", today=TODAY)
        (conflict,) = pc.recorded_conflicts(self.store.connection)
        with self.assertRaises(pc.ProductConsistencyError):
            self.authority.resolve(conflict["id"], resolution="explained", note="ok", actor_ref="human:pm")
        self.authority.resolve(conflict["id"], resolution="explained", note="新财报", actor_ref="human:pm",
                               evidence_refs=["claim-version:" + "b" * 64])
        self.assertEqual(pc.recorded_conflicts(self.store.connection), [])


class WriterBindingTests(unittest.TestCase):
    """The binding fields the writers record (metadata only)."""

    def test_scenario_versions_name_the_model_their_baselines_rest_on(self):
        from dalton_core.company_scenario import model_binding

        self.assertEqual(model_binding({"model_version_ref": "forecast-model-version:synw:3",
                                        "model_version_hash": "h"}),
                         {"model_version_ref": "forecast-model-version:synw:3", "model_version_hash": "h"})
        self.assertEqual(model_binding({}), {"model_version_ref": None, "model_version_hash": None})
        record = {"triggers": [{"baseline": {"source_ref": "forecast-model-version:synw:2#cell"}}]}
        self.assertEqual(pc.scenario_model_ref(record, None), "forecast-model-version:synw:2")

    def test_the_preview_summary_carries_its_upstream_versions(self):
        from dalton_core.earnings_preview import document_summary

        context = {"company_ref": COMPANY, "occurrence": {"expected_date": "2026-11-05",
                                                          "date_confidence": "estimated"},
                   "period_end": "2026-09-30", "model_version_ref": "forecast-model-version:synw:2",
                   "upstream_bindings": pc.upstream_bindings(["forecast-model-version:synw:2",
                                                              "consensus-estimate-version:c1"]),
                   "forecast": [], "consensus": {"available": False}, "guidance": {"available": False}}
        summary = document_summary(context)
        self.assertIn("上游版本 [consensus-estimate-version:c1] [forecast-model-version:synw:2]", summary)
        self.assertEqual(set(pc.version_refs_in(summary)),
                         {"consensus-estimate-version:c1", "forecast-model-version:synw:2"})

    def test_the_return_bridge_binds_model_and_consensus(self):
        from dalton_core.return_attribution import return_bridge_bindings

        rows = return_bridge_bindings({"inputs": {"forecast_model_version_ref": "forecast-model-version:synw:1",
                                                  "consensus_version_ref": "consensus-estimate-version:c"},
                                       "legs": [{"inputs": {"forecast_model_version_ref":
                                                            "forecast-model-version:synw:1"}}]})
        self.assertEqual([row["ref"] for row in rows],
                         ["consensus-estimate-version:c", "forecast-model-version:synw:1"])

    def test_redraft_policy_reads_the_consistency_triggers(self):
        from datetime import datetime, timezone

        from dalton_core.redraft_policy import _consistency

        core = Core()
        refs = derived_on_model_v1(core)
        core.model(2)
        reasons = {item["code"]: item for item in _consistency(
            core.c, COMPANY, "earnings_preview", {"version_ref": refs["preview"]},
            datetime(2026, 10, 9, tzinfo=timezone.utc))}
        self.assertTrue(reasons["stale_binding"]["fired"])
        self.assertEqual(reasons["stale_binding"]["label"], "派生结果基于旧版本上游")
        self.assertFalse(reasons["consistency_conflict"]["fired"])


class CikTests(unittest.TestCase):
    def test_a_nine_digit_cik_ref_is_rejected(self):
        with self.assertRaises(NonCanonicalCompanyRef):
            validate_company_ref("company:sec-cik:009999999")
        self.assertEqual(validate_company_ref("company:sec-cik:0009999999"), "company:sec-cik:0009999999")
        self.assertEqual(validate_company_ref(COMPANY), COMPANY)

    def test_both_spellings_are_found_and_mapped(self):
        text = "overlay company-sec-cik-009999999:3 and company:sec-cik:009999999 beside company:sec-cik:0000000042"
        self.assertEqual(noncanonical_refs_in_text(text),
                         ["company-sec-cik-009999999", "company:sec-cik:009999999"])
        self.assertEqual(canonical_spelling("company-sec-cik-009999999"), "company-sec-cik-0009999999")

    def test_alias_table_resolves_history_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DaltonStore(str(Path(directory) / "core.sqlite"))
            try:
                make_core(store.connection)
                store.connection.execute(
                    "INSERT INTO sensitivity_projections(projection_id,projection_ref,version_number,company_ref,"
                    "model_version_ref,model_version_hash,inputs_hash,fingerprint,body_hash,record_json,"
                    "content_hash,actor_ref,created_at) VALUES('sensitivity-projection:x:1',"
                    "'sensitivity-projection:company:sec-cik:009999999',1,'company:sec-cik:009999999','m','h',"
                    "'i','f','b','{}','c','a','t')")
                report = pc.scan_noncanonical_refs(store.connection)
                self.assertIn("company:sec-cik:009999999", report["refs"])
                authority = pc.ProductConsistencyAuthority(store)
                result = authority.record_aliases_from_scan(actor_ref="automation:synw")
                self.assertEqual(result["aliases"][0]["write_status"], "fresh")
                aliases = pc.company_ref_aliases(store.connection)
                self.assertEqual(resolve_alias("company:sec-cik:009999999", aliases),
                                 "company:sec-cik:0009999999")
                with self.assertRaises(NonCanonicalCompanyRef):
                    authority.record_alias("company:sec-cik:9999999", "company:sec-cik:009999999",
                                           reason="x", actor_ref="automation:synw")
                with self.assertRaises(pc.ProductConsistencyError):
                    authority.record_alias("company:sec-cik:0000000042", "company:sec-cik:0009999999",
                                           reason="x", actor_ref="automation:synw")
                # history is untouched
                self.assertEqual(store.connection.execute(
                    "SELECT company_ref FROM sensitivity_projections").fetchone()[0], "company:sec-cik:009999999")
                with self.assertRaises(sqlite3.DatabaseError):
                    store.connection.execute("UPDATE company_ref_aliases SET canonical_ref='x'")
            finally:
                store.close()


class CitationLifecycleTests(unittest.TestCase):
    def test_a_framework_with_every_citation_retired_needs_a_rewrite(self):
        a, b, c = ("claim-version:" + ch * 64 for ch in "abc")
        record = {"long_term_drivers": [{"title": "订单领先收入", "refs": [a, b]}],
                  "industry_characteristics": [{"title": "预算周期", "refs": [c]}],
                  "evidence_refs": [a, b, c]}
        retired = {ref: {"reason_code": "citation_support_rejected", "decision_ref": "d"} for ref in (a, b, c)}
        sections = affected_sections("industry_framework", record, retired)
        self.assertEqual({item["severity"] for item in sections}, {"high"})
        self.assertNotIn("evidence_refs", {item["section"] for item in sections})
        shown = citation_display("industry_framework", record, retired)
        self.assertEqual({item["display_label"] for item in shown}, {HISTORICAL_CITATION_LABEL})
        self.assertEqual(shown[0]["retirement_reasons"], ["citation_support_rejected"])

    def test_every_retirement_reason_counts_below_the_share(self):
        a, b, c = ("claim-version:" + ch * 64 for ch in "def")
        record = {"sections": [{"title": "S2", "claim_refs": [a, b, c]}]}
        retired = {a: {"reason_code": "subject_absent_from_source", "decision_ref": "d"},
                   b: {"reason_code": "human_judgment", "decision_ref": "d"}}
        (section,) = affected_sections("mission_deliverable", record, retired)
        self.assertEqual(section["severity"], "threshold")
        self.assertEqual(section["retirement_reasons"], ["human_judgment", "subject_absent_from_source"])


class BlankWorkspaceCanaries(unittest.TestCase):
    """A workspace with no tables at all reads as empty, never as an error."""

    def test_every_reader_on_an_empty_core(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.assertEqual(pc.evaluate_bindings(connection, today=TODAY), [])
        self.assertEqual(pc.detect_conflicts(connection, today=TODAY), [])
        self.assertEqual(pc.stale_bindings(connection, today=TODAY)["stale"], 0)
        self.assertEqual(ei.coverage_report(connection), {})
        self.assertIsNone(ei.verdict_for_output(connection, "forecast-model-version:x:1"))
        self.assertEqual(pc.scan_noncanonical_refs(connection)["count"], 0)
        self.assertEqual(pc.recorded_conflicts(connection), [])
        view = pc.consistency_view(connection, COMPANY, today=TODAY)
        self.assertEqual(view["conflict_count"], 0)
        self.assertIn("没有发现不一致", view["summary"])
        self.assertFalse(any(item["fired"] for item in pc.redraft_signals(connection, COMPANY, "dossier",
                                                                           today=TODAY)))

    def test_a_fresh_store_scan_records_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DaltonStore(str(Path(directory) / "core.sqlite"))
            try:
                authority = pc.ProductConsistencyAuthority(store)
                self.assertEqual(authority.scan(actor_ref="automation:blank", today=TODAY),
                                 {"detected": 0, "fresh": 0, "superseded": 0})
                self.assertEqual(authority.record_aliases_from_scan(actor_ref="automation:blank"),
                                 {"aliases": []})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
