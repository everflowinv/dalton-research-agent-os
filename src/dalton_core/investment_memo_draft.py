"""Four bounded drafts and one independent verification for an Investment Memo."""

from __future__ import annotations

from .final_text_contract import final_text_instructions

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .cockpit_model import CockpitModelError, independent_model_call, register_purpose, unwrap_json_object
from .call_budget import default_call_budget, default_run_budget
from .investment_memo_contract import ADV_UNAVAILABLE_LINE, CHECK_REFS, RECOMMENDATION_FORMAT
from .product_contract import GAP_RULES, NUMBER_SOURCE_RULES
from .research_reasoning_lint import VERIFIER_LINT_INSTRUCTIONS
from .store import content_hash

DRAFT_PURPOSE = register_purpose("investment_memo")
VERIFIER_PURPOSE = register_purpose("investment_memo_verifier")
MEMO_PROMPT_CONTRACT_VERSION = "investment-memo-prompt:0.6"
MEMO_VERIFIER_PROMPT_CONTRACT_VERSION = "investment-memo-verifier-prompt:0.3"
_CALL_BUDGET = default_call_budget(DRAFT_PURPOSE)
MAX_INPUT_TOKENS = _CALL_BUDGET["max_input_tokens"]
MAX_OUTPUT_TOKENS = _CALL_BUDGET["max_output_tokens"]
MAX_COST_USD = _CALL_BUDGET["max_cost_usd"]
TIMEOUT_SECONDS = _CALL_BUDGET["timeout_seconds"]
MAX_RUN_COST_USD = default_run_budget(DRAFT_PURPOSE)["max_cost_usd"]
MAX_UNITS = 4
# One paid redo per memo run, for a group whose draft broke the closed contract.
MAX_REPAIRS = 1

GROUPS = (
    ("identity_background", (0, 1, 2, 3), (0, 1, 2)),
    ("view", (4, 5), (3, 4, 5)),
    ("economics", (6, 7, 8, 9), (6, 7, 8)),
    ("monitoring", (10, 11), (9, 10, 11)),
)
FINDING_CODES = (
    "unsupported_statement", "number_not_in_source", "missing_required_section",
    "question_not_answered", "variant_not_compared", "anti_thesis_incomplete",
    "risk_reward_incomplete",
    # Batch D: the recommendation the Key Information title asks for, the
    # shared reasoning lint, and 「材料缺少」 without a MaterialRequest.
    "recommendation_missing", "reasoning_lint", "material_missing_unbound",
)


class InvestmentMemoDraftError(ValueError):
    pass


class InvestmentMemoSourceError(InvestmentMemoDraftError):
    """Acquired-original checks failed; another paid draft cannot repair them."""


class InvestmentMemoCitationError(InvestmentMemoDraftError):
    """A draft cited a ref in a place the memo contract does not let it stand.

    ``refs`` names every offending ref, so the refusal reason (and the repair
    prompt) says exactly which citation was wrong instead of "outside the
    frozen input" for a ref that was in the frozen input all along.
    """

    def __init__(self, message: str, refs: Sequence[str] = (), *, findings=()) -> None:
        super().__init__(message)
        self.refs = list(dict.fromkeys(str(ref) for ref in refs))
        self.findings = list(findings)
        self.diagnostic_sections = []


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Kinds whose rows are computed cells a figure cites as ``numbers[].cell``
# (``mission_deliverable.CELL_SOURCE_KINDS``), with the version they live in.
CELL_KINDS = ("forecast_cell", "valuation_metric", "statement_accession")
# Batch D: rows whose own text is a citable figure source.  ``market_block``
# is the price/market-cap/30-day-ADV block (``market_data_block``) and
# ``pack_number`` an ``[N…]`` number of the material pack -- both cited as
# ``numbers[].number_ref`` and re-checked at publish against the row text;
# ``pack_passage`` is a ``[P…]`` passage, cited as ``numbers[].passage_ref``
# with a verbatim quote; ``pack_ours`` is our own model (``[O…]``), cited as
# ``numbers[].estimate`` and written as 「我们的估计」.
NUMBER_ROW_KINDS = ("market_block", "pack_number")
PASSAGE_ROW_KINDS = ("pack_passage",)
OURS_ROW_KINDS = ("pack_ours",)


