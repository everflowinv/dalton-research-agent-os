"""P12a drafting: one bounded call per unit, verified before it is believed.

The prompt is a **table**, not an object graph, for the reason
``company_model_spec`` found and ``claim_index_tagging`` repeated: the same
content encoded as JSON objects costs several times the bytes in repeated
keys, and the router reserves budget against the size of the prompt.  So the
material is ``tag<TAB>period<TAB>importance<TAB>text`` and the reply is the
only JSON in the exchange.

What the model is shown for one unit:

* the **canonical** Claims for that aspect, importance-ordered and bounded --
  P12b's index is what makes "the Claims about supply and cost, one copy of
  each fact, filings before news" a query rather than a research project;
* the numbers it may cite, from the model input table and the driver model's
  cells, each with the text that carries the figure verbatim;
* the previous version of this section, so that the new one can advance rather
  than restate;
* the slot structure, which for ``demand_drivers`` and ``supply_and_cost``
  came from the Constitution's causal chain.

What comes back is refused whole on any deviation -- a tag that was not shown,
a slot that was not asked for, a slot missing, more sentences than the cap, a
sentence citing nothing, a key the contract does not have.  Never repaired.  A
reply that invents a tag is not a reply that read the table, and the rows it
happened to get right came out of the same reply.

**The verifier is a different model family.**  D2 in the plan: a cognition-layer
output is checked by something that is not the thing that wrote it, and
"different family" is the same independence predicate the thesis-impact commit
gate already uses.  It is enforced *after* the calls by reading both route
decisions, because the family that served is a fact about the route and not
about what we asked for -- and it fails closed: an unresolvable family is not
an independent one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .claim_aspect_vocabulary import DEFINITIONS
from .cockpit_model import (CockpitModelError, independent_model_call,
                            model_failure_trace, register_purpose, unwrap_json_object)
from .company_dossier import (
    CLASSIFICATION_UNIT,
    MAX_GAPS as MAX_DOSSIER_GAPS,
    CLASSIFICATION_DEFINITIONS,
    INDUSTRY_CLASSIFICATIONS,
    MAX_GAP_CHARS,
    MAX_GAPS,
    MAX_SENTENCE_CHARS,
    MAX_SOURCES_PER_SECTION,
    SECTION_SENTENCE_CAP,
    SECTIONS,
    SLOT_SENTENCE_CAP,
    VARIANT_UNIT,
    _CONCLUSION_PATTERNS,
    CompanyDossierValidationError,
    section_body,
    validate_classification,
    validate_section,
    validate_variant_view,
)
from .driver_template import (
    MAX_TEMPLATE_GAPS,
    cost_prompt_block,
    cost_template_gaps,
    section_texts,
    dossier_demand_driver_gaps,
    prompt_block,
    template_for,
)
from .store import content_hash

# P14-0's registry: a lane names its own purpose from its own module rather
# than editing a set in ``cockpit_model``.  Registered at import because
# importing this module is what makes the drafter reachable.
DRAFT_PURPOSE = register_purpose("dossier")
VERIFIER_PURPOSE = register_purpose("dossier_verifier")

# The dossier drafts on the deliverable-drafting configuration, which is
# already in the registry: same route, same broker, same day ledger as the
# Initial Screen it sits above.  A separate configuration would only be worth
# its wiring if the dossier needed its own rate limit.
MODEL_CONFIG_NAME = "initial-screen-model-config.json"

# This identity changes only when the producer reply contract changes.  The
# prompt SHA already binds every WorkOrder; the exported fingerprint also lets
# the lane retire a terminal refusal after a reviewed contract repair without
# pretending that the underlying company evidence changed.
DRAFT_CONTRACT_VERSION = "company-dossier-draft-contract:0.5"
VERIFIER_PROMPT_CONTRACT_VERSION = "company-dossier-verifier-prompt-contract:0.3"
LEGACY_VERIFIER_PROMPT_CONTRACT_VERSION = "company-dossier-verifier-prompt-contract:0.2"
VARIANT_CONCLUSION_RULE_VERSION = "variant-no-investment-conclusion:1"


def draft_contract_fingerprint() -> str:
    return content_hash({
        "version": DRAFT_CONTRACT_VERSION,
        "max_gaps": MAX_GAPS,
        "max_gap_chars": MAX_GAP_CHARS,
        "max_sentence_chars": MAX_SENTENCE_CHARS,
        "max_sources": MAX_SOURCES_PER_SECTION,
        "slot_sentence_cap": SLOT_SENTENCE_CAP,
        "section_sentence_cap": SECTION_SENTENCE_CAP,
        "variant_conclusion_rule": VARIANT_CONCLUSION_RULE_VERSION,
        "variant_conclusion_patterns": _CONCLUSION_PATTERNS,
    })


def verifier_prompt_contract_fingerprint() -> str:
    """Identity of the evidence projection shown to the verifier."""

    return content_hash({
        "version": VERIFIER_PROMPT_CONTRACT_VERSION,
        "source_fields": ["kind", "period", "text"],
        "max_row_chars": MAX_ROW_CHARS,
        "finding_codes": list(VERIFIER_FINDING_CODES),
    })

def legacy_verifier_prompt_contract_fingerprint() -> str:
    """Identity of the evidence projection shown to the verifier."""

    return content_hash({
        "version": LEGACY_VERIFIER_PROMPT_CONTRACT_VERSION,
        "source_fields": ["kind", "period", "text"],
        "max_row_chars": MAX_ROW_CHARS,
        "finding_codes": list(VERIFIER_FINDING_CODES),
    })

# Bounded like ``company_model_cli``'s constants and for the same reason: the
# router estimates on prompt bytes, so a bound that looks frugal buys nothing
# but a refusal.  One unit is one section of one company.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 3_000
MAX_COST_USD = 0.60
TIMEOUT_SECONDS = 180
# What one tick may spend across all its calls, verifier included.  A dossier
# is ten sections plus two blocks; drafting all twelve in one tick would be a
# third of the live mission's daily budget in one lane.
MAX_RUN_COST_USD = 2.50
MAX_UNITS_PER_RUN = 3

# What the prompt may carry.
MAX_CLAIM_ROWS = 40
MAX_NUMBER_ROWS = 30
MAX_ROW_CHARS = 400
MAX_PRIOR_CHARS = 2_000

# What may stand behind a sentence about *the market's* view. A filing and a
# management statement are the company talking; quoting them as evidence of
# what the street thinks is how a variant view becomes a restatement of the
# company's own case with the disagreement invented.
MARKET_VIEW_GRADES: frozenset[str] = frozenset({"sell_side", "news"})

VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
# Closed, and short: a verifier with an open vocabulary writes essays.
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    # a sentence asserts more than the rows it cites carry
    "unsupported_sentence",
    # a figure in the prose is not in the cited row verbatim
    "number_not_in_source",
    # the draft answered a different question from the slot it filled
    "structure_deviation",
    # the draft states an investment conclusion; a dossier is a file, not a call
    "conclusion_beyond_evidence",
)


class DossierDraftError(RuntimeError):
    """The drafting path failed."""


class DossierDraftRefused(DossierDraftError):
    """The reply deviated from the contract and was refused whole."""


class DossierDraftInsufficientEvidence(DossierDraftError):
    """A conforming reply said that every requested slot lacks support."""

    def __init__(self, unit: str, slots: Sequence[Mapping[str, Any]],
                 gaps: Sequence[str]) -> None:
        self.unit = unit
        self.slots = [dict(row) for row in slots]
        self.gaps = list(gaps)
        super().__init__(f"{unit}: the supplied material supports no requested slot")


# ---------------------------------------------------------------------------
# material
# ---------------------------------------------------------------------------


def material_rows(
    claims: Sequence[Mapping[str, Any]] = (),
    numbers: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Tag the material for one unit: ``C``₁..ₙ statements, ``N``₁..ₙ figures.

    Claims arrive in the order the caller chose -- importance first, then
    recency, which is ``claim_index_authority.canonical_order_key`` -- and the
    bound is applied here rather than in the query so that the prompt's size
    is decided in one place.
    """

    rows: list[dict[str, Any]] = []
    for index, claim in enumerate(list(claims)[:MAX_CLAIM_ROWS], start=1):
        rows.append({
            "tag": f"C{index}",
            "kind": "claim",
            "ref": str(claim.get("ref") or claim.get("claim_version_ref") or ""),
            "text": str(claim.get("text") or claim.get("normalized_statement") or "")[:MAX_ROW_CHARS],
            "period": claim.get("period") or claim.get("period_key"),
            "importance": claim.get("importance"),
        })
    for index, number in enumerate(list(numbers)[:MAX_NUMBER_ROWS], start=1):
        kind = str(number.get("kind") or "figure")
        if kind not in ("figure", "forecast_cell"):
            raise CompanyDossierValidationError(
                f"a number row is a figure or a forecast_cell; got {kind!r}")
        rows.append({
            "tag": f"N{index}",
            "kind": kind,
            "ref": str(number.get("ref") or ""),
            "text": str(number.get("text") or "")[:MAX_ROW_CHARS],
            "period": number.get("period"),
            "importance": number.get("importance"),
        })
    seen = {row["tag"] for row in rows}
    if len(seen) != len(rows):
        raise CompanyDossierValidationError("material tags must be unique")
    if any(not row["ref"] or not row["text"] for row in rows):
        raise CompanyDossierValidationError("every material row needs a ref and a text")
    return rows


