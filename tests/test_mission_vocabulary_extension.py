"""The words the analyst layers will need, added once, granted to nobody.

Blueprint line G names ten more objects automation may be permitted to write
and three more decisions a person has to make. Adding them one slice at a time
means one mission version published per slice, each one a signed governance
act the owner has to perform; adding them together costs one. So the words go
in now and the consumers come later, which is only safe if the words are
exactly that -- a scope in AUTOMATION_WRITE_SCOPES is a scope a mission *may*
grant, never one it has.

This file therefore checks two things that pull in opposite directions: a
mission may be created granting the new scopes, and the live mission manifest
grants none of them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    AUTOMATION_WRITE_SCOPES,
    CHECKPOINT_KINDS,
    CoverageMissionAuthority,
    CoverageMissionError,
)
from dalton_core.store import DaltonStore

from tests.p9a_fixtures import (
    ROOT,
    bootstrap_method_authorities,
    load_mission_manifest,
    mission_params,
)


NEW_SCOPES = (
    "market_price",
    "consensus_estimate",
    "valuation",
    "market_event",
    "dossier",
    "debate_map",
    "forecast_revision_proposal",
    "thesis_revision_candidate",
    "research_task",
    "conviction_call",
)
NEW_CHECKPOINTS = ("thesis_revision_candidate", "conviction_call", "gate_reopen")
# What the vocabulary said before P14-0, so an addition is visible as an
# addition and a removal is visible as a removal.
PRIOR_SCOPES = (
    "evidence", "claim", "claim_challenge", "deliverable", "forecast_line",
    "model_run", "research_question", "observation", "stage_record",
    "forecast_reconciliation", "source_discovery",
)
PRIOR_CHECKPOINTS = (
    "deep_insight_gate", "investment_memo", "thesis_admission",
    "thesis_revision", "forecast_overturn", "scope_expansion",
    "budget_expansion",
)


class VocabularyShapeTests(unittest.TestCase):
    def test_the_new_words_are_appended_and_nothing_was_removed(self) -> None:
        self.assertEqual(
            AUTOMATION_WRITE_SCOPES, PRIOR_SCOPES + NEW_SCOPES
        )
        self.assertEqual(CHECKPOINT_KINDS, PRIOR_CHECKPOINTS + NEW_CHECKPOINTS)

    def test_the_json_contract_says_the_same_words(self) -> None:
        # The contract enum had drifted four scopes behind the code; it is the
        # documentation of this vocabulary and is now derived from it by hand
        # in the same commit.
        schema = json.loads(
            (ROOT / "contracts" / "coverage-mission-version.schema.json")
            .read_text(encoding="utf-8")
        )
        autonomy = schema["properties"]["autonomy"]["properties"]
        self.assertEqual(
            tuple(autonomy["may_write"]["items"]["enum"]), AUTOMATION_WRITE_SCOPES
        )
        self.assertEqual(
            tuple(autonomy["human_checkpoints"]["items"]["enum"]), CHECKPOINT_KINDS
        )


class MissionCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = DaltonStore(str(root / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)

    def create(self, params):
        return self.authority.create_mission(params.pop("mission_ref"), **params)

    def test_a_mission_may_grant_the_new_scopes(self) -> None:
        params = mission_params(self.state)
        params["autonomy"]["may_write"] = list(NEW_SCOPES)
        mission = self.create(params)
        self.assertEqual(
            tuple(mission["autonomy"]["may_write"]), NEW_SCOPES
        )

    def test_a_mission_may_require_the_new_checkpoints(self) -> None:
        params = mission_params(self.state)
        params["mission_ref"] = "coverage-mission:checkpoints"
        params["autonomy"]["human_checkpoints"] = sorted(
            set(params["autonomy"]["human_checkpoints"]) | set(NEW_CHECKPOINTS)
        )
        mission = self.create(params)
        for checkpoint in NEW_CHECKPOINTS:
            self.assertIn(checkpoint, mission["autonomy"]["human_checkpoints"])

    def test_a_word_outside_the_vocabulary_is_still_refused(self) -> None:
        params = mission_params(self.state)
        params["mission_ref"] = "coverage-mission:refused"
        params["autonomy"]["may_write"] = ["market_pricing"]
        with self.assertRaises(CoverageMissionError):
            self.create(params)


class LiveMissionManifestTests(unittest.TestCase):
    def test_the_live_mission_grants_none_of_the_new_words(self) -> None:
        # Words only: the owner publishes a new mission version when a
        # consumer exists, not because a tuple grew.
        manifest = load_mission_manifest()
        granted = set(manifest["autonomy"]["may_write"])
        self.assertEqual(granted & set(NEW_SCOPES), set())
        self.assertEqual(
            set(manifest["autonomy"]["human_checkpoints"]) & set(NEW_CHECKPOINTS),
            set(),
        )
        self.assertTrue(granted <= set(AUTOMATION_WRITE_SCOPES))


if __name__ == "__main__":
    unittest.main()
