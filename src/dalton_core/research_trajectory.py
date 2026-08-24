"""Disposable research-event projection for the owner-only Dalton Cockpit.

The projection is rebuilt from already validated authority records.  It is not
persisted, cannot be submitted back to Core, and deliberately exposes missing
authority as a gap instead of inventing an Agenda, plan, WorkOrder, or brief.
"""

from __future__ import annotations

from typing import Any, Mapping

from .store import content_hash


def _exact_ref(
    kind: str,
    ref: str,
    hash_value: str,
    *,
    authority: bool = True,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ref": ref,
        "hash": hash_value,
        "authority": authority,
    }


def _packet_ref(packet: Mapping[str, Any]) -> dict[str, Any]:
    return _exact_ref("review_packet", packet["id"], packet["content_hash"])


def _fragment_ref(
    kind: str, ref: str, fragment: Mapping[str, Any]
) -> dict[str, Any]:
    return _exact_ref(kind, ref, content_hash(fragment), authority=False)


def _node(
    ordinal: int,
    stage: str,
    label: str,
    status: str,
    summary: str,
    refs: list[dict[str, Any]],
    *,
    collapsed_count: int = 0,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "stage": stage,
        "label": label,
        "status": status,
        "summary": summary,
        "exact_refs": refs,
        "collapsed_count": collapsed_count,
    }


