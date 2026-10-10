"""Batch E: the material acquisition service over the MaterialRequest ledger.

Fake connectors only -- no network, no child process.  The evidence index is
built through ``evidence_index.index_document`` exactly as the indexer stores
documents (batch C's synthetic company, Synthetic Widgets / SYNW), and a fake
rung that "acquires" a document registers it through the acquisition-manifest
hook the real lanes use.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core import material_acquisition as ma
from dalton_core import material_requests as mr
from tests.test_material_pack import CORE_SCHEMA, FILL, OTHER, SYNW, Corpus, broker, transcript

NOW = "2026-10-09T12:00:00+00:00"
MISSION = {"id": "coverage-mission-version:test:1", "mission_ref": "coverage-mission:test", "version": 1,
           "industry_ref": "industry:widgets",
           "autonomy": {"automation_principal": "automation:coverage-mission", "human_checkpoints": []},
           "universe": [{"company_ref": SYNW, "ticker": "SYNW", "name": "Synthetic Widgets Co.",
                         "bootstrap_priority": "P1"},
                        {"company_ref": OTHER, "ticker": "OTHR", "name": "Other Gadgets Inc.",
                         "bootstrap_priority": "P0"}]}
EXTRA_CORE = """
CREATE TABLE coverage_mission_source_discoveries(record_id TEXT PRIMARY KEY, company_ref TEXT, source_ref TEXT,
    spec_ref TEXT, query_hash TEXT, created_at TEXT);
CREATE TABLE needs_human_acknowledgements(ack_id TEXT PRIMARY KEY, sequence INTEGER, card_ref TEXT, kind TEXT,
    choice TEXT, condition_hash TEXT, until_at TEXT, document_ref TEXT, reason TEXT, actor_ref TEXT,
    idempotency_key TEXT, record_json TEXT, content_hash TEXT, created_at TEXT);
