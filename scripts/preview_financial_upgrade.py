"""Read-only financial upgrade/material plan; never calls a model or writer."""
import argparse
import json
import sqlite3
from pathlib import Path


def preview(state_dir):
    from dalton_core.coverage_mission import CoverageMissionAuthority
    from dalton_core.company_model_inputs import build_model_inputs
    from dalton_core.company_model_forecast import forecast_reporting_boundary
    from dalton_core.company_model_forecast_cli import needs_model
    from dalton_core.forecast_material_gaps import gap_plans
    from dalton_core.model_forecast_driver import validate_forecast_model
    from dalton_core.economic_invariants import FORECAST_INVARIANT_CONTRACT_HASH
    db = Path(state_dir).resolve()/"core.sqlite"
    with sqlite3.connect(db.as_uri()+"?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        missions = CoverageMissionAuthority.__new__(CoverageMissionAuthority)
        missions.connection = c
        pointer = c.execute("SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1").fetchone()
        if pointer is None:
            return {"mode": "read_only", "status": "no_mission", "companies": []}
        mission = missions.mission(pointer[0])
        companies = []
        for entry in mission["universe"]:
            ref = entry["company_ref"]
            row = {"company_ref": ref, "status": "unavailable"}
            spec = table = record = None
            try:
                spec = missions.latest_company_model_spec(ref)
                if spec is None:
                    row.update(reason="no held model specification")
                    companies.append(row)
                    continue
                stored = c.execute("SELECT record_json FROM forecast_model_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1", (ref,)).fetchone()
                record = validate_forecast_model(json.loads(stored[0])) if stored else None
                table = build_model_inputs(missions, spec)
                boundary = forecast_reporting_boundary(missions, c, ref, table=table)
                row.update(status="planned", spec_ref=spec["spec_id"], model_ref=record.get("id") if record else None,
                    reporting_boundary=boundary, action=needs_model(record, spec, table, reporting_boundary=boundary),
                    material_requests=gap_plans(spec, table, record=record))
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                row.update(reason=failure, material_requests=gap_plans(spec, table or {}, record=record, failure=failure) if spec else [])
            companies.append(row)
    return {"mode": "read_only", "production_writes": 0, "model_calls": 0,
            "validator_contract_hash": FORECAST_INVARIANT_CONTRACT_HASH, "companies": companies}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--output", type=Path)
    args=p.parse_args(argv)
    text=json.dumps(preview(args.state_dir), ensure_ascii=False, indent=2)+"\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
