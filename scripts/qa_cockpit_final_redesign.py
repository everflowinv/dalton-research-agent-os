#!/usr/bin/env python3
"""GET-only visual fixture for the Cockpit redesign; rejects every mutation."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src/dalton_core/cockpit_control.html"


def company(ticker: str, name: str, stage_ref: str, stage: str, status: str) -> dict:
    return {
        "ticker": ticker, "name": name, "company_ref": f"company:{ticker}",
        "stage_ref": stage_ref, "stage": stage, "stage_status": status,
        "note": "所有状态来自当前任务绑定的阶段记录。",
        "progress": {"percent": 83, "found": 18, "read": 14, "waiting": 2},
        "claims": {"total": 31, "today": 2, "latest": [{"statement": "AI 服务需求仍在增长，但转化节奏因客户而异。"}]},
        "figures": {"total": 12, "latest": [{"label": "收入", "value": "4.9B", "currency": "USD", "period": "2026 Q2"}]},
        "checklist": [{"label": "公司披露", "status": "complete", "have": 4, "required": 4, "note": ""}],
        "invariants": {}, "reflections": [], "research_tasks": [], "cadence": [],
    }


OVERVIEW = {
    "as_of": "2026-09-10T18:00:00Z",
    "goal": {"title": "AI 时代的 IT 服务定价权", "objective": "比较五家公司收入、增长和利润率，并把未知项明确留空。", "version": 14, "published_at": "2026-09-10T15:42:54Z", "research_questions": ["需求增长来自哪里？", "利润率变化能否持续？"], "sources": [{"label": "SEC", "connected": True}, {"label": "公司披露", "connected": True}], "history": []},
    "plan": None,
    "totals": {"theses": 18, "found": 82, "read": 63, "waiting": 8, "claims": 129, "claims_today": 7},
    "budgets": {"model_calls": {"used": 42, "cap": 9000, "cost_usd": "3.18", "cost_cap_usd": "100"}, "web": {"spent": 8, "cap": 200}, "alphaengine": {"spent": 54, "cap": 130}, "pools": None},
    "ops": {"lanes": {"counts": {"running": 3}, "total": 19, "note": "3 条运行中", "waiting_on_you": 0}, "gaps": {"headline": 4, "note": "明确登记的缺口"}, "failures": {"headline": 1, "note": "等待外部依赖", "parked_items": 0}, "acceptance": {"available": True, "published": 12, "note": "已核验", "week": "本周"}},
    "activity": {"service_state": "running", "last_tick_at": "2026-09-10T18:00:00Z", "lanes": [{"label": "公司档案", "status": "running", "note": "处理中", "detail": ""}, {"label": "事件判断", "status": "idle", "note": "等待新事件", "detail": ""}], "running": [{"title": "更新 IBM 模型", "detail": "校验输入绑定"}], "ticks": None},
    "companies": [company("ACN", "埃森哲", "active_coverage", "持续覆盖", "已通过"), company("CTSH", "高知特", "company_model", "公司模型", "进行中"), company("EPAM", "EPAM", "industry_model", "行业模型", "等待证据"), company("IBM", "IBM", "investment_memo", "投资备忘录", "待人审"), company("DXC", "DXC", "deep_insight_gate", "深度认知门", "资料不足")],
    "model_available": {"available": True},
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            self._send(HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/v1/cockpit/overview"):
            self._send(json.dumps(OVERVIEW).encode(), "application/json")
        elif self.path.startswith("/v1/cockpit/research"):
            self._send(json.dumps({"company_ref": "company:CTSH", "industry_ref": "industry:it-services", "products": [{"kind": "dossier", "label": "公司档案", "status": "available", "version_ref": "dossier:v3", "content_hash": "a" * 64, "created_at": OVERVIEW["as_of"], "sections": [{"title": "业务与客户", "body": "收入来自数字工程与运营服务。", "sources": [{"kind": "claim", "ref": "claim:v8", "text": "公司披露"}], "gaps": []}], "gaps": []}, {"kind": "investment_memo", "label": "投资备忘录", "status": "missing", "reason": "等待公司模型通过出口门", "sections": [], "gaps": ["等待公司模型通过出口门"]}]}).encode(), "application/json")
        elif self.path.startswith("/v1/cockpit/log"):
            self._send(json.dumps({"events": [], "as_of": OVERVIEW["as_of"], "service_state": "running", "last_tick_at": OVERVIEW["as_of"]}).encode(), "application/json")
        elif self.path.startswith("/v1/cockpit/approvals"):
            self._send(json.dumps({"count": 0, "items": [], "as_of": OVERVIEW["as_of"]}).encode(), "application/json")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        self.send_error(405, "QA fixture is read-only")

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18895)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
