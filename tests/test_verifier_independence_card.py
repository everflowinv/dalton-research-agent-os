"""2026-10-08: 「核验独立性 · 起草模型顺序」 -- one place to steer the draft model
of every stage an independent verifier checks.

Pinned down here:

* pair discovery, from a synthetic registry and from the shipped one (every
  live verifier stage has a producer);
* conflict detection, including the two cooldown cases (a cooling first link
  pushing the draft onto the verifier's family; the only independent verifier
  cooling);
* the recommendation is the minimal reorder;
* the 48 h refusal count reads batch w's ``chain_refusal`` and the pre-batch-w
  shape of the same refusal;
* a canary: a brand-new workspace whose localization draft sits in the
  verifier's family is reported in conflict, the card's save goes through the
  writer's ``set_model_selection``, and afterwards the pair is independent and
  parity says so;
* the page: the card renders, and the chain editors' drag no longer reorders a
  link from a stale drag (the 2026-10-07 save-path audit).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from dalton_core.model_router import ModelRouter
from dalton_core.model_selection import publish_selection, publish_tier_selection
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from dalton_core.verifier_independence import (
    STATUS_CONFLICT,
    STATUS_COOLDOWN,
    STATUS_INDEPENDENT,
    STATUS_NO_VERIFIER,
    discover_pairs,
    environment_report,
    evaluate_pair,
    recommend_order,
    refusal_counts,
    status_label,
)
from tests.test_model_selection import NOW, OWNER, RouterCase, _allowing_config, _controls

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"

GOOGLE_DRAFT = "profile:gemini-3-8-flash-antigravity"
DEEPSEEK = "profile:deepseek-v4-flash"
HAIKU = "profile:claude-haiku"
GLM = "profile:zai-glm-5-3-flash"
VERIFIERS = ["profile:gemini-3-8-flash", "profile:gemini-3-1-pro-preview"]


def _profile(profile_id: str, family: str, *, verify: bool = False,
             price: tuple[float, float] = (1.0, 2.0)) -> dict:
    return {
        "id": profile_id, "family": family, "model": profile_id.split(":", 1)[1],
        "provider": "fixture",
        "capabilities": ["research", *(["provider-controlled-verify"] if verify else [])],
        "availability": {"state": "available", "valid_until": "2036-01-01T00:00:00+00:00"},
        "cost": {"input_per_million_usd": price[0], "output_per_million_usd": price[1]},
    }


PROFILES = {
    GOOGLE_DRAFT: _profile(GOOGLE_DRAFT, "google-gemini-3", price=(0.75, 3.75)),
    DEEPSEEK: _profile(DEEPSEEK, "deepseek-v4", price=(0.15, 0.6)),
    HAIKU: _profile(HAIKU, "anthropic-claude-5", price=(0.1, 0.5)),
    GLM: _profile(GLM, "zhipu-glm-5.3", price=(0.15, 0.5)),
    VERIFIERS[0]: _profile(VERIFIERS[0], "google-gemini-3", verify=True),
    VERIFIERS[1]: _profile(VERIFIERS[1], "google-gemini-3", verify=True),
    "profile:claude-verifier": _profile("profile:claude-verifier", "anthropic-claude-5",
                                        verify=True),
}
LIVE_CHEAP = [GOOGLE_DRAFT, DEEPSEEK, HAIKU, GLM]


class PairDiscoveryTests(unittest.TestCase):
    def test_a_synthetic_registry_pairs_by_name_and_by_declaration(self) -> None:
        tiers = {
            "alpha": "brain", "alpha_verifier": "verifier",
            "beta_draft": "cheap", "beta_verifier": "verifier",
            "gamma": "cheap", "lonely_verifier": "verifier",
            "omega": "cheap", "checked_by_extra": "verifier",
            "old": "brain", "old_verifier": "verifier",
            "delta_assessment": "brain", "delta_verifier": "verifier",
        }
        found = discover_pairs(tiers, extra_producers={"checked_by_extra": ("omega", "missing")},
                               retired=("old_verifier",))
        pairs = {(item["draft_purpose"], item["verifier_purpose"]) for item in found["pairs"]}
        self.assertEqual(pairs, {
            ("alpha", "alpha_verifier"), ("beta_draft", "beta_verifier"),
            ("omega", "checked_by_extra"), ("delta_assessment", "delta_verifier"),
        })
        self.assertEqual(found["unpaired"], ["lonely_verifier"])
        self.assertTrue(all("provider-controlled-verify" in item["requires"]
                            for item in found["pairs"]))

    def test_every_shipped_verifier_stage_names_what_it_checks(self) -> None:
        from dalton_core.model_selection import PURPOSE_LABELS

        found = discover_pairs()
        # Shipped stages all carry an owner label; test probes registered by
        # other suites into the same process-wide table do not.
        self.assertEqual([name for name in found["unpaired"] if name in PURPOSE_LABELS], [])
        pairs = {(item["draft_purpose"], item["verifier_purpose"]) for item in found["pairs"]}
        for expected in [
            ("research_localization", "research_localization_verifier"),
            ("research_language_revision", "research_localization_verifier"),
            ("dossier", "dossier_verifier"), ("deep_insight_gate", "deep_insight_gate_verifier"),
            ("event_judgement", "event_judgement_verifier"),
            ("zero_base_review", "zero_base_review_verifier"),
            ("conviction_call", "conviction_call_verifier"),
            ("investment_memo", "investment_memo_verifier"),
            ("document_extraction", "claim_support_verifier"),
            ("registered_annual_report_draft", "registered_annual_report_verifier"),
        ]:
            self.assertIn(expected, pairs)
        # ADR-0010: the retired thesis-impact pair takes part on neither side.
        self.assertFalse(any("thesis_impact" in part for pair in pairs for part in pair))


class ConflictDetectionTests(unittest.TestCase):
    def evaluate(self, draft, verifier=VERIFIERS, cooldowns=None):
        return evaluate_pair(draft_chain=draft, verifier_chain=verifier, profiles=PROFILES,
                             cooldowns=cooldowns or {}, now=NOW)

    def test_a_draft_in_the_verifiers_family_is_a_named_conflict(self) -> None:
        result = self.evaluate(LIVE_CHEAP)
        self.assertEqual(result["status"], STATUS_CONFLICT)
        self.assertEqual(result["culprit"], GOOGLE_DRAFT)
        self.assertEqual(status_label(result),
                         "冲突：gemini-3-8-flash-antigravity 与核验同家族（google-gemini-3）")
        self.assertTrue(result["draft_links"][0]["conflict"])
        self.assertFalse(result["draft_links"][1]["conflict"])

    def test_another_family_first_is_independent_and_its_fallback_is_named(self) -> None:
        result = self.evaluate([DEEPSEEK, GOOGLE_DRAFT, GLM])
        self.assertEqual(result["status"], STATUS_INDEPENDENT)
        self.assertEqual(status_label(result), "独立")
        self.assertEqual(result["fallback_conflicts"], [GOOGLE_DRAFT])

    def test_a_cooling_first_link_that_drops_the_draft_onto_the_family_is_cooldown(self) -> None:
        result = self.evaluate([DEEPSEEK, GOOGLE_DRAFT, GLM],
                               cooldowns={DEEPSEEK: {"until": "2026-09-09T09:00:00+00:00"}})
        self.assertEqual(result["status"], STATUS_COOLDOWN)
        self.assertEqual(result["culprit"], GOOGLE_DRAFT)
        self.assertEqual(result["nominal_producer"], DEEPSEEK)
        self.assertIn("冷却中：deepseek-v4-flash 正在冷却", status_label(result))
        self.assertEqual(result["draft_links"][0]["cooldown_until"], "2026-09-09T09:00:00+00:00")

    def test_the_only_independent_verifier_cooling_is_cooldown_too(self) -> None:
        verifier = ["profile:claude-verifier", *VERIFIERS]
        self.assertEqual(self.evaluate([GOOGLE_DRAFT], verifier)["status"], STATUS_INDEPENDENT)
        cooled = self.evaluate([GOOGLE_DRAFT], verifier,
                               cooldowns={"profile:claude-verifier": {}})
        self.assertEqual(cooled["status"], STATUS_COOLDOWN)
        self.assertIn("能独立核验", status_label(cooled))

    def test_a_verifier_chain_with_no_controlled_link_is_its_own_conflict(self) -> None:
        result = self.evaluate([DEEPSEEK], [GLM])
        self.assertEqual(result["status"], STATUS_NO_VERIFIER)
        self.assertIn("没有能承担受控核验", status_label(result))


class RecommendationTests(unittest.TestCase):
    def recommend(self, chain, cooldowns=None, candidates=()):
        return recommend_order(chain, purpose="research_localization",
                               verifier_chains=[VERIFIERS], profiles=PROFILES,
                               cooldowns=cooldowns or {}, now=NOW, candidates=candidates)

    def test_the_first_independent_link_moves_up_and_nothing_else_moves(self) -> None:
        result = self.recommend(LIVE_CHEAP)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["chain"], [DEEPSEEK, GOOGLE_DRAFT, HAIKU, GLM])
        self.assertEqual(result["moved"], DEEPSEEK)

    def test_a_cooling_candidate_is_passed_over(self) -> None:
        result = self.recommend(LIVE_CHEAP, cooldowns={DEEPSEEK: {}})
        self.assertEqual(result["chain"], [HAIKU, GOOGLE_DRAFT, DEEPSEEK, GLM])

    def test_a_chain_already_independent_is_left_alone(self) -> None:
        result = self.recommend([DEEPSEEK, GOOGLE_DRAFT])
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["chain"], [DEEPSEEK, GOOGLE_DRAFT])

    def test_with_no_good_link_the_cheapest_catalog_model_goes_in_front(self) -> None:
        result = self.recommend([GOOGLE_DRAFT], candidates=[GLM, DEEPSEEK, HAIKU, VERIFIERS[0]])
        self.assertEqual(result["source"], "catalog")
        self.assertEqual(result["chain"], [HAIKU, GOOGLE_DRAFT])
        self.assertEqual(self.recommend([GOOGLE_DRAFT])["status"], "none")


def _envelope_db(path: Path, rows: list[tuple[str, str, dict, str]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE scheduler_result_envelopes (result_envelope_id TEXT PRIMARY KEY, "
        "work_order_id TEXT, attempt_number INTEGER, result_envelope_hash TEXT, "
        "result_envelope_json TEXT, outcome TEXT, content_hash TEXT, created_at TEXT)")
    for envelope_id, work, envelope, created_at in rows:
        connection.execute(
            "INSERT INTO scheduler_result_envelopes VALUES (?,?,?,?,?,?,?,?)",
            (envelope_id, work, 1, "h", json.dumps(envelope), "failed", "h", created_at))
    connection.commit()
    connection.close()


class RefusalCountTests(unittest.TestCase):
    def test_recorded_and_pre_batch_w_refusals_are_counted_by_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            recent = (NOW - timedelta(hours=1)).isoformat()
            old = (NOW - timedelta(hours=50)).isoformat()
            refused = {"error": {"code": "MODEL_ROUTE_REJECTED"}, "metadata": {
                "control_plane_failure": True, "route_decision_ref": None, "chain_failures": [],
                "chain_refusal": {"reason": "verifier_not_independent",
                                  "purpose": "research_localization_verifier"}}}
            legacy = {"error": {"code": "MODEL_ROUTE_REJECTED"}, "metadata": {
                "control_plane_failure": True, "route_decision_ref": None, "chain_failures": []}}
            routed = {"error": {"code": "MODEL_ROUTE_REJECTED"}, "metadata": {
                "control_plane_failure": True, "route_decision_ref": "route:1",
                "chain_failures": []}}
            work = "work:cockpit-research_localization_verifier-ced2a6263f4f8d8ae7ce2d84958922e7"
            _envelope_db(state / "scheduler.sqlite", [
                ("r1", work, refused, recent), ("r2", work, legacy, recent),
                ("r3", work, routed, recent), ("r4", work, refused, old),
                ("r5", "work:cockpit-dossier_verifier-0123456789abcdef0123", legacy, recent),
            ])
            counts = refusal_counts(state, now=NOW)
        self.assertEqual(counts["research_localization_verifier"],
                         {"recorded": 1, "unrecorded": 1})
        self.assertEqual(counts["dossier_verifier"], {"recorded": 0, "unrecorded": 1})


class NewWorkspaceCanaryTests(RouterCase):
    """A fresh workspace: conflict reported, card save via the writer, then independent."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.root
        # The live shape of 2026-10-07: the cheap tier puts the Antigravity
        # Gemini first; the localization verifier is pinned to the two direct
        # Gemini profiles, the only ones with provider-controlled verification.
        draft_ref = ensure_planner_policy(
            self.router, tier="cheap", now=NOW,
            policy_id="model-routing-policy:canary-localization")["policy_version_ref"]
        draft_ref = publish_tier_selection(
            self.router, policy_version_ref=draft_ref, tier="cheap", mode="explicit",
            chain=[GOOGLE_DRAFT, DEEPSEEK, GLM], now=NOW)["policy_version_ref"]
        verifier_ref = ensure_planner_policy(
            self.router, tier="verifier", now=NOW,
            policy_id="model-routing-policy:canary-localization-verifier")["policy_version_ref"]
        verifier_ref = publish_selection(
            self.router, policy_version_ref=verifier_ref,
            purpose="research_localization_verifier", mode="explicit", chain=VERIFIERS,
            now=NOW)["policy_version_ref"]
        for name, ref, chain in (
            ("research-localization-draft-model-config.json", draft_ref,
             [GOOGLE_DRAFT, DEEPSEEK, GLM]),
            ("research-localization-verifier-model-config.json", verifier_ref, VERIFIERS),
        ):
            (self.state / name).write_text(json.dumps({
                "routing_policy_ref": ref,
                "credential_slot_refs": credential_slots_for(self.router, chain),
                "model_router_db": str(self.root / "model-router.sqlite"),
            }), encoding="utf-8")

    def pair(self, report):
        return next(row for row in report["pairs"]
                    if row["draft_purpose"] == "research_localization")

    def plane(self):
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane
        from dalton_core.writer_server import WriterServer

        writer = object.__new__(WriterServer)
        # The writer's state directory is its Core database's directory.
        writer.db_path = self.state / "core.sqlite"
        self.writer_calls: list[tuple[str, dict]] = []

        def governance(token_config, socket, *, actor_ref, operation, params):
            self.writer_calls.append((operation, dict(params)))
            return getattr(writer, f"_op_{operation}")({**params, "actor_ref": actor_ref})

        (self.root / "run").mkdir(exist_ok=True)
        (self.root / "run" / "heartbeat.json").write_text("{}", encoding="utf-8")
        (self.root / "core.sqlite").write_bytes(b"")
        plane = CockpitPlane(
            CockpitConfig.from_mapping({
                "core_db": str(self.root / "core.sqlite"), "state_dir": str(self.state),
                "heartbeat_path": str(self.root / "run" / "heartbeat.json"),
                "scheduler_db": str(self.root / "scheduler.sqlite"),
                "journal_path": str(self.root / "journal.sqlite"),
            }),
            writer_socket=self.root / "w.sock", token_config=self.root / "t.json",
            governance_call=governance, clock=lambda: NOW)
        self.addCleanup(plane.close)
        return plane

    def test_conflict_is_reported_then_the_cards_save_makes_it_independent(self) -> None:
        from dalton_core.workspace_health_parity import (
            Environment as ParityEnvironment, check_verifier_independence,
        )

        before = self.pair(environment_report(self.state, now=NOW))
        self.assertEqual(before["status"], STATUS_CONFLICT)
        self.assertEqual(before["evaluation"]["culprit"], GOOGLE_DRAFT)
        parity = ParityEnvironment("canary", self.state)
        self.addCleanup(parity.close)
        (row,) = check_verifier_independence(parity, now=NOW)
        self.assertEqual(row["status"], "warn")
        self.assertIn("research_localization -> research_localization_verifier", row["detail"])
        self.assertIn("核验独立性", row["fix"])

        plane = self.plane()
        card = plane.verifier_independence()
        self.assertTrue(card["available"])
        entry = next(item for item in card["rows"]
                     if item["draft_purpose"] == "research_localization")
        self.assertTrue(entry["in_conflict"])
        local = entry["pairs"][0]["environments"][0]
        self.assertTrue(local["local"])
        self.assertIn("与核验同家族", local["status_label"])
        editor = entry["editor"]
        self.assertEqual(editor["chain"], [GOOGLE_DRAFT, DEEPSEEK, GLM])
        self.assertEqual(editor["recommended"]["chain"], [DEEPSEEK, GOOGLE_DRAFT, GLM])
        self.assertEqual(editor["recommended"]["status_after"], ["独立"])
        self.assertIsNotNone(editor["cost"]["per_call_usd"])
        self.assertTrue(editor["tier_chain"])
        self.assertTrue(next(c for c in editor["choices"] if c["model"] == GOOGLE_DRAFT)["conflict"])

        result = plane.select_model("owner@example.com", {
            "purpose": "research_localization", "mode": "explicit",
            "chain": editor["recommended"]["chain"], "reason": "核验独立性：起草改用 DeepSeek"})
        self.assertEqual(self.writer_calls[0][0], "set_model_selection")
        self.assertNotIn("reason", self.writer_calls[0][1])
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["reason"], "核验独立性：起草改用 DeepSeek")
        self.assertIn("research-localization-draft-model-config.json",
                      result["model_configs_repointed"])
        events = plane.journal.connection.execute(
            "SELECT detail FROM cockpit_events WHERE kind='model_selection'").fetchall()
        self.assertIn("理由：核验独立性：起草改用 DeepSeek", events[-1][0])

        after = self.pair(environment_report(self.state, now=NOW))
        self.assertEqual(after["status"], STATUS_INDEPENDENT)
        self.assertEqual(after["draft_mode"], "explicit")
        self.assertEqual(after["draft_chain"], [DEEPSEEK, GOOGLE_DRAFT, GLM])
        (row,) = check_verifier_independence(parity, now=NOW)
        self.assertEqual(row["status"], "info")

        # 恢复系统推荐（按档位）: the stage follows its tier again.
        plane.select_model("owner@example.com", {
            "purpose": "research_localization", "mode": "tier", "reason": "恢复"})
        restored = self.pair(environment_report(self.state, now=NOW))
        self.assertEqual(restored["draft_mode"], "tier")
        self.assertEqual(restored["status"], STATUS_CONFLICT)


