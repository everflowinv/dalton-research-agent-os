"""One Ledger walk per snapshot, not per Claim; one snapshot per tick, not per company.

Live, the dossier lane's per-company fingerprint took 9-19 seconds each on
the writer's store thread once the Ledger held ten thousand Claims: every
claim's status walked the whole snapshot (ten million semantic keys), every
section re-read and re-hashed the whole Claim index, every company took and
hashed its own snapshot, and ``canonical_json`` serialized each node once per
ancestor.  The lane timed out every tick and the cockpit's decisions queued
behind it.  These tests pin the answers to the byte and the reads to the count.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import unittest
from collections.abc import Mapping
from unittest.mock import patch

from dalton_core.claim_index_authority import current_entries
from dalton_core.mission_dossier_lane import (
    MissionDossierLaneCoordinator, company_ledger_signature, permission_key,
)
from dalton_core.store import (
    ClaimStatusProjection, DaltonStore, NotFound, canonical_json,
)
from tests.test_dossier_lane import ACN, Harness


def legacy_canonical_json(value):
    """The implementation before the single-pass walk, kept as the oracle."""
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, Mapping):
        value = {str(k): v for k, v in value.items()}
        value = {k: json.loads(legacy_canonical_json(v)) for k, v in value.items()}
    elif isinstance(value, (tuple, list)):
        value = [json.loads(legacy_canonical_json(v)) for v in value]
    elif isinstance(value, set):
        value = sorted(value)
    elif hasattr(value, "value"):
        value = value.value
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def legacy_status_details(snapshot, claim_version_id):
    """The one-claim walk before ``ClaimStatusProjection``, kept as the oracle."""
    from dalton_core.store import _parse_rfc3339
    rows = list(snapshot.get("claim_versions", []))
    target = next((r for r in rows if r.get("claim_version_id") == claim_version_id), None)
    if target is None:
        raise NotFound(claim_version_id)
    times = [str(target["created_at"])]

    def projected(status):
        latest = max(_parse_rfc3339(v, "t") for v in times)
        return {"status": status, "updated_at": latest.isoformat(timespec="microseconds")}

    latest_refs = dict(snapshot.get("latest_claim_version_refs", {}))
    if latest_refs.get(target["claim_ref"]) != claim_version_id:
        replacement = next((r for r in rows if r.get("claim_version_id")
                            == latest_refs.get(target["claim_ref"])), None)
        if replacement is not None:
            times.append(str(replacement["created_at"]))
        return projected("superseded")
    document = target["claim"]
    if document.get("claim_kind") == "quantitative":
        key = DaltonStore._claim_semantic_key(document)
        conflict = False
        for other in rows:
            if other["claim_version_id"] == claim_version_id:
                continue
            if latest_refs.get(other["claim_ref"]) != other["claim_version_id"]:
                continue
            if (DaltonStore._claim_semantic_key(other["claim"]) == key
                    and not DaltonStore._claim_values_equal(
                        other["claim"].get("value"), document.get("value"))):
                times.append(str(other["created_at"]))
                conflict = True
        if conflict:
            return projected("contested")
    adjudications = [i for i in snapshot.get("latest_adjudications", [])
                     if i.get("claim_version_ref") == claim_version_id]
    if adjudications:
        times.append(str(adjudications[0]["created_at"]))
        return projected(str(adjudications[0]["adjudication"]["adjudicated_status"]))
    return projected("proposed")


class Color(enum.Enum):
    RED = "red"


class Rank(enum.IntEnum):
    ONE = 1


@dataclasses.dataclass
class Record:
    a: int
    b: tuple
    c: Color


class CanonicalJsonTests(unittest.TestCase):
    def test_single_pass_walk_produces_the_same_bytes(self):
        samples = [
            None, True, False, 0, 1, -1.5, "x", "中文 é", "", [], {},
            [1, (2, 3), {4: "four", True: "t", None: "n", 2.5: "f"}],
            {"z": {"y": [Color.RED, Rank.ONE, {"s"}, {"b", "a"}, (1, 2)]},
             "a": Record(1, (2, 3), Color.RED)},
            {1: "int key", "1": "text key"},
            10 ** 30, -0.0, 1e300, [[[[1]]]],
            {"created_at": "2026-09-17T06:00:00+00:00",
             "claim": {"value": "12.5", "period": {"start": "2026-01-01"}}},
        ]
        for value in samples:
            with self.subTest(value=repr(value)[:60]):
                self.assertEqual(canonical_json(value), legacy_canonical_json(value))

    def test_non_json_values_still_refuse(self):
        with self.assertRaises(TypeError):
            canonical_json({"a": frozenset({1})})
        with self.assertRaises(ValueError):
            canonical_json([float("nan")])


def _claim(claim_ref, version, *, kind="qualitative", value=None,
           metric="revenue", created_at):
    version_id = f"{claim_ref}:v{version}"
    return {
        "claim_version_id": version_id, "claim_ref": claim_ref, "version": version,
        "content_hash": "h", "created_at": created_at,
        "claim": {
            "id": version_id, "claim_ref": claim_ref, "subject_ref": ACN,
            "metric_or_aspect": metric, "period": {"start": "2026-01-01", "end": "2026-03-31"},
            "basis": "reported", "unit": "usd", "claim_kind": kind, "value": value,
            "normalized_statement": "s",
        },
    }


class ClaimStatusProjectionTests(unittest.TestCase):
    def setUp(self):
        rows = [
            _claim("A", 1, kind="quantitative", value=1, created_at="2026-09-01T00:00:00+00:00"),
            _claim("A", 2, kind="quantitative", value=2, created_at="2026-09-02T00:00:00+00:00"),
            # Same semantic key as A:v2, different value: A:v2 and B are contested.
            _claim("B", 1, kind="quantitative", value=3, created_at="2026-09-03T00:00:00+00:00"),
            # Same key and equal value: no conflict between C and D.
            _claim("C", 1, kind="quantitative", value="7.0", metric="margin",
                   created_at="2026-09-04T00:00:00+00:00"),
            _claim("D", 1, kind="quantitative", value=7, metric="margin",
                   created_at="2026-09-05T00:00:00+00:00"),
            # Qualitative with the contested key: never contested itself, but a
            # value of None disagrees with A:v2 -- the legacy walk counted it.
            _claim("E", 1, created_at="2026-09-06T00:00:00+00:00"),
            _claim("F", 1, kind="quantitative", value=9, metric="capex",
                   created_at="2026-09-07T00:00:00+00:00"),
        ]
        self.snapshot = {
            "claim_versions": rows,
            "latest_claim_version_refs": {
                "A": "A:v2", "B": "B:v1", "C": "C:v1", "D": "D:v1",
                "E": "E:v1", "F": "F:v1",
            },
            "latest_adjudications": [
                {"claim_version_ref": "F:v1", "created_at": "2026-09-08T00:00:00+00:00",
                 "adjudication": {"adjudicated_status": "corroborated"}},
                {"claim_version_ref": "F:v1", "created_at": "2026-09-09T00:00:00+00:00",
                 "adjudication": {"adjudicated_status": "retracted"}},
            ],
        }

    def test_projection_matches_the_one_claim_walk_for_every_claim(self):
        projection = ClaimStatusProjection(self.snapshot)
        seen = {}
        for row in self.snapshot["claim_versions"]:
            ref = row["claim_version_id"]
            expected = legacy_status_details(self.snapshot, ref)
            self.assertEqual(projection.details(ref), expected, ref)
            self.assertEqual(DaltonStore.project_claim_status_details(self.snapshot, ref), expected)
            self.assertEqual(DaltonStore.project_claim_status(self.snapshot, ref), expected["status"])
            seen[ref] = expected["status"]
        self.assertEqual(seen, {
            "A:v1": "superseded", "A:v2": "contested", "B:v1": "contested",
            "C:v1": "proposed", "D:v1": "proposed", "E:v1": "proposed",
            "F:v1": "corroborated",
        })
        self.assertEqual(projection.details("A:v1")["updated_at"],
                         "2026-09-02T00:00:00.000000+00:00")
        self.assertEqual(projection.details("A:v2")["updated_at"],
                         "2026-09-06T00:00:00.000000+00:00")

    def test_unknown_claim_is_not_found(self):
        with self.assertRaises(NotFound):
            ClaimStatusProjection(self.snapshot).details("Z:v1")
        with self.assertRaises(NotFound):
            DaltonStore.project_claim_status_details(self.snapshot, "Z:v1")

    def test_semantic_keys_are_computed_once_per_snapshot(self):
        projection = ClaimStatusProjection(self.snapshot)
        with patch.object(DaltonStore, "_claim_semantic_key",
                          wraps=DaltonStore._claim_semantic_key) as keyed:
            for row in self.snapshot["claim_versions"]:
                projection.status(row["claim_version_id"])
        latest = len(self.snapshot["latest_claim_version_refs"])
        quantitative_latest = 5
        self.assertEqual(keyed.call_count, latest + quantitative_latest)


class CurrentEntriesFilterTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.connection = self.harness.store.connection
        self.claims = [
            self.harness.tag("t-1", "business_model", statement="一"),
            self.harness.tag("t-2", "segments_and_mix", statement="二"),
            self.harness.tag("t-3", "guidance_style", statement="三"),
        ]

    def test_wanted_refs_are_answered_by_sqlite_and_match_the_full_read(self):
        everything = current_entries(self.connection)
        self.assertTrue(set(claim["claim_version_id"] for claim in self.claims) <= set(everything))
        wanted = [self.claims[0]["claim_version_id"], self.claims[2]["claim_version_id"], "claim-version:absent"]
        statements = []
        self.connection.set_trace_callback(statements.append)
        try:
            subset = current_entries(self.connection, claim_version_refs=wanted)
        finally:
            self.connection.set_trace_callback(None)
        self.assertEqual(subset, {ref: everything[ref] for ref in wanted[:2]})
        index_reads = [sql for sql in statements if "claim_index_entry_versions v" in sql]
        self.assertEqual(len(index_reads), 1)
        self.assertIn("claim_version_ref IN (", index_reads[0])

    def test_no_wanted_refs_reads_nothing(self):
        statements = []
        self.connection.set_trace_callback(statements.append)
        try:
            self.assertEqual(current_entries(self.connection, claim_version_refs=[]), {})
        finally:
            self.connection.set_trace_callback(None)
        self.assertFalse([sql for sql in statements if "claim_index_entry_versions v" in sql])

    def test_subject_filter_still_applies_with_wanted_refs(self):
        wanted = [claim["claim_version_id"] for claim in self.claims]
        self.assertEqual(
            current_entries(self.connection, claim_version_refs=wanted,
                            subject_ref="company:sec-cik:0000000000"),
            {})
        self.assertEqual(
            set(current_entries(self.connection, claim_version_refs=wanted,
                                subject_ref=ACN)),
            set(wanted))
        self.assertEqual(
            set(current_entries(self.connection, subject_ref=ACN, claim_version_refs=wanted[:1])),
            set(wanted[:1]))


class DossierLaneSnapshotTests(unittest.TestCase):
    class Launcher:
        model_config_path = None

        def __init__(self):
            self.started = []

        def start(self, **kwargs):  # pragma: no cover - the test keeps every company quiet
            self.started.append(kwargs)
            raise AssertionError("a quiet company must not be launched")

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.connection = self.harness.store.connection

    def test_one_snapshot_serves_every_company_of_a_tick(self):
        other = "company:sec-cik:0000000002"
        launcher = self.Launcher()
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher,
            companies=lambda: [ACN, other], mission=lambda: None,
            failure_ledger_dir=self.harness.state_dir)
        # The signatures the lane will compute, made quiet ahead of time so the
        # tick walks both companies instead of launching the first.
        for company in (ACN, other):
            evidence = f"{company}|{company_ledger_signature(self.connection, company)}"
            coordinator._quiet_signatures.add(
                permission_key(self.connection, launcher, evidence))

        original = DaltonStore.claim_index_snapshot
        calls = []

        def counted(store, *args, **kwargs):
            calls.append(store)
            return original(store, *args, **kwargs)

        with patch.object(DaltonStore, "claim_index_snapshot", counted):
            result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["companies"], [ACN, other])
        self.assertEqual(len(calls), 1)
        self.assertEqual(launcher.started, [])

    def test_shared_snapshot_yields_the_same_signature(self):
        from dalton_core.company_dossier_cli import _ReadOnlyStoreView
        snapshot = _ReadOnlyStoreView(self.connection).claim_index_snapshot()
        self.assertEqual(
            company_ledger_signature(self.connection, ACN, snapshot=snapshot),
            company_ledger_signature(self.connection, ACN))


if __name__ == "__main__":
    unittest.main()