def build_research_trajectory_projection(
    *,
    packet: Mapping[str, Any],
    manifest: Mapping[str, Any],
    transcript_state: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build one deterministic projection without creating mutable state."""

    packet_exact = _packet_ref(packet)
    source = packet["source"]
    targets = packet["research_targets"]
    correction = packet["proposed_correction_set"]
    packet_claim = packet["candidate_claim"]
    correction_set = transcript_state.get("correction_set")
    binding = transcript_state.get("citation_binding")
    claim_eligible = transcript_state.get("claim_eligible") is True

    nodes: list[dict[str, Any]] = []
    nodes.append(_node(
        1,
        "agenda",
        "Agenda",
        "unrecorded",
        "该 acquire-only packet 未绑定 Agenda decision；轨迹不补造。",
        [packet_exact],
    ))

    target_refs = [
        _fragment_ref("research_target", item["target_ref"], item)
        for item in targets
    ]
    nodes.append(_node(
        2,
        "research_question",
        "研究问题",
        "partial" if target_refs else "unrecorded",
        (
            f"review packet 冻结了 {len(target_refs)} 个研究目标，但没有独立的 ResearchQuestionVersion。"
            if target_refs
            else "packet 没有记录 ResearchQuestionVersion 或研究目标。"
        ),
        [packet_exact, *target_refs],
        collapsed_count=max(0, len(target_refs) - 2),
    ))
    nodes.append(_node(
        3,
        "planning",
        "Plan / Work Order",
        "unrecorded",
        "该 packet 没有 PlanRound、WorkOrder 或 WorkflowRun authority；轨迹不从结果反推计划。",
        [packet_exact],
    ))

    page_refs: list[dict[str, Any]] = []
    for page in manifest["pages"]:
        page_refs.extend([
            _exact_ref(
                "connector_invocation",
                page["connector_invocation_ref"],
                page["connector_invocation_hash"],
            ),
            _exact_ref(
                "raw_page_artifact",
                page["raw_artifact_version_ref"],
                page["raw_artifact_version_hash"],
            ),
            _exact_ref(
                "source_envelope",
                page["source_envelope_ref"],
                page["source_envelope_hash"],
            ),
        ])
    manifest_ref = _exact_ref(
        "source_manifest", manifest["id"], manifest["content_hash"]
    )
    nodes.append(_node(
        4,
        "connector",
        "AlphaEngine connector",
        "complete",
        (
            f"{len(manifest['pages'])} 页采集完成；{manifest['physical_calls']} 次物理调用，"
            f"原始响应共 {manifest['total_raw_response_bytes']} bytes。"
        ),
        [manifest_ref, *page_refs],
        collapsed_count=len(page_refs),
    ))
    raw_ref = _exact_ref(
        "raw_document",
        source["document_ref"],
        manifest["assembled_object"]["content_hash"],
    )
    nodes.append(_node(
        5,
        "raw_artifact",
        "Raw transcript artifact",
        "complete",
        f"完整原文 {source['content_chars']} 字；页面已按 manifest 连续拼接并核对 SHA-256。",
        [raw_ref, manifest_ref],
    ))

    correction_refs = [
        packet_exact,
        _fragment_ref(
            "proposed_correction_set",
            correction["correction_set_ref"],
            correction,
        ),
    ]
    correction_status = "pending"
    correction_summary = (
        f"等待人工核对 {len(correction['corrections'])} 个 ASR 标记；"
        f"其中 {correction['unresolved_overlap_with_formal_citation']} 个与正式引用重叠。"
    )
    if isinstance(correction_set, Mapping):
        correction_refs.append(_exact_ref(
            "correction_set",
            correction_set["id"],
            correction_set["content_hash"],
        ))
        correction_status = "complete"
        correction_summary = (
            f"人工修正集已发布；保留 {len(correction['corrections'])} 个目标标记的明确 disposition。"
        )
    nodes.append(_node(
        6,
        "transcript_correction",
        "Transcript correction review",
        correction_status,
        correction_summary,
        correction_refs,
    ))

    citation_refs = [packet_exact]
    if isinstance(binding, Mapping):
        citation_refs.append(_exact_ref(
            "citation_binding", binding["id"], binding["content_hash"]
        ))
        citation_status = "complete" if claim_eligible else "blocked"
        citation_summary = (
            "raw span citation 已绑定 exact correction set，Claim admission gate 可继续。"
            if claim_eligible
            else "citation binding 已写入，但仍与未决修正重叠，Claim admission 继续阻断。"
        )
    elif correction_set is not None:
        citation_status = "pending"
        citation_summary = "修正集已发布，尚未生成 raw-span citation binding。"
    else:
        citation_status = "blocked"
        citation_summary = "必须先完成 authenticated human correction review。"
    nodes.append(_node(
        7,
        "citation_binding",
        "Claim citation binding",
        citation_status,
        citation_summary,
        citation_refs,
    ))

    staged = candidate is not None
    candidate_refs = [
        packet_exact,
        _fragment_ref(
            "packet_candidate_claim",
            packet_claim["candidate_claim_ref"],
            packet_claim,
        ),
    ]
    decision = None
    commit_event = None
    commit_state = None
    if staged:
        claim = candidate["claim"]
        evidence = candidate["evidence"]
        decision = candidate.get("decision")
        commit_event = candidate.get("commit_event")
        commit_state = candidate.get("commit_state")
        candidate_refs.extend([
            _exact_ref("candidate_evidence", evidence["id"], evidence["content_hash"]),
            _exact_ref("candidate_claim", claim["id"], claim["content_hash"]),
        ])
        candidate_status = "complete"
        candidate_summary = "候选 Evidence 与 Claim 已进入 staging，并绑定 exact source lineage。"
    elif claim_eligible:
        candidate_status = "pending"
        candidate_summary = "citation 已具备资格，候选 Evidence / Claim 尚未进入 staging。"
    else:
        candidate_status = "blocked"
        candidate_summary = "packet 内只有待准入 Claim；citation gate 通过前不能进入候选 staging。"
    nodes.append(_node(
        8,
        "candidate",
        "候选 Evidence / Claim",
        candidate_status,
        candidate_summary,
        candidate_refs,
    ))

    review_refs = [packet_exact]
    if isinstance(decision, Mapping):
        review_refs.append(_exact_ref(
            "human_review", decision["id"], decision["content_hash"]
        ))
        verdict = decision["verdict"]
        if verdict == "accept":
            review_status = "complete" if commit_state == "committed" else (
                "failed" if commit_state == "failed" else "queued"
            )
            review_summary = (
                "人工已接受候选；正式 Ledger 写入完成。"
                if commit_state == "committed"
                else "人工已接受候选；正式 Ledger 写入尚未完成。"
            )
        elif verdict == "revise":
            review_status = "blocked"
            review_summary = "人工要求修订；当前 candidate version 不得进入正式 Ledger。"
        else:
            review_status = "rejected"
            review_summary = "人工已拒绝候选；拒绝事件保留在轨迹中。"
    elif staged:
        review_status = "pending"
        review_summary = "候选已就绪，等待 accept / revise / reject。"
    else:
        review_status = "blocked"
        review_summary = "候选尚未进入 staging，人工 Claim review 不能开始。"
    nodes.append(_node(
        9,
        "human_review",
        "人工 Claim review",
        review_status,
        review_summary,
        review_refs,
    ))

    formal_refs = [packet_exact]
    if isinstance(commit_event, Mapping):
        formal_refs.append(_exact_ref(
            "review_commit_event",
            commit_event["id"],
            commit_event["content_hash"],
        ))
    if commit_state == "committed":
        formal_status = "complete"
        ledger = commit_event.get("ledger_result", {}) if commit_event else {}
        formal_summary = (
            "正式 Evidence / Claim 已原子写入；"
            f"receipt 绑定 {ledger.get('evidence_version_ref')} 与 {ledger.get('claim_version_ref')}。"
        )
    elif commit_state == "failed":
        formal_status = "failed"
        formal_summary = "Ledger 写入失败；candidate 与人工 decision 仍保留，可由 reconciler 重试。"
    else:
        formal_status = "blocked"
        formal_summary = "正式 Evidence / Claim 只能由 accepted human review 经 scoped writer 写入。"
    nodes.append(_node(
        10,
        "formal_ledger",
        "正式 Evidence / Claim",
        formal_status,
        formal_summary,
        formal_refs,
    ))

    brief_status = "pending" if commit_state == "committed" else "blocked"
    brief_summary = (
        "正式 Claim 已就绪，但 US IT Services brief v3 尚未记录为 exact snapshot artifact。"
        if brief_status == "pending"
        else "brief v3 必须等待正式 Claim；packet 中的 brief verdict 只作候选研究目标。"
    )
    nodes.append(_node(
        11,
        "brief",
        "US IT Services brief v3",
        brief_status,
        brief_summary,
        [packet_exact],
    ))

    if commit_state == "committed":
        state = "awaiting_brief"
    elif isinstance(decision, Mapping) and decision.get("verdict") == "reject":
        state = "review_rejected"
    elif staged:
        state = "awaiting_candidate_review"
    elif claim_eligible:
        state = "awaiting_candidate_staging"
    else:
        state = "awaiting_transcript_review"

    identity_hash = content_hash({
        "packet_ref": packet["id"],
        "packet_hash": packet["content_hash"],
    })
    projection = {
        "schema_version": "0.1",
        "id": f"research-trajectory-projection:{identity_hash}",
        "source_packet_ref": packet["id"],
        "source_packet_hash": packet["content_hash"],
        "subject_ref": packet_claim["subject_ref"],
        "title": source["title"],
        "period": packet_claim["period"],
        "state": state,
        "projection_only": True,
        "admission_effect": False,
        "nodes": nodes,
    }
    projection["content_hash"] = content_hash(projection)
    return projection


__all__ = ["build_research_trajectory_projection"]
