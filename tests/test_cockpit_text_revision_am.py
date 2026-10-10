"""Batch am (2026-10-09 text evaluation): display fields added at the source.

* the preliminary-results reconciliation names the amount, the period and the
  forecast version it is measured on (report 5.6);
* the gate buttons speak review language from the registry (report 7.2);
* the page sentence reads those fields.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from dalton_core.cockpit_plane import GATE_ACTIONS, _reconciliation_display
from dalton_core.terminology import label

V2 = Path(__file__).resolve().parents[1] / "src" / "dalton_core" / "cockpit_v2"
NODE = shutil.which("node") is not None


class ServerDisplayFieldTests(unittest.TestCase):
    def test_reconciliation_display_fields(self):
        shown = _reconciliation_display({
            "actual_value": "18044.1", "forecast_value": "18337.6", "unit": "million",
            "period": "2026-06-01..2026-08-31", "forecast_version": 3,
            "forecast_created_at": "2026-09-30T10:00:00+00:00"})
        self.assertEqual(shown, {"actual_label": "180.4 亿美元", "forecast_label": "183.4 亿美元",
                                 "period_label": "2026年6月1日至2026年8月31日",
                                 "forecast_version_label": "v3，2026-09-30 生成"})
        self.assertEqual(_reconciliation_display({"unit": "furlongs", "actual_value": "1"}), {})

    def test_gate_buttons_use_review_language(self):
        self.assertEqual([a["label"] for a in GATE_ACTIONS],
                         [label("decision_button", "gate_approve"), label("decision_button", "gate_return"),
                          label("decision_button", "gate_reject")])
        self.assertEqual(GATE_ACTIONS[0]["label"], "核准通过")


@unittest.skipUnless(NODE, "node is not installed")
class PageSentenceTests(unittest.TestCase):
    def test_reconciliation_sentence_has_amount_period_and_version(self):
        script = (
            'globalThis.window = {DaltonV2: {registerPage() {}, registerWidget() {}}};'
            'const m = await import("./pages/companies.js");'
            'const ctx = {say: t => t, fmt: {number: (n, d) => Number(n).toFixed(d)}};'
            'console.log(JSON.stringify(m.reconciliationSentence(ctx, {period: "FY2026 Q4", rows: []},'
            ' {deviation_percent: "-1.6", status: "provisional", actual_label: "180.4 亿美元",'
            '  forecast_label: "183.4 亿美元", period_label: "2026年6月1日至2026年8月31日",'
            '  forecast_version_label: "v3，2026-09-30 生成"})));')
        result = subprocess.run(["node", "--input-type=module", "-e", script], text=True, capture_output=True,
                                timeout=60, cwd=str(V2))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        sentence = json.loads(result.stdout)
        self.assertEqual(sentence, "实际业绩对账：2026年6月1日至2026年8月31日 营业收入录得 180.4 亿美元，"
                                   "较我们的预测（v3，2026-09-30 生成；预测值 183.4 亿美元）偏离 −1.6%（低于预期）")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
