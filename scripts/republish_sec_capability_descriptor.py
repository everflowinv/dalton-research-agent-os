"""Publish the SEC capability descriptor the approved governance record expects.

Found on live while the mission started queuing SEC filings: the lane refused
every run with "published SEC capability descriptor differs from the governed
spec".  The catalog held a descriptor published under governance v1 (an older
schema identity and policy ref) while the writer was configured with the
owner-approved v2, so the lane failed closed and nothing could run.  Nobody had
noticed because, until P10d, nothing dispatched to that lane.

This publishes the descriptor version the *current* governance record implies.
It makes an already-approved governance decision effective; it does not approve
anything, does not touch the Research Ledger, and refuses when the published
descriptor already matches.

    python3 scripts/republish_sec_capability_descriptor.py \\
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core" \\
        --governance .../connector-governance/sec-company-facts-v2.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def republish(state_dir: Path, governance_path: Path, *, apply: bool) -> dict[str, Any]:
    from dalton_core.capability_catalog import CapabilityCatalog, CapabilityNotFound
    from dalton_core.connector_governance import load_connector_governance
    from dalton_core.connector_inventory import load_packaged_connector_inventory
    from dalton_core.sec_authority_harness import PUBLIC_PERMISSIONS
    from dalton_core.sec_company_facts_lane import OPERATION, sec_descriptor_spec

    governance = load_connector_governance(governance_path)
    template = load_packaged_connector_inventory()["templates"]["sec"]
    # The lane stamps the descriptor with the governance record's own effective
    # time, as a wire string; anything else is not the descriptor it compares.
    from dalton_core.sec_company_facts_lane import _wire_time

    created_at = _wire_time(datetime.fromisoformat(governance.effective_from))
    spec = sec_descriptor_spec(
        template, PUBLIC_PERMISSIONS, created_at,
        capability_policy_ref=governance.policy_ref, operation_name=OPERATION,
    )
    # The catalog refuses to publish without an approval authority, and the
    # governance record is that authority: the same resolvers the lane itself
    # passes when it publishes a missing descriptor.
    catalog = CapabilityCatalog(
        str(state_dir / "catalog.sqlite"),
        approval_resolver=governance.approval,
        policy_resolver=governance.policy,
    )
    try:
        try:
            current = catalog.describe(spec["id"], visibility_scopes=list(spec.get("visibility_scopes", ["research"])))
        except CapabilityNotFound:
            current = None
        report: dict[str, Any] = {
            "capability_id": spec["id"],
            "governance_policy_ref": governance.policy_ref,
            "expected_source_hash": spec["source_hash"],
            "expected_schema_hash": spec["schema_hash"],
            "published_source_hash": None if current is None else current.source_hash,
            "published_schema_hash": None if current is None else current.schema_hash,
            "published_policy_ref": None if current is None else current.eligibility.policy_ref,
        }
        matches = current is not None and (
            current.source_hash == spec["source_hash"]
            and current.schema_hash == spec["schema_hash"]
            and current.eligibility.policy_ref == governance.policy_ref
            and current.permissions.to_dict() == spec["permissions"]
        )
        report["matches"] = matches
        if matches:
            report["status"] = "already_current"
            return report
        if not apply:
            report["status"] = "would_publish"
            return report
        # A descriptor is a version chain: the replacement follows the version
        # already published, it does not overwrite it.
        if current is not None:
            spec = {**spec, "version": int(current.version) + 1}
            report["publishes_version"] = spec["version"]
        published = catalog.publish(spec)
        report["status"] = "published"
        report["revision_ref"] = getattr(published, "revision_ref", None)
        return report
    finally:
        catalog.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True)
    parser.add_argument("--apply", action="store_true", help="publish; without it, only report")
    args = parser.parse_args(argv)
    report = republish(
        Path(os.path.expanduser(args.state_dir)).resolve(),
        Path(os.path.expanduser(args.governance)).resolve(),
        apply=args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
