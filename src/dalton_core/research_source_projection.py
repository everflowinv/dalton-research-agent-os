"""Typed localization fields and an exact pre-typing publication ledger identity.

Formal research records/hashes are untouched. Only trusted source projectors
emit this marker; no caller-supplied limiter key is accepted. Reconstructing
the previous presentation retains its 24-hour run/call counters after typing.
"""
from __future__ import annotations
import copy
import hashlib
import json
from collections.abc import Mapping

PROJECTION_VERSION = "localization-source-fields:1"
SURFACE_KINDS = frozenset({
    "surface_thesis", "surface_weekly_brief", "surface_event_judgement",
    "surface_thesis_reflection", "surface_deep_insight", "surface_zero_base_review",
    "surface_conviction", "surface_cycle_reflection", "surface_earnings_preview",
    "surface_earnings_calibration", "surface_model_notes",
})
def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()

def section_role(product, row):
    """Validate the closed projector marker and this field's source semantics."""
    if product.get("projection_contract") != PROJECTION_VERSION:
        return "narrative"
    kind = product.get("kind"); role = row.get("localization_role", "narrative")
    if role == "required_empty_body":
        # The old surface projector skipped empty research bodies entirely.
        # This new mandatory-body guard must not create fresh ledger capacity.
        if (kind in {"surface_earnings_preview", "surface_earnings_calibration"}
                and set(row) == {"title", "body", "gaps", "localization_role"}
                and isinstance(row["title"], str) and row["title"]
                and row["body"] == "" and row["gaps"] == []):
            return "narrative"
        raise ValueError("unproved empty narrative guard")
    if role == "narrative": return role
    if kind in SURFACE_KINDS and role in {"display_field", "gap_note"}:
        return role
    if kind == "debate_map" and role == "declared_absence":
        position = row.get("position") or {}
        absent = ((position.get("available") is False and position.get("lean") is None)
                  or (position.get("state") == "none_yet" and position.get("side") is None))
        if absent and row.get("body") == "尚未形成判断" and not row.get("sources") and not row.get("gaps"):
            return role
    if kind == "industry_framework" and role == "gap_appendix":
        gaps = row.get("gaps") or []
        if (row.get("title") == "gaps" and isinstance(gaps,list) and gaps
                and all(isinstance(g,str) and g.strip() for g in gaps)
                and row.get("body") == "\n".join(gaps)
                and not row.get("sources") and not row.get("numbers")):
            return role
    raise ValueError("unproved localization projection field")

SLICE_VERSION = "localization-source-slice:1"


def _slice_parent(product):
    if product.get("content_hash") != _hash({k:v for k,v in product.items() if k!="content_hash"}):
        raise ValueError("localization slice content hash mismatch")
    scope = product.get("projection_slice")
    if (not isinstance(scope, Mapping) or set(scope) != {"schema_version", "source_product", "indices"}
            or scope["schema_version"] != SLICE_VERSION):
        raise ValueError("invalid localization slice scope")
    parent = scope["source_product"]; indices = scope["indices"]
    if not isinstance(parent, Mapping) or "projection_slice" in parent:
        raise ValueError("localization slice requires an unsliced source product")
    legacy_projection(parent)  # Verify complete source before trusting its subset.
    sections = parent.get("sections") or []
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or i < 0 or i >= len(sections) for i in indices)
            or indices != sorted(set(indices))):
        raise ValueError("localization slice indices must be unique and ordered")
    if product.get("sections") != [sections[i] for i in indices]:
        raise ValueError("localization slice sections differ from exact source positions")
    omitted = {"sections", "content_hash", "projection_slice"}
    if ({k:v for k,v in product.items() if k not in omitted}
            != {k:v for k,v in parent.items() if k not in omitted}):
        raise ValueError("localization slice source identity differs")
    return parent


def projection_slice(product, indices):
    """Derive an ordered subset only after proving the complete projector output.

    Parent bytes remain local replay evidence; prompts expose only the selected
    sections. The publication budget identity always stays on the whole source.
    """
    legacy_projection(product)
    if "projection_slice" in product:
        raise ValueError("a localization source cannot be sliced twice")
    rows = product.get("sections") or []
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or i < 0 or i >= len(rows) for i in indices)
            or indices != sorted(set(indices))):
        raise ValueError("localization slice indices must be unique and ordered")
    value = copy.deepcopy(dict(product)); value["sections"] = [copy.deepcopy(rows[i]) for i in indices]
    if product.get("projection_contract") is not None:
        value["projection_slice"] = {"schema_version": SLICE_VERSION,
            "source_product": copy.deepcopy(dict(product)), "indices": list(indices)}
        value["content_hash"] = _hash({k:v for k,v in value.items() if k!="content_hash"})
    return value


def legacy_projection(product):
    """Exactly the product shape generated before typed field projection."""
    if "publication_source_hash" in product or "publication_limiter_key" in product:
        raise ValueError("caller-supplied publication limiter key is forbidden")
    if "projection_slice" in product:
        _slice_parent(product)
    marker = product.get("projection_contract")
    if marker is None:
        if any("localization_role" in row for row in product.get("sections") or []):
            raise ValueError("unbound localization projection field")
        return dict(product)
    if marker != PROJECTION_VERSION or product.get("kind") not in SURFACE_KINDS | {"debate_map", "industry_framework"}:
        raise ValueError("unknown localization projection contract")
    value = copy.deepcopy(dict(product));value.pop("projection_contract")
    value.pop("projection_slice", None)
    kept = []
    for row in value.get("sections") or []:
        role = section_role(product,row)
        if row.get("localization_role") == "required_empty_body":
            continue
        kept.append(row)
        row.pop("localization_role",None)
        if role == "gap_appendix": row["body"] = ""
    value["sections"] = kept
    if product["kind"] == "industry_framework" and not any(r.get("title")=="gaps" for r in value.get("sections") or []):
        # deliverable_sections always emitted this optional, empty appendix.
        value["sections"].append({"title":"gaps","body":"","sources":[],"numbers":[],"gaps":[]})
    if product["kind"] in SURFACE_KINDS:
        if product.get("content_hash") != _hash({k:v for k,v in product.items() if k!="content_hash"}):
            raise ValueError("localization projection content hash mismatch")
        value["content_hash"] = _hash({k:v for k,v in value.items() if k!="content_hash"})
    return value

def publication_source_hash(product):
    # Publication/localization ledger hashes include the canonical final newline;
    # the surface product's inner content_hash deliberately does not.
    value = legacy_projection(product)
    if "projection_slice" in product:
        return publication_source_hash(_slice_parent(product))
    encoded = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",", ":")) + "\n"
    return hashlib.sha256(encoded.encode()).hexdigest()
