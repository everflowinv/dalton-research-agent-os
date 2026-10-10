"""ADR-0010: a judgement other than NO_CHANGE always leaves a governed follow-up.

Live legacy on 2026-09-30 held 15 THESIS_WEAKENED judgements and 0 candidates
and 0 proposals.  Three were ACN ``note`` / ``research`` decisions naming the
admitted theses (only ``revise_thesis`` could reach a candidate); twelve were
one EPAM ``revise_forecast`` group (one primary, eleven grouped aliases) that
published a forecast version directly and had no thesis to bind to.  These
tests pin the fix: the forward path and the backfill sweep share one call.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.event_judgement import (
    EventJudgementAuthority,
    EventJudgementValidationError,
    FOLLOWUP_NOTICE_KINDS,
    forecast_moved,
    record_judgement_followups,
    resolve_named_theses,
    sweep_judgement_followups,
)
from dalton_core.event_judgement_cli import run_followup_backfill, run_judgement
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.tracking_cadence import POLICY_PATH
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, OWNER
from tests.test_event_judgement import (
    JUDGE_ROUTE, PASS, VERIFIER_ROUTE, FakeModel, decision, resolver,
)
from tests.test_thesis_revision import RevisionHarness

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
VERIFIED = {"status": "verified", "verdict": "pass", "findings": []}


class FollowupHarness(RevisionHarness):
    """An admitted ACN thesis (ADR-0001), CTSH with none, and the grants."""

    def setUp(self):
        super().setUp()
        self.grant("forecast_revision_proposal")
        self.thesis_version = self.version_one["id"]
        self._n = 0

    def news(self, company_ref=ACN):
        self._n += 1
        return record_event(
            self.events, company_ref=company_ref, kind="news",
            occurred_at=f"2026-09-0{min(self._n, 9)}T1{self._n % 10}:00:00+00:00",
            source_refs=["source:alphaengine", f"alphaengine-doc:{self._n}"],
            payload={"document_ref": f"alphaengine-doc:{self._n}",
                     "source_ref": "source:alphaengine",
                     "spec_ref": "sell-side-reports", "discovery_ref": "d",
                     "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def judged(self, *, company_ref=ACN, word="THESIS_WEAKENED", action="note",
               thesis_refs=(), effect=None, forecast_change=None, event=None):
        event = event or self.news(company_ref)
        body = {
            "decision": word, "action": action, "driver_refs": [],
            "thesis_refs": list(thesis_refs),
            "because": "Bookings fell against a thesis that needs them to convert.",
            "citations": [event["id"]], "note": None, "research_question": None,
            "forecast_change": forecast_change,
            "model": {"work_order_ref": f"work:{event['id']}", "cost_micros": 10},
        }
        written = self.judgements.record(
            event=event, judgement=body, verification=VERIFIED,
            effect=effect or {"kind": action, "status": "queued", "reason": "legacy"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        return event, body, written

    def follow(self, event, body, written, **kwargs):
        return record_judgement_followups(
            self.judgements, judgement_ref=written["id"], event_ref=event["id"],
            company_ref=event["company_ref"], judgement=body,
            effect=written["effect"], mission=self.mission, actor_ref=AUTOMATION,
            **kwargs,
        )

    def count(self, table):
        return self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class ForwardFollowupTests(FollowupHarness):
    def test_a_weakened_note_that_keeps_the_thesis_is_a_no_revision_followup(self):
        # The ACN shape on live (2026-10-01): THESIS_WEAKENED with action
        # note/research, naming the admitted thesis, proposing no statement and
        # no confidence.  As a candidate its 「接受」 was refused (ADR-0008:
        # "says exactly what the current version says"); it is a governed
        # no-revision follow-up instead, and nothing waits on a person.
        event, body, written = self.judged(thesis_refs=[self.thesis_version])
        result = self.follow(event, body, written)
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["notice_ref"])
        self.assertEqual(self.count("thesis_revision_candidates"), 0)
        rows = self.judgements.no_revision_followups(ACN)
        self.assertEqual([row["id"] for row in rows], [result["no_revision_ref"]])
        self.assertEqual(rows[0]["followup_kind"], "no_revision_needed")
        self.assertTrue(rows[0]["label"].startswith("无需修订"))
        self.assertEqual(rows[0]["decision"], "THESIS_WEAKENED")
        self.assertEqual(
            [item["thesis_version_ref"] for item in rows[0]["maintained_theses"]],
            [self.thesis_version])
        self.assertEqual(rows[0]["maintained_theses"][0]["thesis_version_hash"],
                         self.version_one["content_hash"])
        self.assertEqual(self.revisions.undecided(ACN), [])
        self.assertEqual(self.judgements.followups_for(written["id"])["no_revision"],
                         [result["no_revision_ref"]])

    def test_a_judgement_that_proposes_a_confidence_is_a_decidable_candidate(self):
        event, body, written = self.judged(thesis_refs=[self.thesis_version])
        body = {**body, "proposed_confidence": "low"}
        result = self.follow(event, body, written)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["no_revision_ref"])
        candidate = self.revisions.candidate(result["candidates"][0])
        self.assertEqual(candidate["thesis_version_ref"], self.thesis_version)
        self.assertEqual(candidate["thesis_version_hash"], self.version_one["content_hash"])
        self.assertEqual(candidate["proposed_confidence"], "low")
        self.assertEqual(candidate["falsifier_ref"], "falsifier:x")
        # And it is decidable by the person, which is the whole of ADR-0007.
        self.assertIn(candidate["id"],
                      [item["id"] for item in self.revisions.undecided(ACN)])

    def test_the_follow_up_is_idempotent(self):
        event, body, written = self.judged(thesis_refs=[self.thesis_version])
        first = self.follow(event, body, written)
        second = self.follow(event, body, written)
        self.assertEqual(first["no_revision_ref"], second["no_revision_ref"])
        self.assertEqual(self.count("judgement_no_revision_followups"), 1)
        self.assertEqual(self.count("thesis_revision_candidates"), 0)

    def test_no_admitted_thesis_leaves_one_labelled_needs_thesis_notice(self):
        event, body, written = self.judged(company_ref=CTSH, action="research")
        result = self.follow(event, body, written)
        self.assertEqual(result["candidates"], [])
        notice = self.judgements.followup_notices(CTSH)
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["id"], result["notice_ref"])
        self.assertEqual(notice[0]["notice_kind"], "needs_thesis")
        self.assertTrue(notice[0]["label"].startswith("需要论点"))
        self.assertIn("none is admitted", notice[0]["reason"])
        self.assertEqual(notice[0]["judgement_ref"], written["id"])
        self.follow(event, body, written)
        self.assertEqual(self.count("judgement_followup_notices"), 1)

    def test_a_revise_forecast_with_a_model_that_did_not_move_becomes_a_proposal(self):
        change = {"driver_ref": "driver:revenue", "period_end": "2026-12-31",
                  "value": "0.0", "because": "Guidance midpoint implies flat."}
        event, body, written = self.judged(
            company_ref=CTSH, action="revise_forecast", forecast_change=change,
            effect={"kind": "revise_forecast", "status": "proposed",
                    "reason": "the mission does not grant forecast_line writes"})
        result = self.follow(event, body, written,
                             model_version_ref="forecast-model-version:ctsh:3")
        self.assertIsNotNone(result["proposal_ref"])
        self.assertIsNone(result["notice_ref"])
        proposal = self.judgements.forecast_proposals(CTSH)[0]
        self.assertEqual(proposal["model_version_ref"], "forecast-model-version:ctsh:3")
        self.assertEqual(proposal["proposed_value"], "0.0")
        self.assertEqual(proposal["checkpoint_kind"], "forecast_overturn")

    def test_a_revise_forecast_with_no_model_and_no_thesis_needs_a_thesis(self):
        change = {"driver_ref": "driver:revenue", "period_end": "2026-12-31",
                  "value": "0.0", "because": "b"}
        event, body, written = self.judged(
            company_ref=CTSH, action="revise_forecast", forecast_change=change,
            effect={"kind": "revise_forecast", "status": "queued",
                    "reason": "this company has no driver model to revise"})
        result = self.follow(event, body, written, model_version_ref=None)
        self.assertIsNone(result["proposal_ref"])
        notice = self.judgements.followup_notices(CTSH)[0]
        self.assertEqual(notice["notice_kind"], "needs_thesis")
        self.assertIn("no company model", notice["reason"])

    def test_a_forecast_the_judgement_already_moved_is_its_follow_up(self):
        # The EPAM shape on live: forecast_line granted, v9 published.
        effect = {"kind": "revise_forecast", "status": "fresh",
                  "model_version_ref": "forecast-model-version:epam:9",
                  "change_reason": "driver_event", "decision": "THESIS_WEAKENED"}
        self.assertTrue(forecast_moved(effect))
        change = {"driver_ref": "driver:revenue", "period_end": "2026-09-30",
                  "value": "0.0", "because": "b"}
        event, body, written = self.judged(
            company_ref=CTSH, action="revise_forecast", forecast_change=change,
            effect=effect)
        result = self.follow(event, body, written,
                             model_version_ref="forecast-model-version:epam:9")
        self.assertEqual(result["forecast_version_ref"], "forecast-model-version:epam:9")
        self.assertIsNone(result["proposal_ref"])
        self.assertIsNone(result["notice_ref"])
        self.assertEqual(self.count("judgement_followup_notices"), 0)

    def test_no_change_owes_nothing(self):
        event, body, written = self.judged(word="NO_CHANGE", action="no_change")
        self.assertEqual(self.follow(event, body, written)["status"], "not_owed")

    def test_a_named_version_a_person_has_since_revised_is_superseded_not_a_candidate(self):
        event, body, written = self.judged(thesis_refs=[self.thesis_version])
        # The owner revises the thesis after the judgement (live: ACN v1 -> v2
        # on 2026-09-14, after two of the three ACN judgements).
        other = self.candidate(suffix="8", statement="Weaker than we said.")
        self.revisions.decide(candidate_ref=other["id"], candidate_hash=other["content_hash"],
                              verdict="accept", reason="It fired.", actor_ref=OWNER)
        live, superseded = resolve_named_theses(
            self.store.connection, ACN, [self.thesis_version])
        self.assertEqual((live, superseded), ([], [self.thesis_version]))
        result = self.follow(event, body, written)
        self.assertEqual(result["candidates"], [])
        notice = self.judgements.followup_notices(ACN)[0]
        self.assertEqual(notice["notice_kind"], "thesis_superseded")
        self.assertIn(self.thesis_version, notice["reason"])

    def test_without_the_candidate_grant_only_the_proposal_is_written(self):
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = [w for w in autonomy["may_write"]
                                 if w != "thesis_revision_candidate"]
        params["autonomy"] = autonomy
        params.update({"version_id": "coverage-mission-version:us-it-services:40",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:40"})
        ungranted = self.missions.create_mission(self.mission_ref, **params)
        event, body, written = self.judged(company_ref=CTSH, action="research")
        result = record_judgement_followups(
            self.judgements, judgement_ref=written["id"], event_ref=event["id"],
            company_ref=CTSH, judgement=body, effect=written["effect"],
            mission=ungranted, actor_ref=AUTOMATION)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.count("judgement_followup_notices"), 0)

    def test_a_notice_is_append_only(self):
        event, body, written = self.judged(company_ref=CTSH, action="research")
        self.follow(event, body, written)
        for statement in ("UPDATE judgement_followup_notices SET decision='x'",
                          "DELETE FROM judgement_followup_notices"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(statement)

    def test_a_no_change_notice_is_refused_by_contract(self):
        with self.assertRaises(EventJudgementValidationError):
            self.judgements.record_followup_notice(
                judgement_ref="event-judgement:x", company_ref=CTSH,
                notice_kind="needs_thesis", decision="NO_CHANGE", action="note",
                because="b", reason="r", evidence_refs=["e"], named_thesis_refs=[],
                admitted_thesis_refs=[], model_version_ref=None,
                mission=self.mission, actor_ref=AUTOMATION)
        self.assertEqual(FOLLOWUP_NOTICE_KINDS, ("needs_thesis", "thesis_superseded"))


class BackfillSweepTests(FollowupHarness):
    """The live ledger's shapes, written the way the pre-fix code wrote them."""

    def legacy_ledger(self):
        acn_event, _body, acn = self.judged(
            action="research", thesis_refs=[self.thesis_version],
            effect={"kind": "research", "status": "queued",
                    "reason": "no_bindable_template"})
        ctsh_event, _body, ctsh = self.judged(
            company_ref=CTSH, action="note",
            effect={"kind": "note", "status": "fresh"})
        # A primary that already moved the forecast, and one grouped alias.
        moved_event, _body, moved = self.judged(
            company_ref=CTSH, action="revise_forecast",
            forecast_change={"driver_ref": "driver:revenue", "period_end": "2026-09-30",
                             "value": "0.0", "because": "b"},
            effect={"kind": "revise_forecast", "status": "fresh",
                    "model_version_ref": "forecast-model-version:ctsh:9"})
        _alias_event, _body, alias = self.judged(
            company_ref=CTSH, action="revise_forecast",
            effect={"kind": "grouped_judgement", "status": "recorded",
                    "primary_event_ref": moved_event["id"]})
        self.judged(word="NO_CHANGE", action="no_change")
        return acn, ctsh, moved, alias

    def test_dry_run_reports_and_writes_nothing(self):
        acn, ctsh, _moved, _alias = self.legacy_ledger()
        report = sweep_judgement_followups(self.judgements, self.mission,
                                           actor_ref=AUTOMATION, apply=False)
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual([item["judgement_ref"] for item in report["owed"]],
                         [acn["id"], ctsh["id"]])
        self.assertEqual(report["owed"][0]["current_thesis_refs"], [self.thesis_version])
        self.assertEqual((report["examined"], report["grouped"], report["satisfied"]),
                         (4, 1, 1))
        self.assertEqual(self.count("thesis_revision_candidates"), 0)
        self.assertEqual(self.count("judgement_followup_notices"), 0)

    def test_apply_backfills_once_and_a_second_pass_is_a_no_op(self):
        acn, ctsh, _moved, _alias = self.legacy_ledger()
        report = sweep_judgement_followups(self.judgements, self.mission,
                                           actor_ref=AUTOMATION, apply=True)
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["written"]), 2)
        self.assertEqual(self.judgements.followups_for(acn["id"])["no_revision"],
                         [report["written"][0]["no_revision_ref"]])
        self.assertEqual(len(self.judgements.followups_for(ctsh["id"])["notices"]), 1)
        again = sweep_judgement_followups(self.judgements, self.mission,
                                          actor_ref=AUTOMATION, apply=True)
        self.assertEqual((again["owed"], again["written"], again["satisfied"]),
                         ([], [], 3))
        self.assertEqual(self.count("thesis_revision_candidates"), 0)
        self.assertEqual(self.count("judgement_no_revision_followups"), 1)
        self.assertEqual(self.count("judgement_followup_notices"), 1)

    def test_a_revise_forecast_that_never_moved_proposes_against_todays_model(self):
        _event, _body, stuck = self.judged(
            company_ref=CTSH, action="revise_forecast",
            forecast_change={"driver_ref": "driver:revenue", "period_end": "2026-12-31",
                             "value": "0.01", "because": "b"},
            effect={"kind": "revise_forecast", "status": "refused",
                    "reason": "ForecastModelError: stale"})
        report = sweep_judgement_followups(
            self.judgements, self.mission, actor_ref=AUTOMATION,
            model_version_for=lambda ref: "forecast-model-version:ctsh:4", apply=True)
        self.assertEqual(report["written"][0]["proposal_ref"],
                         self.judgements.followups_for(stuck["id"])["proposals"][0])
        proposal = self.judgements.forecast_proposals(CTSH)[0]
        self.assertIn("stale", proposal["reason"])

    def test_the_owner_entry_point_is_read_only_unless_applied(self):
        self.legacy_ledger()
        self.store.connection.commit()
        dry = run_followup_backfill(state_dir=self.state_dir)
        self.assertEqual((dry["status"], dry["mode"], len(dry["owed"])), ("ok", "dry_run", 2))
        self.assertEqual(self.count("judgement_followup_notices"), 0)
        applied = run_followup_backfill(state_dir=self.state_dir, apply=True)
        self.assertEqual((applied["mode"], len(applied["written"])), ("apply", 2))
        self.assertEqual(run_followup_backfill(state_dir=self.state_dir)["owed"], [])


