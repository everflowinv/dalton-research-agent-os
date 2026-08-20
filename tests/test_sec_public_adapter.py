from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.sec_public_adapter import (
    SecCompanyConceptHttpAdapter,
    SecPublicAdapterError,
    SecPublicHttpAdapter,
    SecPublicRouterAdapter,
    normalize_sec_company_concept,
    normalize_sec_submissions,
)


WHEN = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


def payload():
    rows = [
        ("0000000001-25-000001", "8-K", "2025-01-01", "start.htm", None),
        ("0000000002-25-000002", "10-Q", "2025-01-29", "q1.htm", None),
        ("0000000003-25-000003", "10-Q/A", "2025-04-30", "q2a.htm", "0000000002-25-000002"),
        ("0000000004-25-000004", "10-Q", "2025-10-29", "q3.htm", None),
        ("0000000005-25-000005", "8-K", "2025-12-31", "end.htm", None),
    ]
    return {"cik": "0000789019", "filings": {"recent": {
        "accessionNumber": [x[0] for x in rows], "form": [x[1] for x in rows],
        "filingDate": [x[2] for x in rows], "primaryDocument": [x[3] for x in rows],
        "amendmentOf": [x[4] for x in rows],
    }}}


PARAMETERS = {"issuer": "0000789019", "form": "10-Q", "date_from": "2025-01-01", "date_to": "2025-12-31", "limit": 10}


CONCEPT_PARAMETERS = {
    "cik": "0000789019",
    "taxonomy": "us-gaap",
    "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "unit": "USD",
    "form": "10-Q",
    "filed_from": "2025-08-20",
    "filed_to": "2026-08-20",
}


def concept_payload():
    return {
        "cik": 789019,
        "taxonomy": "us-gaap",
        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "label": "Revenue from Contract with Customer, Excluding Assessed Tax",
        "description": "Synthetic test description.",
        "entityName": "MICROSOFT CORPORATION",
        "units": {
            "USD": [
                {
                    "start": "2024-01-01", "end": "2024-03-31",
                    "val": 51000000000, "accn": "0000789019-25-000063",
                    "fy": 2025, "fp": "Q3", "form": "10-Q",
                    "filed": "2025-04-30", "frame": "CY2024Q1",
                },
                {
                    "start": "2025-01-01", "end": "2025-03-31",
                    "val": 61858000000, "accn": "0000789019-25-000063",
                    "fy": 2025, "fp": "Q3", "form": "10-Q",
                    "filed": "2025-04-30", "frame": "CY2025Q1",
                },
                {
                    "start": "2025-01-01", "end": "2025-03-31",
                    "val": 61858000000, "accn": "0000789019-26-000054",
                    "fy": 2026, "fp": "Q3", "form": "10-Q",
                    "filed": "2026-04-29", "frame": "CY2025Q1",
                },
                {
                    "start": "2026-01-01", "end": "2026-03-31",
                    "val": 70066000000, "accn": "0000789019-26-000054",
                    "fy": 2026, "fp": "Q3", "form": "10-Q",
                    "filed": "2026-04-29", "frame": "CY2026Q1",
                },
                {
                    "start": "2025-07-01", "end": "2026-06-30",
                    "val": 281000000000, "accn": "0000789019-26-000099",
                    "fy": 2026, "fp": "FY", "form": "10-K",
                    "filed": "2026-07-29", "frame": "CY2025",
                },
            ]
        },
    }


class Response:
    def __init__(self, status, body=b"{}", headers=None):
        self.status = status
        self.reason = "OK"
        self.body = body
        self.headers = headers or {}
        self.bytes_written = len(body)


class Transport:
    def __init__(self, response):
        self.response = response
        self.timeout = None
        self.headers = None
        self.url = None

    def request(self, request, raw_sink, **kwargs):
        self.headers = dict(request.headers)
        self.timeout = kwargs["timeout_seconds"]
        self.url = request.url
        return self.response


