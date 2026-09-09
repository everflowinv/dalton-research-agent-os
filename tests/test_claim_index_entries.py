"""P12b: the index is append-only, it settles its own groups, and it degrades to nothing.

Three things are worth a test here and the rest follows from them.

An entry is a judgement recorded about a claim, so recording the same judgement
twice is one judgement (``duplicate``) and recording a different one is a new
version -- never an edit, and never a touched ``claim_versions`` row.

``is_canonical`` is a fact about a group at a moment, so when a better-sourced
claim joins a group the members whose answer changed get a new version saying
so. Two canonical members in one group would mean a reader asking for one copy
of a fact gets two, which is the entire complaint the index exists to answer.

And a Core written before P12b has no index. Every reader has to answer there
exactly as it did before, because "this Core has no index" is a normal state
and will be the state of the live Core until the integrator runs the lane.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_aspect_vocabulary import ClaimAspectError
from dalton_core.claim_index_authority import (
    ClaimIndexAuthority,
    ClaimIndexConflict,
    ClaimIndexValidationError,
    canonical_order_key,
    current_entries,
    entry_ref_for,
    group_ref_for,
    table_exists,
    validate_entry,
)
from dalton_core.company_research_view import (
    CompanyResearchViewValidationError,
    annotate_with_index,
    query_company_research,
)
from dalton_core.store import DaltonStore

ACN = "company:sec-cik:0001467373"
ACTOR = "automation:coverage-mission"
GROUP = "quant|" + ACN + "|revenue|2026-05-31|percent"
NOW = "2026-09-09T12:00:00+00:00"


def invocation(invocation_id: str) -> dict:
    return {
        "schema_version": "0.1", "id": invocation_id,
        "created_at": "2026-09-01T00:00:00+00:00", "work_order_ref": "wo-index",
        "profile_ref": "profile-" + invocation_id, "granularity": "task",
        "capability": "research", "provider": "family-a",
        "model": "model-" + invocation_id, "model_family": "family-a",
        "runtime_ref": "runtime-index", "actor_ref": ACTOR, "usage": {"tokens": 1},
        "input_refs": [], "output_refs": [],
        "started_at": "2026-09-01T00:00:00+00:00", "completed_at": None,
        "side_effects": [], "parent_ref": None,
    }


class LedgerFixture:
    """A Core with real Claims, written the way the existing index tests do."""

    def __init__(self, path: str = ":memory:") -> None:
        self.store = DaltonStore(path)

    def add_claim(self, ref, *, subject_ref=ACN, kind="quantitative", value=5.59,
                  unit="percent", metric="revenue", period="2026-03-01..2026-05-31",
                  statement=None, source_type="official_filing"):
        producer = "producer-" + ref
        self.store.register_invocation(invocation(producer))
        evidence = self.store.register_evidence({
            "evidence_ref": "evidence-" + ref, "source_type": source_type,
            "source_ref": "sec:" + ref, "retrieved_at": "2026-09-01T00:00:00+00:00",
            "source_lineage": ["sec:" + ref], "independence_group": "sec:" + ref,
            "actor_ref": ACTOR,
        })
        claim = self.store.register_claim({
            "claim_ref": ref, "subject_ref": subject_ref, "metric_or_aspect": metric,
            "period": period, "basis": "reported",
            "normalized_statement": statement or f"claim {ref}",
            "claim_kind": kind, "value": value, "unit": unit,
            "producer_invocation_refs": [producer], "actor_ref": ACTOR,
        })
        self.store.relate_evidence({
            "id": "relation-" + ref,
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        })
        return claim

    def close(self):
        self.store.close()


_BINDING_FIELDS_FOR_TEST = (
    "claim_version_ref", "claim_version_hash", "claim_ref", "claim_created_at",
    "subject_ref", "metric_or_aspect", "period_key", "claim_kind", "aspect",
    "aspect_source", "as_of", "as_of_basis", "importance", "importance_basis",
    "dedupe_group_ref", "dedupe_group_key", "tagger_ref", "tagger_hash",
    "actor_ref",
)


def _claim_of(authority, recorded):
    """The claim identity an entry was recorded against, for a re-record."""

    return {
        "claim_version_id": recorded["claim_version_ref"],
        "content_hash": recorded["claim_version_hash"],
        "claim_ref": recorded["claim_ref"],
    }


def entry_args(claim, **overrides):
    base = {
        "claim_version_ref": claim["claim_version_id"],
        "claim_version_hash": claim["content_hash"],
        "claim_ref": claim["claim_ref"],
        "claim_created_at": "2026-09-01T00:00:00+00:00",
        "subject_ref": ACN, "metric_or_aspect": "revenue",
        "period_key": "2026-03-01..2026-05-31", "claim_kind": "quantitative",
        "aspect": "segments_and_mix", "aspect_source": "rule",
        "as_of": "2026-05-31", "as_of_basis": "period_end",
        "importance": "news", "importance_basis": "evidence_source_type:public_web",
        "dedupe_group_key": GROUP, "tagger_ref": "rule:claim-index-deterministic:0.1",
        "tagger_hash": "a" * 64, "actor_ref": ACTOR, "created_at": NOW,
    }
    base.update(overrides)
    return base


class EntryContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.authority = ClaimIndexAuthority(self.fixture.store)
        self.claim = self.fixture.add_claim("index-1")

    def test_the_same_judgement_recorded_twice_is_one_judgement(self):
        first = self.authority.record_entry(**entry_args(self.claim))
        self.assertEqual(first["status"], "fresh")
        again = self.authority.record_entry(
            **entry_args(self.claim, created_at="2026-09-10T00:00:00+00:00"))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.authority.counts(), {"entries": 1, "versions": 1})

    def test_a_changed_judgement_is_a_new_version_on_the_same_chain(self):
        first = self.authority.record_entry(**entry_args(self.claim))
        second = self.authority.record_entry(
            **entry_args(self.claim, aspect="demand_drivers", aspect_source="model",
                         tagger_ref="model:work:1", tagger_hash="b" * 64))
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        self.assertEqual(second["revision_reason"], "tagged")
        chain = self.authority.versions(self.claim["claim_version_id"])
        self.assertEqual([item["aspect"] for item in chain],
                         ["segments_and_mix", "demand_drivers"])

    def test_the_claim_it_indexes_is_never_touched(self):
        before = self.fixture.store.connection.execute(
            "SELECT claim_json, content_hash FROM claim_versions").fetchall()
        self.authority.record_entry(**entry_args(self.claim))
        after = self.fixture.store.connection.execute(
            "SELECT claim_json, content_hash FROM claim_versions").fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_entries_cannot_be_edited_or_deleted_or_written_around_the_store(self):
        self.authority.record_entry(**entry_args(self.claim))
        connection = self.fixture.store.connection
        for statement in (
            "UPDATE claim_index_entry_versions SET aspect='other'",
            "DELETE FROM claim_index_entry_versions",
            "INSERT INTO claim_index_entry_versions(version_id,entry_ref,version_number,"
            "claim_version_ref,claim_version_hash,claim_ref,subject_ref,aspect,as_of_basis,"
            "importance,dedupe_group_ref,is_canonical,tagger_ref,record_json,content_hash,"
            "actor_ref,created_at) VALUES('x','y',1,'z','h','c','s','other','unknown',"
            "'news','g',1,'t','{}','h','a','t')",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(statement)

    def test_a_word_outside_the_vocabularies_never_reaches_the_table(self):
        for bad in (
            {"aspect": "moat"},
            {"importance": "very_important"},
            {"as_of_basis": "vibes"},
            {"aspect_source": "guess"},
        ):
            with self.assertRaises((ClaimIndexValidationError, ClaimAspectError)):
                self.authority.record_entry(**entry_args(self.claim, **bad))

    def test_a_date_without_a_basis_is_refused_and_so_is_a_basis_without_a_date(self):
        with self.assertRaises(ClaimIndexValidationError):
            self.authority.record_entry(
                **entry_args(self.claim, as_of=None, as_of_basis="period_end"))
        with self.assertRaises(ClaimIndexValidationError):
            self.authority.record_entry(
                **entry_args(self.claim, as_of="2026-05-31", as_of_basis="unknown"))

    def test_a_stored_entry_cannot_be_altered_even_by_the_store_itself(self):
        recorded = self.authority.record_entry(**entry_args(self.claim))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.fixture.store._transaction() as cur:
                cur.execute(
                    "UPDATE claim_index_entry_versions SET content_hash=? WHERE version_id=?",
                    ("f" * 64, recorded["id"]),
                )
        self.assertEqual(self.authority.entry(recorded["id"])["content_hash"],
                         recorded["content_hash"])

    def test_an_entry_whose_bytes_stopped_agreeing_is_a_conflict_not_a_read(self):
        # The immutability triggers make this unreachable through the store, so
        # it is checked at the validator: a file that was edited under the
        # process, or restored from a bad backup, must not read back as fact.
        recorded = self.authority.record_entry(**entry_args(self.claim))
        tampered = {**recorded, "aspect": "other"}
        tampered.pop("status", None)
        tampered.pop("recanonicalised", None)
        with self.assertRaises(ClaimIndexConflict):
            validate_entry(tampered)

    def test_refs_are_derived_and_cannot_be_asserted(self):
        recorded = self.authority.record_entry(**entry_args(self.claim))
        self.assertEqual(recorded["entry_ref"],
                         entry_ref_for(self.claim["claim_version_id"]))
        self.assertEqual(recorded["dedupe_group_ref"], group_ref_for(GROUP))


class CanonicalGroupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.authority = ClaimIndexAuthority(self.fixture.store)

    def record(self, ref, **overrides):
        claim = self.fixture.add_claim(ref)
        return self.authority.record_entry(**entry_args(claim, **overrides))

    def test_three_copies_of_one_quarter_leave_one_canonical(self):
        # The ACN Initial Screen complaint: one quarter's revenue growth from a
        # filing, a call and a broker note, cited three times side by side.
        news = self.record("copy-news", importance="news",
                           claim_created_at="2026-09-01T00:00:00+00:00")
        call = self.record("copy-call", importance="management_statement",
                           claim_created_at="2026-09-02T00:00:00+00:00")
        filing = self.record("copy-filing", importance="filing",
                             claim_created_at="2026-09-03T00:00:00+00:00")
        members = self.authority.group_members(group_ref_for(GROUP))
        self.assertEqual([item["importance"] for item in members],
                         ["filing", "management_statement", "news"])
        self.assertEqual([item["is_canonical"] for item in members],
                         [True, False, False])
        # The two that lost were re-versioned rather than edited.
        self.assertTrue(news["is_canonical"])
        self.assertTrue(call["is_canonical"])
        self.assertTrue(filing["is_canonical"])
        self.assertEqual(len(filing["recanonicalised"]), 1)
        self.assertEqual(
            self.authority.current_entry(news["claim_version_ref"])["version"], 2)
        self.assertEqual(
            self.authority.current_entry(news["claim_version_ref"])["revision_reason"],
            "recanonicalised")

    def test_equal_importance_breaks_on_date_then_on_the_earliest_claim(self):
        old = self.record("date-old", as_of="2026-03-31",
                          claim_created_at="2026-09-01T00:00:00+00:00")
        new = self.record("date-new", as_of="2026-05-31",
                          claim_created_at="2026-09-02T00:00:00+00:00")
        self.assertFalse(
            self.authority.current_entry(old["claim_version_ref"])["is_canonical"])
        self.assertTrue(
            self.authority.current_entry(new["claim_version_ref"])["is_canonical"])
        first = self.record("tie-first", as_of="2026-05-31",
                            claim_created_at="2026-08-01T00:00:00+00:00")
        # Earliest claim wins a tie, so the citation a reader already saw stays
        # the citation.
        self.assertTrue(
            self.authority.current_entry(first["claim_version_ref"])["is_canonical"])
        self.assertFalse(
            self.authority.current_entry(new["claim_version_ref"])["is_canonical"])

    def test_a_dated_claim_beats_an_undated_one(self):
        undated = canonical_order_key({
            "importance": "news", "as_of": None,
            "claim_created_at": "2026-01-01", "claim_version_ref": "a"})
        dated = canonical_order_key({
            "importance": "news", "as_of": "2020-01-01",
            "claim_created_at": "2026-01-01", "claim_version_ref": "b"})
        self.assertLess(dated, undated)

    def test_a_retag_that_moves_a_claim_settles_the_group_it_left(self):
        # A period that now parses, a basis that was missing: the claim moves
        # to another group, and the group it vacated has to be settled too or
        # the entry it had displaced stays non-canonical with nothing canonical
        # above it -- and a canonical-only read returns neither.
        winner = self.record("mover", importance="filing",
                             claim_created_at="2026-09-01T00:00:00+00:00")
        loser = self.record("stayer", importance="news",
                            claim_created_at="2026-09-02T00:00:00+00:00")
        self.assertFalse(
            self.authority.current_entry(loser["claim_version_ref"])["is_canonical"])
        moved = self.authority.record_entry(**entry_args(
            self.fixture.store.connection.execute(
                "SELECT 1").fetchone() and _claim_of(self.authority, winner),
            dedupe_group_key="quant|elsewhere|revenue|2026-05-31|gaap|percent",
            importance="filing", claim_created_at="2026-09-01T00:00:00+00:00"))
        self.assertEqual(moved["version"], 2)
        self.assertTrue(
            self.authority.current_entry(loser["claim_version_ref"])["is_canonical"])
        self.assertIn(
            self.authority.current_entry(loser["claim_version_ref"])["id"],
            moved["recanonicalised"])

    def test_an_unchanged_tag_still_settles_the_group_around_it(self):
        # Recording the same judgement twice is one judgement, but the group
        # may have changed underneath it, and short-circuiting the write must
        # not short-circuit the settlement.
        first = self.record("settle-first", importance="news",
                            claim_created_at="2026-09-01T00:00:00+00:00")
        self.assertTrue(first["is_canonical"])
        # Reach in and leave the group with two canonical members, the state a
        # crash between the write and the settle would leave behind.
        second = self.fixture.add_claim("settle-second")
        args = entry_args(second, importance="news",
                          claim_created_at="2026-09-02T00:00:00+00:00")
        body = {field: args[field] for field in _BINDING_FIELDS_FOR_TEST
                if field != "dedupe_group_ref"}
        body["dedupe_group_ref"] = group_ref_for(args["dedupe_group_key"])
        with self.fixture.store._transaction() as cur:
            self.authority._write(
                cur, body, version=1, prior_version_ref=None, is_canonical=True,
                revision_reason="tagged", created_at=NOW)
        members = self.authority.group_members(group_ref_for(GROUP))
        self.assertEqual([item["is_canonical"] for item in members], [True, True])
        again = self.authority.record_entry(**entry_args(
            self.fixture.store.connection.execute("SELECT 1").fetchone()
            and _claim_of(self.authority, first),
            importance="news", claim_created_at="2026-09-01T00:00:00+00:00"))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(
            [item["is_canonical"] for item in
             self.authority.group_members(group_ref_for(GROUP))],
            [True, False])

    def test_different_groups_never_interfere(self):
        one = self.record("group-a")
        two = self.record("group-b", dedupe_group_key="quant|other|revenue|x|percent",
                          importance="filing")
        self.assertTrue(self.authority.current_entry(one["claim_version_ref"])["is_canonical"])
        self.assertTrue(self.authority.current_entry(two["claim_version_ref"])["is_canonical"])
        self.assertEqual(two["recanonicalised"], [])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerFixture()
        self.addCleanup(self.fixture.close)
        self.store = self.fixture.store
        self.filing = self.fixture.add_claim("read-filing")
        self.news = self.fixture.add_claim("read-news")
        self.prose = self.fixture.add_claim(
            "read-prose", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="Demand is stable.")

    def index(self):
        authority = ClaimIndexAuthority(self.store)
        authority.record_entry(**entry_args(self.filing, importance="filing",
                                            claim_created_at="2026-09-01T00:00:00+00:00"))
        authority.record_entry(**entry_args(self.news, importance="news",
                                            claim_created_at="2026-09-02T00:00:00+00:00"))
        authority.record_entry(**entry_args(
            self.prose, claim_kind="qualitative", aspect="demand_drivers",
            aspect_source="model", tagger_ref="model:work:1", tagger_hash="c" * 64,
            period_key="current", as_of="2026-09-01", as_of_basis="evidence_retrieved_at",
            importance="management_statement",
            dedupe_group_key="qual|" + ACN + "|" + "d" * 64,
            claim_created_at="2026-09-03T00:00:00+00:00"))
        return authority

    def test_a_core_with_no_index_answers_exactly_as_it_did_before(self):
        self.assertFalse(table_exists(self.store.connection))
        rows = query_company_research(self.store, company_ref=ACN)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertNotIn("index_aspect", row)
            self.assertNotIn("is_canonical", row)
        self.assertEqual(current_entries(self.store.connection), {})

    def test_an_index_filter_on_an_unindexed_core_says_so_rather_than_matching_nothing(self):
        # "There is no index here" and "nothing matched" are different answers
        # and a caller that cannot tell them apart draws the wrong conclusion.
        with self.assertRaises(CompanyResearchViewValidationError) as caught:
            query_company_research(self.store, index_aspect="demand_drivers")
        self.assertIn("no Claim index", str(caught.exception))

    def test_canonical_only_is_the_default_and_drops_the_duplicate(self):
        self.index()
        rows = query_company_research(self.store, company_ref=ACN)
        refs = {row["claim_ref"] for row in rows}
        self.assertEqual(refs, {"read-filing", "read-prose"})
        everything = query_company_research(
            self.store, company_ref=ACN, canonical_only=False)
        self.assertEqual(len(everything), 3)

    def test_the_index_fields_come_back_joined_onto_the_claim_row(self):
        self.index()
        [row] = [r for r in query_company_research(self.store, company_ref=ACN)
                 if r["claim_ref"] == "read-prose"]
        self.assertEqual(row["index_aspect"], "demand_drivers")
        self.assertEqual(row["importance"], "management_statement")
        self.assertEqual(row["as_of_basis"], "evidence_retrieved_at")
        self.assertTrue(row["is_canonical"])
        # The Ledger's own free-text field is untouched and still separate.
        self.assertEqual(row["metric_or_aspect"], "demand environment")

    def test_filters_by_aspect_importance_and_date_range(self):
        self.index()
        self.assertEqual(
            [r["claim_ref"] for r in query_company_research(
                self.store, index_aspect="demand_drivers")],
            ["read-prose"])
        self.assertEqual(
            [r["claim_ref"] for r in query_company_research(
                self.store, importance="filing")],
            ["read-filing"])
        self.assertEqual(
            [r["claim_ref"] for r in query_company_research(
                self.store, as_of_from="2026-06-01")],
            ["read-prose"])
        self.assertEqual(
            query_company_research(self.store, as_of_to="2020-01-01"), [])
        with self.assertRaises(CompanyResearchViewValidationError):
            query_company_research(self.store, index_aspect="moat")
        with self.assertRaises(CompanyResearchViewValidationError):
            query_company_research(self.store, importance="quite_good")

    def test_an_untagged_claim_is_never_hidden_by_canonical_only(self):
        authority = ClaimIndexAuthority(self.store)
        authority.record_entry(**entry_args(self.filing, importance="filing",
                                            claim_created_at="2026-09-01T00:00:00+00:00"))
        rows = query_company_research(self.store, company_ref=ACN)
        self.assertEqual(len(rows), 3)
        untagged = [r for r in rows if r["claim_ref"] == "read-prose"][0]
        self.assertIsNone(untagged["index_aspect"])
        self.assertIsNone(untagged["is_canonical"])

    def test_the_sort_key_is_on_every_joined_row_or_the_list_cannot_be_sorted(self):
        # It used to appear only on tagged rows under a private name, so a
        # caller that sorted by it crashed on the first untagged claim.
        self.index()
        own = [{"claim_version_ref": self.filing["claim_version_id"]},
               {"claim_version_ref": self.news["claim_version_id"]},
               {"claim_version_ref": "claim-version:never-tagged"}]
        joined = annotate_with_index(
            self.store.connection, own, canonical_only=False)
        self.assertEqual(len(joined), 3)
        for row in joined:
            self.assertIn("index_order", row)
        ordered = sorted(joined, key=lambda row: row["index_order"])
        self.assertEqual(ordered[0]["importance"], "filing")
        # An untagged claim sorts after every tagged one.
        self.assertIsNone(ordered[-1]["index_aspect"])

    def test_the_projection_does_not_leak_the_sort_key(self):
        self.index()
        for row in query_company_research(self.store, company_ref=ACN):
            self.assertNotIn("index_order", row)

    def test_the_cockpit_can_join_its_own_rows_without_the_projection(self):
        # The answer context reads claim_versions directly; it needs the same
        # canonical-only view without going through query_company_research.
        self.index()
        own = [{"claim_version_ref": self.filing["claim_version_id"], "ref": "C1"},
               {"claim_version_ref": self.news["claim_version_id"], "ref": "C2"}]
        joined = annotate_with_index(self.store.connection, own)
        self.assertEqual([item["ref"] for item in joined], ["C1"])
        self.assertEqual(joined[0]["importance"], "filing")

    def test_existing_query_arguments_keep_their_meaning(self):
        self.index()
        self.assertEqual(
            [r["claim_ref"] for r in query_company_research(
                self.store, aspect="demand environment")],
            ["read-prose"])
        self.assertEqual(
            len(query_company_research(self.store, period="current")), 1)


class OldCoreFileTests(unittest.TestCase):
    def test_an_index_opened_once_stays_readable_on_the_next_open(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "core.sqlite")
        fixture = LedgerFixture(path)
        claim = fixture.add_claim("persist-1")
        recorded = ClaimIndexAuthority(fixture.store).record_entry(**entry_args(claim))
        fixture.close()

        reopened = DaltonStore(path)
        self.addCleanup(reopened.close)
        self.assertTrue(table_exists(reopened.connection))
        entries = current_entries(reopened.connection)
        self.assertEqual(list(entries), [claim["claim_version_id"]])
        self.assertEqual(entries[claim["claim_version_id"]]["id"], recorded["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