def citation_index(material: Sequence[Mapping[str, Any]] = (), *,
                   allowed_refs: set[str] | None = None) -> dict[str, Any]:
    """Which frozen refs may stand in which slot of a memo section.

    MissionDeliverable admits only live Claims in ``claim_refs`` and
    ``numbers[].claim_version_ref``, and only a resolvable computed cell in
    ``numbers[].cell``.  Every other frozen row -- the dossier, the approved
    gate, the framework, the valuation snapshot, events -- is context the
    drafter reads and a key question may cite, but a section may not.
    """

    claims: set[str] = set()
    cells: dict[str, dict[str, str]] = {}
    kinds: dict[str, str] = {}
    numbers: dict[str, str] = {}
    passages: dict[str, str] = {}
    ours: dict[str, str] = {}
    for row in material:
        ref, kind = str(row.get("ref")), str(row.get("kind"))
        kinds[ref] = kind
        if kind == "claim":
            claims.add(ref)
        elif kind in NUMBER_ROW_KINDS:
            numbers[ref] = str(row.get("text") or "")
        elif kind in PASSAGE_ROW_KINDS:
            passages[ref] = str(row.get("text") or "")
        elif kind in OURS_ROW_KINDS:
            ours[ref] = str(row.get("text") or "")
        elif kind in CELL_KINDS and row.get("version_ref"):
            key = "accession" if kind == "statement_accession" else "version_ref"
            cells[ref] = {"kind": kind, "ref": ref, key: str(row["version_ref"])}
    for ref in allowed_refs or ():
        if ref in kinds:
            continue
        kinds[ref] = "claim" if ref.startswith("claim-version:") else "context"
        if kinds[ref] == "claim":
            claims.add(ref)
    citable = claims | set(cells) | set(numbers) | set(passages) | set(ours)
    return {"claims": claims, "cells": cells, "kinds": kinds, "numbers": numbers,
            "passages": passages, "ours": ours,
            "context": sorted(ref for ref in kinds if ref not in citable)}


