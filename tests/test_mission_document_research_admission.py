from __future__ import annotations

import contextlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_document_research_admission import (
    MissionDocumentAdmissionHostError,
    admit_directed_inquiry,
    open_mission_document_admission_authority,
    registration_index,
)
from dalton_core.research_question_backlog import ResearchQuestionBacklog, ResearchQuestionConflict
from dalton_core.research_task_cli import run_admissions
from dalton_core.mission_document_model_authority import (
    DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME as ANNUAL_DRAFT_CONFIG,
    VERIFIER_MODEL_CONFIG_NAME as ANNUAL_VERIFIER_CONFIG,
)
from dalton_core.store import canonical_json, content_hash
from tests import test_mission_document_research as mission_document_tests


class MissionDocumentAdmissionProducerTests(unittest.TestCase):
    def _fixture(self, *, company_in_mandate=True):
        owner = mission_document_tests.MissionDocumentResearchTests(
            "test_exact_archived_plan_question_and_registration_admit_and_replay"
        )
        self.addCleanup(owner.doCleanups)
        fixture, authority, args, registration, _launcher = owner._fixture(company_in_mandate=company_in_mandate)
        plan = CoverageMissionAuthority(fixture.store).latest_research_plan(
            fixture.mission["id"]
        )
        return fixture, authority, args, registration, plan

    def test_forged_inquiry_refuses_before_any_backlog_write(self):
        fixture, authority, _args, registration, plan = self._fixture(company_in_mandate=False)
        before = fixture.store.connection.execute("SELECT COUNT(*) FROM backlog_questions").fetchone()[0]
        forged = {**plan["inquiries"][0], "question": "A caller-invented question?"}
        with self.assertRaisesRegex(mission_document_tests.MissionDocumentResearchError, "absent or ambiguous"):
            admit_directed_inquiry(
                authority=authority, registrations={registration["id"]: registration},
                backlog=ResearchQuestionBacklog(fixture.store), mission=fixture.mission,
                plan=plan, inquiry=forged,
            )
        self.assertEqual(fixture.store.connection.execute("SELECT COUNT(*) FROM backlog_questions").fetchone()[0], before)

    def test_industry_mandate_admits_company_in_exact_mission_universe(self):
        fixture, authority, _args, registration, plan = self._fixture(company_in_mandate=False)
        result = admit_directed_inquiry(
            authority=authority, registrations={registration["id"]: registration},
            backlog=ResearchQuestionBacklog(fixture.store), mission=fixture.mission,
            plan=plan, inquiry=plan["inquiries"][0],
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["company_ref"], plan["inquiries"][0]["company_ref"])

    def test_mission_question_binding_is_checked_before_duplicate_return(self):
        fixture, _authority, _args, registration, plan = self._fixture(company_in_mandate=False)
        backlog = ResearchQuestionBacklog(fixture.store)
        mission = fixture.mission
        base = dict(
            mandate_version_ref=mission["bindings"]["mandate_version"]["ref"],
            company_ref=plan["inquiries"][0]["company_ref"], question="Unique scope check?",
            answer_criteria="An exact original quote.", source_refs=[registration["source_ref"]],
            actor_ref=mission["autonomy"]["automation_principal"],
            mission_binding={"ref": mission["id"], "hash": mission["content_hash"]},
            idempotency_key="test:mission-question-scope",
        )
        # Failure after all three question inserts must roll everything back.
        counts = {name: fixture.store.connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                  for name in ("backlog_questions", "backlog_question_versions", "backlog_question_pointer",
                               "backlog_question_events", "backlog_idempotency")}
        with patch.object(backlog, "_event", side_effect=RuntimeError("injected after inserts")):
            with self.assertRaisesRegex(RuntimeError, "injected after inserts"):
                backlog.record_question(**base)
        for name, count in counts.items():
            self.assertEqual(fixture.store.connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0], count)
        first = backlog.record_question(**base)
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(backlog.record_question(**base)["question_version_ref"], first["question_version_ref"])
        invalid = [
            {"mission_binding": {"ref": mission["id"], "hash": "0" * 64}},
            {"company_ref": "company:sec-cik:0000000001"},
            {"actor_ref": "automation:another-mission"},
            {"source_refs": ["source:not-connected"]},
            {"mission_binding": None, "idempotency_key": None},
        ]
        for delta in invalid:
            with self.subTest(delta=delta), self.assertRaises(ResearchQuestionConflict):
                backlog.record_question(**{**base, **delta})
        self.assertEqual(fixture.store.connection.execute(
            "SELECT COUNT(*) FROM backlog_questions WHERE question_ref=?", (first["question_ref"],)
        ).fetchone()[0], 1)
        # A new signed mission revokes the question grant. The old binding
        # must fail even for a previously recorded idempotency key; the new
        # binding must also fail because its principal lacks this write scope.
        params = {key: mission[key] for key in (
            "title", "objective", "industry_ref", "universe", "research_questions",
            "deliverables", "source_plan", "bindings", "autonomy", "budget",
        )}
        params["autonomy"] = {
            **mission["autonomy"], "may_write": [item for item in mission["autonomy"]["may_write"]
                                                 if item != "research_question"],
        }
        successor = CoverageMissionAuthority(fixture.store).create_mission(
            mission["mission_ref"], **params, actor_ref="human:test-owner",
            version_id="coverage-mission-version:scope-revoked:2",
            prior_version_ref=mission["id"], idempotency_key="test:scope-revocation",
        )
        with self.assertRaises(ResearchQuestionConflict):
            backlog.record_question(**base)
        with self.assertRaises(ResearchQuestionConflict):
            backlog.record_question(**{**base, "mission_binding": {
                "ref": successor["id"], "hash": successor["content_hash"],
            }})

    def test_producer_derives_every_mutation_input_from_the_stored_inquiry(self):
        fixture, authority, _args, registration, plan = self._fixture()
        inquiry = plan["inquiries"][0]
        result = admit_directed_inquiry(
            authority=authority,
            registrations={registration["id"]: registration},
            backlog=ResearchQuestionBacklog(fixture.store),
            mission=fixture.mission,
            plan=plan,
            inquiry=inquiry,
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["company_ref"], inquiry["company_ref"])
        self.assertEqual(result["document_authority_ref"], registration["id"])
        self.assertEqual(
            fixture.store.connection.execute(
                "SELECT COUNT(*) FROM mission_document_research_admissions"
            ).fetchone()[0],
            1,
        )

    def test_normal_research_task_child_admits_and_replays_the_directed_inquiry(self):
        fixture, authority, _args, registration, _plan = self._fixture()

        @contextlib.contextmanager
        def exact_runtime(**_kwargs):
            yield authority, {registration["id"]: registration}

        kwargs = {
            "state_dir": fixture.state,
            "summary_dir": fixture.state / "mission-document-admission-summary",
            "planner_scheduler_db": Path("/authority/scheduler.sqlite"),
            "planner_model_config_path": Path("/authority/planner.json"),
        }
        with patch(
            "dalton_core.mission_document_research_admission."
            "open_mission_document_admission_authority",
            exact_runtime,
        ):
            first = run_admissions(**kwargs)
            second = run_admissions(**kwargs)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["admitted"], 1)
        self.assertEqual(first["tasks"][0]["kind"], "mission_directed_document_research")
        self.assertEqual(second["admitted"], 0)
        self.assertEqual(second["tasks"][0]["status"], "duplicate")
        self.assertEqual(first["tasks"][0]["admission_ref"], second["tasks"][0]["admission_ref"])

    def test_production_host_reads_real_router_scheduler_and_model_configs(self):
        fixture, _authority, _args, registration, plan = self._fixture()
        for source, target in (
            (ANNUAL_DRAFT_CONFIG, DRAFT_MODEL_CONFIG_NAME),
            (ANNUAL_VERIFIER_CONFIG, VERIFIER_MODEL_CONFIG_NAME),
        ):
            value = json.loads((fixture.state / source).read_text(encoding="utf-8"))
            (fixture.state / target).write_text(
                canonical_json(value) + "\n", encoding="utf-8"
            )
            os.chmod(fixture.state / target, 0o600)
        scheduler_path = fixture.harness.scheduler().connection.execute(
            "PRAGMA database_list"
        ).fetchone()[2]
        inventory = {
            "registry": _authority.registry,
            "registration_by_hash": {registration["content_hash"]: registration},
        }
        with patch(
            "dalton_core.mission_document_research_admission."
            "load_document_inventory_authority",
            return_value=inventory,
        ), open_mission_document_admission_authority(
            store=fixture.store,
            state_dir=fixture.state,
            mission=fixture.mission,
            planner_scheduler_db=scheduler_path,
            planner_model_config_path=fixture.state / ANNUAL_DRAFT_CONFIG,
            clock=fixture.harness.clock,
        ) as (authority, registrations):
            result = admit_directed_inquiry(
                authority=authority,
                registrations=registrations,
                backlog=ResearchQuestionBacklog(fixture.store),
                mission=fixture.mission,
                plan=plan,
                inquiry=plan["inquiries"][0],
            )
        self.assertEqual(result["status"], "fresh")

    def test_unavailable_server_side_registration_is_a_refusal(self):
        fixture, authority, _args, _registration, plan = self._fixture()
        with self.assertRaisesRegex(
            MissionDocumentAdmissionHostError, "registration is unavailable"
        ):
            admit_directed_inquiry(
                authority=authority,
                registrations={},
                backlog=ResearchQuestionBacklog(fixture.store),
                mission=fixture.mission,
                plan=plan,
                inquiry=plan["inquiries"][0],
            )
        self.assertEqual(
            fixture.store.connection.execute(
                "SELECT COUNT(*) FROM mission_document_research_admissions"
            ).fetchone()[0],
            0,
        )

    def test_registration_reindex_rejects_hash_or_id_aliases(self):
        _fixture, _authority, _args, registration, _plan = self._fixture()
        with self.assertRaisesRegex(MissionDocumentAdmissionHostError, "drifted"):
            registration_index({"f" * 64: registration})
        altered = {**registration, "document_ref": "document:other"}
        altered["content_hash"] = content_hash(
            {key: value for key, value in altered.items() if key != "content_hash"}
        )
        with self.assertRaisesRegex(MissionDocumentAdmissionHostError, "drifted"):
            registration_index({
                registration["content_hash"]: registration,
                altered["content_hash"]: altered,
            })


if __name__ == "__main__":
    unittest.main()
