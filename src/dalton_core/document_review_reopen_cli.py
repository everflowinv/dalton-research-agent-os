"""Human-operated, hash-bound supplemental document review execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .document_review_reopen import review_reopen_candidate
from .governance_cli import ephemeral_call


class SupplementalReviewCliError(RuntimeError):
    pass


def _bytes(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SupplementalReviewCliError(f"packet file hash drifted: {path.name}")
    return raw


def verify_packet(manifest_path: Path, expected_manifest_sha256: str, *,
                  core_db: Path, scheduler_db: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = _bytes(manifest_path, expected_manifest_sha256)
    try:
        manifest = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SupplementalReviewCliError("packet manifest is invalid") from exc
    if (not isinstance(manifest, Mapping)
            or set(manifest) != {"schema_version", "actor_ref", "candidate", "candidate_sha256", "params"}
            or manifest["schema_version"] != "0.1" or manifest["actor_ref"] != "human:lumos"
            or not isinstance(manifest["params"], list) or not manifest["params"]):
        raise SupplementalReviewCliError("packet manifest has invalid closed controls")
    base = manifest_path.resolve().parent
    candidate_path = (base / manifest["candidate"]).resolve()
    if candidate_path.parent != base:
        raise SupplementalReviewCliError("candidate must be inside packet directory")
    candidate = json.loads(_bytes(candidate_path, manifest["candidate_sha256"]))
    review = review_reopen_candidate(core_db=core_db, scheduler_db=scheduler_db, candidate=candidate)
    items = {item["review_id"]: item for item in candidate["items"]}
    params: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in manifest["params"]:
        if not isinstance(entry, Mapping) or set(entry) != {"file", "sha256"}:
            raise SupplementalReviewCliError("parameter manifest entry is invalid")
        path = (base / entry["file"]).resolve()
        if path.parent != base:
            raise SupplementalReviewCliError("parameter file must be inside packet directory")
        value = json.loads(_bytes(path, entry["sha256"]))
        if (not isinstance(value, Mapping)
                or set(value) != {"review_id", "expected_review_hash", "decision_ref", "failed_windows"}):
            raise SupplementalReviewCliError("parameter file has invalid closed controls")
        review_id = value["review_id"]
        item = items.get(review_id)
        if item is None or review_id in seen:
            raise SupplementalReviewCliError("parameter review binding is missing or duplicated")
        if (value["expected_review_hash"] != item["prior_review_hash"]
                or value["failed_windows"] != item["failed_windows"]
                or value["decision_ref"] != f"owner-decision:document-supplement:{candidate['candidate_hash']}"):
            raise SupplementalReviewCliError("parameter file does not match reviewed candidate")
        seen.add(review_id)
        params.append(dict(value))
    if seen != set(items) or set(review["ready_review_ids"]) != seen:
        raise SupplementalReviewCliError("packet does not cover every currently ready review")
    return dict(candidate), params


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review or human-execute a supplemental reading packet")
    parser.add_argument("--packet-manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--core-db", type=Path, required=True)
    parser.add_argument("--scheduler-db", type=Path, required=True)
    parser.add_argument("--token-config", type=Path)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    candidate, params = verify_packet(
        args.packet_manifest, args.expected_manifest_sha256,
        core_db=args.core_db, scheduler_db=args.scheduler_db,
    )
    if not args.execute:
        print(json.dumps({"status": "reviewed", "candidate_hash": candidate["candidate_hash"],
                          "ready": len(params), "writes": 0}, sort_keys=True))
        return 0
    if args.token_config is None or args.socket is None or not sys.stdin.isatty():
        raise SupplementalReviewCliError("execution requires an interactive terminal and writer endpoints")
    entered = input("Human reviewer: type the complete candidate hash to authorize these reviews: ").strip()
    if entered != candidate["candidate_hash"]:
        raise SupplementalReviewCliError("human confirmation did not match candidate hash")
    # Close the review-to-write race as far as the client can: hash and
    # authority-check every file again after the human confirmation. The
    # writer still performs the final per-review CAS.
    candidate, params = verify_packet(
        args.packet_manifest, args.expected_manifest_sha256,
        core_db=args.core_db, scheduler_db=args.scheduler_db,
    )
    results = [ephemeral_call(args.token_config, args.socket, actor_ref="human:lumos",
                              operation="reopen_mission_document_review", params=value)
               for value in params]
    print(json.dumps({"status": "executed_by_authenticated_human", "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