def build_group_prompt(*, group: str, section_titles: Sequence[str], questions: Sequence[Mapping[str, str]],
                       material: Sequence[Mapping[str, Any]], company: Mapping[str, Any],
                       repair: Mapping[str, Any] | None = None) -> str:
    index = citation_index(material)
    claim_refs = sorted(index["claims"])
    cells = [index["cells"][ref] for ref in sorted(index["cells"])]
    contract = {
        "sections": [{"title": title, "body": "text", "claim_refs": ["claim ref"],
                      "numbers": [{"text": "figure exactly as cited", "claim_version_ref": "claim ref", "period": "period"},
                                  {"text": "figure exactly as the cell carries it", "cell": {"kind": "cell kind", "ref": "cell ref", "version_ref": "cell version_ref"}, "period": "period"},
                                  {"text": "the number row's text", "number_ref": "market_block / pack_number ref", "period": "period"},
                                  {"text": "verbatim quote containing the figure", "passage_ref": "pack_passage ref", "period": "period"},
                                  {"text": "text stating the result", "derivation": {"expression": "17.0*5.00", "result": "85.0"}, "period": "period"},
                                  {"text": "我们的估计 …", "estimate": {"assumption_ref": "pack_ours ref or assumption:…"}, "period": "period"}],
                      "gaps": [],
                      "gap_items": [{"kind": "company_not_disclosed|not_acquired", "need": "what is missing",
                                     "request_ref": "material-request:… (not_acquired)", "tried": "已尝试：…"}]}
                     for title in section_titles],
        "key_questions": [{"question_ref": q["question_ref"], "question": q["question"],
                           "answer": "answer", "refs": ["any allowed ref"], "unknown": False,
                           "falsifier": "what would disprove the answer"} for q in questions],
    }
    lines = [
        "Draft only the requested Investment Memo sections and Playbook key questions.",
        *final_text_instructions(),
        "Use only the complete frozen evidence rows below. Never use model memory.",
        "Every factual sentence and every number must cite an allowed ref. Evidence-backed",
        "inference is allowed only when its cited rows contain the premises; label the inference",
        "and uncertainty. Do not call vendor/sell-side consensus buy-side consensus, and do not",
        "invent holding-period return arithmetic absent a frozen return-bridge authority.",
        "For each judgement choose the current preferred case; give the alternative trigger,",
        "operating/earnings/valuation impact where supported, falsifier, and next tracking item.",
        "Do not evade the decision with unranked A/B possibilities.",
        "The memo makes a recommendation (owner decision 2026-10-09). When a requested section is",
        "Key Information, write it in exactly this labelled form and keep it a research",
        f"recommendation: {RECOMMENDATION_FORMAT}. Give the reason in one sentence.",
        "If that title asks for 30 日 ADV, write it from a market_block/pack_number row (cite it as",
        f"number_ref); if no row carries it, write 「{ADV_UNAVAILABLE_LINE}」 (the window in words:",
        "a bare 30 with no cited row is an unsourced figure) and add a",
        "not_acquired gap_item that names a MaterialRequest (material-request:… or MR-…).",
        *NUMBER_SOURCE_RULES, *GAP_RULES,
        "Reasoning rules (a deterministic lint and the verifier reject the memo on any of these):",
        *VERIFIER_LINT_INSTRUCTIONS,
        "Every section must have substance: an empty, heading-only or gap-only section refuses the memo.",
        'For an unanswered key question use unknown=true, answer="", refs=[].',
        "Put the exact missing document, metric, period or next research action in falsifier.",
        "If the evidence answers the requested question, use unknown=false,",
        "state only the supported conclusion in answer, cite its refs, and explain its limits",
        "and the evidence that could change it in falsifier. Never pair unknown=true with an answer or refs.",
        "Partial background that does not answer the question belongs in the relevant section,",
        "with its citations and gaps; keep that key question unknown until its core is answered.",
        "Citation slots are closed; a ref in the wrong slot refuses the whole draft:",
        "- sections[].claim_refs and numbers[].claim_version_ref may name ONLY Claim refs.",
        "- A figure from the company model or valuation snapshot is a numbers[] entry with",
        "  \"cell\" copied exactly from Computed cells (and no claim_version_ref).",
        "- A figure from a Number row is numbers[].number_ref (text = the row text); from a",
        "  Passage row numbers[].passage_ref with a verbatim quote; our model (Ours rows) is",
        "  numbers[].estimate and the sentence says 我们的估计; arithmetic is numbers[].derivation.",
        "- Context refs (dossier, gate, framework, snapshot, calendar, price series, events,",
        "  judgements) may be read and cited in key_questions[].refs only, never in a section.",
        "- Every figure in a section body needs its own numbers[] entry; a figure that only a",
        "  context row carries goes into gaps as the authority still missing, not into the body.",
        f"Company: {_json(company)}", f"Group: {group}",
        f"Claim refs: {_json(claim_refs)}",
        f"Computed cells: {_json(cells)}",
        f"Number rows: {_json(sorted(index['numbers']))}",
        f"Passage rows: {_json(sorted(index['passages']))}",
        f"Ours rows: {_json(sorted(index['ours']))}",
        f"Context refs: {_json(index['context'])}",
        "Evidence rows:", _json(list(material)),
        "Return raw JSON only with exactly this shape and the exact requested titles/questions:",
        _json(contract),
    ]
    if repair:
        lines += [
            "Your previous answer for this group was refused and was not used:",
            str(repair.get("reason") or ""),
            f"Offending refs: {_json(list(repair.get('refs') or []))}",
            "Return the complete group again with every citation in its allowed slot.",
        ]
    return "\n".join(lines)


_NUMBER_KEYS = {"text", "claim_version_ref", "period", "cell", "number_ref", "passage_ref",
                "derivation", "estimate", "metric", "unit"}


