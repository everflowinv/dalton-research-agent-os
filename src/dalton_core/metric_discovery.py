"""P11g: learn which figures matter from what the market actually cites.

P11f declared the metric base by hand.  That was wrong for the reason the owner
gave: the figures that decide a company differ by industry and by company, and
nobody can enumerate them in advance.  Bookings decide an IT services company,
same-store sales decide a retailer, net interest margin decides a bank, and a
hardcoded list is a guess that goes stale the moment coverage widens.

So the requirement is discovered rather than declared: read the documents that
say what the market is watching -- the company's own press releases and the
sell-side research written about it -- and see which figures they keep citing.
A metric that management leads with and analysts repeat is a metric this
company is judged on.  Then the requirement exists, and extraction can be asked
for it by name.

What does not change is that a requirement, once established, is a *record*:
named, evidenced, countable.  The alternative -- extracting whatever a model
finds interesting in each document -- cannot answer "what does an Initial
Screen need and does this company have it", which is the question the whole
checklist exists to answer.  Discovery decides what goes on the list; it does
not remove the list.

Two rules keep a proposal honest, both the same shape as the digit check:

* the metric's own wording must appear in the quote that proposed it, so a
  model cannot report the market caring about something the document never
  mentions;
* one document is an anecdote.  A requirement needs corroboration across
  distinct documents before it is established, so a single stray sentence does
  not commit the system to hunting a figure forever.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from .store import content_hash

SCHEMA_VERSION = "0.1"
# The document kinds that say what the market watches. A 10-K says what the
# company must report; a press release and a sell-side note say what it is
# judged on, which is a different question and the one being asked here.
SIGNAL_SPEC_REFS: tuple[str, ...] = (
    "sell-side-reports", "earnings-call-transcripts", "company-press-release",
)
# How many distinct documents must cite a metric before it becomes a
# requirement. Two is the smallest number that is not one: it separates "the
# market watches this" from "someone mentioned it once".
MIN_CORROBORATING_DOCUMENTS = 2
MAX_LABEL_CHARS = 120
_METRIC_REF_RE = re.compile(r"metric:[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_UNITS: tuple[str, ...] = ("currency", "percent", "count", "ratio", "days")


class MetricDiscoveryError(ValueError):
    """A metric proposal is malformed, or unsupported by the text it cites."""


def _text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise MetricDiscoveryError(f"{name} must be text")
    stripped = value.strip()
    if not stripped or len(stripped) > maximum:
        raise MetricDiscoveryError(f"{name} must be 1..{maximum} characters")
    return stripped


def _fold(text: str) -> str:
    """Compare wording the way a reader would, not byte for byte."""

    folded = unicodedata.normalize("NFKC", text).lower()
    folded = folded.replace("-", " ").replace("—", " ").replace("–", " ")
    return re.sub(r"\s+", " ", folded).strip()


def validate_metric_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of one proposed metric requirement."""

    if not isinstance(value, Mapping) or set(value) != {
        "metric_ref", "label", "unit", "evidence_phrase", "quote_id", "document_ref",
    }:
        raise MetricDiscoveryError(
            "metric proposal must be exactly metric_ref/label/unit/evidence_phrase/"
            "quote_id/document_ref"
        )
    metric_ref = _text(value["metric_ref"], "metric_ref", maximum=120)
    if _METRIC_REF_RE.fullmatch(metric_ref) is None:
        raise MetricDiscoveryError("metric_ref must look like metric:kebab-case-name")
    if value["unit"] not in _UNITS:
        raise MetricDiscoveryError(f"unit must be one of {list(_UNITS)}")
    return {
        "metric_ref": metric_ref,
        "label": _text(value["label"], "label", maximum=MAX_LABEL_CHARS),
        "unit": value["unit"],
        "evidence_phrase": _text(value["evidence_phrase"], "evidence_phrase", maximum=200),
        "quote_id": _text(value["quote_id"], "quote_id", maximum=100),
        "document_ref": _text(value["document_ref"], "document_ref", maximum=200),
    }


def verify_metric_proposal(
    proposal: Mapping[str, Any], quotes: Mapping[str, str]
) -> dict[str, Any]:
    """Refuse a proposal whose wording is not in the text it cited.

    The same principle as the digit check: a model may say *where* the market
    named a figure, never invent that it did.
    """

    wire = validate_metric_proposal(proposal)
    quote = quotes.get(wire["quote_id"])
    if not isinstance(quote, str) or not quote:
        raise MetricDiscoveryError("metric proposal cites a quote that was not supplied")
    if _fold(wire["evidence_phrase"]) not in _fold(quote):
        raise MetricDiscoveryError(
            "metric proposal cites wording its quote does not contain"
        )
    verified = {**wire, "schema_version": SCHEMA_VERSION, "citation_text": quote}
    verified["content_hash"] = content_hash(verified)
    return verified


