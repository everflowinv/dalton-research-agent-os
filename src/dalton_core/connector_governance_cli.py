"""Owner CLI for connector governance records.

``approve`` is the one human act the Core-hosted AlphaEngine acquisition
cannot perform for itself: it reads the committed ``proposed`` record, checks
that its hash still binds the packaged connector contract, flips ``status`` to
``approved`` under the named human principal and rewrites the record with a
fresh content hash.  Nothing else about the record may change; a record whose
``expected_source_hash`` / ``expected_schema_hash`` no longer match the
packaged contract is refused so the owner never approves a schema the catalog
will not ask for.

``show`` prints the record and whether it is currently approved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .connector_governance import (
    ConnectorGovernance,
    ConnectorGovernanceError,
    build_governance_record,
    load_connector_governance,
    write_governance_proposal,
)
from .store import canonical_json, content_hash


_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")


class GovernanceCliError(RuntimeError):
    pass


def approve_governance_record(path: str | Path, *, approved_by: str) -> dict[str, Any]:
    if _HUMAN_RE.fullmatch(approved_by) is None:
        raise GovernanceCliError("approved_by must be a human: principal")
    target = Path(path).expanduser().resolve()
    record = json.loads(target.read_text(encoding="utf-8"))
    try:
        governance = ConnectorGovernance(record)
    except ConnectorGovernanceError as exc:
        raise GovernanceCliError(str(exc)) from exc
    body = {key: value for key, value in record.items() if key != "content_hash"}
    if record.get("content_hash") != content_hash(body):
        raise GovernanceCliError("governance record hash does not bind its content; refuse to approve")
    try:
        version = int(record["id"].rsplit(":v", 1)[1])
    except (KeyError, ValueError, IndexError) as exc:
        raise GovernanceCliError("governance record id has no numeric version") from exc
    expected = build_governance_record(
        governance.kind,
        approved_by=record["approved_by"],
        status=record["status"],
        effective_from=record["effective_from"],
        max_lease_seconds=record["max_lease_seconds"],
        version=version,
    )
    if canonical_json(expected) != canonical_json(record):
        raise GovernanceCliError("governance record is not a packaged proposal; refuse to approve")
    if record["status"] == "approved":
        if record["approved_by"] != approved_by:
            raise GovernanceCliError("record is already approved by a different principal")
        return record
    approved = build_governance_record(
        governance.kind,
        approved_by=approved_by,
        status="approved",
        effective_from=record["effective_from"],
        max_lease_seconds=record["max_lease_seconds"],
        version=version,
    )
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(canonical_json(approved) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    # Re-load through the authority class so a record this CLI wrote is one
    # the launcher will accept.
    load_connector_governance(target)
    return approved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="print the record and its approval state")
    show.add_argument("--path", type=Path, required=True)
    approve = sub.add_parser("approve", help="approve a proposed record as a human principal")
    approve.add_argument("--path", type=Path, required=True)
    approve.add_argument("--approved-by", required=True, help="human:<owner>")
    propose = sub.add_parser("propose", help="write a proposed connector governance record")
    propose.add_argument("--kind", required=True)
    propose.add_argument("--path", type=Path, required=True)
    propose.add_argument("--approved-by", required=True, help="human:<owner>")
    propose.add_argument("--effective-from", default="2026-08-26T00:00:00+00:00")
    propose.add_argument("--version", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            governance = load_connector_governance(args.path)
            print(json.dumps({
                "id": governance.id,
                "status": governance.status,
                "approved": governance.approved,
                "approved_by": governance.approved_by,
                "content_hash": governance.content_hash,
            }, indent=1, sort_keys=True))
            return 0
        if args.command == "propose":
            record = write_governance_proposal(
                args.path,
                kind=args.kind,
                approved_by=args.approved_by,
                effective_from=args.effective_from,
                version=args.version,
            )
        else:
            record = approve_governance_record(args.path, approved_by=args.approved_by)
        print(json.dumps({
            "id": record["id"], "status": record["status"],
            "approved_by": record["approved_by"], "content_hash": record["content_hash"],
        }, indent=1, sort_keys=True))
        return 0
    except (GovernanceCliError, ConnectorGovernanceError, FileNotFoundError,
            FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
