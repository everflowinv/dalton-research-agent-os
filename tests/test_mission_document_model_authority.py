from __future__ import annotations

import json
import os
import unittest

from dalton_core.mission_document_model_authority import (
    DRAFT_MODEL_CONFIG_NAME,
    DRAFT_PURPOSE,
    MissionDocumentModelAuthority,
    MissionDocumentModelAuthorityError,
    VERIFIER_MODEL_CONFIG_NAME,
    VERIFIER_PURPOSE,
    load_mission_document_model_configs,
)
from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME as ANNUAL_DRAFT_CONFIG,
    VERIFIER_MODEL_CONFIG_NAME as ANNUAL_VERIFIER_CONFIG,
)
from dalton_core.store import canonical_json, content_hash
from tests.test_mission_annual_research import MissionAnnualFixture


class MissionDocumentModelAuthorityTests(unittest.TestCase):
    def _fixture(self, *, same_family: bool = False):
        fixture = MissionAnnualFixture(self, same_family=same_family)
        for source, target in (
            (ANNUAL_DRAFT_CONFIG, DRAFT_MODEL_CONFIG_NAME),
            (ANNUAL_VERIFIER_CONFIG, VERIFIER_MODEL_CONFIG_NAME),
        ):
            value = json.loads((fixture.state / source).read_text(encoding="utf-8"))
            (fixture.state / target).write_text(
                canonical_json(value) + "\n", encoding="utf-8"
            )
            os.chmod(fixture.state / target, 0o600)
        return fixture

    def test_exact_generic_configs_router_and_budget_build_closed_authority(self):
        fixture = self._fixture()
        resolver = MissionDocumentModelAuthority(
            state_dir=fixture.state,
            router=fixture.router,
            clock=fixture.harness.clock,
        )
        executions, proof = resolver()
        self.assertEqual(set(executions), {"draft", "verifier"})
        self.assertEqual(proof["draft"]["purpose"], DRAFT_PURPOSE)
        self.assertEqual(proof["verifier"]["purpose"], VERIFIER_PURPOSE)
        self.assertEqual(
            proof["draft"]["config_hash"],
            content_hash(load_mission_document_model_configs(fixture.state)[0]),
        )
        self.assertEqual(
            proof["verifier"]["budget_policy_ceiling"]["policy_version_ref"],
            executions["verifier"]["budget_policy_ref"],
        )

    def test_missing_generic_configs_never_fall_back_to_annual_configs(self):
        fixture = MissionAnnualFixture(self)
        with self.assertRaisesRegex(
            MissionDocumentModelAuthorityError, "mission document draft.*not configured"
        ):
            MissionDocumentModelAuthority(
                state_dir=fixture.state,
                router=fixture.router,
                clock=fixture.harness.clock,
            )()

    def test_draft_and_verifier_must_share_router_and_budget_authority(self):
        fixture = self._fixture()
        path = fixture.state / VERIFIER_MODEL_CONFIG_NAME
        value = json.loads(path.read_text(encoding="utf-8"))
        value["budget_policy_ref"] = "budget-policy:foreign"
        path.write_text(canonical_json(value) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(MissionDocumentModelAuthorityError, "one budget authority"):
            load_mission_document_model_configs(fixture.state)

    def test_verifier_family_must_be_independent(self):
        fixture = self._fixture(same_family=True)
        with self.assertRaisesRegex(MissionDocumentModelAuthorityError, "independent"):
            MissionDocumentModelAuthority(
                state_dir=fixture.state,
                router=fixture.router,
                clock=fixture.harness.clock,
            )()


if __name__ == "__main__":
    unittest.main()
