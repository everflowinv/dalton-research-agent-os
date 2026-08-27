"""Research constitution authority: closed contract, exact bindings, immutability."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.research_constitution import (
    ResearchConstitutionAuthority,
    ResearchConstitutionConflict,
    ResearchConstitutionNotFound,
    ResearchConstitutionValidationError,
    validate_constitution_method,
)
from dalton_core.research_doctrine import ResearchDoctrineAuthority
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.weekly_brief_coordinator import WeeklyBriefSchedulePlan


ROOT = Path(__file__).resolve().parents[1]


INDUSTRY = "industry:us-it-services"
COMPANY = "company:sec-cik:0001467373"
OWNER = "human:coverage-owner"


def method_payload(**overrides) -> dict:
    method = {
        "question_admission": ["A question must change a Thesis or driver view."],
        "causal_chain": ["Bookings lead revenue by two to four quarters."],
        "source_standards": {
            "hierarchy": ["SEC filings are the primary numeric authority."],
            "conflict_adjudication": ["GAAP filing numbers override transcripts."],
            "minimum_independent_sources": 1,
        },
        "falsification": {
            "required_falsifier_searches": ["Search for bookings contraction."],
            "alternative_explanations": ["FX translation masquerading as inflection."],
        },
        "materiality": ["State magnitude in revenue-growth points."],
        "lifecycle": {
            "continue_when": ["The brief changes a Thesis or driver view."],
            "refresh_when": ["Evidence exceeds its freshness window."],
            "stop_when": ["All falsifiers resolved."],
            "escalate_when": ["A major Thesis change is proposed."],
        },
        "output_rubric": {
            "criteria": ["State what changed and its impact."],
            "good_samples": ["weekly-brief-version:us-it-services:2026-w35"],
            "bad_samples": ["weekly-brief-feedback:us-it-services:2026-w35:revise"],
        },
    }
    method.update(overrides)
    return method


class ResearchConstitutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.agenda = AgendaStore(self.store)
        self.coverage = CoverageAdmissionAuthority(self.store)
        self.doctrine = ResearchDoctrineAuthority(self.store)
        self.authority = ResearchConstitutionAuthority(self.store)
        self.mandate = self.agenda.create_mandate(
            "mandate:us-it-services", actor_ref=OWNER,
            objective="Establish US IT Services coverage.",
            scope_refs=[INDUSTRY, COMPANY], constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
        )
        self.pack = self.coverage.register_driver_pack(
            "driver-pack:us-it-services", actor_ref=OWNER, industry_ref=INDUSTRY,
            title="US IT Services Driver Pack",
            drivers=[{
                "driver_ref": "driver:demand-and-conversion", "label": "Demand",
                "mechanism": "Demand becomes revenue when bookings convert.",
                "metric_refs": ["metric:new-bookings"],
            }],
            metric_specs=[{
                "metric_ref": "metric:new-bookings", "label": "New bookings",
                "definition": "Contract value recorded as new bookings.", "unit": "USD",
                "periodicity": "quarterly", "preferred_source_refs": ["source:earnings-release"],
                "verification_kind": "numeric", "caveats": [],
            }],
            thesis_templates=[{
                "template_ref": "template:demand-conversion", "statement": "s",
                "mechanism": "m", "driver_refs": ["driver:demand-and-conversion"],
                "implied_expectation": "e", "falsifier_refs": ["falsifier:conversion-breaks"],
            }],
            version_id="driver-pack-version:us-it-services:1", prior_version_ref=None,
            idempotency_key="driver-pack:us-it-services:1",
        )
        self.policy = self.store.active_policy_version()
        self.doctrine_pack = self.doctrine.publish_pack(
            "doctrine-pack:us-it-services", title="US IT Services Doctrine",
            default_lens_ref="lens:demand-inflection",
            lenses=[{
                "lens_ref": "lens:demand-inflection", "label": "Demand inflection",
                "objective": "Track the demand inflection.",
                "priority_topics": ["bookings"],
                "evidence_standard": {
                    "preferred_source_classes": ["source:sec-edgar"],
                    "minimum_independent_sources": 1,
                    "negative_claim_rule": "candidate_only_until_separate_claim_admission",
                },
            }],
            actor_ref=OWNER,
        )

    def bindings(self, **overrides) -> dict:
        bindings = {
            "mandate_version": {"ref": self.mandate["id"], "hash": self.mandate["content_hash"]},
            "driver_pack_version": {"ref": self.pack["id"], "hash": self.pack["content_hash"]},
            "governance_policy_version": {"ref": self.policy.id, "hash": self.policy.content_hash},
            "doctrine_pack_version": {
                "ref": self.doctrine_pack["id"], "hash": self.doctrine_pack["content_hash"],
            },
            "weekly_brief_plan": {
                "ref": "weekly-brief-plan:us-it-services:v2",
                "hash": "da9406d4ca106081047bcb24b1482f2b6c85cae28b748ac24871073a45a4e6dc",
            },
        }
        bindings.update(overrides)
        return bindings

    def publish(self, **overrides) -> dict:
        params = {
            "constitution_ref": "constitution:us-it-services",
            "industry_ref": INDUSTRY,
            "title": "US IT Services Research Constitution",
            "bindings": self.bindings(),
            "method": method_payload(),
            "actor_ref": OWNER,
            "version_id": "constitution-version:us-it-services:1",
            "prior_version_ref": None,
            "idempotency_key": "constitution:us-it-services:1",
        }
        params.update(overrides)
        version_id = params.pop("version_id")
        return self.authority.publish_constitution(
            params.pop("constitution_ref"), version_id=version_id, **params
        )

    def test_publish_binds_active_authorities_replays_and_reports(self) -> None:
        published = self.publish()
        self.assertEqual("fresh", published["status"])
        self.assertEqual(1, published["version"])
        replay = self.publish()
        self.assertEqual("duplicate", replay["status"])
        self.assertEqual(published["content_hash"], replay["content_hash"])
        active = self.authority.active_constitution("constitution:us-it-services")
        self.assertEqual(published["id"], active["id"])
        report = self.authority.constitution_report()
        self.assertEqual(1, report["constitution_count"])
        self.assertEqual(1, report["version_count"])
        self.assertEqual(published["content_hash"], report["constitutions"][0]["content_hash"])
        reread = self.authority.constitution(published["id"])
        self.assertEqual(
            self.doctrine_pack["content_hash"],
            reread["bindings"]["doctrine_pack_version"]["hash"],
        )

    def test_non_human_actor_and_closed_shapes_are_rejected(self) -> None:
        with self.assertRaises(ResearchConstitutionValidationError):
            self.publish(actor_ref="system:planner")
        with self.assertRaises(ResearchConstitutionValidationError):
            self.publish(method=method_payload(question_admission=[]))
        with self.assertRaises(ResearchConstitutionValidationError):
            self.publish(bindings=self.bindings(weekly_brief_plan={"ref": "p", "hash": "zz"}))
        with self.assertRaises(ResearchConstitutionValidationError):
            self.publish(bindings={"mandate_version": self.bindings()["mandate_version"]})
        with self.assertRaises(ResearchConstitutionValidationError):
            validate_constitution_method({"question_admission": ["q"]})

    def test_mandate_binding_must_be_exact_active_and_in_scope(self) -> None:
        with self.assertRaises(ResearchConstitutionNotFound):
            self.publish(bindings=self.bindings(mandate_version={
                "ref": "mandate-version:missing", "hash": self.mandate["content_hash"],
            }))
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(mandate_version={
                "ref": self.mandate["id"], "hash": "0" * 64,
            }))
        other = self.agenda.create_mandate(
            "mandate:other-industry", actor_ref=OWNER, objective="Different scope.",
            scope_refs=["industry:semiconductors"], constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
        )
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(mandate_version={
                "ref": other["id"], "hash": other["content_hash"],
            }))
        self.agenda.create_mandate(
            "mandate:us-it-services", actor_ref=OWNER,
            objective="Second version deactivates the first for this test.",
            scope_refs=[INDUSTRY], constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
        )
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(mandate_version={
                "ref": self.mandate["id"], "hash": self.mandate["content_hash"],
            }))

    def test_driver_pack_and_policy_bindings_fail_closed(self) -> None:
        with self.assertRaises(ResearchConstitutionNotFound):
            self.publish(bindings=self.bindings(driver_pack_version={
                "ref": "driver-pack-version:missing", "hash": self.pack["content_hash"],
            }))
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(driver_pack_version={
                "ref": self.pack["id"], "hash": "1" * 64,
            }))
        other_pack = self.coverage.register_driver_pack(
            "driver-pack:other", actor_ref=OWNER, industry_ref="industry:semiconductors",
            title="Other", drivers=[{
                "driver_ref": "driver:d", "label": "D", "mechanism": "m",
                "metric_refs": ["metric:x"],
            }],
            metric_specs=[{
                "metric_ref": "metric:x", "label": "X", "definition": "d", "unit": "USD",
                "periodicity": "quarterly", "preferred_source_refs": ["source:sec-edgar"],
                "verification_kind": "numeric", "caveats": [],
            }],
            thesis_templates=[{
                "template_ref": "template:x", "statement": "s", "mechanism": "m",
                "driver_refs": ["driver:d"], "implied_expectation": "e",
                "falsifier_refs": ["falsifier:x"],
            }],
            version_id="driver-pack-version:other:1", prior_version_ref=None,
            idempotency_key="driver-pack:other:1",
        )
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(driver_pack_version={
                "ref": other_pack["id"], "hash": other_pack["content_hash"],
            }))
        with self.assertRaises(ResearchConstitutionNotFound):
            self.publish(bindings=self.bindings(governance_policy_version={
                "ref": "policy-missing", "hash": self.policy.content_hash,
            }))
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(governance_policy_version={
                "ref": self.policy.id, "hash": "2" * 64,
            }))

    def test_doctrine_binding_optional_but_must_be_latest(self) -> None:
        published = self.publish(bindings=self.bindings(doctrine_pack_version=None))
        self.assertIsNone(published["bindings"]["doctrine_pack_version"])
        latest = self.doctrine.publish_pack(
            "doctrine-pack:us-it-services", title="US IT Services Doctrine v2",
            default_lens_ref="lens:demand-inflection",
            lenses=[{
                "lens_ref": "lens:demand-inflection", "label": "Demand inflection",
                "objective": "Track the demand inflection.",
                "priority_topics": ["bookings", "conversion"],
                "evidence_standard": {
                    "preferred_source_classes": ["source:sec-edgar"],
                    "minimum_independent_sources": 1,
                    "negative_claim_rule": "candidate_only_until_separate_claim_admission",
                },
            }],
            actor_ref=OWNER, prior_version_ref=self.doctrine_pack["id"],
        )
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(bindings=self.bindings(doctrine_pack_version={
                "ref": self.doctrine_pack["id"], "hash": self.doctrine_pack["content_hash"],
            }))
        superseded = self.publish(
            bindings=self.bindings(doctrine_pack_version={
                "ref": latest["id"], "hash": latest["content_hash"],
            }),
            version_id="constitution-version:us-it-services:2",
            prior_version_ref=published["id"],
            idempotency_key="constitution:us-it-services:2",
        )
        self.assertEqual(2, superseded["version"])

    def test_doctrine_binding_requires_open_doctrine_authority(self) -> None:
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        agenda = AgendaStore(store)
        coverage = CoverageAdmissionAuthority(store)
        authority = ResearchConstitutionAuthority(store)
        mandate = agenda.create_mandate(
            "mandate:us-it-services", actor_ref=OWNER, objective="o",
            scope_refs=[INDUSTRY], constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
        )
        pack = coverage.register_driver_pack(
            "driver-pack:us-it-services", actor_ref=OWNER, industry_ref=INDUSTRY,
            title="p", drivers=[{
                "driver_ref": "driver:d", "label": "D", "mechanism": "m",
                "metric_refs": ["metric:x"],
            }],
            metric_specs=[{
                "metric_ref": "metric:x", "label": "X", "definition": "d", "unit": "USD",
                "periodicity": "quarterly", "preferred_source_refs": ["source:sec-edgar"],
                "verification_kind": "numeric", "caveats": [],
            }],
            thesis_templates=[{
                "template_ref": "template:x", "statement": "s", "mechanism": "m",
                "driver_refs": ["driver:d"], "implied_expectation": "e",
                "falsifier_refs": ["falsifier:x"],
            }],
            version_id="driver-pack-version:us-it-services:1", prior_version_ref=None,
            idempotency_key="driver-pack:us-it-services:1",
        )
        policy = store.active_policy_version()
        with self.assertRaises(ResearchConstitutionNotFound):
            authority.publish_constitution(
                "constitution:us-it-services", industry_ref=INDUSTRY,
                title="t", bindings={
                    "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
                    "driver_pack_version": {"ref": pack["id"], "hash": pack["content_hash"]},
                    "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
                    "doctrine_pack_version": {
                        "ref": "doctrine-pack-version:missing", "hash": "3" * 64,
                    },
                    "weekly_brief_plan": None,
                },
                method=method_payload(), actor_ref=OWNER,
                version_id="constitution-version:us-it-services:1",
                prior_version_ref=None, idempotency_key="c1",
            )

    def test_version_chain_and_duplicate_ids_fail_closed(self) -> None:
        first = self.publish()
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(prior_version_ref=None, idempotency_key="constitution:us-it-services:2")
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(
                prior_version_ref="constitution-version:us-it-services:stale",
                version_id="constitution-version:us-it-services:2",
                idempotency_key="constitution:us-it-services:2",
            )
        with self.assertRaises(ResearchConstitutionConflict):
            self.publish(
                prior_version_ref=first["id"],
                version_id="constitution-version:us-it-services:1",
                idempotency_key="constitution:us-it-services:2",
            )
        second = self.publish(
            prior_version_ref=first["id"],
            version_id="constitution-version:us-it-services:2",
            idempotency_key="constitution:us-it-services:2",
        )
        self.assertEqual(2, second["version"])
        self.assertEqual(
            second["id"],
            self.authority.active_constitution("constitution:us-it-services")["id"],
        )

    def test_versions_are_immutable_and_reads_detect_drift(self) -> None:
        published = self.publish()
        for statement in (
            "UPDATE research_constitution_versions SET content_hash='" + "4" * 64 + "'",
            "DELETE FROM research_constitution_versions",
            "DELETE FROM research_constitution_pointer",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(statement)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "constitution.sqlite"
            store = DaltonStore(str(path))
            agenda = AgendaStore(store)
            coverage = CoverageAdmissionAuthority(store)
            ResearchDoctrineAuthority(store)
            authority = ResearchConstitutionAuthority(store)
            mandate = agenda.create_mandate(
                "mandate:us-it-services", actor_ref=OWNER, objective="o",
                scope_refs=[INDUSTRY], constraints={}, success_criteria={},
                effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
            )
            pack = coverage.register_driver_pack(
                "driver-pack:us-it-services", actor_ref=OWNER, industry_ref=INDUSTRY,
                title="p", drivers=[{
                    "driver_ref": "driver:d", "label": "D", "mechanism": "m",
                    "metric_refs": ["metric:x"],
                }],
                metric_specs=[{
                    "metric_ref": "metric:x", "label": "X", "definition": "d", "unit": "USD",
                    "periodicity": "quarterly", "preferred_source_refs": ["source:sec-edgar"],
                    "verification_kind": "numeric", "caveats": [],
                }],
                thesis_templates=[{
                    "template_ref": "template:x", "statement": "s", "mechanism": "m",
                    "driver_refs": ["driver:d"], "implied_expectation": "e",
                    "falsifier_refs": ["falsifier:x"],
                }],
                version_id="driver-pack-version:us-it-services:1", prior_version_ref=None,
                idempotency_key="driver-pack:us-it-services:1",
            )
            policy = store.active_policy_version()
            wire = authority.publish_constitution(
                "constitution:us-it-services", industry_ref=INDUSTRY, title="t",
                bindings={
                    "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
                    "driver_pack_version": {"ref": pack["id"], "hash": pack["content_hash"]},
                    "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
                    "doctrine_pack_version": None, "weekly_brief_plan": None,
                },
                method=method_payload(), actor_ref=OWNER,
                version_id="constitution-version:us-it-services:1",
                prior_version_ref=None, idempotency_key="c1",
            )
            store.close()
            raw = sqlite3.connect(path)
            raw.execute("DROP TRIGGER research_constitution_versions_no_update")
            base = {
                key: value for key, value in wire.items()
                if key not in ("status", "content_hash")
            }
            base["title"] = "tampered"
            tampered = dict(base)
            tampered["content_hash"] = content_hash(base)
            raw.execute(
                "UPDATE research_constitution_versions SET record_json=? WHERE constitution_version_id=?",
                (canonical_json(tampered), wire["id"]),
            )
            raw.commit()
            raw.close()
            reopened = DaltonStore(str(path))
            self.addCleanup(reopened.close)
            drifted = ResearchConstitutionAuthority(reopened)
            with self.assertRaises(ResearchConstitutionConflict):
                drifted.constitution(wire["id"])
        self.assertEqual(published["version"], 1)


class DeployedP8aArtifactTests(unittest.TestCase):
    ACN_MAPPING = {"company:sec-cik:0001467373": "thesis:acn:ai-reinvention-growth"}
    PLAN_HASH = "da9406d4ca106081047bcb24b1482f2b6c85cae28b748ac24871073a45a4e6dc"

    def test_v2_schedule_plan_parses_and_carries_exact_mapping(self) -> None:
        path = ROOT / "deploy/phase1/weekly-brief-schedule-us-it-services-v2.json"
        plan = WeeklyBriefSchedulePlan.from_mapping(
            json.loads(path.read_text(encoding="utf-8"))
        )
        self.assertEqual("weekly-brief-plan:us-it-services:v2", plan.plan_ref)
        self.assertEqual(self.ACN_MAPPING, dict(plan.company_thesis_refs))
        self.assertEqual(self.PLAN_HASH, plan.content_hash)
        self.assertEqual(
            "2026-09-03T11:00:00.000000+00:00", plan.effective_from
        )

    def test_p8a_bootstrap_manifest_agrees_with_v2_plan_mapping(self) -> None:
        manifest = json.loads(
            (ROOT / "deploy/phase8/p8a-us-it-services-bootstrap-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(self.ACN_MAPPING, manifest["company_thesis_refs"])
        self.assertEqual(
            manifest["company_thesis"]["candidate"]["thesis_ref"],
            self.ACN_MAPPING["company:sec-cik:0001467373"],
        )
        self.assertEqual(
            INDUSTRY, manifest["industry_thesis"]["candidate"]["company_ref"]
        )


if __name__ == "__main__":
    unittest.main()
