"""The scheduled publication worker prints a summary, not its checkpoint.

launchd appends the worker's stdout to publication-worker.stdout.log every five
minutes. It used to print the whole checkpoint -- every product identity, every
UI-text batch and every blocked reason -- which was about 415 KB a line and let
the log grow to 300 MB. The full checkpoint stays in worker-last-run.json.
"""

from __future__ import annotations

import contextlib
import io
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core import research_output_preparation as prep


def live_shaped_checkpoint() -> dict:
    products = [
        {"identity": {"kind": "dossier", "subject_ref": f"company:sec-cik:{i:010d}",
                      "version_ref": f"company-dossier-version:{i}:18"},
         "product_hash": "a" * 64,
         "status": "unchanged" if i % 4 == 0 else "pending"}
        for i in range(50)
    ]
    batches = [
        {"batch_ref": f"ui-text-batch:{i}", "status": "blocked" if i % 3 else "pending",
         "attempts": 6, "texts": ["一段界面文字" * 20] * 5}
        for i in range(1812)
    ]
    reasons = [f"已连续失败 6 次（上限 6），最后一次失败原因：reason {i}" * 3
               for i in range(105)]
    return {
        "schema_version": "research-publication-worker-checkpoint:0.1",
        "products": products, "completed": 0, "pending": 37, "unchanged": 14,
        "ui_texts": {
            "schema_version": "cockpit-ui-text-discovery-poll:0.1",
            "discovered": 11703, "translation_needed": 11244,
            "already_readable": 459, "sealed": 0, "attempted": 1,
            "batches": batches, "batches_per_run": 4, "max_attempts": 6,
            "blocked": 630, "blocked_reasons": reasons, "pending": 1,
        },
        "blocked": 630, "blocked_reasons": reasons, "status": "pending",
        "checked_at": "2026-09-24T08:21:20.499936+00:00",
    }


class WorkerStdoutSummaryTests(unittest.TestCase):
    def test_a_live_sized_checkpoint_prints_as_a_short_summary(self):
        checkpoint = live_shaped_checkpoint()
        self.assertGreater(len(json.dumps(checkpoint, ensure_ascii=False)), 300_000)
        summary = prep.worker_stdout_summary(checkpoint, Path("/w/worker-last-run.json"))
        line = json.dumps(summary, ensure_ascii=False)
        self.assertLess(len(line), 2_000)
        self.assertEqual(summary["status"], "pending")
        self.assertEqual(summary["pending"], 37)
        self.assertEqual(summary["blocked"], 630)
        self.assertEqual(summary["products_count"], 50)
        self.assertEqual(summary["product_statuses"], {"pending": 37, "unchanged": 13})
        self.assertEqual(summary["blocked_reasons_count"], 105)
        self.assertEqual(summary["ui_texts"]["batches_count"], 1812)
        self.assertEqual(summary["ui_texts"]["discovered"], 11703)
        self.assertEqual(summary["checkpoint"], "/w/worker-last-run.json")
        self.assertEqual(summary["schema_version"], prep.WORKER_STDOUT_SUMMARY_SCHEMA)
        for payload in ("products", "blocked_reasons"):
            self.assertNotIn(payload, summary)
        self.assertNotIn("batches", summary["ui_texts"])
        self.assertNotIn("reason 0", line)

    def test_long_text_is_clipped(self):
        summary = prep.worker_stdout_summary(
            {"status": "failed", "error": "x" * 10_000}, Path("/w/c.json"))
        self.assertLessEqual(len(summary["error"]), 201)

    def test_run_worker_prints_the_summary_and_keeps_the_full_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = {name: str(root / name) for name in (
                "core_db", "scheduler_db", "model_config", "verifier_config",
                "checker_config", "brain_config", "work_dir", "output_directory")}
            cfg.update(schema_version="research-publication-worker-config:0.1",
                       workers=4, chunk_chars=4500, max_cost_per_call=1.0,
                       draft_attempts=2, publication_gate={})
            path = root / "worker.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                prep.run_worker(path)
            lines = out.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            printed = json.loads(lines[0])
            checkpoint_path = Path(cfg["work_dir"]) / "worker-last-run.json"
            stored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(printed["checkpoint"], str(checkpoint_path))
            self.assertEqual(printed["status"], stored["status"])
            self.assertEqual(stored["schema_version"],
                             "research-publication-worker-checkpoint:0.1")

    def test_no_branch_prints_the_raw_checkpoint(self):
        source = inspect.getsource(prep.run_worker)
        self.assertNotIn("print(json.dumps(result", source)
        self.assertEqual(source.count("worker_stdout_summary(result"), 2)


if __name__ == "__main__":
    unittest.main()
