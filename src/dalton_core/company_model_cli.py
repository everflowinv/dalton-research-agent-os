"""P13al: the child that decides how one company should be modelled.

Out of process for the same reason every model call here is: the writer
abandons a request after 30 seconds and this one is allowed 300.

One run does four things and stops:

1. pick a company that has filed statements and no current specification, and
   project its filings down to the structure they disclose;
2. if a specification already exists for exactly that structure, return it and
   pay nothing -- a company that has not filed anything new does not need
   deciding about twice;
3. otherwise ask the model, and verify the answer against the filings it was
   shown. A specification resting on a concept the company never reported is
   refused whole, not repaired;
4. store it.

Exit 0 when the run completed, including when it decided nothing needed doing.
``formal_authority_writes`` is always 0: a specification is a judgement about
how to model a company, never a Claim about the world. The numbers it will
eventually produce are the things that have to be cited.

This deliberately uses the strongest model available rather than the cheapest.
Deciding that Accenture is a headcount-times-rate business and IBM is a mix
story is the kind of judgement where a weaker model produces something
plausible and generic, which is worse than nothing -- it looks like a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .cockpit_model import CockpitModel, CockpitModelError, lane_status_for
from .company_model_spec import (
    TASK_HASH,
    CompanyModelSpecError,
    OUTPUT_SCHEMA,
    build_prompt,
    parse_response,
    spec_from_response,
    spec_template_gaps,
)
from .company_model_state import CompanyModelStateError, build_company_model_state
from .driver_template import REGISTRY_HASH as TEMPLATE_REGISTRY_HASH, template_for
from .coverage_mission import CoverageMissionAuthority
from .scheduler import SchedulerError
from .store import DaltonStore, canonical_json, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
# The state is a few hundred concept rows; the answer is a page of structured
# judgement. Both bounds are generous against that.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 6_000
# P13am: the reservation, not the price. The router estimates a call at its
# permitted output and at prompt *bytes* rather than tokens, so it reserves
# roughly four times what the call costs -- IBM's ran for $0.24 against an
# estimate of $0.46, and at 43KB of prompt the estimate was $0.73 and the call
# was refused before it was made. With the day cap at $100 the old $0.60 bought
# nothing but that refusal, so this is sized to admit a large company's
# structure rather than to look frugal.
MAX_COST_USD = 2.50
TIMEOUT_SECONDS = 300
REPAIR_CONTRACT_REF = "contract:company-model-spec-structured-output-repair:0.1"
REPAIR_CONTRACT = {
    "ref": REPAIR_CONTRACT_REF,
    "eligible_error_codes": ["format", "text_length"],
    "validation": "full_spec_from_response_after_every_attempt",
    "text_length_change": "only_overlong_schema_strings_may_change",
    "semantic_failure": "whole_refusal",
}
REPAIR_CONTRACT_HASH = content_hash(REPAIR_CONTRACT)
_REPAIR_AUTHORITY_KEYS = {
    "work_order_ref", "work_order_hash", "result_envelope_ref",
    "result_envelope_hash", "invocation_ref", "route_decision_ref",
}


def model_spec_request_identity(
    state_hash: str,
    task_hash: str = TASK_HASH,
    *,
    repair_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Closed semantic identity for one disclosure and repair policy."""

    resolved = {"max_attempts": 0} if repair_config is None else dict(repair_config)
    return {
        "schema_version": "company-model-spec-request-0.1",
        "state_hash": state_hash,
        "task_hash": task_hash,
        "structured_output_repair": resolved,
    }


def model_spec_request_id(
    state_hash: str,
    task_hash: str = TASK_HASH,
    *,
    repair_config: Mapping[str, Any] | None = None,
) -> str:
    """Scheduler identity for one disclosure under one immutable contract."""

    return content_hash(model_spec_request_identity(
        state_hash, task_hash, repair_config=repair_config,
    ))[:32]


def validate_model_spec_request_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(value)
    config = identity.get("structured_output_repair")
    if (
        set(identity) != {
            "schema_version", "state_hash", "task_hash",
            "structured_output_repair",
        }
        or identity.get("schema_version") != "company-model-spec-request-0.1"
        or not isinstance(identity.get("state_hash"), str)
        or len(identity["state_hash"]) != 64
        or not isinstance(identity.get("task_hash"), str)
        or len(identity["task_hash"]) != 64
        or identity["task_hash"] != TASK_HASH
        or not isinstance(config, Mapping)
        or set(config) != {"max_attempts"}
        or isinstance(config.get("max_attempts"), bool)
        or not isinstance(config.get("max_attempts"), int)
        or config["max_attempts"] < 0
    ):
        raise CockpitModelError("model specification request identity is invalid")
    for name in ("state_hash", "task_hash"):
        if any(ch not in "0123456789abcdef" for ch in identity[name]):
            raise CockpitModelError("model specification request identity is invalid")
    identity["structured_output_repair"] = dict(config)
    return identity


