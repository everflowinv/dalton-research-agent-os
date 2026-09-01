#!/usr/bin/env python3
"""Build the first ACN financial model on an isolated copy of the live Core.

Copies the live Core read-only, admits the formal ACN quarterly revenue
actual from the live SEC claim's evidence, admits the thesis-derived growth
assumption and base scenario through the human decision path, then runs the
deterministic growth extension to derive four forecast quarters and their
model run.  Verifies hash bindings, replay idempotency and immutability.
Nothing touches the live Core, the network, or paid models.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from dalton_core.model_forecast import (
    FORMULA_HASH,
    FORMULA_REF,
    ModelForecastAuthority,
    extend_growth,
)
from dalton_core.model_input import ModelInputLedger
from dalton_core.store import DaltonStore


ACN = "company:sec-cik:0001467373"
ACN_CLAIM = "claim-version:1c4f31d84d431ac4aa2cfd5e10624ed9f0ddff0f17844392bc7a76a339adfa05"
OWNER = "human:isolated-canary-owner"


def run(source_core: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="m1-model-canary-") as directory:
        copied = Path(directory) / "core.sqlite"
        reader = sqlite3.connect(f"{source_core.as_uri()}?mode=ro", uri=True)
        writer = sqlite3.connect(copied)
        reader.backup(writer)
        writer.close()
        reader.close()

        store = DaltonStore(str(copied))
        try:
            ledger = ModelInputLedger(store)
            forecast = ModelForecastAuthority(store)
            connection = store.connection

            claim_row = connection.execute(
                "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
                (ACN_CLAIM,),
            ).fetchone()
            claim = json.loads(claim_row["claim_json"])
            evidence_ref = connection.execute(
                "SELECT evidence_version_id FROM evidence_relations "
                "WHERE claim_version_id=?", (ACN_CLAIM,),
            ).fetchone()["evidence_version_id"]
            evidence_row = connection.execute(
                "SELECT evidence_json, content_hash FROM evidence_versions "
                "WHERE evidence_version_id=?", (evidence_ref,),
            ).fetchone()

            scenario = ledger.decide_input(
                decision_id="decision:input:acn:scenario:1",
                candidate_id="candidate:input:acn:scenario:1",
                candidate_hash=ledger.propose_input(
                    candidate_id="candidate:input:acn:scenario:1",
                    input_kind="scenario",
                    model_input_ref="input:acn:scenario:base",
                    prior_version_ref=None,
                    payload={
                        "schema_version": "0.1",
                        "scenario_ref": "input:acn:scenario:base",
                        "label": "Base",
                        "description": "ACN base scenario bound to the FY2026 Q3 actual.",
                        "base_scenario_version_ref": None,
                        "base_scenario_version_hash": None,
                        "owner_ref": OWNER,
                    },
                    proposed_by="agent:researcher",
                    idempotency_key="candidate:input:acn:scenario:1",
                )["candidate"]["content_hash"],
                verdict="admit", rationale="Isolated canary scenario admission.",
                findings=[], reviewer_ref=OWNER,
                version_id="input-version:acn:scenario:base:1",
                idempotency_key="decision:input:acn:scenario:1",
            )["version"]

            actual = ledger.decide_input(
                decision_id="decision:input:acn:actual:1",
                candidate_id="candidate:input:acn:actual:1",
                candidate_hash=ledger.propose_input(
                    candidate_id="candidate:input:acn:actual:1",
                    input_kind="actual",
                    model_input_ref="input:acn:revenue",
                    prior_version_ref=None,
                    payload={
                        "schema_version": "0.1",
                        "metric_ref": "metric:revenue-usd",
                        "subject_ref": ACN,
                        "business_line_ref": None,
                        "period": {
                            "start": "2026-03-01", "end": "2026-05-31",
                            "calendar": "company:fiscal", "kind": "quarter",
                        },
                        "unit": "million", "currency": "USD",
                        "value": "18718.144",
                        "source_authorities": [{
                            "authority_kind": "evidence_version",
                            "version_ref": evidence_ref,
                            "content_hash": evidence_row["content_hash"],
                        }],
                    },
                    proposed_by="agent:researcher",
                    idempotency_key="candidate:input:acn:actual:1",
                )["candidate"]["content_hash"],
                verdict="admit", rationale="Bound to the live SEC lane claim evidence.",
                findings=[], reviewer_ref=OWNER,
                version_id="input-version:acn:revenue:1",
                idempotency_key="decision:input:acn:actual:1",
            )["version"]

            growth = ledger.decide_input(
                decision_id="decision:input:acn:growth:1",
                candidate_id="candidate:input:acn:growth:1",
                candidate_hash=ledger.propose_input(
                    candidate_id="candidate:input:acn:growth:1",
                    input_kind="assumption",
                    model_input_ref="input:acn:revenue-growth",
                    prior_version_ref=None,
                    payload={
                        "schema_version": "0.1",
                        "driver_ref": "driver:bookings-mix-and-conversion",
                        "subject_ref": ACN,
                        "effective_period": {
                            "start": "2026-06-01", "end": "2027-08-31",
                            "calendar": "company:fiscal", "kind": "forecast_period",
                        },
                        "unit": "percent", "currency": "USD",
                        "value": "1.15", "formula": None,
                        "scenario_version_ref": scenario["id"],
                        "scenario_version_hash": scenario["content_hash"],
                        "owner_ref": OWNER,
                        "rationale": (
                            "Thesis implied_expectation is mid-single-digit "
                            "local-currency annual growth (~4.7%); compounded "
                            "to ~1.15% per fiscal quarter."
                        ),
                        "provenance": "judgment",
                        "source_authorities": [],
                        "dependency_bindings": [],
                    },
                    proposed_by="agent:researcher",
                    idempotency_key="candidate:input:acn:growth:1",
                )["candidate"]["content_hash"],
                verdict="admit",
                rationale="Derives from the admitted ACN thesis expectation.",
                findings=[], reviewer_ref=OWNER,
                version_id="input-version:acn:revenue-growth:1",
                idempotency_key="decision:input:acn:growth:1",
            )["version"]

            first = extend_growth(
                ledger, forecast,
                base_input_version_ref=actual["id"],
                growth_input_version_ref=growth["id"],
                periods=4,
                line_ref_prefix="forecast-line:acn:revenue",
                model_run_ref="model-run:acn-revenue-growth-extend",
                idempotency_key="m1-canary:acn:extend:1",
            )
            replay = extend_growth(
                ledger, forecast,
                base_input_version_ref=actual["id"],
                growth_input_version_ref=growth["id"],
                periods=4,
                line_ref_prefix="forecast-line:acn:revenue",
                model_run_ref="model-run:acn-revenue-growth-extend",
                idempotency_key="m1-canary:acn:extend:1",
            )
            if first["status"] != "fresh" or replay["status"] != "duplicate":
                raise RuntimeError("growth extension did not replay idempotently")
            for line in first["lines"]:
                if line["formula_ref"] != FORMULA_REF or line["formula_hash"] != FORMULA_HASH:
                    raise RuntimeError("derived line lost its formula binding")
                if line["scenario_version_ref"] != scenario["id"]:
                    raise RuntimeError("derived line lost its scenario binding")
            try:
                connection.execute(
                    "UPDATE model_forecast_line_versions SET content_hash='" + "0" * 64 + "'"
                )
                raise RuntimeError("forecast lines were mutable")
            except Exception:
                pass
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            return {
                "status": "passed",
                "mode": "isolated-live-copy",
                "paid_model_calls": 0,
                "scenario_version_ref": scenario["id"],
                "actual_version_ref": actual["id"],
                "growth_version_ref": growth["id"],
                "model_run_ref": first["model_run"]["model_run"]["id"],
                "line_count": len(first["lines"]),
                "lines": [
                    {
                        "line_ref": line["line_ref"],
                        "period": line["period"],
                        "value": line["value"],
                        "value_kind": line["value_kind"],
                    }
                    for line in first["lines"]
                ],
                "claim_statement": claim["normalized_statement"][:80],
                "integrity_check": integrity,
            }
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-core", type=Path,
        default=Path(
            "/Users/everflow/Library/Application Support/Dalton/state/dalton-core/"
            "core.sqlite"
        ),
    )
    args = parser.parse_args()
    result = run(args.source_core.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
