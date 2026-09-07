"""P10a: the mission walks the Playbook's stages, and the lanes follow its gaps."""

from __future__ import annotations

import unittest

from dalton_core.coverage_mission import CoverageMissionAuthority, CoverageMissionConflict
from dalton_core.mission_stage import (
    FIRST_STAGE,
    MissionStageDriver,
    acquisition_needs,
    company_priority_order,
    discovery_needs,
    evaluate_mission,
    planned_spec_refs,
    review_sort_key,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params, OWNER

ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
EPAM = "company:sec-cik:0001352010"
AUTOMATION = "automation:coverage-mission"
TRANSCRIPTS = "earnings-call-transcripts"
REPORTS = "sell-side-reports"
PLANNED = {TRANSCRIPTS, REPORTS}


class StageHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for entry in params["source_plan"]:
            if entry["source_ref"] in {"source:alphaengine", "source:sec-edgar"}:
                entry["status"] = "connected"
        self.params = params
        ref = params.pop("mission_ref")
        self.mission_ref = ref
        self.mission = self.missions.create_mission(ref, **params)
        self._seq = 0

    # -- fixtures written straight into the mission's own tables ---------------
    def discovery(self, company_ref: str, spec_ref: str, *, source_ref: str = "source:alphaengine") -> str:
        self._seq += 1
        record_id = f"mission-source-discovery:{self._seq:032d}"
        # The tables refuse writes from anywhere but the authority; a projection
        # fixture borrows the authority's own transaction rather than the guard.
        with self.missions._transaction() as cur:
            cur.execute(
            "INSERT INTO coverage_mission_source_discoveries(record_id,mission_version_ref,mission_version_hash,"
            "company_ref,source_ref,discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,"
            "connector_invocation_ref,source_envelope_ref,source_envelope_hash,actor_ref,requested_by,"
            "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, self.mission["id"], self.mission["content_hash"], company_ref, source_ref,
             "discovery-plan:test:1", "0" * 64, spec_ref, "1" * 64, f"connector-invocation:{self._seq}",
             f"source-envelope:{self._seq}", "2" * 64, AUTOMATION, AUTOMATION, "{}", "3" * 64,
             f"2026-09-0{1 + self._seq % 9}T00:00:00+00:00"),
            )
        return record_id

    def document(self, company_ref: str, spec_ref: str, status: str, *, read: bool = False,
                 source_ref: str = "source:alphaengine", created_at: str | None = None) -> str:
        discovery_ref = self.discovery(company_ref, spec_ref, source_ref=source_ref)
        self._seq += 1
        record_id = f"mission-discovered-document:{self._seq:032d}"
        document_ref = f"alphaengine-doc:{self._seq:012d}"
        at = created_at or f"2026-09-05T00:00:{self._seq % 60:02d}+00:00"
        with self.missions._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_discovered_documents(record_id,mission_version_ref,company_ref,"
                "source_ref,document_ref,discovery_ref,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, self.mission["id"], company_ref, source_ref, document_ref, discovery_ref, status, at, at),
            )
            if read:
                cur.execute(
                    "INSERT INTO coverage_mission_document_reviews(review_id,mission_version_ref,company_ref,"
                    "source_ref,document_ref,discovered_document_ref,state,registered_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"mission-document-review:{self._seq:032d}", self.mission["id"], company_ref, source_ref,
                     document_ref, record_id, "extraction_staged", AUTOMATION, at, at),
                )
        return document_ref

    def evaluate(self, *, planned=PLANNED, state=None):
        return evaluate_mission(
            self.store.connection, self.missions.active_mission(self.mission_ref),
            planned_specs=planned, stage_state=state or {},
        )

    def item(self, companies, company_ref, item_ref):
        company = next(c for c in companies if c["company_ref"] == company_ref)
        return next(i for i in company["items"] if i["item_ref"] == item_ref)


