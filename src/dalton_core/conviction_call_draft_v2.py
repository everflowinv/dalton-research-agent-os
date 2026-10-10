"""Conviction v2 drafting (ADR-0013): one call writes, an independent one checks.

The v1 drafter was told a call *is* a disagreement and its verifier rejected
anything that was not one ("this_is_not_a_disagreement").  v2 asks for three
separate things instead -- how well the view is supported (evidence
confidence, resting on named thesis and claim rows), what could happen
(a scenario table: probability, payoff over the horizon, the observable
trigger), and where the street leans relative to us -- and lets the owner's
published policy decide whether that adds up to a call.

What stays from v1, deliberately:

* the model may cite only the rows it was shown, by id; one invented id
  refuses the whole draft;
* the deterministic numbers are not the model's: the consensus gap comes from
  the bridge, the valuation from the snapshot, and when a held structured
  scenario (ADR-0012's ``ScenarioVersion``) is named, its probability and
  payoff are copied from the held row, the way v1 copied catalyst dates;
* the expected return is computed here from the table and the policy gate is
  run *before* the verifier is paid for, so a call the policy would refuse
  costs one model call, not two;
* the verifier runs on a different model family and returns a verdict only.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from .cockpit_model import CockpitModelError, independent_model_call, unwrap_json_object
from .conviction_call import (
    ALIGNMENTS,
    CONFIDENCES,
    DIRECTIONS,
    MARKET_VIEW_SOURCES,
    MAX_FALSIFIERS,
    MAX_PERCENT,
    MAX_SCENARIOS,
    POLICY_REFUSAL_LABELS,
    TIME_HORIZONS,
    evaluate_policy_gate,
    expected_return_of,
    rubric_findings_v2,
)
from .conviction_call_draft import (
    MAX_PROMPT_BYTES,
    MAX_STATEMENT_OUT_CHARS,
    MAX_VERIFIER_FINDINGS,
    PURPOSE,
    VERIFIER_FINDING_CODES as V1_FINDING_CODES,
    VERIFIER_PURPOSE,
    VERIFIER_VERDICTS,
    ConvictionDraftError,
    ConvictionDraftRefused,
    _block,
    _closed_reply,
    _provenance,
    _shown,
    _text_field,
    build_input_table,
    independent,
    prompt_drafter,
)
from .final_text_contract import final_text_instructions
from .research_playbook import DECISION_VOCABULARY
from .store import content_hash

TASK_REF_V2 = "task:conviction-call-draft-v2:0.2"
# The governed "no call today" outcome: the drafter, or the policy before any
# draft is paid for, says there is no call the published policy would admit.
NO_CALL_STATUS = "no_call"
NO_CALL_PREFIX = "今日不出观点："
POLICY_REFUSED_PREFIX = "策略拒绝了草稿："
MAX_NO_CALL_REASON_CHARS = 300
MAX_TRIGGER_CHARS = 300
MAX_SCENARIO_NAME_CHARS = 80

VERIFIER_FINDING_CODES_V2: tuple[str, ...] = (
    "our_view_not_in_the_thesis",
    "market_view_not_supported_by_cited_rows",
    "evidence_confidence_overstated",
    "scenario_not_supported_by_cited_rows",
    "probabilities_not_supported",
    "payoff_inconsistent_with_valuation",
    "trigger_is_not_observable",
    "alignment_misread",
    "invalidator_does_not_invalidate",
    "direction_contradicts_the_stated_view",
)
# The provider contract (v0.2) carries both lists, so a v1 verifier reply
# stored before the switch still validates.
PROVIDER_FINDING_CODES: tuple[str, ...] = tuple(dict.fromkeys(
    (*V1_FINDING_CODES, *VERIFIER_FINDING_CODES_V2)))

_REPLY_KEYS = frozenset({
    "direction", "decision", "time_horizon", "our_view", "evidence_confidence",
    "market_view", "alignment", "where_market_is_wrong", "scenarios", "invalidators",
})
_EVIDENCE_KEYS = frozenset({"level", "statement", "refs"})
_MARKET_KEYS = frozenset({"available", "reason", "statement", "refs", "sources"})
_SCENARIO_KEYS = frozenset({
    "name", "probability", "payoff", "trigger", "refs", "scenario_row_id",
})
_INVALIDATOR_KEYS = frozenset({"statement", "thesis_row_id", "falsifier_ref"})

TASK_HASH_V2 = content_hash({
    "task": TASK_REF_V2,
    "output": "one JSON object in the closed conviction-call v2 draft shape",
    "authority": ("cites_only_shown_row_ids; held scenario numbers come from the "
                  "held rows; expected return and policy gate are derived"),
    "verifier_codes": list(VERIFIER_FINDING_CODES_V2),
    # Batch t: the drafter proposes only inside the published policy and may
    # answer {"no_call": {"reason": ...}} instead of a call.
    "policy_bounded": "allowed_directions; min_evidence_confidence<=best_thesis; no_call",
    "limits": {"scenarios": MAX_SCENARIOS, "invalidators": MAX_FALSIFIERS,
               "trigger_chars": MAX_TRIGGER_CHARS,
               "statement_chars": MAX_STATEMENT_OUT_CHARS},
})


class ConvictionNoCall(Exception):
    """The governed answer "no call today", with the reason in the owner's words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _confidence_rank(level: Any) -> int:
    from .conviction_policy import CONFIDENCE_ORDER

    return CONFIDENCE_ORDER.index(level) if level in CONFIDENCE_ORDER else -1


