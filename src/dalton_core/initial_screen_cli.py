"""P10c child: draft one company's Initial Screen and decide its gate.

Runs out of process, like every other lane that calls a model, so a section
that takes a minute cannot stall the writer or the controller tick.  One run
handles at most one company:

1. pick the highest-priority company whose Initial Screen is entered and
   either undrafted or due a redraft (batch C's redraft policy when installed:
   batched thresholds, and ``gate_passed`` is no longer terminal);
2. batch D, the STRL contract: build the material pack, hold with
   MaterialRequests when it is insufficient, draft the analysis worksheet, then
   the whole nine-section document in one call sharing it;
3. verify citations, arithmetic, view / forecast gap / valuation presence and
   the reasoning lint, plus an independent model verifier; one whole-document
   repair; never delete sentences to pass;
4. score it against rubric H, publish it with its contract record, assess the
   Playbook's four gate questions and record the stage and the score.

``--legacy-claim-sections`` replays the pre-batch-D per-section Claim drafter.

Every step is idempotent: a re-run with nothing new to say publishes nothing.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from .claim_retirement import ClaimRetirementAuthority
from .deliverable_reopen import CHANGE_REASON_EVIDENCE, approved_reopen
from .cockpit_model import CockpitModel, CockpitModelError, lane_status_for, register_purpose
from .product_contract import (
    MaterialPackUnavailable,
    build_pack,
    check_sufficiency,
    record_pack_build,
)
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .deep_insight_gate_cli import valuation_rows
from .initial_screen import (
    STRL_CONTRACT_VERSION,
    STRL_TEMPLATE_REF,
    STRL_TITLES,
    InitialScreenContractError,
    assess_strl_gate,
    build_document_prompt,
    build_verifier_prompt,
    build_worksheet_prompt,
    pack_resolvers,
    parse_document_output,
    parse_verifier_output,
    strl_contract_record,
    validate_worksheet,
    verify_document,
    KIND,
    SECTION_GUIDANCE,
    TEMPLATE_KEY,
    VALUATION_GAP,
    valuation_section_held,
    assess_exit_gate,
    build_claim_context,
    build_section_prompt,
    parse_section_output,
    raw_section_body,
    section_titles,
)
from .mission_deliverable import (
    GAP_MARKER,
    MissionDeliverableAuthority,
    MissionDeliverableError,
    WRITE_SCOPE,
    unsourced_numbers,
)
from .prior_screen_import import build_delta_vs_prior, prior_reference
from .research_quality_score import residual_citation_artefacts
from .mission_stage import evaluate_mission, planned_spec_refs_from_directory
from .data_foundation import (
    REASON_CODE as FOUNDATION_REASON_CODE,
    foundation_blockers,
    hold_sentence,
    latest_failures,
    waiting_since,
)
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _live_claims(store: DaltonStore, retired: set[str]) -> dict[str, list[dict[str, Any]]]:
    """subject_ref → its live Claims, oldest first."""

    by_subject: dict[str, list[dict[str, Any]]] = {}
    for row in store.connection.execute(
        "SELECT claim_version_id, claim_json, created_at FROM claim_versions ORDER BY created_at"
    ).fetchall():
        if row["claim_version_id"] in retired:
            continue
        claim = json.loads(row["claim_json"])
        by_subject.setdefault(claim.get("subject_ref") or "", []).append({
            "ref": row["claim_version_id"], "statement": claim.get("normalized_statement") or "",
            "period": claim.get("period"), "aspect": claim.get("metric_or_aspect"),
            "value": claim.get("value"), "created_at": row["created_at"],
            # P10c/Q1: the drafter deduplicates by what a Claim asserts and
            # prefers the filing-grade copy, so it needs the fields that say
            # what was asserted and where it came from.
            "unit": claim.get("unit"), "subject_ref": claim.get("subject_ref"),
            "basis": claim.get("basis"),
        })
    return by_subject


def _playbook(store: DaltonStore, mission: dict[str, Any]) -> dict[str, Any]:
    binding = mission["bindings"]["playbook_version"]
    row = store.connection.execute(
        "SELECT record_json, content_hash FROM research_playbook_versions WHERE playbook_version_id=?",
        (binding["ref"],),
    ).fetchone()
    if row is None:
        raise MissionDeliverableError("the mission's playbook version is missing")
    record = json.loads(row["record_json"])
    if record["content_hash"] != binding["hash"]:
        raise MissionDeliverableError("the mission's playbook binding drifted")
    return record


def _target(
    *, mission: dict[str, Any], stage_rows: list[dict[str, Any]],
    deliverables: dict[str, dict[str, Any]], claims: dict[str, list[dict[str, Any]]],
    reopens: dict[str, dict[str, Any]] | None = None,
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The next company to draft, in mission priority order, and why the rest wait.

    P14d: ``gate_passed`` used to end the sentence.  ADR-0008 says it is the
    state of a *version*, so it now ends the sentence unless a person approved
    a ``gate_reopen`` for this company and no version has spent it yet.  The
    approval also defeats the staleness rule below, because the whole finding
    that produced it is that the evidence base thickened in ways no new Claim
    of this company's records -- 10,023 filed statement lines are not Claims.
    """

    approved = dict(reopens or {})
    skipped: list[dict[str, Any]] = []
    for entry in stage_rows:
        company_ref = entry["company_ref"]
        reopen = approved.get(company_ref)
        if entry["stage"] != "initial_screen":
            skipped.append({"company_ref": company_ref, "reason": f"stage is {entry['stage']}"})
            continue
        if entry["stage_status"] == "gate_passed" and reopen is None:
            # Batch C: a passed screen is no longer terminal, but the reopen
            # lane (redraft_policy.auto_reopen) reopens it; this lane then sees
            # the approved reopen like any other.
            skipped.append({"company_ref": company_ref, "reason": "initial screen already passed"})
            continue
        own = claims.get(company_ref) or []
        if not own:
            skipped.append({"company_ref": company_ref, "reason": "no live Claim to write from"})
            continue
        # The Playbook reads first and writes second: a document drafted before
        # the required readings are in hand is a document that has to be
        # rewritten, and it spends model calls saying what is missing.
        missing = [
            item["label"] for item in entry["items"] if item["status"] in {"partial", "missing"}
        ]
        if missing:
            skipped.append({
                "company_ref": company_ref,
                "reason": "资料底座还没齐：" + "、".join(missing),
            })
            continue
        published = deliverables.get(company_ref)
        # W3: an imported prior screen is version 0 and is not a screen this
        # system wrote. It is the head of the chain until Dalton drafts, so
        # the staleness test below would read it as "we already have one" and
        # the company would never be written up -- which is the exact opposite
        # of what importing it was for. Treated as no screen yet, deliberately
        # and by version number rather than by date.
        if published is not None and published.get("version") == 0:
            published = None
        decision = (decisions or {}).get(company_ref) if decisions is not None else None
        if decisions is not None and reopen is None and published is not None:
            # Owner decision 4 (batched redrafts): batch C's thresholds replace
            # "older than the newest Claim"; one new item never triggers a rewrite.
            if decision is not None and decision.get("decision") == "no":
                skipped.append({"company_ref": company_ref,
                                "reason": str(decision.get("summary") or "未达重起草阈值")})
                continue
        elif (
            reopen is None
            and published is not None
            and published["created_at"] >= own[-1]["created_at"]
        ):
            skipped.append({"company_ref": company_ref, "reason": "nothing new since the last version"})
            continue
        return {**entry, "reopen": reopen,
                "redraft_decision": None if decision is None else {
                    "decision": decision.get("decision"), "summary": decision.get("summary")}}, skipped
    return None, skipped