def structured_output_repair_config(
    model_config: Mapping[str, Any] | None,
) -> dict[str, int]:
    if model_config is None:
        return {"max_attempts": 0}
    value = model_config.get("structured_output_repair")
    return {"max_attempts": 0} if value is None else dict(value)


def validate_structured_output_repair_binding(
    value: Mapping[str, Any], *, prompt: str, request_id: str,
) -> dict[str, Any]:
    expected = {
        "schema_version", "root_original", "repair_parent",
        "original_text_sha256", "parent_text_sha256", "state_hash",
        "task_hash", "validation_error", "repair_contract_ref",
        "repair_contract_hash", "repair_prompt_sha256", "repair_config",
        "repair_number",
    }
    binding = dict(value)
    if set(binding) != expected or binding.get("schema_version") != (
        "company-model-spec-repair-binding-0.1"
    ):
        raise CockpitModelError("structured output repair binding is invalid")
    for name in ("root_original", "repair_parent"):
        proof = binding.get(name)
        if (
            not isinstance(proof, Mapping)
            or set(proof) != _REPAIR_AUTHORITY_KEYS
            or any(not isinstance(item, str) or not item for item in proof.values())
        ):
            raise CockpitModelError("structured output repair authority is invalid")
    digest_names = (
        "original_text_sha256", "parent_text_sha256", "state_hash",
        "task_hash", "repair_contract_hash", "repair_prompt_sha256",
    )
    if any(
        not isinstance(binding.get(name), str)
        or len(binding[name]) != 64
        or any(ch not in "0123456789abcdef" for ch in binding[name])
        for name in digest_names
    ):
        raise CockpitModelError("structured output repair digest is invalid")
    error = binding.get("validation_error")
    config = binding.get("repair_config")
    number = binding.get("repair_number")
    if (
        binding["task_hash"] != TASK_HASH
        or binding.get("repair_contract_ref") != REPAIR_CONTRACT_REF
        or binding.get("repair_contract_hash") != REPAIR_CONTRACT_HASH
        or binding["repair_prompt_sha256"]
           != hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        or not isinstance(error, Mapping)
        or set(error) != {"code", "message"}
        or error.get("code") not in REPAIR_CONTRACT["eligible_error_codes"]
        or not isinstance(error.get("message"), str)
        or not error["message"]
        or not isinstance(config, Mapping)
        or set(config) != {"max_attempts"}
        or isinstance(config.get("max_attempts"), bool)
        or not isinstance(config.get("max_attempts"), int)
        or config["max_attempts"] < 0
        or isinstance(number, bool)
        or not isinstance(number, int)
        or not 1 <= number <= config["max_attempts"]
        or request_id != "model-spec-repair:" + content_hash(binding)[:32]
    ):
        raise CockpitModelError("structured output repair binding is invalid")
    return binding


def _repair_prompt(original_text: str, error: CompanyModelSpecError) -> str:
    return (
        "Repair one company-model specification response. Do not add, remove, "
        "or reinterpret model lines, filed concepts, refs, slots, enum choices, "
        "statement choices, or horizon values. Fix only the reported JSON format "
        "or text-length violation. Return JSON matching OUTPUT_SCHEMA and nothing "
        "else. If that cannot be done without a semantic change, return the original "
        "content unchanged.\n\n"
        f"REPAIR_CONTRACT:\n{canonical_json(REPAIR_CONTRACT)}\n\n"
        f"VALIDATION_ERROR:\n{canonical_json({'code': error.code, 'message': str(error)})}\n\n"
        f"OUTPUT_SCHEMA:\n{json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True)}\n\n"
        f"ORIGINAL_MODEL_OUTPUT:\n{original_text}\n"
    )


