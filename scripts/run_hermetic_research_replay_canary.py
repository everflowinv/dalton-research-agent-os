#!/usr/bin/env python3
"""Run the recorded SEC closure-to-thesis replay canary explicitly.

The selected integration test injects a recorded SEC Company Facts response
and recorded model completions.  It performs no network request and does not
call a model provider.  Keeping this as a separate CI command makes the gate
visible even though the same test also remains part of the full test suite.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

TEST_NAME = (
    "tests.test_thesis_impact_control."
    "ResearchPlanThesisImpactControlTests."
    "test_recorded_supports_path_replays_without_thesis_mutation"
)


def main() -> int:
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_NAME)
    if suite.countTestCases() != 1:
        print(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "canary": "hermetic-research-replay",
                    "status": "failed",
                    "error": "expected exactly one canary test",
                    "test_count": suite.countTestCases(),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    summary = {
        "schema_version": "0.1",
        "canary": "hermetic-research-replay",
        "status": "passed" if result.wasSuccessful() else "failed",
        "test": TEST_NAME,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "network": "recorded-only",
        "model_provider_calls": 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
