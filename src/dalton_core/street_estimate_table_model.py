"""Bounded, independently checked reading of a held broker estimates table."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from datetime import date
from typing import Any, Mapping

from .store import canonical_json, content_hash
from .street_estimate_extraction import build_prompt, build_request, MAX_ESTIMATE_ROWS
from .street_estimate_tables import _fiscal, _from_model, _run_labels, _scale_in, period_cell

EXTRACTOR_REF = "extractor:street-estimate-tables:verified-model:1"
MODEL_CONFIG = "street-estimate-model-config.json"
VERIFIER_CONFIG = "street-estimate-verifier-model-config.json"
MAX_SOURCE_CHARS = 16_000
MAX_CALL_COST_USD = .25
PURPOSE = "street_estimate"
VERIFIER_PURPOSE = "street_estimate_verifier"
CONTRACT_HASH = content_hash({"rule": EXTRACTOR_REF, "max_source_chars": MAX_SOURCE_CHARS,
    "check": "same-source-company-period-currency-unit-scale-column-and-current-house-estimate"})


class TableModelRefused(ValueError):
    pass


def register_contract() -> None:
    from .cockpit_model import register_purpose
    register_purpose(PURPOSE)
    register_purpose(VERIFIER_PURPOSE)


def bounded_model_config(config: Mapping[str, Any], purpose: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A narrower call envelope, preserving every lower owner/shared limit."""
    from .call_budget import resolve_call_budget
    envelope = {"max_input_tokens": 24_000, "max_output_tokens": 3_000,
                "max_cost_usd": MAX_CALL_COST_USD, "timeout_seconds": 120}
    resolved_config = dict(config)
    shared = None
    if config.get("shared_call_budget_policy_path"):
        from .shared_call_budget_policy import load_shared_call_budget_policy, effective_shared_max_cost
        shared = load_shared_call_budget_policy(config["shared_call_budget_policy_path"])
        resolved_config.pop("shared_call_budget_policy_path")
    owner = resolve_call_budget(resolved_config, purpose, defaults=envelope)
    configured = dict(owner)
    shared_max = None
    if shared is not None:
        shared_max = effective_shared_max_cost(shared, purpose)
        owner["max_cost_usd"] = min(owner["max_cost_usd"], shared_max)
    effective = {k: min(v, owner[k]) for k, v in envelope.items()}
    projected = dict(config)
    # Shared policy has already been read and constrains effective. Resolve it
    # once per child; reapplying its larger default would undo the lane cap.
    projected.pop("shared_call_budget_policy_path", None)
    projected["purpose_call_budgets"] = {**config.get("purpose_call_budgets", {}), purpose: effective}
    projected["provider_retry"] = {"max_same_profile_retries": 0, "retry_backoff_seconds": 0}
    return projected, {"source_config_hash": content_hash(config), "owner_budget": owner,
                       "configured_budget": configured, "shared_max_cost_usd": shared_max,
                       "effective_budget": effective,
                       "shared_policy_hash": shared["content_hash"] if shared else None,
                       "shared_policy_path": config.get("shared_call_budget_policy_path")}