def render_material(rows: Sequence[Mapping[str, Any]]) -> str:
    claims = [row for row in rows if row["kind"] == "claim"]
    numbers = [row for row in rows if row["kind"] != "claim"]
    lines: list[str] = []
    lines.append(f"Statements available ({len(claims)}) -- tag, period, source grade, text:")
    for row in claims:
        lines.append("\t".join([
            row["tag"], str(row["period"] or "-"), str(row["importance"] or "-"),
            row["text"],
        ]))
    lines.append("")
    lines.append(f"Figures available ({len(numbers)}) -- tag, period, kind, text:")
    for row in numbers:
        lines.append("\t".join([
            row["tag"], str(row["period"] or "-"), row["kind"], row["text"],
        ]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------


def _unit_purpose(unit: str) -> str:
    if unit in SECTIONS:
        return DEFINITIONS[unit]
    if unit == CLASSIFICATION_UNIT:
        return ("the Deep Insight Gate's first question: which of five kinds of "
                "business this is, why, and the strongest fact against it")
    return ("the variant view: what this file concludes, what the market is "
            "paying for, where they differ and what would settle it")


#: The one section whose slots are the industry's causal chain and whose
#: *content* W4 gives demand its per-classification frame and W5 adds a
#: separate cost-slot frame for ``supply_and_cost``.  They share the drafting
#: path, while each unit receives only the registry whose subject it covers.
TEMPLATE_UNITS: frozenset[str] = frozenset({"demand_drivers", "supply_and_cost"})


def build_unit_prompt(
    *,
    unit: str,
    structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]],
    company: Mapping[str, Any],
    prior_body: str = "",
    profile_table: str = "",
    market_view_available: bool = True,
    classification: Any = None,
    _variant_conclusion_rule: bool = True,
    _analytical_contract: bool = True,
) -> str:
    """One unit's prompt: the slots, the rules, the material, the last version.

    ``classification`` is W4's addition and it changes exactly one section.
    ``demand_drivers`` still takes its *structure* from the Constitution's
    causal chain -- that is C3 and it is not the model's to choose -- but the
    questions worth asking about demand differ by what kind of business this
    is, so the driver template for the company's ``industry_classification`` is
    shown beneath the slots as a checklist. It is a checklist and not a
    structure: a slot it names that the material cannot answer belongs in
    ``gaps``, which is what the prompt asks for.
    """

    lines = [
        "You are writing one part of a company file for a fundamental, long-biased fund.",
        "The file is read by a portfolio manager who knows the sector. Write in Chinese.",
        "You are not advising and not recommending: a file says what is true about a",
        "company; the decision about what to do with it is made elsewhere by a person.",
        "",
        f"Company: {company.get('ticker') or ''} ({company.get('company_ref')})",
        f"Part: {unit} -- {_unit_purpose(unit)}",
        "",
        "Structure. Fill every slot below, in this order, and invent none:",
    ]
    for slot in structure:
        lines.append(f"  {slot['slot_id']}\t{slot['prompt']}")
    lines += [
        "",
        "Hard rules:",
        "- Use ONLY the tagged material below. Cite by tag in the refs array.",
        "- NEVER write a C or N tag inside a sentence's text: not as a word, not in",
        "  brackets, not in a source list. The tags travel in refs; a sentence whose",
        "  subject is a tag becomes a sentence with no subject once the tag is gone.",
        *(([] if _analytical_contract else [
            "- Every sentence must cite at least one tag. A sentence you cannot cite is a",
            "  sentence you may not write.",
        ])),
        "- Copy any figure verbatim from the tag that carries it. Do not convert units",
        "  or scales, do not round, do not recompute a percentage.",
        f"- At most {SLOT_SENTENCE_CAP} sentences in a slot and {SECTION_SENTENCE_CAP} in this part.",
        f"- Return at most {MAX_GAPS} gaps. Each gap must be a non-empty string of at most "
        f"{MAX_GAP_CHARS} characters; combine related missing evidence rather than adding "
        "another item.",
        f"- Every sentence text and every unknown string must be non-empty and at most "
        f"{MAX_SENTENCE_CHARS} characters.",
        f"- Cite at most {MAX_SOURCES_PER_SECTION} distinct material tags across this part.",
        "- If the material does not answer a slot, return that slot as",
        '  {"slot_id": "<id>", "unknown": "<what is missing to answer it>"} instead of',
        "  writing something plausible. An honest unknown is worth more than a guess.",
        "- Do not repeat the previous version. Say what the new evidence changes.",
        "",
    ]
    if _analytical_contract:
        lines += [
            "- Every sentence must cite at least one tag. Analytical inference is allowed only when",
            "  its cited rows contain the premises: label it as 判断/推断 and state the",
            "  uncertainty. A sentence with no cited premise is a sentence you may not write.",
            "- When a slot asks for a view, driver, competitive position, catalyst, or risk,",
            "  choose the current evidence-weighted case. State the alternative trigger, expected",
            "  operating/earnings/valuation impact only where supported, its falsifier, and the next",
            "  observable tracking item. Do not hide behind A也可能、B也可能; qualify confidence.",
            "",
        ]
    if unit == VARIANT_UNIT and _variant_conclusion_rule:
        forbidden = ", ".join(_CONCLUSION_PATTERNS)
        lines += [
            "Variant-view boundary:",
            "- Describe only the cited evidence about the market view and where it differs",
            "  from company evidence. Do not turn that disagreement into our valuation,",
            "  recommendation, position change, or price target.",
            f"- Do not use these investment-conclusion phrases: {forbidden}.",
            "",
        ]
    if unit == CLASSIFICATION_UNIT:
        # A table, and deliberately not indented like the slot list above: the
        # slots are the structure and the vocabulary is a menu, and a reply
        # that read the menu as structure would fill eight slots of two.
        lines.append("Choose exactly one classification from this closed list:")
        for word in INDUSTRY_CLASSIFICATIONS:
            lines.append(f"{word}\t{CLASSIFICATION_DEFINITIONS[word]}")
        lines.append("")
        lines.append("Return raw JSON only, no markdown fence:")
        lines.append('{"classification": "<one word from the list>",')
        lines.append(' "slots": [{"slot_id": "<id>", "sentences": [{"text": "<one sentence>",')
        lines.append('   "refs": ["C3"]}]}], "gaps": ["<what is missing>"]}')
    else:
        if unit == VARIANT_UNIT and not market_view_available:
            lines.append(
                "No material about the market's view was found, so there is no "
                "market_view slot. Do not speculate about what the market thinks.")
            lines.append("")
        lines.append("Return raw JSON only, no markdown fence:")
        lines.append('{"slots": [{"slot_id": "<id>", "sentences": [{"text": "<one sentence>",')
        lines.append('   "refs": ["C3","N1"]}]}], "gaps": ["<what is missing>"]}')
    if unit in TEMPLATE_UNITS:
        lines += [
            (cost_prompt_block(classification) if unit == "supply_and_cost"
             else prompt_block(classification)),
            "",
            "That template is a checklist, not a structure. Fill the slots "
            "above; where a template slot has nothing behind it in the "
            "material, put it in gaps rather than writing round it.",
            "",
        ]
    lines.append("")
    if profile_table:
        lines += [
            "This table is already computed and is not yours to change. Describe it;",
            "do not recount it, do not re-derive the classification:",
            profile_table, "",
        ]
    if prior_body:
        lines += ["Previous version of this part:", prior_body[:MAX_PRIOR_CHARS], ""]
    lines.append(render_material(material))
    return "\n".join(lines)