def verify_metric_proposals(
    proposals: Any, quotes: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split proposals into supported ones and refusals that say why."""

    if not isinstance(proposals, list):
        raise MetricDiscoveryError("metric proposals must be a list")
    verified: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for item in proposals:
        try:
            verified.append(verify_metric_proposal(item, quotes))
        except MetricDiscoveryError as exc:
            refused.append({
                "reason": str(exc),
                "metric_ref": item.get("metric_ref") if isinstance(item, Mapping) else None,
            })
    return verified, refused


def establish_requirements(
    proposals: Sequence[Mapping[str, Any]],
    *,
    periods: int = 4,
    min_documents: int = MIN_CORROBORATING_DOCUMENTS,
) -> list[dict[str, Any]]:
    """Turn corroborated proposals into requirements, most-cited first.

    Corroboration is counted in *distinct documents*, not in proposals: one
    sell-side note that names revenue growth six times is still one document
    saying so, and counting mentions would let a single verbose source create
    requirements on its own.

    A requirement carries its citations. That is what makes it answerable later
    when someone asks why the system is hunting this figure -- the answer is
    the documents that named it, not "a model decided".

    A metric whose documents disagree about its unit is **contested** and is
    left out; see ``contested`` for who disagreed.  This used to raise, which
    made one local disagreement fatal to the whole company: IBM had 176
    observations and got zero requirements because two documents could not
    agree what net retention rate is measured in.  The disagreement is real and
    worth surfacing, but it is about one metric, and the blast radius should be
    that metric.
    """

    if not isinstance(min_documents, int) or isinstance(min_documents, bool) or min_documents < 1:
        raise MetricDiscoveryError("min_documents must be a positive integer")
    if not isinstance(periods, int) or isinstance(periods, bool) or periods < 1:
        raise MetricDiscoveryError("periods must be a positive integer")
    grouped: dict[str, dict[str, Any]] = {}
    for item in proposals:
        wire = validate_metric_proposal(
            {k: item[k] for k in (
                "metric_ref", "label", "unit", "evidence_phrase", "quote_id", "document_ref",
            )}
        )
        entry = grouped.setdefault(wire["metric_ref"], {
            "metric_ref": wire["metric_ref"],
            "label": wire["label"],
            "unit": wire["unit"],
            "units": set(),
            "documents": {},
        })
        entry["units"].add(wire["unit"])
        entry["documents"].setdefault(wire["document_ref"], wire["evidence_phrase"])
    established: list[dict[str, Any]] = []
    for entry in grouped.values():
        documents = entry.pop("documents")
        # The same name reported in two units is two different figures, and
        # requiring either would make the series meaningless.
        if len(entry.pop("units")) > 1:
            continue
        if len(documents) < min_documents:
            continue
        established.append({
            **entry,
            "periods": periods,
            "cited_by": sorted(documents),
            "citation_count": len(documents),
            "prompt": f"{entry['label']} for the period as reported",
        })
    established.sort(key=lambda item: (-item["citation_count"], item["metric_ref"]))
    return established


def uncorroborated(
    proposals: Sequence[Mapping[str, Any]], *, min_documents: int = MIN_CORROBORATING_DOCUMENTS
) -> list[dict[str, Any]]:
    """Proposals seen too rarely to become requirements, and how often.

    Reported rather than dropped: a metric one document mentions today may be
    the one the market moves to next quarter, and silently discarding it would
    make that invisible.
    """

    seen: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    for item in proposals:
        ref = item.get("metric_ref")
        if not isinstance(ref, str):
            continue
        seen.setdefault(ref, set()).add(str(item.get("document_ref")))
        labels.setdefault(ref, str(item.get("label")))
    return sorted(
        (
            {"metric_ref": ref, "label": labels[ref], "citation_count": len(documents)}
            for ref, documents in seen.items()
            if len(documents) < min_documents
        ),
        key=lambda item: (-item["citation_count"], item["metric_ref"]),
    )


def contested(proposals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Metrics whose documents disagree about the unit, and what they said.

    ``establish_requirements`` leaves these out, and absence on its own reads
    as "nobody mentioned it" -- which is the opposite of the truth.  A
    contested metric is one several documents thought worth naming and could
    not agree how to measure, which is a fact about the metric worth seeing.
    """

    units: dict[str, dict[str, set[str]]] = {}
    labels: dict[str, str] = {}
    for item in proposals:
        ref = item.get("metric_ref")
        unit = item.get("unit")
        if not isinstance(ref, str) or not isinstance(unit, str):
            continue
        units.setdefault(ref, {}).setdefault(unit, set()).add(str(item.get("document_ref")))
        labels.setdefault(ref, str(item.get("label")))
    return sorted(
        (
            {
                "metric_ref": ref,
                "label": labels[ref],
                "units": sorted(by_unit),
                "cited_by": {unit: sorted(docs) for unit, docs in sorted(by_unit.items())},
            }
            for ref, by_unit in units.items()
            if len(by_unit) > 1
        ),
        key=lambda item: item["metric_ref"],
    )


__all__ = [
    "MIN_CORROBORATING_DOCUMENTS",
    "MetricDiscoveryError",
    "SIGNAL_SPEC_REFS",
    "contested",
    "establish_requirements",
    "uncorroborated",
    "validate_metric_proposal",
    "verify_metric_proposal",
    "verify_metric_proposals",
]
