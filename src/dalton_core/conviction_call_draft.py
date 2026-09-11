"""P15d drafting: one bounded call writes the argument, a second one checks it.

The gate has already run when anything here is reached: the company has an
active thesis, we stand somewhere the market does not, and the market's view
can be sourced.  So the model is never asked *whether* there is a call.  It is
asked to write the four sentences that make one -- our view, the street's
view, where the street is wrong, and what would move it -- from a table of
rows it may cite by id and nothing else.  A reply that names a row it was not
shown is not partly right; it is evidence the reply was not produced from the
table, so the whole draft is refused and none of it is kept.

**Every number in a call is deterministic.**  The consensus gap comes from the
forecast-versus-consensus bridge, not from the model.  Each pathway step's
date comes from the catalyst calendar row the model *named*, not from a date
the model wrote: a model that can type "2026-09-25" can type it wrong, and a
call whose dates are wrong is worse than one with no dates.  The model
supplies the upside and downside percentages, because those are its argument
rather than a fact -- and the Playbook's verdict on them is recomputed by the
authority from the frozen policy, so a draft can never assert that it cleared
a bar it did not clear.

The verifier is a second call on a *different model family* that returns a
verdict and nothing else.  It fails closed: when the family of either call
cannot be established, nothing is proposed.  A verification that cannot show
it was independent is not a verification.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping, Sequence

from .cockpit_model import (
    CockpitModelError,
    independent_model_call,
    register_purpose,
    unwrap_json_object,
)
from .conviction_call import (
    CHANGE_REASONS,
    CONFIDENCES,
    CONVICTION_POLICY,
    DIRECTIONS,
    MARKET_VIEW_SOURCES,
    MAX_PERCENT,
    MAX_SIGNALS,
    MAX_FALSIFIERS,
    POLICY_HASH,
    POLICY_REF,
    TIME_HORIZONS,
    check_risk_reward,
    rubric_findings,
)
# The independence predicate and the route reader are D2's, written once for
# P12c and imported rather than copied: two implementations of "the verifier
# ran somewhere else" is how one of them quietly stops being true.
from .debate_map_draft import independent, route_family
from .research_playbook import DECISION_VOCABULARY
from .store import content_hash

SCHEMA_VERSION = "0.1"
TASK_REF = "task:conviction-call-draft:0.2"

# The lane names its own purpose from its own module (P14-0's registry).
PURPOSE = register_purpose("conviction_call")
VERIFIER_PURPOSE = register_purpose("conviction_call_verifier")

# One company, one call, two calls' worth of spend.  A conviction call is a
# page, not a report; the bounds are what keep it one.
MAX_THESIS_ROWS = 6
MAX_DEBATE_ROWS = 8
MAX_METRIC_ROWS = 8
MAX_CATALYST_ROWS = 8
MAX_STATEMENT_CHARS = 400
MAX_PROMPT_BYTES = 40_000
MAX_COST_USD = 0.60
MAX_INPUT_TOKENS = 60_000
MAX_OUTPUT_TOKENS = 3_000
TIMEOUT_SECONDS = 240

MAX_SIGNAL_CHARS = 300
MAX_STATEMENT_OUT_CHARS = 800
MAX_VERIFIER_FINDINGS = 8

VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    "our_view_not_in_the_thesis",
    "market_view_not_supported_by_cited_rows",
    "this_is_not_a_disagreement",
    "where_market_is_wrong_is_a_mood_not_a_fact",
    "pathway_step_is_not_observable",
    "risk_reward_not_supported_by_cited_rows",
    "falsifier_does_not_falsify",
    "direction_contradicts_the_stated_view",
)

_REPLY_KEYS = frozenset({
    "direction", "decision", "confidence", "time_horizon", "our_view",
    "market_view", "where_market_is_wrong", "convergence_pathway",
    "event_pathway", "upside", "downside", "falsifiers",
})
_BLOCK_KEYS = frozenset({"statement", "refs"})
_MARKET_KEYS = frozenset({"available", "reason", "statement", "refs", "sources"})
_STEP_KEYS = frozenset({"signal", "catalyst_row_id", "refs"})
_CASE_KEYS = frozenset({"statement", "percent", "refs"})
_FALSIFIER_KEYS = frozenset({"statement", "thesis_row_id", "falsifier_ref"})

TASK_HASH = content_hash({
    "task": TASK_REF,
    "policy_ref": POLICY_REF,
    "policy_hash": POLICY_HASH,
    "output": "one JSON object in the closed conviction-call draft shape",
    "authority": "cites_only_shown_row_ids; dates come from the calendar rows",
    "output_limits": {"signal_chars": MAX_SIGNAL_CHARS,
                      "statement_chars": MAX_STATEMENT_OUT_CHARS,
                      "signals": MAX_SIGNALS, "falsifiers": MAX_FALSIFIERS},
})


class ConvictionDraftError(ValueError):
    """A call cannot be produced or cannot be trusted."""


class ConvictionDraftRefused(ConvictionDraftError):
    """A reply went outside what it was shown; the whole draft is refused."""


# ---------------------------------------------------------------------------
# the input table
# ---------------------------------------------------------------------------

def build_input_table(
    *,
    company_ref: str,
    precheck_record: Mapping[str, Any],
    theses: Sequence[Mapping[str, Any]],
    open_debates: Sequence[Mapping[str, Any]] = (),
    consensus_gap: Mapping[str, Any] | None = None,
    dossier_variant_view: Mapping[str, Any] | None = None,
    dossier_version_ref: str | None = None,
    catalyst_entries: Sequence[Mapping[str, Any]] = (),
    valuation: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] = CONVICTION_POLICY,
) -> dict[str, Any]:
    """Everything one drafting call may look at, and the ids it may cite.

    Divergent debates come first: the rows that carry the disagreement are the
    reason there is a call, and a bounded table that buried them under the
    agreed ones would produce a paraphrase of consensus with a contrarian
    headline.
    """

    divergent = {row["debate_ref"] for row in precheck_record.get("divergent_debates") or ()}
    thesis_rows: list[dict[str, Any]] = []
    for row in list(theses)[:MAX_THESIS_ROWS]:
        ref = str(row.get("thesis_version_ref") or row.get("ref") or "")
        if not ref:
            continue
        thesis_rows.append({
            "row_id": f"T{len(thesis_rows) + 1}",
            "ref": ref,
            "statement": str(row.get("statement") or "").strip()[:MAX_STATEMENT_CHARS],
            "confidence": row.get("confidence"),
            "falsifier_refs": list(row.get("falsifier_refs") or ()),
        })

    ordered = sorted(
        open_debates or (),
        key=lambda item: (0 if item.get("debate_ref") in divergent else 1,
                          str(item.get("debate_ref") or "")),
    )
    debate_rows: list[dict[str, Any]] = []
    for row in ordered[:MAX_DEBATE_ROWS]:
        market = row.get("market_position") or {}
        ours = row.get("our_position") or {}
        debate_rows.append({
            "row_id": f"D{len(debate_rows) + 1}",
            "ref": str(row.get("debate_ref") or ""),
            "question": str(row.get("question") or "")[:MAX_STATEMENT_CHARS],
            "status": row.get("status"),
            "market": ("not established" if not market.get("available")
                       else f"{market.get('lean')}: "
                            f"{str(market.get('statement') or '')[:MAX_STATEMENT_CHARS]}"),
            "ours": ("none yet" if ours.get("state") != "held"
                     else f"{ours.get('side')}: "
                          f"{str(ours.get('statement') or '')[:MAX_STATEMENT_CHARS]}"),
            "divergent": row.get("debate_ref") in divergent,
        })

    gap = dict(consensus_gap or {"status": "unavailable",
                                 "reason": "no consensus authority on this Core",
                                 "metrics": []})
    metric_rows: list[dict[str, Any]] = []
    shown_metrics: list[dict[str, Any]] = []
    for row in (gap.get("metrics") or ())[:MAX_METRIC_ROWS]:
        refs = list(row.get("refs") or ())
        if not refs:
            continue
        metric_rows.append({
            "row_id": f"G{len(metric_rows) + 1}",
            "ref": refs[0],
            "metric": row.get("metric"), "period": row.get("period"),
            "ours": row.get("ours"), "consensus": row.get("consensus"),
            "unit": row.get("unit"), "gap_percent": row.get("gap_percent"),
            "refs": refs,
        })
        shown_metrics.append(dict(row))
    # The gap that goes into the record is the gap that went into the prompt.
    # The table is bounded and drops rows with nothing behind them, so the
    # authority's copy has to be the *shown* rows: a call carrying a metric
    # the drafter never saw is a number nobody weighed, and it would be
    # indistinguishable in the record from one that was argued over.
    if gap.get("status") == "available" and not shown_metrics:
        gap = {"status": "unavailable", "metrics": [],
               "reason": "a consensus estimate exists but none of its rows could "
                         "be shown (every one of them cites nothing)"}
    elif gap.get("status") == "available":
        gap = {"status": "available", "reason": None, "metrics": shown_metrics}

    catalyst_rows: list[dict[str, Any]] = []
    for row in list(catalyst_entries)[:MAX_CATALYST_ROWS]:
        ref = str(row.get("entry_ref") or "")
        if not ref:
            continue
        catalyst_rows.append({
            "row_id": f"K{len(catalyst_rows) + 1}",
            "ref": ref,
            "event_kind": row.get("event_kind"),
            "expected_date": row.get("expected_date"),
            "confidence": row.get("confidence"),
            "date_unconfirmed": bool(row.get("date_unconfirmed")),
        })

    variant_rows: list[dict[str, Any]] = []
    view = dossier_variant_view or {}
    if view.get("status") == "drafted" and dossier_version_ref:
        for slot in view.get("slots") or ():
            sentences = " ".join(
                str(item.get("text") or "") for item in slot.get("sentences") or ())
            variant_rows.append({
                "row_id": f"V{len(variant_rows) + 1}",
                "ref": dossier_version_ref,
                "slot_id": slot.get("slot_id"),
                "text": sentences.strip()[:MAX_STATEMENT_CHARS],
            })

    valuation_rows: list[dict[str, Any]] = []
    if valuation and valuation.get("id"):
        valuation_rows.append({
            "row_id": "P1", "ref": valuation["id"],
            "as_of": valuation.get("as_of"),
            "metrics": valuation.get("metrics") or [],
        })

    citable: dict[str, str] = {}
    for rows in (thesis_rows, debate_rows, metric_rows, catalyst_rows,
                 variant_rows, valuation_rows):
        for row in rows:
            citable[row["row_id"]] = row["ref"]
    return {
        "company_ref": company_ref,
        "theses": thesis_rows,
        "debates": debate_rows,
        "consensus_gap": gap,
        "metrics": metric_rows,
        "catalysts": catalyst_rows,
        "variant_slots": variant_rows,
        "valuation": valuation_rows,
        "market_view_sources": list(policy["market_view_sources"]),
        "time_horizons": list(policy["time_horizons"]),
        "risk_reward_standards": [dict(row) for row in policy["risk_reward_standards"]],
        "citable": citable,
        "precheck": dict(precheck_record),
        "policy_ref": POLICY_REF,
        "policy_hash": POLICY_HASH,
    }


def _rows(lines: Sequence[str]) -> str:
    return "\n".join(f"  {line}" for line in lines) or "  (none)"


def build_prompt(table: Mapping[str, Any]) -> str:
    """The drafting prompt: six tables and one closed answer shape."""

    theses = _rows([
        f"{row['row_id']}\t{row['statement']} (confidence {row['confidence']})"
        for row in table["theses"]
    ])
    debates = _rows([
        f"{row['row_id']}\t{'DIVERGENT' if row['divergent'] else 'aligned'}\t"
        f"{row['question']}\tmarket -> {row['market']}\tus -> {row['ours']}"
        for row in table["debates"]
    ])
    metrics = _rows([
        f"{row['row_id']}\t{row['metric']}\t{row['period']}\tours {row['ours']}"
        f"{row['unit']}\tstreet {row['consensus']}{row['unit']}\t"
        f"gap {row['gap_percent']}%"
        for row in table["metrics"]
    ]) if table["metrics"] else (
        "  (no consensus on this Core: " + str(table["consensus_gap"].get("reason")) + ")"
    )
    catalysts = _rows([
        f"{row['row_id']}\t{row['expected_date']}\t{row['event_kind']}\t"
        f"{'date not confirmed by the company' if row['date_unconfirmed'] else 'confirmed'}"
        for row in table["catalysts"]
    ])
    variant = _rows([
        f"{row['row_id']}\t{row['slot_id']}\t{row['text']}"
        for row in table["variant_slots"]
    ])
    standards = _rows([
        f"{row['standard_ref']}\t{'/'.join(row['directions'])}\t"
        f"{'/'.join(row['horizons'])}\t{row['playbook_text']}"
        for row in table["risk_reward_standards"]
    ])
    return (
        "You write one investment call for a fundamental long-biased fund's own "
        "file. A person decides whether to act on it; you only propose it.\n\n"
        "Understand what the market expects and how the investment can earn its "
        "return. Agreement on business direction can still leave a meaningful "
        "difference in magnitude, timing, probability or valuation. Do not "
        "invent disagreement to make a call qualify. This version of the call "
        "requires a supported pricing difference and an observable path to "
        "realizing it; state plainly when the supplied evidence cannot establish "
        "those. Commit to the best-supported current judgement, explain the "
        "condition that would change it and name the next observation.\n\n"
        f"COMPANY: {table['company_ref']}\n\n"
        "OUR ADMITTED THESES -- <row id>\\t<statement>:\n"
        f"{theses}\n\n"
        "LIVE DEBATES. DIVERGENT means we already stand somewhere the street "
        "does not -- <row id>\\t<flag>\\t<question>\\tmarket\\tus:\n"
        f"{debates}\n\n"
        "OUR FORECAST AGAINST THE STREET -- <row id>\\t<metric>\\t<period>\\t"
        "<ours>\\t<street>\\t<gap>:\n"
        f"{metrics}\n\n"
        "DATED CATALYSTS you may hang a pathway step on -- <row id>\\t<date>\\t"
        "<kind>\\t<how firm the date is>:\n"
        f"{catalysts}\n\n"
        "THE COMPANY FILE'S OWN VARIANT VIEW -- <row id>\\t<slot>\\t<text>:\n"
        f"{variant}\n\n"
        "THE FUND'S RISK/REWARD STANDARDS -- <ref>\\t<directions>\\t<horizons>\\t"
        "<what it says>:\n"
        f"{standards}\n\n"
        "Rules:\n"
        "* Cite only the row ids above (T..., D..., G..., K..., V..., P...). "
        "Never invent one.\n"
        "* our_view is what OUR THESES commit us to. market_view is what the "
        "street is paying for, and its sources must come from this list: "
        + ", ".join(MARKET_VIEW_SOURCES) + ".\n"
        "* If the rows do not establish where the street stands, set "
        "market_view.available to false and give a reason. That is an honest "
        "answer, and it means there is no call to write today.\n"
        "* where_market_is_wrong names the exact point of disagreement -- a "
        "fact, a timing, a transmission or a multiple. Not a mood.\n"
        "* Every event_pathway step is something a person could observe. "
        "catalyst_row_id names the calendar row it happens at, or null when it "
        "is not on the calendar. Do not write dates: the date comes from the "
        "row you name.\n"
        "* upside.percent and downside.percent are your own numbers, as plain "
        "decimals without a sign or a per-cent symbol (a 30% drawdown is "
        "\"30\"). Leave one null only if you genuinely cannot size it.\n"
        "* time_horizon is one of: " + ", ".join(TIME_HORIZONS) + ". A short "
        "must state 3_6_months; the standards table says why.\n"
        "* decision is one of the five Active Coverage words: "
        + ", ".join(DECISION_VOCABULARY) + ".\n"
        "* Every falsifier names the thesis row it would break.\n"
        f"* Text limits count characters, not words: every statement and "
        f"market_view.reason at most {MAX_STATEMENT_OUT_CHARS}; each "
        f"event_pathway.signal at most {MAX_SIGNAL_CHARS}. Use 1–{MAX_SIGNALS} "
        f"pathway steps and 1–{MAX_FALSIFIERS} falsifiers. Keep each signal a "
        "concise observable condition; put its explanation in the statement.\n"
        "* Return one raw JSON object and nothing else. No prose, no code "
        "fence.\n\n"
        "{\"direction\": \"long|short|avoid\", \"decision\": \"<one of the five>\",\n"
        " \"confidence\": \"low|medium|high\", \"time_horizon\": \"<one of the list>\",\n"
        " \"our_view\": {\"statement\": \"...\", \"refs\": [\"T1\"]},\n"
        " \"market_view\": {\"available\": true, \"reason\": null, \"statement\": \"...\",\n"
        "   \"refs\": [\"D1\"], \"sources\": [\"debate_market_position\"]},\n"
        " \"where_market_is_wrong\": {\"statement\": \"...\", \"refs\": [\"D1\"]},\n"
        " \"convergence_pathway\": {\"statement\": \"...\", \"refs\": [\"K1\"]},\n"
        " \"event_pathway\": [{\"signal\": \"...\", \"catalyst_row_id\": \"K1\",\n"
        "   \"refs\": [\"K1\"]}],\n"
        " \"upside\": {\"statement\": \"...\", \"percent\": \"55\", \"refs\": [\"G1\"]},\n"
        " \"downside\": {\"statement\": \"...\", \"percent\": \"20\", \"refs\": [\"D1\"]},\n"
        " \"falsifiers\": [{\"statement\": \"...\", \"thesis_row_id\": \"T1\",\n"
        "   \"falsifier_ref\": null}]}\n"
    )


def prompt_drafter(work_order_ref: str, prompt: str) -> tuple[str, str]:
    """The drafter ref and hash a proposal is recorded under."""

    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return (
        f"model:{work_order_ref}",
        content_hash({"task_hash": TASK_HASH, "prompt_sha256": digest}),
    )


# ---------------------------------------------------------------------------
# parsing the draft
# ---------------------------------------------------------------------------

def _text_field(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConvictionDraftRefused(f"{name} must be non-empty text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ConvictionDraftRefused(f"{name} is longer than {limit} characters")
    return text


def _shown(value: Any, name: str, citable: Mapping[str, str], *, nonempty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ConvictionDraftRefused(f"{name} must be an array of row ids")
    resolved: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in citable:
            raise ConvictionDraftRefused(
                f"{name} names {item!r}, which was not a row in the table")
        ref = citable[item]
        if ref not in resolved:
            resolved.append(ref)
    if nonempty and not resolved:
        raise ConvictionDraftRefused(f"{name} must name at least one row")
    return resolved


def _closed_reply(value: Any, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConvictionDraftRefused(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != set(keys):
        raise ConvictionDraftRefused(
            f"{name} has an invalid shape; missing={sorted(set(keys) - set(wire))}, "
            f"unknown={sorted(set(wire) - set(keys))}")
    return wire


def _block(value: Any, name: str, citable: Mapping[str, str]) -> dict[str, Any]:
    wire = _closed_reply(value, _BLOCK_KEYS, name)
    return {
        "statement": _text_field(wire["statement"], f"{name}.statement",
                                 MAX_STATEMENT_OUT_CHARS),
        "refs": _shown(wire["refs"], f"{name}.refs", citable, nonempty=True),
    }


def _percent(value: Any, name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip().rstrip("%")
    if not text:
        raise ConvictionDraftRefused(f"{name} must be a decimal or null")
    try:
        number = float(text)
    except ValueError as exc:
        raise ConvictionDraftRefused(f"{name} must be a decimal or null") from exc
    if number != number or number in (float("inf"), float("-inf")):
        # NaN and the infinities are the dangerous ones. NaN compares false
        # against every threshold, so it would read as ``not_met``; an infinity
        # compares true against all of them, so it would read as ``met``.
        # Neither is a claim about the company.
        raise ConvictionDraftRefused(
            f"{name} must be a finite decimal; got {text!r}")
    if number < 0:
        # A downside written as "-30" and a downside written as "30" would sort
        # differently against the same standard.  The prompt says unsigned and
        # the parser does not silently repair it.
        raise ConvictionDraftRefused(
            f"{name} is a magnitude without a sign; a 30% drawdown is \"30\"")
    if number > MAX_PERCENT:
        # The same bound the authority enforces, imported rather than restated:
        # a parser that let through what the authority refuses would pay for a
        # verifying call and then throw the result away.
        raise ConvictionDraftRefused(
            f"{name} is {text}, over the {MAX_PERCENT}% bound; a return that "
            "large is a parsing accident, not an argument")
    return text


def parse_draft(text: Any, table: Mapping[str, Any]) -> dict[str, Any]:
    """The reply, resolved to refs -- or a refusal of the whole draft."""

    if not isinstance(text, str) or not text.strip():
        raise ConvictionDraftRefused("the drafting call returned nothing")
    parsed = unwrap_json_object(text)
    if parsed is None:
        raise ConvictionDraftRefused("the draft was not one JSON object")
    wire = _closed_reply(parsed, _REPLY_KEYS, "draft")
    citable = table["citable"]

    direction = wire["direction"]
    if direction not in DIRECTIONS:
        raise ConvictionDraftRefused(f"invalid direction: {direction!r}")
    decision = wire["decision"]
    if decision not in DECISION_VOCABULARY:
        raise ConvictionDraftRefused(f"invalid decision word: {decision!r}")
    confidence = wire["confidence"]
    if confidence not in CONFIDENCES:
        raise ConvictionDraftRefused(f"invalid confidence: {confidence!r}")
    horizon = wire["time_horizon"]
    if horizon not in TIME_HORIZONS:
        raise ConvictionDraftRefused(f"invalid time_horizon: {horizon!r}")

    market = _closed_reply(wire["market_view"], _MARKET_KEYS, "market_view")
    if not isinstance(market["available"], bool):
        raise ConvictionDraftRefused("market_view.available must be a boolean")
    if market["available"]:
        sources = market["sources"]
        if not isinstance(sources, list) or not sources:
            raise ConvictionDraftRefused("market_view.sources must name where it came from")
        for source in sources:
            if source not in MARKET_VIEW_SOURCES:
                raise ConvictionDraftRefused(f"unknown market view source: {source!r}")
        market_view = {
            "available": True, "reason": None,
            "statement": _text_field(market["statement"], "market_view.statement",
                                     MAX_STATEMENT_OUT_CHARS),
            "refs": _shown(market["refs"], "market_view.refs", citable, nonempty=True),
            "sources": list(dict.fromkeys(sources)),
        }
    else:
        market_view = {
            "available": False,
            "reason": _text_field(market["reason"], "market_view.reason",
                                  MAX_STATEMENT_OUT_CHARS),
            "statement": None, "refs": [], "sources": [],
        }

    steps: list[dict[str, Any]] = []
    raw_steps = wire["event_pathway"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ConvictionDraftRefused("event_pathway must name at least one signal")
    if len(raw_steps) > MAX_SIGNALS:
        raise ConvictionDraftRefused(
            f"event_pathway carries more than {MAX_SIGNALS} steps"
        )
    calendar = {row["row_id"]: row for row in table["catalysts"]}
    for index, raw in enumerate(raw_steps):
        step = _closed_reply(raw, _STEP_KEYS, f"event_pathway[{index}]")
        row_id = step["catalyst_row_id"]
        if row_id is None:
            window = {"kind": "unknown", "date": None, "from": None, "to": None}
            catalyst_ref = None
        else:
            if not isinstance(row_id, str) or row_id not in calendar:
                raise ConvictionDraftRefused(
                    f"event_pathway[{index}].catalyst_row_id names {row_id!r}, "
                    "which was not a calendar row")
            entry = calendar[row_id]
            # The date is the calendar's, never the model's.
            window = {"kind": "date", "date": entry["expected_date"],
                      "from": None, "to": None}
            catalyst_ref = entry["ref"]
        steps.append({
            "signal": _text_field(step["signal"], f"event_pathway[{index}].signal",
                                  MAX_SIGNAL_CHARS),
            "window": window,
            "catalyst_ref": catalyst_ref,
            "refs": _shown(step["refs"], f"event_pathway[{index}].refs", citable,
                           nonempty=True),
        })

    cases: dict[str, Any] = {}
    for key in ("upside", "downside"):
        case = _closed_reply(wire[key], _CASE_KEYS, key)
        cases[key] = {
            "statement": _text_field(case["statement"], f"{key}.statement",
                                     MAX_STATEMENT_OUT_CHARS),
            "percent": _percent(case["percent"], f"{key}.percent"),
            "refs": _shown(case["refs"], f"{key}.refs", citable, nonempty=True),
        }

    thesis_refs = {row["row_id"]: row["ref"] for row in table["theses"]}
    known_falsifiers = {
        ref for row in table["theses"] for ref in row["falsifier_refs"]
    }
    raw_falsifiers = wire["falsifiers"]
    if not isinstance(raw_falsifiers, list) or not raw_falsifiers:
        raise ConvictionDraftRefused("falsifiers must name at least one")
    if len(raw_falsifiers) > MAX_FALSIFIERS:
        raise ConvictionDraftRefused(
            f"falsifiers carries more than {MAX_FALSIFIERS} entries"
        )
    falsifiers: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_falsifiers):
        row = _closed_reply(raw, _FALSIFIER_KEYS, f"falsifiers[{index}]")
        row_id = row["thesis_row_id"]
        if not isinstance(row_id, str) or row_id not in thesis_refs:
            raise ConvictionDraftRefused(
                f"falsifiers[{index}].thesis_row_id names {row_id!r}, which was "
                "not a thesis row")
        falsifier_ref = row["falsifier_ref"]
        if falsifier_ref is not None:
            if not isinstance(falsifier_ref, str) or falsifier_ref not in known_falsifiers:
                # A named falsifier belongs to the Driver Pack, and the theses
                # carry theirs.  One nobody has heard of is invented.
                raise ConvictionDraftRefused(
                    f"falsifiers[{index}].falsifier_ref names {falsifier_ref!r}, "
                    "which is not a falsifier any shown thesis carries")
        falsifiers.append({
            "statement": _text_field(row["statement"], f"falsifiers[{index}].statement",
                                     MAX_STATEMENT_OUT_CHARS),
            "falsifier_ref": falsifier_ref,
            "thesis_version_ref": thesis_refs[row_id],
        })

    return {
        "direction": direction, "decision": decision, "confidence": confidence,
        "time_horizon": horizon,
        "variant_view": {
            "our_view": _block(wire["our_view"], "our_view", citable),
            "market_view": market_view,
            "where_market_is_wrong": _block(
                wire["where_market_is_wrong"], "where_market_is_wrong", citable),
            "convergence_pathway": _block(
                wire["convergence_pathway"], "convergence_pathway", citable),
        },
        "event_pathway": steps,
        "risk_reward": {
            "upside": cases["upside"], "downside": cases["downside"],
            "standard": check_risk_reward(
                direction=direction, time_horizon=horizon,
                upside_percent=cases["upside"]["percent"],
                downside_percent=cases["downside"]["percent"],
            ),
        },
        "falsifiers": falsifiers,
    }


def assemble_call(
    draft: Mapping[str, Any], table: Mapping[str, Any]
) -> dict[str, Any]:
    """The draft plus the deterministic blocks, ready for the authority.

    The consensus gap and the debate refs are not the model's to write: they
    are what the bridge and the map already hold, carried through unchanged.
    """

    thesis_refs = [row["ref"] for row in table["theses"]]
    cited = set(draft["variant_view"]["our_view"]["refs"])
    for falsifier in draft["falsifiers"]:
        cited.add(falsifier["thesis_version_ref"])
    ordered = [ref for ref in thesis_refs if ref in cited] or thesis_refs[:1]
    return {
        **{key: draft[key] for key in
           ("direction", "decision", "confidence", "time_horizon",
            "variant_view", "event_pathway", "risk_reward", "falsifiers")},
        "consensus_gap": dict(table["consensus_gap"]),
        "thesis_refs": ordered,
        "debate_refs": [row["ref"] for row in table["debates"] if row["divergent"]],
    }


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------

def build_verifier_prompt(
    table: Mapping[str, Any], call: Mapping[str, Any]
) -> str:
    """The verifier sees the same table and answers one question."""

    by_ref = {ref: row_id for row_id, ref in table["citable"].items()}

    def cite(refs: Sequence[str]) -> str:
        return " ".join(by_ref.get(ref, ref) for ref in refs)

    variant = call["variant_view"]
    reward = call["risk_reward"]
    lines = [
        "You are an independent verifier. Another model wrote the investment call",
        "below from the table that follows it. You do not rewrite it and you do not",
        "improve it. You answer one question: is every part of this call supported",
        "by the rows it cites, including a concrete pricing difference? Shared",
        "bullish or bearish direction does not establish identical expectations:",
        "compare magnitude, timing, probability and valuation. Do not demand",
        "contrarianism or invent a market position absent from the evidence.",
        "",
        f"COMPANY: {table['company_ref']}",
        "",
        "OUR THESES:",
    ]
    lines += [f"  {row['row_id']}\t{row['statement']}" for row in table["theses"]] or ["  (none)"]
    lines += ["", "LIVE DEBATES:"]
    lines += [
        f"  {row['row_id']}\t{'DIVERGENT' if row['divergent'] else 'aligned'}\t"
        f"{row['question']}\tmarket -> {row['market']}\tus -> {row['ours']}"
        for row in table["debates"]
    ] or ["  (none)"]
    if table["metrics"]:
        lines += ["", "OUR FORECAST AGAINST THE STREET:"]
        lines += [
            f"  {row['row_id']}\t{row['metric']} {row['period']}: ours {row['ours']}"
            f"{row['unit']} vs street {row['consensus']}{row['unit']} "
            f"({row['gap_percent']}%)"
            for row in table["metrics"]
        ]
    if table["catalysts"]:
        lines += ["", "DATED CATALYSTS:"]
        lines += [
            f"  {row['row_id']}\t{row['expected_date']}\t{row['event_kind']}"
            for row in table["catalysts"]
        ]
    lines += [
        "",
        "THE CALL UNDER REVIEW:",
        f"  direction: {call['direction']} over {call['time_horizon']} "
        f"(confidence {call['confidence']}, decision {call['decision']})",
        f"  our view: {variant['our_view']['statement']}  "
        f"[{cite(variant['our_view']['refs'])}]",
    ]
    market = variant["market_view"]
    if market["available"]:
        lines.append(
            f"  market view ({', '.join(market['sources'])}): {market['statement']}  "
            f"[{cite(market['refs'])}]")
    else:
        lines.append(f"  market view: not established -- {market['reason']}")
    lines += [
        f"  where the market is wrong: {variant['where_market_is_wrong']['statement']}  "
        f"[{cite(variant['where_market_is_wrong']['refs'])}]",
        f"  convergence pathway: {variant['convergence_pathway']['statement']}  "
        f"[{cite(variant['convergence_pathway']['refs'])}]",
        "  observable signals:",
    ]
    for step in call["event_pathway"]:
        window = step["window"]
        when = window["date"] or "no date on the calendar"
        lines.append(f"    - {when}: {step['signal']}  [{cite(step['refs'])}]")
    lines += [
        f"  upside {reward['upside']['percent'] or 'unsized'}%: "
        f"{reward['upside']['statement']}  [{cite(reward['upside']['refs'])}]",
        f"  downside {reward['downside']['percent'] or 'unsized'}%: "
        f"{reward['downside']['statement']}  [{cite(reward['downside']['refs'])}]",
        f"  against the fund's standard: {reward['standard']['status']} -- "
        f"{reward['standard']['reason']}",
        "  falsifiers:",
    ]
    for falsifier in call["falsifiers"]:
        row_id = by_ref.get(falsifier["thesis_version_ref"], falsifier["thesis_version_ref"])
        lines.append(f"    - {falsifier['statement']}  [{row_id}]")
    lines += [
        "",
        "Return one raw JSON object and nothing else:",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '",',
        '   "detail": "<one sentence>"}]}',
        "A pass verdict has no findings; a reject verdict has at least one.",
        "Use this_is_not_a_disagreement only when there is no supported pricing",
        "difference in the claimed magnitude, timing, probability or valuation.",
        f"Return at most {MAX_VERIFIER_FINDINGS} findings; each detail is at most "
        f"{MAX_STATEMENT_OUT_CHARS} "
        "characters, not words. State the specific unsupported claim concisely.",
    ]
    prompt = "\n".join(lines)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ConvictionDraftRefused(
            f"the verifier prompt is {len(prompt.encode('utf-8'))} bytes, "
            f"over the {MAX_PROMPT_BYTES} bound")
    return prompt


def parse_verdict(text: Any) -> dict[str, Any]:
    """The verdict, or a refusal.  Verdict only: the verifier never rewrites."""

    if not isinstance(text, str) or not text.strip():
        raise ConvictionDraftRefused("the verifier returned nothing")
    parsed = unwrap_json_object(text)
    if parsed is None:
        raise ConvictionDraftRefused("the verifier did not return one JSON object")
    if set(parsed) != {"verdict", "findings"}:
        raise ConvictionDraftRefused(
            f"the verdict must be exactly verdict and findings; got {sorted(parsed)}")
    if parsed["verdict"] not in VERIFIER_VERDICTS:
        raise ConvictionDraftRefused(f"invalid verdict: {parsed['verdict']!r}")
    rows = parsed["findings"]
    if not isinstance(rows, list):
        raise ConvictionDraftRefused("findings must be an array")
    if len(rows) > MAX_VERIFIER_FINDINGS:
        raise ConvictionDraftRefused(
            f"findings carries more than {MAX_VERIFIER_FINDINGS} entries"
        )
    findings: list[dict[str, str]] = []
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != {"code", "detail"}:
            raise ConvictionDraftRefused("each finding is exactly code and detail")
        if raw["code"] not in VERIFIER_FINDING_CODES:
            raise ConvictionDraftRefused(f"unknown finding code: {raw['code']!r}")
        findings.append({
            "code": str(raw["code"]),
            "detail": _text_field(raw["detail"], "finding.detail", MAX_STATEMENT_OUT_CHARS),
        })
    if parsed["verdict"] == "pass" and findings:
        raise ConvictionDraftRefused("a pass verdict cannot carry findings")
    if parsed["verdict"] == "reject" and not findings:
        raise ConvictionDraftRefused("a reject verdict must say what is wrong")
    return {"verdict": parsed["verdict"], "findings": findings}


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _provenance(call: Mapping[str, Any], family: str | None) -> dict[str, Any]:
    return {
        "kind": "model",
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "model_family": family,
    }


def draft_conviction_call(
    *,
    table: Mapping[str, Any],
    model: Any,
    mission: Mapping[str, Any],
    family_of: Callable[[Any], str | None] | None = None,
    rubric_ref: str,
    rubric_hash: str,
) -> dict[str, Any]:
    """One drafting call, one verifying call, and a proposable call or not.

    Never writes.  It returns what a caller may propose and why, so that the
    decision to propose stays with the lane and the authority stays a
    mechanism.
    """

    resolve = family_of or (lambda ref: None)
    prompt = build_prompt(table)
    result: dict[str, Any] = {
        "status": "failed",
        "company_ref": table["company_ref"],
        "prompt_bytes": len(prompt.encode("utf-8")),
        "cost_micros": 0,
        "call": None,
        "reason": None,
        "findings": [],
        "drafted_by": None,
        "verified_by": None,
        "rubric": {"rubric_ref": rubric_ref, "rubric_hash": rubric_hash,
                   "findings": []},
    }
    if result["prompt_bytes"] > MAX_PROMPT_BYTES:
        result.update({"status": "refused",
                       "reason": f"the input table is {result['prompt_bytes']} bytes, "
                                 f"over the {MAX_PROMPT_BYTES} bound"})
        return result
    try:
        drafted = model.call(
            purpose=PURPOSE,
            request_id=prompt_drafter("", prompt)[1][:32],
            prompt=prompt, mission=mission,
        )
    except CockpitModelError as exc:
        result.update({"status": "model_unavailable",
                       "reason": f"{type(exc).__name__}: {exc}"})
        return result
    result["cost_micros"] += int(drafted.get("cost_micros") or 0)
    drafted_by = _provenance(drafted, resolve(drafted.get("route_decision_ref")))
    result["drafted_by"] = drafted_by
    try:
        parsed = parse_draft(drafted.get("text"), table)
    except ConvictionDraftError as exc:
        result.update({"status": "refused", "reason": f"{type(exc).__name__}: {exc}"})
        return result

    if not parsed["variant_view"]["market_view"]["available"]:
        # The drafter's own honest answer, and it is the right one to respect:
        # with no market view there is no variant view, and with no variant
        # view there is no call. Nothing is proposed and nothing is wasted.
        result.update({
            "status": "no_variant_view",
            "reason": parsed["variant_view"]["market_view"]["reason"],
        })
        return result

    call = assemble_call(parsed, table)
    findings = rubric_findings(call)
    result["rubric"]["findings"] = findings
    if findings:
        result.update({
            "status": "rubric_failed",
            "reason": "the call fails the conviction-call rubric mechanically: "
                      + ", ".join(findings),
        })
        return result
    try:
        verifier_prompt = build_verifier_prompt(table, call)
    except ConvictionDraftError as exc:
        result.update({"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    try:
        check = independent_model_call(
            model,
            producer_route_decision_refs=[drafted_by["route_decision_ref"]],
            purpose=VERIFIER_PURPOSE,
            request_id=content_hash({"verify": table["company_ref"],
                                     "direction": call["direction"]})[:32],
            prompt=verifier_prompt, mission=mission,
        )
    except CockpitModelError as exc:
        result.update({"status": "unverified",
                       "reason": f"the verifying call did not run: {exc}"})
        return result
    result["cost_micros"] += int(check.get("cost_micros") or 0)
    verified_by = _provenance(check, resolve(check.get("route_decision_ref")))
    result["verified_by"] = verified_by
    ok, reason = independent(drafted_by, verified_by)
    if not ok:
        result.update({"status": "not_independent", "reason": reason})
        return result
    try:
        verdict = parse_verdict(check.get("text"))
    except ConvictionDraftError as exc:
        result.update({"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    if verdict["verdict"] != "pass":
        result.update({
            "status": "verifier_rejected",
            "findings": verdict["findings"],
            "reason": "; ".join(item["code"] for item in verdict["findings"]),
        })
        return result
    result.update({"status": "verified", "call": call, "reason": None})
    return result


def change_evidence(
    call: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> list[str]:
    """The refs that occasioned this version, per ADR-0008.

    The refs this call cites that the current head of the chain does not.  With
    no previous version everything is new; with a previous version and nothing
    new the caller must not publish, because a call that learned nothing is the
    same call written twice.
    """

    from .conviction_call import cited_refs

    fresh = cited_refs(call)
    if previous is not None:
        fresh = fresh - cited_refs(previous)
    return sorted(fresh)


def change_reason_for(previous: Mapping[str, Any] | None) -> str:
    """``evidence_thicker`` is the only reason a drafting run can honestly give.

    The others in ADR-0008's vocabulary are assertions about the world -- a
    filing landed, a driver moved, a person decided -- and a lane that reads
    theses, a debate map and a forecast and writes a call knows only that there
    is more to stand on than there was.  Naming one of the others would be a
    lie the chain would then preserve.  A human revision arriving through some
    later entry point may say ``human_revision``; this one may not.
    """

    assert set(CHANGE_REASONS)  # the vocabulary is ADR-0008's, not this module's
    return "evidence_thicker"


__all__ = [
    "MAX_COST_USD",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PERCENT",
    "MAX_PROMPT_BYTES",
    "PURPOSE",
    "VERIFIER_PURPOSE",
    "SCHEMA_VERSION",
    "TASK_HASH",
    "TASK_REF",
    "TIMEOUT_SECONDS",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "ConvictionDraftError",
    "ConvictionDraftRefused",
    "assemble_call",
    "build_input_table",
    "build_prompt",
    "build_verifier_prompt",
    "change_evidence",
    "change_reason_for",
    "draft_conviction_call",
    "independent",
    "parse_draft",
    "parse_verdict",
    "prompt_drafter",
    "route_family",
]
