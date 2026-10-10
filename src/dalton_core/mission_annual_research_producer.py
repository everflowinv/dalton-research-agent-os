"""Write mission annual-research admissions from planner repair inquiries.

The annual lane consumed admissions and nothing produced them (C2-3, and the
tick ledgers of both live environments on 2026-10-02).  The design always
named the producer: ``MissionAnnualResearchAuthority`` admits under the active
mission's *automation principal* (its schema refuses any actor that is not
``automation:``), the mission must grant ``research_task``, and the question it
answers is a *planner inquiry* that binds one exact Dossier repair target
(``research_planner`` copies the target ref/hash into the inquiry;
``research_task`` holds such an inquiry as ``repair_target_capability_gap``
because no ad-hoc template can answer it).  This module is that missing
research-task producer step for the annual-report capability.  It is not an
owner door: no human approval is manufactured or required, exactly as the
authority's design note says.

What it admits, and only that:

* an inquiry from a recorded research plan of the active mission version that
  binds a repair target and carries no ``directed_document`` strategy (an
  inquiry with a strategy belongs to the directed document lane, and the
  authority's closed inquiry shape refuses it anyway);
* the target is still in the company's *current* Dossier repair feedback;
* the company has an acquired SEC 10-K reviewed under that same mission
  version (``registered_annual_report.bind_request``'s own predicate);
* the query terms are literal anchors taken from the target itself -- the
  figure and its usual filing renderings, and Latin-script phrases written in
  the target's detail.  Nothing is translated or guessed; a target with no
  literal anchor is declined by name, because the planner's directed-document
  strategy is the place where terms are chosen in the document's language.

One admission per company x 10-K x repair target, ever.  At most one admission
attempt per tick (the admission renders the full filing), at most
``max_units`` admissions per UTC day, and the worst-case model envelope of the
day's admissions may not exceed the mission's own ``max_daily_cost_usd`` or
the producer's ``max_cost_usd``.  The paid-call budget is still admitted
atomically by the model worker immediately before each send; this is the
outer ceiling, not a reservation.  A refused admission is remembered for the
rest of the UTC day so the lane does not re-render the same filing every tick.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Mapping

from .store import content_hash


SCHEMA_VERSION = "0.1"
LEDGER_FILE = "producer.json"
RUN_BUDGET_PURPOSE = "mission_annual_research_admission"
PLAN_SCAN_LIMIT = 30
MAX_QUERY_TERMS = 8
_INQUIRY_FIELDS = frozenset({
    "rank", "company_ref", "question", "wants", "because",
    "repair_target_ref", "repair_target_hash",
})
_DEFAULT_RUN_BUDGET = {
    "max_admissions_per_tick": 1, "max_units": 3, "max_cost_usd": 120.0,
}
# Latin-script phrases: words of letters/digits joined by single spaces or the
# punctuation a filing uses inside a name ("Hogan AI", "net promoter score").
_LATIN_PHRASE = re.compile(
    r"[A-Za-z][A-Za-z0-9&'./-]*(?: [A-Za-z0-9][A-Za-z0-9&'./-]*)*"
)
_FORM_WORDS = frozenset({"10-k", "10-q", "8-k", "20-f", "sec", "fy"})

# Plain words for the tick ledger.  Each names what is missing and where the
# next input has to come from, so an idle lane says why it is idle.
DECLINE_TEXT = {
    "no_active_mission": "没有生效的 CoverageMission",
    "mission_lacks_research_task": "生效 mission 的 autonomy.may_write 未授予 research_task",
    "no_registered_repair_target": "当前没有任何在册的 Dossier 修复目标（最新一次成功 Dossier 运行没有报告缺口）",
    "no_planner_inquiry_for_current_target": (
        "有在册的 Dossier 修复目标，但最近的研究计划里没有绑定当前目标、且不带 directed_document 的 planner inquiry"
    ),
    "no_acquired_10k_for_mission": "有绑定当前修复目标的 planner inquiry，但该公司没有在当前 mission 版本下已取得的 10-K",
    "already_admitted": "所有候选（公司 × 10-K × 修复目标）都已经有 admission",
    "no_literal_anchor": "修复目标里没有可在英文年报中逐字检索的锚点（数字或拉丁文短语）",
    "declined_earlier_today": "候选今天已被 authority 拒绝过，明天或输入变化后再试",
    "daily_cap_reached": "今天的年报研究 admission 已达到每日上限",
    "daily_cost_ceiling_reached": "今天已 admit 的最坏情况模型预算加上这一份会超过 mission 每日成本上限",
    "authority_unavailable": "年报研究 admission authority 无法打开",
}


class MissionAnnualResearchProducerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def producer_run_budget() -> dict[str, Any]:
    from .call_budget import default_run_budget

    return default_run_budget(RUN_BUDGET_PURPOSE, defaults=_DEFAULT_RUN_BUDGET)


def _collapse(value: str) -> str:
    return " ".join(value.casefold().split())


def _figure_terms(figure: str) -> list[str]:
    """The figure as written, plus how a 10-K usually prints the same number."""

    text = figure.strip()
    terms: list[str] = []
    digits = re.sub(r"[,$\s%]", "", text)
    try:
        value = Decimal(digits)
    except InvalidOperation:
        value = None
    if value is not None and value == value.to_integral_value() and abs(value) < 100:
        # "14", "23": a bare small integer is in every paragraph of a filing.
        return []
    if len(text) >= 2:
        terms.append(text)
    if value is None or not value.is_finite():
        return terms
    if value == value.to_integral_value() and abs(value) >= 1000:
        terms.append(f"{int(value):,}")
    for scale, word in ((Decimal(1_000_000_000), "billion"), (Decimal(1_000_000), "million")):
        if abs(value) >= scale:
            scaled = (value / scale).normalize()
            if scaled == scaled.to_integral_value():
                rendered = f"{int(scaled)}"
            else:
                rendered = format(scaled, "f")
            terms.append(f"{rendered} {word}")
    return terms


def query_terms_for_target(
    target: Mapping[str, Any], *, exclude: frozenset[str] = frozenset(),
) -> list[str]:
    """Literal anchors only.  Empty means the target cannot be searched as-is."""

    raw: list[str] = []
    figure = target.get("figure")
    if isinstance(figure, str) and figure.strip():
        raw.extend(_figure_terms(figure))
    detail = target.get("detail")
    if isinstance(detail, str):
        raw.extend(match.group(0) for match in _LATIN_PHRASE.finditer(detail))
    terms: list[str] = []
    for item in raw:
        term = _collapse(item).strip(" .,'/-")
        if (
            not 3 <= len(term) <= 160
            or term in _FORM_WORDS
            or term in exclude
            or re.fullmatch(r"fy ?\d{2,4}", term)
            or term in terms
        ):
            continue
        terms.append(term)
        if len(terms) == MAX_QUERY_TERMS:
            break
    return terms


def _estimated_envelope_usd(model_execution: Mapping[str, Any]) -> Decimal:
    """``bind_request``'s own worst-case physical-call cost for one admission."""

    total = Decimal(0)
    for stage in ("draft", "verifier"):
        execution = model_execution[stage]
        recovery = (execution.get("provider_retry") or {}).get("unknown_recovery")
        fresh = 0 if recovery is None else int(recovery["max_fresh_work_orders"])
        total += (
            Decimal(int(execution["max_attempts"]) * (1 + fresh))
            * Decimal(str(execution["max_cost_usd"]))
        )
    return total


