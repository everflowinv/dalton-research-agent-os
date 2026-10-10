"""Read one unresolved held estimates table outside the writer's tick."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any
from .store import DaltonStore
from .lane_child_launcher import write_owner_only
from .street_estimate_table_model import (MODEL_CONFIG, VERIFIER_CONFIG, EXTRACTOR_REF,
    MAX_CALL_COST_USD, prepare_request, extract_verified, bounded_model_config)


def pending_notes(connection: Any, company_ref: str) -> list[dict[str, Any]]:
    from .street_estimate_tables import EXTRACTOR_REF as deterministic_ref, held_broker_notes
    pending = {r[0] for r in connection.execute(
        "SELECT document_ref FROM street_estimate_figure_scans WHERE company_ref=? "
        "AND extractor_ref=? AND outcome='needs_model'", (company_ref, deterministic_ref))}
    done = {r[0] for r in connection.execute(
        "SELECT document_ref FROM street_estimate_figure_scans WHERE company_ref=? AND extractor_ref=?",
        (company_ref, EXTRACTOR_REF))}
    return [note for note in held_broker_notes(connection, company_ref)
            if note["document_ref"] in pending - done]


def request_for(connection: Any, state_dir: Path, company_ref: str, document_ref: str) -> dict[str, Any]:
    from .street_estimate_tables import note_text, parse_tables, manifest_index
    from .financial_period import fiscal_year_end
    from .street_estimate_table_model import TableModelRefused
    notes = [n for n in pending_notes(connection, company_ref) if n["document_ref"] == document_ref]
    if len(notes) != 1:
        raise TableModelRefused("note is not an unresolved deterministic table scan")
    note = notes[0]
    text, digest, reason = note_text(state_dir, document_ref)
    if text is None:
        raise TableModelRefused(reason)
    calendar = fiscal_year_end(connection, company_ref)
    if calendar is None:
        raise TableModelRefused("filed fiscal calendar is unavailable")
    parsed = parse_tables(text, fye_month=calendar["month"], house=note["broker"], published_on=note["published_on"])
    if parsed["figures"] or not parsed["signals"]:
        raise TableModelRefused("deterministic parsing does not require a model fallback")
    # The original page-one admission supplied company attribution. Read its
    # exact named subject; do not let a model guess an issuer from the note.
    row = connection.execute("SELECT * FROM street_estimates WHERE company_ref=? "
        "AND document_ref=? ORDER BY published_on DESC LIMIT 1", (company_ref, document_ref)).fetchone()
    from .street_estimate import StreetEstimateStore
    attributed = StreetEstimateStore._decode(row) if row else {}
    from .store import content_hash
    manifest = json.loads(manifest_index(state_dir)[document_ref].read_text())
    manifest_hash = manifest.get("content_hash")
    if (manifest.get("document_ref") != document_ref
            or manifest_hash != content_hash({k: v for k, v in manifest.items() if k != "content_hash"})
            or manifest_hash != attributed.get("source_manifest_hash")):
        raise TableModelRefused("held original does not match the admitted broker note receipt")
    subject = attributed.get("subject_as_named")
    request = prepare_request(text, company_ref=company_ref, document_ref=document_ref,
        house=note["broker"], subject_as_named=subject, source_sha256=digest,
        published_on=note["published_on"], fye_month=calendar["month"])
    request.update(source_manifest_hash=manifest_hash, street_estimate_ref=attributed["id"],
                   street_estimate_hash=attributed["content_hash"])
    return request


def run_table(*, state_dir: Path, company_ref: str, document_ref: str,
              expected_request_hash: str, model_factory: Any = None, family_of: Any = None) -> dict[str, Any]:
    from .coverage_mission import CoverageMissionAuthority
    from .street_estimate_tables import StreetEstimateFigureStore
    from .cockpit_model import CockpitModel
    from .document_extraction import validate_model_config
    from .debate_map_draft import route_family
    from .store import content_hash
    state_dir = Path(state_dir)
    summary = {"status": "refused", "document_ref": document_ref, "company_ref": company_ref,
               "cost_micros": None, "cost_authority": "existing model call/day ledgers", "reason": None}
    with DaltonStore(str(state_dir / "core.sqlite")) as store:
        pointer = store.connection.execute("SELECT mission_version_id FROM coverage_mission_pointer "
                                          "ORDER BY mission_ref LIMIT 1").fetchone()
        if pointer is None:
            return {**summary, "reason": "no active mission"}
        authority = CoverageMissionAuthority.__new__(CoverageMissionAuthority)
        authority.connection = store.connection
        mission = authority.mission(pointer[0])
        if "consensus_estimate" not in (mission.get("autonomy") or {}).get("may_write", []):
            return {**summary, "reason": "mission does not grant consensus_estimate"}
        if company_ref not in {r["company_ref"] for r in mission["universe"]}:
            return {**summary, "reason": "company is outside the active mission"}
        request = request_for(store.connection, state_dir, company_ref, document_ref)
        if content_hash(request) != expected_request_hash:
            return {**summary, "reason": "held input changed after selection"}
        configs = [validate_model_config(json.loads((state_dir / name).read_text()))
                   for name in (MODEL_CONFIG, VERIFIER_CONFIG)]
        projections = [bounded_model_config(config, purpose) for config, purpose in zip(configs,
                        ("street_estimate", "street_estimate_verifier"))]
        configs = [item[0] for item in projections]
        factory = model_factory or CockpitModel
        models = [factory(config, scheduler_db=state_dir / "scheduler.sqlite",
            max_input_tokens=24_000, max_output_tokens=3_000,
            max_cost_usd=MAX_CALL_COST_USD, timeout_seconds=120) for config in configs]
        # Config overrides remain governed by the existing immutable call/day
        # contracts. This lane additionally refuses configurations that exceed
        # its two-call envelope; it never raises a mission or pool cap.
        for model, purpose in zip(models, ("street_estimate", "street_estimate_verifier")):
            cap = model.budget_for(purpose)
            if cap["max_cost_usd"] > MAX_CALL_COST_USD or cap["max_input_tokens"] > 24_000 or cap["max_output_tokens"] > 3_000:
                return {**summary, "reason": "street table configuration exceeds its bounded call envelope"}
        families = family_of or (lambda ref: route_family(configs[0]["model_router_db"], ref)
            or route_family(configs[1]["model_router_db"], ref))
        try:
            result = extract_verified(request, producer=models[0], verifier=models[1], mission=mission,
                                      family_of=families)
            result["proof"]["call_budget_bindings"] = [item[1] for item in projections]
        except Exception as exc:
            # A terminal rejected reading is append-only and not repurchased
            # on every tick. Budget/route shortages remain unattempted and are
            # retried only through the existing child ticket/day contract.
            from .street_estimate_table_model import TableModelRefused
            from .street_estimate_extraction import StreetEstimateExtractionError
            if isinstance(exc, (TableModelRefused, StreetEstimateExtractionError)):
                StreetEstimateFigureStore(store).record_document(company_ref=company_ref,
                    document_ref=document_ref, house=request["broker"], published_on=request["published_on"],
                    figures=[], outcome="refused", reason=str(exc), source_sha256=request["source_sha256"],
                    extractor_ref=EXTRACTOR_REF, extraction_method="model")
            return {**summary, "reason": f"{type(exc).__name__}: {exc}"}
        recorded = StreetEstimateFigureStore(store).record_document(company_ref=company_ref,
            document_ref=document_ref, house=request["broker"], published_on=request["published_on"],
            figures=result["figures"], outcome="model_recorded", extraction_method="model",
            source_sha256=request["source_sha256"], extractor_ref=EXTRACTOR_REF)
        return {**summary, **recorded, "status": "succeeded", "cost_micros": result["cost_micros"],
                "request_hash": content_hash(request), "proof": result["proof"]}


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state-dir", "company-ref", "document-ref", "expected-request-hash", "summary-dir"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        summary = run_table(state_dir=Path(args.state_dir), company_ref=args.company_ref,
            document_ref=args.document_ref, expected_request_hash=args.expected_request_hash)
    except Exception as exc:
        summary = {"status": "refused", "reason": f"{type(exc).__name__}: {exc}"}
    write_owner_only(Path(args.summary_dir) / "summary.json", summary)
    # A completed refusal is a terminal ticket, not a crashed child.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