class AdapterTests(unittest.TestCase):
    def request(self):
        return {
            "parameters": PARAMETERS,
            "deadline_at": (WHEN + timedelta(seconds=10)).isoformat(),
            "allowed_hosts": ["data.sec.gov"],
            "network_policy": {"allow_redirects": False, "max_redirects": 0},
            "max_response_bytes": 2_000_000,
            "content_hash": "a" * 64,
        }

    def test_normalizer_includes_amendments_and_strictly_bounds_window(self):
        result = normalize_sec_submissions(payload(), PARAMETERS, provider_status=200)
        self.assertEqual(len(result["records"]), 3)
        self.assertIn("sec:filing:0000000003-25-000003", result["source_record_refs"])
        with self.assertRaises(SecPublicAdapterError):
            normalize_sec_submissions(payload(), {**PARAMETERS, "date_to": "2027-01-01"}, provider_status=200)
        duplicate = json.loads(json.dumps(payload()))
        duplicate["filings"]["recent"]["accessionNumber"][1] = duplicate["filings"]["recent"]["accessionNumber"][0]
        with self.assertRaises(SecPublicAdapterError):
            normalize_sec_submissions(duplicate, PARAMETERS, provider_status=200)
        with self.assertRaises(SecPublicAdapterError):
            normalize_sec_submissions(
                payload(), {**PARAMETERS, "date_from": "2025-02-30"},
                provider_status=200,
            )
        unbound_amendment = json.loads(json.dumps(payload()))
        del unbound_amendment["filings"]["recent"]["amendmentOf"]
        with self.assertRaises(SecPublicAdapterError):
            normalize_sec_submissions(
                unbound_amendment, PARAMETERS, provider_status=200
            )

    def test_credential_free_headers_deadline_and_bounded_retry_after(self):
        body = json.dumps(payload(), separators=(",", ":")).encode()
        transport = Transport(Response(200, body))
        adapter = SecPublicHttpAdapter(transport=transport, clock=lambda: WHEN, user_agent="operator/sec-canary")
        observation = adapter(self.request(), object())
        self.assertEqual(observation["outcome"], "succeeded")
        self.assertEqual(transport.headers, {"User-Agent": "operator/sec-canary", "Accept": "application/json"})
        self.assertGreater(transport.timeout, 9)
        with self.assertRaises(SecPublicAdapterError):
            adapter(self.request(), object(), credential_handle="must-not-enter")

        limited_transport = Transport(Response(429, b"{}", {"retry-after": "2"}))
        limited = SecPublicHttpAdapter(transport=limited_transport, clock=lambda: WHEN)
        limited_observation = limited(self.request(), object())
        self.assertEqual(limited_observation["outcome"], "rate_limited")
        self.assertEqual(limited_observation["retry_after_ms"], 2000)
        unbounded = Transport(Response(429, b"{}", {"retry-after": "999999"}))
        failed = SecPublicHttpAdapter(transport=unbounded, clock=lambda: WHEN)(self.request(), object())
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["error"]["code"], "rate_limited_missing_retry_after")

    def test_company_concept_selects_same_filing_quarters_and_recomputes_growth(self):
        result = normalize_sec_company_concept(
            concept_payload(), CONCEPT_PARAMETERS, provider_status=200
        )
        self.assertEqual(result["current"]["frame"], "CY2026Q1")
        self.assertEqual(result["prior"]["frame"], "CY2025Q1")
        self.assertEqual(
            result["current"]["accession"], result["prior"]["accession"]
        )
        self.assertEqual(result["growth_percent"], "13.27")
        self.assertEqual(result["filed_from"], "2025-08-20")
        self.assertEqual(len(result["source_record_refs"]), 2)

        amended = concept_payload()
        amended["units"]["USD"].append({
            **amended["units"]["USD"][3],
            "val": 999999999999,
            "accn": "0000789019-26-000055",
            "form": "10-Q/A",
            "filed": "2026-05-15",
        })
        amended_result = normalize_sec_company_concept(
            amended, CONCEPT_PARAMETERS, provider_status=200
        )
        self.assertEqual(amended_result["current"]["value"], "70066000000")

    def test_company_concept_fails_closed_on_ambiguous_or_mixed_context(self):
        ambiguous = concept_payload()
        ambiguous["units"]["USD"].append(
            {
                **ambiguous["units"]["USD"][2],
                "val": 60000000000,
            }
        )
        with self.assertRaisesRegex(
            SecPublicAdapterError, "comparative period is ambiguous"
        ):
            normalize_sec_company_concept(
                ambiguous, CONCEPT_PARAMETERS, provider_status=200
            )
        mixed_units = concept_payload()
        mixed_units["units"]["shares"] = []
        with self.assertRaisesRegex(SecPublicAdapterError, "units do not match"):
            normalize_sec_company_concept(
                mixed_units, CONCEPT_PARAMETERS, provider_status=200
            )
        with self.assertRaisesRegex(SecPublicAdapterError, "closed shape"):
            normalize_sec_company_concept(
                concept_payload(),
                {**CONCEPT_PARAMETERS, "guess_tag": True},
                provider_status=200,
            )
        with self.assertRaisesRegex(SecPublicAdapterError, "no eligible quarterly"):
            normalize_sec_company_concept(
                concept_payload(),
                {**CONCEPT_PARAMETERS, "filed_from": "2026-05-01"},
                provider_status=200,
            )
        missing_comparative = concept_payload()
        missing_comparative["units"]["USD"] = [
            row
            for row in missing_comparative["units"]["USD"]
            if not (
                row["accn"] == "0000789019-26-000054"
                and row["frame"] == "CY2025Q1"
            )
        ]
        with self.assertRaisesRegex(SecPublicAdapterError, "same-filing prior-year"):
            normalize_sec_company_concept(
                missing_comparative, CONCEPT_PARAMETERS, provider_status=200
            )
        with self.assertRaisesRegex(SecPublicAdapterError, "0..400 days"):
            normalize_sec_company_concept(
                concept_payload(),
                {**CONCEPT_PARAMETERS, "filed_from": "2025-01-01"},
                provider_status=200,
            )

    def test_company_concept_adapter_is_credential_free_and_route_bound(self):
        body = json.dumps(concept_payload(), separators=(",", ":")).encode()
        transport = Transport(Response(200, body))
        adapter = SecCompanyConceptHttpAdapter(
            transport=transport, clock=lambda: WHEN, user_agent="operator/sec-canary"
        )
        request = {
            "parameters": CONCEPT_PARAMETERS,
            "deadline_at": (WHEN + timedelta(seconds=10)).isoformat(),
            "allowed_hosts": ["data.sec.gov"],
            "network_policy": {"allow_redirects": False, "max_redirects": 0},
            "max_response_bytes": 5_000_000,
            "content_hash": "b" * 64,
        }
        observation = adapter(request, object())
        self.assertEqual(observation["outcome"], "succeeded")
        self.assertEqual(
            transport.url,
            "https://data.sec.gov/api/xbrl/companyconcept/"
            "CIK0000789019/us-gaap/"
            "RevenueFromContractWithCustomerExcludingAssessedTax.json",
        )
        with self.assertRaises(SecPublicAdapterError):
            adapter(request, object(), credential_handle="forbidden")

    def test_router_dispatches_only_the_two_registered_operations(self):
        body = json.dumps(concept_payload(), separators=(",", ":")).encode()
        transport = Transport(Response(200, body))
        router = SecPublicRouterAdapter(transport=transport, clock=lambda: WHEN)
        request = {
            "operation": "get_company_facts",
            "parameters": CONCEPT_PARAMETERS,
            "deadline_at": (WHEN + timedelta(seconds=10)).isoformat(),
            "allowed_hosts": ["data.sec.gov"],
            "network_policy": {"allow_redirects": False, "max_redirects": 0},
            "max_response_bytes": 5_000_000,
            "content_hash": "c" * 64,
        }
        self.assertEqual(router(request, object())["outcome"], "succeeded")
        with self.assertRaisesRegex(SecPublicAdapterError, "not approved"):
            router({**request, "operation": "read_arbitrary_url"}, object())