def legacy_unit_prompt_v02(**kwargs: Any) -> str:
    """Rebuild an immutable v0.2 producer question for formal replay only."""

    return build_unit_prompt(**kwargs, _variant_conclusion_rule=False, _analytical_contract=False)


def legacy_unit_prompt_v03(**kwargs: Any) -> str:
    """Rebuild an immutable v0.3 producer question for formal replay only."""

    return build_unit_prompt(**kwargs, _analytical_contract=False)


# ---------------------------------------------------------------------------
# the reply
# ---------------------------------------------------------------------------


def _resolve_tags(
    slots: Any, material: Sequence[Mapping[str, Any]], *, unit: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn cited tags into refs, or refuse.  Sources are what was cited."""

    by_tag = {row["tag"]: row for row in material}
    if not isinstance(slots, list) or not slots:
        raise DossierDraftRefused(f"{unit}: the reply carries no slots")
    resolved: list[dict[str, Any]] = []
    cited: dict[str, dict[str, Any]] = {}
    for index, slot in enumerate(slots):
        if not isinstance(slot, Mapping):
            raise DossierDraftRefused(f"{unit}: slots[{index}] is not an object")
        if set(slot) == {"slot_id", "unknown"}:
            resolved.append(dict(slot))
            continue
        if set(slot) != {"slot_id", "sentences"}:
            raise DossierDraftRefused(
                f"{unit}: slots[{index}] has keys {sorted(slot)}; the contract is "
                "slot_id with either sentences or unknown")
        rows = slot["sentences"]
        if not isinstance(rows, list):
            raise DossierDraftRefused(f"{unit}: slots[{index}].sentences is not a list")
        sentences = []
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping) or set(row) != {"text", "refs"}:
                raise DossierDraftRefused(
                    f"{unit}: slots[{index}].sentences[{position}] must be text and refs")
            tags = row["refs"]
            if not isinstance(tags, list) or not tags:
                raise DossierDraftRefused(
                    f"{unit}: slots[{index}].sentences[{position}] cites nothing")
            refs = []
            for tag in tags:
                found = by_tag.get(str(tag))
                if found is None:
                    raise DossierDraftRefused(
                        f"{unit}: the reply cites {tag!r}, which was not shown; the "
                        "batch is refused whole")
                refs.append(found["ref"])
                cited[found["ref"]] = found
            sentences.append({"text": row["text"], "refs": refs})
        resolved.append({"slot_id": slot["slot_id"], "sentences": sentences})
    sources = [
        {"kind": row["kind"], "ref": row["ref"], "text": row["text"],
         "period": None if row.get("period") is None else str(row["period"])}
        for row in cited.values()
    ]
    if len(sources) > MAX_SOURCES_PER_SECTION:
        raise DossierDraftRefused(
            f"{unit}: the reply cites {len(sources)} sources; the cap is "
            f"{MAX_SOURCES_PER_SECTION}")
    return resolved, sources


def parse_unit_output(
    text: str,
    *,
    unit: str,
    structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]],
    market_view_available: bool = True,
    profile: Mapping[str, Any] | None = None,
    classification: Any = None,
) -> dict[str, Any]:
    """Validate one reply against the closed contract, or refuse it whole.

    The driver-template check is the one thing here that does *not* refuse.
    Template slots the drafted section says nothing about are appended to
    ``gaps`` -- behind whatever the drafter noticed itself, and only as far as
    the section's own cap allows -- because a missing driver is a hole in the
    file that the reader has to see, and a refusal would delete the record that
    the question was ever asked.
    """

    value = unwrap_json_object(text)
    if value is None:
        raise DossierDraftRefused(f"{unit}: the reply is not a JSON object")
    expected = {"slots", "gaps"} | ({"classification"} if unit == CLASSIFICATION_UNIT else set())
    if set(value) != expected:
        raise DossierDraftRefused(
            f"{unit}: the reply has keys {sorted(value)}; the contract is "
            f"{sorted(expected)}")
    slots, sources = _resolve_tags(value["slots"], material, unit=unit)
    gaps = list(value.get("gaps") or [])
    if slots and all(set(slot) == {"slot_id", "unknown"} for slot in slots):
        # This is the answer the prompt explicitly asks for when the material
        # does not answer the question. Validate its exact closed shape before
        # distinguishing it from a contract violation. It is not a draft and
        # therefore never reaches the citation gate.
        from .company_dossier import _gaps, _slot

        ids = [slot["slot_id"] for slot in structure]
        if len(slots) != len(ids):
            raise DossierDraftRefused(
                f"{unit}: the reply fills {len(slots)} slots; every slot must be "
                "answered or explicitly unknown")
        try:
            if unit == CLASSIFICATION_UNIT:
                checked = validate_classification({
                    "classification": value["classification"],
                    "slots": slots, "sources": [], "gaps": value.get("gaps"),
                })
                checked_slots, checked_gaps = checked["slots"], checked["gaps"]
            elif unit == VARIANT_UNIT:
                checked = validate_variant_view({
                    "status": "drafted", "reason": None,
                    "market_view_available": bool(market_view_available),
                    "market_view_reason": (
                        None if market_view_available else
                        "no consensus, rating, sales note or crowd narrative "
                        "material was found for this company"),
                    "structure": ids, "slots": slots, "sources": [],
                    "gaps": value.get("gaps"),
                })
                checked_slots, checked_gaps = checked["slots"], checked["gaps"]
            else:
                checked_slots = [
                    _slot(slot, f"{unit}.slots[{index}]", expected_id=ids[index],
                          allowed_refs=set())
                    for index, slot in enumerate(slots)
                ]
                checked_gaps = _gaps(value.get("gaps"), f"{unit}.gaps")
        except CompanyDossierValidationError as exc:
            raise DossierDraftRefused(f"{unit}: {exc}") from exc
        raise DossierDraftInsufficientEvidence(unit, checked_slots, checked_gaps)
    if unit == VARIANT_UNIT:
        grades = {row["ref"]: row.get("importance") for row in material}
        for slot in slots:
            if slot["slot_id"] != "market_view":
                continue
            for row in slot.get("sentences") or ():
                for ref in row["refs"]:
                    if grades.get(ref) not in MARKET_VIEW_GRADES:
                        raise DossierDraftRefused(
                            f"{unit}: market_view cites {ref} ({grades.get(ref)}), "
                            "which is the company talking rather than the market")
    ids = [slot["slot_id"] for slot in structure]
    if unit in TEMPLATE_UNITS:
        # The drafter's own gaps first: it read the material and this table did
        # not, so when the cap bites the more specific complaint survives.
        room = max(0, MAX_DOSSIER_GAPS - len(gaps))
        if room:
            section = {"structure": ids, "slots": slots, "gaps": gaps}
            found = (cost_template_gaps(
                classification, section_texts(section),
                subject="档案 supply_and_cost", limit=min(room, MAX_TEMPLATE_GAPS))
                     if unit == "supply_and_cost" else
                     dossier_demand_driver_gaps(
                         section, classification, limit=min(room, MAX_TEMPLATE_GAPS)))
            gaps = gaps + found[:room]
    try:
        if unit == CLASSIFICATION_UNIT:
            return validate_classification({
                "classification": value["classification"],
                "slots": slots, "sources": sources, "gaps": gaps,
            })
        if unit == VARIANT_UNIT:
            return validate_variant_view({
                "status": "drafted", "reason": None,
                "market_view_available": bool(market_view_available),
                "market_view_reason": (None if market_view_available else
                                       "no consensus, rating, sales note or crowd "
                                       "narrative material was found for this company"),
                "structure": ids, "slots": slots, "sources": sources,
                "gaps": gaps,
            })
        return validate_section({
            "aspect": unit, "status": "drafted", "reason": None,
            "structure": ids, "slots": slots, "sources": sources,
            "gaps": gaps, "profile": profile,
        }, unit)
    except CompanyDossierValidationError as exc:
        # Refused, not repaired: a reply that has to be tidied before it can be
        # read is not a reply.
        raise DossierDraftRefused(f"{unit}: {exc}") from exc


def draft_unit(
    model: Any,
    *,
    unit: str,
    structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]],
    company: Mapping[str, Any],
    mission: Mapping[str, Any],
    prior_body: str = "",
    profile: Mapping[str, Any] | None = None,
    profile_table: str = "",
    market_view_available: bool = True,
    classification: Any = None,
) -> dict[str, Any]:
    """One bounded call for one unit.  Returns the drafted block or a refusal."""

    prompt = build_unit_prompt(
        unit=unit, structure=structure, material=material, company=company,
        prior_body=prior_body, profile_table=profile_table,
        market_view_available=market_view_available, classification=classification,
    )
    request_id = content_hash({
        "unit": unit, "company": company.get("company_ref"),
        "prompt_sha": content_hash(prompt),
    })[:32]
    try:
        call = model.call(purpose=DRAFT_PURPOSE, request_id=request_id,
                          prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        return {"status": "unavailable", "unit": unit,
                "reason": f"{type(exc).__name__}: {exc}",
                "failure_trace": model_failure_trace(exc)}
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "request_id": request_id,
        "prompt_hash": content_hash(prompt),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
    }
    try:
        block = parse_unit_output(
            call["text"], unit=unit, structure=structure, material=material,
            market_view_available=market_view_available, profile=profile,
            classification=classification,
        )
    except DossierDraftInsufficientEvidence as exc:
        findings = [
            {"unit": unit, "code": "unsupported_slot",
             "slot_id": slot["slot_id"], "detail": slot["unknown"]}
            for slot in exc.slots
        ] + [
            {"unit": unit, "code": "missing_evidence", "detail": gap}
            for gap in exc.gaps
        ]
        return {"status": "insufficient_evidence", "unit": unit,
                "reason": str(exc), "repair_targets": findings,
                "model": provenance}
    except DossierDraftRefused as exc:
        return {"status": "refused", "unit": unit, "reason": str(exc),
                "model": provenance}
    return {"status": "drafted", "unit": unit, "block": block, "model": provenance,
            "prompt_bytes": len(prompt.encode("utf-8"))}


# ---------------------------------------------------------------------------
# the independent verifier (D2)
# ---------------------------------------------------------------------------


def draft_hash(blocks: Mapping[str, Any]) -> str:
    """What the verifier's verdict is about, by content."""

    return content_hash({key: blocks[key] for key in sorted(blocks)})


def build_verifier_prompt(blocks: Mapping[str, Any], *, company: Mapping[str, Any],
                          _analytical_contract: bool = True) -> str:
    """The verifier reads the draft and the rows it cites, and answers once."""

    lines = [
        "You are an independent verifier. Another model drafted parts of a company file",
        "from a fixed table of evidence. You do not rewrite it, improve it or grade it.",
        *(
            ["You answer one question: is every factual statement supported, and is every",
             "analytical inference a reasonable, explicitly qualified conclusion from its cited premises?"]
            if _analytical_contract else
            ["You answer one question: does every sentence stay inside the rows it cites?"]
        ),
        "",
        f"Company: {company.get('ticker') or ''} ({company.get('company_ref')})",
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"unit": "<part>",',
        '   "code": "' + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
        "",
    ]
    for unit in sorted(blocks):
        block = blocks[unit]
        lines.append(f"## {unit}")
        if unit == CLASSIFICATION_UNIT:
            lines.append(f"classification: {block['classification']}")
        for slot in block.get("slots") or []:
            if "unknown" in slot:
                lines.append(f"- {slot['slot_id']}: (unknown) {slot['unknown']}")
                continue
            for row in slot["sentences"]:
                lines.append(f"- {slot['slot_id']}: {row['text']}")
                for ref in row["refs"]:
                    source = next((item for item in block.get("sources") or []
                                   if item["ref"] == ref), None)
                    if source is not None:
                        # These are the exact closed source fields persisted in
                        # the draft.  The producer saw period as a separate
                        # material column; omitting it here made a date backed
                        # only by that column look invented to the verifier.
                        lines.append(
                            "    cites: "
                            f"kind={source['kind']} | "
                            f"period={source.get('period') or '-'} | "
                            f"text={source['text'][:MAX_ROW_CHARS]}"
                        )
        lines.append("")
    return "\n".join(lines)


def legacy_verifier_prompt_v02(blocks: Mapping[str, Any], *, company: Mapping[str, Any]) -> str:
    return build_verifier_prompt(blocks, company=company, _analytical_contract=False)


def validate_verifier_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DossierDraftRefused("the verifier did not return an object")
    if set(value) != {"verdict", "findings"}:
        raise DossierDraftRefused(
            f"the verifier returned keys {sorted(value)}; the contract is verdict and findings")
    verdict = value["verdict"]
    if verdict not in VERIFIER_VERDICTS:
        raise DossierDraftRefused(f"invalid verdict: {verdict!r}")
    rows = value["findings"] or []
    if not isinstance(rows, list):
        raise DossierDraftRefused("findings must be a list")
    findings = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"unit", "code", "detail"}:
            raise DossierDraftRefused("each finding must be exactly unit, code and detail")
        unit = str(row["unit"] or "").strip()
        if not unit:
            raise DossierDraftRefused("a finding's unit must be non-empty")
        if row["code"] not in VERIFIER_FINDING_CODES:
            raise DossierDraftRefused(f"unknown finding code: {row['code']!r}")
        detail = str(row["detail"] or "").strip()
        if not detail or len(detail) > 300 or "\n" in detail:
            raise DossierDraftRefused("a finding's detail is one short sentence")
        findings.append({"unit": unit, "code": str(row["code"]),
                         "detail": detail})
    if verdict == "pass" and findings:
        raise DossierDraftRefused("a pass verdict must have no findings")
    if verdict == "reject" and not findings:
        raise DossierDraftRefused("a reject verdict must have at least one finding")
    return {"verdict": verdict, "findings": findings}