class MissionAnnualResearchProducer:
    """Select one admissible candidate per tick and admit it, or say why not."""

    def __init__(
        self,
        *,
        store: Any,
        state_dir: str | Path,
        ledger_dir: str | Path,
        authority_factory: Callable[[], ContextManager[Any]],
        draft_config_path: str | Path | None = None,
        verifier_config_path: str | Path | None = None,
        clock: Callable[[], datetime] = _now,
        run_budget: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.ledger_path = Path(ledger_dir) / LEDGER_FILE
        self.authority_factory = authority_factory
        self.draft_config_path = draft_config_path
        self.verifier_config_path = verifier_config_path
        self.clock = clock
        budget = dict(producer_run_budget() if run_budget is None else run_budget)
        self.per_tick = int(budget.get("max_admissions_per_tick", 1))
        self.daily_cap = int(budget.get("max_units", _DEFAULT_RUN_BUDGET["max_units"]))
        self.cost_ceiling = Decimal(str(budget.get(
            "max_cost_usd", _DEFAULT_RUN_BUDGET["max_cost_usd"])))

    # -- the day's memory -------------------------------------------------

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).date().isoformat()

    def _read_ledger(self) -> dict[str, str]:
        if not self.ledger_path.is_file():
            return {}
        try:
            record = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MissionAnnualResearchProducerError(
                "annual research producer ledger is invalid") from exc
        body = dict(record) if isinstance(record, Mapping) else {}
        asserted = body.pop("content_hash", None)
        if (
            set(body) != {"schema_version", "day", "declined"}
            or body.get("schema_version") != SCHEMA_VERSION
            or asserted != content_hash(body)
            or not isinstance(body.get("declined"), dict)
        ):
            raise MissionAnnualResearchProducerError(
                "annual research producer ledger drifted")
        if body["day"] != self._day():
            return {}
        return {str(key): str(value) for key, value in body["declined"].items()}

    def _write_ledger(self, declined: Mapping[str, str]) -> None:
        from .lane_child_launcher import write_owner_only

        body = {
            "schema_version": SCHEMA_VERSION, "day": self._day(),
            "declined": {key: declined[key] for key in sorted(declined)},
        }
        self.ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_owner_only(self.ledger_path, {**body, "content_hash": content_hash(body)})

    # -- read-only inputs -------------------------------------------------

    def _missions(self) -> list[dict[str, Any]]:
        from .coverage_mission import CoverageMissionAuthority

        try:
            rows = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        authority = CoverageMissionAuthority(self.store)
        return [authority.mission(row[0]) for row in rows]

    def _plan_inquiries(self, mission_version_ref: str) -> list[dict[str, Any]]:
        """Bare repair-target inquiries, newest plan first, first per target."""

        try:
            rows = self.connection.execute(
                "SELECT plan_id,inquiries_json FROM coverage_mission_research_plans "
                "WHERE mission_version_ref=? ORDER BY created_at DESC, plan_id DESC "
                "LIMIT ?", (mission_version_ref, PLAN_SCAN_LIMIT),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                inquiries = json.loads(row["inquiries_json"])
            except (TypeError, ValueError):
                continue
            for inquiry in inquiries if isinstance(inquiries, list) else ():
                if (
                    isinstance(inquiry, Mapping)
                    and set(inquiry) == _INQUIRY_FIELDS
                    and isinstance(inquiry.get("repair_target_ref"), str)
                    and inquiry["repair_target_ref"] not in found
                ):
                    found[inquiry["repair_target_ref"]] = {
                        "plan_ref": row["plan_id"], "inquiry": dict(inquiry),
                    }
        return list(found.values())

    def _annual_reports(self, mission_version_ref: str) -> dict[str, list[dict[str, Any]]]:
        """Acquired SEC 10-K reviews under this exact mission version, by company.

        One per accession: a review with a read-completion proof first, then
        the newest.  Mirrors ``bind_request``/``resolve_core_registration``.
        """

        from .registered_annual_report import SOURCE_REF

        try:
            rows = self.connection.execute(
                "SELECT r.review_id,r.company_ref,r.document_ref,r.created_at,"
                "f.cik AS cik,f.accession AS accession,p.proof_id AS proof_id "
                "FROM coverage_mission_document_reviews r "
                "JOIN coverage_mission_discovered_documents d "
                "ON d.record_id=r.discovered_document_ref "
                "JOIN coverage_mission_statement_filings f "
                "ON f.company_ref=r.company_ref AND 'sec:filing:'||f.accession=r.document_ref "
                "LEFT JOIN document_read_completion_proofs p ON p.review_id=r.review_id "
                "WHERE r.mission_version_ref=? AND r.source_ref=? "
                "AND d.status='acquired' AND d.ticket_ref IS NOT NULL AND f.form='10-K' "
                "ORDER BY (p.proof_id IS NULL), r.created_at DESC, r.review_id",
                (mission_version_ref, SOURCE_REF),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                return {}
            raise
        result: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["company_ref"], row["accession"])
            if key in seen:
                continue
            seen.add(key)
            result.setdefault(row["company_ref"], []).append({
                "review_ref": row["review_id"], "accession": row["accession"],
                "issuer_cik": str(row["cik"]).zfill(10),
                "document_read_proof_ref": row["proof_id"],
            })
        return result

    def _admitted_keys(self) -> set[tuple[str, str, str]]:
        try:
            rows = self.connection.execute(
                "SELECT company_ref,repair_target_ref,record_json "
                "FROM mission_annual_research_admissions"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return set()
            raise
        keys = set()
        for row in rows:
            request = json.loads(row["record_json"]).get("request") or {}
            keys.add((row["company_ref"], str(request.get("accession")),
                      row["repair_target_ref"]))
        return keys

    def _today(self) -> tuple[int, Decimal]:
        day = self._day()
        try:
            rows = self.connection.execute(
                "SELECT record_json FROM mission_annual_research_admissions "
                "WHERE substr(created_at,1,10)=?", (day,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return 0, Decimal(0)
            raise
        spent = Decimal(0)
        for row in rows:
            request = json.loads(row["record_json"]).get("request") or {}
            try:
                spent += _estimated_envelope_usd(request["model_execution"])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                # An admission this module cannot price is priced at the cap.
                spent += self.cost_ceiling
        return len(rows), spent

    def _projected_envelope(self) -> Decimal:
        from .annual_report_runtime import (
            load_annual_report_model_configs, plan_model_execution,
        )

        draft, verifier = load_annual_report_model_configs(
            self.state_dir, draft_path=self.draft_config_path,
            verifier_path=self.verifier_config_path,
        )
        return _estimated_envelope_usd({
            "draft": plan_model_execution(draft, "registered_annual_report_draft"),
            "verifier": plan_model_execution(verifier, "registered_annual_report_verifier"),
        })

    # -- the decision -----------------------------------------------------

    def candidates(self) -> dict[str, Any]:
        """Read-only: every candidate this tick, or the first missing input."""

        from .dossier_repair_feedback import read_dossier_repair_feedback

        missions = self._missions()
        if not missions:
            return {"decline": "no_active_mission", "candidates": [], "counts": {}}
        feedback = read_dossier_repair_feedback(self.state_dir)
        counts = {
            "registered_repair_targets": sum(
                len(item["repair_targets"]) for item in feedback.values()),
            "planner_repair_inquiries": 0, "mission_10k": 0,
        }
        candidates: list[dict[str, Any]] = []
        granted = False
        for mission in missions:
            if "research_task" not in set(mission["autonomy"]["may_write"]):
                continue
            granted = True
            reports = self._annual_reports(mission["id"])
            counts["mission_10k"] += sum(len(items) for items in reports.values())
            tickers = {
                item["company_ref"]: _collapse(str(item.get("ticker") or ""))
                for item in mission.get("universe", ()) if isinstance(item, Mapping)
            }
            for found in self._plan_inquiries(mission["id"]):
                inquiry = found["inquiry"]
                company_ref = inquiry["company_ref"]
                current = feedback.get(company_ref)
                target = None if current is None else next((
                    item for item in current["repair_targets"]
                    if item["id"] == inquiry["repair_target_ref"]
                    and item["content_hash"] == inquiry["repair_target_hash"]
                ), None)
                if target is None:
                    continue
                counts["planner_repair_inquiries"] += 1
                for report in reports.get(company_ref, ()):
                    candidates.append({
                        "mission": mission, "plan_ref": found["plan_ref"],
                        "inquiry": inquiry, "feedback": current, "target": target,
                        "report": report,
                        "query_terms": query_terms_for_target(
                            target, exclude=frozenset(
                                {tickers.get(company_ref, "")} - {""})),
                        "key": content_hash({
                            "mission_version_ref": mission["id"],
                            "company_ref": company_ref,
                            "accession": report["accession"],
                            "review_ref": report["review_ref"],
                            "repair_target_ref": target["id"],
                            "repair_target_hash": target["content_hash"],
                            "inquiry_hash": content_hash(inquiry),
                        }),
                    })
        if not granted:
            decline = "mission_lacks_research_task"
        elif counts["registered_repair_targets"] == 0:
            decline = "no_registered_repair_target"
        elif counts["planner_repair_inquiries"] == 0:
            decline = "no_planner_inquiry_for_current_target"
        elif not candidates:
            decline = "no_acquired_10k_for_mission"
        else:
            decline = None
        return {"decline": decline, "candidates": candidates, "counts": counts}

    @staticmethod
    def _rationale(target: Mapping[str, Any], terms: list[str]) -> str:
        anchors = ", ".join(repr(term) for term in terms)
        what = "; ".join(
            f"{key}={target[key]}" for key in ("unit", "slot_id", "section", "check", "code")
            if isinstance(target.get(key), str) and target[key]
        )
        return (
            f"Literal anchors copied from Dossier repair target {target['id']} ({what}): "
            f"{anchors}. Figures are searched as written and in the filing's own "
            "comma/million/billion renderings; Latin phrases are searched exactly as the "
            "target states them. No term was translated or inferred."
        )

    def _declined(self, reason: str, counts: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "status": "declined", "decline": reason,
            "reason": DECLINE_TEXT.get(reason, reason), "counts": dict(counts), **extra,
        }

    def produce_once(self) -> dict[str, Any]:
        found = self.candidates()
        counts = dict(found["counts"])
        if found["decline"] is not None:
            return self._declined(found["decline"], counts)
        admitted = self._admitted_keys()
        declined_today = self._read_ledger()
        fresh: list[dict[str, Any]] = []
        anchorless = repeated = 0
        for candidate in found["candidates"]:
            identity = (candidate["inquiry"]["company_ref"],
                        candidate["report"]["accession"], candidate["target"]["id"])
            if identity in admitted:
                continue
            if not candidate["query_terms"]:
                anchorless += 1
                continue
            if candidate["key"] in declined_today:
                repeated += 1
                continue
            fresh.append(candidate)
        counts.update({
            "candidates": len(found["candidates"]), "no_literal_anchor": anchorless,
            "declined_earlier_today": repeated, "admissible_now": len(fresh),
        })
        if not fresh:
            reason = (
                "declined_earlier_today" if repeated
                else "no_literal_anchor" if anchorless
                else "already_admitted"
            )
            return self._declined(reason, counts)
        admitted_today, envelope_today = self._today()
        counts["admitted_today"] = admitted_today
        if admitted_today >= self.daily_cap:
            return self._declined("daily_cap_reached", counts, daily_cap=self.daily_cap)
        try:
            projected = self._projected_envelope()
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            return self._declined(
                "authority_unavailable", counts, error=f"{type(exc).__name__}: {exc}")
        results: list[dict[str, Any]] = []
        for candidate in fresh[: self.per_tick]:
            mission = candidate["mission"]
            ceiling = min(
                self.cost_ceiling,
                Decimal(str(mission["budget"].get("max_daily_cost_usd", 0))),
            )
            if envelope_today + projected > ceiling:
                return self._declined(
                    "daily_cost_ceiling_reached", counts,
                    estimated_envelope_usd=str(projected),
                    admitted_envelope_today_usd=str(envelope_today),
                    ceiling_usd=str(ceiling),
                )
            results.append(self._admit(candidate, declined_today))
            envelope_today += projected
        fresh_results = [item for item in results if item.get("status") == "fresh"]
        if not fresh_results:
            last = results[-1]
            return self._declined(
                last.get("decline", "authority_unavailable"), counts,
                error=last.get("error"), company_ref=last.get("company_ref"),
            )
        counts["admitted_today"] = admitted_today + len(fresh_results)
        first = fresh_results[0]
        return {
            "status": "admitted",
            "reason": (
                f"已为 {first['company_ref']} 的 10-K {first['accession']} "
                f"按 Dossier 修复目标 {first['repair_target_ref']} 写入年报研究 admission"
            ),
            "admissions": fresh_results,
            "estimated_envelope_usd": str(projected),
            "counts": counts,
        }

    def _admit(self, candidate: Mapping[str, Any], declined_today: dict[str, str]) -> dict[str, Any]:
        from .mission_annual_research import WORKFLOW_CONTRACT_REF
        from .registered_annual_report import (
            DEFAULT_CONTEXT_AFTER_CHARS, DEFAULT_CONTEXT_BEFORE_CHARS,
            DEFAULT_MAX_RESULTS, DEFAULT_MAX_SOURCE_BYTES, OPERATION,
        )

        mission = candidate["mission"]
        inquiry = candidate["inquiry"]
        feedback = candidate["feedback"]
        target = candidate["target"]
        report = candidate["report"]
        terms = list(candidate["query_terms"])
        summary = {
            "company_ref": inquiry["company_ref"], "accession": report["accession"],
            "repair_target_ref": target["id"], "plan_ref": candidate["plan_ref"],
        }
        try:
            with self.authority_factory() as authority:
                admitted = authority.admit(
                    operation=OPERATION, workflow_contract_ref=WORKFLOW_CONTRACT_REF,
                    mission_version_ref=mission["id"],
                    mission_version_hash=mission["content_hash"],
                    company_ref=inquiry["company_ref"],
                    actor_ref=mission["autonomy"]["automation_principal"],
                    repair_feedback_ref=feedback["id"],
                    repair_feedback_hash=feedback["content_hash"],
                    repair_target_ref=target["id"],
                    repair_target_hash=target["content_hash"],
                    inquiry=inquiry,
                    query_rationale=self._rationale(target, terms),
                    review_ref=report["review_ref"], issuer_cik=report["issuer_cik"],
                    accession=report["accession"], query_terms=terms,
                    limits={
                        "max_query_terms": MAX_QUERY_TERMS,
                        "max_results": DEFAULT_MAX_RESULTS,
                        "max_source_bytes": DEFAULT_MAX_SOURCE_BYTES,
                        "context_before_chars": DEFAULT_CONTEXT_BEFORE_CHARS,
                        "context_after_chars": DEFAULT_CONTEXT_AFTER_CHARS,
                    },
                    document_read_proof_ref=report["document_read_proof_ref"],
                )
        except Exception as exc:  # noqa: BLE001 - the authority's refusal is the reason
            error = f"{type(exc).__name__}: {exc}"
            declined_today[candidate["key"]] = error[:300]
            self._write_ledger(declined_today)
            return {**summary, "status": "declined", "decline": "authority_refused",
                    "error": error}
        return {
            **summary, "status": admitted["status_marker"],
            "admission_ref": admitted["id"], "admission_hash": admitted["content_hash"],
        }


@contextmanager
def open_writer_admission_authority(
    *, server: Any, state_dir: Path,
    draft_config_path: Path | None, verifier_config_path: Path | None,
) -> Iterator[Any]:
    """The authority over the writer's own Core, with a read-only Router."""

    from .annual_report_runtime import load_annual_report_model_configs
    from .mission_annual_research import MissionAnnualResearchAuthority
    from .model_router import ModelRouter
    from .registered_annual_report import RegisteredAnnualReportRegistry

    draft, _verifier = load_annual_report_model_configs(
        state_dir, draft_path=draft_config_path, verifier_path=verifier_config_path,
    )
    registry = RegisteredAnnualReportRegistry.from_writer(server)
    router = ModelRouter(draft["model_router_db"], read_only=True)
    try:
        yield MissionAnnualResearchAuthority(
            server.store, state_dir=state_dir, registry=registry, router=router,
            draft_config_path=draft_config_path,
            verifier_config_path=verifier_config_path,
        )
    finally:
        router.close()


__all__ = [
    "DECLINE_TEXT", "LEDGER_FILE", "MissionAnnualResearchProducer",
    "MissionAnnualResearchProducerError", "RUN_BUDGET_PURPOSE",
    "open_writer_admission_authority", "producer_run_budget",
    "query_terms_for_target",
]
