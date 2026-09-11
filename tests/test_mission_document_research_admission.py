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
from dalton_core.research_question_backlog import ResearchQuestionBacklog
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
    def _fixture(self):
        owner = mission_document_tests.MissionDocumentResearchTests(
            "test_exact_archived_plan_question_and_registration_admit_and_replay"
        )
        self.addCleanup(owner.doCleanups)
        fixture, authority, args, registration, _launcher = owner._fixture()
        plan = CoverageMissionAuthority(fixture.store).latest_research_plan(
            fixture.mission["id"]
        )
        return fixture, authority, args, registration, plan

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
