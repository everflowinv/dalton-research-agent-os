"""P10b: a wrong Claim is challenged and retired without the Ledger being edited."""

from __future__ import annotations

import hashlib
import json
import unittest

from dalton_core.claim_retirement import (
    ClaimRetirementAuthority,
    ClaimRetirementConflict,
    ClaimRetirementValidationError,
    detect,
    subject_absent_from_source,
)
from dalton_core.claim_review import (
    ClaimReviewDriver,
    needles_from_plans,
    needles_from_search_terms,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_stage import retired_claim_refs
from dalton_core.store import DaltonStore, content_hash
from pathlib import Path
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

EPAM = "company:sec-cik:0001352010"
ACN = "company:sec-cik:0001467373"
AUTOMATION = "automation:coverage-mission"
OWNER = "human:lumos"
OFF_TOPIC = "Operator: welcome to the Orion lighting call. Management described LED and EV charging demand."
ON_TOPIC = "Operator: welcome to the EPAM Systems call. Management described engineering demand."


class _Spool:
    """The raw spool's read side, by content hash."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads = 0

    def read_object(self, content_hash_value: str) -> bytes:
        self.reads += 1
        if content_hash_value not in self.objects:
            raise KeyError("raw object not found")
        return self.objects[content_hash_value]


class ClaimRetirementHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        self.params = mission_params(self.state)
        ref = self.params.pop("mission_ref")
        self.mission_ref = ref
        self.mission = self.missions.create_mission(ref, **self.params)
        # The citation chain lives in the transcript correction schema; a fixture
        # that builds the chain by hand still creates it the way its own
        # authority does, guards included.
        import dalton_core
        self.store.connection.executescript(
            (Path(dalton_core.__file__).parent / "transcript_correction_schema.sql").read_text(encoding="utf-8")
        )
        self.authority = ClaimRetirementAuthority(self.store)
        self.objects: dict[str, bytes] = {}
        self.spool = _Spool(self.objects)
        self._seq = 0

    def grant_claim_challenge(self) -> None:
        params = dict(self.params)
        params["autonomy"] = {**params["autonomy"],
                              "may_write": list(params["autonomy"]["may_write"]) + ["claim_challenge"]}
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.missions.create_mission(self.mission_ref, **params)

    def claim(self, *, subject: str = EPAM, statement: str = "Management described LED demand.",
              source: str | None = OFF_TOPIC, kind: str = "qualitative", value=None) -> dict[str, str]:
        """One Claim with the citation chain that binds it to an exact original."""

        self._seq += 1
        n = self._seq
        claim = {
            "schema_version": "0.2", "id": f"claim-version:{n:064d}", "claim_ref": f"claim:test:{n}",
            "version": 1, "subject_ref": subject, "metric_or_aspect": "aspect:test", "period": "2026Q2",
            "basis": "fixture", "normalized_statement": statement, "claim_kind": kind, "value": value,
            "unit": None, "currency": None, "scale": None, "producer_execution_refs": [],
            "semantic_review_ref": None, "semantic_review_hash": None, "candidate_origin_ref": None,
            "candidate_origin_hash": None, "actor_ref": "system:research-auto-commit",
            "prior_version_ref": None, "created_at": f"2026-09-0{1 + n % 9}T00:00:00+00:00",
        }
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        digest = None
        if source is not None:
            body = source.encode("utf-8")
            digest = hashlib.sha256(body).hexdigest()
            self.objects[digest] = body
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1, json.dumps(claim, sort_keys=True),
                 claim["content_hash"], claim["created_at"]),
            )
            if digest is not None:
                binding = f"transcript-claim-citation-binding:{n:032d}"
                correction = f"transcript-correction-set-version:{n:032d}"
                evidence_id = f"evidence-version:{n:064d}"
                evidence = {"id": evidence_id, "artifact_refs": [{"ref": binding, "hash": "0" * 64}]}
                cur.execute(
                    "INSERT INTO evidence_versions(evidence_version_id,evidence_ref,version_number,evidence_json,"
                    "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (evidence_id, f"evidence:test:{n}", 1, json.dumps(evidence, sort_keys=True),
                     content_hash(evidence), claim["created_at"]),
                )
                cur.execute(
                    "INSERT INTO evidence_relations(relation_id,evidence_ref,evidence_version_id,claim_ref,"
                    "claim_version_id,relation,relation_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"relation:test:{n}", f"evidence:test:{n}", evidence_id, claim["claim_ref"],
                     claim["id"], "supports", "{}", "0" * 64, claim["created_at"]),
                )
                cur.execute(
                    "INSERT INTO transcript_correction_set_versions(version_id,correction_set_ref,version_number,"
                    "source_manifest_ref,source_manifest_hash,source_content_hash,record_json,content_hash,"
                    "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (correction, f"transcript-correction-set:test:{n}", 1,
                     f"manifest:{n}", "0" * 64, digest,
                     json.dumps({"id": correction, "source_content_hash": digest,
                                 "document_ref": f"alphaengine-doc:{n}"}, sort_keys=True),
                     "0" * 64, AUTOMATION, claim["created_at"]),
                )
                cur.execute(
                    "INSERT INTO transcript_claim_citation_bindings(binding_id,correction_set_version_ref,"
                    "source_manifest_ref,source_manifest_hash,source_content_hash,source_start,source_end,"
                    "claim_eligible,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (binding, correction, f"manifest:{n}", "0" * 64, digest, 0, 10, 1,
                     json.dumps({"id": binding, "correction_set_version_ref": correction}, sort_keys=True),
                     "0" * 64, claim["created_at"]),
                )
        return {"ref": claim["id"], "hash": claim["content_hash"], "source_sha256": digest or ""}

    def driver(self) -> ClaimReviewDriver:
        return ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority, spool=self.spool,
            needles={EPAM: ["epam"], ACN: ["accenture", "acn"]},
        )


class DetectorTests(unittest.TestCase):
    def test_one_mention_anywhere_clears_the_claim(self) -> None:
        self.assertTrue(subject_absent_from_source(OFF_TOPIC, ["epam"]))
        self.assertFalse(subject_absent_from_source(ON_TOPIC, ["epam"]))
        self.assertFalse(subject_absent_from_source("nothing here", []))  # no needles: never fires

    def test_needles_drop_industry_words_that_would_match_anything(self) -> None:
        self.assertEqual(needles_from_search_terms("DXC Technology DXC"), ["dxc"])
        self.assertEqual(needles_from_search_terms("IBM International Business Machines"), ["ibm"])
        self.assertEqual(needles_from_search_terms("Accenture ACN"), ["accenture", "acn"])
        self.assertEqual(
            needles_from_plans([{"companies": {ACN: {"search_terms": "Accenture ACN"}}}]),
            {ACN: ["accenture", "acn"]},
        )

    def test_boilerplate_is_detected_before_the_subject_check(self) -> None:
        hit = detect(
            statement="J.P. Morgan states that past performance is not indicative of future results.",
            source_text=ON_TOPIC, needles=["epam"],
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "boilerplate_disclaimer")
        self.assertIsNone(detect(statement="Management sees stronger demand.", source_text=ON_TOPIC, needles=["epam"]))


class AuthorityTests(ClaimRetirementHarness):
    def test_a_challenge_binds_the_exact_claim_version_and_is_append_only(self) -> None:
        claim = self.claim()
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.challenge(
                claim_version_ref=claim["ref"], claim_version_hash="0" * 64,
                reason_code="subject_absent_from_source", rationale="wrong hash", actor_ref=AUTOMATION)
        record = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="subject_absent_from_source", rationale="原文里没有 epam", actor_ref=AUTOMATION)
        self.assertEqual(record["status"], "fresh")
        self.assertEqual(record["subject_ref"], EPAM)
        self.assertEqual(record["detector_ref"], "claim-detector:subject-absent-from-source:v1")
        again = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="subject_absent_from_source", rationale="再来一次", actor_ref=AUTOMATION)
        self.assertEqual(again["status"], "duplicate")
        with self.assertRaises(ClaimRetirementValidationError):
            self.authority.challenge(
                claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
                reason_code="invented", rationale="x", actor_ref=OWNER)

    def test_automation_may_not_judge_and_may_not_keep(self) -> None:
        claim = self.claim()
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.challenge(
                claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
                reason_code="human_judgment", rationale="I think so", actor_ref=AUTOMATION)
        record = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="subject_absent_from_source", rationale="原文里没有 epam", actor_ref=AUTOMATION)
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(
                challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="kept",
                actor_ref=AUTOMATION, rationale="keep it")

    def test_automation_reruns_the_detector_and_refuses_when_it_no_longer_fires(self) -> None:
        claim = self.claim()
        record = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="subject_absent_from_source", rationale="原文里没有 epam", actor_ref=AUTOMATION)
        # The caller says the check fired; the authority checks the bytes itself.
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(
                challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="retired",
                actor_ref=AUTOMATION, rationale="retire", subject_needles=["epam"], source_text=ON_TOPIC)
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(
                challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="retired",
                actor_ref=AUTOMATION, rationale="retire", subject_needles=["epam"], source_text=None)
        decision = self.authority.decide(
            challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="retired",
            actor_ref=AUTOMATION, rationale="retire", subject_needles=["epam"], source_text=OFF_TOPIC)
        self.assertEqual((decision["status"], decision["decision"]), ("fresh", "retired"))
        self.assertEqual(self.authority.retired_claim_version_refs(), {claim["ref"]})
        # The Claim itself is untouched: the Ledger row and its hash still verify.
        row = self.store.connection.execute(
            "SELECT claim_json, content_hash FROM claim_versions WHERE claim_version_id=?", (claim["ref"],)
        ).fetchone()
        self.assertEqual(json.loads(row["claim_json"])["content_hash"], row["content_hash"])
        self.assertEqual(json.loads(row["claim_json"])["normalized_statement"], "Management described LED demand.")

    def test_a_person_may_keep_a_challenged_claim_and_it_does_not_come_back(self) -> None:
        claim = self.claim()
        record = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="human_judgment", rationale="looks wrong to me", actor_ref=OWNER)
        kept = self.authority.decide(
            challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="kept",
            actor_ref=OWNER, rationale="it is fine")
        self.assertEqual(kept["decision"], "kept")
        self.assertEqual(self.authority.retired_claim_version_refs(), set())
        self.assertEqual(self.authority.challenges(open_only=True), [])


class LaneTests(ClaimRetirementHarness):
    def test_without_the_grant_the_lane_reports_what_it_found_and_writes_nothing(self) -> None:
        wrong = self.claim()
        self.claim(statement="Management sees stronger engineering demand.", source=ON_TOPIC)
        result = self.driver().run_once()
        self.assertEqual(result["status"], "held")
        self.assertEqual([item["claim_version_ref"] for item in result["detected"]], [wrong["ref"]])
        self.assertEqual((result["challenged"], result["retired"]), ([], []))
        self.assertIn("claim_challenge", result["grant"]["hold"])
        self.assertEqual(self.authority.challenges(), [])

    def test_under_the_grant_the_lane_challenges_and_retires_exactly_the_wrong_claims(self) -> None:
        wrong = self.claim()
        disclaimer = self.claim(
            statement="J.P. Morgan states that past performance is not indicative of future results.",
            source=ON_TOPIC)
        good = self.claim(statement="Management sees stronger engineering demand.", source=ON_TOPIC)
        unreadable = self.claim(statement="Management said something.", source=None)
        self.grant_claim_challenge()
        result = self.driver().run_once()
        self.assertEqual(result["status"], "acted")
        retired = {item["claim_version_ref"] for item in result["retired"]}
        self.assertEqual(retired, {wrong["ref"], disclaimer["ref"]})
        self.assertEqual(result["unreadable"], 1)
        self.assertNotIn(good["ref"], retired)
        self.assertNotIn(unreadable["ref"], retired)
        self.assertEqual(self.authority.retired_claim_version_refs(), retired)
        # Re-running writes nothing and reports nothing new.
        again = self.driver().run_once()
        self.assertEqual((again["status"], again["challenged"], again["retired"]), ("idle", [], []))
        # Read paths skip the retired versions.
        self.assertEqual(retired_claim_refs(self.store.connection), retired)

    def test_the_document_budget_bounds_the_io_and_defers_the_rest(self) -> None:
        for index in range(4):
            self.claim(source=f"{OFF_TOPIC} Session {index}.")  # four distinct originals
        self.grant_claim_challenge()
        result = self.driver().run_once(max_documents=2)
        self.assertEqual((result["documents_read"], result["deferred"]), (2, 2))
        self.assertEqual(len(result["retired"]), 2)
        rest = self.driver().run_once(max_documents=2)
        self.assertEqual(len(rest["retired"]), 2)
        self.assertEqual(len(self.authority.retired_claim_version_refs()), 4)

    def test_a_quantitative_claim_is_never_touched_by_these_detectors(self) -> None:
        self.claim(kind="quantitative", value=1.0, statement="Revenue rose.", source=OFF_TOPIC)
        self.grant_claim_challenge()
        result = self.driver().run_once()
        self.assertEqual((result["scanned"], result["retired"]), (0, []))


if __name__ == "__main__":
    unittest.main()
