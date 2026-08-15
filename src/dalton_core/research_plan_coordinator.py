"""Authority-bound WorkOrder admission coordinator for the research plan tree.

The planner start records the complete four-node tree (SEC connector ->
authority resolver -> source/numeric verifier -> candidate staging) but
enqueues only the root connector WorkOrder.  This coordinator is the only
admission path for the three downstream nodes: it admits exactly the child of
an exact upstream node after that upstream node is proved to have succeeded
inside the Scheduler authority, and it never accepts a caller-supplied success
boolean or opaque payload as authorization.

Admission is a pure read-verify + single-enqueue operation:

- every node identity is re-derived from the immutable ResearchPlanVersion
  (never from caller content), so wrong-plan, wrong-workflow or
  wrong-upstream substitution fails closed;
- the exact plan start binding (approval, WorkflowRunVersion, WorkOrderLink
  rows, root WorkOrder) is re-read and re-validated on every call, so a
  tampered plan, workflow or link fails closed before any admission is
  attempted;
- the upstream node must exist in ``scheduler_work_orders`` byte-identical to
  plan authority, its full attempt-event chain must re-verify canonically, the
  latest attempt must be ``succeeded``, the formal result must be terminal
  ``succeeded``, and the ResultEnvelope must bind the exact WorkOrder;
- for the connector edge, the succeeded ResultEnvelope must additionally bind
  its actual ConnectorRunnerRequest.  Exactly one completion receipt must bind
  the Scheduler result, the compiled request and the exact WorkOrder attempt.
  A v0.2 receipt also re-reads the actual request and terminal ``responded``
  event from the Core runner journal.  Receipt source/artifact refs must equal
  the ResultEnvelope outputs.  Every internal stage uses a closed, hashed
  ``ResearchPlanStageOutput`` that binds plan, step, output contract, exact
  upstream result and stage-specific typed ref/hash records; an opaque success
  payload cannot admit its child;
- enqueue is performed through the Scheduler's deterministic idempotency
  (the child identity and idempotency key are plan-derived), so replayed
  admission converges on the same WorkOrder and a crash between verification
  and enqueue cannot create a second divergent task tree; a conflicting
  child row fails closed.

Outcomes are closed: ``fresh``/``duplicate`` (child admitted), ``complete``
(upstream is the final tree node - no child exists), ``pending_upstream``
(upstream still ready/leased/retryable), ``blocked`` (upstream terminated
unsuccessfully).  Tampering, missing rows, wrong bindings, wrong upstream
results never admit anything.  Pending work returns ``pending_upstream``;
failed or plan-exhausted work returns ``blocked``; authority drift raises
``ResearchPlanCoordinatorConflict``.

This slice creates no database schema, no capability lease, no credential,
no Ledger write and no second queue/DAG system; the Scheduler stays the only queue
authority.  The connector receipt chain is read from the exact
``ResearchCoordinatorStore`` instance the connector path already writes
(``research_runner_requests``/``research_completion_receipts``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .connector_runner import (
    validate_connector_runner_request,
    validate_connector_runner_response,
)
from .contracts import ResultEnvelope
from .research_coordinator import (
    ResearchCoordinatorStore,
    validate_connector_completion_receipt,
)
from .research_plan import (
    ResearchPlanAuthority,
    ResearchPlanError,
    _plan_link_specs,
    _plan_work_orders,
    plan_start_ref_for,
    read_exact_research_plan_start,
    read_exact_research_plan_version,
)
from .scheduler import Scheduler
from .store import canonical_json, content_hash

_NON_TERMINAL_STATES = frozenset({"ready", "leased", "retryable"})
_STAGE_RECORD_KINDS = {
    "authority_resolver": ("authority_resolution",),
    "verifier": ("source_verification", "numeric_verification"),
    "candidate_staging": ("candidate_evidence", "candidate_claim"),
}
_STAGE_RECORD_PREFIXES = {
    "authority_resolution": "authority-resolution:",
    "source_verification": "verification-bundle:",
    "numeric_verification": "verification-bundle:",
    "candidate_evidence": "candidate-evidence-version:",
    "candidate_claim": "candidate-claim-version:",
}


class ResearchPlanCoordinatorError(ResearchPlanError):
    """Base error for the plan-tree admission coordinator."""


class ResearchPlanCoordinatorConflict(ResearchPlanCoordinatorError):
    """The admission boundary detected tampering or broken authority bindings."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchPlanCoordinatorError(f"{name} must be a non-empty string")
    return value


def _stage_output_ref(
    *,
    plan_version_ref: str,
    step_ref: str,
    upstream_result_ref: str,
    upstream_result_hash: str,
) -> str:
    identity = {
        "plan_version_ref": plan_version_ref,
        "step_ref": step_ref,
        "upstream_result_ref": upstream_result_ref,
        "upstream_result_hash": upstream_result_hash,
    }
    return f"research-plan-stage-output:{content_hash(identity)[:32]}"


