"""P13y: a measure learned from a document that was not about this company.

Three passes read a window. Two of them refuse a document that never names the
company it was filed under; the metric-discovery pass -- added last -- did not,
and it was missed because it writes no claim.

That is the trap. Two observations make a requirement, and a requirement is
what the numeric pass then hunts, in this company's own filings, for as long as
it stands. 170 of the first 496 live observations came from documents the other
two passes had already refused.

The second failure here is separate and larger: one metric whose documents
disagreed about its unit used to raise, which discarded every other
observation the company had. IBM held 176 and got zero requirements because two
documents could not agree what net retention rate is measured in.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionNotFound,
)
from dalton_core.metric_discovery import contested, establish_requirements
from dalton_core.store import DaltonStore

COMPANY = "company:sec-cik:0001352010"
ACTOR = "automation:dalton-mission"
REASON = "document is not about this company"


def proposal(**overrides):
    base = {
        "metric_ref": "metric:new-bookings",
        "label": "new bookings",
        "unit": "currency",
        "evidence_phrase": "new bookings",
        "quote_id": "quote:0:200:aaaaaaaaaaaaaaaa",
        "document_ref": "alphaengine-doc:note-1",
        "citation_text": "We remain focused on new bookings this quarter.",
    }
    base.update(overrides)
    return base


class ContestedUnitTests(unittest.TestCase):
    """A disagreement about one metric is about one metric."""

    def observations(self):
        return [
            proposal(document_ref="doc:1"),
            proposal(document_ref="doc:2"),
            proposal(metric_ref="metric:net-retention-rate", label="net retention rate",
                     unit="percent", document_ref="doc:1"),
            proposal(metric_ref="metric:net-retention-rate", label="net retention rate",
                     unit="ratio", document_ref="doc:2"),
        ]

    def test_the_contested_metric_alone_is_left_out(self):
        established = establish_requirements(self.observations())
        self.assertEqual([item["metric_ref"] for item in established],
                         ["metric:new-bookings"])

    def test_a_conflict_no_longer_discards_the_whole_company(self):
        # The regression that mattered: this used to raise, and every caller
        # swallowed the exception and read it as "no requirements learned".
        # 176 observations, zero requirements, no error anywhere.
        many = self.observations() + [
            proposal(metric_ref=f"metric:m{i}", label=f"m{i}", document_ref=doc)
            for i in range(6) for doc in ("doc:1", "doc:2")
        ]
        self.assertEqual(len(establish_requirements(many)), 7)

    def test_contested_says_who_disagreed(self):
        # Absent from the requirement list reads as "nobody mentioned it",
        # which is the opposite of what happened.
        [item] = contested(self.observations())
        self.assertEqual(item["metric_ref"], "metric:net-retention-rate")
        self.assertEqual(item["units"], ["percent", "ratio"])
        self.assertEqual(item["cited_by"], {"percent": ["doc:1"], "ratio": ["doc:2"]})

    def test_agreement_is_not_contested(self):
        self.assertEqual(contested([proposal(document_ref="doc:1"),
                                    proposal(document_ref="doc:2")]), [])


class RetractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def record(self, *proposals):
        return self.authority.record_metric_observations(
            company_ref=COMPANY, proposals=list(proposals), observed_by=ACTOR)

    def observation_id(self, document_ref="alphaengine-doc:note-1"):
        row = self.store.connection.execute(
            "SELECT observation_id FROM coverage_mission_metric_observations "
            "WHERE document_ref=?", (document_ref,),
        ).fetchone()
        return row["observation_id"]

    def test_a_retracted_observation_is_not_read_back(self):
        self.record(proposal())
        self.record(proposal(document_ref="alphaengine-doc:note-2"))
        self.assertEqual(len(self.authority.metric_requirements(COMPANY)), 1)
        self.authority.retract_metric_observation(
            self.observation_id(), reason=REASON, retracted_by=ACTOR)
        self.assertEqual(len(self.authority.metric_observations(COMPANY)), 1)
        # One citation left, so the requirement it was corroborating is gone:
        # the numeric pass stops hunting a figure it was never owed.
        self.assertEqual(self.authority.metric_requirements(COMPANY), [])

    def test_the_reason_survives_the_retraction(self):
        self.record(proposal())
        self.authority.retract_metric_observation(
            self.observation_id(), reason=REASON, retracted_by=ACTOR)
        [row] = self.authority.retracted_metric_observations()
        self.assertEqual(row["reason"], REASON)
        self.assertEqual(row["retracted_by"], ACTOR)
        self.assertEqual(row["metric_ref"], "metric:new-bookings")
        self.assertEqual(row["company_ref"], COMPANY)

    def test_retracting_twice_is_not_a_second_retraction(self):
        self.record(proposal())
        first = self.authority.retract_metric_observation(
            self.observation_id(), reason=REASON, retracted_by=ACTOR)
        again = self.authority.retract_metric_observation(
            self.observation_id(), reason="something else", retracted_by=ACTOR)
        self.assertEqual(first["status_marker"], "fresh")
        self.assertEqual(again["status_marker"], "duplicate")
        self.assertEqual(again["reason"], REASON)
        self.assertEqual(len(self.authority.retracted_metric_observations()), 1)

    def test_an_unknown_observation_cannot_be_retracted(self):
        with self.assertRaises(CoverageMissionNotFound):
            self.authority.retract_metric_observation(
                "mission-metric-observation:nope", reason=REASON, retracted_by=ACTOR)

    def test_re_proposing_a_retracted_measure_does_not_revive_it(self):
        self.record(proposal())
        self.authority.retract_metric_observation(
            self.observation_id(), reason=REASON, retracted_by=ACTOR)
        result = self.record(proposal())
        self.assertEqual(result["recorded"], [])
        # Reported apart from a plain duplicate: "we decided this was wrong"
        # is a different answer from "we already have this".
        self.assertEqual(result["retracted"], ["metric:new-bookings"])
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(self.authority.metric_observations(COMPANY), [])

    def test_retractions_are_append_only(self):
        self.record(proposal())
        self.authority.retract_metric_observation(
            self.observation_id(), reason=REASON, retracted_by=ACTOR)
        for sql in ("UPDATE coverage_mission_metric_observation_retractions SET reason='x'",
                    "DELETE FROM coverage_mission_metric_observation_retractions"):
            with self.assertRaises(Exception):
                self.store.connection.execute(sql)

    def test_an_unauthorized_writer_cannot_retract(self):
        self.record(proposal())
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "INSERT INTO coverage_mission_metric_observation_retractions"
                "(observation_id,reason,retracted_by,retracted_at) VALUES(?,?,?,?)",
                (self.observation_id(), REASON, ACTOR, "2026-09-09T00:00:00+00:00"),
            )


class AuditScriptTests(unittest.TestCase):
    """The audit reads the verdicts the running system already reached."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def write_summary(self, name, payload):
        directory = self.state / "extractions" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_verdicts_are_collected_from_any_pass(self):
        from scripts.retract_unattributed_metric_observations import unattributed_reviews

        self.write_summary("a", {
            "numeric": [{"review_id": "review:1", "status": "not_attributed"},
                        {"review_id": "review:2", "status": "read"}],
            "discovery": [{"review_id": "review:1", "status": "read"}],
            "admitted": [],
            "created_at": "2026-09-09T00:00:00+00:00",
        })
        self.write_summary("b", {
            "qualitative": [{"review_id": "review:3", "status": "not_attributed"}],
        })
        self.assertEqual(unattributed_reviews(self.state),
                         {"review:1": 1, "review:3": 1})

    def test_a_missing_extractions_directory_is_not_a_crash(self):
        from scripts.retract_unattributed_metric_observations import unattributed_reviews

        self.assertEqual(unattributed_reviews(self.state / "nowhere"), {})

    def test_unreadable_summaries_are_skipped(self):
        from scripts.retract_unattributed_metric_observations import unattributed_reviews

        directory = self.state / "extractions" / "torn"
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(unattributed_reviews(self.state), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