def _number(number: Mapping[str, Any], *, title: str, index: Mapping[str, Any]) -> dict[str, Any]:
    if set(number) - _NUMBER_KEYS or "text" not in number:
        raise InvestmentMemoDraftError(f"section「{title}」has a number with an open shape")
    claims, cells, kinds = index["claims"], index["cells"], index["kinds"]
    claim_ref, cell = number.get("claim_version_ref"), number.get("cell")
    extra = {key: number[key] for key in ("metric", "unit") if key in number}
    sources = [key for key in ("claim_version_ref", "cell", "number_ref", "passage_ref",
                               "derivation", "estimate") if number.get(key) is not None]
    if len(sources) > 1 and set(sources) != {"claim_version_ref", "cell"}:
        raise InvestmentMemoCitationError(
            f"section「{title}」number cites more than one source: {sources}", [])
    base = {key: number[key] for key in ("text", "period") if key in number}
    if number.get("derivation") is not None:
        derivation = number["derivation"]
        if not isinstance(derivation, Mapping):
            raise InvestmentMemoDraftError("a section number derivation must be an object")
        return {**base, **extra, "derivation": {"expression": derivation.get("expression"),
                                                "result": derivation.get("result")}}
    if number.get("number_ref") is not None:
        ref = str(number["number_ref"])
        if ref not in index["numbers"]:
            raise _misplaced(ref, f"section「{title}」number number_ref", kinds)
        # The row's own text is what the authority re-checks the figure against.
        return {**base, **extra, "number_ref": ref}
    if number.get("passage_ref") is not None:
        ref = str(number["passage_ref"])
        if ref not in index["passages"]:
            raise _misplaced(ref, f"section「{title}」number passage_ref", kinds)
        return {**base, **extra, "passage_ref": ref}
    if number.get("estimate") is not None:
        estimate = number["estimate"]
        ref = str(estimate.get("assumption_ref") if isinstance(estimate, Mapping) else estimate)
        if ref not in index["ours"] and not ref.startswith("assumption:"):
            raise _misplaced(ref, f"section「{title}」number estimate", kinds)
        return {**base, **extra, "estimate": {"assumption_ref": ref}}
    if cell is not None and not isinstance(cell, Mapping):
        raise InvestmentMemoDraftError("a section number cell must be an object")
    out = {**base, **extra}
    if claim_ref is not None and cell is not None:
        raise InvestmentMemoCitationError(
            f"section「{title}」number cites both a Claim and a cell: {claim_ref}", [str(claim_ref)])
    if claim_ref is not None:
        ref = str(claim_ref)
        if ref in claims:
            return {**out, "claim_version_ref": ref}
        if ref in cells:
            # The model named a computed cell in the Claim slot; the frozen
            # material says exactly which cell that is, so cite it as one.
            return {**out, "cell": dict(cells[ref])}
        raise _misplaced(ref, f"section「{title}」number claim_version_ref", kinds)
    if cell is not None:
        ref = str(cell.get("ref"))
        if ref in cells:
            return {**out, "cell": dict(cells[ref])}
        raise _misplaced(ref, f"section「{title}」number cell", kinds)
    raise InvestmentMemoCitationError(f"section「{title}」has a number with no citation")


def _misplaced(ref: str, slot: str, kinds: Mapping[str, str]) -> InvestmentMemoCitationError:
    # The ref leads the message: hold reasons are cut to a couple of hundred
    # characters, and the ref is the one thing the reader must not lose.
    if ref in kinds:
        return InvestmentMemoCitationError(
            f"{ref} is a frozen {kinds[ref]} row cited in {slot}, where only Claims "
            "or computed cells may stand", [ref])
    return InvestmentMemoCitationError(
        f"{ref} cited in {slot} is not in the frozen material", [ref])


def _short(title: str) -> str:
    return title if len(title) <= 24 else title[:24] + "…"