def _overlong_paths(value: Any, schema: Mapping[str, Any], path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    found: set[tuple[Any, ...]] = set()
    if isinstance(value, str) and isinstance(schema.get("maxLength"), int):
        if len(value) > schema["maxLength"]:
            found.add(path)
    elif isinstance(value, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, item in value.items():
                child = properties.get(key)
                if isinstance(child, Mapping):
                    found.update(_overlong_paths(item, child, path + (key,)))
    elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            found.update(_overlong_paths(item, schema["items"], path + (index,)))
    return found


def _assert_text_length_only_changed(original: Any, repaired: Any) -> None:
    allowed = _overlong_paths(original, OUTPUT_SCHEMA)
    if not allowed:
        raise CompanyModelSpecError(
            "text-length repair has no schema-overlong source field"
        )

    def compare(left: Any, right: Any, path: tuple[Any, ...] = ()) -> None:
        if path in allowed:
            if not isinstance(right, str) or right == left:
                raise CompanyModelSpecError(
                    "text-length repair did not replace the overlong string"
                )
            return
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            if set(left) != set(right):
                raise CompanyModelSpecError(
                    "text-length repair changed the specification structure"
                )
            for key in left:
                compare(left[key], right[key], path + (key,))
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                raise CompanyModelSpecError(
                    "text-length repair changed the specification structure"
                )
            for index, item in enumerate(left):
                compare(item, right[index], path + (index,))
            return
        if type(left) is not type(right) or left != right:
            raise CompanyModelSpecError(
                "text-length repair changed a field that was already valid"
            )

    compare(original, repaired)


def _validated_spec_with_repair(
    *, model: CockpitModel, state: Mapping[str, Any], mission: Mapping[str, Any],
    original_call: Mapping[str, Any], repair_config: Mapping[str, int], decided_by: str,
    repair_attempts: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    root_call = dict(original_call)
    current_call = dict(original_call)
    repairs = [] if repair_attempts is None else repair_attempts
    for repair_number in range(repair_config["max_attempts"] + 1):
        try:
            spec = spec_from_response(state, current_call["text"], decided_by=decided_by)
            return spec, repairs, current_call
        except CompanyModelSpecError as error:
            if (
                error.code not in REPAIR_CONTRACT["eligible_error_codes"]
                or repair_number >= repair_config["max_attempts"]
            ):
                raise
            prompt = _repair_prompt(current_call["text"], error)
            binding = {
                "schema_version": "company-model-spec-repair-binding-0.1",
                "root_original": {
                    key: root_call.get(key) for key in (
                        "work_order_ref", "work_order_hash", "result_envelope_ref",
                        "result_envelope_hash", "invocation_ref", "route_decision_ref",
                    )
                },
                "repair_parent": {
                    key: current_call.get(key) for key in (
                        "work_order_ref", "work_order_hash", "result_envelope_ref",
                        "result_envelope_hash", "invocation_ref", "route_decision_ref",
                    )
                },
                "original_text_sha256": hashlib.sha256(
                    root_call["text"].encode("utf-8")
                ).hexdigest(),
                "parent_text_sha256": hashlib.sha256(
                    current_call["text"].encode("utf-8")
                ).hexdigest(),
                "state_hash": state["state_hash"],
                "task_hash": TASK_HASH,
                "validation_error": {"code": error.code, "message": str(error)},
                "repair_contract_ref": REPAIR_CONTRACT_REF,
                "repair_contract_hash": REPAIR_CONTRACT_HASH,
                "repair_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "repair_config": dict(repair_config),
                "repair_number": repair_number + 1,
            }
            if any(not isinstance(value, str) or not value
                   for value in binding["root_original"].values()):
                raise CompanyModelSpecError(
                    "original model result lacks repair authority"
                ) from error
            if any(not isinstance(value, str) or not value
                   for value in binding["repair_parent"].values()):
                raise CompanyModelSpecError(
                    "repair parent lacks immutable model authority"
                ) from error
            repaired_call = model.call(
                purpose="model_spec",
                request_id="model-spec-repair:" + content_hash(binding)[:32],
                prompt=prompt,
                mission=mission,
                _structured_output_repair=binding,
            )
            # Persist the physical call's accounting references before checking
            # its content.  A paid response that is later refused for semantic
            # drift or another validation failure still happened.
            repairs.append({
                "repair_number": repair_number + 1,
                "work_order_ref": repaired_call.get("work_order_ref"),
                "result_envelope_ref": repaired_call.get("result_envelope_ref"),
                "invocation_ref": repaired_call.get("invocation_ref"),
                "route_decision_ref": repaired_call.get("route_decision_ref"),
                "replayed": bool(repaired_call.get("replayed")),
                "cost_micros": int(repaired_call.get("cost_micros") or 0),
                "binding_hash": content_hash(binding),
            })
            if error.code == "text_length":
                _assert_text_length_only_changed(
                    parse_response(current_call["text"]),
                    parse_response(repaired_call["text"]),
                )
            current_call = dict(repaired_call)
    raise AssertionError("bounded model specification repair did not return")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def filed_classifications(store: Any) -> dict[str, str]:
    """Each company's current ``industry_classification``, or nothing.

    Read from the dossier rather than asked of anybody: the classification is
    the Deep Insight Gate's first question and the dossier is where its answer
    is versioned. A company with no dossier, an undrafted classification block,
    or ``insufficient_evidence`` is simply absent from the map, and the
    specification lane then works from the generic template and says so.
    """

    from .company_dossier import CompanyDossierAuthority
    from .company_dossier_cli import table_exists

    if not table_exists(store.connection, "company_dossier_versions"):
        return {}
    # This is a projection read on the coordinator/child hot path.  Do not run
    # the authority constructor here: it installs/migrates its schema.  The
    # table's owning installer does that, while these bound reads still use the
    # authority's canonical row-validation methods.
    dossiers = object.__new__(CompanyDossierAuthority)
    dossiers.store = store
    dossiers.connection = store.connection
    out: dict[str, str] = {}
    for company_ref in dossiers.companies():
        record = dossiers.latest(company_ref)
        if record is None:
            continue
        word = str((record.get("industry_classification") or {})
                   .get("classification") or "")
        if word and word != "insufficient_evidence":
            out[company_ref] = word
    return out


def choose_company(
    missions: CoverageMissionAuthority, mission: dict[str, Any],
    *, company_ref: str | None = None,
    classifications: Mapping[str, str] | None = None,
    exclude_company_refs: frozenset[str] = frozenset(),
) -> tuple[str | None, dict[str, Any] | None]:
    """The company to decide about, and the disclosure to decide from.

    Companies with statements but no current specification come first, oldest
    filing first so the queue drains in the order the lane filled it. A company
    named explicitly is used as given -- that is the hand-run path. The lane
    may exclude exact companies it has already found durably held while it
    searches the same immutable candidate order for another launchable one.

    The *state* is returned rather than rebuilt by the caller, and this is not
    a convenience. The ticker is part of the state and therefore part of its
    hash, so a projection built without one hashes differently. The first
    version selected companies with a tickerless projection while the run
    stored its specification against a projection with the ticker, so the
    selector could never see the answer it had just produced.

    What that cost was not money -- the child rebuilt the state with the
    ticker, found the stored specification and replayed it for nothing. It cost
    *progress*: the lane relaunched the same company every tick and the other
    four companies would have waited forever behind it. One projection, one
    hash, and the lane moves on.
    """

    universe = {
        str(item.get("company_ref")): item.get("ticker")
        for item in (mission.get("universe") or [])
        if isinstance(item, dict)
    }
    refs = ([company_ref] if company_ref is not None
            else [filing["company_ref"] for filing in missions.statement_filings()])
    for held in refs:
        if company_ref is None and held in exclude_company_refs:
            continue
        if company_ref is None and held not in universe:
            continue
        try:
            state = build_company_model_state(
                missions, held, ticker=universe.get(held),
                # In the hash, deliberately: reclassifying a company is a
                # reason to decide its model again, and a selector that
                # ignored the reclassification would keep replaying the
                # specification written under the old frame.
                industry_classification=(classifications or {}).get(held))
        except CompanyModelStateError:
            if company_ref is not None:
                raise
            continue
        if missions.company_model_spec_for_state(
            held, state["state_hash"], task_hash=TASK_HASH
        ) is None:
            return held, state
    return None, None


def run_model_spec(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None,
    company_ref: str | None = None,
    expected_state_hash: str | None = None,
    expected_task_hash: str | None = None,
    expected_repair_policy_hash: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "model",
        "company_ref": company_ref,
        "state_hash": None,
        "spec_status": None,
        "revenue_drivers": 0,
        "expense_lines": 0,
        "operating_metrics": 0,
        "forecast_statements": [],
        "replayed": False,
        "cost_micros": 0,
        "failure_reason": None,
        "repair_attempts": [],
        "repair_policy_hash": None,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "spec_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        try:
            chosen, state = choose_company(
                missions, mission, company_ref=company_ref,
                classifications=filed_classifications(store))
        except CompanyModelStateError as exc:
            summary.update({"status": "idle", "spec_status": "no_statements",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        if chosen is None:
            # Either every company has a current specification, or the one
            # asked for already does. Both are "nothing to decide".
            summary.update({"status": "idle", "spec_status": "nothing_to_decide"})
            return summary
        summary["company_ref"] = chosen
        summary["state_hash"] = state["state_hash"]
        summary["concepts"] = len(state["concepts"])
        template = template_for(state.get("industry_classification"))
        summary["driver_template"] = {
            "classification": template["classification"],
            "generic": bool(template["generic"]),
            "registry_hash": TEMPLATE_REGISTRY_HASH,
        }
        raw_model_config = (
            None if model_config_path is None
            else json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        )
        repair_config = structured_output_repair_config(raw_model_config)
        repair_policy_hash = content_hash(repair_config)
        summary["repair_policy_hash"] = repair_policy_hash
        if ((expected_state_hash is not None
             and expected_state_hash != state["state_hash"])
                or (expected_task_hash is not None
                    and expected_task_hash != TASK_HASH)
                or (expected_repair_policy_hash is not None
                    and expected_repair_policy_hash != repair_policy_hash)):
            summary.update({
                "status": "succeeded", "spec_status": "stale_input",
                "failure_reason": (
                    "the model specification input changed after its ticket was created"),
            })
            return summary
        if dry_run or model_config_path is None:
            summary.update({
                "status": "succeeded", "spec_status": "gated",
                "failure_reason": None if dry_run else "no model configured",
                "prompt_bytes": len(build_prompt(state).encode("utf-8")),
            })
            return summary

        model = CockpitModel(
            raw_model_config,
            scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        repair_config = structured_output_repair_config(model.config)
        initial_identity = model_spec_request_identity(
            state["state_hash"], repair_config=repair_config,
        )
        try:
            call = model.call(
                purpose="model_spec",
                # Keyed by the disclosure, so an unchanged company replays
                # instead of being paid for again.
                request_id=model_spec_request_id(
                    state["state_hash"],
                    repair_config=repair_config,
                ),
                prompt=build_prompt(state), mission=mission,
                _model_spec_request_identity=initial_identity,
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "spec_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({
                "status": "succeeded",
                # C2: a spent pool is a budget decision, not an outage.
                "spec_status": lane_status_for(exc, "model_unavailable"),
                "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["replayed"] = bool(call.get("replayed"))
        summary["cost_micros"] = int(call.get("cost_micros") or 0)
        repairs: list[dict[str, Any]] = []
        try:
            spec, repairs, accepted_call = _validated_spec_with_repair(
                model=model,
                state=state,
                mission=mission,
                original_call=call,
                repair_config=repair_config,
                decided_by=mission["autonomy"]["automation_principal"],
                repair_attempts=repairs,
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "spec_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({
                "status": "succeeded",
                "spec_status": lane_status_for(exc, "model_unavailable"),
                "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CompanyModelSpecError as exc:
            # Refused whole. A specification with the invented lines stripped
            # out is no longer the model the model meant to describe.
            summary.update({"status": "succeeded", "spec_status": "refused",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
        finally:
            summary["repair_attempts"] = repairs
            summary["cost_micros"] += sum(
                item["cost_micros"] for item in repairs
            )
        if summary.get("spec_status") == "refused":
            return summary
        stored = missions.record_company_model_spec(
            spec, mission_version_ref=mission["id"],
            work_order_ref=accepted_call.get("work_order_ref"),
        )
        # Reported beside the specification, not enforced over it: a template
        # slot the model did not model is a question for the reader, and a
        # refusal here would make a table about a *kind* of company the
        # gatekeeper over a judgement about *this* one.
        summary["template_gaps"] = spec_template_gaps(spec, state)
        summary.update({
            "status": "succeeded", "spec_status": stored["status"],
            "spec_ref": stored["spec_id"],
            "assessment": spec["assessment"],
            "revenue_drivers": len(spec["revenue_drivers"]),
            "expense_lines": len(spec["expense_lines"]),
            "operating_metrics": len(spec["operating_metrics"]),
            "forecast_statements": [
                f"{item['statement']}:{item['importance']}"
                for item in spec["forecast_statements"]
            ],
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--company-ref", help="decide about this company rather than the next")
    parser.add_argument("--expected-state-hash")
    parser.add_argument("--expected-task-hash")
    parser.add_argument("--expected-repair-policy-hash")
    parser.add_argument("--dry-run", action="store_true",
                        help="assemble the state and stop; no model call, no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_model_spec(
        state_dir=args.state_dir, model_config_path=args.model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        scheduler_db=args.scheduler_db, company_ref=args.company_ref,
        expected_state_hash=args.expected_state_hash,
        expected_task_hash=args.expected_task_hash,
        expected_repair_policy_hash=args.expected_repair_policy_hash,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_COST_USD", "build_parser", "choose_company", "main",
    "model_spec_request_id", "model_spec_request_identity", "run_model_spec",
    "structured_output_repair_config", "validate_model_spec_request_identity",
    "validate_structured_output_repair_binding",
]
