"""2026-09-24: the debate map counts brokers, and a sales note now has one.

Every live debate sat at 0/0 independent sources: nothing fed the ladder a
publisher for a sales note (or read the one AlphaEngine's wire recorded), so
every broker note fell to the ``document`` rung, which does not count.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest

from dalton_core.debate_map import count_independent_sources, index_claims
from dalton_core.debate_map_draft import (
    document_attribution,
    provenance_attribution,
    reassess_source_independence,
)
from dalton_core.extraction_backlog import DocumentProvenanceStore, apply_schema
from dalton_core.sales_note_broker import (
    backfill_sales_note_provenance,
    broker_for_sender_domain,
    note_header,
    sales_note_provenance,
)


def _raw(note_id: str, domain: str, sender: str = "Desk <desk@x>") -> bytes:
    return json.dumps({"note": {
        "note_id": note_id, "sender": sender, "sender_address": f"desk@{domain}",
        "sender_domain": domain, "subject": "AMZN: callback", "body_sha256": "0" * 64,
    }}, sort_keys=True).encode("utf-8")


class FakeSpool:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        self.objects[digest] = raw
        return digest

    def read_object(self, digest: str) -> bytes:
        return self.objects[digest]


def _core() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE connector_source_envelopes (
            source_envelope_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
        CREATE TABLE observability_artifact_versions_v2 (
            version_id TEXT PRIMARY KEY, artifact_content_hash TEXT NOT NULL);
    """)
    apply_schema(connection)
    return connection


def _serve(connection, spool, index: int, note_id: str, domain: str, *,
           raw: bytes | None = None, stored_hash: str | None = None) -> None:
    raw = raw if raw is not None else _raw(note_id, domain)
    digest = spool.put(raw)
    artifact = f"artifact:host-tool:sales-notes:{index:020x}:raw"
    connection.execute("INSERT INTO observability_artifact_versions_v2 VALUES(?,?)",
                       (artifact, stored_hash or digest))
    connection.execute("INSERT INTO connector_source_envelopes VALUES(?,?)", (
        f"source-envelope:host-tool:sales-notes:{index:020x}",
        json.dumps({"source": "source:sales-notes", "operation": "get_note",
                    "raw_artifact_version_ref": artifact,
                    "source_record_refs": [note_id],
                    "retrieved_at": f"2026-09-2{index % 10}T00:00:00+00:00"})))


