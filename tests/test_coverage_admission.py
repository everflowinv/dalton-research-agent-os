import json
import os
import sqlite3
import tempfile
import unittest

from dalton_core.agenda import AgendaStore
from dalton_core.coverage_admission import (
    CoverageAdmissionAuthority,
    CoverageAdmissionConflict,
    CoverageAdmissionValidationError,
)
from dalton_core.contracts import ThesisVersion
from dalton_core.errors import GateRejected
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.thesis_impact import ThesisImpactAuthority
from dalton_core.writer_server import CORE_OPERATIONS, Principal, WriterServer


COMPANY_REF = "company:sec-cik:0001467373"
INDUSTRY_REF = "industry:us-it-services"


def pack_params():
    return {
        "driver_pack_ref": "driver-pack:us-it-services",
        "industry_ref": INDUSTRY_REF,
        "title": "US IT Services Driver Pack",
        "drivers": [{
            "driver_ref": "driver:bookings-conversion",
            "label": "Bookings conversion",
            "mechanism": "Bookings convert into revenue with a lag.",
            "metric_refs": ["metric:new-bookings"],
        }],
        "metric_specs": [{
            "metric_ref": "metric:new-bookings",
            "label": "New bookings",
            "definition": "Contract value recorded as new bookings in the period.",
            "unit": "USD",
            "periodicity": "quarterly",
            "preferred_source_refs": ["source:earnings-release"],
            "verification_kind": "numeric_and_semantic",
            "caveats": ["Booking definitions can differ across issuers."],
        }],
        "thesis_templates": [{
            "template_ref": "template:ai-reinvention-growth",
            "statement": "AI-led reinvention work can support durable growth.",
            "mechanism": "Bookings convert into revenue while delivery productivity protects margin.",
            "driver_refs": ["driver:bookings-conversion"],
            "implied_expectation": "Bookings and revenue growth remain aligned over time.",
            "falsifier_refs": ["falsifier:bookings-conversion-breaks"],
        }],
        "actor_ref": "human:coverage-owner",
        "version_id": "driver-pack-version:us-it-services:1",
        "prior_version_ref": None,
        "idempotency_key": "driver-pack:us-it-services:1",
    }


def thesis_content():
    return {
        "statement": "AI and reinvention demand can sustain Accenture growth.",
        "mechanism": "Bookings convert into revenue while delivery productivity protects margin.",
        "confidence": "medium",
        "implied_expectation": "Bookings growth supports subsequent revenue growth.",
        "claim_refs": [],
        "catalyst_refs": ["catalyst:quarterly-results"],
        "falsifier_refs": ["falsifier:bookings-conversion-breaks"],
        "change_reason": "Initial human-reviewed ACN coverage admission.",
    }


def invocation(identifier, family):
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": "2026-08-23T00:00:00+00:00",
        "work_order_ref": f"work:{identifier}",
        "profile_ref": f"profile:{identifier}",
        "granularity": "task",
        "capability": "research",
        "provider": f"provider:{family}",
        "model": f"model:{family}",
        "model_family": family,
        "runtime_ref": "runtime:test",
        "actor_ref": "automation:test",
        "usage": {"tokens": 1},
        "input_refs": [],
        "output_refs": [],
        "started_at": "2026-08-23T00:00:00+00:00",
        "completed_at": None,
        "side_effects": [],
        "parent_ref": None,
    }


class CoverageAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.agenda = AgendaStore(self.store)
        self.authority = CoverageAdmissionAuthority(self.store)
        self.mandate = self.agenda.create_mandate(
            "mandate:us-it-services-acn",
            objective="Establish initial ACN coverage under the US IT Services method.",
            scope_refs=[COMPANY_REF, INDUSTRY_REF],
            constraints={"human_thesis_admission_required": True},
            success_criteria={"initial_thesis_count": 1},
            effective_from="2020-01-01T00:00:00+00:00",
            effective_until=None,
            actor_ref="human:coverage-owner",
            activate=True,
            version_id="mandate-version:us-it-services-acn:1",
            idempotency_key="mandate:us-it-services-acn:1",
        )
        params = pack_params()
        ref = params.pop("driver_pack_ref")
        self.pack = self.authority.register_driver_pack(ref, **params)

    def propose(self):
        return self.authority.propose_thesis_admission(
            candidate_id="thesis-admission-candidate:acn:1",
            thesis_ref="thesis:acn:ai-reinvention-growth",
            company_ref=COMPANY_REF,
            industry_ref=INDUSTRY_REF,
            template_ref="template:ai-reinvention-growth",
            driver_refs=["driver:bookings-conversion"],
            mandate_version_ref=self.mandate["id"],
            mandate_version_hash=self.mandate["content_hash"],
            driver_pack_version_ref=self.pack["id"],
            driver_pack_version_hash=self.pack["content_hash"],
            content=thesis_content(),
            actor_ref="human:coverage-owner",
            idempotency_key="thesis-admission-candidate:acn:1",
        )

    def test_human_admission_is_bound_immutable_and_idempotent(self):
        duplicate_pack_params = pack_params()
        duplicate_ref = duplicate_pack_params.pop("driver_pack_ref")
        duplicate_pack = self.authority.register_driver_pack(
            duplicate_ref, **duplicate_pack_params
        )
        self.assertEqual(duplicate_pack["status"], "duplicate")

        candidate = self.propose()
        duplicate_candidate = self.propose()
        self.assertEqual(duplicate_candidate["status"], "duplicate")
        self.assertEqual(candidate["driver_refs"], ["driver:bookings-conversion"])
        self.assertEqual(
            self.authority.candidate(candidate["id"])["content_hash"],
            candidate["content_hash"],
        )

        self.store.stage_change(
            "change:unauthorized-acn",
            thesis_id="thesis:acn:ai-reinvention-growth",
            content=thesis_content(),
            producer_invocation=invocation("invocation:producer", "family-a"),
        )
        self.store.verify_change(
            "change:unauthorized-acn",
            verification_id="verification:unauthorized-acn",
            verifier_invocation=invocation("invocation:verifier", "family-b"),
            verdict="pass",
            findings=[],
        )
        with self.assertRaises(GateRejected):
            self.store.commit(
                "change:unauthorized-acn",
                "verification:unauthorized-acn",
                "commit:unauthorized-acn",
            )

        decision_args = {
            "candidate_id": candidate["id"],
            "candidate_hash": candidate["content_hash"],
            "verdict": "admit",
            "rationale": "The thesis has explicit drivers and falsifiers.",
            "decision_id": "thesis-admission-decision:acn:1",
            "actor_ref": "human:portfolio-manager",
            "idempotency_key": "thesis-admission-decision:acn:1",
        }
        admitted = self.authority.decide_thesis_admission(**decision_args)
        replay = self.authority.decide_thesis_admission(**decision_args)
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(
            replay["thesis_version"]["id"], admitted["thesis_version"]["id"]
        )
        thesis = ThesisVersion.from_dict(admitted["thesis_version"])
        self.assertEqual(thesis.schema_version, "0.2")
        self.assertEqual(thesis.confidence, "medium")
        self.assertEqual(thesis.authority_kind, "human_admission")
        self.assertEqual(thesis.authority_ref, decision_args["decision_id"])
        pointer = self.store.current_pointer("thesis:acn:ai-reinvention-growth")
        self.assertEqual(pointer["version_id"], thesis.id)
        sql_row = self.store.conn.execute(
            "SELECT change_id,verification_id,admission_decision_id,authority_kind "
            "FROM thesis_versions WHERE version_id=?", (thesis.id,)
        ).fetchone()
        self.assertIsNone(sql_row["change_id"])
        self.assertIsNone(sql_row["verification_id"])
        self.assertEqual(sql_row["admission_decision_id"], decision_args["decision_id"])
        self.assertEqual(sql_row["authority_kind"], "human_admission")
        impact_read = ThesisImpactAuthority._read_current_thesis(
            self.store.conn.cursor(), thesis.id
        )
        self.assertEqual(impact_read["authority_kind"], "human_admission")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute(
                "UPDATE thesis_admission_decisions SET verdict='reject' WHERE decision_id=?",
                (decision_args["decision_id"],),
            )

    def test_nonhuman_and_unbound_candidates_fail_closed(self):
        with self.assertRaises(CoverageAdmissionValidationError):
            bad = pack_params()
            bad["version_id"] = "driver-pack-version:automation"
            bad["idempotency_key"] = "driver-pack:automation"
            bad["actor_ref"] = "automation:coverage"
            self.authority.register_driver_pack(
                bad.pop("driver_pack_ref"), **bad
            )
        with self.assertRaises(CoverageAdmissionConflict):
            self.authority.propose_thesis_admission(
                candidate_id="candidate:bad-driver",
                thesis_ref="thesis:acn:bad-driver",
                company_ref=COMPANY_REF,
                industry_ref=INDUSTRY_REF,
                template_ref="template:ai-reinvention-growth",
                driver_refs=["driver:not-in-pack"],
                mandate_version_ref=self.mandate["id"],
                mandate_version_hash=self.mandate["content_hash"],
                driver_pack_version_ref=self.pack["id"],
                driver_pack_version_hash=self.pack["content_hash"],
                content=thesis_content(),
                actor_ref="human:coverage-owner",
                idempotency_key="candidate:bad-driver",
            )

    def test_second_decision_and_initial_revision_are_rejected(self):
        candidate = self.propose()
        self.authority.decide_thesis_admission(
            candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"],
            verdict="admit",
            rationale="Initial admission.",
            decision_id="decision:one",
            actor_ref="human:portfolio-manager",
            idempotency_key="decision:one",
        )
        with self.assertRaises(CoverageAdmissionConflict):
            self.authority.decide_thesis_admission(
                candidate_id=candidate["id"],
                candidate_hash=candidate["content_hash"],
                verdict="reject",
                rationale="Conflicting second decision.",
                decision_id="decision:two",
                actor_ref="human:portfolio-manager",
                idempotency_key="decision:two",
            )
        with self.assertRaises(CoverageAdmissionConflict):
            self.authority.propose_thesis_admission(
                candidate_id="candidate:revision",
                thesis_ref="thesis:acn:ai-reinvention-growth",
                company_ref=COMPANY_REF,
                industry_ref=INDUSTRY_REF,
                template_ref="template:ai-reinvention-growth",
                driver_refs=["driver:bookings-conversion"],
                mandate_version_ref=self.mandate["id"],
                mandate_version_hash=self.mandate["content_hash"],
                driver_pack_version_ref=self.pack["id"],
                driver_pack_version_hash=self.pack["content_hash"],
                content=thesis_content(),
                actor_ref="human:coverage-owner",
                idempotency_key="candidate:revision",
            )