def policy_no_call_reason(policy: Mapping[str, Any],
                          theses: Sequence[Mapping[str, Any]]) -> str | None:
    """Why the published policy cannot admit any call today, before a paid draft.

    Mechanical and free.  A call's evidence confidence rests on the thesis
    rows it cites (``parse_draft_v2`` requires one), and it may not be rated
    above the best of them; so when no admitted thesis reaches the policy's
    minimum, no draft can pass the gate and none is paid for.
    """

    from .conviction_policy import VALUE_LABELS

    if not list(policy.get("allowed_directions") or ()):
        return NO_CALL_PREFIX + "策略没有允许任何方向"
    minimum = str(policy.get("min_evidence_confidence") or "low")
    best = max((_confidence_rank(row.get("confidence")) for row in theses), default=-1)
    if best < _confidence_rank(minimum):
        return (NO_CALL_PREFIX + "没有一条生效论点的置信度达到策略要求的"
                f"「{VALUE_LABELS.get(minimum, minimum)}」，证据置信度不可能达标")
    return None


# ---------------------------------------------------------------------------
# the input table
# ---------------------------------------------------------------------------

def build_input_table_v2(
    *,
    company_ref: str,
    precheck_record: Mapping[str, Any],
    theses: Sequence[Mapping[str, Any]],
    open_debates: Sequence[Mapping[str, Any]] = (),
    consensus_gap: Mapping[str, Any] | None = None,
    street: Mapping[str, Any] | None = None,
    dossier_variant_view: Mapping[str, Any] | None = None,
    dossier_version_ref: str | None = None,
    catalyst_entries: Sequence[Mapping[str, Any]] = (),
    valuation: Mapping[str, Any] | None = None,
    held_scenarios: Sequence[Mapping[str, Any]] = (),
    policy_effective: Mapping[str, Any],
) -> dict[str, Any]:
    """v1's rows, plus the street's price target and any held scenarios.

    ``precheck_record`` is the v2 precheck; v1's table builder reads only its
    ``divergent_debates``, which under v2 is "whatever we happen to stand
    apart from the street on" -- shown first, no longer required.
    """

    from .conviction_call import divergent_debates

    v1_view = {**dict(precheck_record),
               "divergent_debates": divergent_debates(open_debates)}
    table = build_input_table(
        company_ref=company_ref, precheck_record=v1_view, theses=theses,
        open_debates=open_debates, consensus_gap=consensus_gap,
        dossier_variant_view=dossier_variant_view,
        dossier_version_ref=dossier_version_ref, catalyst_entries=catalyst_entries,
        valuation=valuation,
    )
    street_rows: list[dict[str, Any]] = []
    if street and street.get("id"):
        street_rows.append({
            "row_id": "M1", "ref": str(street["id"]), "as_of": street.get("as_of"),
            "close": street.get("close"), "price_target": street.get("price_target"),
            "recommendations": street.get("recommendations"),
        })
    scenario_rows: list[dict[str, Any]] = []
    for row in list(held_scenarios)[:MAX_SCENARIOS]:
        ref = str(row.get("ref") or "")
        if not ref:
            continue
        scenario_rows.append({
            "row_id": f"S{len(scenario_rows) + 1}", "ref": ref,
            "name": str(row.get("name") or "")[:MAX_SCENARIO_NAME_CHARS],
            "probability": row.get("probability"), "payoff": row.get("payoff"),
            "trigger": str(row.get("trigger") or "")[:MAX_TRIGGER_CHARS],
        })
    citable = dict(table["citable"])
    for row in (*street_rows, *scenario_rows):
        citable[row["row_id"]] = row["ref"]
    thesis_claims = {
        str(row.get("thesis_version_ref") or row.get("ref") or ""):
            list(row.get("claim_refs") or ()) for row in theses
    }
    return {
        **table,
        "street": street_rows,
        "held_scenarios": scenario_rows,
        "citable": citable,
        "thesis_claims": thesis_claims,
        "precheck": dict(precheck_record),
        "policy": dict(policy_effective["policy"]),
        "policy_ref": policy_effective["version_ref"],
        "policy_hash": policy_effective["policy_hash"],
    }