class SenderDomainTests(unittest.TestCase):
    def test_the_three_live_houses_are_read_off_their_sending_domains(self) -> None:
        for domain, broker in (("mail.marquee.gs.com", "Goldman Sachs"),
                               ("bofa.com", "BofA Securities"),
                               ("globalresearch.bofa.com", "BofA Securities"),
                               ("jefferies.com", "Jefferies"),
                               ("JEFFERIES.COM.", "Jefferies")):
            with self.subTest(domain=domain):
                self.assertEqual(broker_for_sender_domain(domain), broker)

    def test_a_lookalike_or_unknown_domain_is_nobody(self) -> None:
        for domain in ("notgs.com", "gs.com.evil.example", "gmail.com", "", None,
                       "desk@gs.com"):
            with self.subTest(domain=domain):
                self.assertIsNone(broker_for_sender_domain(domain))

    def test_the_record_keeps_no_title_date_or_companies(self) -> None:
        record = sales_note_provenance(note_header(_raw("sales-note:1", "mail.marquee.gs.com")))
        self.assertEqual((record["broker"], record["broker_key"], record["provenance_tier"]),
                         ("Goldman Sachs", "goldman sachs", "sales_note"))
        self.assertEqual((record["title"], record["published_at"], record["named_companies"]),
                         (None, None, []))
        self.assertEqual((record["broker_basis"], record["sender_domain"]),
                         ("sender_domain", "mail.marquee.gs.com"))


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _core()
        self.addCleanup(self.connection.close)
        self.spool = FakeSpool()

    def test_every_served_note_is_recorded_once_and_a_rerun_selects_nothing(self) -> None:
        _serve(self.connection, self.spool, 1, "sales-note:a", "mail.marquee.gs.com")
        _serve(self.connection, self.spool, 2, "sales-note:b", "bofa.com")
        _serve(self.connection, self.spool, 3, "sales-note:a", "mail.marquee.gs.com")  # refetched
        _serve(self.connection, self.spool, 4, "sales-note:c", "unknown-bank.example")
        first = backfill_sales_note_provenance(self.connection, self.spool)
        self.assertEqual((first["recorded"], first["unattributed"]), (3, 1), first)
        self.assertEqual(first["brokers"], {"Goldman Sachs": 1, "BofA Securities": 1})
        store = DocumentProvenanceStore(self.connection)
        self.assertEqual(store.get("sales-note:a")["broker_key"], "goldman sachs")
        self.assertIsNone(store.get("sales-note:c")["broker"])
        again = backfill_sales_note_provenance(self.connection, self.spool)
        self.assertEqual((again["envelopes"], again["recorded"]), (0, 0), again)

    def test_bytes_that_do_not_hash_or_name_another_note_are_not_believed(self) -> None:
        _serve(self.connection, self.spool, 1, "sales-note:a", "mail.marquee.gs.com",
               stored_hash="f" * 64)
        _serve(self.connection, self.spool, 2, "sales-note:b", "bofa.com",
               raw=_raw("sales-note:other", "bofa.com"))
        result = backfill_sales_note_provenance(self.connection, self.spool)
        self.assertEqual((result["recorded"], result["unreadable"], result["refused"]),
                         (0, 1, 1), result)
        self.assertIsNone(DocumentProvenanceStore(self.connection).get("sales-note:b"))

    def test_no_spool_and_no_envelope_table_are_answers_not_errors(self) -> None:
        self.assertEqual(backfill_sales_note_provenance(self.connection, None)["status"], "no_spool")
        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        self.assertEqual(backfill_sales_note_provenance(bare, self.spool)["status"],
                         "no_sales_note_envelopes")


class LadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _core()
        self.addCleanup(self.connection.close)
        store = DocumentProvenanceStore(self.connection)
        for record in (
            sales_note_provenance(note_header(_raw("sales-note:gs1", "mail.marquee.gs.com"))),
            sales_note_provenance(note_header(_raw("sales-note:gs2", "mail.marquee.gs.com"))),
            sales_note_provenance(note_header(_raw("sales-note:bofa", "bofa.com"))),
            sales_note_provenance(note_header(_raw("sales-note:nobody", "unknown.example"))),
        ):
            store.record(record)
        for ref, broker, key in (("alphaengine-doc:gs", "Goldman Sachs", "goldman sachs"),
                                 ("alphaengine-doc:wf", "Wells Fargo Securities, LLC", "wells fargo"),
                                 ("alphaengine-doc:gg", "Guggenheim Securities LLC", "guggenheim"),
                                 ("alphaengine-doc:cn", "中信建投", "")):
            store.record({"document_ref": ref, "source_ref": "source:alphaengine",
                          "provenance_tier": "sell_side", "broker": broker, "broker_key": key,
                          "title": f"{broker}: note", "named_companies": [],
                          "metadata_seen": True})

    def test_one_house_is_one_key_whichever_source_carried_it(self) -> None:
        found = provenance_attribution(self.connection)
        self.assertEqual(found["sales-note:gs1"]["publisher"], "goldman")
        self.assertEqual(found["alphaengine-doc:gs"]["publisher"], "goldman")
        self.assertEqual(found["sales-note:bofa"]["publisher"], "bofa")
        self.assertEqual(found["alphaengine-doc:wf"]["publisher"], "wells-fargo")
        self.assertEqual(found["alphaengine-doc:gg"]["publisher"], "guggenheim")
        self.assertEqual(found["alphaengine-doc:cn"]["publisher"], "中信建投")
        self.assertNotIn("sales-note:nobody", found)
        # The merge reads the provenance table even when the discovered-document
        # table carries no attribution column at all (today's live shape).
        self.assertEqual(document_attribution(self.connection)["sales-note:bofa"]["publisher"], "bofa")

    def test_two_notes_from_one_house_are_one_voice_and_two_houses_are_two(self) -> None:
        found = provenance_attribution(self.connection)
        rows = [{"claim_version_ref": f"claim-version:{ref}", "subject_ref": "company:ticker:amzn",
                 "document_ref": ref, "document_publisher": found[ref]["publisher"],
                 "spec_ref": "sales-notes"}
                for ref in ("sales-note:gs1", "sales-note:gs2", "sales-note:bofa", "alphaengine-doc:gs")]
        rows.append({"claim_version_ref": "claim-version:sales-note:nobody",
                     "subject_ref": "company:ticker:amzn", "document_ref": "sales-note:nobody",
                     "spec_ref": "sales-notes"})
        claims = index_claims(rows)
        same = count_independent_sources(
            ["claim-version:sales-note:gs1", "claim-version:sales-note:gs2",
             "claim-version:alphaengine-doc:gs"], claims)
        self.assertEqual((same["count"], same["keys"]), (1, ["publisher:goldman"]))
        two = count_independent_sources(
            ["claim-version:sales-note:gs1", "claim-version:sales-note:bofa",
             "claim-version:sales-note:nobody"], claims)
        self.assertEqual((two["count"], two["unattributed"]), (2, 1))


