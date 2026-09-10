from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dalton_core.lane_failure_class import CONTENT_REFUSED, Classification
from dalton_core.lane_failure_ledger import lane_budget
from dalton_core.mission_debate_map_lane import _business_key as debate_business_key
from dalton_core.mission_conviction_lane import _business_key as conviction_business_key
from dalton_core.debate_map_launcher import run_digest as debate_run_digest
from dalton_core.conviction_call_launcher import run_digest as conviction_run_digest

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = tuple(sorted((ROOT / "src" / "dalton_core").glob("*provider-output*.schema.json")))
GOOGLE_PORTABLE_KEYS = frozenset({
    "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description",
    "enum", "items", "prefixItems", "minItems", "maxItems", "minimum", "maximum",
    "anyOf", "oneOf", "properties", "additionalProperties", "required",
    "propertyOrdering",
})


def assert_portable(test: unittest.TestCase, node: object, *, in_properties: bool = False) -> None:
    if isinstance(node, list):
        for child in node:
            assert_portable(test, child)
    elif isinstance(node, dict):
        for key, child in node.items():
            if not in_properties:
                test.assertIn(key, GOOGLE_PORTABLE_KEYS)
            assert_portable(test, child, in_properties=key in {"properties", "$defs"})


class PortableVerifierContractTests(unittest.TestCase):
    def test_every_packaged_provider_schema_matches_google_portable_boundary(self) -> None:
        self.assertGreaterEqual(len(SCHEMAS), 9)
        for path in SCHEMAS:
            with self.subTest(schema=path.name):
                assert_portable(self, json.loads(path.read_text(encoding="utf-8")))

    def test_every_schema_crosses_the_installed_google_validator(self) -> None:
        modules = sorted(Path.home().glob(
            ".openclaw/tools/node-*/lib/node_modules/openclaw/node_modules/"
            "@openclaw/ai/dist/google-shared-*.mjs"
        ))
        if not modules:
            self.skipTest("installed OpenClaw Google provider-control module unavailable")
        module = modules[-1]
        for path in SCHEMAS:
            script = r'''import { pathToFileURL } from "node:url";
import fs from "node:fs";
const mod = await import(pathToFileURL(process.argv[1]));
const schema = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const hash = process.argv[3];
const now = Date.now();
let validated = false;
const controls = {mode:"google-generative-ai-count-tokens-v1",maxInputTokens:100,
 maxOutputTokens:10,maxTotalTokens:110,maxCostUsd:1,structuredOutput:{schemaName:"dalton_test",schemaHash:hash,jsonSchema:schema},
 rateCard:{model:"google/gemini-test",serviceTier:"default",inputUsdPerMillion:1,cachedInputUsdPerMillion:1,cacheWriteUsdPerMillion:1,outputUsdPerMillion:1,verifiedAt:new Date(now-1000).toISOString(),expiresAt:new Date(now+60000).toISOString()}};
try { await mod.r({model:{provider:"google",api:"google-generative-ai",id:"gemini-test"},
 createClient:()=>({models:{countTokens:async()=>{validated=true; throw new Error("VALIDATED_SENTINEL")}}}),
 buildParams:()=>({contents:[]}),output:{},options:{providerControls:controls}}); }
catch (error) { if (validated) process.exit(0); console.error(error.message); process.exit(2); }
process.exit(validated ? 0 : 3);'''
            import hashlib
            schema = json.loads(path.read_text(encoding="utf-8"))
            encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(module), str(path), hashlib.sha256(encoded).hexdigest()],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, f"{path.name}: {result.stderr}")

    def test_contract_change_retires_old_lane_hold_identity(self) -> None:
        mission = {"id": "mission-version:1", "content_hash": "a" * 64}
        classification = Classification(CONTENT_REFUSED, "old refusal", "test")
        with tempfile.TemporaryDirectory() as root:
            debate = lane_budget("mission_debate_map", state_dir=root)
            with patch("dalton_core.cockpit_model.verifier_provider_contract_fingerprint", return_value="1" * 64):
                old = debate_business_key("company:ACN", "evidence", mission)
            debate.record(old, classification=classification)
            with patch("dalton_core.cockpit_model.verifier_provider_contract_fingerprint", return_value="2" * 64):
                new = debate_business_key("company:ACN", "evidence", mission)
            self.assertIsNotNone(debate.blocked(old))
            self.assertIsNone(debate.blocked(new))
            self.assertNotEqual(debate_run_digest("company:ACN", "evidence", "1" * 64),
                                debate_run_digest("company:ACN", "evidence", "2" * 64))

            conviction = lane_budget("mission_conviction", state_dir=root)
            with patch("dalton_core.cockpit_model.verifier_provider_contract_fingerprint", return_value="1" * 64):
                old = conviction_business_key("company:ACN", "evidence")
            conviction.record(old, classification=classification)
            with patch("dalton_core.cockpit_model.verifier_provider_contract_fingerprint", return_value="2" * 64):
                new = conviction_business_key("company:ACN", "evidence")
            self.assertIsNotNone(conviction.blocked(old))
            self.assertIsNone(conviction.blocked(new))
            self.assertNotEqual(conviction_run_digest("company:ACN", "evidence", "1" * 64),
                                conviction_run_digest("company:ACN", "evidence", "2" * 64))