class SourceBaseTests(StageHarness):
    def test_the_checklist_counts_the_playbook_readings_by_the_spec_that_found_them(self) -> None:
        for _ in range(4):
            self.document(ACN, TRANSCRIPTS, "acquired", read=True)
        self.document(ACN, REPORTS, "acquired")
        self.document(ACN, REPORTS, "discovered")
        self.document(ACN, REPORTS, "acquisition_failed")
        companies = self.evaluate()
        calls = self.item(companies, ACN, "earnings_calls")
        self.assertEqual((calls["status"], calls["have"], calls["read"], calls["required"]), ("complete", 4, 4, 4))
        reports = self.item(companies, ACN, "broker_research")
        self.assertEqual((reports["status"], reports["have"], reports["pending"], reports["failed"]), ("partial", 1, 1, 1))
        self.assertIn("还差 2 份", reports["note"])
        self.assertIn("排队等取", reports["note"])
        # An item with no discovery spec anywhere says so instead of looking merely missing.
        annual = self.item(companies, ACN, "annual_report")
        self.assertEqual(annual["status"], "not_planned")
        self.assertIn("搜索规格", annual["note"])
        # A company with nothing is missing, not complete, and the mission is not ready.
        untouched = next(c for c in companies if c["company_ref"] == EPAM)
        self.assertEqual({i["status"] for i in untouched["items"]} - {"not_planned"}, {"missing"})
        self.assertFalse(untouched["source_base_ready"])
        self.assertEqual(untouched["stage"], None)
        self.assertEqual(untouched["stage_label"], "还没开始")

    def test_a_source_that_is_not_connected_is_reported_as_unavailable_not_missing(self) -> None:
        params = dict(self.params)
        params["source_plan"] = [
            {**entry, "status": "not_connected"} if entry["source_ref"] == "source:alphaengine" else entry
            for entry in params["source_plan"]
        ]
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.missions.create_mission(self.mission_ref, **params)
        companies = self.evaluate()
        calls = self.item(companies, ACN, "earnings_calls")
        self.assertEqual(calls["status"], "source_unavailable")
        self.assertIn("还没有接入", calls["note"])
        self.assertIn("earnings_calls", next(c for c in companies if c["company_ref"] == ACN)["blocked_on"])

    def test_quarterly_financials_count_distinct_periods_of_quantitative_claims(self) -> None:
        self.assertEqual(self.item(self.evaluate(), ACN, "quarterly_financials")["have"], 0)
        rows = [
            (f"claim-version:q{index}", f"claim:q{index}", period, "1.0")
            for index, period in enumerate(
                ("2026-01-01..2026-03-31", "2025-10-01..2025-12-31", "2025-10-01..2025-12-31"))
        ]
        # A qualitative Claim (no value) is not a quarter of financials.
        rows.append(("claim-version:qualitative", "claim:qualitative", "2026-01-01..2026-03-31", "null"))
        with self.store._transaction() as cur:
            for version_id, claim_ref, period, value in rows:
                cur.execute(
                    "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                    "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (version_id, claim_ref, 1,
                     f'{{"subject_ref":"{ACN}","period":"{period}","value":{value}}}',
                     "4" * 64, "2026-09-01T00:00:00+00:00"),
                )
        financials = self.item(self.evaluate(), ACN, "quarterly_financials")
        self.assertEqual((financials["have"], financials["status"]), (2, "partial"))


