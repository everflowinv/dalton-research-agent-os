from __future__ import annotations

import importlib.util
import hashlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
)
from dalton_core.openclaw_connector_bridge import HostToolInvocationResult
from dalton_core.raw_spool import RawSpool
from dalton_core.store import canonical_json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "alphaengine_transcript_polish_canary",
    ROOT / "scripts" / "run_alphaengine_transcript_polish_canary.py",
)
assert SPEC is not None and SPEC.loader is not None
CANARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CANARY)


class FakeHandle:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def invoke(self, tool_name, arguments, **kwargs):
        del kwargs
        self.calls += 1
        self.assertions = (tool_name, dict(arguments))
        payload = {
            "metadata": {"doc_id": arguments["doc_id"], "title": "Fixture"},
            "content_chars": len(self.text),
            "content_sha256": hashlib.sha256(
                self.text.encode("utf-8")
            ).hexdigest(),
            "offset": arguments["offset"],
            "returned_chars": len(self.text),
            "text": self.text,
            "next_offset": None,
            "complete": True,
        }
        request_id = "provider-canary-fixture"
        result = {
            "content": [{"type": "text", "text": canonical_json(payload)}],
            "isError": False,
        }
        raw = canonical_json({
            "jsonrpc": "2.0", "id": request_id, "result": result,
        }).encode("utf-8")
        return HostToolInvocationResult(
            request_id=request_id, raw_response=raw, result=result
        )


class AlphaEngineTranscriptPolishCanaryTests(unittest.TestCase):
    def test_live_page_port_forms_complete_hash_bound_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            text = "发言人Operator： Welcome.\n发言人Analyst： Revenue was $20 million."
            spool = RawSpool(temporary, max_total_bytes=1_000_000)
            plan = build_alphaengine_document_acquisition_plan(
                document_ref="alphaengine-doc:canary-fixture",
                created_at="2026-08-24T10:00:00.000000+00:00",
                max_pages=1,
                page_max_response_bytes=100_000,
                max_total_response_bytes=100_000,
                max_document_chars=10_000,
            )
            authority = CANARY._CanaryAuthorityReader()
            handle = FakeHandle(text)
            port = CANARY._LiveAlphaEnginePagePort(
                plan=plan,
                authority=authority,
                spool=spool,
                credential_handle=handle,
                page_max_chars=30_000,
                call_timeout_seconds=30,
                created_at=plan["created_at"],
            )
            manifest = AlphaEngineDocumentAcquisitionCoordinator(
                plan=plan,
                page_port=port,
                authority_reader=authority,
                spool=spool,
            ).execute()
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["physical_calls"], 1)
            self.assertEqual(manifest["document_quota_units"], 1)
            self.assertEqual(handle.calls, 1)
            raw = spool.read_object(
                manifest["assembled_object"]["content_hash"]
            ).decode("utf-8")
            self.assertEqual(raw, text)
            self.assertEqual(
                CANARY._speaker_terms(text),
                ["发言人Operator", "发言人Analyst"],
            )

    def test_single_span_rejects_ambiguous_review_authority(self) -> None:
        with self.assertRaises(CANARY.AlphaEngineTranscriptCanaryError):
            CANARY._single_span("same same", "same", "term")


if __name__ == "__main__":
    unittest.main()
