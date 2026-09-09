"""P12b: the child that files a bounded batch of Claims into the dossier's sections.

Out of process for the reason every model call here is: the writer abandons a
request after 30 seconds and this one is allowed 180.

One run does four things and stops:

1. find the claims this Core has not indexed yet, or has indexed under an older
   claim version -- computed from the Ledger, never drained from a queue;
2. tag every one of them by rule: importance from the document it came from,
   as_of from its period, dedupe key from subject/measure/period/unit, and the
   aspect too where the aspect follows from the subject or the measure;
3. ask the model for the aspect of the qualitative claims that are left, in one
   batched call, and verify the reply against the rows it was shown -- a reply
   that goes outside them refuses the whole batch;
4. record the entries.

Exit 0 when the run completed, including when it decided nothing needed doing.

``formal_authority_writes`` is always 0.  An index entry is a judgement *about*
Claims and never a Claim: it cannot change a value, a period or a status, and
the Ledger is not opened for writing here at all.

**Approval first.** The run checks the mission's ``may_write`` before spending
anything: drafting a batch and then being refused costs the mission a real
model call.  The scope it wants is ``claim_index``, which is not in the frozen
vocabulary yet (Wave 0 adds ``dossier``, ``debate_map`` and the rest, but not
this one).  Until it exists, a mission granting ``claim`` is accepted -- an
index entry is strictly weaker than the Claim it points at -- and the run says
in its summary which of the two words let it write.  The integrator flips this
by adding ``claim_index`` to ``AUTOMATION_WRITE_SCOPES`` and granting it; no
code change is needed, because the preferred word is already checked first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .claim_index_authority import ClaimIndexAuthority
from .claim_index_tagging import (
    MAX_CLAIMS_PER_BATCH,
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
    ClaimIndexTaggingError,
    ProvenanceResolver,
    aspects_from_response,
    build_batch,
    build_prompt,
    pending_claims,
    prompt_tagger,
    rule_tags,
)
from .cockpit_model import CockpitModel, CockpitModelError
from .coverage_mission import CoverageMissionAuthority
from .scheduler import SchedulerError
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
PURPOSE = "claim_index"

# TODO(integrator): add ``claim_index`` to
# ``coverage_mission.AUTOMATION_WRITE_SCOPES`` and grant it in the next mission
# version; then this constant is the only word checked and the fallback below
# can be deleted.
WRITE_SCOPE = "claim_index"
FALLBACK_WRITE_SCOPES: tuple[str, ...] = ("claim",)

DEFAULT_MAX_CLAIMS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def granted_scope(mission: Any) -> str | None:
    """The word that lets this run write, or None if the mission grants none."""

    may_write = set((mission.get("autonomy") or {}).get("may_write") or [])
    if WRITE_SCOPE in may_write:
        return WRITE_SCOPE
    for word in FALLBACK_WRITE_SCOPES:
        if word in may_write:
            return word
    return None


def mission_companies(mission: Any) -> list[str]:
    return [
        str(item["company_ref"])
        for item in (mission.get("universe") or [])
        if isinstance(item, dict) and item.get("company_ref")
    ]


def record_tagged(
    authority: ClaimIndexAuthority,
    rows: Sequence[dict[str, Any]],
    *,
    actor_ref: str,
    created_at: str,
) -> dict[str, int]:
    """Write one entry per row; returns how many were new and how many stood."""

    counts = {"fresh": 0, "duplicate": 0, "recanonicalised": 0}
    for row in rows:
        result = authority.record_entry(
            claim_version_ref=row["claim_version_ref"],
            claim_version_hash=row["claim_version_hash"],
            claim_ref=row["claim_ref"],
            claim_created_at=row["claim_created_at"],
            subject_ref=row["subject_ref"],
            metric_or_aspect=row["metric_or_aspect"] or "-",
            period_key=row["tags"]["period_key"],
            claim_kind=row["claim_kind"],
            aspect=row["tags"]["aspect"],
            aspect_source=row["tags"]["aspect_source"],
            as_of=row["tags"]["as_of"],
            as_of_basis=row["tags"]["as_of_basis"],
            importance=row["tags"]["importance"],
            importance_basis=row["tags"]["importance_basis"],
            dedupe_group_key=row["tags"]["dedupe_group_key"],
            tagger_ref=row["tags"]["tagger_ref"],
            tagger_hash=row["tags"]["tagger_hash"],
            actor_ref=actor_ref,
            created_at=created_at,
        )
        counts[result["status"]] += 1
        counts["recanonicalised"] += len(result["recanonicalised"])
    return counts


def run_claim_index(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None,
    company_ref: str | None = None,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "mode": "dry_run" if dry_run else "model",
        "company_ref": company_ref,
        "index_status": None,
        "write_scope": None,
        "pending": 0,
        "rule_tagged": 0,
        "model_tagged": 0,
        "batch_size": 0,
        "fresh": 0,
        "duplicate": 0,
        "recanonicalised": 0,
        "replayed": False,
        "cost_micros": 0,
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "index_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        scope = granted_scope(mission)
        if scope is None:
            summary.update({
                "status": "held", "index_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE}（或 "
                    f"{'/'.join(FALLBACK_WRITE_SCOPES)}）写入范围；"
                    "在授权前不打标签，免得白花模型调用"
                ),
            })
            return summary
        summary["write_scope"] = scope
        actor_ref = mission["autonomy"]["automation_principal"]

        subjects = [company_ref] if company_ref else mission_companies(mission)
        pending = pending_claims(store, subject_refs=subjects, limit=max_claims)
        summary["pending"] = len(pending)
        if not pending:
            summary.update({"status": "idle", "index_status": "nothing_to_tag"})
            return summary

        provenance = ProvenanceResolver(store.connection)
        for row in pending:
            row["tags"] = rule_tags(row, provenance.resolve(row["claim_version_ref"]))
        settled = [row for row in pending if row["tags"]["aspect"] is not None]
        open_rows = [row for row in pending if row["tags"]["aspect"] is None]

        batch = build_batch(open_rows, max_claims=MAX_CLAIMS_PER_BATCH)
        summary["batch_size"] = len(batch)
        summary["rule_tagged"] = len(settled)
        if dry_run:
            # A dry run says what it would do and writes nothing at all -- not
            # the rule tags either. "Assemble and stop" is what a dry run is
            # for, and a dry run that had already written half the answer is
            # not one anybody can use to look before they leap.
            summary.update({
                "status": "succeeded", "index_status": "dry_run",
                "prompt_bytes": len(build_prompt(batch).encode("utf-8")) if batch else 0,
            })
            return summary

        authority = ClaimIndexAuthority(store)
        created_at = _now()
        counts = record_tagged(
            authority, settled, actor_ref=actor_ref, created_at=created_at
        )
        summary["fresh"] += counts["fresh"]
        summary["duplicate"] += counts["duplicate"]
        summary["recanonicalised"] += counts["recanonicalised"]

        if not batch:
            summary.update({"status": "succeeded", "index_status": "rules_only"})
            return summary
        if model_config_path is None:
            summary.update({
                "status": "succeeded", "index_status": "gated",
                "failure_reason": "no model configured",
                "prompt_bytes": len(build_prompt(batch).encode("utf-8")),
            })
            return summary

        prompt = build_prompt(batch)
        model = CockpitModel(
            json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8")),
            scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        try:
            call = model.call(
                purpose=PURPOSE,
                # Keyed by the batch, so re-asking about the same claims
                # replays instead of being paid for twice.
                request_id=prompt_tagger("", prompt)[1][:32],
                prompt=prompt, mission=mission,
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "index_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "succeeded", "index_status": "model_unavailable",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["replayed"] = bool(call.get("replayed"))
        summary["cost_micros"] = int(call.get("cost_micros") or 0)
        try:
            assigned = aspects_from_response(batch, call["text"])
        except ClaimIndexTaggingError as exc:
            # Refused whole.  A batch with the invented rows removed is not the
            # answer the model gave, and the rows it happened to get right came
            # out of the same reply.
            summary.update({"status": "succeeded", "index_status": "refused",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        tagger_ref, tagger_hash = prompt_tagger(call["work_order_ref"], prompt)
        by_ref = {row["claim_version_ref"]: row for row in open_rows}
        tagged = []
        for claim_version_ref, aspect in assigned.items():
            row = by_ref[claim_version_ref]
            row["tags"] = {
                **row["tags"], "aspect": aspect, "aspect_source": "model",
                "tagger_ref": tagger_ref, "tagger_hash": tagger_hash,
            }
            tagged.append(row)
        counts = record_tagged(
            authority, tagged, actor_ref=actor_ref, created_at=created_at
        )
        summary["model_tagged"] = len(tagged)
        summary["fresh"] += counts["fresh"]
        summary["duplicate"] += counts["duplicate"]
        summary["recanonicalised"] += counts["recanonicalised"]
        summary.update({"status": "succeeded", "index_status": "tagged"})
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def run_promote_figures(
    *,
    state_dir: Path,
    staging_db: Path,
    summary_dir: Path,
    company_ref: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """ADR-0007's promoter as a hand-run subcommand.  Not wired to any lane.

    Staging only: a candidate still goes through the same review and
    adjudication path every other candidate does.  What this removes is the
    step where a verified number sat in ``coverage_mission_document_figures``
    with no way forward at all.
    """

    from .claim_index_figures import promote_verified_figures
    from .research_verification import CandidateStagingStore

    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "company_ref": company_ref,
        "promoted": [],
        "skipped": [],
        "truncated": 0,
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(Path(state_dir).expanduser().resolve() / "core.sqlite"))
    staging = CandidateStagingStore(str(staging_db))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "failure_reason": "no active mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        may_write = set(mission["autonomy"]["may_write"])
        missing = {"claim", "stage_record"} - may_write
        if missing:
            summary.update({
                "status": "held",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {sorted(missing)} 写入范围"
                ),
            })
            return summary
        result = promote_verified_figures(
            store, staging,
            actor_ref=mission["autonomy"]["automation_principal"],
            company_ref=company_ref, limit=limit,
        )
        summary.update({
            "status": "succeeded",
            "promoted": result["promoted"],
            "skipped": result["skipped"],
            "truncated": result["truncated"],
        })
        return summary
    except Exception as exc:
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        staging.close()
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--company-ref", help="index this company rather than all")
    parser.add_argument("--max-claims", type=int, default=DEFAULT_MAX_CLAIMS)
    parser.add_argument("--dry-run", action="store_true",
                        help="rule-tag and stop; no model call")
    parser.add_argument("--promote-figures", action="store_true",
                        help="ADR-0007: stage verified figures as candidates")
    parser.add_argument("--staging-db", type=Path,
                        help="candidate staging database for --promote-figures")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary_dir = args.summary_dir if args.summary_dir is not None else args.state_dir
    if args.promote_figures:
        if args.staging_db is None:
            raise SystemExit("--promote-figures needs --staging-db")
        summary = run_promote_figures(
            state_dir=args.state_dir, staging_db=args.staging_db,
            summary_dir=summary_dir, company_ref=args.company_ref, limit=args.limit,
        )
    else:
        summary = run_claim_index(
            state_dir=args.state_dir, model_config_path=args.model_config,
            summary_dir=summary_dir, scheduler_db=args.scheduler_db,
            company_ref=args.company_ref, max_claims=args.max_claims,
            dry_run=args.dry_run,
        )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "DEFAULT_MAX_CLAIMS",
    "FALLBACK_WRITE_SCOPES",
    "PURPOSE",
    "WRITE_SCOPE",
    "build_parser",
    "granted_scope",
    "main",
    "mission_companies",
    "record_tagged",
    "run_claim_index",
    "run_promote_figures",
]
