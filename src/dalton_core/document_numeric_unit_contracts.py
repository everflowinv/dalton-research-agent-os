"""Scoped directed-numeric units; the legacy extraction contract stays frozen.

A duration in years is recorded as years, never converted to days. The held
Figure's verifier marker selects the replay contract, so caller text cannot
silently expand the unit vocabulary of an old Figure.
"""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal
from typing import Any

DIRECTED_NUMERIC_CONTRACT_REF = "directed-document-numeric:0.1"
LEGACY_FIGURE_VERIFIER_REF = "verifier:document-figure-citation-digits:0.1"
DIRECTED_FIGURE_VERIFIER_REF = "verifier:document-figure-citation-digits-directed:0.1"
LEGACY_NUMERIC_UNITS = ("percent", "currency", "count", "ratio", "days", "basis_points")
# Explicit quantity-unit adjacency, not a year elsewhere in the paragraph.
_YEAR_AMOUNT = re.compile(r"(?<![\w.])(?P<value>[-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:-\s*)?(?:years?|yrs?\.?|年)(?!\w)", re.IGNORECASE)

def numeric_units(contract_ref: str | None = None) -> tuple[str, ...]:
    if contract_ref is None:
        return LEGACY_NUMERIC_UNITS
    if contract_ref != DIRECTED_NUMERIC_CONTRACT_REF:
        raise ValueError("unknown document numeric unit contract")
    return (*LEGACY_NUMERIC_UNITS, "years")

def verify_years_quantity(value: str, quote: str, *, currency: Any, scale: Any) -> None:
    if currency is not None or scale is not None:
        raise ValueError("a years figure must have null currency and scale")
    normalized = unicodedata.normalize("NFKC", quote).replace("−", "-")
    present = {Decimal(m.group("value").replace(",", "")) for m in _YEAR_AMOUNT.finditer(normalized)}
    if Decimal(value) not in present:
        raise ValueError("a years figure must cite its exact value explicitly reported in years")

def figure_numeric_contract(verifier_ref: Any) -> str | None:
    if verifier_ref == LEGACY_FIGURE_VERIFIER_REF:
        return None
    if verifier_ref == DIRECTED_FIGURE_VERIFIER_REF:
        return DIRECTED_NUMERIC_CONTRACT_REF
    raise ValueError("figure numeric verifier contract is not recognized")
