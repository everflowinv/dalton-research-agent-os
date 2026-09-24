#!/usr/bin/env python3
"""Read-only: model families and the verifier-independence preflight, per workspace.

For every Dalton state directory (default: the legacy
``~/Library/Application Support/Dalton/state/dalton-core`` and every
``~/.dalton/workspaces/*/state/dalton-core``) this opens the model router
read-only and reports:

* ``unclassified_profile_ids`` -- live broker profiles whose family is
  ``unclassified:*`` (never independent of anything);
* ``family_drift`` -- profiles whose stored family differs from what the
  current code would project from the OpenClaw catalog (the catalog lane
  appends the corrected version on its next pass);
* ``document_verifier_preflight`` -- the exact mission-document model authority
  preflight the document lane runs at admission, including
  "verifier cannot remain independent of every producer family".

Nothing is written: the router is opened with ``read_only=True`` and the budget
ledger through the authority's own read-only path. Exit 0 when every preflight
passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dalton_core.model_router import ModelRouter

ROUTER_NAME = "model-router.sqlite"
CATALOG_LANE_NAME = "model-catalog-sync.json"


def default_state_dirs() -> list[Path]:
    """The legacy environment plus every ~/.dalton workspace that has a router."""

    home = Path.home()
    candidates = [home / "Library" / "Application Support" / "Dalton" / "state" / "dalton-core"]
    candidates += sorted((home / ".dalton" / "workspaces").glob("*/state/dalton-core"))
    return [path for path in candidates if (path / ROUTER_NAME).is_file()]


def _family_drift(router: ModelRouter, state_dir: Path, now: datetime) -> Any:
    from dalton_core.openclaw_catalog_reconcile import (
        _metadata_declarations, _router_broker_profiles, load_openclaw_config,
        openclaw_broker_profiles_from_config,
    )

    lane = state_dir / CATALOG_LANE_NAME
    if not lane.is_file():
        return {"status": "skipped", "reason": f"no {CATALOG_LANE_NAME}"}
    source = Path(json.loads(lane.read_text(encoding="utf-8"))["openclaw_config_path"])
    desired = {
        row["id"]: row["family"]
        for row in openclaw_broker_profiles_from_config(
            load_openclaw_config(source), checked_at=now,
            metadata_declarations=_metadata_declarations(router),
        )
    }
    held = _router_broker_profiles(router)
    return {
        profile_id: {"stored": held[profile_id].get("family"), "projected": family}
        for profile_id, family in sorted(desired.items())
        if profile_id in held and held[profile_id].get("family") != family
    }


def check_state_dir(state_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    from dalton_core.mission_document_model_authority import (
        MissionDocumentModelAuthority,
    )

    now = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {"state_dir": str(state_dir)}
    with ModelRouter(state_dir / ROUTER_NAME, read_only=True) as router:
        live = [profile for profile in router.latest_profiles()
                if str(profile.get("id", "")).startswith("profile:")
                and profile.get("status") != "retired"]
        report["unclassified_profile_ids"] = sorted(
            profile["id"] for profile in live
            if str(profile.get("family", "")).startswith("unclassified:"))
        try:
            report["family_drift"] = _family_drift(router, state_dir, now)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            report["family_drift"] = {"status": "failed",
                                      "reason": f"{type(exc).__name__}: {exc}"[:500]}
        try:
            _selection, proof = MissionDocumentModelAuthority(
                state_dir=state_dir, router=router, clock=lambda: now)()
            report["document_verifier_preflight"] = {
                "status": "ok",
                "families": {
                    stage: sorted({item["family"] for item in
                                   proof[stage]["configured_candidate_profiles"]
                                   if not item["preflight_reasons"]})
                    for stage in ("draft", "verifier")
                },
            }
        except Exception as exc:  # noqa: BLE001 - report, never raise
            report["document_verifier_preflight"] = {
                "status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:500]}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, action="append",
                        help="a dalton-core state directory; repeatable "
                             "(default: legacy plus every ~/.dalton workspace)")
    args = parser.parse_args(argv)
    state_dirs = args.state_dir or default_state_dirs()
    if not state_dirs:
        print("no Dalton state directory with a model router was found", file=sys.stderr)
        return 1
    reports = [check_state_dir(path.expanduser().resolve()) for path in state_dirs]
    print(json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item["document_verifier_preflight"]["status"] == "ok"
                    for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