class ThesisAuthorityMigrationTests(unittest.TestCase):
    def test_empty_legacy_table_is_upgraded_to_nullable_human_authority_shape(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        connection = sqlite3.connect(handle.name)
        connection.execute(
            "CREATE TABLE thesis_versions ("
            "version_id TEXT PRIMARY KEY, thesis_id TEXT NOT NULL, "
            "version_number INTEGER NOT NULL, content_json TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, prior_version_id TEXT, "
            "change_id TEXT NOT NULL, verification_id TEXT, committed_by TEXT, "
            "created_at TEXT NOT NULL, UNIQUE(thesis_id,version_number))"
        )
        connection.commit()
        connection.close()
        store = DaltonStore(handle.name)
        self.addCleanup(store.close)
        columns = {
            row["name"]: row for row in store.conn.execute(
                "PRAGMA table_info(thesis_versions)"
            )
        }
        self.assertEqual(columns["change_id"]["notnull"], 0)
        self.assertIn("admission_decision_id", columns)
        self.assertIn("authority_kind", columns)
        self.assertIn("authority_ref", columns)

    def test_legacy_thesis_row_replays_with_exact_verification_authority(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        thesis_body = {
            "statement": "Legacy statement.",
            "mechanism": "Legacy mechanism.",
            "confidence": 0.6,
            "implied_expectation": "Legacy expectation.",
            "claim_refs": [],
            "catalyst_refs": [],
            "falsifier_refs": [],
            "change_reason": "Legacy version.",
        }
        thesis_hash = content_hash(thesis_body)
        thesis_wire = {
            "schema_version": "0.1",
            "id": "thesis-version:legacy:1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "thesis_ref": "thesis:legacy",
            "version": 1,
            **thesis_body,
            "prior_version_ref": None,
            "verification_ref": "verification:legacy:1",
            "committed_by_ref": "system:legacy",
            "content_hash": thesis_hash,
        }
        connection = sqlite3.connect(handle.name)
        connection.executescript(
            "CREATE TABLE staging_changes ("
            "change_id TEXT PRIMARY KEY, thesis_id TEXT NOT NULL, "
            "content_json TEXT NOT NULL, content_hash TEXT NOT NULL, "
            "producer_invocation_id TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE verification_records ("
            "verification_id TEXT PRIMARY KEY, change_id TEXT, "
            "producer_invocation_id TEXT NOT NULL, verifier_invocation_id TEXT NOT NULL, "
            "verdict TEXT NOT NULL, findings_json TEXT NOT NULL, "
            "verification_json TEXT NOT NULL, content_hash TEXT NOT NULL, "
            "policy_version_id TEXT NOT NULL, created_at TEXT NOT NULL);"
            "CREATE TABLE thesis_versions ("
            "version_id TEXT PRIMARY KEY, thesis_id TEXT NOT NULL, "
            "version_number INTEGER NOT NULL, content_json TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, prior_version_id TEXT, "
            "change_id TEXT NOT NULL, verification_id TEXT, committed_by TEXT, "
            "created_at TEXT NOT NULL, UNIQUE(thesis_id,version_number));"
        )
        connection.execute(
            "INSERT INTO staging_changes VALUES(?,?,?,?,?,?,?,?)",
            (
                "change:legacy:1", "thesis:legacy", canonical_json(thesis_body),
                thesis_hash, "invocation:legacy-producer", "committed",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO verification_records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "verification:legacy:1", "change:legacy:1",
                "invocation:legacy-producer", "invocation:legacy-verifier", "pass",
                "[]", "{}", thesis_hash, "policy:legacy",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO thesis_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "thesis-version:legacy:1", "thesis:legacy", 1,
                canonical_json(thesis_wire), thesis_hash, None, "change:legacy:1",
                "verification:legacy:1", "system:legacy",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
        connection.close()

        store = DaltonStore(handle.name)
        self.addCleanup(store.close)
        replayed = store.get_version("thesis-version:legacy:1")
        self.assertEqual(replayed["content"], thesis_wire)
        self.assertEqual(replayed["authority_kind"], "verification")
        self.assertEqual(replayed["authority_ref"], "verification:legacy:1")
        self.assertEqual(ThesisVersion.from_dict(replayed["content"]).confidence, 0.6)


class AcnTargetMappingTests(unittest.TestCase):
    def test_exact_acn_mapping_adds_one_target_and_removal_adds_none(self):
        class Plans:
            @staticmethod
            def plans():
                return [{
                    "state": "started",
                    "start_binding": {"id": "research-plan-start:acn:1"},
                    "plan_version": {
                        "id": "research-plan-version:acn:1",
                        "content_hash": "a" * 64,
                        "question_ref": "research-question:acn:revenue-growth",
                    },
                }]

        class Backlog:
            @staticmethod
            def question(_question_ref):
                return {
                    "state": "answered",
                    "head": {"company_ref": COMPANY_REF},
                }

        with tempfile.TemporaryDirectory() as directory:
            server = WriterServer(
                os.path.join(directory, "core.sqlite"),
                os.path.join(directory, "writer.sock"),
                {
                    "core": Principal(
                        "core", "core-token", CORE_OPERATIONS, unrestricted=True
                    )
                },
            )
            server._research_plan = Plans()
            server._backlog = Backlog()
            mapping = {
                COMPANY_REF: "thesis:acn:ai-reinvention-growth"
            }
            targets = server._op_thesis_impact_targets({
                "company_thesis_refs": mapping, "limit": 10
            })
            self.assertEqual(len(targets), 1)
            self.assertEqual(
                targets[0]["thesis_ref"],
                "thesis:acn:ai-reinvention-growth",
            )
            self.assertEqual(
                server._op_thesis_impact_targets({
                    "company_thesis_refs": {}, "limit": 10
                }),
                [],
            )


if __name__ == "__main__":
    unittest.main()