class StageEntryTests(StageHarness):
    def driver(self, **kwargs) -> MissionStageDriver:
        return MissionStageDriver(self.missions, planned_specs=PLANNED, **kwargs)

    def test_every_company_enters_the_first_stage_once_under_the_mission_principal(self) -> None:
        result = self.driver().run_once()
        self.assertEqual(result["status"], "entered")
        entered = [item["company_ref"] for item in result["entered"]]
        self.assertEqual(entered, company_priority_order(self.missions.active_mission(self.mission_ref)))
        self.assertEqual(entered[0], ACN)  # the mission's own P0 company leads
        records = self.missions.stage_records(self.mission["id"])
        self.assertEqual({r["stage_ref"] for r in records}, {FIRST_STAGE})
        self.assertEqual({r["status"] for r in records}, {"entered"})
        self.assertEqual({r["actor_ref"] for r in records}, {AUTOMATION})
        # A second tick writes nothing and says so.
        again = self.driver().run_once()
        self.assertEqual((again["status"], again["entered"]), ("idle", []))
        self.assertEqual(len(self.missions.stage_records(self.mission["id"])), len(records))
        # The report carries the stage and the gaps the lanes should close.
        mission_report = again["missions"][0]
        self.assertEqual(mission_report["companies"][0]["stage"], FIRST_STAGE)
        self.assertIn("earnings_calls", mission_report["companies"][0]["gaps"])
        self.assertIn("annual_report", mission_report["companies"][0]["blocked_on"])

    def test_a_mission_that_does_not_grant_stage_records_is_reported_not_raised(self) -> None:
        params = dict(self.params)
        params["autonomy"] = {**params["autonomy"],
                              "may_write": [s for s in params["autonomy"]["may_write"] if s != "stage_record"]}
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.missions.create_mission(self.mission_ref, **params)
        result = self.driver().run_once()
        self.assertEqual((result["status"], result["entered"]), ("idle", []))
        self.assertEqual(len(result["skipped"]), len(self.mission["universe"]))
        self.assertIn("stage_record", result["skipped"][0]["reason"])
        self.assertEqual(self.missions.stage_records(self.missions.active_mission(self.mission_ref)["id"]), [])

    def test_a_human_checkpoint_stage_still_refuses_an_automation_gate_pass(self) -> None:
        self.driver().run_once()
        mission = self.missions.active_mission(self.mission_ref)
        self.missions.record_stage(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=ACN, stage_ref=FIRST_STAGE, status="gate_passed",
            evidence_refs=[mission["id"]], rationale="fixture", actor_ref=AUTOMATION,
            idempotency_key="fixture:initial-screen-pass",
        )
        with self.assertRaises(CoverageMissionConflict):
            self.missions.record_stage(
                mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
                company_ref=ACN, stage_ref="deep_insight_gate", status="gate_passed",
                evidence_refs=[mission["id"]], rationale="automation cannot pass a human gate",
                actor_ref=AUTOMATION, idempotency_key="fixture:deep-insight-pass",
            )
        state = {ACN: {FIRST_STAGE: ["entered", "gate_passed"]}}
        company = next(c for c in self.evaluate(state=state) if c["company_ref"] == ACN)
        self.assertEqual((company["stage"], company["stage_status"]), (FIRST_STAGE, "gate_passed"))
        self.assertEqual(company["stage_status_label"], "已通过")