def _redraft_decisions(state: Path, stage_rows: list[dict[str, Any]],
                       published: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """Batch C's ``redraft_policy.evaluate_targets`` for every screen-stage company.

    ``None`` when the sibling module is not installed -- the Claim rule then
    holds -- so this lane never depends on batch C to run at all.
    """

    from .product_contract import sibling_module

    try:
        policy = sibling_module("redraft_policy")
    except MaterialPackUnavailable:
        return None
    last: dict[str, Any] = {}
    for entry in stage_rows:
        if entry.get("stage") != "initial_screen":
            continue
        record = published.get(entry["company_ref"])
        if record is not None and record.get("version") == 0:
            record = None
        last[entry["company_ref"]] = policy.last_draft_from_version(
            record, stage_status=entry.get("stage_status"))
    if not last:
        return {}
    return policy.evaluate_targets(state, KIND, last)


def _filings_since(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """The company's filings, newest last, or nothing if the lane never ran.

    A read that fails is an absence, not an exception: "what changed since the
    last version" is a reported block, and a Core with no statements lane must
    say it does not know rather than take the gate down with it.
    """

    try:
        rows = store.connection.execute(
            "SELECT accession, form, filed_at FROM coverage_mission_statement_filings "
            "WHERE company_ref=? ORDER BY filed_at",
            (company_ref,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - reported, never a trigger
        return []
    return [
        {"ref": row["accession"], "form": row["form"], "filed_at": str(row["filed_at"])[:10]}
        for row in rows
    ]


def _shifted_debates(
    store: Any, company_ref: str, since: str | None
) -> list[dict[str, Any]]:
    """Debates that are moving, or that opened after the prior version.

    Read off the current map rather than diffed against a stored version,
    because the prior version this compares to is a *date* -- an imported v0
    carries a manifest date and no map version at all -- and a debate's
    ``first_seen_at`` is the field that answers "is this argument newer than
    what we last wrote".
    """

    try:
        from .debate_map import DebateMapAuthority

        authority = DebateMapAuthority(store)
        current = authority.current(company_ref)
    except Exception:  # noqa: BLE001 - reported, never a trigger
        return []
    if current is None:
        return []
    return [
        dict(debate) for debate in current["debates"]
        if debate.get("status") == "shifting"
        or (since and str(debate.get("first_seen_at") or "")[:10] > since)
    ]


def reopen_revision(reopen: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Turn an approved gate reopen into the version's ``revision`` block.

    The refs are the proposal's -- the statement filings, figures and Claims
    that flipped an item from 缺 to 有 -- plus the approval itself, so a reader
    of version N+1 can get from "why does this exist" to the exact diff and the
    person who said yes without leaving the record.
    """

    if reopen is None:
        return None
    proposal = reopen.get("proposal") or {}
    refs = list(proposal.get("evidence_refs") or ())
    refs = [reopen["id"], reopen["proposal_ref"], proposal.get("passed_version_ref"), *refs]
    return {
        "change_reason": proposal.get("change_reason") or CHANGE_REASON_EVIDENCE,
        "evidence_refs": [ref for ref in refs if ref],
        "reopen_ref": reopen["proposal_ref"],
    }


# A URL's punctuation is a URL's punctuation: "https://" is a colon and two
# slashes, and "?a=1&b=2" is not a citation that lost its citations.
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。；、）)】」]+", re.IGNORECASE)
_ORPHAN_PREFIX = "orphan_"


def _actionable_residue(raw_body: str, cleaned_body: str) -> list[dict[str, Any]]:
    """Which residual-artefact findings the drafter should actually act on.

    The scorer's detector is deliberately broad because it *grades*: a false
    positive there costs a point on one criterion. Here it *gates* -- a flagged
    section spends a corrective call and is then dropped to a gap -- so a false
    positive costs the section, and on S4 it costs the exit gate, permanently.
    The two filters are the difference between the two jobs.

    **A finding inside a URL is not wreckage.** It is a URL.

    **An orphan verb is only wreckage if stripping the tags created it.**
    "关键驱动因素：反映了行业周期的位置" is ordinary Chinese: it reads the same
    before and after the tags come out, so there was never a tag holding that
    position. "：C12显示…" does not match before and does after, which is the
    whole shape of the defect -- a sentence whose subject went with the tag.
    The comparison is by what matched rather than by offset, because removing
    the tags moves every offset after them.
    """

    spans = [match.span() for match in _URL_RE.finditer(cleaned_body)]
    before = Counter(
        (finding["code"], finding["matched"])
        for finding in residual_citation_artefacts(raw_body)
        if finding["code"].startswith(_ORPHAN_PREFIX)
    )
    actionable: list[dict[str, Any]] = []
    for finding in residual_citation_artefacts(cleaned_body):
        if finding["code"].startswith(_ORPHAN_PREFIX):
            key = (finding["code"], finding["matched"])
            if before[key]:
                before[key] -= 1
                continue
        elif any(start <= finding["at"] < end for start, end in spans):
            continue
        actionable.append(finding)
    return actionable


def _correction_note(
    stray: list[str], residue: list[dict[str, Any]],
    period_issues: list[dict[str, str]] | None = None,
) -> str:
    """What the one corrective attempt is told, in the terms of the defect."""

    notes = ["\n"]
    if stray:
        notes.append(
            "\nYour previous draft wrote figures no N tag carries: "
            + "、".join(stray[:8])
            + f"。Rewrite the section without them: copy a figure verbatim from an N tag "
              f"or write {GAP_MARKER}. Do not convert units or scales."
        )
    if residue:
        notes.append(
            "\nYour previous draft left citation scaffolding in the prose. After the C/N tags "
            "are removed the text reads: "
            + "；".join(f"「{item['excerpt']}」" for item in residue[:4])
            + "。Rewrite the section with no C or N tag anywhere in the body and no sentence "
              "whose subject was a tag: name the source in words (管理层、该季报、卖方研报) and "
              "put the tags in the JSON arrays only."
        )
    if period_issues:
        notes.append(
            "\nYour previous draft attached an authoritative amount to the wrong quarter: "
            + "；".join(
                f"USD {item['amount']} was written as {item['written_period']} but its N tag "
                f"is {item['expected_period']} ({item['period']})"
                for item in period_issues[:4]
            )
            + ". Rewrite those period labels to match the N tags; do not change the amounts, "
              "rates, thesis, or any other fact."
        )
    return "".join(notes)


_QUARTER_LABEL = re.compile(
    r"(?P<year>20\d{2})\s*(?:(?:年\s*)?(?:第\s*)?"
    r"(?P<cn_quarter>[一二三四1-4])\s*(?:季度|季)|Q(?P<q_quarter>[1-4]))",
    re.IGNORECASE,
)
_PRIMARY_USD_AMOUNT = re.compile(r"\breported\b.*?\bof\s+USD\s+([0-9][0-9,]*)", re.IGNORECASE)


def inconsistent_number_periods(
    body: str, numbers: list[Mapping[str, Any]]
) -> list[dict[str, str]]:
    """Find a quarter label that contradicts the Claim carrying its amount.

    This is deliberately narrow.  It checks only a filing Claim's primary USD
    amount when that exact raw amount and a nearby quarter label both appear in
    prose.  Omitting a label, formatting an amount, or citing a comparison
    figure is left to the existing source and completeness checks.
    """

    issues: list[dict[str, str]] = []
    quarter_number = {"一": 1, "二": 2, "三": 3, "四": 4}
    for number in numbers:
        period = str(number.get("period") or "")
        match = re.fullmatch(r"(20\d{2})-(\d{2})-\d{2}\.\.(20\d{2})-(\d{2})-\d{2}", period)
        amount_match = _PRIMARY_USD_AMOUNT.search(str(number.get("text") or ""))
        if match is None or amount_match is None:
            continue
        start_year, end_year = int(match.group(1)), int(match.group(3))
        start_month, end_month = int(match.group(2)), int(match.group(4))
        # A date range such as Accenture's Mar-May fiscal quarter does not
        # reveal the issuer's fiscal-quarter number.  Only calendar-aligned
        # ranges carry enough information for this comparison.
        expected_range = (
            start_year == end_year
            and period[8:10] == "01"
            and int(period[-2:]) == calendar.monthrange(end_year, end_month)[1]
            and (start_month, end_month) in {(1, 3), (4, 6), (7, 9), (10, 12)}
        )
        if not expected_range:
            continue
        expected_year = int(match.group(1))
        expected_quarter = (start_month - 1) // 3 + 1
        amount = amount_match.group(1).replace(",", "")
        for occurrence in re.finditer(rf"(?<!\d){re.escape(amount)}(?!\d)", body):
            prefix = body[max(0, occurrence.start() - 48):occurrence.start()]
            labels = list(_QUARTER_LABEL.finditer(prefix))
            if not labels:
                continue
            label = labels[-1]
            label_prefix = prefix[max(0, label.start() - 12):label.start()]
            if re.search(r"(?:FY|fiscal\s*|财年\s*)$", label_prefix, re.IGNORECASE):
                continue
            actual_year = int(label.group("year"))
            raw_quarter = label.group("cn_quarter") or label.group("q_quarter")
            actual_quarter = quarter_number.get(raw_quarter, int(raw_quarter) if raw_quarter.isdigit() else 0)
            if (actual_year, actual_quarter) != (expected_year, expected_quarter):
                issues.append({
                    "claim_version_ref": str(number.get("claim_version_ref") or ""),
                    "amount": amount,
                    "period": period,
                    "written_period": f"{actual_year}Q{actual_quarter}",
                    "expected_period": f"{expected_year}Q{expected_quarter}",
                })
            break
    return issues


def _dropped_section(
    title: str, stray: list[str], residue: list[dict[str, Any]]
) -> dict[str, Any]:
    """A section whose one corrective attempt did not fix it.

    Dropped to a gap rather than published: the rest of the document can still
    go out, and the gap says which defect took this section, which is what a
    reader needs to know that the absence is deliberate.
    """

    reasons = []
    if stray:
        reasons.append(f"写了没有来源的数字（{'、'.join(stray[:5])}）；需要的数字还没有进入账本")
    if residue:
        reasons.append(
            "引用标记剥离后留下了残句（"
            + "、".join(sorted({item["code"] for item in residue}))
            + f"）：{residue[0]['excerpt']}"
        )
    return {"title": title, "body": "", "claim_refs": [], "numbers": [],
            "gaps": ["这一节已丢弃：" + "；".join(reasons)]}


def _run_legacy_sections(
    args: argparse.Namespace, *, state: Path, store: DaltonStore, mission: dict[str, Any],
    playbook: dict[str, Any], entry: dict[str, Any], summary: dict[str, Any],
    deliverable_authority: MissionDeliverableAuthority, missions: CoverageMissionAuthority,
    claims: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """The pre-batch-D per-section Claim drafter, kept only behind
    ``--legacy-claim-sections`` for replay; the default path is the STRL contract."""

    company_ref = entry["company_ref"]
    titles = section_titles(playbook)
    # P11c-E: the computed multiples, if this Core has priced the company.
    # Read through the gate's reader rather than a second one: it already
    # mints ``valuation-metric:<version>:<metric>``, already drops the
    # metrics a snapshot published as unavailable, and a second
    # implementation of that filter would disagree with it eventually.
    valuation = valuation_rows(store, company_ref)
    context = build_claim_context(claims.get(company_ref) or [],
                                  valuation=valuation)
    summary["context"] = {
        "claims": len(context["claims"]), "numbers": len(context["numbers"]),
        # Reported separately from ``numbers`` because it is the one input
        # that decides whether the valuation section gets written at all.
        "valuation_numbers": len(valuation),
        # One series means the screen this drafts will have one numeric
        # series however well it is written; that is a Ledger fact and the
        # summary is where it should be visible.
        "series": context["series"],
        "duplicates_dropped": context["duplicates_dropped"],
    }
    model = None
    if not args.dry_run:
        model = CockpitModel(
            json.loads(Path(args.model_config).expanduser().read_text(encoding="utf-8")),
            scheduler_db=args.scheduler_db or str(state / "scheduler.sqlite"),
            max_output_tokens=1800, timeout_seconds=args.timeout_seconds,
        )
    # W3: what this fund said about this company last time, if anything.
    # ``published`` was cleared by the selection rule when the head is an
    # imported v0, so the block is read from the chain rather than from
    # that variable.
    prior_block = prior_reference(
        deliverable_authority.latest(f"mission-deliverable:{KIND}:"
                                     f"{company_ref.rsplit(':', 1)[-1]}")
    )
    summary["prior_reference"] = None if prior_block is None else {
        "version_ref": prior_block["version_ref"],
        "version": prior_block["version"],
        "as_of": prior_block["as_of"],
        "age_months": prior_block["age_months"],
        "imported": prior_block["imported"],
    }
    sections: list[dict[str, Any]] = []
    invocations: list[str] = []
    for index, title in enumerate(titles):
        guidance = SECTION_GUIDANCE[index] if index < len(SECTION_GUIDANCE) else ""
        # P11c-E: the valuation section is gated on having something to
        # write it from, not on being the valuation section. With a
        # published snapshot it is drafted like any other; without one
        # there is still no figure in this Core that could support it, and
        # a drafted-but-empty section would read as "we looked and it is
        # not interesting" rather than "we have not priced this company".
        if valuation_section_held(title, guidance, valuation):
            sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                             "gaps": [VALUATION_GAP]})
            summary["sections"].append({"title": title, "status": "not_drafted", "reason": "valuation gate"})
            continue
        prompt = build_section_prompt(
            title=title, guidance=guidance,
            company={"ticker": entry["ticker"], "company_ref": company_ref},
            mission=mission, context=context, checklist=entry["items"],
            prior_reference=prior_block,
        )
        if model is None:
            sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                             "gaps": ["dry run: 没有调用模型"]})
            summary["sections"].append({"title": title, "status": "dry_run"})
            continue
        try:
            call = model.call(
                purpose="draft",
                request_id=f"{mission['id']}:{company_ref}:{KIND}:{index}:{len(claims.get(company_ref) or [])}",
                prompt=prompt, mission=mission,
            )
        except CockpitModelError as exc:
            sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                             "gaps": [f"这一节没能起草：{exc}"]})
            # C2: a spent pool is a budget decision, not a failure.
            summary["sections"].append({
                "title": title, "status": lane_status_for(exc, "failed"),
                "reason": str(exc)})
            continue
        section = parse_section_output(call["text"], context=context, title=title)
        if call.get("invocation_ref"):
            invocations.append(call["invocation_ref"])
        raw = raw_section_body(call["text"])
        # Two defects the publish path cannot catch on its own. The authority
        # refuses the whole document for one unsourced figure, and it accepts
        # citation-strip wreckage without comment -- live, four of the five
        # published screens carry some, and gate_passed is terminal, so what
        # is published this way is published forever. Give the section one
        # corrective attempt with both named, then drop its body to a gap so
        # the rest of the document can still be published.
        stray = unsourced_numbers(section["body"], section["numbers"])
        residue = _actionable_residue(raw, section["body"])
        period_issues = inconsistent_number_periods(section["body"], section["numbers"])
        retried = False
        if stray or residue or period_issues:
            retried = True
            try:
                correction = model.call(
                    purpose="draft",
                    request_id=f"{mission['id']}:{company_ref}:{KIND}:{index}:"
                               f"{len(claims.get(company_ref) or [])}:retry",
                    prompt=prompt + _correction_note(stray, residue, period_issues),
                    mission=mission,
                )
                candidate = parse_section_output(correction["text"], context=context, title=title)
                if correction.get("invocation_ref"):
                    invocations.append(correction["invocation_ref"])
                candidate_stray = unsourced_numbers(candidate["body"], candidate["numbers"])
                candidate_residue = _actionable_residue(
                    raw_section_body(correction["text"]), candidate["body"])
                candidate_period_issues = inconsistent_number_periods(
                    candidate["body"], candidate["numbers"]
                )
                if (candidate["body"] and not candidate_stray
                        and not candidate_residue and not candidate_period_issues):
                    section = candidate
                    stray, residue, period_issues = [], [], []
                else:
                    stray = candidate_stray or stray
                    residue = candidate_residue or residue
                    period_issues = candidate_period_issues or period_issues
            except CockpitModelError as exc:
                summary["sections"].append({
                    "title": title,
                    "status": lane_status_for(exc, "retry_failed"),
                    "reason": str(exc)})
        if stray or residue or period_issues:
            if period_issues and not stray and not residue:
                section = {
                    "title": title, "body": "", "claim_refs": [], "numbers": [],
                    "gaps": [
                        "这一节暂缺：正文中的财务期间与引用来源不一致，需要核对后补充。"
                    ],
                }
            else:
                section = _dropped_section(title, stray, residue)
        sections.append(section)
        summary["sections"].append({
            "title": title,
            "status": ("drafted" if section["body"]
                       else ("dropped_unsourced" if stray else (
                           "dropped_residual_citation" if residue else "dropped_period_mismatch"))),
            "retried": retried,
            "period_issues": period_issues,
            "chars": len(section["body"]), "claims": len(section["claim_refs"]),
            "numbers": len(section["numbers"]), "replayed": call["replayed"],
            "cost_usd": round(call["cost_micros"] / 1_000_000, 6),
        })
    written = [item for item in sections if item["body"]]
    if not written:
        summary["status"] = "failed"
        summary["failure_reason"] = "没有任何一节写出来，不发布空壳"
        return summary
    summary_text = written[0]["body"][:1200]
    try:
        record = deliverable_authority.publish(
            kind=KIND, subject_ref=company_ref, mission=mission, playbook=playbook,
            template_ref=f"playbook:deliverable_templates.{TEMPLATE_KEY}",
            sections=sections, summary=summary_text,
            gaps=[gap for section in sections for gap in section["gaps"]],
            model_invocation_refs=invocations,
            actor_ref=mission["autonomy"]["automation_principal"],
            revision=reopen_revision(entry.get("reopen")),
        )
    except MissionDeliverableError as exc:
        summary["status"] = "failed"
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    summary["deliverable"] = {
        "ref": record["deliverable_ref"], "version": record["version"],
        "version_ref": record["id"], "status": record["status"],
    }
    summary["formal_authority_writes"] = 1 if record["status"] == "fresh" else 0
    gate = assess_exit_gate(
        playbook=playbook, checklist_entry=entry, sections=record["sections"],
        delta_vs_prior=build_delta_vs_prior(
            prior=prior_block,
            filings=_filings_since(store, company_ref),
            shifted_debates=_shifted_debates(store, company_ref,
                                             (prior_block or {}).get("as_of")),
        ),
    )
    summary["gate"] = gate
    try:
        stage = missions.record_stage(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=company_ref, stage_ref="initial_screen",
            status="gate_passed" if gate["passed"] else "gate_failed",
            evidence_refs=[record["id"], mission["id"]],
            rationale=f"P10c 出口门自评：{gate['rationale']}"[:2000],
            actor_ref=mission["autonomy"]["automation_principal"],
            idempotency_key=f"{mission['id']}:{company_ref}:initial_screen:{record['id']}",
        )
        summary["gate"]["stage_record"] = stage.get("status_marker", "recorded")
    except CoverageMissionError as exc:
        summary["gate"]["stage_record"] = f"not_recorded:{type(exc).__name__}: {exc}"
    return summary

# ---------------------------------------------------------------------------
# Batch D: the STRL-shaped contract -- material pack, worksheet, one document
# ---------------------------------------------------------------------------

#: Bounds for the two-step drafter.  The pack is 150-250K characters (owner
#: decision 3 accepts the cost), so the call bound is the pack, not 60K.
STRL_MAX_INPUT_TOKENS = 600_000
STRL_MAX_OUTPUT_TOKENS = 16_000
#: Batch C2: the STRL worksheet and document run under their own purpose, not
#: the legacy section drafter's ``draft``, so the long-context call floor
#: (``call_budget.LONG_CONTEXT_CALL_FLOOR``) and the deliverable tier apply to
#: them and to nothing else.
STRL_PURPOSE = register_purpose("initial_screen")
STRL_VERIFIER_PURPOSE = register_purpose("initial_screen_verifier")


def _call_json(model: Any, *, request_id: str, prompt: str, mission: Mapping[str, Any],
               invocations: list[str], purpose: str = STRL_PURPOSE,
               producer_routes: Sequence[str] = ()) -> tuple[Any, dict[str, Any]]:
    from .cockpit_model import independent_model_call, unwrap_json_object

    if producer_routes:
        call = independent_model_call(model, producer_route_decision_refs=producer_routes,
                                      purpose=purpose, request_id=request_id, prompt=prompt,
                                      mission=mission)
    else:
        call = model.call(purpose=purpose, request_id=request_id, prompt=prompt, mission=mission)
    if call.get("invocation_ref"):
        invocations.append(call["invocation_ref"])
    return unwrap_json_object(call["text"]), call


def _run_strl(
    args: argparse.Namespace, *, state: Path, store: DaltonStore, mission: dict[str, Any],
    playbook: dict[str, Any], entry: dict[str, Any], summary: dict[str, Any],
    deliverable_authority: MissionDeliverableAuthority, missions: CoverageMissionAuthority,
    claims: dict[str, list[dict[str, Any]]], model: Any = None,
) -> dict[str, Any]:
    """Draft one company's Initial Screen under the STRL contract.

    1. build the material pack (batch C); hold, with MaterialRequests opened
       for what is missing, when it is not sufficient;
    2. step one: the analysis worksheet (view, forecasts, valuation
       arithmetic, key passages), validated deterministically;
    3. step two: the whole document in one call sharing the worksheet;
    4. verify (citations, arithmetic, presence of view / forecast gap /
       valuation, reasoning lint), then the model verifier; one whole-document
       repair on rejection; a document that still fails is not published --
       nothing is deleted to make it pass;
    5. score it against rubric H, publish with the contract record, decide
       the gate, and record the score.
    """

    company_ref = entry["company_ref"]
    summary["contract"] = STRL_CONTRACT_VERSION
    deliverable_ref = f"mission-deliverable:{KIND}:{company_ref.rsplit(':', 1)[-1]}"
    run_ref = f"{KIND}-run:{company_ref}:{summary.get('created_at') or _now()}"
    as_of = getattr(args, "as_of", None)
    # Batch C: collect first, then draft. The sufficiency check opens (or
    # joins) a MaterialRequest per missing item -- never on a dry run -- and
    # a product that may not draft yet holds with the reason and the wait.
    try:
        check = check_sufficiency(
            state, company_ref, KIND, as_of,
            requester={"lane": KIND, "product_ref": deliverable_ref, "run_ref": run_ref},
            record=not args.dry_run)
    except MaterialPackUnavailable as exc:
        summary["status"] = "held"
        summary["reason_code"] = "material_pack_unavailable"
        summary["failure_reason"] = str(exc)
        return summary
    summary["sufficiency"] = {key: check.get(key) for key in (
        "status", "drafting_allowed", "reason", "wait_until", "gap_request_refs")}
    if not check.get("drafting_allowed"):
        summary["status"] = "held"
        summary["reason_code"] = "material_insufficient"
        summary["failure_reason"] = ("材料包未通过充分性检查，先取材料再起草："
                                     + str(check.get("reason") or "缺必需材料"))[:800]
        summary["material_requests"] = list(check.get("gap_request_refs") or [])
        return summary
    try:
        pack = build_pack(state, company_ref, KIND, as_of)
    except MaterialPackUnavailable as exc:
        summary["status"] = "held"
        summary["reason_code"] = "material_pack_unavailable"
        summary["failure_reason"] = str(exc)
        return summary
    # The check this run made is the one the drafter cites (its requests).
    pack["sufficiency"] = {**dict(pack.get("sufficiency") or {}),
                           **{key: check[key] for key in ("status", "gap_request_refs", "missing")
                              if key in check}}
    summary["pack"] = {
        "content_hash": pack["content_hash"], "recipe_version": pack["recipe_version"],
        "chars": len(pack["text"]), "tags": len(pack["tags"]),
    }
    if not args.dry_run:
        try:
            record_pack_build(state, pack, run_ref=run_ref)
        except Exception as exc:  # noqa: BLE001 - the ledger row is provenance, not the draft
            summary["pack"]["build_ledger"] = f"not_recorded: {type(exc).__name__}: {exc}"[:300]
    prior_block = prior_reference(
        deliverable_authority.latest(f"mission-deliverable:{KIND}:{company_ref.rsplit(':', 1)[-1]}"))
    company = {"ticker": entry["ticker"], "company_ref": company_ref}
    worksheet_prompt = build_worksheet_prompt(company=company, pack=pack, prior_reference=prior_block)
    summary["prompts"] = {"worksheet_chars": len(worksheet_prompt)}
    if args.dry_run:
        summary["sections"] = [{"title": title, "status": "dry_run"} for title in STRL_TITLES]
        return summary
    if model is None:
        model = CockpitModel(
            json.loads(Path(args.model_config).expanduser().read_text(encoding="utf-8")),
            scheduler_db=args.scheduler_db or str(state / "scheduler.sqlite"),
            max_input_tokens=STRL_MAX_INPUT_TOKENS, max_output_tokens=STRL_MAX_OUTPUT_TOKENS,
            timeout_seconds=args.timeout_seconds,
        )
    invocations: list[str] = []
    routes: list[str] = []
    base_id = f"{mission['id']}:{company_ref}:{KIND}:strl:{pack['content_hash'][:16]}"
    try:
        # Step one, with one repair on deterministic findings.
        raw, call = _call_json(model, request_id=f"{base_id}:worksheet", prompt=worksheet_prompt,
                               mission=mission, invocations=invocations)
        routes.append(str(call.get("route_decision_ref") or ""))
        try:
            worksheet, sheet_findings = validate_worksheet(raw, pack)
        except InitialScreenContractError as exc:
            worksheet, sheet_findings = None, [{"code": "worksheet_shape", "detail": str(exc)}]
        if sheet_findings:
            raw, call = _call_json(
                model, request_id=f"{base_id}:worksheet:repair",
                prompt=worksheet_prompt + "\n\nYour previous worksheet was rejected: "
                + "；".join(f"{item['code']} {item.get('detail') or ''}" for item in sheet_findings[:20])
                + "\nReturn the complete worksheet again with every finding fixed.",
                mission=mission, invocations=invocations)
            routes.append(str(call.get("route_decision_ref") or ""))
            try:
                worksheet, sheet_findings = validate_worksheet(raw, pack)
            except InitialScreenContractError as exc:
                worksheet, sheet_findings = None, [{"code": "worksheet_shape", "detail": str(exc)}]
        summary["worksheet"] = {"findings": sheet_findings,
                                "stance": (worksheet or {}).get("stance")}
        if worksheet is None or sheet_findings:
            summary["status"] = "failed"
            summary["failure_reason"] = "底稿未通过核验：" + "、".join(
                sorted({item["code"] for item in sheet_findings}))
            return summary
        # Step two: the whole document, one call, then verify; one repair.
        live = deliverable_authority.live_claim_version_refs()
        resolve_cell = deliverable_authority.cell_resolver()
        resolve_passage = deliverable_authority.passage_resolver()
        verdict: dict[str, Any] = {"verdict": "reject", "findings": []}
        sections: list[dict[str, Any]] = []
        repair: list[dict[str, Any]] = []
        attempts = []
        for attempt in range(2):
            prompt = build_document_prompt(company=company, pack=pack, worksheet=worksheet,
                                           prior_reference=prior_block, repair=repair)
            raw, call = _call_json(model, request_id=f"{base_id}:document:{attempt}", prompt=prompt,
                                   mission=mission, invocations=invocations)
            routes.append(str(call.get("route_decision_ref") or ""))
            try:
                sections, parse_findings = parse_document_output(raw, pack=pack)
            except InitialScreenContractError as exc:
                sections, parse_findings = [], [{"code": "document_shape", "detail": str(exc)}]
            verdict = (verify_document(sections, worksheet=worksheet, pack=pack, live_claim_refs=live,
                                       resolve_cell=resolve_cell, resolve_passage=resolve_passage)
                       if sections else {"verdict": "reject", "findings": []})
            findings = parse_findings + verdict["findings"]
            if not findings and sections:
                # The model verifier reads what a regex cannot; it checks and
                # never rewrites.
                checked, _ = _call_json(
                    model, request_id=f"{base_id}:verify:{attempt}",
                    prompt=build_verifier_prompt(sections, worksheet=worksheet, pack=pack),
                    mission=mission, invocations=invocations, purpose=STRL_VERIFIER_PURPOSE,
                    producer_routes=[ref for ref in routes if ref])
                try:
                    model_verdict = parse_verifier_output(checked)
                except InitialScreenContractError as exc:
                    model_verdict = {"verdict": "reject",
                                     "findings": [{"code": "verifier_output", "detail": str(exc)}]}
                findings = model_verdict["findings"]
            verdict = {"verdict": "pass" if not findings and sections else "reject", "findings": findings}
            attempts.append({"attempt": attempt, "verdict": verdict["verdict"],
                             "codes": sorted({str(item.get("code")) for item in findings})})
            if verdict["verdict"] == "pass":
                break
            repair = findings or [{"code": "document_shape", "detail": "no sections"}]
        summary["verification"] = attempts
    except CockpitModelError as exc:
        summary["status"] = lane_status_for(exc, "failed")
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    summary["sections"] = [
        {"title": section["title"], "chars": len(section["body"]),
         "numbers": len(section["numbers"]), "sources": len(section.get("source_refs") or [])}
        for section in sections]
    if verdict["verdict"] != "pass":
        summary["status"] = "failed"
        summary["failure_reason"] = "核验未通过，未发布（不删句子凑过）：" + "、".join(
            sorted({str(item.get("code")) for item in verdict["findings"]}))[:800]
        summary["findings"] = verdict["findings"][:40]
        return summary
    numbers_by_ref, passages_by_ref = pack_resolvers(pack)
    # Rubric H runs on every draft, before publish, and its result travels in
    # the summary whether or not the version is new.
    from .research_quality_score import run_deterministic, strl_artefact
    from .research_quality_rubrics import INITIAL_SCREEN_STRL

    contract = strl_contract_record(pack=pack, worksheet=worksheet, verdict=verdict)
    draft_artefact = strl_artefact(
        ref=f"draft:{base_id}", hash=pack["content_hash"], subject_ref=company_ref,
        sections=sections, contract=contract)
    quality = run_deterministic(draft_artefact, INITIAL_SCREEN_STRL)
    summary["quality"] = {"rubric": INITIAL_SCREEN_STRL.id, "passed": quality["passed"],
                          "failed_checks": quality["failed_checks"]}
    try:
        record = deliverable_authority.publish(
            kind=KIND, subject_ref=company_ref, mission=mission, playbook=playbook,
            template_ref=STRL_TEMPLATE_REF, sections=sections,
            summary=sections[0]["body"][:1200],
            gaps=[f"{item['kind']}：{item['need']}" + (f"（{item['request_ref']}）" if item.get("request_ref") else "")
                  for section in sections for item in section.get("gap_items") or ()],
            model_invocation_refs=invocations,
            actor_ref=mission["autonomy"]["automation_principal"],
            revision=reopen_revision(entry.get("reopen")),
            pack_numbers=numbers_by_ref, pack_passages=passages_by_ref, contract=contract,
            material_pack=pack.get("record"),
        )
    except MissionDeliverableError as exc:
        summary["status"] = "failed"
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    summary["deliverable"] = {"ref": record["deliverable_ref"], "version": record["version"],
                              "version_ref": record["id"], "status": record["status"]}
    summary["formal_authority_writes"] = 1 if record["status"] == "fresh" else 0
    gate = assess_strl_gate(
        playbook=playbook, checklist_entry=entry, sections=record["sections"], verdict=verdict,
        delta_vs_prior=build_delta_vs_prior(
            prior=prior_block, filings=_filings_since(store, company_ref),
            shifted_debates=_shifted_debates(store, company_ref, (prior_block or {}).get("as_of"))),
    )
    summary["gate"] = gate
    try:
        stage = missions.record_stage(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=company_ref, stage_ref="initial_screen",
            status="gate_passed" if gate["passed"] else "gate_failed",
            evidence_refs=[record["id"], mission["id"]],
            rationale=f"P10c 出口门自评（{STRL_CONTRACT_VERSION}）：{gate['rationale']}"[:2000],
            actor_ref=mission["autonomy"]["automation_principal"],
            idempotency_key=f"{mission['id']}:{company_ref}:initial_screen:{record['id']}",
        )
        summary["gate"]["stage_record"] = stage.get("status_marker", "recorded")
    except CoverageMissionError as exc:
        summary["gate"]["stage_record"] = f"not_recorded:{type(exc).__name__}: {exc}"
    summary["quality"]["recorded"] = _record_quality(store, record, mission)
    return summary


def _record_quality(store: DaltonStore, record: Mapping[str, Any],
                    mission: Mapping[str, Any]) -> str:
    """Rubric H's deterministic layer, recorded against the published version."""

    from .research_quality_rubrics import INITIAL_SCREEN_STRL
    from .research_quality_score import (
        QualityScoreAuthority,
        ResearchQualityError,
        artefact_from_deliverable,
        run_deterministic,
    )

    try:
        art = artefact_from_deliverable(record)
        deterministic = run_deterministic(art, INITIAL_SCREEN_STRL, core=store.connection,
                                          mission_ref=mission.get("mission_ref"))
        scored = QualityScoreAuthority(store).record(
            artefact_kind="initial_screen", target_ref=art["ref"], target_hash=art["hash"],
            rubric=INITIAL_SCREEN_STRL, deterministic=deterministic,
            subject_ref=record.get("subject_ref"),
            actor_ref=mission["autonomy"]["automation_principal"])
        return str(scored.get("status") or "recorded")
    except ResearchQualityError as exc:
        return f"not_recorded:{type(exc).__name__}: {exc}"


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION, "created_at": _now(), "status": "succeeded",
        "drafted": None, "sections": [], "skipped": [], "gate": None,
        "formal_authority_writes": 0, "failure_reason": None,
    }
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        deliverable_authority = MissionDeliverableAuthority(store)
        retirements = ClaimRetirementAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary["status"] = "idle"
            summary["failure_reason"] = "no active mission"
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        # Check the grant before spending: drafting eight sections and then
        # being refused at publish costs the mission real model calls.
        if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
            summary["status"] = "held"
            summary["failure_reason"] = (
                f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                "在授权前不起草，免得白花模型调用"
            )
            return summary
        playbook = _playbook(store, mission)
        # P14-S: folded across every version of the mission_ref, not read off
        # the active version. Read per-version, the four Initial Screens that
        # passed under v13 come back as ``entered`` the moment v14 exists, and
        # the selection rule below -- which skips a company whose gate has
        # passed -- would re-draft all four.
        stage_state = missions.stage_state_by_company(mission["mission_ref"])
        stage_rows = evaluate_mission(
            store.connection, mission,
            planned_specs=planned_spec_refs_from_directory(state / "discovery-plans"),
            stage_state=stage_state,
        )
        claims = _live_claims(store, retirements.retired_claim_version_refs())
        published = {
            record["subject_ref"]: record
            for record in deliverable_authority.deliverables(mission["id"], kind=KIND)
        }
        reopens = {
            row["company_ref"]: row
            for row in (
                approved_reopen(store.connection, member["company_ref"])
                for member in mission["universe"]
            )
            if row is not None
        }
        entry, skipped = _target(
            mission=mission, stage_rows=stage_rows, deliverables=published, claims=claims,
            reopens=reopens,
            decisions=None if getattr(args, "legacy_claim_sections", False)
            else _redraft_decisions(state, stage_rows, published),
        )
        summary["skipped"] = skipped
        if entry is None:
            # 2026-10-02 (ws-7d): "nothing to draft" is the right word for a
            # finished queue and the wrong one for four companies that have
            # waited fifteen days on readings that are not arriving.  The
            # second case is a hold with a reason: which items, why each is
            # not arriving, and whether a person has to act.  The owner's
            # to-do list and the parity check read this block.
            blockers = foundation_blockers(
                stage_rows,
                waiting=waiting_since(store.connection, mission["mission_ref"]),
                failures=latest_failures(store.connection, mission["mission_ref"]),
            )
            blocked = {block["company_ref"] for block in blockers}
            waiting_on_foundation = {
                row["company_ref"] for row in skipped
                if str(row.get("reason") or "").startswith("资料底座还没齐")
            }
            blockers = [block for block in blockers if block["company_ref"] in waiting_on_foundation]
            if blockers and waiting_on_foundation <= blocked:
                summary["status"] = "held"
                summary["reason_code"] = FOUNDATION_REASON_CODE
                summary["failure_reason"] = hold_sentence(blockers)
                summary["foundation"] = {
                    "mission_ref": mission["mission_ref"],
                    "mission_version_ref": mission["id"],
                    "companies": blockers,
                }
                return summary
            summary["status"] = "idle"
            summary["failure_reason"] = "nothing to draft"
            return summary
        company_ref = entry["company_ref"]
        summary["drafted"] = {"company_ref": company_ref, "ticker": entry["ticker"]}
        if getattr(args, "legacy_claim_sections", False):
            return _run_legacy_sections(
                args, state=state, store=store, mission=mission, playbook=playbook, entry=entry,
                summary=summary, deliverable_authority=deliverable_authority, missions=missions,
                claims=claims)
        return _run_strl(
            args, state=state, store=store, mission=mission, playbook=playbook, entry=entry,
            summary=summary, deliverable_authority=deliverable_authority, missions=missions,
            claims=claims)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--model-config")
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--scheduler-db")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true", help="pick a target and build prompts, call nothing")
    parser.add_argument("--as-of", help="material pack as-of date (default: today)")
    parser.add_argument("--legacy-claim-sections", action="store_true",
                        help="replay the pre-batch-D per-section Claim drafter")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.model_config:
        parser.error("--model-config is required unless --dry-run")
    summary_dir = Path(args.summary_dir).expanduser().resolve()
    summary_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(args)
    except Exception as exc:  # noqa: BLE001 - the ticket must always carry a reason
        summary = {
            "schema_version": SUMMARY_SCHEMA_VERSION, "created_at": _now(), "status": "failed",
            "failure_reason": f"unexpected {type(exc).__name__}: {exc}",
            "drafted": None, "sections": [], "skipped": [], "gate": None,
            "formal_authority_writes": 0,
        }
    _write_owner_only(summary_dir / "summary.json", summary)
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    # "held" is a governed refusal, not a failure: the ticket should read as a
    # clean run that changed nothing.
    return 0 if summary["status"] in {"succeeded", "idle", "held"} else 1


if __name__ == "__main__":
    sys.exit(main())
