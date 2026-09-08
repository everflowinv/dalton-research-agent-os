"""P11f: which figures a stage needs, so extraction is asked and not guessed.

A 10-Q contains hundreds of numbers and almost none of them matter yet.  The
question "which figures should we pull out of this document" has a wrong answer
in both directions: extracting everything fills the ledger with noise nobody
asked for, and extracting whatever a model finds interesting makes the mission's
own requirements non-deterministic -- you could not say what an Initial Screen
needs, or check whether it has it.

So figures work the way documents already do.  ``SOURCE_BASE_ITEMS`` declares
which *documents* a stage needs and the checklist counts what is held; this
declares which *figures* a stage needs and counts the same way.  Depth arrives
by entering a deeper stage, not by a model deciding to go deeper: the Initial
Screen asks for the handful of figures that decide whether a company is worth
modelling, and the notes-level detail behind asset quality is asked for by the
stage that actually needs it.

That leaves the planner doing what a planner is good at -- which company, which
document, in what order, within what budget -- and not deciding what the
research requires.  The requirement is declared, hash-bound and readable; the
scheduling is automation's.

An extraction request names the metric it wants.  The model fills a declared
slot rather than proposing metrics of its own, which is what makes coverage
measurable at all: "ACN is missing free cash flow for FY2026Q3" is a sentence
this module can produce, and "the model did not happen to mention it" is not.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "0.1"

# Units a metric is reported in. The unit belongs to the metric, not to the
# figure: a model that returns revenue as a percent has misread something, and
# declaring the unit here is what lets that be caught rather than stored.
METRIC_UNITS: tuple[str, ...] = ("currency", "percent", "count", "ratio", "days")

# What the Initial Screen needs to say whether a company is worth modelling:
# what it earns, what it keeps, what it returns, and the one or two operating
# numbers its own management leads with. Deliberately short -- this is the
# screen, not the model.
INITIAL_SCREEN_METRICS: tuple[dict[str, Any], ...] = (
    {
        "metric_ref": "metric:revenue",
        "label": "收入",
        "unit": "currency",
        "periods": 4,
        "prompt": "total revenue for the period as reported",
    },
    {
        "metric_ref": "metric:operating-income",
        "label": "经营利润",
        "unit": "currency",
        "periods": 4,
        "prompt": "operating income for the period as reported",
    },
    {
        "metric_ref": "metric:net-income",
        "label": "净利润",
        "unit": "currency",
        "periods": 4,
        "prompt": "net income attributable to the company for the period",
    },
    {
        "metric_ref": "metric:free-cash-flow",
        "label": "自由现金流",
        "unit": "currency",
        "periods": 4,
        # Free cash flow is usually stated by management rather than being a
        # line item; if only its components are reported, this stays missing
        # rather than being computed here, because a figure the filing does
        # not contain cannot be verified against the filing.
        "prompt": "free cash flow for the period as reported by management",
    },
    {
        "metric_ref": "metric:dividends-paid",
        "label": "分红",
        "unit": "currency",
        "periods": 4,
        "prompt": "cash dividends paid during the period",
    },
    {
        "metric_ref": "metric:buybacks",
        "label": "回购",
        "unit": "currency",
        "periods": 4,
        "prompt": "cash used to repurchase shares during the period",
    },
)

# The operating numbers differ by company, which is the point: bookings decide
# an IT services company and would be meaningless for a bank. Declared per
# company so the requirement stays checkable, rather than left to whatever the
# document happens to emphasise.
OPERATING_METRICS: Mapping[str, tuple[dict[str, Any], ...]] = {
    "company:sec-cik:0001467373": (  # ACN
        {"metric_ref": "metric:new-bookings", "label": "新签订单", "unit": "currency",
         "periods": 4, "prompt": "new bookings for the period"},
    ),
    "company:sec-cik:0001058290": (  # CTSH
        {"metric_ref": "metric:headcount", "label": "员工人数", "unit": "count",
         "periods": 2, "prompt": "total headcount at period end"},
    ),
    "company:sec-cik:0001352010": (  # EPAM
        {"metric_ref": "metric:headcount", "label": "员工人数", "unit": "count",
         "periods": 2, "prompt": "total headcount at period end"},
        {"metric_ref": "metric:utilization", "label": "利用率", "unit": "percent",
         "periods": 2, "prompt": "utilization rate for the period"},
    ),
}

# Stages beyond the screen need more, and say so where the requirement can be
# read. They are declared empty until the stage is built rather than guessed
# at now: an unbuilt stage asking for figures nobody serves would put a
# permanent gap on the cockpit, which is the "progress-shaped but not
# progress" trap the source base already avoids.
METRIC_BASE: Mapping[str, tuple[dict[str, Any], ...]] = {
    "initial_screen": INITIAL_SCREEN_METRICS,
    "deep_insight_gate": (),
    "industry_model": (),
    "company_model": (),
    "investment_memo": (),
    "active_coverage": (),
}


class MetricBaseError(ValueError):
    """The metric base was asked something it cannot answer."""


def metrics_for(stage: str, company_ref: str) -> list[dict[str, Any]]:
    """Every figure this stage needs for this company, declared, in order."""

    if stage not in METRIC_BASE:
        raise MetricBaseError(f"{stage} is not a stage with a declared metric base")
    items = list(METRIC_BASE[stage])
    if stage == "initial_screen":
        items += list(OPERATING_METRICS.get(company_ref, ()))
    return [dict(item) for item in items]


def metric_spec(stage: str, company_ref: str, metric_ref: str) -> dict[str, Any]:
    """One declared metric, or a refusal naming what was asked for."""

    for item in metrics_for(stage, company_ref):
        if item["metric_ref"] == metric_ref:
            return item
    raise MetricBaseError(f"{metric_ref} is not declared for {stage}")


def missing_metrics(
    held: Sequence[Mapping[str, Any]], *, stage: str, company_ref: str
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
    for item in metrics_for(stage, company_ref):
        have = len(periods_by_metric.get(item["metric_ref"], ()))
        if have < int(item["periods"]):
            missing.append({
                **item,
                "have": have,
                "still_needed": int(item["periods"]) - have,
            })
    return missing


def extraction_requests(
    held: Sequence[Mapping[str, Any]], *, stage: str, company_ref: str, limit: int = 6
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
        missing_metrics(held, stage=stage, company_ref=company_ref),
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
    "INITIAL_SCREEN_METRICS",
    "METRIC_BASE",
    "METRIC_UNITS",
    "MetricBaseError",
    "OPERATING_METRICS",
    "SCHEMA_VERSION",
    "extraction_requests",
    "metric_spec",
    "metrics_for",
    "missing_metrics",
]
