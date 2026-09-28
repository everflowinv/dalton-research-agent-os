"""C2-4 child: promote the numbers this install already holds, deterministically.

One run:

  1. sweeps every verified ``company-filed-document`` figure through the
     ADR-0007 promoter that already exists (``claim_index_figures``) and that
     nothing had ever called -- bookings, backlog, headcount and a guidance
     range come from here;
  2. sweeps the filed XBRL statement lines -- revenue, segment revenue, cost,
     operating income, net income, diluted EPS, cash flow, capex, buyback --
     and the two margins derived from two lines of one filing;
  3. writes each one down once in ``quantitative_claim_promotions``, with its
     exact anchor, so the sweep is idempotent and a new filing adds only its
     own numbers;
  4. asks the Ledger to admit it -- and takes no for an answer.

Step 4 is WP-F and it is the whole governance surface of this lane.  A number
crosses into ``claim_versions`` only when the *active signed policy* lists the
rule that admits it (``research-auto-commit:mission-verified-figure:v1`` for a
company-filed document figure, ``research-auto-commit:sec-statement-line:v1``
for a filed XBRL row or a margin derived from two of them).  With neither rule
signed this run writes exactly zero Ledger rows, leaves every number staged,
and records against each one which rule the owner has yet to sign.  ``--stage-
only`` refuses to ask at all, whatever the policy says.

No model is called. Exit 0 when the run completed (even with nothing to
promote), 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .coverage_mission import CoverageMissionAuthority
from .quantitative_claim_promotion import (
    QuantitativeClaimPromotionError,
    QuantitativeClaimPromotionLedger,
    SecStatementLineAuthorityResolver,
    document_figure_proposal,
    stage_statement_line_candidate,
    statement_line_proposals,
)
from .research_auto_commit import (
    MISSION_VERIFIED_FIGURE_RULE_REF as FIGURE_RULE,
    ResearchAutoCommitRejected,
    SEC_STATEMENT_LINE_RULE_REF as STATEMENT_LINE_RULE,
)
from .research_verification import ResearchVerificationError
from .store import DaltonStore, GateRejected, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_LIMIT = 200
# The two words a mission must grant before automation may stage a Claim.
WRITE_SCOPES = frozenset({"claim", "stage_record"})


def _unsigned(rule_ref: str) -> str:
    """Why a staged number is still waiting, in the owner's own vocabulary.

    Not a generic "policy refused": the one thing the owner has to do is sign a
    policy that lists exactly this rule, so the row says exactly that.
    """

    return (
        f"已完成确定性核验并进入候选暂存，但当前生效治理策略的 "
        f"policy.research_candidate_auto_commit.rules 未列出 {rule_ref}；"
        f"owner 签署列出该规则的新策略版本后即可自动入账"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


SAMPLE_LIMIT = 50


def _signed_rules(policy: Any) -> frozenset[str]:
    """The auto-commit rules the *active signed policy* lists, or none.

    Fail-closed on every shape it does not recognise: an absent block, a
    disabled block and a malformed block all mean the same thing, which is that
    nothing enters the Ledger without a person.
    """

    from .research_auto_commit import KNOWN_RULE_REFS

    body = policy.get("policy") if isinstance(policy, Mapping) else None
    rule = body.get("research_candidate_auto_commit") if isinstance(body, Mapping) else None
    if not isinstance(rule, Mapping) or rule.get("enabled") is not True:
        return frozenset()
    rules = rule.get("rules")
    if not isinstance(rules, list):
        return frozenset()
    return frozenset(item for item in rules if item in KNOWN_RULE_REFS)


def _ingest_of(proposal: Mapping[str, Any]) -> str:
    anchor = proposal["anchor"]
    if proposal["origin_kind"] == "statement_line":
        return str(anchor["ingest_id"])
    return str(anchor["numerator"]["ingest_id"])


def _sample(bucket: list[Any], item: Any) -> None:
    if len(bucket) < SAMPLE_LIMIT:
        bucket.append(item)


def run_promotion(
    *,
    state_dir: Path,
    summary_dir: Path,
    staging_db: Path | None = None,
    company_ref: str | None = None,
    limit: int = DEFAULT_LIMIT,
    admit: bool = True,
) -> dict[str, Any]:
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "company_ref": company_ref,
        "limit": limit,
        # Numbers newly written down this run, by where they came from.
        "promoted": {"statement_line": 0, "derived_ratio": 0, "document_figure": 0},
        "duplicates": 0,
        # Figures that repeat another figure of the same document (2026-09-28).
        "duplicate_figures": 0,
        "staged": 0,
        "admitted": 0,
        "staged_candidates": [],
        "admitted_claims": [],
        "blocked": [],
        "skipped": [],
        "held_totals": {},
        "failure_reason": None,
        # Which rules the owner has signed, and therefore which of these
        # numbers are allowed to cross the Ledger boundary without a person.
        "signed_rules": [],
        "unsigned_rules": [],
        # Claim versions this run wrote. Zero unless the owner signed a rule.
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(Path(state_dir).expanduser().resolve() / "core.sqlite"))
    staging = None
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "failure_reason": "没有生效中的任务"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        missing = WRITE_SCOPES - set(mission["autonomy"]["may_write"])
        if missing:
            summary.update({
                "status": "held",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {sorted(missing)} 写入范围；"
                    "定量结论已经算好但不会写入"
                ),
            })
            return summary
        actor_ref = mission["autonomy"]["automation_principal"]
        ledger = QuantitativeClaimPromotionLedger(store.connection)
        signed = _signed_rules(store.active_policy()) if admit else frozenset()
        summary["signed_rules"] = sorted(signed)
        summary["unsigned_rules"] = sorted(
            {FIGURE_RULE, STATEMENT_LINE_RULE} - set(signed))

        def settle(proposal, *, disposition, candidate_claim_ref=None,
                   claim_version_ref=None, reason=None, ledger_write=False):
            """Write the number down once, then move it forward as it travels."""

            record = ledger.record(
                proposal, disposition=disposition,
                candidate_claim_ref=candidate_claim_ref,
                claim_version_ref=claim_version_ref, reason=reason)
            if record["status"] == "fresh":
                summary["promoted"][proposal["origin_kind"]] += 1
            else:
                summary["duplicates"] += 1
                record = ledger.settle(
                    record["promotion_id"], disposition=disposition,
                    candidate_claim_ref=candidate_claim_ref,
                    claim_version_ref=claim_version_ref, reason=reason)
            entry = {
                "promotion_id": record["promotion_id"],
                "company_ref": proposal["company_ref"],
                "metric_or_aspect": proposal["metric_or_aspect"],
                "period": proposal["period"],
                "normalized_statement": proposal["normalized_statement"],
            }
            if disposition == "admitted":
                summary["admitted"] += 1
                # Only a commit that actually wrote a ClaimVersion counts; a
                # replayed run admits the same numbers and writes nothing, and
                # a number that says otherwise is the kind of number this whole
                # work package exists to stop.
                if ledger_write:
                    summary["formal_authority_writes"] += 1
                _sample(summary["admitted_claims"],
                        {**entry, "claim_version_ref": claim_version_ref})
            elif disposition == "staged":
                summary["staged"] += 1
                _sample(summary["staged_candidates"],
                        {**entry, "candidate_claim_ref": candidate_claim_ref,
                         "reason": reason})
            else:
                _sample(summary["blocked"], {**entry, "reason": reason})
            return record

        def admit_candidate(*, evidence, claim, material, source_verification,
                            numeric_verification, numeric_spec=None):
            """Ask the Ledger; a refusal is an answer, not a failure."""

            try:
                promoted = store.commit_policy_candidate(
                    evidence=evidence, claim=claim, material=material,
                    numeric_spec=numeric_spec,
                    source_verification=source_verification,
                    numeric_verification=numeric_verification,
                    idempotency_key="policy-ledger:" + claim["id"])
            except (ResearchAutoCommitRejected, GateRejected) as exc:
                return None, f"{type(exc).__name__}: {exc}"
            return promoted, None

        # -- 1. the figures pass's numbers, through the promoter that exists --
        if staging_db is not None:
            from .claim_index_figures import promote_verified_figures
            from .research_verification import CandidateStagingStore

            from .document_figure_identity import duplicate_groups

            staging = CandidateStagingStore(str(staging_db))
            # 2026-09-28: a figure already admitted is not restaged.  Its
            # candidate's sentence may differ now (the filer's label, the
            # period as dates), and a different sentence is a different
            # candidate -- restaged, it would enter the Ledger a second time.
            admitted_figures = _admitted_figures(store)
            figures = promote_verified_figures(
                store, staging, actor_ref=actor_ref,
                company_ref=company_ref, limit=limit, promoted=admitted_figures,
            )
            summary["skipped"].extend(figures["skipped"])
            results_by_figure = {item["figure_id"]: item for item in figures["results"]}
            held = _held_figures(store, company_ref)
            duplicates, identities = duplicate_groups(store.connection, [
                item for item in held if item.get("source_grade") == "company-filed-document"])
            answered = {duplicates.get(item, item) for item in admitted_figures}
            for figure in held:
                if figure["figure_id"] in admitted_figures:
                    continue
                if figure["figure_id"] in duplicates or figure["figure_id"] in answered:
                    # The same number from the same document: written down
                    # once, under the figure it repeats.
                    summary["duplicate_figures"] += 1
                    continue
                proposal = document_figure_proposal(
                    figure, identities.get(figure["figure_id"]))
                if proposal is None:
                    continue
                result = results_by_figure.get(figure["figure_id"])
                if result is None:
                    settle(proposal, disposition="blocked",
                           reason="candidate staging did not accept this figure")
                    continue
                claim_ref = result["claim"]["id"]
                if FIGURE_RULE not in signed:
                    settle(proposal, disposition="staged",
                           candidate_claim_ref=claim_ref,
                           reason=_unsigned(FIGURE_RULE))
                    continue
                promoted, refusal = admit_candidate(
                    evidence=result["evidence"], claim=result["claim"],
                    material=result["material"],
                    source_verification=result["source_verification"],
                    numeric_verification=result["numeric_verification"])
                if promoted is None:
                    settle(proposal, disposition="staged",
                           candidate_claim_ref=claim_ref, reason=refusal)
                else:
                    settle(proposal, disposition="admitted",
                           candidate_claim_ref=claim_ref,
                           claim_version_ref=promoted.get("claim_version_ref"),
                           ledger_write=promoted.get("status") == "fresh")
        else:
            summary["skipped"].append({
                "pass": "document_figure",
                "reason": "no candidate staging database was configured for this run",
            })

        # -- 2. the filed statements ----------------------------------------
        proposals = statement_line_proposals(
            store.connection, company_ref=company_ref, limit=limit)
        resolvers: dict[str, Any] = {}
        for proposal in proposals:
            if staging is None:
                settle(proposal, disposition="blocked",
                       reason="本次运行没有配置候选暂存库，申报行无法进入暂存")
                continue
            ingest_id = _ingest_of(proposal)
            resolver = resolvers.get(ingest_id)
            if resolver is None:
                resolver = SecStatementLineAuthorityResolver(store.connection)
                resolvers[ingest_id] = resolver
            try:
                bundle = stage_statement_line_candidate(
                    store.connection, staging, ingest_id=ingest_id,
                    origin_ref=proposal["origin_ref"], actor_ref=actor_ref,
                    resolver=resolver)
            except (QuantitativeClaimPromotionError, ResearchVerificationError) as exc:
                settle(proposal, disposition="blocked",
                       reason=f"{type(exc).__name__}: {exc}")
                continue
            claim_ref = bundle["claim"]["id"]
            if STATEMENT_LINE_RULE not in signed:
                settle(proposal, disposition="staged",
                       candidate_claim_ref=claim_ref,
                       reason=_unsigned(STATEMENT_LINE_RULE))
                continue
            promoted, refusal = admit_candidate(
                evidence=bundle["evidence"], claim=bundle["claim"],
                material=bundle["material"], numeric_spec=bundle["numeric_spec"],
                source_verification=bundle["source_verification"],
                numeric_verification=bundle["numeric_verification"])
            if promoted is None:
                settle(proposal, disposition="staged",
                       candidate_claim_ref=claim_ref, reason=refusal)
            else:
                settle(proposal, disposition="admitted",
                       candidate_claim_ref=claim_ref,
                       claim_version_ref=promoted.get("claim_version_ref"),
                       ledger_write=promoted.get("status") == "fresh")

        summary["held_totals"] = ledger.counts()
        summary["status"] = "succeeded"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface it
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        if staging is not None:
            staging.close()
        store.close()


def _admitted_figures(store: Any) -> set[str]:
    """Figure ids this ledger has already seen into ``claim_versions``."""

    try:
        rows = store.connection.execute(
            "SELECT origin_ref FROM quantitative_claim_promotions "
            "WHERE origin_kind='document_figure' AND disposition='admitted'").fetchall()
    except Exception:  # noqa: BLE001 - a fresh install has no ledger yet
        return set()
    return {str(row[0]) for row in rows}


def _held_figures(store: Any, company_ref: str | None) -> list[dict[str, Any]]:
    from .claim_index_figures import MissionFigureAuthorityResolver

    try:
        return MissionFigureAuthorityResolver(store.connection).figures(
            company_ref=company_ref)
    except Exception:  # noqa: BLE001 - an install with no figures has none
        return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, required=True)
    parser.add_argument("--candidate-staging", type=Path,
                        help="shared CandidateStaging database; enables figure staging")
    parser.add_argument("--company-ref")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--stage-only", action="store_true",
                        help="verify and stage, but never ask the Ledger to admit; "
                             "the run then writes no ClaimVersion whatever the policy says")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.limit <= 5000:
        raise SystemExit("--limit must be 1..5000")
    summary = run_promotion(
        state_dir=args.state_dir, summary_dir=args.summary_dir,
        staging_db=args.candidate_staging, company_ref=args.company_ref,
        limit=args.limit, admit=not args.stage_only,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
