from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy

from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    AlphaEngineDocumentAcquisitionError,
    build_alphaengine_document_acquisition_plan,
    validate_alphaengine_document_page_request,
)
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.raw_spool import RawSpool
from dalton_core.store import canonical_json, content_hash
from tests.test_connector import assert_wire_schema


WHEN = "2026-08-23T09:30:00.000000+00:00"
DOCUMENT_REF = "alphaengine-doc:320000610033807"


def with_hash(value: dict) -> dict:
    return {**value, "content_hash": content_hash(value)}


class FakeAuthorityReader:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def put(self, value: dict) -> dict:
        self.records[value["id"]] = value
        return value

    def _get(self, ref: str):
        value = self.records.get(ref)
        return None if value is None else deepcopy(value)

    get_invocation = _get
    get_profile = _get
    get_call_spec = _get
    get_reservation = _get
    get_physical_attempt = _get
    get_usage_entry = _get
    get_cost_entry = _get
    get_quota_settlement = _get
    get_source_envelope = _get
    get_artifact_version = _get


class FakePagePort:
    def __init__(
        self,
        *,
        plan: dict,
        pages: list[str],
        authority: FakeAuthorityReader,
        spool: RawSpool,
        crash_after_provider_ordinal: int | None = None,
        tamper_page_ordinal: int | None = None,
        fail_page_ordinal: int | None = None,
    ) -> None:
        self.plan = plan
        self.pages = pages
        self.authority = authority
        self.spool = spool
        self.crash_after_provider_ordinal = crash_after_provider_ordinal
        self.tamper_page_ordinal = tamper_page_ordinal
        self.fail_page_ordinal = fail_page_ordinal
        self.crashed = False
        self.provider_calls = 0
        self.cache: dict[str, dict] = {}
        self.document_text = "".join(pages)
        self.document_hash = hashlib.sha256(
            self.document_text.encode("utf-8")
        ).hexdigest()

    def execute_page(self, request):
        page_request = validate_alphaengine_document_page_request(request)
        cached = self.cache.get(page_request["id"])
        if cached is not None:
            duplicate = deepcopy(cached)
            duplicate["idempotency_status"] = "duplicate"
            duplicate["content_hash"] = content_hash(
                {
                    key: value for key, value in duplicate.items()
                    if key != "content_hash"
                }
            )
            return duplicate
        ordinal = int(page_request["page_ordinal"])
        response = self._provider_call(page_request)
        self.cache[page_request["id"]] = response
        if (
            self.crash_after_provider_ordinal == ordinal
            and not self.crashed
        ):
            self.crashed = True
            raise RuntimeError("simulated crash after provider observation")
        return deepcopy(response)

    def _provider_call(self, request: dict) -> dict:
        ordinal = int(request["page_ordinal"])
        self.provider_calls += 1
        text = self.pages[ordinal - 1]
        offset = int(request["expected_offset"])
        terminal = ordinal == len(self.pages)
        next_offset = None if terminal else offset + len(text)
        digest = self.document_hash
        if self.tamper_page_ordinal == ordinal:
            digest = "f" * 64
        payload = {
            "metadata": {"doc_id": self.plan["document_id"], "title": "ACN"},
            "content_chars": len(self.document_text),
            "content_sha256": digest,
            "offset": offset,
            "returned_chars": len(text),
            "text": text,
            "next_offset": next_offset,
            "complete": terminal,
        }
        rpc_id = f"rpc-{ordinal}"
        raw = canonical_json(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": canonical_json(payload),
                        }
                    ]
                },
            }
        ).encode("utf-8")
        raw_hash = hashlib.sha256(raw).hexdigest()
        sink = self.spool.open_sink(
            "raw-sink:" + content_hash(
                {"page_request_hash": request["content_hash"]}
            ),
            max_response_bytes=request["max_response_bytes"],
        )
        sink.write(raw)
        raw_object = sink.finalize()

        suffix = content_hash(
            {"page_request_hash": request["content_hash"]}
        )[:20]
        profile = self.authority.put(
            with_hash(
                {
                    "id": f"connector-profile:alphaengine-page:{suffix}",
                    "source_identity": load_packaged_connector_inventory()[
                        "templates"
                    ]["alphaengine"]["source_identity"],
                    "source_hash": self.plan["source_hash"],
                    "pagination": {
                        "mode": "cursor",
                        "cursor_field": "cursor",
                        "max_pages": 20,
                    },
                    "max_response_bytes": request["max_response_bytes"],
                }
            )
        )
        parameters = {"document_ref": self.plan["document_ref"]}
        if request["request_cursor"] is not None:
            parameters["cursor"] = request["request_cursor"]
        call = self.authority.put(
            with_hash(
                {
                    "id": f"connector-call:alphaengine-page:{suffix}",
                    "connector_profile_ref": profile["id"],
                    "operation": "get_document",
                    "parameters": parameters,
                    "query_hash": content_hash(
                        {"operation": "get_document", "parameters": parameters}
                    ),
                }
            )
        )
        invocation = self.authority.put(
            with_hash(
                {
                    "id": f"connector-invocation:alphaengine-page:{suffix}",
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_ref": call["id"],
                    "call_spec_hash": call["content_hash"],
                    "execution_ref": f"execution:alphaengine-page:{suffix}",
                }
            )
        )
        units = 1 if ordinal == 1 else 0
        reservation = self.authority.put(
            with_hash(
                {
                    "id": f"connector-reservation:alphaengine-page:{suffix}",
                    "reserved": {
                        "calls": 1,
                        "bytes": request["max_response_bytes"],
                        "records": units,
                        "cost_micros": 0,
                    },
                }
            )
        )
        attempt = self.authority.put(
            with_hash(
                {
                    "id": f"connector-attempt:alphaengine-page:{suffix}",
                    "connector_invocation_ref": invocation["id"],
                    "outcome": "succeeded",
                }
            )
        )
        metrics = {
            "calls": 1,
            "bytes": len(raw),
            "records": units,
            "cost_micros": 0,
        }
        usage = self.authority.put(
            with_hash(
                {
                    "id": f"connector-usage:alphaengine-page:{suffix}",
                    "physical_attempt_ref": attempt["id"],
                    "metrics": metrics,
                }
            )
        )
        cost = self.authority.put(
            with_hash(
                {
                    "id": f"connector-cost:alphaengine-page:{suffix}",
                    "usage_entry_ref": usage["id"],
                }
            )
        )
        settlement = self.authority.put(
            with_hash(
                {
                    "id": f"connector-settlement:alphaengine-page:{suffix}",
                    "reservation_ref": reservation["id"],
                    "state": "consumed",
                    "usage_entry_ref": usage["id"],
                    "cost_entry_ref": cost["id"],
                    "actual": metrics,
                }
            )
        )
        artifact = self.authority.put(
            with_hash(
                {
                    "id": f"artifact-version:alphaengine-page:{suffix}",
                    "artifact_content_hash": raw_object.content_hash,
                    "size_bytes": raw_object.size_bytes,
                    "producer_execution_ref": invocation["execution_ref"],
                }
            )
        )
        source_record_ref = (
            f"alphaengine-doc:{self.plan['document_id']}:sha256:{digest}"
        )
        source = self.authority.put(
            with_hash(
                {
                    "id": f"source-envelope:alphaengine-page:{suffix}",
                    "connector_invocation_ref": invocation["id"],
                    "connector_profile_ref": profile["id"],
                    "physical_attempt_refs": [attempt["id"]],
                    "result_physical_attempt_ref": attempt["id"],
                    "source": "source:alphaengine",
                    "operation": "get_document",
                    "source_record_refs": [source_record_ref],
                    "cursor": None if terminal else str(next_offset),
                    "provider_request_id": rpc_id,
                    "raw_artifact_version_ref": artifact["id"],
                    "raw_response_hash": raw_hash,
                    "status": "complete" if terminal else "partial",
                    "completeness": "enumerated" if terminal else "partial",
                }
            )
        )
        base = {
            "schema_version": "0.2",
            "id": f"connector-runner-response:alphaengine-page:{suffix}",
            "created_at": WHEN,
            "runner_request_ref": f"connector-runner-request:{suffix}",
            "runner_request_hash": content_hash({"runner": suffix}),
            "idempotency_status": "fresh",
            "connector_invocation_ref": invocation["id"],
            "connector_invocation_hash": invocation["content_hash"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "usage_entry_ref": usage["id"],
            "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"],
            "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "raw_artifact_version_ref": artifact["id"],
            "raw_artifact_version_hash": artifact["content_hash"],
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "result_envelope_ref": f"result-envelope:{suffix}",
            "result_envelope_hash": content_hash({"result": suffix}),
            "outcome": "succeeded",
            "retry_at": None,
        }
        response = {**base, "content_hash": content_hash(base)}
        if self.fail_page_ordinal == ordinal:
            attempt_base = {
                key: value for key, value in attempt.items()
                if key != "content_hash"
            }
            attempt_base["outcome"] = "failed"
            attempt = self.authority.put(with_hash(attempt_base))
            usage_base = {
                key: value for key, value in usage.items()
                if key != "content_hash"
            }
            usage_base["metrics"] = {
                "calls": 1,
                "bytes": 0,
                "records": 0,
                "cost_micros": 0,
            }
            usage = self.authority.put(with_hash(usage_base))
            settlement_base = {
                key: value for key, value in settlement.items()
                if key != "content_hash"
            }
            settlement_base["state"] = "indeterminate"
            settlement_base["actual"] = usage["metrics"]
            settlement = self.authority.put(with_hash(settlement_base))
            self.authority.records.pop(artifact["id"])
            self.authority.records.pop(source["id"])
            failed_base = {
                key: value for key, value in response.items()
                if key != "content_hash"
            }
            failed_base.update(
                {
                    "physical_attempt_hash": attempt["content_hash"],
                    "usage_entry_hash": usage["content_hash"],
                    "quota_settlement_hash": settlement["content_hash"],
                    "raw_artifact_version_ref": None,
                    "raw_artifact_version_hash": None,
                    "source_envelope_ref": None,
                    "source_envelope_hash": None,
                    "outcome": "failed",
                }
            )
            response = {**failed_base, "content_hash": content_hash(failed_base)}
        return response


class AlphaEngineDocumentAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)

    def plan(self, **overrides) -> dict:
        values = {
            "document_ref": DOCUMENT_REF,
            "created_at": WHEN,
            "max_pages": 3,
            "page_max_response_bytes": 10_000,
            "max_total_response_bytes": 30_000,
            "max_document_chars": 1_000,
        }
        values.update(overrides)
        return build_alphaengine_document_acquisition_plan(**values)

    def coordinator(self, plan: dict, port: FakePagePort, authority):
        return AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=port,
            authority_reader=authority,
            spool=self.spool,
        )

    def test_three_pages_form_one_document_unit_and_exact_manifest(self) -> None:
        plan = self.plan()
        authority = FakeAuthorityReader()
        port = FakePagePort(
            plan=plan,
            pages=["alpha ", "beta ", "gamma"],
            authority=authority,
            spool=self.spool,
        )
        manifest = self.coordinator(plan, port, authority).execute()
        assert_wire_schema(
            self, "alphaengine-document-acquisition-plan.schema.json", plan
        )
        assert_wire_schema(
            self,
            "alphaengine-document-acquisition-manifest.schema.json",
            manifest,
        )
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["physical_calls"], 3)
        self.assertEqual(manifest["document_quota_units"], 1)
        self.assertEqual(
            [page["document_quota_units"] for page in manifest["pages"]],
            [1, 0, 0],
        )
        self.assertEqual(
            manifest["assembled_prefix_sha256"], port.document_hash
        )
        self.assertEqual(
            self.spool.read_object(manifest["assembled_object"]["content_hash"]),
            port.document_text.encode("utf-8"),
        )

    def test_crash_replay_reuses_pages_and_returns_identical_manifest(self) -> None:
        plan = self.plan()
        authority = FakeAuthorityReader()
        port = FakePagePort(
            plan=plan,
            pages=["one", "two", "three"],
            authority=authority,
            spool=self.spool,
            crash_after_provider_ordinal=2,
        )
        coordinator = self.coordinator(plan, port, authority)
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            coordinator.execute()
        self.assertEqual(port.provider_calls, 2)
        recovered = coordinator.execute()
        self.assertEqual(port.provider_calls, 3)
        replayed = coordinator.execute()
        self.assertEqual(port.provider_calls, 3)
        self.assertEqual(replayed, recovered)

    def test_page_hash_drift_fails_closed(self) -> None:
        plan = self.plan()
        authority = FakeAuthorityReader()
        port = FakePagePort(
            plan=plan,
            pages=["first", "second", "third"],
            authority=authority,
            spool=self.spool,
            tamper_page_ordinal=2,
        )
        with self.assertRaisesRegex(
            AlphaEngineDocumentAcquisitionError,
            "document identity changed between pages",
        ):
            self.coordinator(plan, port, authority).execute()

    def test_failed_page_keeps_conservative_document_quota_projection(self) -> None:
        for failed_ordinal, expected_status, expected_units in (
            (1, "failed", 1),
            (2, "partial", 1),
        ):
            with self.subTest(failed_ordinal=failed_ordinal):
                plan = self.plan()
                authority = FakeAuthorityReader()
                port = FakePagePort(
                    plan=plan,
                    pages=["first", "second", "third"],
                    authority=authority,
                    spool=self.spool,
                    fail_page_ordinal=failed_ordinal,
                )
                manifest = self.coordinator(plan, port, authority).execute()
                self.assertEqual(manifest["status"], expected_status)
                self.assertEqual(manifest["termination_reason"], "page_failed")
                self.assertEqual(
                    manifest["document_quota_units"], expected_units
                )
                self.assertEqual(
                    manifest["failed_page"]["page_ordinal"], failed_ordinal
                )

    def test_page_and_byte_budgets_return_partial_prefix(self) -> None:
        for plan, expected_reason in (
            (self.plan(max_pages=2), "max_pages"),
            (
                self.plan(max_total_response_bytes=10_000),
                "max_response_bytes",
            ),
        ):
            with self.subTest(reason=expected_reason):
                authority = FakeAuthorityReader()
                port = FakePagePort(
                    plan=plan,
                    pages=["first", "second", "third"],
                    authority=authority,
                    spool=self.spool,
                )
                manifest = self.coordinator(plan, port, authority).execute()
                self.assertEqual(manifest["status"], "partial")
                self.assertEqual(manifest["termination_reason"], expected_reason)
                self.assertEqual(manifest["document_quota_units"], 1)
                self.assertIsNotNone(manifest["next_cursor"])


if __name__ == "__main__":
    unittest.main()
