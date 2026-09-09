#!/usr/bin/env python3
"""P13y: unlearn the measures taken from documents about other companies.

The figures pass and the qualitative pass each refuse a document that never
names the company it was filed under.  The metric-discovery pass -- the third
one, added later -- did not, and nobody noticed because it writes no claim.

Writing no claim is not the same as being harmless.  Two observations of the
same measure make a *requirement*, and a requirement is what the numeric pass
then goes hunting in every later window.  A measure learned from a Haier
earnings call filed under EPAM does not just sit there: it sends the expensive
pass looking for a line item EPAM does not report, in EPAM's own filings, for
as long as it stands.

Which observations are wrong is not re-derived here.  The running system has
already judged these documents, window by window, with the same check and the
same bytes: every extraction summary that recorded ``not_attributed`` for a
review is that judgement, on disk.  This reads those verdicts, maps them back
to the documents they were about, and retracts what was learned from them.
Re-deriving would mean re-reading the source documents through the writer's
own launchers, and would answer a question that has already been answered.

Retracts; never deletes.  Prints the plan and stops unless ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.coverage_mission import CoverageMissionAuthority  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402

REASON = (
    "P13y: learned by the metric-discovery pass from a document the figures and "
    "qualitative passes had already refused as not about this company"
)
NOT_ATTRIBUTED = "not_attributed"


def unattributed_reviews(state_dir: Path) -> dict[str, int]:
    """Review ids the running system judged not-attributed, and how often.

    A verdict is a verdict whichever pass reached it: the check is on the
    document and the company, not on what the window was being read for.
    """

    verdicts: Counter[str] = Counter()
    root = state_dir / "extractions"
    if not root.is_dir():
        return {}
    for summary in sorted(root.glob("*/summary.json")):
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for value in payload.values():
            if not isinstance(value, list):
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                review_id = entry.get("review_id")
                if entry.get("status") == NOT_ATTRIBUTED and isinstance(review_id, str):
                    verdicts[review_id] += 1
    return dict(verdicts)


def plan(store: DaltonStore, state_dir: Path) -> dict[str, Any]:
    reviews = unattributed_reviews(state_dir)
    documents: dict[tuple[str, str], list[str]] = {}
    for review_id in sorted(reviews):
        row = store.connection.execute(
            "SELECT company_ref, document_ref FROM coverage_mission_document_reviews "
            "WHERE review_id=?", (review_id,),
        ).fetchone()
        if row is None:
            continue
        documents.setdefault((row["company_ref"], row["document_ref"]), []).append(review_id)
    targets: list[dict[str, Any]] = []
    for (company_ref, document_ref), review_ids in sorted(documents.items()):
        rows = store.connection.execute(
            "SELECT o.observation_id, o.metric_ref, o.label, o.unit "
            "FROM coverage_mission_metric_observations o "
            "LEFT JOIN coverage_mission_metric_observation_retractions r "
            "ON r.observation_id=o.observation_id "
            "WHERE r.observation_id IS NULL AND o.company_ref=? AND o.document_ref=? "
            "ORDER BY o.created_at, o.observation_id",
            (company_ref, document_ref),
        ).fetchall()
        for row in rows:
            targets.append({
                "observation_id": row["observation_id"], "company_ref": company_ref,
                "document_ref": document_ref, "metric_ref": row["metric_ref"],
                "label": row["label"], "unit": row["unit"],
                "judged_by_reviews": review_ids,
            })
    return {
        "unattributed_reviews": len(reviews),
        "unattributed_documents": len(documents),
        "observations_to_retract": len(targets),
        "by_company": dict(Counter(item["company_ref"] for item in targets)),
        "targets": targets,
    }


def requirement_effect(
    authority: CoverageMissionAuthority, companies: list[str]
) -> dict[str, Any]:
    """What each company's requirement list looks like now, for the record."""

    effect = {}
    for company_ref in sorted(companies):
        requirements = authority.metric_requirements(company_ref)
        effect[company_ref] = {
            "observations": len(authority.metric_observations(company_ref)),
            "requirements": [item["metric_ref"] for item in requirements],
        }
    return effect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="service.json")
    parser.add_argument("--retracted-by", required=True,
                        help="who is withdrawing these, e.g. agent:dalton-core")
    parser.add_argument("--apply", action="store_true",
                        help="without this the retraction is only described")
    args = parser.parse_args(list(argv) if argv is not None else None)

    service = json.loads(args.config.expanduser().resolve().read_text(encoding="utf-8"))
    core_db = Path(service["core_db"])
    state_dir = core_db.parent
    store = DaltonStore(str(core_db))
    try:
        authority = CoverageMissionAuthority(store)
        result = plan(store, state_dir)
        companies = sorted({item["company_ref"] for item in result["targets"]})
        result["before"] = requirement_effect(authority, companies)
        if args.apply:
            applied, already = [], []
            for item in result["targets"]:
                outcome = authority.retract_metric_observation(
                    item["observation_id"], reason=REASON, retracted_by=args.retracted_by)
                (already if outcome["status_marker"] == "duplicate" else applied).append(
                    item["observation_id"])
            result["applied"] = len(applied)
            result["already_retracted"] = len(already)
            result["after"] = requirement_effect(authority, companies)
        result["status"] = "applied" if args.apply else "planned"
    finally:
        store.close()
    # The target list is long and the counts are the answer; keep it readable.
    result.pop("targets", None)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script
    sys.exit(main())
