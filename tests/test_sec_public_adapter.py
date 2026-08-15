from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.sec_public_adapter import SecPublicAdapterError, SecPublicHttpAdapter, normalize_sec_submissions


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

    def request(self, request, raw_sink, **kwargs):
        self.headers = dict(request.headers)
        self.timeout = kwargs["timeout_seconds"]
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