def _rows(lines: Sequence[str]) -> str:
    return "\n".join(f"  {line}" for line in lines) or "  (none)"


def _table_text(table: Mapping[str, Any]) -> str:
    theses = _rows([f"{row['row_id']}\t{row['statement']} (confidence {row['confidence']})"
                    for row in table["theses"]])
    debates = _rows([
        f"{row['row_id']}\t{'we differ' if row['divergent'] else 'aligned/undecided'}\t"
        f"{row['question']}\tmarket -> {row['market']}\tus -> {row['ours']}"
        for row in table["debates"]])
    metrics = _rows([
        f"{row['row_id']}\t{row['metric']}\t{row['period']}\tours {row['ours']}"
        f"{row['unit']}\tstreet {row['consensus']}{row['unit']}\tgap {row['gap_percent']}%"
        for row in table["metrics"]]) if table["metrics"] else (
        "  (" + str(table["consensus_gap"].get("reason")) + ")")
    street = _rows([
        f"{row['row_id']}\tas of {row['as_of']}\tclose {row['close']}\tprice target "
        f"{json.dumps(row['price_target'], ensure_ascii=False, sort_keys=True)}\t"
        f"ratings {json.dumps(row['recommendations'], ensure_ascii=False, sort_keys=True)}"
        for row in table["street"]])
    valuation = _rows([
        f"{row['row_id']}\tas of {row['as_of']}\t"
        f"{json.dumps(row['metrics'], ensure_ascii=False, sort_keys=True)}"
        for row in table["valuation"]])
    catalysts = _rows([f"{row['row_id']}\t{row['expected_date']}\t{row['event_kind']}"
                       for row in table["catalysts"]])
    variant = _rows([f"{row['row_id']}\t{row['slot_id']}\t{row['text']}"
                     for row in table["variant_slots"]])
    held = _rows([
        f"{row['row_id']}\t{row['name']}\tprobability {row['probability']}\tpayoff "
        f"{row['payoff']}%\ttrigger: {row['trigger']}" for row in table["held_scenarios"]])
    return (
        "OUR ADMITTED THESES -- <row id>\\t<statement>:\n" f"{theses}\n\n"
        "LIVE DEBATES -- <row id>\\t<where we stand>\\t<question>\\tmarket\\tus:\n"
        f"{debates}\n\n"
        "OUR FORECAST AGAINST THE STREET:\n" f"{metrics}\n\n"
        "THE STREET'S PRICE TARGET AND RATINGS:\n" f"{street}\n\n"
        "TODAY'S VALUATION SNAPSHOT (multiples and their own history):\n"
        f"{valuation}\n\n"
        "DATED CATALYSTS:\n" f"{catalysts}\n\n"
        "THE COMPANY FILE'S OWN VARIANT VIEW:\n" f"{variant}\n\n"
        "HELD STRUCTURED SCENARIOS (use them when they fit; their probability and "
        "payoff are fixed):\n" f"{held}\n"
    )


