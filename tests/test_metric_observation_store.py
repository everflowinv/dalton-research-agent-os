"""P11q: what the market was seen calling a figure, kept as a record."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionValidationError,
)
from dalton_core.store import DaltonStore

COMPANY = "company:sec-cik:0001467373"
ACTOR = "automation:dalton-mission"


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


class MetricObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def record(self, *proposals, company_ref=COMPANY):
        return self.authority.record_metric_observations(
            company_ref=company_ref, proposals=list(proposals), observed_by=ACTOR,
        )

    def test_one_document_names_a_measure_but_does_not_require_it(self) -> None:
        # One note is an anecdote; the observation is kept either way, because
        # the second note may arrive tomorrow.
        self.assertEqual(self.record(proposal())["recorded"], ["metric:new-bookings"])
        self.assertEqual(len(self.authority.metric_observations(COMPANY)), 1)
        self.assertEqual(self.authority.metric_requirements(COMPANY), [])

    def test_a_second_document_makes_it_a_requirement(self) -> None:
        self.record(proposal())
        self.record(proposal(document_ref="alphaengine-doc:note-2"))
        [requirement] = self.authority.metric_requirements(COMPANY)
        self.assertEqual(requirement["metric_ref"], "metric:new-bookings")
        self.assertEqual(requirement["citation_count"], 2)
        # The requirement can say which documents asked for it, which is the
        # answer to "why is the system hunting this figure".
        self.assertEqual(len(requirement["cited_by"]), 2)

    def test_the_same_document_read_twice_cannot_corroborate_itself(self) -> None:
        self.record(proposal())
        again = self.record(proposal(evidence_phrase="new bookings"))
        self.assertEqual(again["recorded"], [])
        self.assertEqual(again["duplicates"], ["metric:new-bookings"])
        self.assertEqual(self.authority.metric_requirements(COMPANY), [])

    def test_one_company_does_not_learn_from_another(self) -> None:
        self.record(proposal())
        self.record(proposal(document_ref="alphaengine-doc:note-2"),
                    company_ref="company:sec-cik:0000051143")
        self.assertEqual(self.authority.metric_requirements(COMPANY), [])

    def test_an_observation_without_its_quote_is_refused(self) -> None:
        bare = proposal()
        bare.pop("citation_text")
        with self.assertRaises(CoverageMissionValidationError):
            self.record(bare)

    def test_a_malformed_proposal_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(Exception):
            self.record(proposal(), proposal(metric_ref="Not A Slug",
                                             document_ref="alphaengine-doc:note-2"))
        self.assertEqual(self.authority.metric_observations(COMPANY), [])

    def test_observations_cannot_be_edited_or_deleted(self) -> None:
        self.record(proposal())
        for statement in (
            "UPDATE coverage_mission_metric_observations SET label='x'",
            "DELETE FROM coverage_mission_metric_observations",
        ):
            with self.assertRaises(Exception):
                self.store.connection.execute(statement)

    def test_writing_outside_the_authority_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "INSERT INTO coverage_mission_metric_observations("
                "observation_id,company_ref,document_ref,metric_ref,label,unit,"
                "evidence_phrase,quote_id,citation_text,observed_by,created_at) "
                "VALUES('x',?,'d','metric:x','x','currency','x','q','c','a','t')",
                (COMPANY,),
            )


if __name__ == "__main__":
    unittest.main()
