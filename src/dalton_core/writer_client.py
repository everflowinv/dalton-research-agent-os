"""Client for Dalton Core's local writer service.

The client deliberately stores only a socket endpoint and a scoped token.  It
has no ``DaltonStore`` import, no SQLite connection, and no database path.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Mapping

from .writer_protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    RemoteAuthorizationError,
    RemoteError,
    RemoteProtocolError,
    decode_frame,
    parse_response,
    request_frame,
)


class WriterClient:
    def __init__(self, socket_path: str, token: str, *, timeout: float = 10.0):
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError("socket_path must be a non-empty string")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.socket_path = socket_path
        self._token = token
        self.timeout = float(timeout)

    def call(self, operation: str, params: Mapping[str, Any] | None = None, *, request_id: str | None = None) -> Any:
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation must be a non-empty string")
        request_id = request_id or uuid.uuid4().hex
        frame = request_frame(request_id, operation, params or {}, self._token)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(frame)
                reader = sock.makefile("rb")
                line = reader.readline(MAX_FRAME_BYTES + 1)
                reader.close()
        except OSError as exc:
            # Do not expose the endpoint, OS path, or nested traceback to the
            # runtime.  The caller gets a stable transport error only.
            raise RemoteError("transport_error", "writer service is unavailable", request_id=request_id) from None
        try:
            response = parse_response(decode_frame(line))
        except ProtocolError:
            raise RemoteProtocolError("protocol_error", "writer service returned a malformed response", request_id=request_id) from None
        if response.request_id != request_id:
            raise RemoteProtocolError("protocol_error", "writer service returned an unexpected request id", request_id=request_id)
        if not response.ok:
            error = response.error or {}
            code = str(error.get("code", "remote_error"))
            message = str(error.get("message", "writer service rejected the request"))
            error_type = RemoteAuthorizationError if code == "forbidden" else RemoteError
            raise error_type(code, message, request_id=request_id)
        return response.result

    def register_invocation(self, invocation: Mapping[str, Any]) -> Any:
        return self.call("register_invocation", {"invocation": dict(invocation)})

    def stage_change(self, **params: Any) -> Any:
        return self.call("stage_change", params)

    def verify_change(self, **params: Any) -> Any:
        return self.call("verify_change", params)

    def commit(self, **params: Any) -> Any:
        return self.call("commit", params)

    def commit_reviewed_candidate(self, **params: Any) -> Any:
        return self.call("commit_reviewed_candidate", params)

    def create_policy(self, **params: Any) -> Any:
        return self.call("create_policy", params)

    def current_pointer(self, thesis_id: str) -> Any:
        return self.call("current_pointer", {"thesis_id": thesis_id})

    def get_version(self, version_id: str) -> Any:
        return self.call("get_version", {"version_id": version_id})

    def list_events(self, aggregate_id: str | None = None) -> Any:
        return self.call("list_events", {} if aggregate_id is None else {"aggregate_id": aggregate_id})

    def active_policy(self) -> Any:
        return self.call("active_policy", {})

    def register_evidence(self, **params: Any) -> Any:
        return self.call("register_evidence", params)

    def register_claim(self, **params: Any) -> Any:
        return self.call("register_claim", params)

    def relate_evidence(self, **params: Any) -> Any:
        return self.call("relate_evidence", params)

    def adjudicate_claim(self, **params: Any) -> Any:
        return self.call("adjudicate_claim", params)

    def submit_capability_proposal(self, **params: Any) -> Any:
        return self.call("submit_capability_proposal", params)

    def record_capability_evaluation(self, **params: Any) -> Any:
        return self.call("record_capability_evaluation", params)

    def decide_capability_promotion(self, **params: Any) -> Any:
        return self.call("decide_capability_promotion", params)

    def rollback_capability(self, **params: Any) -> Any:
        return self.call("rollback_capability", params)

    def active_capability(self, capability_ref: str) -> Any:
        return self.call("active_capability", {"capability_ref": capability_ref})

    def get_capability_version(self, revision_ref: str) -> Any:
        return self.call("get_capability_version", {"revision_ref": revision_ref})

    def get_capability_evaluation(self, evaluation_id: str) -> Any:
        return self.call("get_capability_evaluation", {"evaluation_id": evaluation_id})

    def get_capability_decision(self, decision_id: str) -> Any:
        return self.call("get_capability_decision", {"decision_id": decision_id})

    def capability_pointer_history(self, capability_ref: str) -> Any:
        return self.call("capability_pointer_history", {"capability_ref": capability_ref})

    def create_agenda_policy(self, **params: Any) -> Any:
        return self.call("create_agenda_policy", params)

    def active_agenda_policy(self, **params: Any) -> Any:
        return self.call("active_agenda_policy", params)

    def agenda_budget_status(self, **params: Any) -> Any:
        return self.call("agenda_budget_status", params)

    def create_mandate(self, **params: Any) -> Any:
        return self.call("create_mandate", params)

    def active_mandates(self, **params: Any) -> Any:
        return self.call("active_mandates", params)

    def create_priority_override(self, **params: Any) -> Any:
        return self.call("create_priority_override", params)

    def active_priority_overrides(self, **params: Any) -> Any:
        return self.call("active_priority_overrides", params)

    def set_agenda_pause(self, **params: Any) -> Any:
        return self.call("set_agenda_pause", params)

    def agenda_control_state(self) -> Any:
        return self.call("agenda_control_state", {})

    def register_perception_snapshot(self, **params: Any) -> Any:
        return self.call("register_perception_snapshot", params)

    def materialize_agenda_context(self, **params: Any) -> Any:
        return self.call("materialize_agenda_context", params)

    def get_agenda_mandate_version(self, **params: Any) -> Any:
        return self.call("get_agenda_mandate_version", params)

    def get_agenda_policy_version(self, **params: Any) -> Any:
        return self.call("get_agenda_policy_version", params)

    def get_perception_snapshot(self, **params: Any) -> Any:
        return self.call("get_perception_snapshot", params)

    def start_agenda_cycle(self, **params: Any) -> Any:
        return self.call("start_agenda_cycle", params)

    def add_agenda_candidates(self, **params: Any) -> Any:
        return self.call("add_agenda_candidates", params)

    def decide_agenda_cycle(self, **params: Any) -> Any:
        return self.call("decide_agenda_cycle", params)

    def fail_agenda_cycle(self, **params: Any) -> Any:
        return self.call("fail_agenda_cycle", params)

    def agenda_cycle(self, cycle_id: str) -> Any:
        return self.call("agenda_cycle", {"cycle_id": cycle_id})

    def agenda_cycle_by_key(self, cycle_key: str) -> Any:
        return self.call("agenda_cycle_by_key", {"cycle_key": cycle_key})

    def pending_agenda_outbox(self, limit: int = 100) -> Any:
        return self.call("pending_agenda_outbox", {"limit": limit})

    def claim_agenda_outbox(self, **params: Any) -> Any:
        return self.call("claim_agenda_outbox", params)

    def record_agenda_delivery(self, **params: Any) -> Any:
        return self.call("record_agenda_delivery", params)

    def list_agenda_feedback_targets(self, **params: Any) -> Any:
        return self.call("list_agenda_feedback_targets", params)

    def record_agenda_feedback(self, **params: Any) -> Any:
        return self.call("record_agenda_feedback", params)

    def create_workflow_version(self, **params: Any) -> Any:
        return self.call("create_workflow_version", params)

    def link_work_order(self, **params: Any) -> Any:
        return self.call("link_work_order", params)

    def record_usage(self, invocation_ref: str, **params: Any) -> Any:
        return self.call("record_usage", {"invocation_ref": invocation_ref, **params})

    def create_price_rate_version(self, price_rate_ref: str, **params: Any) -> Any:
        return self.call("create_price_rate_version", {"price_rate_ref": price_rate_ref, **params})

    def record_cost(self, usage_entry_ref: str, **params: Any) -> Any:
        return self.call("record_cost", {"usage_entry_ref": usage_entry_ref, **params})

    def register_driver_pack(self, driver_pack_ref: str, **params: Any) -> Any:
        return self.call(
            "register_driver_pack", {"driver_pack_ref": driver_pack_ref, **params}
        )

    def get_driver_pack(self, version_id: str) -> Any:
        return self.call("get_driver_pack", {"version_id": version_id})

    def propose_thesis_admission(self, **params: Any) -> Any:
        return self.call("propose_thesis_admission", params)

    def get_thesis_admission_candidate(self, candidate_id: str) -> Any:
        return self.call(
            "get_thesis_admission_candidate", {"candidate_id": candidate_id}
        )

    def decide_thesis_admission(self, **params: Any) -> Any:
        return self.call("decide_thesis_admission", params)

    def get_thesis_admission_decision(self, decision_id: str) -> Any:
        return self.call(
            "get_thesis_admission_decision", {"decision_id": decision_id}
        )

    def propose_model_input(self, **params: Any) -> Any:
        return self.call("propose_model_input", params)

    def get_model_input_candidate(self, candidate_id: str) -> Any:
        return self.call("get_model_input_candidate", {"candidate_id": candidate_id})

    def get_model_input_decision(self, decision_id: str) -> Any:
        return self.call("get_model_input_decision", {"decision_id": decision_id})

    def get_model_input_version(self, version_id: str) -> Any:
        return self.call("get_model_input_version", {"version_id": version_id})

    def current_model_input(self, model_input_ref: str) -> Any:
        return self.call("current_model_input", {"model_input_ref": model_input_ref})

    def decide_model_input(self, **params: Any) -> Any:
        return self.call("decide_model_input", params)

    def record_model_run(self, **params: Any) -> Any:
        return self.call("record_model_run", params)

    def record_model_reconciliation(self, **params: Any) -> Any:
        return self.call("record_model_reconciliation", params)

    def get_model_reconciliations(self, model_run_version_ref: str) -> Any:
        return self.call(
            "get_model_reconciliations",
            {"model_run_version_ref": model_run_version_ref},
        )

    def model_input_integrity_report(self) -> Any:
        return self.call("model_input_integrity_report", {})

    def register_industry_evidence_pack(self, evidence_pack_ref: str, **params: Any) -> Any:
        return self.call(
            "register_industry_evidence_pack", {"evidence_pack_ref": evidence_pack_ref, **params}
        )

    def get_industry_evidence_pack(self, version_id: str) -> Any:
        return self.call("get_industry_evidence_pack", {"version_id": version_id})

    def register_company_overlay(self, overlay_ref: str, **params: Any) -> Any:
        return self.call(
            "register_company_overlay", {"overlay_ref": overlay_ref, **params}
        )

    def get_company_overlay(self, version_id: str) -> Any:
        return self.call("get_company_overlay", {"version_id": version_id})

    def industry_brief_snapshot(
        self, evidence_pack_version_id: str, company_overlay_version_ids: list[str],
    ) -> Any:
        return self.call("industry_brief_snapshot", {
            "evidence_pack_version_id": evidence_pack_version_id,
            "company_overlay_version_ids": company_overlay_version_ids,
        })

    def render_industry_brief_markdown(
        self, evidence_pack_version_id: str, company_overlay_version_ids: list[str],
    ) -> Any:
        return self.call("render_industry_brief_markdown", {
            "evidence_pack_version_id": evidence_pack_version_id,
            "company_overlay_version_ids": company_overlay_version_ids,
        })

    def industry_research_integrity_report(self) -> Any:
        return self.call("industry_research_integrity_report", {})

    def thesis_impact_targets(
        self, company_thesis_refs: Mapping[str, str], *, limit: int = 100
    ) -> Any:
        return self.call(
            "thesis_impact_targets",
            {"company_thesis_refs": dict(company_thesis_refs), "limit": limit},
        )

    def thesis_impact_start(self, *, plan_version_ref: str, thesis_ref: str) -> Any:
        return self.call(
            "thesis_impact_start",
            {"plan_version_ref": plan_version_ref, "thesis_ref": thesis_ref},
        )

    def thesis_impact_advance_assessment(
        self, *, plan_version_ref: str, thesis_ref: str
    ) -> Any:
        return self.call(
            "thesis_impact_advance_assessment",
            {"plan_version_ref": plan_version_ref, "thesis_ref": thesis_ref},
        )

    def thesis_impact_advance_verification(
        self,
        *,
        plan_version_ref: str,
        thesis_ref: str,
        assessment_ref: str,
    ) -> Any:
        return self.call(
            "thesis_impact_advance_verification",
            {
                "plan_version_ref": plan_version_ref,
                "thesis_ref": thesis_ref,
                "assessment_ref": assessment_ref,
            },
        )

    def thesis_impact_assessment(self, assessment_ref: str) -> Any:
        return self.call("thesis_impact_assessment", {"assessment_ref": assessment_ref})

    def thesis_impact_invocation(self, invocation_ref: str) -> Any:
        return self.call("thesis_impact_invocation", {"invocation_ref": invocation_ref})

    def thesis_impact_find_invocation(self, invocation_ref: str) -> Any:
        return self.call(
            "thesis_impact_find_invocation", {"invocation_ref": invocation_ref}
        )

    def __repr__(self) -> str:
        return f"WriterClient(socket_path=<local>, timeout={self.timeout!r})"


Client = WriterClient
