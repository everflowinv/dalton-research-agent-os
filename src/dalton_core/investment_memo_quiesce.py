"""Pause a memo only between calls whose formal results and fees are durable.

This is deployment control, never research completion or retry authority.
Resuming uses the ordinary content-addressed model calls, not cached parsed prose.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import sqlite3
from pathlib import Path

from .contracts import ResultEnvelope, WorkOrder
from .model_router import ModelRouterError
from .scheduler import SchedulerError
from .thesis_impact_budget import ThesisImpactBudgetError
from .store import canonical_json, content_hash

SCHEMA = "investment-memo-quiesce:0.1"
FIELDS = ("id", "pid", "started_at", "command", "model_config_path",
          "model_config_fingerprint", "verifier_model_config_path",
          "verifier_model_config_fingerprint")


class MemoQuiesced(BaseException):
    def __init__(self, request, completed):
        self.request, self.completed = request, completed
        super().__init__("investment memo quiesced at a settled call boundary")


class MemoQuiesceUnproved(BaseException):
    """Stop dispatch, but do not claim that outstanding fees are settled."""


def _binding(ticket):
    return {key: ticket[key] for key in FIELDS}


def request_quiesce(run_dir: Path, *, expected_ticket_hash: str):
    from .lane_child_launcher import ticket_identity
    directory = Path(run_dir)
    path = directory / "ticket.json"
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("memo quiesce requires an exact regular ticket")
    ticket = json.loads(path.read_text())
    if (ticket.get("status") != "running" or content_hash(ticket) != expected_ticket_hash
            or ticket_identity(ticket, path) is not True):
        raise ValueError("memo quiesce ticket changed or live process identity is unproved")
    body = {"schema_version": SCHEMA, "ticket_binding": _binding(ticket),
            "ticket_hash_at_request": expected_ticket_hash}
    request = {**body, "content_hash": content_hash(body)}
    destination = directory / "quiesce-request.json"
    fd, temporary = tempfile.mkstemp(prefix=".memo-quiesce-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(request, stream, sort_keys=True); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or json.loads(destination.read_text()) != request:
                raise ValueError("memo quiesce request conflicts")
    finally:
        Path(temporary).unlink(missing_ok=True)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return request


def settled_call(model, call, mission, *, purpose):
    """Read exact formal/route and every admission for this Work, without DDL."""
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    with _authority_connection(model.scheduler_db) as scheduler, recovery_readers(
            model.config["model_router_db"], model.config["budget_db"],
            now=model.clock()) as (router, budget):
        wr = scheduler.execute("SELECT * FROM scheduler_work_orders WHERE work_order_id=?",
                               (call["work_order_ref"],)).fetchone()
        fr = scheduler.execute("SELECT * FROM scheduler_formal_results WHERE work_order_id=? "
                               "ORDER BY attempt_number DESC,created_at DESC,result_record_id DESC LIMIT 1",
                               (call["work_order_ref"],)).fetchone()
        if wr is None or fr is None or budget is None:
            raise ValueError("memo call has no complete formal and budget authority")
        work = WorkOrder.from_dict(json.loads(wr["work_order_json"])).to_dict()
        result = ResultEnvelope.from_dict(json.loads(fr["result_envelope_json"])).to_dict()
        formal = {k: fr[col] for k, col in (
            ("id", "result_record_id"), ("work_order_id", "work_order_id"),
            ("attempt_number", "attempt_number"), ("result_envelope_id", "result_envelope_id"),
            ("result_envelope_hash", "result_envelope_hash"), ("terminal_state", "terminal_state"),
            ("created_at", "created_at"))}
        metadata = work["metadata"]
        if (canonical_json(work) != wr["work_order_json"]
                or content_hash(work) != wr["work_order_hash"]
                or work["id"] != call["work_order_ref"]
                or metadata.get("purpose") != purpose
                or metadata.get("mission_version_ref") != mission["id"]
                or metadata.get("mission_version_hash") != mission["content_hash"]
                or canonical_json(result) != fr["result_envelope_json"]
                or content_hash(result) != fr["result_envelope_hash"]
                or content_hash(formal) != fr["content_hash"]
                or fr["terminal_state"] != "succeeded" or result["status"] != "succeeded"
                or result["work_order_ref"] != work["id"]
                or result["id"] != call["result_envelope_ref"]
                or result.get("invocation_ref") != call["invocation_ref"]
                or result["metadata"].get("route_decision_ref") != call["route_decision_ref"]):
            raise ValueError("memo formal call binding drifted")
        route = router.get_decision(call["route_decision_ref"])
        if (route.get("outcome") != "selected" or route.get("work_order_ref") != work["id"]
                or route.get("work_order_hash") != wr["work_order_hash"]
                or route.get("attempt_number") != fr["attempt_number"]):
            raise ValueError("memo route binding drifted")
        rows = budget.connection.execute(
            "SELECT attempt_number,phase FROM thesis_impact_day_admissions WHERE work_order_ref=? "
            "ORDER BY attempt_number,phase",
            (work["id"],)).fetchall()
        if not rows:
            raise ValueError("memo paid call has no admission")
        settled = []
        selected = False
        for row in rows:
            proof = budget.admission(work_order_ref=work["id"],
                                     attempt_number=row["attempt_number"], phase=row["phase"])
            binding = proof["mission_binding"] or {}
            if (proof["settlement"] is None
                    or binding.get("mission_version_ref") != mission["id"]
                    or binding.get("mission_version_hash") != mission["content_hash"]):
                raise ValueError("memo admission is unsettled or belongs to another mission")
            admission = proof["admission"]
            # A fallback chain shares its first route's reservation. Require
            # the admission's own exact route and current attempt, not that
            # the eventual serving route was necessarily the first link.
            admitted_route = router.get_decision(admission["route_decision_ref"])
            if (admitted_route.get("work_order_ref") != work["id"]
                    or admitted_route.get("work_order_hash") != wr["work_order_hash"]
                    or admitted_route.get("attempt_number") != row["attempt_number"]):
                raise ValueError("memo admission route binding drifted")
            selected |= row["attempt_number"] == fr["attempt_number"]
            settled.append({"admission_ref": admission["admission_id"],
                            "admission_hash": admission["content_hash"],
                            "settlement_ref": proof["settlement"]["settlement_id"],
                            "settlement_hash": proof["settlement"]["content_hash"]})
        if not selected:
            raise ValueError("memo selected route has no settled admission")
        return {"work_order_ref": work["id"], "work_order_hash": wr["work_order_hash"],
                "result_envelope_ref": result["id"], "formal_hash": fr["content_hash"],
                "settlements": settled, "cost_status": call.get("cost_status", "unproved")}


class RunQuiesce:
    def __init__(self, directory, producer_path, verifier_path, *, producer_bytes, verifier_bytes):
        self.directory = Path(directory)
        self.paths = (str(producer_path), str(verifier_path))
        self.fingerprints = tuple(hashlib.sha256(b).hexdigest() for b in (producer_bytes, verifier_bytes))
        self.calls = []

    def returned(self, model, call, mission, *, purpose):
        # Only identities/accounting travel to the pause receipt, never prose.
        identity = {key: call.get(key) for key in (
            "work_order_ref", "result_envelope_ref", "route_decision_ref", "invocation_ref",
            "cost_micros", "cost_status")}
        self.calls.append((model, identity, dict(mission), purpose))
        self.checkpoint()

    def checkpoint(self):
        path = self.directory / "quiesce-request.json"
        if not path.exists():
            return
        try:
            if path.is_symlink():
                raise ValueError("memo quiesce request must be regular")
            request = json.loads(path.read_text())
            ticket = json.loads((self.directory / "ticket.json").read_text())
            body = {k: v for k, v in request.items() if k != "content_hash"}
            binding = _binding(ticket)
            if (set(request) != {"schema_version", "ticket_binding", "ticket_hash_at_request", "content_hash"}
                    or request["schema_version"] != SCHEMA
                    or request["content_hash"] != content_hash(body)
                    or request["ticket_binding"] != binding
                    or request["ticket_hash_at_request"] != content_hash(ticket)
                    or ticket["pid"] != os.getpid() or ticket["status"] != "running"
                    or (binding["model_config_path"], binding["verifier_model_config_path"]) != self.paths
                    or (binding["model_config_fingerprint"], binding["verifier_model_config_fingerprint"]) != self.fingerprints):
                raise ValueError("memo quiesce request does not bind this live run and loaded configs")
            completed = [settled_call(model, call, mission, purpose=purpose)
                         for model, call, mission, purpose in self.calls]
        except (ValueError, KeyError, OSError, sqlite3.Error,
                ModelRouterError, SchedulerError, ThesisImpactBudgetError) as exc:
            raise MemoQuiesceUnproved(str(exc)) from exc
        raise MemoQuiesced(request, completed)


def quiesced_summary_matches(summary, ticket):
    if not isinstance(summary, dict) or summary.get("status") != "quiesced":
        return False
    proof = summary.get("quiesce")
    if not isinstance(proof, dict):
        return False
    original = {**ticket, "status": "running", "exit_code": None, "completed_at": None}
    for k in ("orphaned_reason", "adopted_from_summary", "summary"):
        original.pop(k, None)
    try:
        return (set(proof) == {"schema_version", "ticket_binding", "ticket_hash_at_request", "content_hash"}
                and proof["schema_version"] == SCHEMA
                and proof["content_hash"] == content_hash({k: v for k, v in proof.items() if k != "content_hash"})
                and proof["ticket_binding"] == _binding(original)
                and proof["ticket_hash_at_request"] == content_hash(original)
                and summary.get("resumable") is True
                and summary.get("stop_reason") == "cooperative_quiesce")
    except KeyError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pause one memo after its current settled call")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--expected-ticket-hash", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(request_quiesce(args.run_dir, expected_ticket_hash=args.expected_ticket_hash), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
