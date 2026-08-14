from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.dashboard import (
    DashboardApplication,
    DashboardError,
    DashboardQueryService,
    ProjectionValidationError,
    ProjectionWriter,
    _is_loopback_host_header,
    serve,
)


NOW = "2026-08-14T06:00:00+00:00"
HASH = "a" * 64


def snapshot() -> dict:
    return {
        "metadata": {
            "schema_version": "0.1",
            "as_of": NOW,
            "source_watermark": "watermark:42",
            "build_state": "ready",
            "partial_data": True,
            "warnings": ["成本中有一条尚未定价"],
        },
        "workflow_summaries": [
            {
                "workflow_ref": "workflow:research",
                "title": "更新研究判断",
                "objective": "核对催化剂并更新证据链",
                "display_status": "执行中",
                "source_state": "leased",
                "source_ref": "attempt-event:2",
                "status_reason": "资料提取任务正在执行",
                "total_tasks": 2,
                "completed_tasks": 1,
                "running_tasks": 1,
                "failed_tasks": 0,
                "total_tokens": 1450,
                "artifact_count": 1,
                "recent_activity": NOW,
            }
        ],
        "work_items": [
            {
                "work_order_ref": "work:root",
                "workflow_ref": "workflow:research",
                "parent_work_order_ref": None,
                "sequence": 0,
                "title": "更新研究判断",
                "question": "哪些证据发生变化？",
                "display_status": "已完成",
                "source_state": "succeeded",
                "source_ref": "result:root",
                "status_reason": "正式结果已经提交",
                "attempt_number": 1,
                "model_count": 1,
                "total_tokens": 450,
                "artifact_count": 0,
                "latest_result_ref": "result:root",
                "latest_error_summary": None,
                "created_at": NOW,
                "updated_at": NOW,
            },
            {
                "work_order_ref": "work:extract",
                "workflow_ref": "workflow:research",
                "parent_work_order_ref": "work:root",
                "sequence": 1,
                "title": "提取专家观点",
                "question": "Guidepoint 中有哪些新证据？",
                "display_status": "执行中",
                "source_state": "leased",
                "source_ref": "lease:2",
                "status_reason": "worker 持有有效租约",
                "attempt_number": 1,
                "model_count": 1,
                "total_tokens": 1000,
                "artifact_count": 1,
                "latest_result_ref": None,
                "latest_error_summary": None,
                "created_at": NOW,
                "updated_at": NOW,
            },
        ],
        "invocation_slices": [
            {
                "invocation_ref": "invocation:1",
                "workflow_ref": "workflow:research",
                "work_order_ref": "work:extract",
                "provider": "openai",
                "model": "gpt-example",
                "model_family": "gpt-example-family",
                "profile_ref": "profile:research",
                "runtime_ref": "runtime:native",
                "capability": "mcp:guidepoint/search_library",
                "granularity": "task",
                "started_at": NOW,
                "completed_at": NOW,
                "duration_ms": 1200,
                "input_tokens": 700,
                "output_tokens": 300,
                "reasoning_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "total_tokens": 1000,
                "metering_source": "provider_reported",
                "measurement_status": "measured",
            }
        ],
        "cost_slices": [
            {
                "cost_entry_ref": "cost:1",
                "invocation_ref": "invocation:1",
                "workflow_ref": "workflow:research",
                "work_order_ref": "work:extract",
                "amount_micros": 125000,
                "currency": "USD",
                "cost_status": "estimated",
                "price_rate_ref": "rate:1",
                "created_at": NOW,
            },
            {
                "cost_entry_ref": "cost:2",
                "invocation_ref": "invocation:1",
                "workflow_ref": "workflow:research",
                "work_order_ref": "work:extract",
                "amount_micros": None,
                "currency": "USD",
                "cost_status": "unpriced",
                "price_rate_ref": None,
                "created_at": NOW,
            },
        ],
        "artifact_index": [
            {
                "artifact_ref": "artifact:notes",
                "workflow_ref": "workflow:research",
                "work_order_ref": "work:extract",
                "title": "专家观点摘录",
                "kind": "research_notes",
                "media_type": "text/markdown",
                "size_bytes": 2048,
                "content_hash": HASH,
                "access_class": "internal",
                "preview_status": "metadata_only",
                "producer_execution_ref": "invocation:1",
                "created_at": NOW,
            }
        ],
        "capability_status": [
            {
                "capability_id": "mcp:guidepoint/search_library",
                "label": "Guidepoint 专家访谈检索",
                "kind": "connector",
                "source_type": "mcp",
                "eligibility_state": "ready",
                "active_revision_ref": "capability:guidepoint:v1",
                "decision_state": "approved",
                "updated_at": NOW,
            }
        ],
        "model_status": [
            {
                "profile_ref": "profile:research",
                "provider": "openai",
                "model": "gpt-example",
                "model_family": "gpt-example-family",
                "availability": "available",
                "auth_state": "ready",
                "capabilities_json": json.dumps(["research", "tools"]),
                "context_window": 100000,
                "cost_class": "medium",
                "last_used_at": NOW,
                "total_tokens": 1000,
            }
        ],
        "agenda_supervision": [
            {
                "singleton": 1,
                "paused": False,
                "pause_reason": "Phase 1 shadow",
                "policy_version_ref": "agenda-policy:1",
                "cutover_enabled": False,
                "total_cycles": 1,
                "decided_cycles": 1,
                "failed_cycles": 0,
                "pending_deliveries": 0,
                "delivered_cards": 1,
                "labeled_decisions": 1,
                "auto_accepted_decisions": 0,
                "agreement_rate": 1.0,
                "last_cycle_at": NOW,
            }
        ],
        "agenda_cycle_summaries": [
            {
                "cycle_ref": "agenda-cycle:1",
                "cycle_key": "agenda:2026-08-14:wanhua",
                "company_ref": "wanhua",
                "state": "delivered",
                "decision_ref": "agenda-decision:1",
                "selected_count": 1,
                "deferred_count": 1,
                "rejected_count": 0,
                "delivery_state": "delivered",
                "delivery_attempts": 1,
                "feedback_state": "agree",
                "agree_count": 1,
                "disagree_count": 0,
                "partial_count": 0,
                "auto_accept_count": 0,
                "created_at": NOW,
                "updated_at": NOW,
            }
        ],
        "agenda_questions": [
            {
                "candidate_ref": "agenda-candidate:1",
                "cycle_ref": "agenda-cycle:1",
                "decision_ref": "agenda-decision:1",
                "selection_state": "selected",
                "selection_rank": 1,
                "question": "MDI 价差变化是否改变盈利预期？",
                "answer_criteria": "核对价格、成本和销量",
                "rationale": "展示理由",
                "total_score": 31,
                "features_json": json.dumps({"mandate_relevance": 3}),
            }
        ],
    }


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "projection.db"
        with ProjectionWriter(self.path) as writer:
            writer.replace(snapshot())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_summary_keeps_currency_and_partial_data_explicit(self) -> None:
        service = DashboardQueryService(self.path)
        try:
            result = service.summary()
            self.assertTrue(result["partial_data"])
            self.assertEqual(result["projection_watermark"], "watermark:42")
            self.assertEqual(result["data"]["total_tokens"], 1450)
            self.assertEqual(result["data"]["costs"][0]["currency"], "USD")
            self.assertEqual(result["data"]["costs"][0]["unpriced_entries"], 1)
        finally:
            service.close()

    def test_workflow_tree_and_usage_are_fixed_queries(self) -> None:
        service = DashboardQueryService(self.path)
        try:
            tree = service.workflow_tree("workflow:research")["data"]
            self.assertEqual([row["work_order_ref"] for row in tree], ["work:root", "work:extract"])
            usage = service.usage_summary("model")["data"]
            self.assertEqual(usage["rows"][0]["group_key"], "gpt-example")
            with self.assertRaises(ProjectionValidationError):
                service.usage_summary("model; DROP TABLE work_items")
            agenda = service.agenda()["data"]
            self.assertEqual(agenda["overview"]["labeled_decisions"], 1)
            self.assertEqual(agenda["questions"][0]["selection_state"], "selected")
        finally:
            service.close()

    def test_query_connection_cannot_write(self) -> None:
        service = DashboardQueryService(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                service.connection.execute("DELETE FROM workflow_summaries")
        finally:
            service.close()

    def test_query_service_supports_http_worker_threads(self) -> None:
        service = DashboardQueryService(self.path)
        results = []
        errors = []

        def read_summary() -> None:
            try:
                results.append(service.summary())
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        worker = threading.Thread(target=read_summary)
        worker.start()
        worker.join(timeout=2)
        try:
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0]["data"]["workflows"], 1)
        finally:
            service.close()

    def test_projection_rejects_unknown_fields_atomically(self) -> None:
        bad = snapshot()
        bad["work_items"][0]["raw_prompt"] = "secret"
        with ProjectionWriter(self.path) as writer:
            with self.assertRaises(ProjectionValidationError):
                writer.replace(bad)
        service = DashboardQueryService(self.path)
        try:
            self.assertEqual(len(service.workflow_tree("workflow:research")["data"]), 2)
        finally:
            service.close()

    def test_http_surface_is_get_only_and_hides_raw_content(self) -> None:
        service = DashboardQueryService(self.path)
        try:
            app = DashboardApplication(service)
            status, content_type, body = app.dispatch("/v1/work-orders/work%3Aextract")
            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            self.assertNotIn(b"prompt", body.lower())
            status, _, html = app.dispatch("/")
            self.assertEqual(status, 200)
            self.assertIn("Dalton", html.decode("utf-8"))
            status, _, _ = app.dispatch("/v1/usage/summary?group_by=not_allowed")
            self.assertEqual(status, 400)
        finally:
            service.close()

    def test_non_loopback_bind_is_rejected_before_server_start(self) -> None:
        with self.assertRaises(DashboardError):
            serve(self.path, host="0.0.0.0", port=0)
        with self.assertRaises(DashboardError):
            serve(self.path, host="localhost", port=0)

    def test_http_host_header_requires_ip_literal_loopback(self) -> None:
        self.assertTrue(_is_loopback_host_header("127.0.0.1:8765"))
        self.assertTrue(_is_loopback_host_header("[::1]:8765"))
        self.assertFalse(_is_loopback_host_header("localhost:8765"))
        self.assertFalse(_is_loopback_host_header("127.0.0.1.example.com"))
        self.assertFalse(_is_loopback_host_header(None))


if __name__ == "__main__":
    unittest.main()