def router_family_resolver(model_config: Mapping[str, Any]) -> Callable[[str], str | None]:
    """Read the family that actually served one route decision.

    The family is a fact about the route, not about what was asked for, so it
    is read back from the router rather than assumed from the configuration --
    the same thing the event lane does with its decisions.
    """

    def resolve(route_decision_ref: str | None) -> str | None:
        if not route_decision_ref:
            return None
        from .model_router import ModelRouter

        try:
            with ModelRouter(model_config["model_router_db"]) as router:
                decision = router.get_decision(route_decision_ref)
                profile_ref = decision.get("selected_profile_version_ref")
                if not profile_ref:
                    return None
                return router.get_profile(profile_ref).get("family")
        except Exception:  # noqa: BLE001 - unresolvable is not independent
            return None

    return resolve


def independence(
    *, draft_routes: Sequence[str | None], verifier_route: str | None,
    resolve: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """Whether the verifier was a different family from every drafting call.

    Fails closed.  A family that cannot be resolved is not a family that has
    been shown to differ, and a verification whose independence is unknown is
    worth exactly as much as no verification.
    """

    verifier_family = resolve(verifier_route)
    draft_families = [resolve(ref) for ref in draft_routes]
    if verifier_family is None:
        return {"independent": False, "reason": "the verifier's model family could "
                "not be resolved from its route decision",
                "verifier_family": None, "draft_families": draft_families}
    if not draft_families or any(family is None for family in draft_families):
        return {"independent": False, "reason": "a drafting call's model family could "
                "not be resolved from its route decision",
                "verifier_family": verifier_family, "draft_families": draft_families}
    clash = sorted({family for family in draft_families if family == verifier_family})
    if clash:
        return {"independent": False,
                "reason": f"the verifier ran on {verifier_family}, which also drafted",
                "verifier_family": verifier_family, "draft_families": draft_families}
    return {"independent": True, "reason": None, "verifier_family": verifier_family,
            "draft_families": draft_families}


def independence_precheck(
    *, draft_routes: Sequence[str | None], resolve: Callable[[str | None], str | None],
) -> dict[str, Any] | None:
    """Refuse a verification that could not have been independent, before paying.

    The family that served is a fact about the route decision, and the drafting
    routes are already recorded by the time this runs. If any of them cannot be
    resolved, no verdict from any second call could be shown to be independent,
    so the call is not worth making. Returning ``None`` means it is.
    """

    families = [resolve(ref) for ref in draft_routes]
    if not families or any(family is None for family in families):
        return {
            "independent": False,
            "reason": ("a drafting call's model family could not be resolved from "
                       "its route decision, so no verification could be shown to "
                       "be independent"),
            "verifier_family": None, "draft_families": families,
        }
    return None


def verify(
    model: Any,
    blocks: Mapping[str, Any],
    *,
    company: Mapping[str, Any],
    mission: Mapping[str, Any],
    producer_route_decision_refs: Sequence[str | None] = (),
) -> dict[str, Any]:
    """A second, separate call that returns only a verdict on the draft."""

    if not blocks:
        return {"status": "skipped", "reason": "nothing was drafted"}
    digest = draft_hash(blocks)
    prompt = build_verifier_prompt(blocks, company=company)
    try:
        call = independent_model_call(
            model, producer_route_decision_refs=producer_route_decision_refs,
            purpose=VERIFIER_PURPOSE,
            request_id=(f"verify-{digest[:24]}-"
                        f"{verifier_prompt_contract_fingerprint()[:16]}"),
            prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}",
                "failure_trace": model_failure_trace(exc)}
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "request_id": (f"verify-{digest[:24]}-"
                       f"{verifier_prompt_contract_fingerprint()[:16]}"),
        "prompt_hash": content_hash(prompt),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
    }
    try:
        validated = validate_verifier_output(unwrap_json_object(call["text"]))
    except DossierDraftRefused as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance}
    return {
        "status": "verified",
        # Bound to what was verified: a verdict that does not name the draft it
        # read is a verdict about nothing.
        "verified_draft_hash": digest,
        **validated,
        "model": provenance,
    }