class ReassessmentTests(unittest.TestCase):
    """The stored map is not rewritten; the reassessment only reads."""

    def test_a_stored_zero_zero_debate_is_groundable_under_todays_attribution(self) -> None:
        connection = _core()
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE evidence_relations (claim_version_id TEXT, evidence_version_id TEXT,
                                             relation TEXT);
            CREATE TABLE evidence_versions (evidence_version_id TEXT PRIMARY KEY,
                                            evidence_json TEXT);
            CREATE TABLE transcript_correction_set_versions (version_id TEXT PRIMARY KEY,
                                                             record_json TEXT);
        """)
        store = DocumentProvenanceStore(connection)
        notes = {"a": "mail.marquee.gs.com", "b": "bofa.com", "c": "jefferies.com",
                 "d": "bofa.com"}
        for key, domain in notes.items():
            store.record(sales_note_provenance(note_header(_raw(f"sales-note:{key}", domain))))
            connection.execute("INSERT INTO transcript_correction_set_versions VALUES(?,?)",
                               (f"tcs:{key}", json.dumps({"document_ref": f"sales-note:{key}"})))
            connection.execute("INSERT INTO evidence_versions VALUES(?,?)", (
                f"ev:{key}", json.dumps({"source_lineage": ["source:sales-notes", f"tcs:{key}"],
                                         "source_type": "sell_side_note"})))
            connection.execute("INSERT INTO evidence_relations VALUES(?,?,?)",
                               (f"cv:{key}", f"ev:{key}", "supports"))
        record = {"subject_ref": "company:ticker:amzn", "debates": [
            {"debate_ref": "debate:1", "status": "candidate",
             "source_independence": {"bull_sources": 0, "bear_sources": 0},
             "bull_position": {"claim_refs": ["cv:a", "cv:b"]},
             "bear_position": {"claim_refs": ["cv:c", "cv:d"]}},
            {"debate_ref": "debate:2", "status": "candidate",
             "source_independence": {"bull_sources": 0, "bear_sources": 0},
             "bull_position": {"claim_refs": ["cv:b", "cv:d"]},
             "bear_position": {"claim_refs": ["cv:a"]}},
        ]}
        before = connection.total_changes
        first, second = reassess_source_independence(connection, record)
        self.assertEqual(connection.total_changes, before)
        self.assertEqual(first["stored"], {"bull_sources": 0, "bear_sources": 0})
        self.assertEqual((first["now"]["bull_sources"], first["now"]["bear_sources"]), (2, 2))
        self.assertTrue(first["groundable_now"])
        # Two BofA notes on the bull side are one house.
        self.assertEqual((second["now"]["bull_sources"], second["groundable_now"]), (1, False))


if __name__ == "__main__":
    unittest.main()