def build_prompt_v2(table: Mapping[str, Any]) -> str:
    policy = table["policy"]
    thresholds = "; ".join(
        f"{horizon}: {policy['min_expected_return_percent'][horizon]}% "
        f"(same direction as the street: "
        f"{policy['same_direction_min_expected_return_percent'][horizon]}%)"
        for horizon in policy["allowed_horizons"])
    return (
        "\n".join(final_text_instructions()) + "\n\n"
        "You write one investment call for a fundamental long-biased fund's own "
        "file. A person decides whether to act on it; you only propose it.\n\n"
        "Keep three things apart. (1) EVIDENCE CONFIDENCE: how well our view is "
        "supported by the rows -- an ordinal, resting on the thesis rows and the "
        "other rows you cite. (2) SCENARIOS: what could happen over the horizon "
        "-- a small table of outcomes, each with a probability, the share price "
        "return (payoff, in per cent, signed) and the observable trigger that "
        "would tell us we are in it. Probabilities add up to 1. Cover both "
        "sides: at least one outcome where the position loses. (3) ALIGNMENT: "
        "whether the street leans the same way we do. Agreeing with the market "
        "is allowed; the fund simply asks more of the expected return when we "
        "agree. Do not invent disagreement, and do not invent a market view: if "
        "the rows do not show where the street stands, say market_view.available "
        "false and alignment market_view_unavailable.\n\n"
        f"COMPANY: {table['company_ref']}\n\n" + _table_text(table) + "\n"
        "THE FUND'S POLICY FOR A CALL (the expected return is computed from your "
        "table, not typed by you). Propose only a call this policy admits:\n"
        f"  allowed directions: {', '.join(policy['allowed_directions'])}\n"
        f"  allowed horizons: {', '.join(policy['allowed_horizons'])}\n"
        f"  minimum evidence confidence: {policy['min_evidence_confidence']}\n"
        f"  at least {policy['required_scenarios']} scenarios and "
        f"{policy['min_invalidators']} invalidator(s)\n"
        f"  minimum expected return for the position: {thresholds}\n\n"
        "Rules:\n"
        "* direction must be one of the allowed directions: "
        f"{', '.join(policy['allowed_directions'])}. evidence_confidence.level must "
        f"be at least {policy['min_evidence_confidence']} and no higher than the "
        "best confidence among the T rows it cites.\n"
        "* If no call inside this policy is supported by the rows -- the evidence "
        "is not strong enough, the only honest direction is not allowed, or no "
        "allowed direction clears the expected-return threshold -- do not draft a "
        "call. Return exactly {\"no_call\": {\"reason\": \"<one sentence>\"}} "
        "instead; that is a complete, acceptable answer.\n"
        "* Cite only the row ids above. Never invent one.\n"
        "* evidence_confidence.refs must include at least one T row.\n"
        "* market_view.sources come from: " + ", ".join(MARKET_VIEW_SOURCES) + ".\n"
        "* alignment is one of: " + ", ".join(ALIGNMENTS) + ".\n"
        "* where_market_is_wrong is optional (null) -- give it only when the rows "
        "show a concrete point where the street's pricing differs from ours.\n"
        "* A scenario that uses a HELD STRUCTURED SCENARIO names its row in "
        "scenario_row_id; otherwise scenario_row_id is null. probability is a "
        "decimal in (0, 1]; payoff is the share's return over the horizon as a "
        "signed decimal per cent (\"-25\" is a 25% fall).\n"
        "* Every invalidator is an observable condition that would end the call; "
        "thesis_row_id names the thesis it breaks, or null.\n"
        "* decision is one of: " + ", ".join(DECISION_VOCABULARY) + ".\n"
        f"* Statements at most {MAX_STATEMENT_OUT_CHARS} characters; triggers at "
        f"most {MAX_TRIGGER_CHARS}; 2–{MAX_SCENARIOS} scenarios; 1–{MAX_FALSIFIERS} "
        "invalidators.\n"
        "* Return one raw JSON object and nothing else.\n\n"
        "{\"direction\": \"" + "|".join(policy["allowed_directions"]) + "\", \"decision\": \"<one of the five>\",\n"
        " \"time_horizon\": \"<one of the allowed horizons>\",\n"
        " \"our_view\": {\"statement\": \"...\", \"refs\": [\"T1\"]},\n"
        " \"evidence_confidence\": {\"level\": \"low|medium|high\", \"statement\": "
        "\"why this level\", \"refs\": [\"T1\", \"G1\"]},\n"
        " \"market_view\": {\"available\": true, \"reason\": null, \"statement\": \"...\",\n"
        "   \"refs\": [\"M1\"], \"sources\": [\"sell_side_rating\"]},\n"
        " \"alignment\": \"same_direction\", \"where_market_is_wrong\": null,\n"
        " \"scenarios\": [{\"name\": \"base\", \"probability\": \"0.5\", \"payoff\": \"15\",\n"
        "   \"trigger\": \"...\", \"refs\": [\"P1\"], \"scenario_row_id\": null}],\n"
        " \"invalidators\": [{\"statement\": \"...\", \"thesis_row_id\": \"T1\", "
        "\"falsifier_ref\": null}]}\n"
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _decimal_text(value: Any, name: str, *, low: Decimal, high: Decimal) -> str:
    if isinstance(value, bool) or value is None:
        raise ConvictionDraftRefused(f"{name} must be a decimal")
    text = str(value).strip().rstrip("%").strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ConvictionDraftRefused(f"{name} must be a decimal") from exc
    if not number.is_finite() or number < low or number > high:
        raise ConvictionDraftRefused(f"{name} must be a finite decimal in [{low}, {high}]")
    return str(number)


def parse_draft_v2(text: Any, table: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ConvictionDraftRefused("the drafting call returned nothing")
    parsed = unwrap_json_object(text)
    if parsed is None:
        raise ConvictionDraftRefused("the draft was not one JSON object")
    if isinstance(parsed, Mapping) and set(parsed) == {"no_call"}:
        body = parsed["no_call"]
        reason = body.get("reason") if isinstance(body, Mapping) else body
        if not isinstance(reason, str) or not reason.strip():
            raise ConvictionDraftRefused("no_call must carry a reason")
        raise ConvictionNoCall(NO_CALL_PREFIX + reason.strip()[:MAX_NO_CALL_REASON_CHARS])
    wire = _closed_reply(parsed, _REPLY_KEYS, "draft")
    citable = table["citable"]
    for key, allowed in (("direction", DIRECTIONS), ("decision", DECISION_VOCABULARY),
                         ("time_horizon", TIME_HORIZONS), ("alignment", ALIGNMENTS)):
        if wire[key] not in allowed:
            raise ConvictionDraftRefused(f"invalid {key}: {wire[key]!r}")

    thesis_by_row = {row["row_id"]: row["ref"] for row in table["theses"]}
    thesis_refs = set(thesis_by_row.values())
    evidence = _closed_reply(wire["evidence_confidence"], _EVIDENCE_KEYS,
                             "evidence_confidence")
    if evidence["level"] not in CONFIDENCES:
        raise ConvictionDraftRefused(f"invalid evidence level: {evidence['level']!r}")
    refs = _shown(evidence["refs"], "evidence_confidence.refs", citable, nonempty=True)
    cited_theses = [ref for ref in refs if ref in thesis_refs]
    if not cited_theses:
        raise ConvictionDraftRefused("evidence_confidence must rest on at least one thesis row")
    claim_refs = [ref for ref in refs if ref not in thesis_refs]
    for ref in cited_theses:
        for claim in table.get("thesis_claims", {}).get(ref, ()):
            if claim not in claim_refs:
                claim_refs.append(claim)

    market = _closed_reply(wire["market_view"], _MARKET_KEYS, "market_view")
    if not isinstance(market["available"], bool):
        raise ConvictionDraftRefused("market_view.available must be a boolean")
    if market["available"]:
        sources = market["sources"]
        if not isinstance(sources, list) or not sources or any(
                source not in MARKET_VIEW_SOURCES for source in sources):
            raise ConvictionDraftRefused("market_view.sources must come from the closed list")
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
    if (wire["alignment"] == "market_view_unavailable") == market_view["available"]:
        raise ConvictionDraftRefused(
            "alignment is market_view_unavailable exactly when no market view is held")
    wrong = wire["where_market_is_wrong"]
    wrong_block = None if wrong is None else _block(wrong, "where_market_is_wrong", citable)

    raw = wire["scenarios"]
    if not isinstance(raw, list) or len(raw) < 2 or len(raw) > MAX_SCENARIOS:
        raise ConvictionDraftRefused(f"scenarios must carry 2–{MAX_SCENARIOS} rows")
    held = {row["row_id"]: row for row in table.get("held_scenarios") or ()}
    scenarios: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        row = _closed_reply(item, _SCENARIO_KEYS, f"scenarios[{index}]")
        source = row["scenario_row_id"]
        held_row = None
        if source is not None:
            if not isinstance(source, str) or source not in held:
                raise ConvictionDraftRefused(
                    f"scenarios[{index}].scenario_row_id names {source!r}, which was "
                    "not a held scenario row")
            held_row = held[source]
        # Held numbers win, like calendar dates did in v1.
        probability = (held_row or {}).get("probability")
        if probability is None:
            probability = row["probability"]
        payoff = (held_row or {}).get("payoff")
        if payoff is None:
            payoff = row["payoff"]
        scenarios.append({
            "name": _text_field(row["name"], f"scenarios[{index}].name",
                                MAX_SCENARIO_NAME_CHARS),
            "probability": _decimal_text(probability, f"scenarios[{index}].probability",
                                         low=Decimal("0.0001"), high=Decimal("1")),
            "payoff": _decimal_text(payoff, f"scenarios[{index}].payoff",
                                    low=Decimal("-100"), high=Decimal(MAX_PERCENT)),
            "trigger": _text_field(row["trigger"], f"scenarios[{index}].trigger",
                                   MAX_TRIGGER_CHARS),
            "refs": _shown(row["refs"], f"scenarios[{index}].refs", citable, nonempty=True),
            "source_ref": None if held_row is None else held_row["ref"],
        })
    total = sum(Decimal(row["probability"]) for row in scenarios)
    if abs(total - 1) > Decimal("0.01"):
        raise ConvictionDraftRefused(f"scenario probabilities add up to {total}, not 1")

    known_falsifiers = {ref for row in table["theses"] for ref in row["falsifier_refs"]}
    raw_inv = wire["invalidators"]
    if not isinstance(raw_inv, list) or not raw_inv or len(raw_inv) > MAX_FALSIFIERS:
        raise ConvictionDraftRefused(f"invalidators must carry 1–{MAX_FALSIFIERS} rows")
    invalidators: list[dict[str, Any]] = []
    for index, item in enumerate(raw_inv):
        row = _closed_reply(item, _INVALIDATOR_KEYS, f"invalidators[{index}]")
        thesis_row = row["thesis_row_id"]
        if thesis_row is not None and thesis_row not in thesis_by_row:
            raise ConvictionDraftRefused(
                f"invalidators[{index}].thesis_row_id names {thesis_row!r}, which was "
                "not a thesis row")
        falsifier = row["falsifier_ref"]
        if falsifier is not None and falsifier not in known_falsifiers:
            raise ConvictionDraftRefused(
                f"invalidators[{index}].falsifier_ref names {falsifier!r}, which no "
                "shown thesis carries")
        invalidators.append({
            "statement": _text_field(row["statement"], f"invalidators[{index}].statement",
                                     MAX_STATEMENT_OUT_CHARS),
            "thesis_version_ref": None if thesis_row is None else thesis_by_row[thesis_row],
            "falsifier_ref": falsifier,
        })
    return {
        "direction": wire["direction"], "decision": wire["decision"],
        "time_horizon": wire["time_horizon"],
        "our_view": _block(wire["our_view"], "our_view", citable),
        "evidence_confidence": {
            "level": evidence["level"],
            "statement": _text_field(evidence["statement"], "evidence_confidence.statement",
                                     MAX_STATEMENT_OUT_CHARS),
            "thesis_refs": cited_theses, "claim_refs": claim_refs,
        },
        "scenarios": scenarios,
        "market_view_alignment": {"alignment": wire["alignment"],
                                  "market_view": market_view,
                                  "where_market_is_wrong": wrong_block},
        "invalidators": invalidators,
    }


def assemble_call_v2(draft: Mapping[str, Any], table: Mapping[str, Any],
                     valuation_binding: Mapping[str, Any]) -> dict[str, Any]:
    """The draft plus the deterministic blocks the authority will re-derive."""

    ordered = [row["ref"] for row in table["theses"]]
    cited = set(draft["evidence_confidence"]["thesis_refs"])
    cited.update(draft["our_view"]["refs"])
    cited.update(row["thesis_version_ref"] for row in draft["invalidators"]
                 if row["thesis_version_ref"])
    thesis_refs = [ref for ref in ordered if ref in cited]
    debate_by_ref = {row["ref"] for row in table["debates"]}
    used: set[str] = set()
    for block in (draft["our_view"], draft["market_view_alignment"]["market_view"],
                  draft["market_view_alignment"]["where_market_is_wrong"] or {}):
        used.update(block.get("refs") or [])
    for row in draft["scenarios"]:
        used.update(row["refs"])
    used.update(draft["evidence_confidence"]["claim_refs"])
    call = {
        **{key: draft[key] for key in (
            "direction", "decision", "time_horizon", "our_view", "evidence_confidence",
            "scenarios", "market_view_alignment", "invalidators")},
        "consensus_gap": dict(table["consensus_gap"]),
        "valuation": dict(valuation_binding),
        "thesis_refs": thesis_refs,
        "debate_refs": sorted(ref for ref in used if ref in debate_by_ref),
    }
    call["expected_return"] = expected_return_of(call["scenarios"], call["direction"])
    call["gate"] = evaluate_policy_gate(
        direction=call["direction"], time_horizon=call["time_horizon"],
        evidence_level=call["evidence_confidence"]["level"],
        scenario_count=len(call["scenarios"]),
        invalidator_count=len(call["invalidators"]),
        alignment=call["market_view_alignment"]["alignment"],
        position_percent=call["expected_return"]["position_percent"],
        policy=table["policy"],
    )
    return call


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------

def build_verifier_prompt_v2(table: Mapping[str, Any], call: Mapping[str, Any]) -> str:
    by_ref = {ref: row_id for row_id, ref in table["citable"].items()}

    def cite(refs: Sequence[str]) -> str:
        return " ".join(by_ref.get(ref, ref) for ref in refs)

    alignment = call["market_view_alignment"]
    market = alignment["market_view"]
    expected = call["expected_return"]
    lines = [
        "You are an independent verifier. Another model wrote the investment call",
        "below from the table that follows. You do not rewrite or improve it. You",
        "answer one question: is every part of it supported by the rows it cites?",
        "Agreeing with the market is allowed and is not a finding. Check that the",
        "evidence confidence is not overstated, that each scenario's probability and",
        "payoff are plausible against the valuation and street rows, that each",
        "trigger is observable, and that the stated alignment reads the rows right.",
        "",
        f"COMPANY: {table['company_ref']}", "", _table_text(table),
        "THE CALL UNDER REVIEW:",
        f"  direction: {call['direction']} over {call['time_horizon']} "
        f"(decision {call['decision']})",
        f"  our view: {call['our_view']['statement']}  [{cite(call['our_view']['refs'])}]",
        f"  evidence confidence {call['evidence_confidence']['level']}: "
        f"{call['evidence_confidence']['statement']}  "
        f"[{cite(call['evidence_confidence']['thesis_refs'] + call['evidence_confidence']['claim_refs'])}]",
        (f"  market view ({', '.join(market['sources'])}): {market['statement']}  "
         f"[{cite(market['refs'])}]" if market["available"]
         else f"  market view: not established -- {market['reason']}"),
        f"  alignment with the street: {alignment['alignment']}",
    ]
    if alignment["where_market_is_wrong"]:
        lines.append(f"  where the market is wrong: "
                     f"{alignment['where_market_is_wrong']['statement']}  "
                     f"[{cite(alignment['where_market_is_wrong']['refs'])}]")
    lines.append("  scenarios (probability, share return over the horizon, trigger):")
    for row in call["scenarios"]:
        held = " (held structured scenario)" if row["source_ref"] else ""
        lines.append(f"    - {row['name']}: p={row['probability']}, {row['payoff']}%, "
                     f"trigger: {row['trigger']}{held}  [{cite(row['refs'])}]")
    lines += [
        f"  expected share return {expected['stock_percent']}%; for this position "
        f"{expected['position_percent'] if expected['position_percent'] is not None else 'n/a'}%",
        "  invalidators:",
    ]
    for row in call["invalidators"]:
        lines.append(f"    - {row['statement']}")
    lines += [
        "",
        "Return one raw JSON object and nothing else:",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES_V2) + '",',
        '   "detail": "<one sentence>"}]}',
        "A pass verdict has no findings; a reject verdict has at least one.",
        f"Return at most {MAX_VERIFIER_FINDINGS} findings; each detail at most "
        f"{MAX_STATEMENT_OUT_CHARS} characters.",
    ]
    prompt = "\n".join(lines)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ConvictionDraftRefused(
            f"the verifier prompt is {len(prompt.encode('utf-8'))} bytes, over the "
            f"{MAX_PROMPT_BYTES} bound")
    return prompt


def parse_verdict_v2(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ConvictionDraftRefused("the verifier returned nothing")
    parsed = unwrap_json_object(text)
    if parsed is None or set(parsed) != {"verdict", "findings"}:
        raise ConvictionDraftRefused("the verdict must be exactly verdict and findings")
    if parsed["verdict"] not in VERIFIER_VERDICTS:
        raise ConvictionDraftRefused(f"invalid verdict: {parsed['verdict']!r}")
    rows = parsed["findings"]
    if not isinstance(rows, list) or len(rows) > MAX_VERIFIER_FINDINGS:
        raise ConvictionDraftRefused("findings must be a short array")
    findings = []
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != {"code", "detail"}:
            raise ConvictionDraftRefused("each finding is exactly code and detail")
        if raw["code"] not in VERIFIER_FINDING_CODES_V2:
            raise ConvictionDraftRefused(f"unknown finding code: {raw['code']!r}")
        findings.append({"code": str(raw["code"]),
                         "detail": _text_field(raw["detail"], "finding.detail",
                                               MAX_STATEMENT_OUT_CHARS)})
    if (parsed["verdict"] == "pass") == bool(findings):
        raise ConvictionDraftRefused("a pass has no findings and a reject has some")
    return {"verdict": parsed["verdict"], "findings": findings}


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def model_call_record(phase: str, answer: Mapping[str, Any]) -> dict[str, Any]:
    """One model answer, as the daily drafting cap counts it.

    ``cost_micros`` is what this run paid.  A replayed answer (same request,
    unchanged prompt) pays nothing now, so it also carries what the work
    order was booked at and on which day (``settled_*``): the cap counts each
    work order once, at its paid cost, on the day it was paid.
    """

    record: dict[str, Any] = {
        "phase": phase,
        "work_order_ref": answer.get("work_order_ref"),
        "cost_micros": int(answer.get("cost_micros") or 0),
        "cost_status": answer.get("cost_status"),
        "replayed": bool(answer.get("replayed")),
    }
    if answer.get("settled_cost_micros") is not None:
        record["settled_cost_micros"] = int(answer["settled_cost_micros"])
        record["settled_day"] = answer.get("settled_day")
    return record


def draft_conviction_call_v2(
    *,
    table: Mapping[str, Any],
    valuation_binding: Mapping[str, Any],
    model: Any,
    mission: Mapping[str, Any],
    family_of: Callable[[Any], str | None] | None = None,
    rubric_ref: str,
    rubric_hash: str,
) -> dict[str, Any]:
    """One drafting call, the policy gate, one verifying call.  Never writes."""

    resolve = family_of or (lambda ref: None)
    prompt = build_prompt_v2(table)
    result: dict[str, Any] = {
        "status": "failed", "company_ref": table["company_ref"],
        "prompt_bytes": len(prompt.encode("utf-8")), "cost_micros": 0,
        "model_called": False, "model_calls": [], "call": None, "reason": None,
        "findings": [], "gate": None, "drafted_by": None, "verified_by": None,
        "rubric": {"rubric_ref": rubric_ref, "rubric_hash": rubric_hash, "findings": []},
    }
    if result["prompt_bytes"] > MAX_PROMPT_BYTES:
        result.update({"status": "refused",
                       "reason": f"the input table is {result['prompt_bytes']} bytes, "
                                 f"over the {MAX_PROMPT_BYTES} bound"})
        return result
    no_call = policy_no_call_reason(table["policy"], table["theses"])
    if no_call is not None:
        # The policy cannot admit any call on these inputs: say so for free.
        result.update({"status": NO_CALL_STATUS, "reason": no_call})
        return result
    try:
        drafted = model.call(purpose=PURPOSE,
                             request_id=prompt_drafter("", prompt)[1][:32],
                             prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        result.update({"status": "model_unavailable", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    result["model_called"] = True
    result["cost_micros"] += int(drafted.get("cost_micros") or 0)
    result["model_calls"].append(model_call_record("draft", drafted))
    drafted_by = _provenance(drafted, resolve(drafted.get("route_decision_ref")))
    result["drafted_by"] = drafted_by
    try:
        parsed = parse_draft_v2(drafted.get("text"), table)
    except ConvictionNoCall as exc:
        result.update({"status": NO_CALL_STATUS, "reason": exc.reason})
        return result
    except ConvictionDraftError as exc:
        result.update({"status": "refused", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    call = assemble_call_v2(parsed, table, valuation_binding)
    result["gate"] = call["gate"]
    if call["gate"]["status"] != "passed":
        if call["direction"] == "avoid" and "avoid" not in table["policy"]["allowed_directions"]:
            # "Avoid" is the drafter abstaining, not a position the policy
            # admits: the governed answer is "no call today", with its reason.
            result.update({
                "status": NO_CALL_STATUS, "call": call,
                "reason": NO_CALL_PREFIX + call["our_view"]["statement"][
                    :MAX_NO_CALL_REASON_CHARS]})
            return result
        # The owner's thresholds, applied before a second model call is paid.
        result.update({
            "status": "policy_refused", "call": call,
            "reason": POLICY_REFUSED_PREFIX + "；".join(
                POLICY_REFUSAL_LABELS[item] for item in call["gate"]["reasons"]),
        })
        return result
    findings = rubric_findings_v2(call)
    result["rubric"]["findings"] = findings
    if findings:
        result.update({"status": "rubric_failed",
                       "reason": "the call fails the v2 rubric mechanically: "
                                 + ", ".join(findings)})
        return result
    try:
        verifier_prompt = build_verifier_prompt_v2(table, call)
        check = independent_model_call(
            model, producer_route_decision_refs=[drafted_by["route_decision_ref"]],
            purpose=VERIFIER_PURPOSE,
            request_id=content_hash({"verify-v2": table["company_ref"],
                                     "direction": call["direction"],
                                     "policy": table["policy_hash"]})[:32],
            prompt=verifier_prompt, mission=mission)
    except ConvictionDraftError as exc:
        result.update({"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    except CockpitModelError as exc:
        result.update({"status": "unverified",
                       "reason": f"the verifying call did not run: {exc}"})
        return result
    result["cost_micros"] += int(check.get("cost_micros") or 0)
    result["model_calls"].append(model_call_record("verify", check))
    verified_by = _provenance(check, resolve(check.get("route_decision_ref")))
    result["verified_by"] = verified_by
    ok, reason = independent(drafted_by, verified_by)
    if not ok:
        result.update({"status": "not_independent", "reason": reason})
        return result
    try:
        verdict = parse_verdict_v2(check.get("text"))
    except ConvictionDraftError as exc:
        result.update({"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    if verdict["verdict"] != "pass":
        result.update({"status": "verifier_rejected", "findings": verdict["findings"],
                       "reason": "; ".join(item["code"] for item in verdict["findings"])})
        return result
    result.update({"status": "verified", "call": call, "reason": None})
    return result


__all__ = [
    "NO_CALL_PREFIX", "NO_CALL_STATUS", "POLICY_REFUSED_PREFIX", "ConvictionNoCall",
    "policy_no_call_reason",
    "PROVIDER_FINDING_CODES", "TASK_HASH_V2", "TASK_REF_V2", "VERIFIER_FINDING_CODES_V2",
    "assemble_call_v2", "build_input_table_v2", "build_prompt_v2",
    "build_verifier_prompt_v2", "draft_conviction_call_v2", "parse_draft_v2",
    "parse_verdict_v2",
]