def prepare_request(text: str, *, company_ref: str, document_ref: str, house: str,
                    subject_as_named: str, source_sha256: str, published_on: str,
                    fye_month: int) -> dict[str, Any]:
    if not text:
        raise TableModelRefused("held note is empty")
    if hashlib.sha256(text.encode()).hexdigest() != source_sha256:
        raise TableModelRefused("held source bytes do not match their receipt")
    if not subject_as_named or subject_as_named.casefold() not in text.casefold():
        raise TableModelRefused("company name is not present in the held original")
    if not house or type(fye_month) is not int or fye_month not in range(1, 13):
        raise TableModelRefused("broker, publication date and filed fiscal calendar are required")
    try:
        if date.fromisoformat(published_on).isoformat() != published_on:
            raise ValueError("not an ISO date")
    except (ValueError, TypeError):
        raise TableModelRefused("publication date must be an exact ISO calendar date") from None
    # Exact prefix, ending at a line boundary. This is an explicit reading
    # scope, not a claim that the whole note was read. No cell outside this
    # window or a table whose header was clipped may be accepted.
    end = min(len(text), MAX_SOURCE_CHARS)
    if end < len(text):
        end = text.rfind("\n", 0, end) + 1 or end
    window = text[:end]
    if subject_as_named.casefold() not in window.casefold():
        raise TableModelRefused("company name is outside the bounded reading scope")
    window_hash = hashlib.sha256(window.encode()).hexdigest()
    quotes = [{"quote_id": f"quote:0:{end}:{window_hash[:16]}", "raw_text": window}]
    request = build_request({"company_ref": company_ref, "document_ref": document_ref,
                             "broker": house, "quotes": quotes})
    request.update({"contract_hash": CONTRACT_HASH, "source_sha256": source_sha256,
                    "subject_as_named": subject_as_named, "published_on": published_on,
                    "fye_month": fye_month, "source_window": {"start": 0, "end": end,
                        "sha256": window_hash, "source_chars": len(text), "truncated": end < len(text)}})
    return request


