"""Bounded maintenance of one question authority, without model or search calls."""
from __future__ import annotations
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from .store import canonical_json, content_hash
from .research_question_backlog import ResearchQuestionBacklog, read_exact_backlog_question_version

OPERATIONS_PATTERN = re.compile(r"\b(?:gateway|socket|launchd|provider outage|lane status|pipeline status|restart service)\b|服务状态|系统状态|重启服务|网关故障", re.I)

def migrate_legacy_shadow(store, backlog, active):
    if not active or not store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='research_question_versions'").fetchone():
        return []
    permitted={i["company_ref"] for m in active for i in m["universe"]} | {m["industry_ref"] for m in active}
    migrated=[]
    rows=store.connection.execute("SELECT v.* FROM research_question_versions v LEFT JOIN backlog_legacy_question_links l ON l.legacy_version_ref=v.version_id WHERE l.legacy_version_ref IS NULL ORDER BY v.created_at,v.version_id").fetchall()
    for row in rows:
        legacy=json.loads(row["record_json"])
        if (canonical_json(legacy)!=row["record_json"] or legacy.get("content_hash")!=row["content_hash"]
                or content_hash({k:v for k,v in legacy.items() if k!='content_hash'})!=row["content_hash"]):
            raise ValueError("legacy shadow question hash drifted")
        if any(legacy.get(k) != row[k] for k in ("company_ref", "question", "answer_criteria", "question_ref", "actor_ref", "created_at")):
            raise ValueError("legacy question indexed authority drifted")
        company=legacy["company_ref"]
        canonical_ref=None
        reason="已超出当前研究目标的公司范围，历史候选归档并保留原始记录。"
        if company in permitted:
            matches=store.connection.execute("SELECT question_ref FROM backlog_question_versions WHERE company_ref=? AND question=? AND answer_criteria=? AND source_refs_json=? ORDER BY created_at,version_id LIMIT 1", (company,row["question"],row["answer_criteria"],row["source_refs_json"])).fetchall()
            if matches:
                canonical_ref=matches[0][0]
                backlog.question(canonical_ref)
                reason="历史候选已链接统一研究问题状态，旧版本仅作为可复核记录保留。"
            else:
                reason="历史候选尚未形成可执行的当前研究问题，结束等待；新的研究计划须按当前资料与预算重新安排。"
        body={"schema_version":"legacy-question-migration-v1","legacy_version_ref":row["version_id"],
              "legacy_version_hash":row["content_hash"],"canonical_question_ref":canonical_ref,
              "disposition":"superseded" if canonical_ref else "retired","reason":reason,
              "active_mission_hashes":[m["content_hash"] for m in active],
              "actor_ref":"automation:research-question-maintenance"}
        wire={**body,"content_hash":content_hash(body)}
        with store._transaction() as cur:
            cur.execute("INSERT INTO backlog_legacy_question_links VALUES(?,?,?,?)",(row["version_id"],canonical_ref,canonical_json(wire),wire["content_hash"]))
        migrated.append(wire)
    return migrated

