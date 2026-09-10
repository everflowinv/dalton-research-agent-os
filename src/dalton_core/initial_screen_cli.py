"""P10c child: draft one company's Initial Screen and decide its gate.

Runs out of process, like every other lane that calls a model, so a section
that takes a minute cannot stall the writer or the controller tick.  One run
handles at most one company:

1. pick the highest-priority company whose Initial Screen is entered, not yet
   passed, and either undrafted or older than its newest live Claim;
2. draft each templated section through the mission's own budgeted model
   route (the valuation section is never drafted: no market-data authority);
3. publish the document through the deliverable authority, which refuses any
   figure that no quantitative Claim accounts for;
4. assess the Playbook's four gate questions by structural checks and record
   ``gate_passed`` or ``gate_failed`` in the mission's stage ledger.

Every step is idempotent: a re-run with nothing new to say publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .claim_retirement import ClaimRetirementAuthority
from .deliverable_reopen import CHANGE_REASON_EVIDENCE, approved_reopen
from .cockpit_model import CockpitModel, CockpitModelError, lane_status_for
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .initial_screen import (
    KIND,
    SECTION_GUIDANCE,
    TEMPLATE_KEY,
    VALUATION_GAP,
    VALUATION_TITLE_HINT,
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
from .research_quality_score import residual_citation_artefacts
from .mission_stage import evaluate_mission, planned_spec_refs_from_directory
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
        if (
            reopen is None
            and published is not None
            and published["created_at"] >= own[-1]["created_at"]
        ):
            skipped.append({"company_ref": company_ref, "reason": "nothing new since the last version"})
            continue
        return {**entry, "reopen": reopen}, skipped
    return None, skipped


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


def _correction_note(stray: list[str], residue: list[dict[str, Any]]) -> str:
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
    return "".join(notes)


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
        stage_state: dict[str, dict[str, list[str]]] = {}
        for record in missions.stage_records(mission["id"]):
            stage_state.setdefault(record["company_ref"], {}).setdefault(record["stage_ref"], []).append(
                record["status"]
            )
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
        )
        summary["skipped"] = skipped
        if entry is None:
            summary["status"] = "idle"
            summary["failure_reason"] = "nothing to draft"
            return summary
        company_ref = entry["company_ref"]
        summary["drafted"] = {"company_ref": company_ref, "ticker": entry["ticker"]}
        titles = section_titles(playbook)
        context = build_claim_context(claims.get(company_ref) or [])
        summary["context"] = {
            "claims": len(context["claims"]), "numbers": len(context["numbers"]),
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
        sections: list[dict[str, Any]] = []
        invocations: list[str] = []
        for index, title in enumerate(titles):
            guidance = SECTION_GUIDANCE[index] if index < len(SECTION_GUIDANCE) else ""
            if VALUATION_TITLE_HINT in title and not guidance:
                sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                                 "gaps": [VALUATION_GAP]})
                summary["sections"].append({"title": title, "status": "not_drafted", "reason": "valuation gate"})
                continue
            prompt = build_section_prompt(
                title=title, guidance=guidance,
                company={"ticker": entry["ticker"], "company_ref": company_ref},
                mission=mission, context=context, checklist=entry["items"],
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
            retried = False
            if stray or residue:
                retried = True
                try:
                    correction = model.call(
                        purpose="draft",
                        request_id=f"{mission['id']}:{company_ref}:{KIND}:{index}:"
                                   f"{len(claims.get(company_ref) or [])}:retry",
                        prompt=prompt + _correction_note(stray, residue),
                        mission=mission,
                    )
                    candidate = parse_section_output(correction["text"], context=context, title=title)
                    if correction.get("invocation_ref"):
                        invocations.append(correction["invocation_ref"])
                    candidate_stray = unsourced_numbers(candidate["body"], candidate["numbers"])
                    candidate_residue = _actionable_residue(
                        raw_section_body(correction["text"]), candidate["body"])
                    if candidate["body"] and not candidate_stray and not candidate_residue:
                        section = candidate
                        stray, residue = [], []
                    else:
                        stray = candidate_stray or stray
                        residue = candidate_residue or residue
                except CockpitModelError as exc:
                    summary["sections"].append({
                        "title": title,
                        "status": lane_status_for(exc, "retry_failed"),
                        "reason": str(exc)})
            if stray or residue:
                section = _dropped_section(title, stray, residue)
            sections.append(section)
            summary["sections"].append({
                "title": title,
                "status": ("drafted" if section["body"]
                           else ("dropped_unsourced" if stray else "dropped_residual_citation")),
                "retried": retried,
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
        gate = assess_exit_gate(playbook=playbook, checklist_entry=entry, sections=record["sections"])
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
