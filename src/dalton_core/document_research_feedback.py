"""Feed exact directed-reading outcomes back into the next research plan.

These are execution observations, not investment evidence. In particular,
an unsuccessful query does not establish absence of information in a source.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .store import content_hash


def _read_observations(connection: Any, mission_version_ref: str):
    from .mission_document_research_executor import read_mission_document_research_observations
    from .directed_numeric_executor import numeric_feedback
    return (read_mission_document_research_observations(connection, mission_version_ref=mission_version_ref)
            + numeric_feedback(connection, mission_version_ref))


def read_document_research_feedback(core: Any, mission: Mapping[str, Any]) -> dict[str, Any]:
    companies = {item["company_ref"] for item in mission["universe"]}
    try:
        observations = _read_observations(core.connection, mission["id"])
        resolved = {(item['company_ref'], content_hash(binding))
                    for item in observations if item.get('outcome') == 'numeric_targets_completed'
                    for binding in item.get('resolved_numeric_rejections') or ()}
        grouped = {ref: [] for ref in sorted(companies)}
        from .directed_numeric_period_replacement import superseding_admission
        from .mission_document_research import MissionDocumentResearchAuthority
        replacements = {}
        numeric_successors = {}
        for observation in observations:
            ref=observation.get('admission_ref')
            if ref and ref not in replacements:
                row=core.connection.execute('SELECT * FROM mission_document_research_admissions WHERE admission_id=?',(ref,)).fetchone()
                replacements[ref]=(superseding_admission(core.connection,
                    MissionDocumentResearchAuthority._read_row(None,row)) if row is not None else None)
            replacement=replacements.get(ref)
            if replacement is not None:
                observation={**observation,'original_outcome':observation['outcome'],
                    'outcome':'numeric_period_replacement_admitted',
                    'replacement_admission_ref':replacement,
                    'meaning':'The original failed period task is superseded by one normal-plan admission; the new admission still requires complete execution and verification. Original results and fees are unchanged.'}
            from .directed_numeric_research import numeric_rejection
            rejection=numeric_rejection(core.connection,observation["admission_ref"]) if observation.get("admission_ref") else None
            if rejection is not None and (observation['company_ref'], content_hash(
                    {k:rejection[k] for k in ('admission_ref','admission_hash','rejection_ref','rejection_hash')})) not in resolved:
                ref=rejection['admission_ref']
                if ref not in numeric_successors:
                    from .numeric_capability_supersession import successor_for
                    try:numeric_successors[ref]=(successor_for(core.connection,rejection,mission=mission),None)
                    except Exception as exc:numeric_successors[ref]=(None,type(exc).__name__)
                successor,unproved=numeric_successors[ref]
                if successor is None:
                    observation={**observation,"numeric_strategy_required":True,
                        "replaces_numeric_rejection":{k:rejection[k] for k in ("admission_ref","admission_hash","rejection_ref","rejection_hash")}}
                    if unproved:
                        observation={**observation,'numeric_upgrade_supersession_status':'unverified',
                            'numeric_upgrade_supersession_reason':unproved}
                else:
                    observation={k:v for k,v in observation.items() if k not in ('numeric_strategy_required','replaces_numeric_rejection')}
                    observation={**observation,'numeric_upgrade_status':'superseded_by_admitted_numeric_read',
                        'numeric_upgrade_supersession':successor}
            if (observation.get("mission_version_ref") != mission["id"]
                    or observation.get("company_ref") not in companies):
                raise ValueError("directed-reading feedback escaped its mission/company")
            if observation["outcome"]=="query_miss" and observation.get("admission_ref"):
                import json
                row=core.connection.execute("SELECT record_json FROM mission_document_research_admissions WHERE admission_id=?",(observation["admission_ref"],)).fetchone()
                admission=json.loads(row["record_json"])
                if admission.get("numeric_authority") is not None:
                    observation={**observation,"numeric_targets":admission["numeric_authority"]["targets"],
                                 "numeric_strategy_status":"query_miss", "meaning":"The bounded numeric query did not match; the original may still contain the required facts."}
            # Keep the validated observation identity with all attempted terms
            # and missing evidence, rather than reducing it to a success count.
            grouped[observation["company_ref"]].append(dict(observation))
        from .document_execution_failure import failure_from_observations
        from .mission_document_research import MissionDocumentResearchAuthority
        for ref in sorted({item['admission_ref'] for item in observations
                           if item.get('outcome') == 'recovery_required'}):
            if replacements.get(ref) is not None:
                continue
            row = core.connection.execute(
                'SELECT * FROM mission_document_research_admissions WHERE admission_id=?', (ref,)).fetchone()
            if row is None:
                raise ValueError('document failure admission unavailable')
            admission = MissionDocumentResearchAuthority._read_row(None, row)
            failure = failure_from_observations(core.connection, admission, observations)
            if failure is not None:
                if failure.get('numeric_failure_diagnostics', {}).get('unexecutable_period_targets'):
                    from .directed_numeric_period_replacement import binding_for, consumer
                    binding = binding_for(core.connection, ref)
                    if binding is not None and consumer(core.connection, binding) is None:
                        failure = {**failure, 'replaces_numeric_contract_failure': binding}
                grouped[admission['company_ref']].append(failure)
        from .directed_numeric_period_replacement import historical_feedback
        for failure in historical_feedback(core, mission):
            grouped[failure['company_ref']].append(failure)
        body = {"status": "available", "by_company": grouped}
    except Exception as exc:
        body = {"status": "unavailable", "by_company": {}, "reason": type(exc).__name__}
    # A source lookup failure must not erase previously proved execution feedback.
    from .numeric_source_admission_feedback import read_numeric_source_feedback
    try:
        source_feedback = read_numeric_source_feedback(core, mission)
    except Exception as exc:
        source_feedback = {"status": "unverified", "records": [],
                           "unverified": [{"reason": type(exc).__name__}]}
    if source_feedback is not None:
        body["admission_source_feedback_status"] = {
            key: source_feedback[key] for key in ("status", "unverified")}
        if body["status"] == "available":
            for item in source_feedback["records"]:
                body["by_company"][item["company_ref"]].append(item)
    return {**body, "content_hash": content_hash(body)}


def document_research_feedback_signature(core: Any) -> str:
    """Wake the planner even when a query wrote no new Claim or document."""
    import json
    try:
        missions = core.connection.execute(
            "SELECT v.record_json FROM coverage_mission_versions v "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=v.mission_version_id "
            "ORDER BY v.mission_version_id")
        body = [read_document_research_feedback(core, json.loads(row["record_json"]))
                for row in missions]
        from .numeric_source_admission_feedback import ADMISSION_OUTCOMES, semantic_feedback
        for feedback in body:
            if "admission_source_feedback_status" not in feedback:
                continue  # preserve the signature of historical feedback unchanged
            feedback.pop("content_hash", None)
            status = feedback["admission_source_feedback_status"]
            status["unverified"] = [semantic_feedback(item) for item in status["unverified"]]
            for company, rows in feedback.get("by_company", {}).items():
                feedback["by_company"][company] = [semantic_feedback(item)
                    if item.get("outcome") in ADMISSION_OUTCOMES else item for item in rows]
    except Exception as exc:
        body = {"status": "unavailable", "reason": type(exc).__name__}
    return content_hash(body)


# 2026-10-01: an owner withdrawal, or an admission reaching a terminal
# outcome, is a change in the research state.  Live 2026-09-30 21:05 the owner
# withdrew 27 unrecoverable admissions in legacy (10 in ws-7d) so the planner
# could ask those questions again under the current budget and contracts.
# Nothing the planner reads moved: the withdrawal table was in neither the
# coordinator's wake signature nor the planning hash, 25 of the 27 belonged to
# earlier mission versions the per-version feedback never shows, and the two
# that did still read ``recovery_required`` -- "execution is blocked".  Legacy
# held ``no_material_change`` and its two-hourly plans never re-asked one.
SETTLEMENT_RULE_REF = "rule:document-research-settlement:0.1"
# Reopened inquiries a company carries in full, newest withdrawal first.  The
# rest are a count: one equivalence class per row already folds the live
# rephrasings ("DXC 970" eleven times) into one.
REOPENED_KEEP_PER_COMPANY = 4
REOPENED_QUESTION_CHARS = 300


def _admission_rows(connection: Any, companies: set[str]) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT admission_id, company_ref, mission_version_ref, record_json, created_at "
            "FROM mission_document_research_admissions ORDER BY created_at, admission_id"
        ).fetchall()
    except Exception as exc:  # a Core from before directed reads has none
        if "no such table" in str(exc).lower():
            return []
        raise
    import json
    result = []
    for admission_id, company_ref, version_ref, record_json, created_at in (
            tuple(row) for row in rows):
        if company_ref not in companies:
            continue
        record = json.loads(record_json)
        if not isinstance(record, Mapping) or record.get("id") != admission_id:
            raise ValueError("stored admission escaped its identity")
        result.append({"id": admission_id, "company_ref": company_ref,
                       "mission_version_ref": version_ref, "created_at": created_at,
                       "planner_inquiry": record.get("planner_inquiry")})
    return result


def _settled_and_withdrawn(connection: Any, refs: list[str]) -> tuple[dict[str, str], dict[str, Any]]:
    from .mission_document_research import _settlement_instants
    from .mission_document_research_withdrawal import read_withdrawals

    wanted = set(refs)
    withdrawn = {ref: row for ref, row in read_withdrawals(connection).items() if ref in wanted}
    return _settlement_instants(connection, list(refs)), withdrawn


def _settlement_body(settled: Mapping[str, str],
                     withdrawn: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """What is done with, by exact ref: monotone, so each change is one event."""

    return {
        "rule_ref": SETTLEMENT_RULE_REF,
        "withdrawn": sorted(f"{ref}={row['id']}" for ref, row in withdrawn.items()),
        "settled": sorted(ref for ref in settled if ref not in withdrawn),
    }


def _reason_code(reason: Any) -> str:
    text = str(reason or "")
    if text.startswith("unrecoverable:"):
        return text.split(":", 2)[1].strip()[:80]
    return "owner_withdrawal"


def read_document_research_settlement(
    core: Any, mission: Mapping[str, Any], *, as_of: str | None = None,
) -> dict[str, Any]:
    """Which of this mission's directed reads are done with, and what is open again.

    ``settlement_hash`` binds the exact withdrawn and settled admission refs of
    the mission's companies (every mission version: the equivalence gate is
    per company, not per version).  It only grows, so it moves once per
    settlement and never on a tick where nothing settled.

    ``reopened_by_company`` lists withdrawn inquiries the gate would admit
    again now: one per equivalence class (``EQUIVALENT_INQUIRY_RULE_REF``),
    and none whose equivalent is in flight or answered inside the cooldown --
    the gate would refuse those, so offering them would only buy a refusal.
    """

    from datetime import datetime, timezone

    from .document_inquiry_equivalence import (
        ANSWERED_COOLDOWN, EQUIVALENT_INQUIRY_RULE_REF, inquiry_signature_for,
        signatures_equivalent,
    )

    companies = {item["company_ref"] for item in mission["universe"]}
    try:
        now = (datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        admissions = _admission_rows(core.connection, companies)
        settled, withdrawn = _settled_and_withdrawn(
            core.connection, [item["id"] for item in admissions])
        signatures = {}
        for item in admissions:
            inquiry = item["planner_inquiry"]
            if isinstance(inquiry, Mapping) and isinstance(inquiry.get("question"), str):
                signatures[item["id"]] = inquiry_signature_for(item["company_ref"], inquiry)

        def blocked(signature: Any) -> bool:
            for other in admissions:
                if other["id"] in withdrawn or other["id"] not in signatures:
                    continue
                if signatures_equivalent(signature, signatures[other["id"]]) is None:
                    continue
                at = settled.get(other["id"])
                if at is None:
                    return True
                moment = datetime.fromisoformat(at)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if now - moment < ANSWERED_COOLDOWN:
                    return True
            return False

        reopened: dict[str, list[dict[str, Any]]] = {ref: [] for ref in sorted(companies)}
        classes: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        newest_first = sorted(
            (item for item in admissions if item["id"] in withdrawn and item["id"] in signatures),
            key=lambda item: (withdrawn[item["id"]]["created_at"], item["created_at"], item["id"]),
            reverse=True)
        for item in newest_first:
            signature = signatures[item["id"]]
            seen = classes.setdefault(item["company_ref"], [])
            match = next((row for sig, row in seen
                          if signatures_equivalent(signature, sig) is not None), None)
            if match is not None:
                match["equivalent_withdrawn"] += 1
                continue
            inquiry = item["planner_inquiry"]
            strategy = inquiry.get("directed_document")
            row = {
                "admission_ref": item["id"],
                "mission_version_ref": item["mission_version_ref"],
                "question": inquiry["question"][:REOPENED_QUESTION_CHARS],
                "document_ref": (strategy.get("document_ref")
                                 if isinstance(strategy, Mapping) else None),
                "withdrawn_at": withdrawn[item["id"]]["created_at"],
                "withdrawn_because": _reason_code(withdrawn[item["id"]].get("reason")),
                "equivalent_withdrawn": 0,
            }
            seen.append((signature, row))
            if not blocked(signature):
                reopened[item["company_ref"]].append(row)
        by_company = {}
        for ref, rows in reopened.items():
            by_company[ref] = {
                "reopened": rows[:REOPENED_KEEP_PER_COMPANY],
                "reopened_omitted": max(0, len(rows) - REOPENED_KEEP_PER_COMPANY),
            }
        body = {
            "status": "available",
            "equivalence_rule_ref": EQUIVALENT_INQUIRY_RULE_REF,
            "admissions": len(admissions),
            "open": sum(1 for item in admissions
                        if item["id"] not in settled and item["id"] not in withdrawn),
            "settled": sum(1 for ref in settled if ref not in withdrawn),
            "withdrawn": len(withdrawn),
            "settlement_hash": content_hash(_settlement_body(settled, withdrawn)),
        }
    except Exception as exc:  # disclosed, never invented
        body = {"status": "unavailable", "reason": type(exc).__name__,
                "settlement_hash": None}
        by_company = {}
    return {**body, "by_company": by_company}


def document_research_settlement_signature(core: Any) -> str:
    """Wake the planner when an admission is withdrawn or settles.

    Every admission in the Core, not one mission's: this is the coordinator's
    cheap "has anything moved" proxy, and a settlement anywhere is a reason to
    let the child look.
    """

    try:
        refs = [tuple(row)[0] for row in core.connection.execute(
            "SELECT admission_id FROM mission_document_research_admissions")]
        settled, withdrawn = _settled_and_withdrawn(core.connection, refs)
        body: Any = _settlement_body(settled, withdrawn)
    except Exception as exc:  # noqa: BLE001 - an absent table is "nothing settled"
        body = ({"rule_ref": SETTLEMENT_RULE_REF, "withdrawn": [], "settled": []}
                if "no such table" in str(exc).lower()
                else {"status": "unavailable", "reason": type(exc).__name__})
    return content_hash(body)
