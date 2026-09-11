"""Four bounded drafts and one independent verification for an Investment Memo."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .cockpit_model import CockpitModelError, independent_model_call, register_purpose, unwrap_json_object
from .investment_memo_contract import CHECK_REFS
from .store import content_hash

DRAFT_PURPOSE = register_purpose("investment_memo")
VERIFIER_PURPOSE = register_purpose("investment_memo_verifier")
MEMO_PROMPT_CONTRACT_VERSION = "investment-memo-prompt:0.2"
MEMO_VERIFIER_PROMPT_CONTRACT_VERSION = "investment-memo-verifier-prompt:0.2"
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 6_000
MAX_COST_USD = 1.0
TIMEOUT_SECONDS = 300
MAX_RUN_COST_USD = 5.0
MAX_UNITS = 4

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
)


class InvestmentMemoDraftError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_group_prompt(*, group: str, section_titles: Sequence[str], questions: Sequence[Mapping[str, str]],
                       material: Sequence[Mapping[str, Any]], company: Mapping[str, Any]) -> str:
    allowed_refs = [str(row["ref"]) for row in material]
    contract = {
        "sections": [{"title": title, "body": "text", "claim_refs": ["allowed ref"],
                      "numbers": [{"text": "figure exactly as cited", "claim_version_ref": "allowed claim ref", "period": "period"}],
                      "gaps": []} for title in section_titles],
        "key_questions": [{"question_ref": q["question_ref"], "question": q["question"],
                           "answer": "answer", "refs": ["allowed ref"], "unknown": False,
                           "falsifier": "what would disprove the answer"} for q in questions],
    }
    return "\n".join([
        "Draft only the requested Investment Memo sections and Playbook key questions.",
        "Use only the complete frozen evidence rows below. Never use model memory.",
        "Every factual sentence and every number must cite an allowed ref. Evidence-backed",
        "inference is allowed only when its cited rows contain the premises; label the inference",
        "and uncertainty. Do not call vendor/sell-side consensus buy-side consensus, and do not",
        "invent holding-period return arithmetic absent a frozen return-bridge authority.",
        "For each judgement choose the current preferred case; give the alternative trigger,",
        "operating/earnings/valuation impact where supported, falsifier, and next tracking item.",
        "Do not evade the decision with unranked A/B possibilities. This does not authorize a",
        "recommendation or return calculation that the frozen inputs and human gate do not support.",
        "If evidence is missing, leave body/answer empty, set unknown true for a question,",
        "and name the exact document, metric, period, comparison, or research action needed.",
        f"Company: {_json(company)}", f"Group: {group}",
        f"Allowed refs: {_json(allowed_refs)}", "Evidence rows:", _json(list(material)),
        "Return raw JSON only with exactly this shape and the exact requested titles/questions:",
        _json(contract),
    ])


def parse_group_output(value: Any, *, section_titles: Sequence[str],
                       questions: Sequence[Mapping[str, str]], allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"sections", "key_questions"}:
        raise InvestmentMemoDraftError("draft output must contain exactly sections and key_questions")
    sections = value["sections"]
    if not isinstance(sections, list) or [s.get("title") for s in sections if isinstance(s, Mapping)] != list(section_titles):
        raise InvestmentMemoDraftError("draft returned the wrong section titles or order")
    checked_sections = []
    for section in sections:
        if not isinstance(section, Mapping):
            raise InvestmentMemoDraftError("each section must be an object")
        if set(section) != {"title", "body", "claim_refs", "numbers", "gaps"}:
            raise InvestmentMemoDraftError("a section has an open or incomplete shape")
        if not isinstance(section["body"], str):
            raise InvestmentMemoDraftError("section body must be text")
        refs = section["claim_refs"]
        if not isinstance(refs, list) or any(ref not in allowed_refs or not str(ref).startswith("claim-version:") for ref in refs):
            raise InvestmentMemoDraftError("a section cites material outside the frozen input")
        if not isinstance(section["numbers"], list) or not isinstance(section["gaps"], list):
            raise InvestmentMemoDraftError("section numbers and gaps must be lists")
        if any(not isinstance(gap, str) for gap in section["gaps"]):
            raise InvestmentMemoDraftError("section gaps must be text")
        for number in section["numbers"]:
            if not isinstance(number, Mapping):
                raise InvestmentMemoDraftError("each section number must be an object")
            cell = number.get("cell")
            if cell is not None and not isinstance(cell, Mapping):
                raise InvestmentMemoDraftError("a section number cell must be an object")
            refs_for_number = [number.get("claim_version_ref"), (cell or {}).get("ref")]
            if not any(ref in allowed_refs for ref in refs_for_number if ref):
                raise InvestmentMemoDraftError("a number cites material outside the frozen input")
        checked_sections.append({**dict(section), "body": str(section["body"]).strip(),
                                 "claim_refs": list(dict.fromkeys(refs)),
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
        if not isinstance(answer["refs"], list) or any(ref not in allowed_refs for ref in answer["refs"]):
            raise InvestmentMemoDraftError("a key question cites material outside the frozen input")
        if answer["unknown"] is True:
            if answer["answer"] or answer["refs"]:
                raise InvestmentMemoDraftError("an unknown answer cannot assert or cite an answer")
        elif answer["unknown"] is not False or not answer["answer"] or not answer["refs"] or not answer["falsifier"]:
            raise InvestmentMemoDraftError("an answered key question needs answer, refs and falsifier")
        checked_answers.append({**dict(answer), "answer": str(answer["answer"]).strip(),
                                "falsifier": str(answer["falsifier"]).strip(),
                                "refs": list(dict.fromkeys(answer["refs"]))})
    return {"sections": checked_sections, "key_questions": checked_answers}


def draft_group(model: Any, *, group: str, section_titles: Sequence[str],
                questions: Sequence[Mapping[str, str]], material: Sequence[Mapping[str, Any]],
                company: Mapping[str, Any], mission: Mapping[str, Any]) -> dict[str, Any]:
    prompt = build_group_prompt(group=group, section_titles=section_titles, questions=questions,
                                material=material, company=company)
    request_id = "memo-" + content_hash({"contract": MEMO_PROMPT_CONTRACT_VERSION, "group": group, "prompt": prompt})[:24]
    call: Mapping[str, Any] = {}
    try:
        call = model.call(purpose=DRAFT_PURPOSE, request_id=request_id, prompt=prompt, mission=mission)
        parsed = parse_group_output(unwrap_json_object(call["text"]), section_titles=section_titles,
                                    questions=questions, allowed_refs={str(row["ref"]) for row in material})
    except (CockpitModelError, InvestmentMemoDraftError, KeyError, json.JSONDecodeError) as exc:
        return {"status": "refused", "reason": f"{type(exc).__name__}: {exc}",
                "cost_micros": int(call.get("cost_micros") or 0)}
    return {"status": "drafted", **parsed, "group": group,
            "model": {"work_order_ref": call.get("work_order_ref"),
                      "route_decision_ref": call.get("route_decision_ref"),
                      "result_envelope_ref": call.get("result_envelope_ref"),
                      "invocation_ref": call.get("invocation_ref"),
                      "cost_micros": int(call.get("cost_micros") or 0)}}


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
        "A pass has finding_codes=[]. Return raw JSON only:", _json(contract),
        "Complete memo sections:", _json(list(sections)), "All 12 key questions:", _json(list(questions)),
        "Complete cited evidence rows:", _json(list(material)),
    ])


def verify_memo(model: Any, *, sections: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]],
                material: Sequence[Mapping[str, Any]], material_hash: str,
                mission: Mapping[str, Any], producer_route_decision_refs: Sequence[str]) -> dict[str, Any]:
    prompt = build_verifier_prompt(sections=sections, questions=questions, material=material,
                                   material_hash=material_hash)
    call: Mapping[str, Any] = {}
    try:
        call = independent_model_call(model, producer_route_decision_refs=producer_route_decision_refs,
            purpose=VERIFIER_PURPOSE, request_id="memo-verify-" + material_hash[:24] + "-" + content_hash(MEMO_VERIFIER_PROMPT_CONTRACT_VERSION)[:8], prompt=prompt, mission=mission)
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


__all__ = ["DRAFT_PURPOSE", "VERIFIER_PURPOSE", "GROUPS", "FINDING_CODES",
           "InvestmentMemoDraftError", "build_group_prompt", "parse_group_output", "draft_group",
           "verified_material_hash", "build_verifier_prompt", "verify_memo"]
