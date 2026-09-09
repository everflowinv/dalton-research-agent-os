"""P12c: the debate map contract, the constitution gate and the readers."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.debate_map import (
    DEBATE_POLICY,
    GATE_REASONS,
    POLICY_HASH,
    DebateMapAuthority,
    DebateMapConflict,
    DebateMapNotFound,
    DebateMapValidationError,
    contested_aspects,
    count_independent_sources,
    evidence_fingerprint,
    index_claims,
    map_ref_for,
    novelty,
    polarity,
    publisher_of,
    screen_candidate,
    screen_candidates,
    source_identity,
    tier_of,
    validate_version,
)
from dalton_core.store import DaltonStore

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "deploy/phase9/p12c-debate-policy-v1.json"

SUBJECT = "company:sec-cik:0001467373"
METHOD = {
    "question_admission": ["A question must change a thesis or a driver view."],
    "causal_chain": ["Bookings lead revenue by two to four quarters.",
                     "Utilisation leads margin by one quarter."],
}
DRIVERS = ["driver:bookings", "driver:utilisation"]


def position(statement: str, refs) -> dict:
    return {"statement": statement, "claim_refs": list(refs)}


def debate(
    ref: str = "debate:bookings",
    *,
    status: str = "open",
    bull_refs=("cv-a",),
    bear_refs=("cv-b",),
    shift=None,
    market=None,
    ours=None,
    question: str = "Will bookings growth hold above ten per cent?",
    drivers=("driver:bookings",),
    first_seen_at: str = "2026-09-09T00:00:00+00:00",
    admission_index: int = 0,
    causal_link_index: int = 0,
) -> dict:
    return {
        "debate_ref": ref,
        "question": question,
        "driver_refs": list(drivers),
        "admission_index": admission_index,
        "causal_link_index": causal_link_index,
        "bull_position": position("Bookings are accelerating.", bull_refs),
        "bear_position": position("Bookings are decelerating.", bear_refs),
        "market_position": market or {
            "available": True, "lean": "bear",
            "statement": "Four of five brokers model deceleration.",
            "refs": list(bear_refs),
        },
        "our_position": ours or {
            "state": "held", "side": "bull",
            "statement": "We take the other side.", "refs": list(bull_refs),
        },
        "status": status,
        "last_shift_reason": shift,
        "first_seen_at": first_seen_at,
        "source_independence": {"bull_sources": 2, "bear_sources": 2},
    }


# Every debate this module builds is already contract-valid; the alias is kept
# because the tests read better when the name says so.
valid_debate = debate


class DebatePolicyTests(unittest.TestCase):
    def test_the_published_policy_file_is_the_policy_the_code_runs(self) -> None:
        published = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(published.pop("content_hash"), POLICY_HASH)
        self.assertEqual(published, dict(DEBATE_POLICY))

    def test_a_longer_publisher_name_wins_over_a_substring(self) -> None:
        self.assertEqual(publisher_of("Morgan Stanley — ACN initiation"), "morgan-stanley")
        self.assertEqual(publisher_of("JPMorgan raises ACN target"), "jpmorgan")
        self.assertIsNone(publisher_of("A newspaper column about Accenture"))


class SourceIndependenceTests(unittest.TestCase):
    def row(self, ref: str, **kwargs) -> dict:
        return {"claim_version_ref": ref, "subject_ref": SUBJECT, **kwargs}

    def test_two_notes_from_the_same_broker_are_one_source(self) -> None:
        claims = index_claims([
            self.row("cv-1", importance="sell_side",
                     document_title="TD Cowen: Accenture Q4 review"),
            self.row("cv-2", importance="sell_side",
                     document_title="TD Cowen: Accenture model update"),
            self.row("cv-3", importance="sell_side",
                     document_title="Wolfe Research: Accenture bookings"),
        ])
        both = count_independent_sources(["cv-1", "cv-2"], claims)
        self.assertEqual(both["count"], 1)
        self.assertEqual(both["keys"], ["publisher:td"])
        three = count_independent_sources(["cv-1", "cv-2", "cv-3"], claims)
        self.assertEqual(three["count"], 2)

    def test_the_issuer_is_one_voice_however_many_filings_it_makes(self) -> None:
        claims = index_claims([
            self.row("cv-1", importance="filing"),
            self.row("cv-2", importance="management_statement"),
        ])
        counted = count_independent_sources(["cv-1", "cv-2"], claims)
        self.assertEqual(counted["count"], 1)
        self.assertEqual(counted["keys"], [f"issuer:{SUBJECT}"])

    def test_an_unattributed_claim_is_evidence_but_never_a_source(self) -> None:
        claims = index_claims([self.row("cv-1", importance="news")])
        counted = count_independent_sources(["cv-1"], claims)
        self.assertEqual(counted["count"], 0)
        self.assertEqual(counted["unattributed"], 1)
        self.assertEqual(source_identity(claims["cv-1"])["basis"], "unattributed")

    def test_two_web_hosts_are_two_sources(self) -> None:
        claims = index_claims([
            self.row("cv-1", importance="news",
                     document_ref="https://reuters.com/a"),
            self.row("cv-2", importance="news",
                     document_ref="https://ft.com/b"),
        ])
        self.assertEqual(count_independent_sources(["cv-1", "cv-2"], claims)["count"], 2)

    def test_a_claim_known_only_by_its_document_does_not_count(self) -> None:
        # Live, this is 100% of the sell-side evidence: no title reaches the
        # Ledger, so no publisher matches and the issuer rung does not apply.
        # If ``document`` counted, two notes out of one house would open a
        # debate, which is the failure the whole rule exists to prevent.
        claims = index_claims([
            self.row("cv-1", importance="sell_side", document_ref="alphaengine-doc:1"),
            self.row("cv-2", importance="sell_side", document_ref="alphaengine-doc:2"),
        ])
        self.assertEqual(claims["cv-1"]["basis"], "document")
        counted = count_independent_sources(["cv-1", "cv-2"], claims)
        self.assertEqual(counted["count"], 0)
        self.assertEqual(counted["unattributed"], 2)
        self.assertEqual(DEBATE_POLICY["counting_bases"],
                         ["publisher", "issuer", "host"])

    def test_an_upstream_attribution_is_taken_at_its_word(self) -> None:
        # The seam the extraction-throughput slice fills: a broker name read
        # off the document's own metadata beats any title matching, and two
        # notes from that house fold into one voice.
        claims = index_claims([
            self.row("cv-1", importance="sell_side", document_ref="alphaengine-doc:1",
                     document_publisher="TD Cowen"),
            self.row("cv-2", importance="sell_side", document_ref="alphaengine-doc:2",
                     document_publisher="td cowen"),
            self.row("cv-3", importance="sell_side", document_ref="alphaengine-doc:3",
                     document_publisher="Redburn Atlantic"),
        ])
        self.assertEqual(claims["cv-1"]["basis"], "publisher")
        self.assertEqual(claims["cv-1"]["key"], "publisher:td-cowen")
        self.assertEqual(count_independent_sources(["cv-1", "cv-2"], claims)["count"], 1)
        # A house the frozen table has never heard of is still a house.
        self.assertEqual(claims["cv-3"]["key"], "publisher:redburn-atlantic")
        self.assertEqual(
            count_independent_sources(["cv-1", "cv-2", "cv-3"], claims)["count"], 2)

    def test_document_metadata_is_matched_when_nothing_attributed_it(self) -> None:
        claims = index_claims([
            self.row("cv-1", importance="sell_side", document_ref="alphaengine-doc:1",
                     document_authors="Bryan Bergin, TD Cowen"),
            self.row("cv-2", importance="sell_side", document_ref="alphaengine-doc:2",
                     document_sources="TD Cowen Equity Research"),
        ])
        self.assertEqual(count_independent_sources(["cv-1", "cv-2"], claims)["count"], 1)
        self.assertEqual(claims["cv-1"]["publisher"], "td")

    def test_the_recorded_host_beats_the_document_ref(self) -> None:
        claims = index_claims([
            self.row("cv-1", importance="news", document_ref="public-web-url:sha256:a",
                     host="reuters.com"),
            self.row("cv-2", importance="news", document_ref="public-web-url:sha256:b",
                     host="reuters.com"),
        ])
        self.assertEqual(claims["cv-1"]["basis"], "host")
        self.assertEqual(count_independent_sources(["cv-1", "cv-2"], claims)["count"], 1)

    def test_the_ledgers_own_independence_group_is_not_a_source_key(self) -> None:
        # Live it holds three values, one per connector; treating it as an
        # identity would make two unrelated newspapers one source.
        claims = index_claims([
            self.row("cv-1", importance="news",
                     independence_group="independence:source:public-web"),
        ])
        self.assertEqual(claims["cv-1"]["basis"], "unattributed")

    def test_the_discovery_spec_beats_the_importance_tier(self) -> None:
        row = self.row("cv-1", importance="management_statement",
                       spec_ref="sell-side-reports")
        self.assertEqual(tier_of(row), "sell_side")


class ContestedAspectTests(unittest.TestCase):
    def test_the_pre_pass_finds_an_aspect_the_evidence_disagrees_about(self) -> None:
        rows = [
            {"claim_version_ref": "cv-1", "subject_ref": SUBJECT,
             "index_aspect": "demand_drivers", "importance": "sell_side",
             "document_title": "TD Cowen note",
             "normalized_statement": "Demand is accelerating into next year."},
            {"claim_version_ref": "cv-2", "subject_ref": SUBJECT,
             "index_aspect": "demand_drivers", "importance": "sell_side",
             "document_title": "Wolfe Research note",
             "normalized_statement": "Discretionary demand is decelerating."},
            {"claim_version_ref": "cv-3", "subject_ref": SUBJECT,
             "index_aspect": "guidance_style", "importance": "sell_side",
             "document_title": "HSBC note",
             "normalized_statement": "Guidance looks strong."},
        ]
        found = contested_aspects(rows)
        self.assertEqual([item["aspect"] for item in found], ["demand_drivers"])
        self.assertEqual(found[0]["bull_claim_refs"], ["cv-1"])
        self.assertEqual(found[0]["bear_claim_refs"], ["cv-2"])
        self.assertFalse(found[0]["would_open"])

    def test_a_statement_that_argues_both_ways_is_neutral(self) -> None:
        self.assertEqual(polarity("Demand is accelerating but margin is weak"), "neutral")
        self.assertEqual(polarity("nothing notable here"), "neutral")
        self.assertEqual(polarity("bookings accelerating"), "bull")


def candidate(**kwargs) -> dict:
    wire = {
        "debate_ref": "new-1",
        "question": "Will bookings growth hold?",
        "driver_refs": ["driver:bookings"],
        "question_admission_index": 0,
        "causal_chain_index": 0,
        "bull": position("Bookings accelerating.", ["cv-1"]),
        "bear": position("Bookings decelerating.", ["cv-2"]),
        "market": {"available": False, "lean": None, "statement": None, "refs": []},
        "ours": {"state": "none_yet", "side": None, "statement": None, "refs": []},
        "gaining": "neither",
        "resolution": None,
    }
    wire.update(kwargs)
    return wire


class ConstitutionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claims = index_claims([
            {"claim_version_ref": "cv-1", "subject_ref": SUBJECT,
             "importance": "sell_side", "document_title": "TD Cowen note"},
            {"claim_version_ref": "cv-2", "subject_ref": SUBJECT,
             "importance": "sell_side", "document_title": "Wolfe Research note"},
            {"claim_version_ref": "cv-3", "subject_ref": SUBJECT,
             "importance": "sell_side", "document_title": "HSBC note"},
            {"claim_version_ref": "cv-4", "subject_ref": SUBJECT,
             "importance": "sell_side", "document_title": "RBC Capital note"},
        ])

    def screen(self, wire, **kwargs):
        return screen_candidate(
            wire, method=METHOD, driver_refs=DRIVERS, claims=self.claims, **kwargs
        )

    def test_every_gate_reason_has_a_candidate_that_triggers_it(self) -> None:
        cases = {
            "empty_question": candidate(question="   "),
            "empty_position": candidate(bull=position("  ", ["cv-1"])),
            "no_driver_binding": candidate(driver_refs=[]),
            "unknown_driver": candidate(driver_refs=["driver:nobody-has-heard-of"]),
            "no_bull_evidence": candidate(bull={"statement": "up", "claim_refs": []}),
            "no_bear_evidence": candidate(bear={"statement": "down", "claim_refs": []}),
            "unknown_claim_ref": candidate(bull=position("up", ["cv-not-shown"])),
            "no_question_admission_rule": candidate(question_admission_index=None),
            "unknown_question_admission_rule": candidate(question_admission_index=9),
            "no_causal_chain_link": candidate(causal_chain_index=None),
            "unknown_causal_chain_link": candidate(causal_chain_index=-1),
        }
        self.assertEqual(sorted(cases), sorted(GATE_REASONS))
        for reason, wire in cases.items():
            with self.subTest(reason=reason):
                verdict = self.screen(wire)
                self.assertFalse(verdict["admitted"])
                self.assertIn(reason, verdict["reasons"])
                self.assertIsNone(verdict["status"])

    def test_a_rejection_lists_its_reasons_in_the_frozen_order(self) -> None:
        verdict = self.screen(candidate(driver_refs=[], question="  "))
        self.assertEqual(verdict["reasons"], ["empty_question", "no_driver_binding"])

    def test_one_source_a_side_is_admitted_as_a_candidate_not_an_open_debate(self) -> None:
        verdict = self.screen(candidate())
        self.assertTrue(verdict["admitted"])
        self.assertEqual(verdict["status"], "candidate")
        self.assertEqual(verdict["source_independence"]["bull_sources"], 1)

    def test_two_independent_sources_a_side_opens_the_debate(self) -> None:
        verdict = self.screen(candidate(
            bull=position("up", ["cv-1", "cv-2"]),
            bear=position("down", ["cv-3", "cv-4"]),
        ))
        self.assertEqual(verdict["status"], "open")

    def test_a_gaining_side_only_shifts_a_debate_that_already_existed(self) -> None:
        wire = candidate(
            debate_ref="debate:known", gaining="bull",
            bull=position("up", ["cv-1", "cv-2"]),
            bear=position("down", ["cv-3", "cv-4"]),
        )
        self.assertEqual(self.screen(wire)["status"], "open")
        self.assertEqual(
            self.screen(wire, known_debate_refs=["debate:known"])["status"], "shifting"
        )

    def test_a_shift_standing_on_nothing_new_is_only_open(self) -> None:
        # "This argument has moved since you last read it" is an assertion
        # about time. If every reference the gaining side cites was already in
        # the previous version, nothing moved and the reader would be told it
        # had without being able to see what.
        wire = candidate(
            debate_ref="debate:known", gaining="bull",
            bull=position("up", ["cv-1", "cv-2"]),
            bear=position("down", ["cv-3", "cv-4"]),
        )
        self.assertEqual(
            self.screen(wire, known_debate_refs=["debate:known"],
                        prior_refs=["cv-1", "cv-2", "cv-3", "cv-4"])["status"],
            "open",
        )
        self.assertEqual(
            self.screen(wire, known_debate_refs=["debate:known"],
                        prior_refs=["cv-2", "cv-3", "cv-4"])["status"],
            "shifting",
        )

    def test_a_resolution_with_refs_resolves_and_one_without_does_not(self) -> None:
        grounded = {"bull": position("up", ["cv-1", "cv-2"]),
                    "bear": position("down", ["cv-3", "cv-4"])}
        with_refs = candidate(resolution={"reason": "the filing settled it",
                                          "refs": ["cv-3"]}, **grounded)
        self.assertEqual(self.screen(with_refs)["status"], "resolved")
        without = candidate(resolution={"reason": "we feel it is settled", "refs": []},
                            **grounded)
        self.assertEqual(self.screen(without)["status"], "open")

    def test_a_debate_nobody_independent_was_having_cannot_resolve(self) -> None:
        # One broker asked and one filing answered: never a debate, so there is
        # nothing to settle. It stays a candidate rather than entering the
        # record as a resolved argument.
        thin = candidate(resolution={"reason": "the filing settled it",
                                     "refs": ["cv-3"]})
        self.assertEqual(self.screen(thin)["status"], "candidate")

    def test_screening_keeps_the_refusals(self) -> None:
        screened = screen_candidates(
            [candidate(), candidate(debate_ref="new-2", driver_refs=[])],
            method=METHOD, driver_refs=DRIVERS, claims=self.claims,
            observed_at="2026-09-09T00:00:00+00:00",
        )
        self.assertEqual(len(screened["admitted"]), 1)
        self.assertEqual(len(screened["rejected"]), 1)
        self.assertEqual(screened["rejected"][0]["reasons"], ["no_driver_binding"])
        self.assertEqual(screened["rejected"][0]["candidate_ref"], "new-2")


class DebateMapContractTests(unittest.TestCase):
    def version(self, **kwargs) -> dict:
        from dalton_core.store import content_hash

        wire = {
            "schema_version": "0.1",
            "id": "debate-map-version:x",
            "created_at": "2026-09-09T00:00:00+00:00",
            "map_ref": map_ref_for(SUBJECT),
            "version": 1,
            "prior_version_ref": None,
            "subject_ref": SUBJECT,
            "subject_kind": "company",
            "change_reason": "evidence_thicker",
            "change_evidence_refs": ["cv-a"],
            "constitution_ref": "constitution-version:x:1",
            "constitution_hash": "a" * 64,
            "policy_ref": "debate-policy:p12c:v1",
            "policy_hash": POLICY_HASH,
            "evidence_fingerprint": evidence_fingerprint(["cv-a", "cv-b"]),
            "debates": [valid_debate()],
            "rejected_by_constitution": [],
            "drafted_by": None,
            "verified_by": None,
            "actor_ref": "automation:dalton",
        }
        wire.update(kwargs)
        wire["content_hash"] = content_hash(wire)
        return wire

    def test_a_shifting_debate_must_say_what_moved_it(self) -> None:
        with self.assertRaises(DebateMapValidationError):
            validate_version(self.version(debates=[valid_debate(status="shifting")]))

    def test_a_resolved_debate_needs_a_resolving_reference(self) -> None:
        with self.assertRaises(DebateMapValidationError):
            validate_version(self.version(debates=[valid_debate(status="resolved")]))
        validate_version(self.version(debates=[valid_debate(
            status="resolved", shift={"reason": "the filing settled it", "refs": ["cv-a"]},
        )]))

    def test_the_placement_the_drafter_named_is_on_the_record(self) -> None:
        stored = validate_version(self.version())
        self.assertEqual(stored["debates"][0]["admission_index"], 0)
        self.assertEqual(stored["debates"][0]["causal_link_index"], 0)
        for field in ("admission_index", "causal_link_index"):
            with self.subTest(field=field):
                with self.assertRaises(DebateMapValidationError):
                    validate_version(self.version(
                        debates=[valid_debate(**{field: -1})]))
                with self.assertRaises(DebateMapValidationError):
                    validate_version(self.version(
                        debates=[valid_debate(**{field: "first"})]))

    def test_an_unavailable_market_position_cannot_also_take_a_lean(self) -> None:
        wire = valid_debate()
        wire["market_position"] = {"available": False, "lean": "bull",
                                   "statement": None, "refs": []}
        with self.assertRaises(DebateMapValidationError):
            validate_version(self.version(debates=[wire]))

    def test_a_change_reason_must_point_at_evidence_the_version_cites(self) -> None:
        with self.assertRaises(DebateMapValidationError):
            validate_version(self.version(change_evidence_refs=["cv-elsewhere"]))

    def test_the_content_hash_binds_the_whole_version(self) -> None:
        wire = self.version()
        wire["debates"][0]["question"] = "a different question"
        with self.assertRaises(DebateMapConflict):
            validate_version(wire)


class DebateMapAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = DebateMapAuthority(self.store)

    def publish(self, debates, *, refs, created_at="2026-09-09T00:00:00+00:00", **kwargs):
        params = {
            "subject_ref": SUBJECT,
            "subject_kind": "company",
            "change_reason": "evidence_thicker",
            "change_evidence_refs": refs,
            "constitution_ref": "constitution-version:x:1",
            "constitution_hash": "a" * 64,
            "evidence_fingerprint": evidence_fingerprint(["cv-a", "cv-b"]),
            "debates": debates,
            "actor_ref": "automation:dalton",
            "created_at": created_at,
        }
        params.update(kwargs)
        return self.authority.publish_map(**params)

    def test_the_first_version_reads_back_exactly_as_written(self) -> None:
        published = self.publish([valid_debate()], refs=["cv-a"])
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["version"], 1)
        stored = self.authority.version(published["id"])
        self.assertEqual(stored["content_hash"], published["content_hash"])
        self.assertEqual(self.authority.current(SUBJECT)["id"], published["id"])
        self.assertEqual(self.authority.counts(), {"maps": 1, "versions": 1})

    def test_the_same_map_twice_is_a_duplicate_not_a_second_version(self) -> None:
        first = self.publish([valid_debate()], refs=["cv-a"])
        again = self.publish([valid_debate()], refs=["cv-a"],
                             created_at="2026-09-10T00:00:00+00:00")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.authority.counts()["versions"], 1)

    def test_a_new_reference_is_a_new_version(self) -> None:
        self.publish([valid_debate()], refs=["cv-a"])
        second = self.publish(
            [valid_debate(bull_refs=("cv-a", "cv-c"))], refs=["cv-c"],
            created_at="2026-09-10T00:00:00+00:00",
        )
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertTrue(second["reason"].startswith("new_ref:"))

    def test_a_status_change_is_a_new_version_even_with_no_new_reference(self) -> None:
        self.publish([valid_debate()], refs=["cv-a"])
        moved = self.publish(
            [valid_debate(status="shifting",
                          shift={"reason": "bull gaining", "refs": ["cv-a"]})],
            refs=["cv-a"], created_at="2026-09-10T00:00:00+00:00",
        )
        self.assertEqual(moved["status"], "fresh")
        self.assertTrue(moved["reason"].startswith("status_change:"))

    def test_dropping_a_stale_debate_is_a_new_version(self) -> None:
        self.publish(
            [valid_debate(), valid_debate(ref="debate:stale",
                                          question="Is this still argued about?")],
            refs=["cv-a"],
        )
        # Nothing added, nothing re-stated: the only change is that one debate
        # is gone. Retiring an argument is a change of mind and has to be
        # publishable as one.
        dropped = self.publish([valid_debate()], refs=["cv-a"],
                               created_at="2026-09-10T00:00:00+00:00")
        self.assertEqual(dropped["status"], "fresh")
        self.assertEqual(dropped["reason"], "dropped_debate:debate:stale")
        self.assertEqual(
            [item["debate_ref"] for item in dropped["debates"]], ["debate:bookings"])

    def test_the_authority_refuses_a_version_whose_reason_names_nothing_new(self) -> None:
        self.publish([valid_debate()], refs=["cv-a"])
        outcome = novelty(self.authority.current(SUBJECT), self.authority.current(SUBJECT))
        self.assertFalse(outcome["new"])

    def test_versions_cannot_be_updated_or_deleted(self) -> None:
        published = self.publish([valid_debate()], refs=["cv-a"])
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE debate_map_versions SET actor_ref='someone-else'"
            )
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "DELETE FROM debate_map_versions WHERE version_id=?",
                (published["id"],),
            )

    def test_an_insert_outside_the_store_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "INSERT INTO debate_map_versions(version_id,map_ref,version_number,"
                "prior_version_id,subject_ref,subject_kind,change_reason,"
                "evidence_fingerprint,debate_count,live_count,rejected_count,"
                "record_json,content_hash,actor_ref,created_at) "
                "VALUES('x','y',1,NULL,'z','company','evidence_thicker','f',0,0,0,"
                "'{}','h','a','t')"
            )


class DebateMapReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = DebateMapAuthority(self.store)
        base = {
            "subject_ref": SUBJECT, "subject_kind": "company",
            "change_reason": "evidence_thicker",
            "constitution_ref": "constitution-version:x:1",
            "constitution_hash": "a" * 64,
            "evidence_fingerprint": evidence_fingerprint(["cv-a"]),
            "actor_ref": "automation:dalton",
        }
        self.first = self.authority.publish_map(
            **base, change_evidence_refs=["cv-a"],
            debates=[valid_debate(), valid_debate(
                ref="debate:margin", status="candidate", drivers=("driver:utilisation",),
                question="Is utilisation about to roll over?",
            )],
            created_at="2026-09-09T00:00:00+00:00",
        )
        self.second = self.authority.publish_map(
            **base, change_evidence_refs=["cv-c"],
            debates=[
                valid_debate(bull_refs=("cv-a", "cv-c"), status="shifting",
                             shift={"reason": "bull gaining", "refs": ["cv-c"]}),
                valid_debate(ref="debate:margin", status="candidate",
                             drivers=("driver:utilisation",),
                             question="Is utilisation about to roll over?"),
            ],
            created_at="2026-09-10T00:00:00+00:00",
        )

    def test_open_debates_exclude_candidates_and_include_shifting_ones(self) -> None:
        live = self.authority.open_debates(SUBJECT)
        self.assertEqual([item["debate_ref"] for item in live], ["debate:bookings"])
        self.assertEqual(live[0]["status"], "shifting")

    def test_open_debates_on_a_subject_with_no_map_is_empty_not_an_error(self) -> None:
        self.assertEqual(self.authority.open_debates("company:nobody"), [])

    def test_shifted_since_names_the_status_move_and_the_new_refs(self) -> None:
        moved = self.authority.shifted_since(SUBJECT, 1)
        self.assertEqual(moved["from_version"], 1)
        self.assertEqual(moved["to_version"], 2)
        self.assertEqual([item["debate_ref"] for item in moved["changed"]],
                         ["debate:bookings"])
        self.assertEqual(moved["changed"][0]["from_status"], "open")
        self.assertEqual(moved["changed"][0]["to_status"], "shifting")
        self.assertEqual(moved["changed"][0]["new_refs"], ["cv-c"])

    def test_shifted_since_accepts_a_version_id_as_well_as_a_number(self) -> None:
        by_id = self.authority.shifted_since(SUBJECT, self.first["id"])
        self.assertEqual(by_id["from_version"], 1)

    def test_shifted_since_refuses_a_version_outside_this_chain(self) -> None:
        with self.assertRaises(DebateMapNotFound):
            self.authority.shifted_since(SUBJECT, 9)
        with self.assertRaises(DebateMapNotFound):
            self.authority.shifted_since("company:nobody", 1)

    def test_debate_for_driver_finds_the_argument_behind_one_driver(self) -> None:
        found = self.authority.debate_for_driver("driver:utilisation")
        self.assertEqual([item["debate_ref"] for item in found], ["debate:margin"])
        self.assertEqual(found[0]["subject_ref"], SUBJECT)
        self.assertEqual(self.authority.debate_for_driver("driver:nothing"), [])


if __name__ == "__main__":
    unittest.main()