def parse_group_output(value: Any, *, section_titles: Sequence[str],
                       questions: Sequence[Mapping[str, str]], allowed_refs: set[str],
                       material: Sequence[Mapping[str, Any]] = (),
                       original_passages: Mapping[str, str] | None = None) -> dict[str, Any]:
    index = citation_index(material, allowed_refs=allowed_refs)
    allowed = set(index["kinds"])
    if not isinstance(value, Mapping) or set(value) != {"sections", "key_questions"}:
        raise InvestmentMemoDraftError("draft output must contain exactly sections and key_questions")
    sections = value["sections"]
    if not isinstance(sections, list) or [s.get("title") for s in sections if isinstance(s, Mapping)] != list(section_titles):
        raise InvestmentMemoDraftError("draft returned the wrong section titles or order")
    checked_sections = []
    problems: list[tuple[str, list[str]]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            raise InvestmentMemoDraftError("each section must be an object")
        if not {"title", "body", "claim_refs", "numbers", "gaps"} <= set(section) \
                or set(section) - {"title", "body", "claim_refs", "numbers", "gaps", "gap_items"}:
            raise InvestmentMemoDraftError("a section has an open or incomplete shape")
        if not isinstance(section.get("gap_items", []), list):
            raise InvestmentMemoDraftError("section gap_items must be a list")
        if not isinstance(section["body"], str):
            raise InvestmentMemoDraftError("section body must be text")
        title = _short(str(section["title"]))
        refs = section["claim_refs"]
        if not isinstance(refs, list):
            raise InvestmentMemoDraftError("section claim_refs must be a list")
        for ref in refs:
            if ref not in index["claims"]:
                error = _misplaced(str(ref), f"section「{title}」claim_refs", index["kinds"])
                problems.append((str(error), error.refs))
        if not isinstance(section["numbers"], list) or not isinstance(section["gaps"], list):
            raise InvestmentMemoDraftError("section numbers and gaps must be lists")
        if any(not isinstance(gap, str) for gap in section["gaps"]):
            raise InvestmentMemoDraftError("section gaps must be text")
        numbers = []
        sources = []
        for number in section["numbers"]:
            if not isinstance(number, Mapping):
                raise InvestmentMemoDraftError("each section number must be an object")
            try:
                checked_number = _number(number, title=title, index=index)
                text = checked_number.get("text")
                ref = checked_number.get("passage_ref")
                from .mission_deliverable import value_tokens
                if ref and isinstance(text, str) and text.strip() and not value_tokens(text, include_small=True):
                    original = (original_passages or {}).get(ref)
                    if original_passages is None:
                        # Standalone parsing only: a rendered header/trimmed pack
                        # cannot stand in for its original. Production supplies
                        # acquired-original proofs for every qualitative quote.
                        import hashlib
                        from .evidence_index import parse_passage_ref
                        passage = index["passages"][ref]
                        if hashlib.sha256(passage.encode()).hexdigest()[:12] == parse_passage_ref(ref)["hash12"]:
                            original = passage
                    if not isinstance(original, str) or text not in original:
                        raise InvestmentMemoCitationError(
                            f"section「{title}」qualitative quote is not verbatim in its acquired original: {ref}", [ref])
                    if {"ref": ref} not in sources:
                        sources.append({"ref": ref})
                    continue
                numbers.append(checked_number)
            except InvestmentMemoCitationError as exc:
                problems.append((str(exc), exc.refs))
        checked = {**dict(section)}
        if not checked.get("gap_items"):
            checked.pop("gap_items", None)
        if sources:
            checked["source_refs"] = sources
        checked_sections.append({**checked, "body": str(section["body"]).strip(),
                                 "claim_refs": list(dict.fromkeys(str(ref) for ref in refs)),
                                 "numbers": numbers,
                                 "gaps": [str(gap).strip() for gap in section["gaps"]]})
    expected = [q["question_ref"] for q in questions]
    answers = value["key_questions"]
    if not isinstance(answers, list) or [q.get("question_ref") for q in answers if isinstance(q, Mapping)] != expected:
        raise InvestmentMemoDraftError("draft returned the wrong key questions or order")
    checked_answers = []
    for answer, expected_question in zip(answers, questions):
        if not isinstance(answer, Mapping):
            raise InvestmentMemoDraftError("each key question answer must be an object")
        if set(answer) != {"question_ref", "question", "answer", "refs", "unknown", "falsifier"}:
            raise InvestmentMemoDraftError("a key question has an open or incomplete shape")
        if not isinstance(answer["answer"], str) or not isinstance(answer["falsifier"], str):
            raise InvestmentMemoDraftError("answer and falsifier must be text")
        if answer["question"] != expected_question["question"]:
            raise InvestmentMemoDraftError("a key question's text drifted")
        if not isinstance(answer["refs"], list):
            raise InvestmentMemoDraftError("key question refs must be a list")
        unknown_refs = [str(ref) for ref in answer["refs"] if ref not in allowed]
        if unknown_refs:
            problems.append((f"{', '.join(unknown_refs[:3])} cited in key question "
                             f"{answer['question_ref']} is not in the frozen material",
                             unknown_refs))
        if answer["unknown"] is True:
            if answer["answer"] or answer["refs"]:
                raise InvestmentMemoDraftError("an unknown answer cannot assert or cite an answer")
        elif answer["unknown"] is not False or not answer["answer"] or not answer["refs"] or not answer["falsifier"]:
            raise InvestmentMemoDraftError("an answered key question needs answer, refs and falsifier")
        checked_answers.append({**dict(answer), "answer": str(answer["answer"]).strip(),
                                "falsifier": str(answer["falsifier"]).strip(),
                                "refs": list(dict.fromkeys(answer["refs"]))})
    if problems:
        refs = [ref for _, found in problems for ref in found]
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        error = InvestmentMemoCitationError(problems[0][0] + more, refs,
            findings=[{"code":"citation_slot", "detail":detail, "refs":list(refs)}
                      for detail, refs in problems])
        # Already parsed sections are diagnostic only. They are never accepted
        # or published when any citation fails, but remaining section arithmetic
        # must also reach the one bounded repair instead of being hidden.
        error.diagnostic_sections = checked_sections
        raise error
    return {"sections": checked_sections, "key_questions": checked_answers}


def draft_group(model: Any, *, group: str, section_titles: Sequence[str],
                questions: Sequence[Mapping[str, str]], material: Sequence[Mapping[str, Any]],
                company: Mapping[str, Any], mission: Mapping[str, Any],
                repair: Mapping[str, Any] | None = None, quiesce: Any = None,
                maintenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One paid draft of one group, or -- with ``repair`` -- its one paid redo.

    ``repair`` carries the refused draft's reason and offending refs; the
    redo is the same prompt plus that reason, so the model is told exactly
    which citation to move or drop rather than guessing again.
    """

    prompt = build_group_prompt(group=group, section_titles=section_titles, questions=questions,
                                material=material, company=company, repair=repair)
    if maintenance is not None:
        prompt = maintenance["prompt"]
    request_id = "memo-" + content_hash({"contract": MEMO_PROMPT_CONTRACT_VERSION, "group": group, "prompt": prompt})[:24]
    call: Mapping[str, Any] = {}
    try:
        if quiesce is not None:
            quiesce.checkpoint()
        call = model.call(purpose=DRAFT_PURPOSE, request_id=request_id, prompt=prompt, mission=mission)
        if quiesce is not None:
            quiesce.returned(model, call, mission, purpose=DRAFT_PURPOSE)
        return parse_group_call(model, call, group=group, section_titles=section_titles,
                                questions=questions, material=material)
    except (CockpitModelError, KeyError, json.JSONDecodeError) as exc:
        return {"status": "refused", "reason": f"{type(exc).__name__}: {exc}",
                "cost_micros": int(call.get("cost_micros") or 0)}


def parse_group_call(model, call, *, group, section_titles, questions, material):
    """Validate a durable answer without dispatch; retain its exact paid identity.

    Typed content diagnostics authorize no retry themselves. Source failures
    are distinct, and the maintenance authority re-runs this whole validator.
    """
    metadata = {key:call.get(key) for key in (
        "work_order_ref", "route_decision_ref", "result_envelope_ref", "invocation_ref")}
    metadata["cost_micros"] = int(call.get("cost_micros") or 0)
    evidence = {"model":metadata, "raw_response":call.get("text")}
    try:
        value = unwrap_json_object(call["text"])
        from .mission_deliverable import value_tokens
        index = citation_index(material)
        candidate_sections = value.get("sections") if isinstance(value, Mapping) else None
        qualitative_refs = {str(number["passage_ref"])
            for section in (candidate_sections if isinstance(candidate_sections, list) else [])
            if isinstance(section, Mapping) and isinstance(section.get("numbers"), list)
            for number in section["numbers"] if isinstance(number, Mapping)
            and isinstance(number.get("passage_ref"), str) and number["passage_ref"] in index["passages"]
            and isinstance(number.get("text"), str) and number["text"].strip()
            and not value_tokens(number["text"], include_small=True)}
        originals = None
        if qualitative_refs:
            from pathlib import Path
            from .initial_screen_failure_feedback import _checked_originals
            scheduler_db = getattr(model, "scheduler_db", None)
            if scheduler_db and str(scheduler_db) != ":memory:":
                try:
                    proofs = _checked_originals(Path(scheduler_db).parent, qualitative_refs)
                    originals = {ref: proofs[ref]["text"] for ref in qualitative_refs}
                except (ValueError, OSError, KeyError) as exc:
                    raise InvestmentMemoSourceError("qualitative quotation original could not be verified") from exc
            else:
                raise InvestmentMemoSourceError("qualitative quotation needs its acquired original")
        parsed = parse_group_output(value, section_titles=section_titles,
                                    questions=questions, allowed_refs={str(row["ref"]) for row in material},
                                    material=material, original_passages=originals)
    except InvestmentMemoSourceError as exc:
        return {**evidence, "status":"refused", "reason":f"{type(exc).__name__}: {exc}",
                "refs":[], "repairable":False, "failure_class":"source_unavailable",
                "cost_micros":metadata["cost_micros"]}
    except InvestmentMemoCitationError as exc:
        return {**evidence, "status":"refused", "reason":f"{type(exc).__name__}: {exc}",
                "refs":list(exc.refs), "repairable":True, "failure_class":"content",
                "findings":exc.findings or [{"code":"citation_slot","detail":str(exc),"refs":list(exc.refs)}],
                "diagnostic_sections":exc.diagnostic_sections,
                "cost_micros":metadata["cost_micros"]}
    except (InvestmentMemoDraftError, json.JSONDecodeError) as exc:
        return {**evidence, "status":"refused", "reason":f"{type(exc).__name__}: {exc}",
                "refs":[], "repairable":True, "failure_class":"content",
                "findings":[{"code":"group_shape", "detail":str(exc)}],
                "cost_micros":metadata["cost_micros"]}
    except KeyError as exc:
        return {**evidence, "status":"refused", "reason":f"{type(exc).__name__}: {exc}",
                "repairable":False, "failure_class":"authority_unavailable",
                "cost_micros":metadata["cost_micros"]}
    return {**evidence, "status":"drafted", **parsed, "group":group}


def verified_material_hash(*, sections: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]],
                           input_bindings: Sequence[Mapping[str, Any]], record_fields: Mapping[str, Any]) -> str:
    return content_hash({**dict(record_fields), "sections": list(sections),
                         "key_questions": list(questions), "input_bindings": list(input_bindings)})


def build_verifier_prompt(*, sections: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]],
                          material: Sequence[Mapping[str, Any]], material_hash: str) -> str:
    contract = {"verdict": "pass|reject", "verified_body_hash": material_hash,
                "finding_codes": ["one or more closed codes"]}
    return "\n".join([
        "Independently verify this complete Investment Memo against every cited frozen row.",
        "Do not rewrite it. Permit an explicitly labelled analytical inference only when its",
        "cited evidence contains the premises and the uncertainty is stated. Reject unsupported",
        "prose/numbers, claims of buy-side consensus without that authority, invented return",
        "arithmetic, or an answered judgement that evades the requested current case with",
        "unranked alternatives. Reject missing sections, unknown key",
        "questions, absent variant-vs-consensus, incomplete Anti-thesis, or incomplete risk/reward.",
        "The memo must make a labelled research recommendation in Key Information:",
        f"{RECOMMENDATION_FORMAT}. A recommendation is required, not forbidden; reject",
        "(recommendation_missing) when direction, position range or horizon is absent (pass needs",
        "only the direction). Number sources:", *NUMBER_SOURCE_RULES, *GAP_RULES,
        "Reject with reasoning_lint when any of these reasoning rules is broken:",
        *VERIFIER_LINT_INSTRUCTIONS,
        "Reject with material_missing_unbound when 「材料缺少」 is not bound to a MaterialRequest.",
        "A pass has finding_codes=[]. Return raw JSON only:", _json(contract),
        "Complete memo sections:", _json(list(sections)), "All 12 key questions:", _json(list(questions)),
        "Complete cited evidence rows:", _json(list(material)),
    ])


def verify_memo(model: Any, *, sections: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]],
                material: Sequence[Mapping[str, Any]], material_hash: str,
                mission: Mapping[str, Any], producer_route_decision_refs: Sequence[str],
                quiesce: Any = None) -> dict[str, Any]:
    prompt = build_verifier_prompt(sections=sections, questions=questions, material=material,
                                   material_hash=material_hash)
    call: Mapping[str, Any] = {}
    try:
        if quiesce is not None:
            quiesce.checkpoint()
        call = independent_model_call(model, producer_route_decision_refs=producer_route_decision_refs,
            purpose=VERIFIER_PURPOSE, request_id="memo-verify-" + material_hash[:24] + "-" + content_hash(MEMO_VERIFIER_PROMPT_CONTRACT_VERSION)[:8], prompt=prompt, mission=mission)
        if quiesce is not None:
            quiesce.returned(model, call, mission, purpose=VERIFIER_PURPOSE)
        value = unwrap_json_object(call["text"])
    except (CockpitModelError, KeyError, json.JSONDecodeError) as exc:
        return {"status": "refused", "reason": f"{type(exc).__name__}: {exc}",
                "cost_micros": int(call.get("cost_micros") or 0)}
    if not isinstance(value, Mapping) or set(value) != {"verdict", "verified_body_hash", "finding_codes"}:
        return {"status": "refused", "reason": "verifier output has an invalid closed shape",
                "cost_micros": int(call.get("cost_micros") or 0)}
    codes = value["finding_codes"]
    if value["verified_body_hash"] != material_hash or value["verdict"] not in ("pass", "reject") \
            or not isinstance(codes, list) or any(code not in FINDING_CODES for code in codes) \
            or len(codes) != len(set(codes)) \
            or (value["verdict"] == "pass") != (codes == []):
        return {"status": "refused", "reason": "verifier verdict is inconsistent or unbound",
                "cost_micros": int(call.get("cost_micros") or 0)}
    return {"status": "verified", "verdict": value["verdict"], "verified_body_hash": material_hash,
            "finding_codes": list(codes), "model": {"work_order_ref": call.get("work_order_ref"),
            "route_decision_ref": call.get("route_decision_ref"),
            "result_envelope_ref": call.get("result_envelope_ref"),
            "invocation_ref": call.get("invocation_ref"),
            "cost_micros": int(call.get("cost_micros") or 0)}}


__all__ = ["DRAFT_PURPOSE", "VERIFIER_PURPOSE", "GROUPS", "FINDING_CODES", "CELL_KINDS",
           "NUMBER_ROW_KINDS", "PASSAGE_ROW_KINDS", "OURS_ROW_KINDS",
           "InvestmentMemoDraftError", "InvestmentMemoCitationError", "citation_index", "build_group_prompt", "parse_group_output", "draft_group",
           "verified_material_hash", "build_verifier_prompt", "verify_memo"]