class LaneFollowupTests(FollowupHarness):
    """Through the judgement child, which is what a deploy actually runs."""

    def setUp(self):
        super().setUp()
        self.pass_screen(ACN)
        self.pass_screen(CTSH)

    def run_child(self, replies):
        return run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=FakeModel(replies, route=JUDGE_ROUTE),
            verifier_model=FakeModel([{**PASS, **({"direction_checks": [
                {"thesis_version_ref": ref, "relation": "same", "reason": "Confidence changes while the directional proposition is retained."}
                for ref in reply.get("thesis_refs") or ()]} if reply.get("proposed_statement") else {})}
                for reply in replies], route=VERIFIER_ROUTE),
            family_resolver=resolver())

    def test_a_revise_thesis_decision_records_its_candidate(self):
        # Before the fix this raised KeyError('id'): company_theses names a
        # version by ``ref`` and the candidate binds by ``id``.
        event = self.news(ACN)
        summary = self.run_child([decision(
            action="revise_thesis", word="THESIS_WEAKENED",
            thesis_refs=[self.thesis_version], citations=[event["id"]])])
        self.assertEqual(summary["status"], "succeeded", summary.get("failure_reason"))
        effect = next(item for item in summary["effects"] if item.get("judgement_ref"))
        self.assertEqual(len(effect["candidates"]), 1)
        self.assertEqual(self.count("thesis_revision_candidates"), 1)

    def test_a_weakened_note_on_a_company_with_no_thesis_leaves_a_notice(self):
        event = self.news(CTSH)
        summary = self.run_child([decision(
            action="note", word="THESIS_WEAKENED", citations=[event["id"]],
            note="客户削减可选支出，需求触底尚未确认。")])
        self.assertEqual(summary["status"], "succeeded", summary.get("failure_reason"))
        effect = next(item for item in summary["effects"] if item.get("judgement_ref"))
        self.assertEqual(effect["followup"]["status"], "recorded")
        notice = self.judgements.followup_notices(CTSH)
        self.assertEqual([item["notice_kind"] for item in notice], ["needs_thesis"])

    def test_the_next_tick_backfills_a_judgement_written_before_the_fix(self):
        self.judged(action="research", thesis_refs=[self.thesis_version])
        self.store.connection.commit()
        summary = self.run_child([])
        self.assertEqual(summary["judgement_status"], "nothing_unjudged")
        sweep = summary["followup_sweep"]
        self.assertEqual((sweep["status"], sweep["mode"], len(sweep["written"])),
                         ("ok", "apply", 1))
        self.assertEqual(self.count("thesis_revision_candidates"), 0)
        self.assertEqual(self.count("judgement_no_revision_followups"), 1)
        self.assertEqual(self.run_child([])["followup_sweep"]["written"], [])

    def test_a_dry_run_tick_only_reports_what_it_would_backfill(self):
        self.judged(action="research", thesis_refs=[self.thesis_version])
        self.store.connection.commit()
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW, dry_run=True)
        self.assertEqual(summary["followup_sweep"]["mode"], "dry_run")
        self.assertEqual(len(summary["followup_sweep"]["owed"]), 1)
        self.assertEqual(self.count("thesis_revision_candidates"), 0)


