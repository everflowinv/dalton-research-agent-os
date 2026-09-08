"""P11f/P11g: which figures a stage needs, so extraction is asked and not guessed.

A 10-Q holds hundreds of numbers and almost none of them matter yet.  Both
obvious answers are wrong: extracting everything fills the ledger with noise
nobody asked for, and extracting whatever a model finds interesting makes the
mission's own requirements non-deterministic -- you could not say what an
Initial Screen needs, or check whether a company has it.

So figures work the way documents already do.  ``SOURCE_BASE_ITEMS`` says which
documents a stage needs and the checklist counts what is held; this says which
figures it needs and counts the same way.  What it does *not* do any more is
name them in advance.  P11f hardcoded that list and the owner was right to
reject it: the figures that decide a company differ by industry and by company,
and no list written today survives coverage widening.

The requirement above a universal floor is discovered instead -- from the
documents that say what the market is watching, corroborated across sources,
carrying its citations (see ``metric_discovery``).  Discovery decides what goes
on the list; it does not remove the list, because "what does this screen need
and does this company have it" is the question the whole checklist exists to
answer.

Depth still arrives by entering a deeper stage rather than by a model deciding
to go deeper, which leaves the planner doing what a planner is good at -- which
company, which document, in what order, within what budget -- and not deciding
what the research requires.

An extraction request names the metric it wants.  The model fills a named slot
rather than proposing metrics of its own, which is what makes coverage
measurable: "ACN is missing free cash flow for FY2026Q3" is a sentence this can
produce, and "the model did not happen to mention it" is not.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "0.1"

# Units a metric is reported in. The unit belongs to the metric, not to the
# figure: a model that returns revenue as a percent has misread something, and
# declaring the unit here is what lets that be caught rather than stored.
METRIC_UNITS: tuple[str, ...] = ("currency", "percent", "count", "ratio", "days")

# The floor, and deliberately only the floor: what is true of any company that
# files financials at all. P11f hardcoded a screen-wide list including free
# cash flow, dividends, buybacks and per-company operating metrics; the owner
# was right that this cannot be enumerated in advance, because the figures that
# decide a company differ by industry and by company and coverage will widen.
#
# So everything above this floor is discovered from what the market actually
# cites (see metric_discovery). Revenue and earnings stay declared because a
# company with neither is not a company being screened, and a screen with no
# figures at all until discovery has run would be useless rather than honest.
UNIVERSAL_SPINE: tuple[dict[str, Any], ...] = (
    {
        "metric_ref": "metric:revenue",
        "label": "收入",
        "unit": "currency",
        "periods": 4,
        "prompt": "total revenue for the period as reported",
    },
    {
        "metric_ref": "metric:net-income",
        "label": "净利润",
        "unit": "currency",
        "periods": 4,
        "prompt": "net income attributable to the company for the period",
    },
)

# Stages beyond the screen add depth by being entered, not by a model deciding
# to go deeper. They are empty until built: a stage asking for figures nobody
# serves would put a permanent gap on the cockpit.
STAGE_SPINE: Mapping[str, tuple[dict[str, Any], ...]] = {
    "initial_screen": UNIVERSAL_SPINE,
    "deep_insight_gate": (),
    "industry_model": (),
    "company_model": (),
    "investment_memo": (),
    "active_coverage": (),
}


class MetricBaseError(ValueError):
    """The metric base was asked something it cannot answer."""


def metrics_for(
    stage: str, company_ref: str, discovered: Sequence[Mapping[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Every figure this stage needs for this company: the floor plus what the
    market was observed to care about.

    ``discovered`` comes from metric_discovery, already corroborated across
    documents and carrying its citations. A discovered metric that repeats one
    already in the spine is folded in rather than duplicated, so the market
    naming revenue does not create a second revenue requirement.
    """

    if stage not in STAGE_SPINE:
        raise MetricBaseError(f"{stage} is not a stage with a declared metric base")
    items: list[dict[str, Any]] = [dict(item) for item in STAGE_SPINE[stage]]
    if stage != "initial_screen":
        # Discovery answers "what is this company judged on", which is the
        # screen's question. A deeper stage asks for its own figures.
        return items
    seen = {item["metric_ref"] for item in items}
    for item in discovered:
        ref = item.get("metric_ref")
        if not isinstance(ref, str) or ref in seen:
            continue
        for field in ("label", "unit", "periods", "prompt"):
            if field not in item:
                raise MetricBaseError(f"discovered metric {ref} is missing {field}")
        seen.add(ref)
        items.append({
            "metric_ref": ref, "label": item["label"], "unit": item["unit"],
            "periods": int(item["periods"]), "prompt": item["prompt"],
        })
    return items


def metric_spec(
    stage: str, company_ref: str, metric_ref: str,
    discovered: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One required metric, or a refusal naming what was asked for."""

    for item in metrics_for(stage, company_ref, discovered):
        if item["metric_ref"] == metric_ref:
            return item
    raise MetricBaseError(f"{metric_ref} is not declared for {stage}")


def missing_metrics(
    held: Sequence[Mapping[str, Any]], *, stage: str, company_ref: str,
    discovered: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The declared figures this company still owes, with how many periods.

    ``held`` is the company's quantitative claims, each naming a metric and a
    period.  A metric is satisfied by having as many distinct periods as it
    asks for, the same way the source base counts documents: four quarters of
    revenue is four quarters, not four claims about one quarter.
    """

    periods_by_metric: dict[str, set[str]] = {}
    for claim in held:
        if not isinstance(claim, Mapping):
            continue
        ref, period = claim.get("metric_ref"), claim.get("period")
        if isinstance(ref, str) and isinstance(period, str) and period:
            periods_by_metric.setdefault(ref, set()).add(period)
    missing: list[dict[str, Any]] = []
    for item in metrics_for(stage, company_ref, discovered):
        have = len(periods_by_metric.get(item["metric_ref"], ()))
        if have < int(item["periods"]):
            missing.append({
                **item,
                "have": have,
                "still_needed": int(item["periods"]) - have,
            })
    return missing


def extraction_requests(
    held: Sequence[Mapping[str, Any]], *, stage: str, company_ref: str,
    discovered: Sequence[Mapping[str, Any]] = (), limit: int = 6,
) -> list[dict[str, Any]]:
    """What to ask a document for, most-owed first.

    Ordering by what is most missing keeps a company that has nothing from
    waiting behind one that is a single quarter short, and the limit keeps one
    window's request small enough that the model is filling named slots rather
    than being asked to read everything at once.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MetricBaseError("limit must be a positive integer")
    ordered = sorted(
        missing_metrics(
            held, stage=stage, company_ref=company_ref, discovered=discovered
        ),
        key=lambda item: (-item["still_needed"], item["metric_ref"]),
    )
    return [
        {
            "metric_ref": item["metric_ref"],
            "label": item["label"],
            "unit": item["unit"],
            "prompt": item["prompt"],
            "still_needed": item["still_needed"],
        }
        for item in ordered[:limit]
    ]


__all__ = [
    "METRIC_UNITS",
    "MetricBaseError",
    "STAGE_SPINE",
    "UNIVERSAL_SPINE",
    "SCHEMA_VERSION",
    "extraction_requests",
    "metric_spec",
    "metrics_for",
    "missing_metrics",
]