def _js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth, index = 0, source.index("{", start)
    while True:
        char = source[index]
        depth += char == "{"
        depth -= char == "}"
        index += 1
        if depth == 0:
            return source[start:index]


class PageTests(unittest.TestCase):
    def test_the_card_is_on_the_model_page_and_saves_through_model_select(self) -> None:
        page = HTML.read_text(encoding="utf-8")
        self.assertIn('getJson("/v1/cockpit/verifier_independence")', page)
        self.assertIn("核验独立性 · 起草模型顺序", page)
        self.assertIn("sheet.append(verifierIndependenceSection(independence))", page)
        for label in ("推荐顺序", "恢复系统推荐（按档位）", "起草链", "核验链", "单次"):
            self.assertIn(label, page)
        self.assertIn('{purpose:row.draft_purpose,mode:"explicit",chain:[...picked],reason:why}', page)
        self.assertIn('{purpose:row.draft_purpose,mode:"tier",reason:why}', page)
        self.assertIn("verifierSyncLines(out)", page)
        # Both editors move links through the shared pure function.
        self.assertEqual(page.count("chainDropIndex(ev,dragToken,dragIndex)"), 2)
        self.assertEqual(page.count("item.ondragend=()=>{dragIndex=null}"), 2)
        self.assertNotIn("const [moved]=picked.splice(dragIndex,1)", page)

    def test_a_stale_or_foreign_drop_moves_nothing(self) -> None:
        fallback = Path.home() / ".openclaw" / "tools" / "node" / "bin" / "node"
        node = shutil.which("node") or (str(fallback) if fallback.is_file() else None)
        if node is None:
            self.skipTest("node is unavailable")
        page = HTML.read_text(encoding="utf-8")
        script = "\n".join([
            _js_function(page, "chainMoved"),
            _js_function(page, "chainDropIndex"),
            """
            const assert=(c,m)=>{if(!c){console.error(m);process.exit(1)}};
            const ev=t=>({dataTransfer:{getData:()=>t}});
            const chain=["deepseek","gemini","glm"];
            // A drag that started here and ends here moves the link.
            assert(JSON.stringify(chainMoved(chain,chainDropIndex(ev("tok"),"tok",1),0))
                   ==='["gemini","deepseek","glm"]',"own drag");
            // A cancelled drag left its index behind; a text or picker drop
            // (no token) or another editor's link (other token) is no move.
            assert(chainMoved(chain,chainDropIndex(ev(""),"tok",1),0)===null,"foreign text drop");
            assert(chainMoved(chain,chainDropIndex(ev("other"),"tok",1),0)===null,"other editor");
            assert(chainMoved(chain,chainDropIndex(ev("tok"),"tok",null),0)===null,"no drag");
            assert(chainMoved(chain,0,5)===null&&chainMoved(chain,1,1)===null,"out of range");
            assert(JSON.stringify(chain)==='["deepseek","gemini","glm"]',"input untouched");
            """,
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "drag.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True, text=True,
                                    check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class TierCardVariantTests(RouterCase):
    """Audit: the tier editor opens on one stage's pin; disagreement is said."""

    def test_tier_stages_on_different_orders_are_named_on_the_card(self) -> None:
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane

        openclaw = self.root / "openclaw.json"
        openclaw.write_text(json.dumps(self.catalog_config()), encoding="utf-8")
        refs = {}
        for name, chain in (("claim-index-model-config.json", [DEEPSEEK, GOOGLE_DRAFT]),
                            ("document-extraction-model-config.json", [GOOGLE_DRAFT, DEEPSEEK])):
            ref = ensure_planner_policy(
                self.router, tier="cheap", now=NOW,
                policy_id=f"model-routing-policy:variant-{name.split('-')[0]}")["policy_version_ref"]
            refs[name] = publish_tier_selection(
                self.router, policy_version_ref=ref, tier="cheap", mode="explicit",
                chain=chain, now=NOW)["policy_version_ref"]
            (self.root / name).write_text(json.dumps({
                "routing_policy_ref": refs[name],
                "credential_slot_refs": credential_slots_for(self.router, chain),
                "model_router_db": str(self.root / "model-router.sqlite")}), encoding="utf-8")
        planner = self.root / "research-planner-model-config.json"
        planner.write_text(json.dumps({
            "routing_policy_ref": self.policies["brain"],
            "model_router_db": str(self.root / "model-router.sqlite")}), encoding="utf-8")
        (self.root / "run").mkdir(exist_ok=True)
        (self.root / "run" / "heartbeat.json").write_text("{}", encoding="utf-8")
        (self.root / "core.sqlite").write_bytes(b"")
        plane = CockpitPlane(CockpitConfig.from_mapping({
            "core_db": str(self.root / "core.sqlite"), "state_dir": str(self.root),
            "heartbeat_path": str(self.root / "run" / "heartbeat.json"),
            "scheduler_db": str(self.root / "scheduler.sqlite"),
            "journal_path": str(self.root / "journal.sqlite"),
            "openclaw_config_path": str(openclaw), "model_config_path": str(planner),
        }), writer_socket=self.root / "w.sock", token_config=self.root / "t.json",
            governance_call=lambda *a, **k: {}, clock=lambda: NOW)
        self.addCleanup(plane.close)
        view = plane.models()
        cheap = next(card for card in view["tier_cards"] if card["tier"] == "cheap")
        self.assertIn("2 种不同的当前顺序", cheap["chain_variants_note"])
        self.assertIn("建立研究结论索引", cheap["chain_variants_note"])
        self.assertEqual([link["model"] for link in cheap["chain"]], [DEEPSEEK, GOOGLE_DRAFT])
        brain = next(card for card in view["tier_cards"] if card["tier"] == "brain")
        self.assertNotIn("chain_variants_note", brain)
        page = HTML.read_text(encoding="utf-8")
        self.assertIn("card.chain_variants_note", page)


if __name__ == "__main__":
    unittest.main()
