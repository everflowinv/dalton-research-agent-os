"""Closed model selection of ranked discovery candidates before acquisition."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .store import canonical_json, content_hash
from .alphaengine_core_search import SEARCH_MAX_RECORDS
from .model_fallback_chain import TIER_CHEAP, register_purpose_tier

CONTRACT_REF = "discovery-candidate-selection-contract:0.1"
PURPOSE = "discovery_selection"
register_purpose_tier(PURPOSE, TIER_CHEAP)
_TEXT_FIELDS = ("title", "publish_time", "rank_date", "document_code", "type_id")
_LIST_FIELDS = ("companies", "industries", "markets", "sources")


class CandidateSelectionError(ValueError):
    pass


class CockpitDiscoveryCandidateSelector:
    def __init__(self, model: Any, *, config_version: str = "0.1") -> None:
        if config_version != "0.1":
            raise CandidateSelectionError("unsupported candidate selector config version")
        self.model = model
        self.config_hash = content_hash({"version": config_version, "purpose": PURPOSE,
                                         "contract_ref": CONTRACT_REF})

    def select(self, view: Mapping[str, Any], *, mission: Mapping[str, Any],
               company: Mapping[str, Any], missing_periods: list[str],
               recovery_epoch: int = 0) -> dict[str, Any]:
        if not isinstance(recovery_epoch, int) or isinstance(recovery_epoch, bool) or recovery_epoch < 0:
            raise CandidateSelectionError("selection recovery epoch is invalid")
        identity = content_hash({"config_hash": self.config_hash,
                                 "view_hash": view["content_hash"], "company": dict(company),
                                 "missing_periods": missing_periods,
                                 "recovery_epoch": recovery_epoch})
        call = self.model.call(
            purpose=PURPOSE, request_id=f"candidate-selection:{identity[:32]}",
            prompt=selection_prompt(view, company=company, missing_periods=missing_periods),
            mission=mission)
        return {**validate_selection(call["text"], view),
                "work_order_ref": call["work_order_ref"],
                "result_envelope_ref": call["result_envelope_ref"],
                "invocation_ref": call["invocation_ref"],
                "route_decision_ref": call["route_decision_ref"],
                "replayed": bool(call.get("replayed")), "config_hash": self.config_hash,
                "recovery_epoch": recovery_epoch}


def candidate_view(raw_response: bytes, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Project only bounded search metadata from exact raw JSON-RPC bytes."""
    import hashlib
    envelope_wire = dict(envelope)
    claimed_envelope_hash = envelope_wire.pop("content_hash", None)
    if claimed_envelope_hash != content_hash(envelope_wire):
        raise CandidateSelectionError("candidate source envelope hash drifted")
    if hashlib.sha256(raw_response).hexdigest() != envelope.get("raw_response_hash"):
        raise CandidateSelectionError("candidate raw response hash drifted")
    try:
        rpc = json.loads(raw_response.decode("utf-8"))
        blocks = rpc["result"]["content"]
        texts = [row["text"] for row in blocks if isinstance(row, Mapping)
                 and row.get("type") == "text"]
        payload = json.loads(texts[0]) if len(texts) == 1 else None
        results = payload["results"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateSelectionError("candidate raw response is invalid") from exc
    if not isinstance(results, list) or len(results) > SEARCH_MAX_RECORDS:
        raise CandidateSelectionError("candidate results are invalid")
    expected = list(envelope.get("source_record_refs") or ())
    candidates = []
    for rank, raw in enumerate(results):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("doc_id"), str):
            raise CandidateSelectionError("candidate result is invalid")
        ref = f"alphaengine-doc:{raw['doc_id']}"
        if rank >= len(expected) or expected[rank] != ref:
            raise CandidateSelectionError("candidate order differs from source authority")
        item: dict[str, Any] = {"document_ref": ref, "rank": rank + 1}
        for field in _TEXT_FIELDS:
            value = raw.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise CandidateSelectionError(f"candidate {field} is invalid")
                item[field] = value[:240]
        snippet = raw.get("snippet")
        if snippet is not None:
            if not isinstance(snippet, str):
                raise CandidateSelectionError("candidate snippet is invalid")
            item["snippet"] = snippet[:800]
            item["snippet_truncated"] = len(snippet) > 800
            item["snippet_hash"] = hashlib.sha256(snippet.encode()).hexdigest()
        for field in _LIST_FIELDS:
            values = raw.get(field)
            if values is not None:
                if (not isinstance(values, list) or len(values) > 20
                        or not all(isinstance(x, str) for x in values)):
                    raise CandidateSelectionError(f"candidate {field} is invalid")
                item[field] = [x[:120] for x in values]
        candidates.append(item)
    if len(candidates) != len(expected):
        raise CandidateSelectionError("candidate count differs from source authority")
    base = {"schema_version": "0.1", "contract_ref": CONTRACT_REF,
            "source_envelope_ref": envelope["id"],
            "source_envelope_hash": envelope["content_hash"], "candidates": candidates}
    return {**base, "content_hash": content_hash(base)}


def selection_prompt(view: Mapping[str, Any], *, company: Mapping[str, Any],
                     missing_periods: list[str]) -> str:
    if (not isinstance(company, Mapping)
            or not all(isinstance(company.get(key), str) and company[key]
                       for key in ("company_ref", "name", "ticker"))
            or set(company) != {"company_ref", "name", "ticker", "aliases"}
            or not isinstance(company["aliases"], list)
            or not all(isinstance(x, str) and x for x in company["aliases"])):
        raise CandidateSelectionError("company selection identity is invalid")
    return ("Select only documents likely to be quarterly earnings-call transcripts for the "
            "specified company and missing periods. Tags are hints, not authority. Do not select "
            "another issuer. Return strict JSON {selected:[{document_ref,reason}]}.\nINPUT="
            + canonical_json({"company": dict(company), "missing_periods": missing_periods,
                              "candidate_view": dict(view)}))


def validate_selection(text: str, view: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CandidateSelectionError("selection is not JSON") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"selected"} or not isinstance(raw["selected"], list):
        raise CandidateSelectionError("selection has an invalid closed shape")
    allowed = {row["document_ref"] for row in view["candidates"]}
    selected = []
    seen = set()
    for row in raw["selected"]:
        if (not isinstance(row, Mapping) or set(row) != {"document_ref", "reason"}
                or row.get("document_ref") not in allowed or row["document_ref"] in seen
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()
                or len(row["reason"]) > 300):
            raise CandidateSelectionError("selection item is invalid")
        seen.add(row["document_ref"])
        selected.append({"document_ref": row["document_ref"], "reason": row["reason"].strip()})
    base = {"schema_version": "0.1", "contract_ref": CONTRACT_REF,
            "candidate_view_hash": view["content_hash"], "selected": selected}
    return {**base, "content_hash": content_hash(base)}