def _reverify_scheduler_work_order(
    cursor: sqlite3.Cursor, work_order_id: str, expected: Mapping[str, Any]
) -> dict[str, Any]:
    """One WorkOrder row must exist and be byte-identical to plan authority."""

    row = cursor.execute(
        "SELECT * FROM scheduler_work_orders "
        "WHERE work_order_id=?",
        (work_order_id,),
    ).fetchone()
    if row is None:
        raise ResearchPlanCoordinatorConflict(
            f"Scheduler WorkOrder {work_order_id} is not admitted"
        )
    # Hash the plan-derived body directly; the stored hash must match both
    # the body bytes and the plan authority.
    expected_hash = content_hash(expected)
    if (
        row["work_order_json"] != canonical_json(expected)
        or row["work_order_hash"] != expected_hash
    ):
        raise ResearchPlanCoordinatorConflict(
            "Scheduler WorkOrder drifted from immutable plan authority"
        )
    policy_row = cursor.execute(
        "SELECT policy_json,policy_hash,created_at FROM scheduler_policy_versions "
        "WHERE policy_version_id=?",
        (row["policy_version_id"],),
    ).fetchone()
    if policy_row is None:
        raise ResearchPlanCoordinatorConflict(
            "Scheduler WorkOrder lacks its frozen policy version"
        )
    try:
        policy = json.loads(policy_row["policy_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchPlanCoordinatorConflict(
            "scheduler policy record_json is not valid JSON"
        ) from exc
    if (
        not isinstance(policy, Mapping)
        or canonical_json(policy) != policy_row["policy_json"]
        or policy.get("id") != row["policy_version_id"]
        or policy.get("created_at") != policy_row["created_at"]
        or content_hash({key: value for key, value in policy.items() if key != "created_at"})
        != policy_row["policy_hash"]
        or policy.get("max_attempts") != row["max_attempts"]
    ):
        raise ResearchPlanCoordinatorConflict(
            "Scheduler WorkOrder frozen policy binding drifted"
        )
    return dict(row)


def _reverify_lease(
    cursor: sqlite3.Cursor,
    lease_revision_id: str,
    *,
    work_order_id: str,
    attempt_number: int,
) -> dict[str, Any]:
    row = cursor.execute(
        "SELECT * FROM scheduler_leases WHERE lease_revision_id=?",
        (lease_revision_id,),
    ).fetchone()
    if row is None:
        raise ResearchPlanCoordinatorConflict(
            "scheduler attempt event lacks its exact lease revision"
        )
    wire = {
        "schema_version": "0.1",
        "id": row["lease_revision_id"],
        "lease_id": row["lease_id"],
        "lease_version": row["lease_version"],
        "created_at": row["created_at"],
        "work_order_ref": row["work_order_id"],
        "attempt_number": row["attempt_number"],
        "owner_ref": row["owner_ref"],
        "lease_token_hash": row["lease_token_hash"],
        "issued_at": row["issued_at"],
        "renewed_at": row["renewed_at"],
        "expires_at": row["expires_at"],
        "prior_lease_ref": row["prior_lease_revision_id"],
    }
    if (
        content_hash(wire) != row["content_hash"]
        or wire["work_order_ref"] != work_order_id
        or wire["attempt_number"] != attempt_number
    ):
        raise ResearchPlanCoordinatorConflict(
            "scheduler lease revision drifted from its attempt"
        )
    return {**wire, "content_hash": row["content_hash"]}


def _reverify_attempt_event(row: Any, *, prior_event_id: str | None) -> dict[str, Any]:
    """Rebuild one immutable scheduler attempt event and re-check its hash.

    New events are hashed with ``wire_version`` and ``not_before`` declared
    (epoch 0.2); historical rows carry NULL ``wire_version`` and were hashed
    without ``not_before``, matching the Scheduler's stated hash epochs.
    """

    event_id = row["event_id"]
    original_prior = row["prior_event_id"]
    if original_prior != prior_event_id:
        raise ResearchPlanCoordinatorConflict(
            "scheduler attempt chain prior-event binding drifted"
        )
    wire = {
        "schema_version": "0.1",
        "id": event_id,
        "created_at": row["created_at"],
        "work_order_ref": row["work_order_id"],
        "attempt_number": row["attempt_number"],
        "state": row["state"],
        "lease_ref": row["lease_revision_id"],
        "result_envelope_ref": row["result_envelope_id"],
        "result_envelope_hash": row["result_envelope_hash"],
        "reason": row["reason"],
        "prior_event_ref": original_prior,
    }
    if row["wire_version"] == "0.2":
        wire["wire_version"] = "0.2"
        wire["not_before"] = row["not_before"]
    if content_hash(wire) != row["content_hash"]:
        raise ResearchPlanCoordinatorConflict(
            "scheduler attempt event content_hash mismatch"
        )
    return wire


def _attempt_chain(cursor: sqlite3.Cursor, work_order_id: str) -> list[dict[str, Any]]:
    rows = cursor.execute(
        "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? "
        "ORDER BY event_seq",
        (work_order_id,),
    ).fetchall()
    if not rows:
        raise ResearchPlanCoordinatorConflict(
            f"Scheduler WorkOrder {work_order_id} has no attempt history"
        )
    events: list[dict[str, Any]] = []
    prior: str | None = None
    prior_state: str | None = None
    prior_attempt = 0
    for row in rows:
        if row["work_order_id"] != work_order_id:
            raise ResearchPlanCoordinatorConflict(
                "scheduler attempt event drifted from its WorkOrder history"
            )
        event = _reverify_attempt_event(row, prior_event_id=prior)
        attempt = event["attempt_number"]
        state = event["state"]
        if not events:
            valid_transition = (
                attempt == 1
                and state == "ready"
                and event["lease_ref"] is None
                and event["result_envelope_ref"] is None
            )
        elif prior_state == "ready":
            valid_transition = attempt == prior_attempt and state == "leased"
        elif prior_state == "leased":
            valid_transition = attempt == prior_attempt and state in {
                "succeeded", "retryable", "failed", "expired"
            }
        elif prior_state in {"retryable", "expired"}:
            valid_transition = (
                (attempt == prior_attempt + 1 and state == "ready")
                or (attempt == prior_attempt and state == "failed")
            )
        else:
            valid_transition = False
        if not valid_transition:
            raise ResearchPlanCoordinatorConflict(
                "scheduler attempt history contains an invalid state transition"
            )
        if event["lease_ref"] is not None:
            _reverify_lease(
                cursor,
                event["lease_ref"],
                work_order_id=work_order_id,
                attempt_number=attempt,
            )
        if state in {"ready", "leased"} and (
            event["result_envelope_ref"] is not None
            or event["result_envelope_hash"] is not None
        ):
            raise ResearchPlanCoordinatorConflict(
                "non-completion scheduler event fabricates a ResultEnvelope binding"
            )
        if state in {"succeeded", "retryable"} and (
            event["result_envelope_ref"] is None
            or event["result_envelope_hash"] is None
        ):
            raise ResearchPlanCoordinatorConflict(
                "worker completion event lacks its ResultEnvelope binding"
            )
        if state == "failed" and (
            (event["result_envelope_ref"] is None)
            != (event["result_envelope_hash"] is None)
        ):
            raise ResearchPlanCoordinatorConflict(
                "failed scheduler event carries a partial ResultEnvelope binding"
            )
        events.append(event)
        prior = row["event_id"]
        prior_state = state
        prior_attempt = attempt
    return events


def _reverify_result_envelope(row: Any) -> dict[str, Any]:
    """Restore one immutable ResultEnvelope row and re-check its bindings."""

    try:
        raw = json.loads(row["result_envelope_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchPlanCoordinatorConflict(
            "scheduler ResultEnvelope record_json is not valid JSON"
        ) from exc
    try:
        wire = ResultEnvelope.from_dict(raw).to_dict()
    except Exception as exc:
        raise ResearchPlanCoordinatorConflict(
            "scheduler ResultEnvelope is not a closed contract wire"
        ) from exc
    envelope_hash = content_hash(wire)
    if canonical_json(wire) != row["result_envelope_json"]:
        raise ResearchPlanCoordinatorConflict(
            "scheduler ResultEnvelope record_json is not canonical"
        )
    if envelope_hash != row["result_envelope_hash"]:
        raise ResearchPlanCoordinatorConflict(
            "scheduler ResultEnvelope hash mismatch"
        )
    receipt = {
        "result_envelope_id": row["result_envelope_id"],
        "work_order_id": row["work_order_id"],
        "attempt_number": row["attempt_number"],
        "result_envelope_hash": row["result_envelope_hash"],
        "outcome": row["outcome"],
        "created_at": row["created_at"],
    }
    if (
        content_hash(receipt) != row["content_hash"]
        or row["result_envelope_id"] != wire["id"]
        or row["work_order_id"] != wire["work_order_ref"]
        or row["outcome"] != wire["status"]
    ):
        raise ResearchPlanCoordinatorConflict(
            "scheduler ResultEnvelope receipt binding drifted"
        )
    return wire


def _reverify_formal_result(
    cursor: sqlite3.Cursor, work_order_id: str, *, attempt_number: int,
    result_envelope_id: str, result_envelope_hash: str,
) -> dict[str, Any]:
    row = cursor.execute(
        "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
        (work_order_id,),
    ).fetchone()
    if row is None:
        raise ResearchPlanCoordinatorConflict(
            "succeeded attempt lacks its exact formal result"
        )
    wire = {
        "id": row["result_record_id"],
        "work_order_id": row["work_order_id"],
        "attempt_number": row["attempt_number"],
        "result_envelope_id": row["result_envelope_id"],
        "result_envelope_hash": row["result_envelope_hash"],
        "terminal_state": row["terminal_state"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchPlanCoordinatorConflict(
            "scheduler formal result content_hash mismatch"
        )
    if (
        wire["work_order_id"] != work_order_id
        or wire["attempt_number"] != attempt_number
        or wire["result_envelope_id"] != result_envelope_id
        or wire["result_envelope_hash"] != result_envelope_hash
    ):
        raise ResearchPlanCoordinatorConflict(
            "scheduler formal result does not bind its succeeded attempt"
        )
    try:
        formal_envelope = json.loads(row["result_envelope_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchPlanCoordinatorConflict(
            "scheduler formal result embeds invalid ResultEnvelope JSON"
        ) from exc
    if canonical_json(formal_envelope) != row["result_envelope_json"]:
        raise ResearchPlanCoordinatorConflict(
            "scheduler formal result embeds non-canonical ResultEnvelope JSON"
        )
    return {**wire, "result_envelope": formal_envelope}


def _upstream_outcome(
    cursor: sqlite3.Cursor, work_order_id: str, expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Re-read one upstream node and return its deterministic outcome.

    ``pending`` (still ready/leased/retryable), ``blocked`` (terminal failure
    or exhaustion) or ``succeeded`` (exact attempt + formal result + envelope
    chain).  Any hash/binding drift, a missing row, a failed-terminal formal
    result under a succeeded attempt, or a result envelope bound to another
    WorkOrder raises instead of admitting.
    """

    _reverify_scheduler_work_order(cursor, work_order_id, expected)
    events = _attempt_chain(cursor, work_order_id)
    latest = events[-1]
    state = latest["state"]
    plan_attempt_limit = expected.get("budget", {}).get("step_max_attempts")
    if (
        isinstance(plan_attempt_limit, bool)
        or not isinstance(plan_attempt_limit, int)
        or plan_attempt_limit < 1
    ):
        raise ResearchPlanCoordinatorConflict(
            "upstream WorkOrder lacks its immutable plan attempt bound"
        )
    if latest["attempt_number"] > plan_attempt_limit:
        return {
            "state": "blocked",
            "attempt_state": "plan_attempts_exhausted",
        }
    if state in _NON_TERMINAL_STATES:
        return {"state": "pending", "attempt_state": state}
    if state in {"failed", "expired"}:
        return {"state": "blocked", "attempt_state": state}
    if state != "succeeded":
        raise ResearchPlanCoordinatorConflict(
            f"unexpected scheduler attempt state {state!r}"
        )
    formal = _reverify_formal_result(
        cursor,
        work_order_id,
        attempt_number=latest["attempt_number"],
        result_envelope_id=latest["result_envelope_ref"],
        result_envelope_hash=latest["result_envelope_hash"],
    )
    if formal["terminal_state"] != "succeeded":
        raise ResearchPlanCoordinatorConflict(
            "succeeded attempt carries a failed terminal formal result"
        )
    envelope_row = cursor.execute(
        "SELECT * FROM "
        "scheduler_result_envelopes WHERE result_envelope_id=?",
        (formal["result_envelope_id"],),
    ).fetchone()
    if envelope_row is None:
        raise ResearchPlanCoordinatorConflict(
            "formal result lacks its exact ResultEnvelope row"
        )
    envelope = _reverify_result_envelope(envelope_row)
    if canonical_json(envelope) != canonical_json(formal["result_envelope"]):
        raise ResearchPlanCoordinatorConflict(
            "scheduler formal result embeds a different ResultEnvelope"
        )
    if (
        envelope_row["attempt_number"] != latest["attempt_number"]
        or envelope_row["result_envelope_hash"] != latest["result_envelope_hash"]
    ):
        raise ResearchPlanCoordinatorConflict(
            "scheduler ResultEnvelope row does not bind its succeeded attempt"
        )
    if envelope["work_order_ref"] != work_order_id:
        raise ResearchPlanCoordinatorConflict(
            "ResultEnvelope binds a different WorkOrder than the upstream"
        )
    if envelope["status"] != "succeeded":
        raise ResearchPlanCoordinatorConflict(
            "upstream ResultEnvelope is not a succeeded result"
        )
    if envelope.get("error") is not None:
        raise ResearchPlanCoordinatorConflict(
            "succeeded upstream ResultEnvelope cannot carry an error"
        )
    return {
        "state": "succeeded",
        "attempt_state": "succeeded",
        "attempt": latest,
        "formal": formal,
        "envelope": envelope,
    }


def _canonical_connector_request(row: Any) -> dict[str, Any]:
    try:
        raw = json.loads(row["request_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchPlanCoordinatorConflict(
            "connector runner request record_json is not valid JSON"
        ) from exc
    try:
        wire = validate_connector_runner_request(raw)
    except Exception as exc:
        raise ResearchPlanCoordinatorConflict(
            "connector runner request is not a closed contract wire"
        ) from exc
    if canonical_json(wire) != row["request_json"] or wire["content_hash"] != row["content_hash"]:
        raise ResearchPlanCoordinatorConflict(
            "connector runner request record drifted from its stored hash"
        )
    return wire


def _reverify_runner_journal_completion(
    cursor: sqlite3.Cursor,
    request: Mapping[str, Any],
    *,
    result_envelope_ref: str,
    result_envelope_hash: str,
) -> dict[str, Any]:
    rows = cursor.execute(
        "SELECT * FROM runner_attempt_journal_events WHERE runner_request_ref=? "
        "ORDER BY event_seq",
        (request["id"],),
    ).fetchall()
    if not rows:
        raise ResearchPlanCoordinatorConflict(
            "actual connector request has no runner journal history"
        )
    transitions = {
        None: {"admitted"},
        "admitted": {"reserved"},
        "reserved": {"transport_started", "observed", "released_recovered"},
        "transport_started": {"observed", "indeterminate_recovered"},
        "observed": {"responded"},
        "indeterminate_recovered": {"responded"},
        "responded": set(),
        "released_recovered": set(),
    }
    prior_state: str | None = None
    latest: dict[str, Any] | None = None
    for ordinal, row in enumerate(rows, start=1):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ResearchPlanCoordinatorConflict(
                "runner journal event payload is not valid JSON"
            ) from exc
        event = {
            "runner_request_ref": row["runner_request_ref"],
            "request_ordinal": ordinal,
            "state": row["state"],
            "reservation_ref": row["reservation_ref"],
            "event_at": row["event_at"],
            "recorded_at": row["created_at"],
            "payload": payload,
        }
        if (
            row["runner_request_ref"] != request["id"]
            or row["state"] not in transitions.get(prior_state, set())
            or canonical_json(payload) != row["payload_json"]
            or content_hash(event) != row["content_hash"]
        ):
            raise ResearchPlanCoordinatorConflict(
                "runner journal completion history drifted"
            )
        prior_state = row["state"]
        latest = event
    assert latest is not None
    payload = latest["payload"]
    response_raw = payload.get("response") if isinstance(payload, Mapping) else None
    if latest["state"] != "responded" or not isinstance(response_raw, Mapping):
        raise ResearchPlanCoordinatorConflict(
            "actual connector request has no terminal responded event"
        )
    try:
        response = validate_connector_runner_response(response_raw)
    except Exception as exc:
        raise ResearchPlanCoordinatorConflict(
            "runner journal responded event has an invalid connector response"
        ) from exc
    if (
        response["runner_request_ref"] != request["id"]
        or response["runner_request_hash"] != request["content_hash"]
        or response["result_envelope_ref"] != result_envelope_ref
        or response["result_envelope_hash"] != result_envelope_hash
        or response["outcome"] != "succeeded"
    ):
        raise ResearchPlanCoordinatorConflict(
            "runner journal response does not bind the Scheduler result"
        )
    return response


def _reverify_stage_output(
    plan_wire: Mapping[str, Any],
    step: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    upstream_work_order: Mapping[str, Any],
    upstream_outcome: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed, hashed output proof for a non-connector stage."""

    output = outcome["envelope"].get("outputs")
    fields = {
        "schema_version", "id", "created_at", "plan_version_ref",
        "plan_version_hash", "step_ref", "step_hash", "stage", "operation",
        "output_contract_ref",
        "upstream_work_order_ref", "upstream_result_ref",
        "upstream_result_hash", "records", "content_hash",
    }
    if not isinstance(output, Mapping) or set(output) != fields:
        raise ResearchPlanCoordinatorConflict(
            "internal stage ResultEnvelope lacks its closed output proof"
        )
    wire = json.loads(canonical_json(output))
    declared_hash = wire.pop("content_hash")
    if (
        wire["schema_version"] != "0.1"
        or wire["id"] != _stage_output_ref(
            plan_version_ref=plan_wire["id"],
            step_ref=step["id"],
            upstream_result_ref=upstream_outcome["formal"]["result_envelope_id"],
            upstream_result_hash=upstream_outcome["formal"]["result_envelope_hash"],
        )
        or wire["created_at"] != outcome["envelope"]["created_at"]
        or declared_hash != content_hash(wire)
        or wire["plan_version_ref"] != plan_wire["id"]
        or wire["plan_version_hash"] != plan_wire["content_hash"]
        or wire["step_ref"] != step["id"]
        or wire["step_hash"] != step["content_hash"]
        or wire["stage"] != step["stage"]
        or wire["operation"] != step["operation"]
        or wire["output_contract_ref"] != step["output_contract_ref"]
        or wire["upstream_work_order_ref"] != upstream_work_order["id"]
        or wire["upstream_result_ref"]
        != upstream_outcome["formal"]["result_envelope_id"]
        or wire["upstream_result_hash"]
        != upstream_outcome["formal"]["result_envelope_hash"]
    ):
        raise ResearchPlanCoordinatorConflict(
            "internal stage output proof drifted from plan/upstream authority"
        )
    expected_kinds = _STAGE_RECORD_KINDS.get(step["stage"])
    records = wire["records"]
    if (
        expected_kinds is None
        or not isinstance(records, list)
        or tuple(item.get("kind") for item in records if isinstance(item, Mapping))
        != expected_kinds
        or len(records) != len(expected_kinds)
    ):
        raise ResearchPlanCoordinatorConflict(
            "internal stage output record kinds do not match the frozen stage"
        )
    seen_refs: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"kind", "ref", "hash"}:
            raise ResearchPlanCoordinatorConflict(
                "internal stage output record is not a closed ref/hash binding"
            )
        kind = record["kind"]
        ref = record["ref"]
        digest = record["hash"]
        if (
            not isinstance(ref, str)
            or not ref.startswith(_STAGE_RECORD_PREFIXES[kind])
            or ref in seen_refs
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ResearchPlanCoordinatorConflict(
                "internal stage output record ref/hash is invalid"
            )
        seen_refs.add(ref)
    if outcome["envelope"]["actual_side_effects"] != list(
        step["declared_side_effects"]
    ) or outcome["envelope"]["artifact_refs"]:
        raise ResearchPlanCoordinatorConflict(
            "internal stage ResultEnvelope reports undeclared effects/artifacts"
        )
    return {**wire, "content_hash": declared_hash}


def _connector_receipt_chain(
    core_cursor: sqlite3.Cursor,
    connector_cursor: sqlite3.Cursor,
    envelope: Mapping[str, Any],
    *,
    result_envelope_hash: str,
    work_order_id: str,
    work_order_hash: str,
    attempt_number: int,
) -> dict[str, Any]:
    """Verify the connector completion receipt/artifact chain for one edge.

    A succeeded connector WorkOrder is only admissible when its ResultEnvelope
    binds the actual ConnectorRunnerRequest in ``metadata``; exactly one
    ConnectorCompletionReceipt binds the Scheduler ResultEnvelope; the
    receipt's compiled request binds the exact root WorkOrder/attempt; and its
    source-envelope/artifact refs are exactly the ResultEnvelope outputs.  A
    v0.2 receipt additionally binds the actual request in the Core runner
    journal, rather than trusting the receipt's declared actual-request hash.
    Without this chain a caller could admit downstream work on a bare success
    flag and no connector output.
    """

    metadata = envelope.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ResearchPlanCoordinatorConflict(
            "connector ResultEnvelope lacks a closed metadata mapping"
        )
    runner_request_ref = metadata.get("runner_request_ref")
    if not isinstance(runner_request_ref, str) or not runner_request_ref:
        raise ResearchPlanCoordinatorConflict(
            "connector ResultEnvelope metadata lacks runner_request_ref"
        )
    receipt_rows = connector_cursor.execute(
        "SELECT receipt_json,content_hash FROM research_completion_receipts"
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for row in receipt_rows:
        try:
            raw = json.loads(row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise ResearchPlanCoordinatorConflict(
                "connector completion receipt record_json is not valid JSON"
            ) from exc
        try:
            receipt = validate_connector_completion_receipt(raw)
        except Exception as exc:
            raise ResearchPlanCoordinatorConflict(
                "connector completion receipt is not a closed contract wire"
            ) from exc
        if (
            receipt["result_ref"] == envelope["id"]
            and receipt["result_hash"] == result_envelope_hash
        ):
            if (
                canonical_json(receipt) != row["receipt_json"]
                or receipt["content_hash"] != row["content_hash"]
            ):
                raise ResearchPlanCoordinatorConflict(
                    "connector completion receipt record drifted from its stored hash"
                )
            matches.append(receipt)
    if len(matches) != 1:
        raise ResearchPlanCoordinatorConflict(
            "connector completion receipt chain is not exact for the Scheduler result"
        )
    receipt = matches[0]
    if receipt["status"] != "succeeded":
        raise ResearchPlanCoordinatorConflict(
            "connector completion receipt is not a succeeded completion"
        )
    if not receipt["source_envelopes"] or not receipt["artifacts"]:
        raise ResearchPlanCoordinatorConflict(
            "connector completion receipt carries no source-envelope/artifact outputs"
        )
    request_row = connector_cursor.execute(
        "SELECT request_json,content_hash FROM research_runner_requests "
        "WHERE runner_request_id=?",
        (receipt["runner_request_ref"],),
    ).fetchone()
    if request_row is None:
        raise ResearchPlanCoordinatorConflict(
            "connector receipt lacks its exact compiled runner request"
        )
    request = _canonical_connector_request(request_row)
    if (
        request["id"] != receipt["runner_request_ref"]
        or request["content_hash"] != receipt["runner_request_hash"]
        or request["work_order_ref"] != work_order_id
        or request["work_order_hash"] != work_order_hash
        or request["scheduler_attempt_number"] != attempt_number
    ):
        raise ResearchPlanCoordinatorConflict(
            "connector receipt request does not bind the exact WorkOrder attempt"
        )

    actual_request = request
    if receipt["schema_version"] == "0.2":
        actual_ref = receipt["actual_runner_request_ref"]
        actual_row = core_cursor.execute(
            "SELECT request_json,request_hash FROM runner_request_journal "
            "WHERE runner_request_ref=?",
            (actual_ref,),
        ).fetchone()
        if actual_row is None:
            raise ResearchPlanCoordinatorConflict(
                "connector receipt lacks its actual request in the runner journal"
            )
        actual_request = _canonical_connector_request({
            "request_json": actual_row["request_json"],
            "content_hash": actual_row["request_hash"],
        })
        if (
            actual_request["id"] != actual_ref
            or actual_request["content_hash"]
            != receipt["actual_runner_request_hash"]
            or actual_request["work_order_ref"] != work_order_id
            or actual_request["work_order_hash"] != work_order_hash
            or actual_request["scheduler_attempt_number"] != attempt_number
        ):
            raise ResearchPlanCoordinatorConflict(
                "actual connector request drifted from the receipt/WorkOrder attempt"
            )
        _reverify_runner_journal_completion(
            core_cursor,
            actual_request,
            result_envelope_ref=envelope["id"],
            result_envelope_hash=result_envelope_hash,
        )

    if runner_request_ref != actual_request["id"]:
        raise ResearchPlanCoordinatorConflict(
            "connector ResultEnvelope does not bind its actual runner request"
        )
    metadata_hash = metadata.get("runner_request_hash")
    if metadata_hash is not None and metadata_hash != actual_request["content_hash"]:
        raise ResearchPlanCoordinatorConflict(
            "connector ResultEnvelope runner request hash drifted"
        )
    source_refs = [item["ref"] for item in receipt["source_envelopes"]]
    artifact_refs = [item["ref"] for item in receipt["artifacts"]]
    if (
        len(source_refs) != 1
        or envelope["outputs"].get("source_envelope_ref") != source_refs[0]
        or envelope["artifact_refs"] != artifact_refs
    ):
        raise ResearchPlanCoordinatorConflict(
            "connector ResultEnvelope outputs do not bind receipt source/artifacts"
        )
    return {
        "runner_request": request,
        "actual_runner_request": actual_request,
        "receipt": receipt,
    }


class ResearchPlanCoordinator:
    """Admits each downstream plan-tree WorkOrder only after exact upstream proof.

    Every call re-reads the immutable plan, the exact start binding
    (approval + workflow + links + Scheduler rows) and the exact upstream
    attempt/result/receipt chain; the only write is the single idempotent
    Scheduler enqueue of the plan-derived child WorkOrder.  A replayed
    admission converges to the same child (``duplicate``); a conflicting
    child row, tampered plan/workflow/link/result or failed/cancelled/exhausted
    upstream attempt fails closed.
    """

    def __init__(
        self,
        *,
        plan: ResearchPlanAuthority,
        scheduler: Scheduler,
        connector_records: ResearchCoordinatorStore,
        fault_injector: Callable[[str], None] | None = None,
    ):
        if not isinstance(plan, ResearchPlanAuthority):
            raise TypeError("plan must be an exact ResearchPlanAuthority")
        if not isinstance(scheduler, Scheduler):
            raise TypeError("scheduler must be an exact Scheduler")
        if not isinstance(connector_records, ResearchCoordinatorStore):
            raise TypeError(
                "connector_records must be an exact ResearchCoordinatorStore"
            )
        if scheduler.connection is not plan.connection:
            raise TypeError(
                "plan and scheduler must share one Core connection"
            )
        self.plan = plan
        self.scheduler = scheduler
        self.connector_records = connector_records
        self.fault_injector = fault_injector

    def _inject(self, seam: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(seam)

    @staticmethod
    def _tree(plan_wire: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        work_orders = _plan_work_orders(plan_wire)
        return work_orders, _plan_link_specs(plan_wire, work_orders)

    def _admission_context(
        self, plan_version_ref: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Exact plan + start binding + derived tree; any drift fails closed."""

        cursor = self.plan.connection.cursor()
        plan_wire = read_exact_research_plan_version(cursor, plan_version_ref)
        # Re-validates approval, workflow version, WorkOrderLink rows and
        # every admitted Scheduler WorkOrder against plan authority.
        read_exact_research_plan_start(
            cursor, plan_start_ref_for(plan_version_ref)
        )
        work_orders, link_specs = self._tree(plan_wire)
        return plan_wire, work_orders, link_specs

    def _verify_prefix(
        self,
        cursor: sqlite3.Cursor,
        connector_cursor: sqlite3.Cursor,
        plan_wire: Mapping[str, Any],
        work_orders: Sequence[Mapping[str, Any]],
        *,
        upto: int,
    ) -> list[dict[str, Any]]:
        """Every node at or below ``upto`` must be admitted and exact.

        Nodes below the immediate upstream must already be terminal
        ``succeeded``: the coordinator never admits in a way that could skip
        a dependency, so a non-succeeded prefix node while a later node is
        presented as upstream is broken authority and fails closed.
        """

        outcomes: list[dict[str, Any]] = []
        for index in range(upto + 1):
            outcome = _upstream_outcome(
                cursor, work_orders[index]["id"], work_orders[index]
            )
            if index == 0 and outcome["state"] == "succeeded":
                outcome["connector_proof"] = _connector_receipt_chain(
                    cursor,
                    connector_cursor,
                    outcome["envelope"],
                    result_envelope_hash=outcome["formal"]["result_envelope_hash"],
                    work_order_id=work_orders[index]["id"],
                    work_order_hash=content_hash(work_orders[index]),
                    attempt_number=outcome["formal"]["attempt_number"],
                )
            elif index > 0 and outcome["state"] == "succeeded":
                outcome["stage_output"] = _reverify_stage_output(
                    plan_wire,
                    plan_wire["execution_scope"]["steps"][index],
                    outcome,
                    upstream_work_order=work_orders[index - 1],
                    upstream_outcome=outcomes[index - 1],
                )
            if index < upto and outcome["state"] != "succeeded":
                raise ResearchPlanCoordinatorConflict(
                    "plan-tree admission prefix is not exactly succeeded; "
                    "authority is inconsistent"
                )
            outcomes.append(outcome)
        return outcomes

    def admit_next_work_order(
        self,
        *,
        plan_version_ref: str,
        upstream_work_order_ref: str,
    ) -> dict[str, Any]:
        """Admit the immediate child of an exact succeeded upstream node.

        Only the child of the given upstream is ever enqueued; the coordinator
        never skips a node and never enqueues more than one WorkOrder per
        call.  Replays converge: if the child is already admitted byte-
        identical to plan authority the call returns ``duplicate`` without
        touching the Scheduler.
        """

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        upstream_work_order_ref = _text(
            upstream_work_order_ref, "upstream_work_order_ref"
        )
        plan_wire, work_orders, _ = self._admission_context(plan_version_ref)
        index_by_ref = {
            work_order["id"]: index
            for index, work_order in enumerate(work_orders)
        }
        if upstream_work_order_ref not in index_by_ref:
            raise ResearchPlanCoordinatorConflict(
                "upstream work order is not a node of this research plan"
            )
        index = index_by_ref[upstream_work_order_ref]
        steps = plan_wire["execution_scope"]["steps"]
        upstream = work_orders[index]
        upstream_step = steps[index]
        cursor = self.plan.connection.cursor()
        connector_cursor = self.connector_records.connection.cursor()
        outcomes = self._verify_prefix(
            cursor, connector_cursor, plan_wire, work_orders, upto=index
        )
        upstream_outcome = outcomes[index]
        if upstream_outcome["state"] == "pending":
            return {
                "status": "pending_upstream",
                "plan_version_ref": plan_wire["id"],
                "upstream_work_order_ref": upstream["id"],
                "upstream_stage": upstream_step["stage"],
                "upstream_state": upstream_outcome["attempt_state"],
            }
        if upstream_outcome["state"] == "blocked":
            return {
                "status": "blocked",
                "plan_version_ref": plan_wire["id"],
                "upstream_work_order_ref": upstream["id"],
                "upstream_stage": upstream_step["stage"],
                "upstream_state": upstream_outcome["attempt_state"],
                "reason": (
                    "upstream attempt terminated unsuccessfully; downstream "
                    "admission is blocked until a fresh exact upstream result"
                ),
            }
        if index == len(work_orders) - 1:
            return {
                "status": "complete",
                "plan_version_ref": plan_wire["id"],
                "upstream_work_order_ref": upstream["id"],
                "upstream_stage": upstream_step["stage"],
            }
        child = work_orders[index + 1]
        child_step = steps[index + 1]
        # A child already present must be byte-identical to plan authority
        # before the call converges on ``duplicate``.
        child_row = cursor.execute(
            "SELECT work_order_json FROM scheduler_work_orders "
            "WHERE work_order_id=?",
            (child["id"],),
        ).fetchone()
        if child_row is not None:
            _reverify_scheduler_work_order(cursor, child["id"], child)
            _attempt_chain(cursor, child["id"])
            return {
                "status": "duplicate",
                "plan_version_ref": plan_wire["id"],
                "upstream_work_order_ref": upstream["id"],
                "upstream_stage": upstream_step["stage"],
                "admitted_work_order_ref": child["id"],
                "admitted_work_order_hash": content_hash(child),
                "admitted_step_ordinal": child_step["ordinal"],
                "admitted_stage": child_step["stage"],
                "admission_state": "queued",
            }
        self._inject("before_enqueue")
        enqueued = self.scheduler.enqueue(child)
        if enqueued["status"] == "conflict":
            raise ResearchPlanCoordinatorConflict(
                "child WorkOrder enqueue conflict; plan authority drifted"
            )
        self._inject("after_enqueue")
        _reverify_scheduler_work_order(cursor, child["id"], child)
        _attempt_chain(cursor, child["id"])
        return {
            "status": enqueued["status"],
            "plan_version_ref": plan_wire["id"],
            "upstream_work_order_ref": upstream["id"],
            "upstream_stage": upstream_step["stage"],
            "admitted_work_order_ref": child["id"],
            "admitted_work_order_hash": enqueued["work_order_hash"],
            "admitted_step_ordinal": child_step["ordinal"],
            "admitted_stage": child_step["stage"],
            "admission_state": "queued",
        }

    def tree_status(self, plan_version_ref: str) -> dict[str, Any]:
        """Read-only admission projection for one started plan tree.

        Derives every node from plan authority and reports per node the
        plan-derived ref/hash, admission state (``planned`` until the
        Scheduler holds the exact row) and the Scheduler attempt state.
        This is a projection; the Scheduler and plan rows remain the only
        authority.
        """

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        plan_wire, work_orders, link_specs = self._admission_context(plan_version_ref)
        steps = plan_wire["execution_scope"]["steps"]
        cursor = self.plan.connection.cursor()
        admitted: list[bool] = []
        for work_order in work_orders:
            admitted.append(
                cursor.execute(
                    "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
                    (work_order["id"],),
                ).fetchone()
                is not None
            )
        if admitted and not admitted[0]:
            raise ResearchPlanCoordinatorConflict(
                "started plan root WorkOrder is not admitted"
            )
        first_gap = next(
            (index for index, present in enumerate(admitted) if not present),
            len(admitted),
        )
        if any(admitted[first_gap:]):
            raise ResearchPlanCoordinatorConflict(
                "plan tree contains a non-contiguous downstream admission"
            )
        outcomes = self._verify_prefix(
            cursor,
            self.connector_records.connection.cursor(),
            plan_wire,
            work_orders,
            upto=first_gap - 1,
        )
        nodes: list[dict[str, Any]] = []
        for index, (step, work_order) in enumerate(
            zip(steps, work_orders, strict=True)
        ):
            if not admitted[index]:
                nodes.append({
                    "step_ref": step["id"],
                    "work_order_ref": work_order["id"],
                    "work_order_hash": content_hash(work_order),
                    "stage": step["stage"],
                    "admission_state": "planned",
                    "attempt_state": None,
                })
                continue
            outcome = outcomes[index]
            nodes.append({
                "step_ref": step["id"],
                "work_order_ref": work_order["id"],
                "work_order_hash": content_hash(work_order),
                "stage": step["stage"],
                "admission_state": "queued",
                "attempt_state": outcome["attempt_state"],
            })
        return {
            "plan_version_ref": plan_wire["id"],
            "plan_version_hash": plan_wire["content_hash"],
            "state": "started",
            "nodes": nodes,
            "work_order_links": [
                {
                    "link_id": link["link_id"],
                    "parent_work_order_ref": link["parent_work_order_ref"],
                    "child_work_order_ref": link["child_work_order_ref"],
                    "relation": link["relation"],
                    "sequence": link["sequence"],
                }
                for link in link_specs
            ],
        }


__all__ = [
    "ResearchPlanCoordinator",
    "ResearchPlanCoordinatorConflict",
    "ResearchPlanCoordinatorError",
]