class LaneOrderTests(StageHarness):
    def test_needs_put_the_priority_company_and_the_playbook_reading_order_first(self) -> None:
        self.document(EPAM, TRANSCRIPTS, "discovered")
        self.document(ACN, REPORTS, "discovered")
        self.document(ACN, TRANSCRIPTS, "discovered")
        companies = self.evaluate()
        needs = acquisition_needs(companies, source_ref="source:alphaengine")
        self.assertEqual([(n["company_ref"], n["spec_ref"]) for n in needs][:3],
                         [(ACN, TRANSCRIPTS), (ACN, REPORTS), (CTSH, TRANSCRIPTS)][:2] + [(EPAM, TRANSCRIPTS)])
        # Nothing discovered for a company means nothing to acquire; that gap is
        # for the discovery lane, and it is reported there instead.
        self.assertNotIn((CTSH, TRANSCRIPTS), [(n["company_ref"], n["spec_ref"]) for n in needs])
        discovery = discovery_needs(companies, source_ref="source:alphaengine")
        self.assertIn((CTSH, TRANSCRIPTS), [(n["company_ref"], n["spec_ref"]) for n in discovery])
        # A complete item asks for nothing.
        for _ in range(4):
            self.document(CTSH, TRANSCRIPTS, "acquired")
        after = discovery_needs(self.evaluate(), source_ref="source:alphaengine")
        self.assertNotIn((CTSH, TRANSCRIPTS), [(n["company_ref"], n["spec_ref"]) for n in after])

    def test_the_acquisition_lane_picks_the_needed_document_over_the_oldest_one(self) -> None:
        old = self.document(EPAM, REPORTS, "discovered", created_at="2026-08-01T00:00:00+00:00")
        wanted = self.document(ACN, TRANSCRIPTS, "discovered", created_at="2026-09-06T00:00:00+00:00")
        # Without needs the oldest row wins: this is what spent a live day of
        # governed calls on one company.
        self.assertEqual(self.missions.next_discovered_document(source_ref="source:alphaengine")["document_ref"], old)
        needs = [{"company_ref": n["company_ref"], "spec_ref": n["spec_ref"]}
                 for n in acquisition_needs(self.evaluate(), source_ref="source:alphaengine")]
        picked = self.missions.next_discovered_document(source_ref="source:alphaengine", preferred_needs=needs)
        self.assertEqual(picked["document_ref"], wanted)
        # A document no need names is still picked when nothing needed remains.
        with self.missions._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status='acquired' WHERE document_ref=?", (wanted,))
        self.assertEqual(
            self.missions.next_discovered_document(source_ref="source:alphaengine", preferred_needs=needs)["document_ref"],
            old,
        )

    def test_the_extraction_lane_reads_the_priority_company_and_the_original_first(self) -> None:
        specs = {"doc:acn-report": REPORTS, "doc:acn-call": TRANSCRIPTS, "doc:epam-call": TRANSCRIPTS}
        rank = {ref: index for index, ref in enumerate(company_priority_order(self.mission))}
        reviews = [
            {"review_id": "r1", "company_ref": EPAM, "document_ref": "doc:epam-call", "created_at": "2026-08-01"},
            {"review_id": "r2", "company_ref": ACN, "document_ref": "doc:acn-report", "created_at": "2026-08-02"},
            {"review_id": "r3", "company_ref": ACN, "document_ref": "doc:acn-call", "created_at": "2026-09-02"},
        ]
        ordered = sorted(reviews, key=lambda r: review_sort_key(r, company_rank=rank, spec_by_document=specs))
        self.assertEqual([r["review_id"] for r in ordered], ["r3", "r2", "r1"])

    def test_the_coordinator_reports_its_ordering_instead_of_swallowing_it(self) -> None:
        """Live, a wrong attribute silently disabled the ordering; the report caught it."""

        from dalton_core.mission_source_discovery import (
            MissionSourceDiscoveryCoordinator, build_discovery_plan,
        )

        self.document(ACN, TRANSCRIPTS, "discovered")
        plan = build_discovery_plan(
            plan_id="discovery-plan:test:1", created_at="2026-09-01T00:00:00.000000+00:00",
            mission_ref=self.mission_ref, companies={ACN: "Accenture ACN"},
            specs=[{"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes",
                    "query_template": "{terms} earnings call transcript", "lookback_days": 400,
                    "rediscovery_interval_days": 7, "retry_interval_days": 1}],
        )
        coordinator = MissionSourceDiscoveryCoordinator(
            store=self.store, missions=self.missions, plan=plan,
            search_launcher=None, acquisition_launcher=None,
        )
        needs, error = coordinator._stage_needs()
        self.assertIsNone(error)
        self.assertEqual(needs[0], {"company_ref": ACN, "spec_ref": TRANSCRIPTS})

    def test_document_spec_refs_maps_every_document_to_the_spec_that_found_it(self) -> None:
        call = self.document(ACN, TRANSCRIPTS, "acquired")
        report = self.document(CTSH, REPORTS, "discovered")
        mapping = self.missions.document_spec_refs(self.mission["id"])
        self.assertEqual((mapping[call], mapping[report]), (TRANSCRIPTS, REPORTS))

    def test_planned_spec_refs_reads_the_plans_the_writer_actually_loads(self) -> None:
        self.assertEqual(
            planned_spec_refs([
                {"specs": [{"spec_ref": TRANSCRIPTS}, {"spec_ref": REPORTS}]},
                {"specs": [{"spec_ref": "industry-demand"}]},
                {},
            ]),
            {TRANSCRIPTS, REPORTS, "industry-demand"},
        )


if __name__ == "__main__":
    unittest.main()