class WeeklyBriefFollowupTests(unittest.TestCase):
    """The brief is the end of ADR-0010's path; it lists what the path left."""

    def setUp(self):
        from tests.test_weekly_brief import WeeklyBriefAuthorityTests

        brief = WeeklyBriefAuthorityTests(
            methodName="test_first_issue_is_baseline_not_fake_weekly_delta_and_replays")
        brief.setUp()
        self.addCleanup(brief.doCleanups)
        self.brief = brief
        self.store = brief.fixture.store
        self.company = brief.fixture.authority.industry_brief_snapshot(
            brief.pack["id"], [brief.overlay["id"]])["coverage_universe"][0]["company_ref"]
        self.judgements = EventJudgementAuthority(self.store)
        self.mission = {"id": "coverage-mission-version:x:1", "content_hash": "c" * 64,
                        "autonomy": {"automation_principal": AUTOMATION,
                                     "may_write": ["thesis_revision_candidate"],
                                     "human_checkpoints": ["thesis_revision_candidate"]}}

    def notice(self):
        event = {"id": "research-event:brief", "content_hash": "e" * 64, "kind": "news",
                 "evidence_tier": "secondary", "company_ref": self.company}
        body = {"decision": "THESIS_WEAKENED", "action": "research", "driver_refs": [],
                "thesis_refs": [], "because": "Clients cut discretionary spend.",
                "citations": [event["id"]]}
        written = self.judgements.record(
            event=event, judgement=body, verification=VERIFIED,
            effect={"kind": "research", "status": "queued"}, mission=self.mission,
            actor_ref=OWNER)
        return record_judgement_followups(
            self.judgements, judgement_ref=written["id"], event_ref=event["id"],
            company_ref=self.company, judgement=body, effect=written["effect"],
            mission=self.mission, actor_ref=OWNER)["notice_ref"]

    def publish(self, *, second=False, start, end, prior=None):
        params = self.brief.issue_params(second=second)
        params.update({"period_start": start.isoformat(timespec="seconds"),
                       "period_end": end.isoformat(timespec="seconds"),
                       "prior_version_ref": prior})
        brief_ref = params.pop("brief_ref")
        return self.brief.weekly.publish_issue(brief_ref, **params)

    def test_the_issue_lists_replays_and_renders_the_follow_up(self):
        notice_ref = self.notice()
        now = datetime.now(timezone.utc)
        issue = self.publish(start=now - timedelta(days=1), end=now + timedelta(days=1))
        self.assertEqual([item["ref"] for item in issue["judgement_followups"]],
                         [notice_ref])
        self.assertEqual(issue["judgement_followups"][0]["notice_kind"], "needs_thesis")
        replayed = self.brief.weekly.issue(issue["id"])
        self.assertEqual(replayed["judgement_followups"], issue["judgement_followups"])
        body = self.brief.weekly.render_markdown(issue["id"])["body"]
        self.assertIn("事件判断的后续", body)
        self.assertIn("需要论点", body)
        self.assertIn(notice_ref, body)
        later = self.publish(second=True, prior=issue["id"],
                             start=now + timedelta(days=1), end=now + timedelta(days=2))
        self.assertEqual(later["judgement_followups"], [])
        self.assertIn("本期没有非 NO_CHANGE 判断",
                      self.brief.weekly.render_markdown(later["id"])["body"])

    def test_a_kept_thesis_is_listed_as_no_revision_needed(self):
        event = {"id": "research-event:brief-kept", "content_hash": "e" * 64,
                 "kind": "news", "evidence_tier": "secondary",
                 "company_ref": self.company}
        body = {"decision": "THESIS_WEAKENED", "action": "note", "driver_refs": [],
                "thesis_refs": [], "because": "首选判断是维持现有论点。",
                "citations": [event["id"]]}
        written = self.judgements.record(
            event=event, judgement=body, verification=VERIFIED,
            effect={"kind": "note", "status": "queued"}, mission=self.mission,
            actor_ref=OWNER)
        kept = self.judgements.record_no_revision_followup(
            judgement_ref=written["id"], company_ref=self.company,
            decision="THESIS_WEAKENED", action="note", because=body["because"],
            evidence_refs=[event["id"]],
            maintained_theses=[{"ref": "thesis-version:kept", "content_hash": "d" * 64,
                                "thesis_ref": "thesis:kept", "confidence": "low"}],
            mission=self.mission, actor_ref=OWNER)
        now = datetime.now(timezone.utc)
        issue = self.publish(start=now - timedelta(days=1), end=now + timedelta(days=1))
        self.assertEqual([item["kind"] for item in issue["judgement_followups"]],
                         ["judgement_no_revision"])
        self.assertEqual(issue["judgement_followups"][0]["thesis_version_refs"],
                         ["thesis-version:kept"])
        replayed = self.brief.weekly.issue(issue["id"])
        self.assertEqual(replayed["judgement_followups"], issue["judgement_followups"])
        body_text = self.brief.weekly.render_markdown(issue["id"])["body"]
        self.assertIn("事件判断的后续", body_text)
        self.assertIn("无需修订", body_text)
        self.assertIn("维持 thesis-version:kept", body_text)
        self.assertIn(kept["id"], body_text)

    def test_an_issue_published_before_the_field_renders_without_it(self):
        from dalton_core.weekly_brief import WeeklyBriefAuthority

        now = datetime.now(timezone.utc)
        issue = self.publish(start=now - timedelta(days=1), end=now + timedelta(days=1))

        class Before(WeeklyBriefAuthority):
            def issue(self, version_id):
                record = dict(super().issue(version_id))
                record.pop("judgement_followups", None)
                return record

        old = Before(self.store, self.brief.fixture.authority)
        body = old.render_markdown(issue["id"])["body"]
        self.assertNotIn("事件判断的后续", body)
        self.assertIn("## 预测对账", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
