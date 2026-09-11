"""P10p: the governed SEC filings index, end to end against Core authority."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_governance import ConnectorGovernance, build_governance_record
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.sec_filings_index import CAPABILITY_ID
from dalton_core.sec_filings_index_core import (
    SecFilingsIndexCore,
    SecFilingsIndexCoreError,
    validate_filings_index_spec,
)
from dalton_core.store import DaltonStore

OWNER = "human:lumos"
ISSUER = "0001467373"
SPEC = {
    "issuer": ISSUER, "form": "10-K",
    "date_from": "2025-02-14", "date_to": "2025-12-31", "limit": 10,
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def submissions_body() -> bytes:
    rows = [
        ("0001467373-25-000217", "10-K", "2025-10-10", "acn-20250831.htm", None),
        ("0001467373-25-000100", "8-K", "2025-06-02", "event.htm", None),
        ("0001467373-25-000001", "10-K", "2025-02-14", "older.htm", None),
    ]
    return json.dumps({"cik": ISSUER, "filings": {"recent": {
        "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows], "primaryDocument": [r[3] for r in rows],
        "amendmentOf": [r[4] for r in rows],
    }}}).encode()


class _Response:
    """The HTTP response shape PublicHttpTransport consumes."""

    status = 200
    reason = "OK"

    def __init__(self, body: bytes) -> None:
        self._body = body

    def getheaders(self):
        return [
            ("content-type", "application/json"),
            ("content-length", str(len(self._body))),
        ]

    def read(self, _amount=None):
        body, self._body = self._body, b""
        return body

    def close(self):
        return None


class CountingAdapter:
    """The real SEC adapter over a fixture transport; counts source reads."""

    def __init__(self, body: bytes, clock) -> None:
        from dalton_core.public_http_transport import PublicHttpTransport
        from dalton_core.sec_public_adapter import SecPublicHttpAdapter

        self.body = body
        self.calls = 0
        self.adapter = SecPublicHttpAdapter(
            transport=PublicHttpTransport(
                resolver=lambda _host, _port: ("93.184.216.34",),
                exchange=lambda *_a, **_k: _Response(self.body),
            ),
            clock=clock,
        )

    def __call__(self, request, raw_sink, credential_handle=None):
        self.calls += 1
        return self.adapter(request, raw_sink, credential_handle)


class Harness:
    def __init__(self, root: Path, *, governance=None) -> None:
        self.clock = Clock()
        self.core = DaltonStore(str(root / "core.sqlite"))
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            str(root / "scheduler.sqlite"), clock=self.clock,
            default_lease_seconds=30, max_lease_seconds=60,
        )
        governance = governance or ConnectorGovernance(
            build_governance_record(
                "sec-filings-index", approved_by=OWNER, status="approved"
            )
        )
        self.governance = governance
        self.catalog = CapabilityCatalog(
            str(root / "catalog.sqlite"), clock=self.clock,
            approval_resolver=governance.approval, policy_resolver=governance.policy,
        )
        self.spool = RawSpool(str(root / "spool"), max_total_bytes=50_000_000)
        self.adapter = CountingAdapter(submissions_body(), self.clock)
        self.index = SecFilingsIndexCore(
            store=self.core, connectors=self.connectors, observability=self.observability,
            journal=self.journal, scheduler=self.scheduler, catalog=self.catalog,
            spool=self.spool, governance=governance, adapter=self.adapter, clock=self.clock,
        )

    def close(self) -> None:
        self.catalog.close()
        self.scheduler.close()
        self.core.close()


class SecFilingsIndexCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.harness = Harness(Path(self.tmp.name))
        self.addCleanup(self.harness.close)

    def test_one_governed_read_lands_authority_and_names_the_filings(self) -> None:
        index = self.harness.index
        receipt = index.list_filings(index.build_request(SPEC))

        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(receipt["provider_calls"], 1)
        self.assertEqual(index.ensure_authorities()["rate_policy"]["limits"]["calls"], 200)
        self.assertIsNone(index.ensure_authorities()["rate_policy_compatibility"])
        # Authority actually persisted, not just an in-memory answer.
        self.assertTrue(receipt["connector_invocation_ref"])
        self.assertTrue(receipt["source_envelope_ref"])
        self.assertTrue(receipt["raw_artifact_version_ref"])

        # Only the 10-K filings, and each one named by a fetchable ref of the
        # shape the public web fetch lane already resolves.
        self.assertEqual(len(receipt["filings"]), 2)
        self.assertTrue(all(f["form"] == "10-K" for f in receipt["filings"]))
        self.assertEqual(
            receipt["filings"][0]["canonical_url"],
            "https://www.sec.gov/Archives/edgar/data/1467373/"
            "000146737325000217/acn-20250831.htm",
        )
        self.assertEqual(
            receipt["document_refs"], [f["url_ref"] for f in receipt["filings"]]
        )
        for ref in receipt["document_refs"]:
            self.assertTrue(ref.startswith("public-web-url:sha256:"))

    def test_the_8k_in_the_same_block_never_reaches_the_fetch_lane(self) -> None:
        index = self.harness.index
        receipt = index.list_filings(index.build_request(SPEC))
        urls = " ".join(f["canonical_url"] for f in receipt["filings"])
        self.assertNotIn("event.htm", urls)

    def test_a_replay_binds_the_same_authority_without_calling_the_source(self) -> None:
        index = self.harness.index
        request = index.build_request(SPEC)
        first = index.list_filings(request)
        self.assertEqual(self.harness.adapter.calls, 1)

        second = index.list_filings(request)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["provider_calls"], 0)
        # A replay that invented a second provider call would silently double
        # spend the source's rate budget.
        self.assertEqual(self.harness.adapter.calls, 1)
        self.assertEqual(
            second["source_envelope_ref"], first["source_envelope_ref"]
        )
        self.assertEqual(second["document_refs"], first["document_refs"])

    def test_existing_stricter_v1_is_reused_without_widening(self) -> None:
        from dalton_core.connector_quota_policy import governed_daily_quota
        lower = dict(governed_daily_quota("sec", "list_filings")); lower["daily_unit_limit"] = 50
        with patch("dalton_core.sec_filings_index_core.governed_daily_quota", return_value=lower):
            first = self.harness.index.ensure_authorities()
        self.assertEqual(first["rate_policy"]["limits"]["calls"], 50)
        replacement = SecFilingsIndexCore(
            store=self.harness.core, connectors=self.harness.connectors,
            observability=self.harness.observability, journal=self.harness.journal,
            scheduler=self.harness.scheduler, catalog=self.harness.catalog,
            spool=self.harness.spool, governance=self.harness.governance,
            adapter=self.harness.adapter, clock=self.harness.clock)
        recovered = replacement.ensure_authorities()
        self.assertEqual(recovered["rate_policy"]["limits"]["calls"], 50)
        self.assertEqual(recovered["rate_policy_compatibility"]["status"],
                         "configured_ceiling_not_activated")
        self.assertEqual(replacement.ensure_authorities(), recovered)

    def test_existing_policy_wider_than_current_ceiling_is_rejected(self) -> None:
        self.harness.index.ensure_authorities()
        from dalton_core.connector_quota_policy import governed_daily_quota
        lower = dict(governed_daily_quota("sec", "list_filings")); lower["daily_unit_limit"] = 50
        replacement = SecFilingsIndexCore(
            store=self.harness.core, connectors=self.harness.connectors,
            observability=self.harness.observability, journal=self.harness.journal,
            scheduler=self.harness.scheduler, catalog=self.harness.catalog,
            spool=self.harness.spool, governance=self.harness.governance,
            adapter=self.harness.adapter, clock=self.harness.clock)
        with patch("dalton_core.sec_filings_index_core.governed_daily_quota", return_value=lower):
            with self.assertRaisesRegex(SecFilingsIndexCoreError, "exceeds governed ceiling"):
                replacement.ensure_authorities()

    def test_it_refuses_a_governance_record_for_another_capability(self) -> None:
        other = ConnectorGovernance(
            build_governance_record(
                "sec-company-facts", approved_by=OWNER, status="approved"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SecFilingsIndexCoreError):
                Harness(Path(tmp), governance=other)

    def test_the_descriptor_it_publishes_is_the_signed_capability(self) -> None:
        descriptor = self.harness.index.ensure_descriptor()
        self.assertEqual(descriptor.id, CAPABILITY_ID)
        self.assertEqual(
            descriptor.schema_hash,
            self.harness.governance.wire["expected_schema_hash"],
        )

    def test_the_spec_is_closed(self) -> None:
        for bad in (
            {**SPEC, "extra": 1},
            {**SPEC, "issuer": "1467373"},
            {**SPEC, "date_from": "2025-13-01"},
            {**SPEC, "date_from": "2025-12-31", "date_to": "2025-02-14"},
            {**SPEC, "limit": 0},
        ):
            with self.assertRaises(SecFilingsIndexCoreError):
                validate_filings_index_spec(bad)


if __name__ == "__main__":
    unittest.main()


class FilingUrlAuthorityFromEnvelopeTests(unittest.TestCase):
    """P10t: a queued filing resolves to a fetchable URL from its envelope."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.harness = Harness(Path(self.tmp.name))
        self.addCleanup(self.harness.close)
        index = self.harness.index
        self.receipt = index.list_filings(index.build_request(SPEC))
        self.assertEqual(self.receipt["outcome"], "succeeded")

    def test_the_filing_ref_resolves_through_the_shared_fetch_path(self) -> None:
        from dalton_core.public_web_core_fetch import url_authority_from_discovery

        record_ref = self.receipt["source_record_refs"][0]
        authority = url_authority_from_discovery(
            self.harness.core.connection, self.harness.spool,
            url_ref=record_ref,
            source_envelope_ref=self.receipt["source_envelope_ref"],
        )
        self.assertEqual(authority["host"], "www.sec.gov")
        self.assertIn("/Archives/edgar/data/1467373/", authority["canonical_url"])
        self.assertEqual(
            authority["discovery_source_envelope_ref"], self.receipt["source_envelope_ref"]
        )

    def test_a_filing_the_envelope_never_named_is_refused(self) -> None:
        from dalton_core.public_web_core_fetch import (
            PublicWebCoreFetchError, url_authority_from_discovery,
        )

        # The 8-K sits in the same raw block, so it is reachable in the bytes
        # but was not part of what this call returned. Deriving a URL for it
        # would let the fetch lane spend on a document nobody discovered.
        with self.assertRaises(PublicWebCoreFetchError):
            url_authority_from_discovery(
                self.harness.core.connection, self.harness.spool,
                url_ref="sec:filing:0001467373-25-000100",
                source_envelope_ref=self.receipt["source_envelope_ref"],
            )