def _candidates(response: Any, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(response, Mapping) or set(response) != {"schema_version", "estimates"}:
        raise TableModelRefused("table response has an invalid closed shape")
    fields = {"quote_id", "metric_ref", "as_reported_label", "period", "value", "unit",
              "currency", "scale", "superseded"}
    if not isinstance(response["estimates"], list):
        raise TableModelRefused("estimates must be a list")
    if len(response["estimates"]) > MAX_ESTIMATE_ROWS:
        raise TableModelRefused("too many estimates in one bounded reading")
    for row in response["estimates"]:
        if not isinstance(row, Mapping) or set(row) != fields or type(row["superseded"]) is not bool:
            raise TableModelRefused("each table row must match the closed extraction contract")
        if row["metric_ref"] not in {"metric:eps-estimate", "metric:revenue-estimate"}:
            raise TableModelRefused("table metric is not revenue or EPS")
    from .document_numeric_claim import NumericCandidateError, validate_numeric_candidate, _fold_words
    from .document_figure_grade import BROKER_RESEARCH
    verified = []
    quotes = {q["quote_id"]: q["raw_text"] for q in request["quotes"]}
    for row in response["estimates"]:
        if row["superseded"]:
            continue
        try:
            candidate = validate_numeric_candidate({k: row[k] for k in
                ("quote_id", "metric_ref", "as_reported_label", "period", "value", "unit", "currency", "scale")} |
                {"subject_as_named": request["subject_as_named"], "basis": "broker-estimate"}, grade=BROKER_RESEARCH)
        except NumericCandidateError as exc:
            # Invalid paid output is a completed source reading, not a route
            # shortage. Persist its refusal so the normal scanner cannot buy
            # this same note again on later ticks or a new day.
            raise TableModelRefused(f"numeric table candidate is invalid: {exc}") from exc
        quote = quotes.get(candidate["quote_id"])
        if not quote or _fold_words(candidate["as_reported_label"]) not in _fold_words(quote):
            raise TableModelRefused("table label is not in its exact supplied quote")
        # PDF table cells often sit on successive lines. The prose tokenizer
        # accepts whitespace inside a number and can merge those cells. Keep
        # explicit decimal/thousands boundaries here, never a newline group.
        tokens = re.findall(r"(?<![\d.,])(?:\((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)|[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?)(?![\d.,])", quote)
        present = {Decimal((("-" + t[1:-1]) if t.startswith("(") else t).replace(",", "")) for t in tokens}
        if Decimal(candidate["value"]) not in present:
            raise TableModelRefused("table value must be the printed number before scaling")
        verified.append({**candidate, "citation_text": quote,
            "citation_hash": content_hash({"quote_id": candidate["quote_id"], "raw_text": quote})})
    figures = []
    identities = set()
    for item in verified:
        p = period_cell(item["period"])
        if p is None or p["form"] not in {"y", "qy"} or p.get("estimate") is False:
            raise TableModelRefused("estimate must bind an explicit fiscal year/quarter header")
        fiscal = _fiscal(p, request["fye_month"])
        if not fiscal or fiscal["period_end"] <= request["published_on"][:10]:
            raise TableModelRefused("reported periods cannot be filed as forward estimates")
        if item["unit"] != "currency" or item["currency"] is None:
            raise TableModelRefused("EPS and revenue require an evidenced currency")
        if item["metric_ref"] == "metric:eps-estimate" and item.get("scale") not in {None, "one"}:
            raise TableModelRefused("EPS scale must be per share, without a revenue scale")
        figure = _from_model(item, request["fye_month"])
        if figure is None:
            raise TableModelRefused("table period cannot be mapped to the filed fiscal calendar")
        identity = (figure["metric"], figure["period"])
        if identity in identities:
            raise TableModelRefused("duplicate table metric/period")
        identities.add(identity)
        figure["currency"] = item["currency"]
        figures.append(figure)
    return figures


def extract_verified(request: Mapping[str, Any], *, producer: Any, verifier: Any,
                     mission: Mapping[str, Any], family_of: Any) -> dict[str, Any]:
    """At most one extraction and one independent check; no paid repair."""
    if "consensus_estimate" not in (mission.get("autonomy") or {}).get("may_write", []):
        raise TableModelRefused("mission does not grant consensus_estimate")
    from .cockpit_model import unwrap_json_object
    register_contract()
    identity = content_hash(request)
    draft = producer.call(purpose=PURPOSE, request_id=f"street-table:{identity}",
        prompt=build_prompt(request) + "\nUse only complete headers and cells within the supplied source window. "
            "Never complete a clipped table. EPS unit is currency per share, currency names its ISO code, "
            "EPS scale is null; never use one, units, or per_share as a scale. "
            "Revenue scale must be null or one of thousand/million/billion/trillion, and must be printed in the original.\nHeld original and attribution:\n" + canonical_json(request),
        mission=mission)
    response = unwrap_json_object(draft.get("text", ""))
    figures = _candidates(response, request)
    # An empty reading is a refusal, rather than a successful resolved gap.
    if not figures:
        raise TableModelRefused("no forward house estimates were read")
    candidate_hash = content_hash(response)
    check_input = {"request": request, "candidate": response, "candidate_hash": candidate_hash}
    check = verifier.call(purpose=VERIFIER_PURPOSE, request_id=f"street-table-check:{identity}:{candidate_hash}",
        producer_route_decision_refs=[draft.get("route_decision_ref")], mission=mission,
        prompt=("Independently check EVERY proposed current house estimate against the held original. "
            "Reject peer/actual/prior/consensus cells, incorrect columns, fiscal periods, scale, currency "
            "or EPS unit. Do not repair the extraction. Return exactly {verdict: pass|reject, "
            "company_ref, document_ref, source_sha256, candidate_hash, checked_rows: [zero-based "
            "indices of ALL non-superseded candidate rows], cell_bindings: [{row_index, "
            "period_as_printed, period_kind: fiscal_year|quarter, currency_as_printed, "
            "scale_as_printed: string|null, unit_as_printed}], reason: nonempty string}. "
            "Each binding quotes exact original strings, including the header; for EPS the unit "
            "text must be the printed EPS/per-share label and scale_as_printed is null. "
            "A pass requires all identities and dimensions to be supported by this original.\n"
            + canonical_json(check_input)))
    producer_family, verifier_family = family_of(draft.get("route_decision_ref")), family_of(check.get("route_decision_ref"))
    if (not producer_family or not verifier_family or producer_family == verifier_family
            or any(str(f).startswith("unclassified:") for f in (producer_family, verifier_family))):
        raise TableModelRefused("table check is not an independent model family")
    verdict = unwrap_json_object(check.get("text", ""))
    fields = {"verdict", "company_ref", "document_ref", "source_sha256", "candidate_hash", "checked_rows", "cell_bindings", "reason"}
    if not isinstance(verdict, Mapping) or set(verdict) != fields:
        raise TableModelRefused("independent table check has an invalid closed shape")
    for key in ("company_ref", "document_ref", "source_sha256"):
        if verdict[key] != request[key]:
            raise TableModelRefused("independent table check changed the original identity")
    expected = [i for i, row in enumerate(response["estimates"]) if not row["superseded"]]
    if (verdict["verdict"] != "pass" or verdict["candidate_hash"] != candidate_hash
            or verdict["checked_rows"] != expected
            or any(type(i) is not int for i in verdict["checked_rows"])
            or not isinstance(verdict["reason"], str) or not verdict["reason"].strip()):
        raise TableModelRefused("independent table check did not accept every bound estimate")
    bindings = verdict["cell_bindings"]
    if not isinstance(bindings, list) or len(bindings) != len(expected):
        raise TableModelRefused("every checked cell needs its original dimensions")
    for index, binding, figure in zip(expected, bindings, figures):
        required = {"row_index", "period_as_printed", "period_kind", "currency_as_printed",
                    "scale_as_printed", "unit_as_printed"}
        if not isinstance(binding, Mapping) or set(binding) != required or type(binding["row_index"]) is not int or binding["row_index"] != index:
            raise TableModelRefused("cell binding has an invalid closed identity")
        row = response["estimates"][index]
        quote = next(q["raw_text"] for q in request["quotes"] if q["quote_id"] == row["quote_id"])
        for key in ("period_as_printed", "currency_as_printed", "unit_as_printed"):
            if not isinstance(binding[key], str) or not binding[key] or binding[key] not in quote:
                raise TableModelRefused("cell dimension is not quoted from the held original")
        header = period_cell(binding["period_as_printed"])
        if header is None or header.get("estimate") is False:
            raise TableModelRefused("checked header is not an estimated fiscal period")
        mapped = _run_labels({"periods": [header]}, request["fye_month"])[0]
        if not mapped or (mapped["label"], mapped["period_kind"]) != (figure["period"], binding["period_kind"]) or binding["period_kind"] != figure["period_kind"]:
            raise TableModelRefused("checked header maps to a different fiscal period")
        currency = binding["currency_as_printed"]
        aliases = {"USD": {"USD", "US$", "$"}, "EUR": {"EUR", "€"}, "GBP": {"GBP", "£"}}
        if currency not in aliases.get(row["currency"], {row["currency"]}):
            raise TableModelRefused("checked currency disagrees with the candidate")
        scale = binding["scale_as_printed"]
        if figure["metric"] == "revenue" and row["scale"] is not None:
            if not isinstance(scale, str) or scale not in quote or _scale_in(scale) != row["scale"]:
                raise TableModelRefused("checked revenue scale is not printed in the original")
        elif scale is not None:
            raise TableModelRefused("unscaled cells cannot carry a different checked scale")
        if figure["metric"] == "eps" and not any(s in binding["unit_as_printed"].casefold() for s in ("eps", "per share", "per-share")):
            raise TableModelRefused("checked EPS unit does not name a per-share line")
    proof = {"extractor_ref": EXTRACTOR_REF, "contract_hash": CONTRACT_HASH,
        "request_hash": identity, "candidate_hash": candidate_hash,
        "source_sha256": request["source_sha256"], "document_ref": request["document_ref"],
        "company_ref": request["company_ref"], "published_on": request["published_on"],
        "fye_month": request["fye_month"], "verification": dict(verdict),
        "source_window": request["source_window"],
        "source_manifest_hash": request.get("source_manifest_hash"),
        "street_estimate_ref": request.get("street_estimate_ref"),
        "street_estimate_hash": request.get("street_estimate_hash"),
        "producer_work_order_ref": draft.get("work_order_ref"),
        "verifier_work_order_ref": check.get("work_order_ref"),
        "producer_route_decision_ref": draft["route_decision_ref"],
        "verifier_route_decision_ref": check["route_decision_ref"],
        "producer_family": producer_family, "verifier_family": verifier_family}
    for figure in figures:
        figure["model_proof"] = proof
    return {"status": "model_recorded", "figures": figures, "proof": proof,
            "cost_micros": int(draft.get("cost_micros", 0)) + int(check.get("cost_micros", 0))}