"""


def later(hours: float = 0, days: float = 0) -> str:
    base = datetime.fromisoformat(NOW)
    return (base + timedelta(hours=hours, days=days)).isoformat()


class FakeRung:
    """A connector rung that returns scripted results and counts its calls."""

    costed = True

    def __init__(self, name, results, *, calls=1, applies=True, on_run=None):
        self.name = name
        self.results = list(results)
        self.calls = calls
        self._applies = applies
        self.runs = 0
        self.settles = 0
        self.on_run = on_run

    def applies(self, request):
        return self._applies

    def estimate_calls(self, request):
        return self.calls

    def _next(self):
        result = self.results.pop(0) if self.results else ma.RungResult("empty")
        return result if isinstance(result, ma.RungResult) else ma.RungResult(result)

    def run(self, ctx):
        self.runs += 1
        if self.on_run:
            self.on_run(ctx)
        result = self._next()
        if result.query is None:
            result.query = ctx.queries
        return result

    def settle(self, ctx):
        self.settles += 1
        return self._next()


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        core = sqlite3.connect(self.state / "core.sqlite")
        core.executescript(CORE_SCHEMA + EXTRA_CORE)
        core.execute("ALTER TABLE coverage_mission_versions ADD COLUMN created_at TEXT")
        core.execute("INSERT INTO coverage_mission_versions VALUES (?,?,?,?,?)",
                     (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION), "2026-01-01"))
        core.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)",
                     (MISSION["mission_ref"], MISSION["id"], "2026-01-01"))
        core.commit()
        core.close()
        self.corpus = Corpus(self.state)

    def core_execute(self, sql, params=()):
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute(sql, params)
        core.commit()
        core.close()

    def request(self, *, need=None, period=None, subject=SYNW, lane="initial_screen", section=None,
                source_priority=None, acceptance=None, budget=None, deadline=None, now=NOW):
        return mr.open_request(
            self.state, requester={"lane": lane, "section": section}, subject=subject,
            need=need or {"kind": "document", "doc_kinds": ["transcript"]}, period=period,
            source_priority=source_priority, acceptance=acceptance, budget=budget,
            deadline=deadline or later(hours=24), now=now)

    def attempts(self, request_id):
        view = mr.request(self.state, request_id)
        return [e for e in view["events"] if e["event_kind"] == "attempt"]


# ---------------------------------------------------------------------------
# ladder
# ---------------------------------------------------------------------------


class LadderTests(Fixture):
    def test_dispatch_and_settlement_share_one_attempt_so_web_is_reached(self):
        req = self.request(need={"kind": "answer", "question": "When is the next event?"},
                           source_priority=list(ma.LADDER), budget={"max_attempts": 6})
        rungs = {name: FakeRung(name, ["empty"]) for name in ma.LADDER}
        for name in ("indexed_corpus", "sales_notes"):
            rungs[name].costed = False
        rungs["sales_notes"].results = ["dispatched", "empty"]
        rungs["alphaengine"].results = [ma.RungResult("dispatched", calls=3), "empty"]
        rungs["sec_edgar"]._applies = False
        rungs["guidepoint"].results = [ma.RungResult("dispatched", calls=1), "not_connected"]
        rungs["web"].results = [ma.RungResult("dispatched", calls=3), "empty"]
        for step in range(4):
            result = ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=step))
            self.assertEqual(result["outcome"], "waiting")
        self.assertEqual(rungs["web"].runs, 1)
        old = list(self.attempts(req["id"]))
        result = ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=4))
        self.assertEqual(result["outcome"], "exhausted")
        self.assertEqual(self.attempts(req["id"])[:len(old)], old)
        self.assertEqual(ma.request_spend(self.state, req["id"])["calls"], 7)
        ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=5))
        self.assertEqual(rungs["web"].runs, 1)

    def test_real_attempt_cap_still_stops_new_source_and_names_unsearched_source(self):
        req = self.request(source_priority=["alphaengine", "web"], budget={"max_attempts": 1})
        alpha = FakeRung("alphaengine", ["dispatched", "empty"])
        web = FakeRung("web", ["accepted"])
        rungs = {"alphaengine": alpha, "web": web}
        ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        result = ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=1))
        self.assertEqual(result["outcome"], "exhausted")
        self.assertEqual(web.runs, 0)
        reason = mr.request(self.state, req["id"])["events"][-1]["reason"]
        self.assertIn("尚未检索", reason)
        self.assertNotIn("所有来源都没有找到", reason)

    def test_ladder_runs_in_order_and_stops_at_the_first_accepting_rung(self) -> None:
        req = self.request(period="quarter_ending:2026-08-31",
                           source_priority=["indexed_corpus", "sales_notes", "alphaengine", "web"])
        sales = FakeRung("sales_notes", ["empty"], calls=0)
        sales.costed = False
        alpha = FakeRung("alphaengine", [ma.RungResult("accepted", items=[{"document_ref": "alpha:doc:1",
                                                                           "kind": "transcript"}],
                                                       calls=1, detail="transcript found")])
        web = FakeRung("web", ["accepted"])
        rungs = {"indexed_corpus": ma.IndexedCorpusRung(), "sales_notes": sales, "alphaengine": alpha, "web": web}
        result = ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        self.assertEqual(result["outcome"], "found")
        self.assertEqual([r["source"] for r in result["rungs"]], ["indexed_corpus", "sales_notes", "alphaengine"])
        self.assertEqual(web.runs, 0)
        attempts = self.attempts(req["id"])
        self.assertEqual([(a["source"], a["outcome"]) for a in attempts],
                         [("indexed_corpus", "empty"), ("sales_notes", "empty"), ("alphaengine", "accepted")])
        self.assertTrue(all(a.get("query") for a in attempts))
        self.assertEqual(attempts[2]["calls"], 1)
        view = mr.request(self.state, req["id"])
        self.assertEqual((view["status"], view["terminal"]), ("found", True))

    def test_indexed_corpus_alone_closes_a_request_the_index_already_answers(self) -> None:
        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25",
                        period="FY2026-Q4")
        req = self.request(period="quarter_ending:2026-08-31", acceptance={"published_after": "2026-08-31"})
        alpha = FakeRung("alphaengine", ["accepted"])
        result = ma.serve(self.state, req["id"], rungs={"indexed_corpus": ma.IndexedCorpusRung(),
                                                         "alphaengine": alpha}, now=NOW)
        self.assertEqual(result["outcome"], "found")
        self.assertEqual(alpha.runs, 0)
        linked = ma.material_arrived(self.state, request_id=req["id"])
        self.assertEqual(len(linked), 1)
        self.assertTrue(linked[0]["document_ref"].startswith("test-doc:transcript"))
        self.assertIsNotNone(linked[0]["arrival_id"])
        self.assertEqual(ma.requests_for_document(self.state, linked[0]["document_ref"]), [req["id"]])

    def test_all_rungs_failing_exhausts_with_the_attempt_list(self) -> None:
        req = self.request(source_priority=["indexed_corpus", "alphaengine", "web"])
        rungs = {"indexed_corpus": ma.IndexedCorpusRung(), "alphaengine": FakeRung("alphaengine", ["empty"]),
                 "web": FakeRung("web", ["empty"])}
        result = ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        self.assertEqual(result["outcome"], "exhausted")
        view = mr.request(self.state, req["id"])
        close = [e for e in view["events"] if e["event_kind"] == "closed"][-1]
        self.assertIn("已尝试", close["reason"])
        self.assertIn("AlphaEngine", close["reason"])
        self.assertIn("公开网页", close["reason"])

    def test_partial_results_close_partial_and_link_what_was_found(self) -> None:
        req = self.request(source_priority=["alphaengine", "web"])
        partial = ma.RungResult("partial", items=[{"document_ref": "doc:p", "kind": "broker_report"}])
        result = ma.serve(self.state, req["id"], rungs={"alphaengine": FakeRung("alphaengine", [partial]),
                                                         "web": FakeRung("web", ["empty"])}, now=NOW)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(mr.request(self.state, req["id"])["status"], "partial")
        self.assertEqual([row["document_ref"] for row in ma.material_arrived(self.state)], ["doc:p"])

    def test_a_dispatched_rung_waits_and_is_settled_on_the_next_serve(self) -> None:
        req = self.request(source_priority=["alphaengine", "web"])
        alpha = FakeRung("alphaengine", [ma.RungResult("dispatched", calls=3), ma.RungResult("waiting"),
                                         ma.RungResult("accepted", items=[{"document_ref": "doc:a"}])], calls=3)
        web = FakeRung("web", ["accepted"])
        rungs = {"alphaengine": alpha, "web": web}
        self.assertEqual(ma.serve(self.state, req["id"], rungs=rungs, now=NOW)["outcome"], "waiting")
        self.assertEqual(ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=1))["outcome"], "waiting")
        self.assertEqual(ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=2))["outcome"], "found")
        self.assertEqual((alpha.runs, alpha.settles, web.runs), (1, 2, 0))
        self.assertEqual([a["outcome"] for a in self.attempts(req["id"])], ["dispatched", "accepted"])
        self.assertEqual(ma.request_spend(self.state, req["id"])["calls"], 3)

    def test_deadline_times_out_after_the_free_rungs(self) -> None:
        req = self.request(deadline=later(hours=-1), source_priority=["indexed_corpus", "alphaengine"])
        alpha = FakeRung("alphaengine", ["accepted"])
        result = ma.serve(self.state, req["id"], rungs={"indexed_corpus": ma.IndexedCorpusRung(),
                                                         "alphaengine": alpha}, now=NOW)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(alpha.runs, 0)
        self.assertEqual(mr.request(self.state, req["id"])["status"], "timeout")

    def test_an_unknown_source_is_recorded_once_and_skipped(self) -> None:
        req = self.request(source_priority=["8-K Exhibit 99.1 earnings release", "web"])
        result = ma.serve(self.state, req["id"], rungs={"web": FakeRung("web", ["accepted"])}, now=NOW)
        self.assertEqual(result["outcome"], "found")
        self.assertEqual(self.attempts(req["id"])[0]["outcome"], "unsupported")

    def test_a_rung_that_raises_is_an_error_attempt_not_a_crash(self) -> None:
        class Boom(FakeRung):
            def run(self, ctx):
                raise RuntimeError("connector broke")

        req = self.request(source_priority=["alphaengine", "web"])
        result = ma.serve(self.state, req["id"], rungs={"alphaengine": Boom("alphaengine", []),
                                                         "web": FakeRung("web", ["accepted"])}, now=NOW)
        self.assertEqual(result["outcome"], "found")
        self.assertEqual(self.attempts(req["id"])[0]["outcome"], "error")

    def test_found_material_is_registered_through_the_acquisition_manifest_path(self) -> None:
        req = self.request(period="quarter_ending:2026-08-31", source_priority=["alphaengine"])
        manifest = self.state / "acquisitions" / "abc" / "manifest.json"

        def index_manifest(state_dir, path):
            self.assertEqual(Path(path), manifest)
            self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
            return {"indexed": 1}

        alpha = FakeRung("alphaengine", [ma.RungResult("dispatched", manifests=[str(manifest)])])
        with mock.patch("dalton_core.evidence_index.index_acquired_manifest", side_effect=index_manifest) as hook:
            result = ma.serve(self.state, req["id"], rungs={"alphaengine": alpha}, now=NOW)
        hook.assert_called_once()
        self.assertEqual(result["outcome"], "found")
        self.assertEqual(len(ma.material_arrived(self.state, request_id=req["id"])), 1)


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


class AcceptanceTests(Fixture):
    def test_document_acceptance_honours_period_and_aboutness(self) -> None:
        req = self.request(period="quarter_ending:2026-08-31", acceptance={"published_after": "2026-08-31"})
        self.corpus.add("transcript", "Synthetic Widgets Q3 2026", transcript("Q3", 2026), date="2026-06-20")
        self.assertFalse(ma.evaluate_acceptance(self.state, req, now=NOW)["met"])
        self.corpus.add("transcript", "Other Gadgets Q4 2026", ("Other Gadgets call. " + FILL) * 20,
                        date="2026-09-26", company=None)
        self.assertFalse(ma.evaluate_acceptance(self.state, req, now=NOW)["met"])
        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
        verdict = ma.evaluate_acceptance(self.state, req, now=NOW)
        self.assertTrue(verdict["met"], verdict)
        self.assertEqual(verdict["items"][0]["kind"], "transcript")

    def test_min_documents_short_is_partial(self) -> None:
        req = self.request(need={"kind": "document", "doc_kinds": ["broker_report"]}, period="183d_to:2026-10",
                           acceptance={"min_documents": 4, "max_age_days": 183, "about_company": True})
        for house, date, target in (("BullHouse", "2026-09-20", "300"), ("BearHouse", "2026-09-10", "190")):
            self.corpus.add("broker_report", f"Synthetic Widgets (SYNW): {house}", broker(house, "Buy", target),
                            date=date, house=house)
        verdict = ma.evaluate_acceptance(self.state, req, now=NOW)
        self.assertEqual((verdict["met"], verdict["partial"]), (False, True))
        self.assertEqual(len(verdict["items"]), 2)

    def test_data_item_needs_the_item_a_number_and_the_period(self) -> None:
        req = self.request(need={"kind": "data", "data_item": "bookings"}, period="quarter_ending:2026-08-31")
        self.corpus.add("transcript", "Synthetic Widgets Q2 2026", transcript("Q2", 2026), date="2026-03-20")
        early = ma.evaluate_acceptance(self.state, req, now=NOW)
        self.assertFalse(early["met"])
        self.assertTrue(early["partial"])
        self.corpus.add("sec_exhibit", "Synthetic Widgets 8-K Exhibit 99.1",
                        "Synthetic Widgets reports fourth quarter results for the quarter ended August 31, 2026. "
                        "New bookings of $5.1 billion. " + FILL * 4, date="2026-09-25")
        verdict = ma.evaluate_acceptance(self.state, req, now=NOW)
        self.assertTrue(verdict["met"], verdict)
        self.assertTrue(verdict["items"][0]["passage_ref"].startswith("doc-passage:"))

    def test_answer_needs_enough_covering_passages(self) -> None:
        req = self.request(need={"kind": "answer", "question": "pricing competition large deals"},
                           acceptance={"min_passages": 1})
        self.assertFalse(ma.evaluate_acceptance(self.state, req, now=NOW)["met"])
        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
        self.assertTrue(ma.evaluate_acceptance(self.state, req, now=NOW)["met"])

    def test_queries_are_deterministic_and_two_or_three(self) -> None:
        req = self.request(need={"kind": "data", "data_item": "bookings"}, period="quarter_ending:2026-08-31")
        identity = {"name": "Synthetic Widgets", "ticker": "SYNW"}
        first = ma.build_queries(req, identity, now=NOW)
        self.assertEqual(first, ma.build_queries(req, identity, now=NOW))
        self.assertTrue(2 <= len(first) <= 3)
        self.assertIn("bookings", first[0])
        self.assertTrue(any("book-to-bill" in q for q in first))
        doc = self.request(period="FY2026-Q4")
        self.assertIn("Synthetic Widgets Q4 2026", " ".join(ma.build_queries(doc, identity, now=NOW))
                      .replace("earnings call", "").replace("  ", " "))
        self.assertTrue(all(len(q) <= 400 for q in first))

    def test_period_windows(self) -> None:
        window = ma.period_window("quarter_ending:2026-08-31", now=NOW)
        self.assertEqual(window["date_from"], "2026-09-01")
        self.assertIn("August 31, 2026", window["tokens"])
        self.assertEqual(ma.period_window("FY2026-Q4")["label"], "Q4 2026")
        self.assertEqual(ma.period_window("183d_to:2026-10", now=NOW)["date_from"], "2026-04-09")

    def test_xbrl_facts_answer_a_data_item_in_the_sec_rung(self) -> None:
        self.core_execute("CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT PRIMARY KEY, "
                          "company_ref TEXT, form TEXT, accession TEXT, filed TEXT)")
        self.core_execute("CREATE TABLE coverage_mission_statement_lines(line_id TEXT PRIMARY KEY, ingest_id TEXT, "
                          "concept TEXT, label TEXT, dimension_axis TEXT, period_start TEXT, period_end TEXT, "
                          "value TEXT, unit TEXT)")
        self.core_execute("INSERT INTO coverage_mission_statement_filings VALUES ('i1', ?, '10-K', "
                          "'0000000000-26-000001', '2026-10-15')", (SYNW,))
        self.core_execute("INSERT INTO coverage_mission_statement_lines VALUES ('l1','i1','Revenues','Revenues',"
                          "NULL,'2025-09-01','2026-08-31','21300000000','USD')")
        req = self.request(need={"kind": "data", "data_item": "revenue"}, period="quarter_ending:2026-08-31",
                           source_priority=["sec_edgar"])
        result = ma.serve(self.state, req["id"], rungs={"sec_edgar": ma.TargetedSourceRung("sec_edgar")}, now=NOW)
        self.assertEqual(result["outcome"], "found")
        self.assertEqual(ma.material_arrived(self.state)[0]["kind"], "xbrl_fact")
        self.assertEqual(ma.pending_targeted_queries(self.state, "source:sec-edgar"), [])


# ---------------------------------------------------------------------------
# targeted queries, health holds, credentials
# ---------------------------------------------------------------------------


class TargetedTests(Fixture):
    def test_targeted_query_round_trip(self) -> None:
        req = self.request(period="quarter_ending:2026-08-31", source_priority=["alphaengine"],
                           acceptance={"published_after": "2026-08-31"})
        rung = ma.TargetedSourceRung("alphaengine")
        result = ma.serve(self.state, req["id"], rungs={"alphaengine": rung}, now=NOW)
        self.assertEqual(result["outcome"], "waiting")
        pending = ma.pending_targeted_queries(self.state, "source:alphaengine")
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["company_ref"], pending[0]["spec_ref"]), (SYNW, "earnings-call-transcripts"))
        self.assertEqual(ma.serve(self.state, req["id"], rungs={"alphaengine": rung},
                                  now=later(hours=0.5))["outcome"], "waiting")
        self.assertEqual(len(ma.pending_targeted_queries(self.state, "source:alphaengine")), 1)
        ma.record_targeted_event(self.state, pending[0]["query_id"], status="launched", now=later(hours=1))
        self.assertEqual(ma.pending_targeted_queries(self.state, "source:alphaengine"), [])
        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
        self.assertEqual(ma.serve(self.state, req["id"], rungs={"alphaengine": rung},
                                  now=later(hours=1.5))["outcome"], "found")
        self.assertEqual(ma.request_spend(self.state, req["id"])["calls"], ma.CALL_ESTIMATES["alphaengine"])

    def test_unanswered_targeted_query_gives_up_after_the_rung_wait(self) -> None:
        req = self.request(source_priority=["alphaengine", "web"])
        rungs = {"alphaengine": ma.TargetedSourceRung("alphaengine"), "web": FakeRung("web", ["empty"])}
        ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        result = ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=ma.RUNG_WAIT_HOURS + 0.1))
        self.assertEqual(result["outcome"], "exhausted")
        self.assertEqual([a["outcome"] for a in self.attempts(req["id"])], ["dispatched", "not_connected", "empty"])

    def test_health_hold_is_respected_and_keeps_the_request_open(self) -> None:
        from dalton_core.connector_health import ConnectorHealth

        ConnectorHealth(self.state, clock=lambda: datetime.fromisoformat(NOW)).record_auth_failure(
            "source:alphaengine", kind="search", ref=None, detail={"message": "刷新 token"})
        req = self.request(source_priority=["alphaengine", "web"])
        web = FakeRung("web", ["empty"])
        rungs = {"alphaengine": ma.TargetedSourceRung("alphaengine"), "web": web}
        result = ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        self.assertEqual(result["outcome"], "deferred")
        self.assertEqual(ma.pending_targeted_queries(self.state, "source:alphaengine"), [])
        self.assertEqual(web.runs, 1)
        self.assertEqual(mr.request(self.state, req["id"])["status"], "open")
        ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=0.1))
        self.assertEqual([a["outcome"] for a in self.attempts(req["id"])], ["health_hold", "empty"])

    def test_a_refusal_for_credentials_reaches_the_owner(self) -> None:
        req = self.request(source_priority=["alphaengine"])
        rungs = {"alphaengine": ma.TargetedSourceRung("alphaengine")}
        ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        query = ma.pending_targeted_queries(self.state, "source:alphaengine")[0]
        ma.record_targeted_event(self.state, query["query_id"], status="refused", kind="credentials",
                                 reason="governance record is not approved; owner approval is required")
        self.assertEqual(ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=1))["outcome"], "exhausted")
        kinds = {card["kind"] for card in ma.owner_door_cards(self.state, now=later(hours=1))}
        self.assertEqual(kinds, {"material_credentials_needed", "material_sources_exhausted"})

    def test_coordinator_hook_launches_a_targeted_query_through_its_own_launcher(self) -> None:
        from dalton_core import mission_source_discovery as msd

        req = self.request(source_priority=["alphaengine"])
        ma.serve(self.state, req["id"], rungs={"alphaengine": ma.TargetedSourceRung("alphaengine")}, now=NOW)
        launcher = mock.Mock(state_dir=self.state)
        launcher.running.return_value = False
        launcher.start.return_value = {"id": "alphaengine-discovery:" + "a" * 24}
        missions = mock.Mock()
        missions.active_mission.return_value = {**MISSION, "content_hash": "h" * 64}
        missions.authorize_source_discovery.return_value = {"max_alphaengine_calls_24h": 50}
        missions.record_discovery_dispatch.return_value = {"dispatch_id": "mission-discovery-dispatch:1"}
        fake = SimpleNamespace(
            search_launcher=launcher, missions=missions, source_ref="source:alphaengine",
            plan={"mission_ref": MISSION["mission_ref"], "source_ref": "source:alphaengine", "id": "plan:1",
                  "content_hash": "p" * 64, "schema_version": "0.7", "companies": {SYNW: {}},
                  "specs": [{"spec_ref": "earnings-call-transcripts", "document_type": "meeting_minutes"}]},
            selection_launcher=None, clock=lambda: datetime.fromisoformat(NOW),
            _health_hold=lambda: None, _budget=lambda cap: {"remaining": 5},
            _missing_periods=lambda mission, company, spec: [])
        with mock.patch.object(msd, "build_discovery_parameters", return_value={"query": "x"}), \
                mock.patch.object(msd, "discovery_query_hash", return_value="q" * 64):
            result = msd.MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake)
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["material_request_ref"], req["id"])
        launcher.start.assert_called_once()
        self.assertEqual(launcher.start.call_args.kwargs["spec_ref"], "earnings-call-transcripts")
        query = ma.targeted_query_status(self.state, result["targeted_query_ref"])
        self.assertEqual((query["status"], query["dispatch_ref"]), ("launched", "mission-discovery-dispatch:1"))
        self.assertIsNone(msd.MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake))

    def test_coordinator_hook_defers_on_a_spent_call_window_and_refuses_other_companies(self) -> None:
        from dalton_core import mission_source_discovery as msd

        req = self.request(source_priority=["alphaengine"])
        ma.serve(self.state, req["id"], rungs={"alphaengine": ma.TargetedSourceRung("alphaengine")}, now=NOW)
        launcher = mock.Mock(state_dir=self.state)
        launcher.running.return_value = False
        missions = mock.Mock()
        missions.active_mission.return_value = {**MISSION, "content_hash": "h" * 64}
        missions.authorize_source_discovery.return_value = {"max_alphaengine_calls_24h": 50}
        plan = {"mission_ref": MISSION["mission_ref"], "source_ref": "source:alphaengine", "id": "plan:1",
                "content_hash": "p" * 64, "schema_version": "0.7", "companies": {SYNW: {}},
                "specs": [{"spec_ref": "earnings-call-transcripts"}]}
        fake = SimpleNamespace(search_launcher=launcher, missions=missions, source_ref="source:alphaengine",
                               plan=plan, selection_launcher=None, clock=lambda: datetime.fromisoformat(NOW),
                               _health_hold=lambda: None, _budget=lambda cap: {"remaining": 0},
                               _missing_periods=lambda *a: [])
        self.assertIsNone(msd.MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake))
        launcher.start.assert_not_called()
        pending = ma.pending_targeted_queries(self.state, "source:alphaengine")
        self.assertEqual(len(pending), 1)
        self.assertEqual(ma.targeted_query_status(self.state, pending[0]["query_id"])["kind"], "budget")
        fake.plan = {**plan, "companies": {}}
        result = msd.MissionSourceDiscoveryCoordinator.launch_targeted_discovery(fake)
        self.assertEqual(result["status"], "targeted_refused")
        self.assertEqual(ma.pending_targeted_queries(self.state, "source:alphaengine"), [])

    def test_guidepoint_hook_turns_a_targeted_query_into_a_due_plan_query(self) -> None:
        from dalton_core import mission_guidepoint_lane as gp

        req = self.request(need={"kind": "answer", "question": "client spend on AI programmes"},
                           source_priority=["guidepoint"])
        ma.serve(self.state, req["id"], rungs={"guidepoint": ma.TargetedSourceRung("guidepoint")}, now=NOW)
        compiled = [{"kind": "company", "spec_ref": "vendor-checks", "company_ref": SYNW, "parameters": {},
                     "query_hash": "q" * 64}]
        fake = SimpleNamespace(launcher=SimpleNamespace(state_dir=self.state), plan={},
                               clock=lambda: datetime.fromisoformat(NOW))
        fake._state_dir = lambda: gp.GuidepointLaneCoordinator._state_dir(fake)
        fake._answer_targeted = lambda *a, **k: gp.GuidepointLaneCoordinator._answer_targeted(fake, *a, **k)
        with mock.patch.object(gp, "guidepoint_plan_queries", return_value=compiled):
            due = gp.GuidepointLaneCoordinator.targeted_queries(fake)
        self.assertEqual(len(due), 1)
        self.assertEqual((due[0]["reason"], due[0]["company_ref"]), ("material_request", SYNW))
        fake._answer_targeted(due[0], "launched", None, None, dispatch_ref="d:1")
        self.assertEqual(ma.pending_targeted_queries(self.state, "source:guidepoint"), [])

    def test_absent_guidepoint_moves_to_web_without_three_hour_wait(self) -> None:
        from dalton_core import mission_guidepoint_lane as gp

        req = self.request(need={"kind": "answer", "question": "next confirmed company event date"},
                           source_priority=["guidepoint", "web"])
        web = FakeRung("web", [ma.RungResult("dispatched", calls=3)])
        rungs = {"guidepoint": ma.TargetedSourceRung("guidepoint"), "web": web}
        ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        query = ma.pending_targeted_queries(self.state, "source:guidepoint")[0]
        before = ma.request_spend(self.state, req["id"])
        server = SimpleNamespace(state_dir=self.state, lane_launcher=lambda _: None)
        result = gp.dispatch(server, {})
        self.assertEqual(result["targeted_refused"], 1)
        self.assertEqual(ma.request_spend(self.state, req["id"]), before)
        self.assertEqual(ma.targeted_query_status(self.state, query["query_id"])["kind"], "unconfigured")
        self.assertEqual(gp.dispatch(server, {})["targeted_refused"], 0)
        result = ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=0.01))
        self.assertEqual(web.runs, 1)
        self.assertIn({"source": "guidepoint", "outcome": "not_connected"}, result["rungs"])
        self.assertFalse(mr.request(self.state, req["id"])["closed"])


# ---------------------------------------------------------------------------
# idempotency, negative cache, budget
# ---------------------------------------------------------------------------


class CacheAndBudgetTests(Fixture):
    def exhaust(self, req):
        rungs = {"web": FakeRung("web", ["empty"])}
        self.assertEqual(ma.serve(self.state, req["id"], rungs=rungs, now=NOW)["outcome"], "exhausted")

    def test_idempotent_by_digest(self) -> None:
        first = self.request(period="quarter_ending:2026-08-31")
        second = mr.open_request(self.state, requester={"lane": "debate_map"}, subject=SYNW,
                                 need={"kind": "document", "doc_kinds": ["transcript"]},
                                 period="quarter_ending:2026-08-31", now=NOW)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["write_status"], "joined")
        ma.serve(self.state, first["id"], rungs={"indexed_corpus": FakeRung("indexed_corpus", ["accepted"])},
                 now=NOW)
        again = ma.serve(self.state, first["id"], rungs={}, now=later(days=30))
        self.assertEqual(again["outcome"], "cached")

    def test_negative_cache_holds_for_seven_days(self) -> None:
        req = self.request(source_priority=["web"])
        self.exhaust(req)
        web = FakeRung("web", ["accepted"])
        self.assertEqual(ma.serve(self.state, req["id"], rungs={"web": web}, now=later(days=3))["outcome"], "cached")
        self.assertEqual(web.runs, 0)
        result = ma.serve(self.state, req["id"], rungs={"web": web}, now=later(days=7.1))
        self.assertEqual(result["outcome"], "found")
        view = mr.request(self.state, req["id"])
        self.assertEqual((view["round"], view["status"]), (2, "found"))

    def test_a_new_discovery_batch_lifts_the_negative_cache(self) -> None:
        req = self.request(source_priority=["web"])
        self.exhaust(req)
        self.core_execute("INSERT INTO coverage_mission_source_discoveries VALUES ('d1', ?, 'source:alphaengine', "
                          "'s', 'q', ?)", (SYNW, later(days=1)))
        result = ma.serve(self.state, req["id"], rungs={"web": FakeRung("web", ["accepted"])}, now=later(days=2))
        self.assertEqual(result["outcome"], "found")

    def test_a_new_indexed_arrival_lifts_the_negative_cache_in_the_sweep(self) -> None:
        req = self.request(source_priority=["web"])
        self.exhaust(req)
        self.assertEqual(ma.retry_sweep(self.state, now=later(days=1)), [])
        self.corpus.add("sales_note", "SYNW desk", "Synthetic Widgets flat. " + FILL, date="2026-10-10",
                        now=later(days=1.5))
        reopened = ma.retry_sweep(self.state, now=later(days=2))
        self.assertEqual([row["request_id"] for row in reopened], [req["id"]])
        self.assertEqual(mr.request(self.state, req["id"])["status"], "open")

    def test_request_cap_stops_the_ladder_and_an_approval_lifts_it(self) -> None:
        req = self.request(source_priority=["alphaengine", "web"], budget={"max_calls": 2, "max_usd": "0.10"})
        alpha = FakeRung("alphaengine", ["accepted"], calls=3)
        result = ma.serve(self.state, req["id"], rungs={"alphaengine": alpha, "web": FakeRung("web", [])}, now=NOW)
        self.assertEqual(result["outcome"], "budget_blocked")
        self.assertEqual(result["budget"]["scope"], "request")
        self.assertEqual(alpha.runs, 0)
        cards = [c for c in ma.owner_door_cards(self.state, now=NOW) if c["kind"] == "material_budget_exceeded"]
        self.assertEqual(len(cards), 1)
        self.assertIn("批准追加", cards[0]["action"])
        parsed = ma.parse_budget_card_ref(cards[0]["ref"])
        self.assertEqual((parsed["scope"], parsed["key"], parsed["calls"]), ("request", req["id"], 3))
        self.core_execute("INSERT INTO needs_human_acknowledgements(ack_id,sequence,card_ref,kind,choice,created_at)"
                          " VALUES ('a1',1,?,'material_budget_exceeded','approve_spend',?)", (cards[0]["ref"], NOW))
        self.assertEqual(ma.serve(self.state, req["id"], rungs={"alphaengine": alpha}, now=later(hours=1))["outcome"],
                         "found")

    def test_pool_cap_stops_costed_rungs_but_not_the_free_ones(self) -> None:
        (self.state / "material-acquisition-pool.json").write_text(json.dumps({"calls_per_day": 2}))
        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
        free = self.request(period="quarter_ending:2026-08-31", acceptance={"published_after": "2026-08-31"})
        costly = self.request(need={"kind": "document", "doc_kinds": ["expert_call"]},
                              source_priority=["indexed_corpus", "alphaengine"])
        alpha = FakeRung("alphaengine", ["accepted"], calls=3)
        rungs = {"indexed_corpus": ma.IndexedCorpusRung(), "alphaengine": alpha}
        self.assertEqual(ma.serve(self.state, free["id"], rungs=rungs, now=NOW)["outcome"], "found")
        blocked = ma.serve(self.state, costly["id"], rungs=rungs, now=NOW)
        self.assertEqual((blocked["outcome"], blocked["budget"]["scope"]), ("budget_blocked", "pool"))
        card = next(c for c in ma.owner_door_cards(self.state, now=NOW) if c["kind"] == "material_budget_exceeded")
        self.assertTrue(card["ref"].startswith("material-budget:pool:2026-10-09:"))
        self.assertIn("今天的材料获取额度已用完", card["title"])
        status = ma.pool_status(self.state, now=NOW)
        self.assertEqual((status["cap_calls"], status["spent_calls"]), (2, 0))


# ---------------------------------------------------------------------------
# conversions
# ---------------------------------------------------------------------------


class ConversionTests(Fixture):
    INQUIRY = {"rank": 0, "company_ref": SYNW, "question": "Why did utilisation fall while headcount rose?",
               "wants": "the last two calls' utilisation commentary", "because": "contradiction"}

    def test_planner_inquiry_converts_to_an_answer_request(self) -> None:
        from dalton_core.research_planner import inquiry_material_request

        spec = inquiry_material_request(self.INQUIRY, plan_ref="research-plan:1", reason="no_bindable_template")
        self.assertEqual(spec["need"]["kind"], "answer")
        self.assertEqual(spec["requester"]["lane"], "research_planner")
        self.assertIsNone(inquiry_material_request({**self.INQUIRY, "company_ref": None}))
        self.assertEqual(inquiry_material_request({**self.INQUIRY, "company_ref": None},
                                                  industry_ref="industry:widgets")["subject"], "industry:widgets")

    def test_research_task_gap_refusals_open_requests_and_keep_admissible_ones(self) -> None:
        from dalton_core.research_task import open_material_requests_for_gaps

        plan = {"plan_id": "research-plan:1", "inquiries": [self.INQUIRY, {**self.INQUIRY, "question": "Q2?"},
                                                            {**self.INQUIRY, "company_ref": None,
                                                             "question": "Industry pricing?"}]}
        entries = [{"ordinal": 0, "admissible": False, "reason": "no_bindable_template", "inquiry_ref": "i0"},
                   {"ordinal": 1, "admissible": True, "reason": None, "inquiry_ref": "i1"},
                   {"ordinal": 2, "admissible": False, "reason": "industry_inquiry_has_no_company_probe",
                    "inquiry_ref": "i2"},
                   {"ordinal": 0, "admissible": False, "reason": "out_of_mandate_scope", "inquiry_ref": "i0"}]
        out = open_material_requests_for_gaps(self.state, plan, entries, industry_ref="industry:widgets", now=NOW)
        self.assertEqual([row["inquiry_ref"] for row in out], ["i0", "i2"])
        self.assertTrue(all(row["write_status"] == "fresh" for row in out))
        again = open_material_requests_for_gaps(self.state, plan, entries, industry_ref="industry:widgets", now=NOW)
        self.assertEqual([row["write_status"] for row in again], ["duplicate", "duplicate"])
        subjects = sorted(v["subject"] for v in mr.open_requests(self.state))
        self.assertEqual(subjects, ["company:ticker:synw", "industry:widgets"])

    def test_industry_request_skips_company_only_sources(self) -> None:
        req = self.request(subject="industry:widgets", need={"kind": "answer", "question": "Industry pricing?"},
                           source_priority=["alphaengine"])
        result = ma.serve(self.state, req["id"], rungs={"alphaengine": ma.TargetedSourceRung("alphaengine")},
                          now=NOW)
        self.assertEqual(result["outcome"], "exhausted")
        self.assertEqual(self.attempts(req["id"])[0]["outcome"], "not_applicable")

    def test_repair_targets_convert_only_missing_material(self) -> None:
        missing = {"id": "dossier-repair-target:1", "unit": "industry", "code": "missing_evidence",
                   "detail": "No source gives the segment growth for FY26."}
        numbers = {"id": "dossier-repair-target:2", "check": "numbers_repair", "section": "x", "figure": "970"}
        opened = ma.open_for_repair_target(self.state, SYNW, missing, now=NOW)
        self.assertEqual((opened["need"]["kind"], opened["requesters"][0]["lane"]), ("answer", "company_dossier"))
        self.assertIsNone(ma.open_for_repair_target(self.state, SYNW, numbers, now=NOW))

    def test_dossier_feedback_sweep_converts_once(self) -> None:
        feedback = {SYNW: {"id": "dossier-repair-feedback:1", "published_version_ref": "dossier-version:1",
                           "material": {"stable_repair_target_keys": []},
                           "repair_targets": [{"id": "dossier-repair-target:1", "unit": "risks",
                                               "code": "missing_evidence", "detail": "No churn data.",
                                               "content_hash": "c" * 64}]}}
        with mock.patch("dalton_core.dossier_repair_feedback.read_dossier_repair_feedback", return_value=feedback):
            first = ma.convert_repair_targets(self.state, now=NOW)
            second = ma.convert_repair_targets(self.state, now=NOW)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_rewrite_flag_requests_official_replacement_material_first(self) -> None:
        flag = {"id": "product-rewrite-flag:1", "subject_ref": SYNW, "product_version_ref": "pv:1",
                "requires_material": [{"kind": "sec_exhibit"}, {"kind": "transcript"}, {"kind": "web_page"}]}
        opened = ma.open_for_rewrite_flag(self.state, flag, now=NOW)
        self.assertEqual(opened["need"]["doc_kinds"], ["sec_exhibit", "transcript"])
        self.assertEqual(opened["source_priority"][:2], ["indexed_corpus", "sec_edgar"])
        self.assertEqual(opened["requesters"][0]["lane"], "product_rewrite")

    def test_open_for_effect(self) -> None:
        doc = ma.open_for_effect(self.state, {"company_ref": SYNW, "doc_kinds": ["sec_exhibit"],
                                              "event_ref": "event:1", "period": "quarter_ending:2026-08-31"},
                                 now=NOW)
        self.assertEqual((doc["requesters"][0]["lane"], doc["requesters"][0]["run_ref"]),
                         ("event_judgement", "event:1"))
        data = ma.open_for_effect(self.state, {"subject_ref": SYNW, "data_item": "bookings", "lane": "ask",
                                               "deadline_hours": 2}, now=NOW)
        self.assertEqual(data["deadline"], later(hours=2).replace("+00:00", "+00:00")[:19] + "+00:00")
        self.assertEqual(ma.open_for_effect(self.state, {"subject_ref": SYNW, "data_item": "bookings",
                                                         "lane": "ask"}, now=NOW)["write_status"], "duplicate")
        # F's ledger-shaped effect (``event_judgement_result.gap_need``) passes through, digest unchanged.
        gap = {"requester": {"lane": "event_judgement", "product_ref": "event:9", "section": "body"},
               "subject": SYNW, "need": {"kind": "document", "doc_kinds": ["sec_exhibit"], "question": "read it"},
               "period": "event_document:doc:9", "acceptance": {"document_ref": "doc:9", "indexed": True},
               "kind": "event_body", "event_ref": "event:9"}
        opened = ma.open_for_effect(self.state, gap, now=NOW)
        direct = mr.find_request(self.state, subject=SYNW, need=gap["need"], period=gap["period"])
        self.assertEqual(opened["id"], direct["id"])
        self.assertFalse(ma.evaluate_acceptance(self.state, opened, now=NOW)["met"])
        for bad in ({"doc_kinds": ["x"]}, {"subject_ref": SYNW}, {"subject_ref": SYNW, "question": "q", "x": 1},
                    {"subject_ref": SYNW, "question": "q", "deadline_hours": 0}):
            with self.assertRaises(ma.MaterialAcquisitionError):
                ma.open_for_effect(self.state, bad, now=NOW)


# ---------------------------------------------------------------------------
# the lane
# ---------------------------------------------------------------------------


class LaneTests(Fixture):
    def test_tick_serves_a_bounded_slice_in_priority_order(self) -> None:
        planner = self.request(lane="research_planner", need={"kind": "answer", "question": "old question"},
                               now=later(hours=-5))
        other_p0 = self.request(lane="research_planner", subject=OTHER,
                                need={"kind": "answer", "question": "p0 question"}, now=later(hours=-1))
        drafting = self.request(lane="initial_screen", section="latest_transcript", now=later(hours=-0.5))
        served = []

        def serve(state_dir, request_id, **kwargs):
            served.append(request_id)
            return {"request_id": request_id, "outcome": "waiting"}

        with mock.patch.object(ma, "serve", side_effect=serve):
            summary = ma.tick(self.state, max_requests=2, now=NOW, convert=False)
        self.assertEqual(served, [drafting["id"], other_p0["id"]])
        self.assertEqual((summary["open"], summary["served"], summary["waiting"]), (3, 2, 2))
        self.assertNotIn(planner["id"], served)

    def test_tick_stops_costed_rungs_once_the_pool_is_spent(self) -> None:
        a = self.request(source_priority=["alphaengine"])
        b = self.request(need={"kind": "document", "doc_kinds": ["sec_10k"]}, source_priority=["alphaengine"])
        calls = []

        def serve(state_dir, request_id, *, allow_costed=True, **kwargs):
            calls.append(allow_costed)
            return {"request_id": request_id, "outcome": "budget_blocked", "budget": {"scope": "pool"}}

        with mock.patch.object(ma, "serve", side_effect=serve):
            ma.tick(self.state, max_requests=5, now=NOW, convert=False)
        self.assertEqual(calls, [True, False])
        self.assertTrue(a and b)

    def test_lane_is_registered_from_code_and_idle_on_a_blank_workspace(self) -> None:
        from dalton_core import lane_registry
        from dalton_core.material_acquisition_lane import DRIVER_KEY, OPERATION, dispatch

        self.assertIn("dalton_core.material_acquisition_lane", lane_registry.LANE_MODULES)
        spec = lane_registry.lane_for_operation(OPERATION)
        self.assertEqual((spec.order, spec.driver_key), (38, DRIVER_KEY))
        from dalton_core.budget_pools import pool_for_operation

        self.assertEqual(pool_for_operation(OPERATION), "coverage")
        with tempfile.TemporaryDirectory() as blank:
            result = dispatch(SimpleNamespace(state_dir=Path(blank)), {})
            self.assertEqual(result["status"], "idle")
            self.assertFalse((Path(blank) / mr.LEDGER_DB).exists())
        self.assertEqual(dispatch(SimpleNamespace(), {})["status"], "unconfigured")
        req = self.request(source_priority=["indexed_corpus"])
        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
        result = dispatch(SimpleNamespace(state_dir=self.state), {})
        self.assertEqual((result["status"], result["found"]), ("served", 1))
        self.assertEqual(mr.request(self.state, req["id"])["status"], "found")


# ---------------------------------------------------------------------------
# owner door and cockpit
# ---------------------------------------------------------------------------


class OwnerDoorTests(Fixture):
    def test_only_three_situations_reach_the_owner(self) -> None:
        held = self.request(need={"kind": "data", "data_item": "consensus_estimates"},
                            source_priority=["consensus_vendor"])
        with mock.patch.object(ma.LaneOwnedRung, "run", return_value=ma.RungResult("waiting_on_lane")):
            self.assertEqual(ma.serve(self.state, held["id"], now=NOW)["outcome"], "deferred")
        timed = self.request(need={"kind": "document", "doc_kinds": ["sec_10k"]}, deadline=later(hours=-1),
                             source_priority=["indexed_corpus"])
        self.assertEqual(ma.serve(self.state, timed["id"], now=NOW)["outcome"], "timeout")
        self.assertEqual(ma.owner_door_cards(self.state, now=NOW), [])
        gone = self.request(need={"kind": "document", "doc_kinds": ["expert_call"]}, source_priority=["web"])
        ma.serve(self.state, gone["id"], rungs={"web": FakeRung("web", ["empty"])}, now=NOW)
        cards = ma.owner_door_cards(self.state, now=NOW, company_labels={SYNW: "Synthetic Widgets"})
        self.assertEqual([c["kind"] for c in cards], ["material_sources_exhausted"])
        self.assertIn("已尝试", cards[0]["why"])
        self.assertTrue(cards[0]["title"].startswith("Synthetic Widgets："))

    def test_needs_human_collect_shows_the_cards_with_their_entries(self) -> None:
        from dalton_core import needs_human
        from dalton_core.terminology import banned_hits

        gone = self.request(need={"kind": "document", "doc_kinds": ["expert_call"]}, source_priority=["web"])
        ma.serve(self.state, gone["id"], rungs={"web": FakeRung("web", ["empty"])}, now=NOW)
        blocked = self.request(source_priority=["alphaengine"], budget={"max_calls": 1})
        ma.serve(self.state, blocked["id"], rungs={"alphaengine": FakeRung("alphaengine", [], calls=3)}, now=NOW)
        listing = needs_human.collect(state_dir=self.state, clock=lambda: datetime.fromisoformat(NOW))
        mine = {row["kind"]: row for row in listing["items"] if row["kind"].startswith("material_")}
        self.assertEqual(set(mine), {"material_sources_exhausted", "material_budget_exceeded"})
        budget = mine["material_budget_exceeded"]
        self.assertEqual(budget["entry"]["mode"], "acknowledge")
        choices = [c["choice"] for c in budget["entry"]["choices"]]
        self.assertEqual(choices[0], "approve_spend")
        self.assertIn("美元", budget["entry"]["choices"][0]["confirm"])
        for row in mine.values():
            for name in ("title", "why_blocked", "consequence", "where"):
                self.assertEqual(banned_hits(row[name]), [], (name, row[name]))

    def test_approve_spend_is_accepted_only_on_a_budget_card(self) -> None:
        from dalton_core.needs_human_ack import KIND_CHOICES, NeedsHumanAckAuthority, NeedsHumanAckError
        from dalton_core.store import DaltonStore

        self.assertIn("approve_spend", KIND_CHOICES["material_budget_exceeded"])
        store = DaltonStore(str(self.state / "ack-core.sqlite"))
        try:
            authority = NeedsHumanAckAuthority(store)
            receipt = authority.record(card_ref="material-budget:pool:2026-10-09:0.50:10",
                                       kind="material_budget_exceeded", choice="approve_spend",
                                       condition_hash="0" * 64, reason="继续", actor_ref="human:owner")
            self.assertEqual(receipt["status"], "recorded")
            with self.assertRaises(NeedsHumanAckError):
                authority.record(card_ref="fetch-cap:x", kind="material_budget_exceeded", choice="approve_spend",
                                 condition_hash="0" * 64, reason="继续", actor_ref="human:owner")
        finally:
            store.close()

    def test_an_existing_acknowledgement_table_is_widened_once_and_keeps_its_rows(self) -> None:
        from dalton_core.needs_human_ack import NeedsHumanAckAuthority, _migrate_choices
        from dalton_core.store import DaltonStore

        path = self.state / "old-core.sqlite"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE needs_human_acknowledgements (ack_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL "
                    "UNIQUE, card_ref TEXT NOT NULL, kind TEXT NOT NULL, choice TEXT NOT NULL CHECK(choice IN "
                    "('noted', 'snooze', 'abandon_document')), condition_hash TEXT NOT NULL, until_at TEXT, "
                    "document_ref TEXT, reason TEXT NOT NULL, actor_ref TEXT NOT NULL, idempotency_key TEXT UNIQUE, "
                    "record_json TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)")
        old.execute("INSERT INTO needs_human_acknowledgements VALUES ('a',1,'fetch-cap:x','fetch_failure_capped',"
                    "'noted',?,NULL,NULL,'ok','human:owner',NULL,'{}','h1',?)", ("0" * 64, NOW))
        old.commit()
        old.close()
        store = DaltonStore(str(path))
        try:
            authority = NeedsHumanAckAuthority(store)
            self.assertFalse(_migrate_choices(store.connection))
            rows = store.connection.execute("SELECT card_ref FROM needs_human_acknowledgements").fetchall()
            self.assertEqual([r[0] for r in rows], ["fetch-cap:x"])
            receipt = authority.record(card_ref="material-budget:request:material-request:1:0.50:3",
                                       kind="material_budget_exceeded", choice="approve_spend",
                                       condition_hash="1" * 64, reason="继续", actor_ref="human:owner")
            self.assertEqual(receipt["sequence"], 2)
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("DELETE FROM needs_human_acknowledgements")
        finally:
            store.close()

    def test_cockpit_material_requests_payload(self) -> None:
        from dalton_core.cockpit_v2 import material_requests_api as api
        from dalton_core.cockpit_v2.charts_api import ChartReader

        req = self.request(source_priority=["indexed_corpus", "web"])
        ma.serve(self.state, req["id"], rungs={"indexed_corpus": ma.IndexedCorpusRung(),
                                                "web": FakeRung("web", ["empty"])}, now=NOW)

        def get(state, **query):
            status, _, body = api.material_requests_routes()[api.PATH](
                query, ChartReader(Path(state) / "core.sqlite"))
            return status, json.loads(body)

        status, body = get(self.state, company="SYNW")
        self.assertEqual((status, body["status"]), (200, "ok"), body)
        item = body["requests"][0]
        # G's fields stay as the question page reads them ...
        self.assertEqual((item["id"], item["status"], item["resolved"]), (req["id"], "exhausted", False))
        self.assertEqual(item["need"]["kind"], "document")
        # ... and batch E's ladder rides along.
        self.assertEqual([a["source_label"] for a in item["attempts"]], ["已入库资料检索", "公开网页检索"])
        self.assertEqual(item["attempts"][0]["outcome_label"], "没有找到")
        self.assertIn("已入库资料检索（没有找到）", item["tried"])
        self.assertIn("已尝试", item["close_reason"])
        self.assertEqual((item["round"], item["requester_lanes"]), (1, ["initial_screen"]))
        self.assertEqual(body["pool"]["label"], "今日材料获取额度")
        status, by_id = get(self.state, ids=req["id"])
        self.assertEqual(by_id["requests"][0]["attempts"], item["attempts"])
        self.assertEqual(get(self.state, company="SYNW", status="bogus")[0], 400)
        self.assertEqual(get(self.state, company="SYNW", status="found")[1]["requests"], [])
        with tempfile.TemporaryDirectory() as blank:
            status, body = get(blank, company="SYNW")
            self.assertEqual((status, body["status"]), (200, "empty"))
            self.assertFalse((Path(blank) / mr.LEDGER_DB).exists())

    def test_found_requests_show_what_arrived(self) -> None:
        from dalton_core.cockpit_v2 import material_requests_api as api
        from dalton_core.cockpit_v2.charts_api import ChartReader

        self.corpus.add("transcript", "Synthetic Widgets Q4 2026", transcript("Q4", 2026), date="2026-09-25")
        req = self.request(period="quarter_ending:2026-08-31", acceptance={"published_after": "2026-08-31"})
        ma.serve(self.state, req["id"], rungs={"indexed_corpus": ma.IndexedCorpusRung()}, now=NOW)
        _, _, raw = api.material_requests_routes()[api.PATH]({"ids": req["id"]},
                                                              ChartReader(self.state / "core.sqlite"))
        item = json.loads(raw)["requests"][0]
        self.assertTrue(item["resolved"])
        self.assertTrue(item["arrived"][0]["document_ref"].startswith("test-doc:transcript"))

    def test_route_is_served_through_the_v2_router(self) -> None:
        from dalton_core.cockpit_v2 import material_requests_api as api
        from dalton_core.cockpit_v2.routes import register_v2_routes

        router = register_v2_routes(SimpleNamespace())
        self.assertIn(api.PATH, router.raw_gets)


class BlankWorkspaceTests(unittest.TestCase):
    def test_blank_workspace_canaries(self) -> None:
        with tempfile.TemporaryDirectory() as blank:
            state = Path(blank)
            self.assertEqual(ma.tick(state, now=NOW)["open"], 0)
            self.assertEqual(ma.serve(state, "material-request:" + "0" * 32, now=NOW)["outcome"], "missing")
            self.assertEqual(ma.owner_door_cards(state, now=NOW), [])
            self.assertEqual(ma.pending_targeted_queries(state, "source:alphaengine"), [])
            self.assertEqual(ma.material_arrived(state), [])
            self.assertFalse((state / mr.LEDGER_DB).exists())
            request = {"id": "material-request:x", "subject": SYNW,
                       "need": {"kind": "document", "doc_kinds": ["transcript"]}, "period": None}
            self.assertFalse(ma.evaluate_acceptance(state, request, now=NOW)["met"])
            self.assertEqual(ma.pool_status(state, now=NOW)["spent_calls"], 0)
            opened = ma.open_for_effect(state, {"subject_ref": SYNW, "question": "why?"}, now=NOW)
            result = ma.serve(state, opened["id"], rungs={"indexed_corpus": ma.IndexedCorpusRung()}, now=NOW)
            self.assertEqual(result["outcome"], "exhausted")


if __name__ == "__main__":
    unittest.main()
