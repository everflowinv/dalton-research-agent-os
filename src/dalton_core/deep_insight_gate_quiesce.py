"""Pause a deep gate only between calls whose formal results and fees are durable.

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

SCHEMA = "deep-insight-gate-quiesce:0.1"
FIELDS = ("id", "pid", "started_at", "command", "model_config_path",
          "model_config_fingerprint", "verifier_model_config_path",
          "verifier_model_config_fingerprint", "company_ref", "source_fingerprint")


class GateQuiesced(BaseException):
    def __init__(self, request, completed):
        self.request, self.completed = request, completed
        super().__init__("deep insight gate quiesced at a settled call boundary")


class GateQuiesceUnproved(BaseException):
    """Stop dispatch, but do not claim that outstanding fees are settled."""


def _binding(ticket):
    return {key: ticket[key] for key in FIELDS}


def request_quiesce(run_dir: Path, *, expected_ticket_hash: str):
    from .lane_child_launcher import ticket_identity
    directory = Path(run_dir)
    path = directory / "ticket.json"
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("deep-gate quiesce requires an exact regular ticket")
    ticket = json.loads(path.read_text())
    if (ticket.get("status") != "running" or content_hash(ticket) != expected_ticket_hash
            or ticket_identity(ticket, path) is not True):
        raise ValueError("deep-gate quiesce ticket changed or live process identity is unproved")
    body = {"schema_version": SCHEMA, "ticket_binding": _binding(ticket),
            "ticket_hash_at_request": expected_ticket_hash}
    request = {**body, "content_hash": content_hash(body)}
    destination = directory / "quiesce-request.json"
    fd, temporary = tempfile.mkstemp(prefix=".deep-gate-quiesce-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(request, stream, sort_keys=True); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or json.loads(destination.read_text()) != request:
                raise ValueError("deep-gate quiesce request conflicts")
    finally:
        Path(temporary).unlink(missing_ok=True)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return request


def settled_call(model, call, mission, *, purpose):
    """Keep original success authority; old deep Work has no mission hash field."""
    from .deep_insight_gate_accounting import billed_work
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    from .scheduler import Scheduler
    with _authority_connection(model.scheduler_db) as c, recovery_readers(
            model.config['model_router_db'],model.config['budget_db'],now=model.clock())as(router,budget):
        reader=Scheduler.__new__(Scheduler);reader.connection=c
        authority=reader.work_order_authority(call['work_order_ref'])
        formal=reader.formal_result(call['work_order_ref'])
        if formal is None or budget is None:
            raise ValueError('deep-gate call lacks its formal and fee authority')
        work=authority['work_order'];result=formal['result_envelope']
        bill=billed_work(c,router,budget,authority,formal)
        if (work['metadata'].get('purpose')!=purpose
                or work['metadata'].get('mission_version_ref')!=mission['id']
                or bill['mission_hashes']!=[mission['content_hash']]
                or formal['terminal_state']!='succeeded' or result['status']!='succeeded'
                or result['id']!=call['result_envelope_ref']
                or result['invocation_ref']!=call['invocation_ref']
                or result['metadata'].get('route_decision_ref')!=call['route_decision_ref']
                or bill['unknown_fee_retained']
                or any(f['settlement_ref']is None for f in bill['fees'])):
            raise ValueError('deep-gate success or fee boundary is unproved')
        return bill


class RunQuiesce:
    def __init__(self, directory, producer_path, verifier_path, *, producer_bytes, verifier_bytes, accounting, company_ref, source_fingerprint):
        self.directory = Path(directory)
        self.paths = (str(producer_path), str(verifier_path))
        self.fingerprints = tuple(hashlib.sha256(b).hexdigest() for b in (producer_bytes, verifier_bytes))
        self.calls = []
        self.accounting = accounting
        self.company_ref = company_ref
        self.source_fingerprint = source_fingerprint

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
                raise ValueError("deep-gate quiesce request must be regular")
            request = json.loads(path.read_text())
            ticket = json.loads((self.directory / "ticket.json").read_text())
            body = {k: v for k, v in request.items() if k != "content_hash"}
            binding = _binding(ticket)
            if (set(request) != {"schema_version", "ticket_binding", "ticket_hash_at_request", "content_hash"}
                    or request["schema_version"] != SCHEMA
                    or request["content_hash"] != content_hash(body)
                    or request["ticket_binding"] != binding
                    or request["ticket_hash_at_request"] != content_hash(ticket)
                    or binding["company_ref"] != self.company_ref
                    or binding["source_fingerprint"] != self.source_fingerprint
                    or ticket["pid"] != os.getpid() or ticket["status"] != "running"
                    or (binding["model_config_path"], binding["verifier_model_config_path"]) != self.paths
                    or (binding["model_config_fingerprint"], binding["verifier_model_config_fingerprint"]) != self.fingerprints):
                raise ValueError("deep-gate quiesce request does not bind this live run and loaded configs")
            if self.accounting is not None:
                proof=self.accounting.snapshot()[2]
                if (not proof['cost_complete']
                        or set(proof['unknown_work_refs'])-self.accounting._allowed_unknowns()):
                    raise ValueError('deep-gate whole frozen-input costs are not fully proved for resume')
            completed = [settled_call(model, call, mission, purpose=purpose)
                         for model, call, mission, purpose in self.calls]
        except (ValueError, KeyError, OSError, sqlite3.Error,
                ModelRouterError, SchedulerError, ThesisImpactBudgetError) as exc:
            raise GateQuiesceUnproved(str(exc)) from exc
        raise GateQuiesced(request, completed)


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
    parser = argparse.ArgumentParser(description="Pause one deep gate after its current settled call")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--expected-ticket-hash", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(request_quiesce(args.run_dir, expected_ticket_hash=args.expected_ticket_hash), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
