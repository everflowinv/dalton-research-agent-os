import copy
import sqlite3
import unittest

from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.industry_research import (
    IndustryResearchAuthority,
    IndustryResearchConflict,
    IndustryResearchValidationError,
)
from dalton_core.model_input import ModelInputLedger
from dalton_core.store import DaltonStore


INDUSTRY = "industry:us-it-services"
ACN = "company:sec-cik:0001467373"
NOW = "2026-08-23T12:00:00+00:00"


def invocation(identifier: str) -> dict:
    return {
        "schema_version": "0.1", "id": identifier, "created_at": NOW,
        "work_order_ref": "work:industry-pack", "profile_ref": "profile:test",
        "granularity": "task", "capability": "research", "provider": "provider:test",
        "model": "model:test", "model_family": "family:test", "runtime_ref": "runtime:test",
        "actor_ref": "automation:researcher", "usage": {"tokens": 1},
        "input_refs": [], "output_refs": [], "started_at": NOW, "completed_at": NOW,
        "side_effects": [], "parent_ref": None,
    }


class IndustryResearchAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.model = ModelInputLedger(self.store)
        self.coverage = CoverageAdmissionAuthority(self.store)
        self.authority = IndustryResearchAuthority(self.store)
        self.addCleanup(self.store.close)
        self.driver_pack = self.coverage.register_driver_pack(
            "driver-pack:us-it-services",
            industry_ref=INDUSTRY,
            title="US IT Services Driver Pack",
            drivers=[{
                "driver_ref": "driver:demand-and-conversion",
                "label": "Demand and conversion",
                "mechanism": "Demand becomes revenue only when bookings convert.",
                "metric_refs": ["metric:new-bookings", "metric:ai-demand-signal"],
            }],
            metric_specs=[{
                "metric_ref": "metric:new-bookings", "label": "New bookings",
                "definition": "Contract value recorded as new bookings.", "unit": "USD",
                "periodicity": "quarterly", "preferred_source_refs": ["source:earnings-release"],
                "verification_kind": "numeric_and_semantic", "caveats": [],
            }, {
                "metric_ref": "metric:ai-demand-signal", "label": "AI demand signal",
                "definition": "Issuer commentary about large-scale AI programs.", "unit": "qualitative",
                "periodicity": "quarterly", "preferred_source_refs": ["source:earnings-release"],
                "verification_kind": "semantic", "caveats": ["Not a standardized accounting metric."],
            }],
            thesis_templates=[{
                "template_ref": "template:demand-conversion", "statement": "AI demand can support growth.",
                "mechanism": "Bookings convert into revenue.",
                "driver_refs": ["driver:demand-and-conversion"],
                "implied_expectation": "Demand signals are followed by bookings and revenue.",
                "falsifier_refs": ["falsifier:conversion-breaks"],
            }],
            actor_ref="human:coverage-owner",
            version_id="driver-pack-version:us-it-services:1",
            prior_version_ref=None, idempotency_key="driver-pack:us-it-services:1",
        )
        self.evidence = self.store.register_evidence({
            "evidence_ref": "evidence:acn:q3fy26-release",
            "source_type": "sec-filing-exhibit",
            "source_ref": "sec:0001467373-26-000031:q3fy26earnings8-kexhibit.htm",
            "retrieved_at": NOW,
            "source_lineage": ["sec:0001467373-26-000031"],
            "independence_group": "issuer:acn:q3fy26",
            "actor_ref": "system:sec-adapter",
        })
        self.store.register_invocation(invocation("invocation:industry-research"))
        self.bookings = self.store.register_claim({
            "claim_ref": "claim:acn:q3fy26:new-bookings", "subject_ref": ACN,
            "metric_or_aspect": "metric:new-bookings", "period": "FY2026Q3",
            "basis": "issuer-reported", "normalized_statement": "Q3 FY2026 new bookings were USD 19.32 billion.",
            "claim_kind": "quantitative", "value": 19.32, "unit": "USD_billion",
            "producer_invocation_refs": ["invocation:industry-research"],
            "actor_ref": "automation:researcher",
        })
        self.demand = self.store.register_claim({
            "claim_ref": "claim:acn:q3fy26:ai-demand", "subject_ref": ACN,
            "metric_or_aspect": "metric:ai-demand-signal", "period": "FY2026Q3",
            "basis": "issuer-commentary", "normalized_statement": "Management reported more large-scale AI transformation programs.",
            "claim_kind": "qualitative", "value": None, "unit": None,
            "producer_invocation_refs": ["invocation:industry-research"],
            "actor_ref": "automation:researcher",
        })
        self.relations = {}
        for label, claim in (("bookings", self.bookings), ("demand", self.demand)):
            self.relations[label] = self.store.relate_evidence({
                "id": f"relation:acn:q3fy26:{label}",
                "evidence_version_ref": self.evidence["evidence_version_id"],
                "claim_version_ref": claim["claim_version_id"], "relation": "supports",
            })
        candidate = self.model.propose_input(
            candidate_id="candidate:acn:q3fy26:new-bookings", input_kind="actual",
            model_input_ref="input:acn:q3fy26:new-bookings", prior_version_ref=None,
            payload={
                "schema_version": "0.1", "metric_ref": "metric:new-bookings",
                "subject_ref": ACN, "business_line_ref": None,
                "period": {"start": "2026-03-01", "end": "2026-05-31", "calendar": "company:fiscal", "kind": "quarter"},
                "unit": "billion", "currency": "USD", "value": "19.32",
                "source_authorities": [{
                    "authority_kind": "evidence_version",
                    "version_ref": self.evidence["evidence_version_id"],
                    "content_hash": self.evidence["content_hash"],
                }],
            }, proposed_by="agent:researcher", idempotency_key="candidate:bookings",
        )["candidate"]
        self.model_input = self.model.decide_input(
            decision_id="decision:acn:q3fy26:new-bookings", candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"], verdict="admit",
            rationale="Matched the exact issuer filing exhibit.", findings=[],
            reviewer_ref="human:analyst", version_id="input-version:acn:q3fy26:new-bookings:1",
            idempotency_key="decision:bookings",
        )["version"]

    def pack_params(self) -> dict:
        bindings = []
        for label, driver_metric, claim in (
            ("bookings", "metric:new-bookings", self.bookings),
            ("demand", "metric:ai-demand-signal", self.demand),
        ):
            relation = self.relations[label]
            bindings.append({
                "binding_ref": f"binding:acn:{label}",
                "driver_ref": "driver:demand-and-conversion", "metric_ref": driver_metric,
                "claim_version_ref": claim["claim_version_id"],
                "claim_version_hash": claim["content_hash"],
                "relation_refs": [{"ref": relation["relation_id"], "hash": relation["content_hash"]}],
            })
        return {
            "industry_ref": INDUSTRY, "title": "US IT Services Evidence Pack v1", "as_of": NOW,
            "boundary": {
                "definition": "Publicly traded providers of consulting, digital engineering, and managed IT services.",
                "inclusion_rules": ["Material IT services revenue."],
                "exclusion_rules": ["Pure-play software and hardware vendors."],
            },
            "coverage_universe": [
                {"company_ref": ACN, "ticker": "ACN", "role": "scaled global pure-play", "comparability_tier": "core"},
                {"company_ref": "company:sec-cik:0001058290", "ticker": "CTSH", "role": "offshore-heavy peer", "comparability_tier": "core"},
                {"company_ref": "company:sec-cik:0001352010", "ticker": "EPAM", "role": "digital-engineering peer", "comparability_tier": "adjacent"},
            ],
            "driver_pack_version_ref": self.driver_pack["id"],
            "driver_pack_version_hash": self.driver_pack["content_hash"],
            "evidence_bindings": bindings,
            "debates": [{
                "debate_ref": "debate:ai-demand-versus-conversion",
                "question": "Will AI demand convert into durable bookings and revenue?", "status": "open",
                "positions": [
                    {"label": "demand is visible", "stance": "supports", "claim_version_refs": [self.demand["claim_version_id"]]},
                    {"label": "conversion still needs proof", "stance": "qualifies", "claim_version_refs": [self.bookings["claim_version_id"]]},
                ],
            }],
            "source_plan": [
                {"source_ref": "source:sec-edgar", "purpose": "Financial statements and filing exhibits", "priority": 1, "required": True},
                {"source_ref": "source:alphaengine", "purpose": "Sell-side and transcript discovery", "priority": 2, "required": False},
            ],
            "report_contract": {
                "industry_brief_sections": ["boundary", "drivers", "debates", "falsifiers"],
                "company_difference_fields": ["role", "driver stance", "differentiators", "watchpoints"],
            },
            "actor_ref": "human:coverage-owner", "version_id": "industry-evidence-pack-version:us-it-services:1",
            "prior_version_ref": None, "idempotency_key": "industry-evidence-pack:us-it-services:1",
        }

    def register_pack(self) -> dict:
        return self.authority.register_evidence_pack(
            "industry-evidence-pack:us-it-services", **self.pack_params()
        )

    def overlay_params(self, pack: dict) -> dict:
        return {
            "company_ref": ACN, "industry_ref": INDUSTRY, "title": "Accenture company overlay v1",
            "as_of": NOW, "role": "scaled global pure-play",
            "evidence_pack_version_ref": pack["id"], "evidence_pack_version_hash": pack["content_hash"],
            "driver_views": [{
                "driver_ref": "driver:demand-and-conversion", "stance": "mixed",
                "claim_version_refs": [
                    {"ref": self.bookings["claim_version_id"], "hash": self.bookings["content_hash"]},
                    {"ref": self.demand["claim_version_id"], "hash": self.demand["content_hash"]},
                ],
                "model_input_version_refs": [{"ref": self.model_input["id"], "hash": self.model_input["content_hash"]}],
                "metric_coverage": [{
                    "metric_ref": "metric:new-bookings", "status": "observed",
                    "claim_version_refs": [self.bookings["claim_version_id"]],
                    "rationale": "The reviewed filing exhibit reports total new bookings.",
                }, {
                    "metric_ref": "metric:ai-demand-signal", "status": "observed",
                    "claim_version_refs": [self.demand["claim_version_id"]],
                    "rationale": "The reviewed filing exhibit contains management AI-demand commentary.",
                }],
                "differentiators": ["Scaled global delivery and managed-services mix."],
                "watchpoints": ["Bookings conversion and consulting growth."],
            }],
            "key_differences": ["Broader global scale than digital-engineering peers."],
            "open_questions": ["How quickly do large AI programs convert to recurring managed services?"],
            "falsifier_refs": ["falsifier:conversion-breaks"],
            "thesis_candidate_refs": ["thesis-admission-candidate:acn:1"],
            "actor_ref": "human:coverage-owner", "version_id": "company-overlay-version:acn:1",
            "prior_version_ref": None, "idempotency_key": "company-overlay:acn:1",
        }

    def test_exact_ledger_and_model_bindings_publish_idempotently(self) -> None:
        pack = self.register_pack()
        replay = self.register_pack()
        self.assertEqual("duplicate", replay["status"])
        overlay = self.authority.register_company_overlay(
            "company-overlay:acn", **self.overlay_params(pack)
        )
        self.assertEqual(pack["id"], overlay["evidence_pack_version_ref"])
        self.assertEqual(
            {key: value for key, value in pack.items() if key != "status"},
            self.authority.evidence_pack(pack["id"]),
        )
        self.assertEqual(
            {key: value for key, value in overlay.items() if key != "status"},
            self.authority.company_overlay(overlay["id"]),
        )
        with self.assertRaises(IndustryResearchConflict):
            self.authority.industry_brief_snapshot(pack["id"], [overlay["id"]])
        report = self.authority.integrity_report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(1, report["evidence_pack_versions"])
        self.assertEqual(1, report["company_overlay_versions"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "UPDATE industry_evidence_pack_versions SET actor_ref='tampered'"
            )

    def test_unbound_claim_relation_and_driver_fail_closed(self) -> None:
        wrong_hash = self.pack_params()
        wrong_hash["evidence_bindings"][0]["relation_refs"][0]["hash"] = "0" * 64
        with self.assertRaises(IndustryResearchConflict):
            self.authority.register_evidence_pack(
                "industry-evidence-pack:bad-relation", **wrong_hash
            )
        missing_driver = self.pack_params()
        missing_driver["evidence_bindings"] = []
        with self.assertRaises(IndustryResearchValidationError):
            self.authority.register_evidence_pack(
                "industry-evidence-pack:missing-driver", **missing_driver
            )

    def test_overlay_cannot_cross_subject_driver_or_model_authority(self) -> None:
        pack = self.register_pack()
        wrong_role = self.overlay_params(pack)
        wrong_role["role"] = "different role"
        with self.assertRaises(IndustryResearchConflict):
            self.authority.register_company_overlay("company-overlay:wrong-role", **wrong_role)
        wrong_hash = self.overlay_params(pack)
        wrong_hash["driver_views"][0]["model_input_version_refs"][0]["hash"] = "0" * 64
        with self.assertRaises(IndustryResearchConflict):
            self.authority.register_company_overlay("company-overlay:wrong-model", **wrong_hash)
        missing_metric = self.overlay_params(pack)
        missing_metric["driver_views"][0]["metric_coverage"].pop()
        with self.assertRaises(IndustryResearchConflict):
            self.authority.register_company_overlay("company-overlay:missing-metric", **missing_metric)
        false_gap = self.overlay_params(pack)
        false_gap["driver_views"][0]["metric_coverage"][0]["status"] = "not_comparable"
        with self.assertRaises(IndustryResearchValidationError):
            self.authority.register_company_overlay("company-overlay:false-gap", **false_gap)
        other_evidence = self.store.register_evidence({
            "evidence_ref": "evidence:acn:unbound-source", "source_type": "filing",
            "source_ref": "sec:acn:unbound", "retrieved_at": NOW,
            "source_lineage": ["sec:acn:unbound"], "independence_group": "sec:acn:unbound",
            "actor_ref": "system:sec-adapter",
        })
        candidate = self.model.propose_input(
            candidate_id="candidate:unbound-source", input_kind="actual",
            model_input_ref="input:acn:unbound-bookings", prior_version_ref=None,
            payload={
                "schema_version": "0.1", "metric_ref": "metric:new-bookings",
                "subject_ref": ACN, "business_line_ref": None,
                "period": {"start": "2026-03-01", "end": "2026-05-31", "calendar": "company:fiscal", "kind": "quarter"},
                "unit": "billion", "currency": "USD", "value": "19.32",
                "source_authorities": [{
                    "authority_kind": "evidence_version",
                    "version_ref": other_evidence["evidence_version_id"],
                    "content_hash": other_evidence["content_hash"],
                }],
            }, proposed_by="agent:researcher", idempotency_key="candidate:unbound-source",
        )["candidate"]
        unbound_input = self.model.decide_input(
            decision_id="decision:unbound-source", candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"], verdict="admit",
            rationale="Separate valid source.", findings=[], reviewer_ref="human:analyst",
            version_id="input-version:unbound-source:1", idempotency_key="decision:unbound-source",
        )["version"]
        unbound = self.overlay_params(pack)
        unbound["driver_views"][0]["model_input_version_refs"] = [{
            "ref": unbound_input["id"], "hash": unbound_input["content_hash"]
        }]
        with self.assertRaises(IndustryResearchConflict):
            self.authority.register_company_overlay("company-overlay:unbound-source", **unbound)

    def test_version_chain_must_continue_latest(self) -> None:
        first = self.register_pack()
        params = self.pack_params()
        params["version_id"] = "industry-evidence-pack-version:us-it-services:2"
        params["idempotency_key"] = "industry-evidence-pack:us-it-services:2"
        params["prior_version_ref"] = "missing"
        with self.assertRaises(IndustryResearchConflict):
            self.authority.register_evidence_pack("industry-evidence-pack:us-it-services", **params)
        params["prior_version_ref"] = first["id"]
        second = self.authority.register_evidence_pack("industry-evidence-pack:us-it-services", **params)
        self.assertEqual(2, second["version"])


if __name__ == "__main__":
    unittest.main()