def summarise_blocks(blocks: Mapping[str, Any]) -> dict[str, Any]:
    """How much prose and how many refs the draft carries, for the summary."""

    sentences = refs = 0
    for block in blocks.values():
        for slot in block.get("slots") or []:
            sentences += len(slot.get("sentences") or ())
        refs += len(block.get("sources") or ())
    return {"units": len(blocks), "sentences": sentences, "cited_sources": refs}


def rendered_bodies(blocks: Mapping[str, Any]) -> dict[str, str]:
    return {unit: section_body(block) for unit, block in blocks.items()}


__all__ = [
    "DRAFT_CONTRACT_VERSION",
    "DRAFT_PURPOSE",
    "VERIFIER_PURPOSE",
    "MAX_CLAIM_ROWS",
    "MAX_COST_USD",
    "MAX_INPUT_TOKENS",
    "MAX_NUMBER_ROWS",
    "MAX_OUTPUT_TOKENS",
    "MAX_RUN_COST_USD",
    "MAX_UNITS_PER_RUN",
    "MARKET_VIEW_GRADES",
    "MODEL_CONFIG_NAME",
    "TIMEOUT_SECONDS",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "DossierDraftError",
    "DossierDraftInsufficientEvidence",
    "DossierDraftRefused",
    "TEMPLATE_UNITS",
    "build_unit_prompt",
    "build_verifier_prompt",
    "draft_hash",
    "draft_contract_fingerprint",
    "verifier_prompt_contract_fingerprint",
    "draft_unit",
    "independence",
    "independence_precheck",
    "material_rows",
    "parse_unit_output",
    "render_material",
    "rendered_bodies",
    "router_family_resolver",
    "summarise_blocks",
    "validate_verifier_output",
    "verify",
]