def maintain_questions(store, *, staging=None, now=None, limit=250):
    from .coverage_mission import CoverageMissionAuthority
    moment = now or datetime.now(timezone.utc)
    missions = CoverageMissionAuthority(store)
    backlog = ResearchQuestionBacklog(store)
    active = []
    for row in store.connection.execute("SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"):
        active.append(missions.mission(row[0]))
    legacy_migration = migrate_legacy_shadow(store, backlog, active)
    scopes = {}
    for mission in active:
        from .agenda import read_exact_mandate_version
        mandate_ref = read_exact_mandate_version(store.connection, mission["bindings"]["mandate_version"]["ref"])["mandate_ref"]
        scopes.setdefault(mandate_ref, set()).update(i["company_ref"] for i in mission["universe"])
    retired = []
    rows = store.connection.execute("SELECT p.question_ref,p.version_id FROM backlog_question_pointer p WHERE (SELECT e.state FROM backlog_question_events e WHERE e.question_ref=p.question_ref ORDER BY e.event_seq DESC LIMIT 1) NOT IN ('answered','blocked','retired') ORDER BY p.question_ref LIMIT ?", (limit,)).fetchall()
    for row in rows[:limit]:
        question = backlog.question(row["question_ref"])
        if question["state"] in {"answered", "blocked", "retired"}:
            continue
        head = question["head"]
        operational = bool(OPERATIONS_PATTERN.search(head["question"]))
        # The active mission's exact mandate version bounds company scope.
        permitted_subjects = set().union(*scopes.values()) | {m["industry_ref"] for m in active}
        out_of_scope = bool(active) and head["company_ref"] not in permitted_subjects
        if not operational and not out_of_scope:
            continue
        # Do not close a question while a Scheduler lease still owns its work.
        running = store.connection.execute("SELECT 1 FROM scheduler_work_orders w JOIN scheduler_attempt_events e ON e.work_order_id=w.work_order_id "
            "JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
            "WHERE w.work_order_json LIKE ? AND e.state='leased' AND l.expires_at>? "
            "AND e.event_seq=(SELECT MAX(e2.event_seq) FROM scheduler_attempt_events e2 WHERE e2.work_order_id=w.work_order_id) LIMIT 1",
            ("%"+row["question_ref"]+"%", moment.isoformat())).fetchone() if store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_work_orders'").fetchone() else None
        if running:
            continue
        reason = "转交运行维护，系统状态问题不作为研究结论" if operational else "已超出当前研究目标的公司范围，归档并保留历史"
        with store._transaction() as cur:
            current = backlog._head(cur, row["question_ref"])
            if current["content_hash"] != head["content_hash"]:
                continue
            event = backlog._event(cur, row["question_ref"], "retired", reason,
                {"maintenance_kind": "operations" if operational else "outside_scope",
                 "question_version_ref": head["id"], "question_version_hash": head["content_hash"],
                 "active_mission_hashes": [m["content_hash"] for m in active],
                 "requestor_terminal_reason": reason}, "automation:research-question-maintenance")
            retired.append(event)
    waiting_requests = []
    if store.path != ":memory:":
        from .material_requests import open_request
        pending = store.connection.execute("SELECT p.question_ref,p.version_id FROM backlog_question_pointer p LEFT JOIN backlog_material_request_links l ON l.question_version_ref=p.version_id WHERE l.question_version_ref IS NULL AND NOT EXISTS(SELECT 1 FROM backlog_answer_bindings b WHERE b.question_ref=p.question_ref) AND (SELECT state FROM backlog_question_events e WHERE e.question_ref=p.question_ref ORDER BY event_seq DESC LIMIT 1) NOT IN ('answered','blocked','retired') ORDER BY p.question_ref LIMIT ?", (limit,)).fetchall()
        for row in pending:
            head = read_exact_backlog_question_version(store.connection, row["version_id"])
            request = open_request(Path(store.path).parent,
                requester={"lane": "research_question", "product_ref": row["question_ref"], "run_ref": head["id"], "section": "answer"},
                subject=head["company_ref"], need={"kind": "answer", "question": head["question"]},
                source_priority=["indexed_corpus"], acceptance={"answer_criteria": head["answer_criteria"], "question_version_hash": head["content_hash"]},
                budget={"max_attempts": 0, "max_cost_usd": 0}, actor_ref="automation:research-question-maintenance")
            body = {"question_version_ref": head["id"], "question_version_hash": head["content_hash"], "request_ref": request["id"],
                    "reason": "等待符合回答标准的持有资料；新付费检索须另行授权。", "actor_ref": "automation:research-question-maintenance"}
            wire = {**body, "content_hash": content_hash(body)}
            with store._transaction() as cur:
                cur.execute("INSERT INTO backlog_material_request_links VALUES(?,?,?,?)", (head["id"], request["id"], canonical_json(wire), wire["content_hash"]))
            waiting_requests.append(wire)
    candidate_result = None
    if staging is not None:
        hashes = []
        for row in store.connection.execute("SELECT claim_json,content_hash FROM claim_versions"):
            claim = json.loads(row[0])
            if canonical_json(claim) != row[0] or claim.get("content_hash") != row[1] or content_hash({k:v for k,v in claim.items() if k != "content_hash"}) != row[1]:
                raise ValueError("formal candidate-origin authority drifted")
            if claim.get("candidate_origin_hash"):
                hashes.append(claim["candidate_origin_hash"])
        promoted_refs = [r[0] for r in store.connection.execute("SELECT candidate_claim_ref FROM reviewed_candidate_commits")]
        candidate_result = staging.expire_staged(now=moment, formal_candidate_hashes=hashes, formal_candidate_refs=promoted_refs)
    return {"status": "completed", "retired": retired, "legacy_migration": legacy_migration, "candidate_sla": candidate_result,
            "waiting_requests": waiting_requests, "question_authority": "research_question_backlog", "model_calls": 0}
