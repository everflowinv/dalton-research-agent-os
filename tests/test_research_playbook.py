"""Adversarial tests for the human-only ResearchPlaybook authority."""

from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path

from dalton_core.research_playbook import (
    DECISION_VOCABULARY,
    STAGE_ORDER,
    ResearchPlaybookAuthority,
    ResearchPlaybookConflict,
    ResearchPlaybookNotFound,
    ResearchPlaybookValidationError,
    read_exact_playbook_version,
    validate_research_playbook_version,
)
from dalton_core.store import DaltonStore, canonical_json
from tests.p9a_fixtures import ROOT, OWNER, playbook_params


class ResearchPlaybookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ResearchPlaybookAuthority(self.store)

    def publish(self, **overrides):
        params = playbook_params()
        params.update(overrides)
        ref = params.pop("playbook_ref")
        return self.authority.publish_playbook(ref, **params)

    def test_manifest_publishes_and_replays(self) -> None:
        first = self.publish()
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(first["version"], 1)
        self.assertEqual([s["stage_ref"] for s in first["stages"]], list(STAGE_ORDER))
        self.assertEqual(first["decision_vocabulary"], list(DECISION_VOCABULARY))
        self.assertEqual(len(first["stages"][1]["exit_gate"]["questions"]), 12)
        replay = self.publish()
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["content_hash"], first["content_hash"])
        active = self.authority.active_playbook(first["playbook_ref"])
        self.assertEqual(active["content_hash"], first["content_hash"])
        exact = read_exact_playbook_version(self.store.connection, first["id"])
        self.assertEqual(exact["content_hash"], first["content_hash"])
        report = self.authority.playbook_report()
        self.assertEqual(report["playbook_count"], 1)
        self.assertEqual(report["playbooks"][0]["human_checkpoint_stages"], ["deep_insight_gate", "investment_memo"])

    def test_json_contract_matches_record_shape(self) -> None:
        record = self.publish()
        record.pop("status")
        schema = json.loads((ROOT / "contracts/research-playbook-version.schema.json").read_text())
        self.assertEqual(set(schema["required"]), set(record))
        validate_research_playbook_version(record)

    def test_stage_order_is_frozen(self) -> None:
        params = playbook_params()
        params["stages"] = list(reversed(params["stages"]))
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)
        params = playbook_params()
        params["stages"] = params["stages"][:5]
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)

    def test_human_checkpoint_cannot_be_removed(self) -> None:
        params = playbook_params()
        params["stages"][1]["human_checkpoint"] = False
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)
        params = playbook_params()
        params["stages"][4]["human_checkpoint"] = False
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)

    def test_frozen_vocabularies_cannot_be_weakened(self) -> None:
        params = playbook_params()
        params["decision_vocabulary"] = list(DECISION_VOCABULARY)[:4]
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)
        params = playbook_params()
        params["evidence_discipline"]["number_provenance_rule"] = "numbers_may_come_from_memory"
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)
        params = playbook_params()
        params["stages"][0]["required_outputs"] = []
        ref = params.pop("playbook_ref")
        with self.assertRaises(ResearchPlaybookValidationError):
            self.authority.publish_playbook(ref, **params)

    def test_only_human_actors_publish(self) -> None:
        for actor in ("automation:coverage-mission", "system:planner", "core", ""):
            with self.subTest(actor=actor):
                with self.assertRaises(ResearchPlaybookValidationError):
                    self.publish(actor_ref=actor)

    def test_version_chain_and_idempotency_conflicts(self) -> None:
        first = self.publish()
        with self.assertRaises(ResearchPlaybookConflict):
            self.publish(title="Different body under the same key")
        with self.assertRaises(ResearchPlaybookConflict):
            self.publish(
                version_id="research-playbook-version:team-analyst-manual:2",
                idempotency_key="research-playbook:team-analyst-manual:2",
                prior_version_ref=None,
            )
        second = self.publish(
            title="团队分析师研究手册 Playbook v2",
            version_id="research-playbook-version:team-analyst-manual:2",
            idempotency_key="research-playbook:team-analyst-manual:2",
            prior_version_ref=first["id"],
        )
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.authority.active_playbook(first["playbook_ref"])["id"], second["id"])
        self.assertEqual(self.authority.playbook(first["id"])["version"], 1)
        with self.assertRaises(ResearchPlaybookNotFound):
            self.authority.playbook("research-playbook-version:missing")

    def test_schema_triggers_block_direct_writes(self) -> None:
        first = self.publish()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO research_playbook_versions(playbook_version_id,playbook_ref,version_number,"
                "prior_version_id,record_json,content_hash,actor_ref,created_at) "
                "VALUES('x','y',9,NULL,'{}','h','human:x','t')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE research_playbook_versions SET record_json='{}' WHERE playbook_version_id=?",
                (first["id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM research_playbook_versions")

    def test_drifted_record_is_detected_on_read(self) -> None:
        first = self.publish()
        tampered = copy.deepcopy(first)
        tampered.pop("status")
        tampered["title"] = "tampered"
        with self.assertRaises(ResearchPlaybookValidationError):
            validate_research_playbook_version(tampered)
        # Bypass the triggers the way a hostile process would: rebuild the row
        # through a raw connection without the authorized guard function.
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        raw.executescript(
            (Path(__file__).resolve().parents[1] / "src/dalton_core/research_playbook_schema.sql")
            .read_text(encoding="utf-8")
            .replace("dalton_research_playbook_authorized() = 0", "0 = 1")
        )
        body = copy.deepcopy(first)
        body.pop("status")
        body["title"] = "tampered"
        raw.execute(
            "INSERT INTO research_playbook_versions(playbook_version_id,playbook_ref,version_number,"
            "prior_version_id,record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                body["id"], body["playbook_ref"], 1, None, canonical_json(body),
                body["content_hash"], body["actor_ref"], body["created_at"],
            ),
        )
        with self.assertRaises(ResearchPlaybookValidationError):
            read_exact_playbook_version(raw, body["id"])


if __name__ == "__main__":
    unittest.main()
