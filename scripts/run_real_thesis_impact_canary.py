#!/usr/bin/env python3
"""Run Gate 2 against the live OpenClaw model broker in isolated authority.

The parent copies one closed Gate 1 sample, admits one owner-authorized
ThesisVersion in that copy, and enqueues the existing thesis-impact assessment.
A child process executes the assessment and exits immediately after durable
model accounting but before Scheduler completion.  The parent waits for the
lease to expire, recovers the exact broker completion through replayOnly, runs
an independent verifier model, and proves that a final replay does not contact
the broker or add accounting rows.

This command never modifies the source Gate 1 bundle, the live Dalton store, a
Thesis current pointer after admission, OpenClaw configuration, or cron state.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.model_deployment import openclaw_broker_profiles  # noqa: E402
from dalton_core.model_router import ModelRouter  # noqa: E402
from dalton_core.observability import ObservabilityStore  # noqa: E402
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter  # noqa: E402
from dalton_core.research_coordinator import ResearchCoordinatorStore  # noqa: E402
from dalton_core.research_plan import ResearchPlanAuthority  # noqa: E402
from dalton_core.research_plan_closure import ResearchPlanClosureCoordinator  # noqa: E402
from dalton_core.research_plan_coordinator import ResearchPlanCoordinator  # noqa: E402
from dalton_core.research_question_backlog import ResearchQuestionBacklog  # noqa: E402
from dalton_core.research_review import HumanReviewAuthority  # noqa: E402
from dalton_core.scheduler import Scheduler  # noqa: E402
from dalton_core.store import DaltonStore, canonical_json, content_hash  # noqa: E402
from dalton_core.thesis_impact import ThesisImpactAuthority  # noqa: E402
from dalton_core.thesis_impact_control import (  # noqa: E402
    ASSESSMENT_BUDGET,
    VERIFIER_BUDGET,
    ResearchPlanThesisImpactCoordinator,
)
from dalton_core.thesis_impact_model_worker import (  # noqa: E402
    ResearchPlanThesisImpactRuntime,
    ThesisImpactModelWorker,
)


SCHEMA_VERSION = "0.1"
CRASH_EXIT_CODE = 86
THESIS_REF = "thesis:msft:revenue-operating-leverage"
ROUTING_POLICY_REF = "model-routing-policy-version:gate2-real-canary:1"
BROKER_CLIENT_ID = "client:dalton-core"
BROKER_AGENT_ID = "chem"
ASSESSMENT_PROFILE_ID = "profile:gpt-5-6-sol"
VERIFIER_PROFILE_ID = "profile:claude-opus-5"
GATE2_ACTOR = "system:gate2-real-thesis-impact-canary"
BROKER_INVOCATION_ACTOR = "runtime:openclaw-model-broker"


class Gate2CanaryError(RuntimeError):
    """The real-model canary failed a closed acceptance condition."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    )


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Gate2CanaryError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Gate2CanaryError(f"JSON root must be an object: {path}")
    return value


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _git_state() -> tuple[str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return head, dirty


def _required_paths(root: Path) -> dict[str, Path]:
    paths = {
        "core": root / "core.sqlite",
        "review": root / "candidate-staging.sqlite",
        "coordinator": root / "research-coordinator.sqlite",
        "source_result": root / "result.json",
        "router": root / "model-router.sqlite",
    }
    missing = [
        str(path)
        for name, path in paths.items()
        if name != "router" and not path.is_file()
    ]
    if missing:
        raise Gate2CanaryError(
            "isolated authority is missing required files: " + ", ".join(missing)
        )
    return paths


class Authorities:
    """One shared Core connection plus the two external SQLite authorities."""

    def __init__(
        self,
        root: Path,
        *,
        scheduler_clock: Callable[[], datetime] = _now,
    ) -> None:
        paths = _required_paths(root)
        self.core = DaltonStore(paths["core"])
        self.review = HumanReviewAuthority(paths["review"])
        self.records = ResearchCoordinatorStore(paths["coordinator"])
        self.backlog = ResearchQuestionBacklog(self.core)
        self.plan = ResearchPlanAuthority(self.core)
        self.scheduler = Scheduler(
            connection=self.core.connection,
            clock=scheduler_clock,
        )
        self.coordinator = ResearchPlanCoordinator(
            plan=self.plan,
            scheduler=self.scheduler,
            connector_records=self.records,
        )
        self.closure = ResearchPlanClosureCoordinator(
            plan=self.plan,
            backlog=self.backlog,
            coordinator=self.coordinator,
            review=self.review,
        )
        self.impact = ThesisImpactAuthority(self.core, self.scheduler)
        self.control = ResearchPlanThesisImpactCoordinator(
            closure=self.closure,
            impact=self.impact,
        )
        self.observability = ObservabilityStore(self.core)

    def close(self) -> None:
        self.records.close()
        self.review.close()
        self.core.close()

    def __enter__(self) -> "Authorities":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _recorded_invocation(
    *,
    identifier: str,
    work_order_ref: str,
    capability: str,
    provider: str,
    model: str,
    family: str,
    actor_ref: str,
) -> dict[str, Any]:
    created_at = _wire_time()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": identifier,
        "created_at": created_at,
        "work_order_ref": work_order_ref,
        "profile_ref": "runtime-profile:owner-authorized-recorded:0.1",
        "granularity": "task" if capability == "research" else "verification",
        "capability": capability,
        "provider": provider,
        "model": model,
        "model_family": family,
        "input_refs": [],
        "output_refs": [],
        "started_at": created_at,
        "completed_at": created_at,
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0},
        "side_effects": [],
        "runtime_ref": "runtime:owner-authorized-recorded:0.1",
        "actor_ref": actor_ref,
        "parent_ref": None,
        "environment_hash": hashlib.sha256(
            b"gate2-owner-authorized-isolated-thesis-admission"
        ).hexdigest(),
    }


def _admit_thesis(authorities: Authorities, *, policy_owner: str) -> dict[str, Any]:
    if authorities.core.current_pointer(THESIS_REF) is not None:
        raise Gate2CanaryError("isolated source unexpectedly already has the Gate 2 thesis")
    producer = _recorded_invocation(
        identifier="invocation:gate2-owner-thesis-admission",
        work_order_ref="work:gate2-owner-thesis-admission",
        capability="research",
        provider="owner-authorization",
        model="lumos-obliviate-explicit-approval",
        family="human-owner",
        actor_ref=policy_owner,
    )
    verifier = _recorded_invocation(
        identifier="invocation:gate2-thesis-admission-verifier",
        work_order_ref="work:gate2-thesis-admission-verifier",
        capability="verify",
        provider="recorded-control",
        model="thesis-admission-contract-verifier",
        family="deterministic-control",
        actor_ref=GATE2_ACTOR,
    )
    change_ref = "change:gate2-msft-thesis-admission"
    verification_ref = "verification:gate2-msft-thesis-admission"
    authorities.core.stage_change(
        change_ref,
        thesis_id=THESIS_REF,
        content={
            "statement": (
                "Microsoft quarterly revenue growth should support earnings growth."
            ),
            "mechanism": "Quarterly revenue growth sustains operating leverage.",
            "confidence": 0.6,
            "implied_expectation": (
                "Microsoft reports positive year-over-year quarterly revenue growth."
            ),
            "claim_refs": [],
            "catalyst_refs": [],
            "falsifier_refs": [],
            "change_reason": "owner-authorized isolated Gate 2 thesis admission",
        },
        producer_invocation=producer,
        actor_id=policy_owner,
    )
    authorities.core.verify_change(
        change_ref,
        verification_id=verification_ref,
        verifier_invocation=verifier,
        verdict="pass",
        findings=[{
            "code": "THESIS_ADMISSION_CONTRACT_VALID",
            "message": "closed thesis contract and independent admission verifier",
        }],
        actor_id=GATE2_ACTOR,
    )
    committed = authorities.core.commit(
        change_ref,
        verification_ref,
        "commit:gate2-msft-thesis-admission",
        actor_id=policy_owner,
    )
    pointer = authorities.core.current_pointer(THESIS_REF)
    if pointer is None or pointer["version_id"] != committed["version_id"]:
        raise Gate2CanaryError("owner-authorized thesis admission did not commit")
    return {"commit": committed, "pointer": pointer}


def _install_router(router_path: Path) -> dict[str, Any]:
    if router_path.exists():
        raise Gate2CanaryError(f"router authority already exists: {router_path}")
    checked_at = _now()
    wanted = {ASSESSMENT_PROFILE_ID, VERIFIER_PROFILE_ID}
    profiles = [
        profile
        for profile in openclaw_broker_profiles(
            checked_at=checked_at,
            availability_ttl=timedelta(days=1),
        )
        if profile["id"] in wanted
    ]
    if {profile["id"] for profile in profiles} != wanted:
        raise Gate2CanaryError("required live broker profiles are missing")
    for profile in profiles:
        profile["capabilities"] = [
            "research" if profile["id"] == ASSESSMENT_PROFILE_ID else "verify"
        ]
        profile["limits"]["max_cost_usd"] = 0.25
    policy = {
        "schema_version": SCHEMA_VERSION,
        "policy_version_ref": ROUTING_POLICY_REF,
        "id": "model-routing-policy:gate2-real-canary",
        "version": 1,
        "created_at": _wire_time(checked_at),
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": sorted(wanted),
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
            "required_modalities": ["text"],
            "family_independence_capabilities": ["verify"],
        },
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }
    with ModelRouter(router_path) as router:
        installed_profiles = [router.register_profile(profile) for profile in profiles]
        installed_policy = router.register_policy(policy)
    return {"policy": installed_policy, "profiles": installed_profiles}


def _key_provider(path: Path) -> Callable[[], bytes]:
    def read_key() -> bytes:
        try:
            value = path.read_bytes().strip()
        except OSError as exc:
            raise Gate2CanaryError("broker authentication key is unavailable") from exc
        if len(value) != 64:
            raise Gate2CanaryError("broker authentication key has an invalid length")
        return value

    return read_key


def _broker_replay_only_preflight(
    *, socket_path: Path, auth_key_path: Path
) -> dict[str, Any]:
    """Prove the live broker accepts replayOnly without calling a provider."""

    identity = hashlib.sha256(
        b"gate2-live-broker-replay-only-protocol-preflight"
    ).hexdigest()[:32]
    core_request = {
        "schemaVersion": SCHEMA_VERSION,
        "invocationId": f"invocation:gate2-preflight-{identity}",
        "workOrderId": f"work:gate2-preflight-{identity}",
        "profileId": ASSESSMENT_PROFILE_ID,
        "model": "openai/gpt-5.6-sol",
        "prompt": "Gate 2 replay-only protocol preflight; do not call a provider.",
        "maxTokens": 1,
        "timeoutMs": 1,
        "replayOnly": True,
    }
    auth = {
        "scheme": "hmac-sha256-v1",
        "clientId": BROKER_CLIENT_ID,
        "timestampMs": int(_now().timestamp() * 1000),
        "nonce": secrets.token_hex(16),
    }
    unsigned = {**core_request, "auth": auth}
    secret = _key_provider(auth_key_path)()
    auth["mac"] = hmac.new(
        secret,
        canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    frame = (canonical_json({**core_request, "auth": auth}) + "\n").encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    response = bytearray()
    try:
        client.settimeout(5.0)
        client.connect(os.fspath(socket_path))
        client.sendall(frame)
        while True:
            chunk = client.recv(16_384)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 262_144:
                raise Gate2CanaryError("broker preflight response exceeded frame limit")
    except OSError as exc:
        raise Gate2CanaryError("broker replay-only preflight could not connect") from exc
    finally:
        client.close()
    if not response or response[-1:] != b"\n" or response.count(b"\n") != 1:
        raise Gate2CanaryError("broker replay-only preflight returned an invalid frame")
    try:
        wire = json.loads(bytes(response[:-1]).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Gate2CanaryError("broker replay-only preflight returned invalid JSON") from exc
    expected_keys = {
        "schemaVersion", "brokerVersion", "runtimeVersion", "invocationId",
        "workOrderId", "profileId", "requestHash", "idempotencyStatus", "ok",
        "provider", "model", "canonicalModel", "agentId", "text", "usage",
        "cost", "error", "contentHash",
    }
    if (
        not isinstance(wire, dict)
        or set(wire) != expected_keys
        or wire.get("ok") is not False
        or wire.get("idempotencyStatus") != "fresh"
        or wire.get("error") != {
            "code": "IDEMPOTENCY_MISS",
            "message": (
                "no durable completion exists; replay-only request did not call the host"
            ),
        }
        or wire.get("text") is not None
    ):
        error = wire.get("error") if isinstance(wire, dict) else None
        raise Gate2CanaryError(
            "live broker does not expose the required replay-only protocol; "
            f"error={error!r}"
        )
    unhashed = dict(wire)
    asserted_hash = unhashed.pop("contentHash")
    if asserted_hash != hashlib.sha256(
        canonical_json(unhashed).encode("utf-8")
    ).hexdigest():
        raise Gate2CanaryError("broker replay-only preflight hash did not verify")
    return {
        "status": "pass",
        "broker_version": wire["brokerVersion"],
        "runtime_version": wire["runtimeVersion"],
        "provider_called": False,
        "error_code": wire["error"]["code"],
    }


def _worker(
    *,
    authorities: Authorities,
    router_path: Path,
    socket_path: Path,
    auth_key_path: Path,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[ModelRouter, ThesisImpactModelWorker]:
    router = ModelRouter(router_path)
    adapter = OpenClawModelAdapter(
        socket_path,
        route_resolver=router.get_decision,
        expected_agent_id=BROKER_AGENT_ID,
        auth_client_id=BROKER_CLIENT_ID,
        auth_key_provider=_key_provider(auth_key_path),
        timeout_seconds=120.0,
        clock=_now,
    )
    worker = ThesisImpactModelWorker(
        scheduler=authorities.scheduler,
        router=router,
        adapter=adapter,
        impact=authorities.impact,
        observability=authorities.observability,
        routing_policy_ref=ROUTING_POLICY_REF,
        credential_slot_refs=(
            "credential-slot:openclaw:openai",
            "credential-slot:openclaw:claude-cli",
        ),
        clock=_now,
        fault_hook=fault_hook,
    )
    return router, worker


def _plan_ref(root: Path) -> str:
    source = _load_object(root / "result.json")
    try:
        plan_ref = source["plan"]["ref"]
    except (KeyError, TypeError) as exc:
        raise Gate2CanaryError("source result lacks a plan ref") from exc
    if source.get("status") != "autonomous-closed":
        raise Gate2CanaryError("source result is not an autonomous closed plan")
    return plan_ref


def _crash_child(args: argparse.Namespace) -> int:
    root = args.output_dir.expanduser().resolve()
    plan_ref = _plan_ref(root)

    def crash(seam: str) -> None:
        if seam == "after_model_accounting":
            os._exit(CRASH_EXIT_CODE)

    with Authorities(root) as authorities:
        started = authorities.control.start_from_closed_plan(
            plan_version_ref=plan_ref,
            thesis_ref=THESIS_REF,
        )
        router, worker = _worker(
            authorities=authorities,
            router_path=root / "model-router.sqlite",
            socket_path=args.socket_path,
            auth_key_path=args.auth_key_path,
            fault_hook=crash,
        )
        try:
            worker.run_once(started["assessment_work_order"])
        finally:
            router.close()
    raise Gate2CanaryError("fault hook did not terminate the assessment worker")


def _gate2_rows(core: DaltonStore) -> dict[str, list[dict[str, Any]]]:
    invocations = [
        json.loads(row["invocation_json"])
        for row in core.connection.execute(
            "SELECT invocation_json FROM model_invocations "
            "WHERE actor_ref=? ORDER BY created_at,invocation_id",
            (BROKER_INVOCATION_ACTOR,),
        ).fetchall()
    ]
    usage = [
        json.loads(row["record_json"])
        for row in core.connection.execute(
            "SELECT u.record_json FROM observability_usage_entries u "
            "JOIN model_invocations m ON m.invocation_id=u.invocation_ref "
            "WHERE m.actor_ref=? ORDER BY u.created_at,u.usage_entry_id",
            (BROKER_INVOCATION_ACTOR,),
        ).fetchall()
    ]
    costs = [
        json.loads(row["record_json"])
        for row in core.connection.execute(
            "SELECT c.record_json FROM observability_cost_entries c "
            "JOIN observability_usage_entries u ON u.usage_entry_id=c.usage_entry_ref "
            "JOIN model_invocations m ON m.invocation_id=u.invocation_ref "
            "WHERE m.actor_ref=? ORDER BY c.created_at,c.cost_entry_id",
            (BROKER_INVOCATION_ACTOR,),
        ).fetchall()
    ]
    return {"invocations": invocations, "usage": usage, "costs": costs}


def _total_cost_usd(rows: dict[str, list[dict[str, Any]]]) -> Decimal:
    total_micros = 0
    for cost in rows["costs"]:
        amount = cost.get("amount_micros")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise Gate2CanaryError("Gate 2 has an unpriced model cost")
        total_micros += amount
    return Decimal(total_micros) / Decimal(1_000_000)


def _counts(rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {name: len(values) for name, values in rows.items()}


def _wait_for_lease_expiry(root: Path, work_order_ref: str) -> dict[str, Any]:
    connection = sqlite3.connect(root / "core.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        lease = connection.execute(
            "SELECT * FROM scheduler_leases WHERE work_order_id=? "
            "ORDER BY lease_version DESC LIMIT 1",
            (work_order_ref,),
        ).fetchone()
    finally:
        connection.close()
    if lease is None:
        raise Gate2CanaryError("crashed worker left no Scheduler lease")
    expires_at = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
    wait_seconds = max(0.0, (expires_at - _now()).total_seconds() + 0.25)
    if wait_seconds > 31.0:
        raise Gate2CanaryError("assessment lease expiry exceeds the bounded wait")
    if wait_seconds:
        print(f"waiting {wait_seconds:.2f}s for crashed lease expiry", flush=True)
        time.sleep(wait_seconds)
    return {
        "lease_id": lease["lease_id"],
        "attempt_number": lease["attempt_number"],
        "expires_at": lease["expires_at"],
        "wait_seconds": round(wait_seconds, 3),
    }


def _assert_budget_contract(spend_cap: Decimal) -> dict[str, str]:
    if spend_cap <= 0:
        raise Gate2CanaryError("spend cap must be positive")
    reserved = Decimal(str(ASSESSMENT_BUDGET["max_cost_usd"])) + Decimal(
        str(VERIFIER_BUDGET["max_cost_usd"])
    )
    if reserved > spend_cap:
        raise Gate2CanaryError(
            f"WorkOrder hard maximum {reserved} exceeds authorized cap {spend_cap}"
        )
    return {
        "authorized_total_spend_cap_usd": format(spend_cap, "f"),
        "assessment_hard_cap_usd": str(ASSESSMENT_BUDGET["max_cost_usd"]),
        "verifier_hard_cap_usd": str(VERIFIER_BUDGET["max_cost_usd"]),
        "maximum_executable_spend_usd": format(reserved, "f"),
    }


def _integrity(
    authorities: Authorities, router: ModelRouter
) -> dict[str, str]:
    result = {
        "core": authorities.core.connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0],
        "review": authorities.review.connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0],
        "coordinator": authorities.records.connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0],
        "router": router.connection.execute("PRAGMA integrity_check").fetchone()[0],
    }
    if set(result.values()) != {"ok"}:
        raise Gate2CanaryError(f"SQLite integrity failed: {result}")
    return result


def _parent(args: argparse.Namespace) -> int:
    head, dirty = _git_state()
    if dirty and not args.allow_dirty:
        raise Gate2CanaryError("repository must be clean before a paid canary")
    source = args.source_dir.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        raise Gate2CanaryError(f"output directory already exists: {output}")
    _required_paths(source)
    broker_preflight = _broker_replay_only_preflight(
        socket_path=args.socket_path,
        auth_key_path=args.auth_key_path,
    )
    shutil.copytree(source, output)
    os.chmod(output, 0o700)
    for path in output.iterdir():
        if path.is_file():
            os.chmod(path, 0o600)
    plan_ref = _plan_ref(output)
    budget = _assert_budget_contract(args.spend_cap_usd)
    router_install = _install_router(output / "model-router.sqlite")

    with Authorities(output) as authorities:
        admission = _admit_thesis(authorities, policy_owner=args.policy_owner)
        started = authorities.control.start_from_closed_plan(
            plan_version_ref=plan_ref,
            thesis_ref=THESIS_REF,
        )
        if started["status"] != "assessment_ready":
            raise Gate2CanaryError(f"assessment did not become ready: {started['status']}")
        assessment_work = started["assessment_work_order"]
        if assessment_work["budget"] != ASSESSMENT_BUDGET:
            raise Gate2CanaryError("assessment WorkOrder budget drifted")
        pointer_before = dict(authorities.core.current_pointer(THESIS_REF) or {})
    print("isolated thesis admitted; assessment enqueued", flush=True)

    child_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--crash-child",
        "--output-dir",
        str(output),
        "--socket-path",
        str(args.socket_path),
        "--auth-key-path",
        str(args.auth_key_path),
    ]
    print("starting paid assessment with crash injection", flush=True)
    try:
        child = subprocess.run(
            child_command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise Gate2CanaryError("assessment child exceeded 180 seconds") from exc
    if child.returncode != CRASH_EXIT_CODE:
        raise Gate2CanaryError(
            "assessment child did not stop at the crash seam; "
            f"exit={child.returncode}, stderr={child.stderr[-1000:]!r}"
        )
    print("assessment worker terminated after durable accounting", flush=True)

    with Authorities(output) as crashed:
        crash_rows = _gate2_rows(crashed.core)
        if _counts(crash_rows) != {"invocations": 1, "usage": 1, "costs": 1}:
            raise Gate2CanaryError(
                f"crash seam accounting did not commit exactly once: {_counts(crash_rows)}"
            )
        if crashed.scheduler.formal_result(assessment_work["id"]) is not None:
            raise Gate2CanaryError("crash seam unexpectedly completed Scheduler result")
    lease = _wait_for_lease_expiry(output, assessment_work["id"])

    with Authorities(output) as authorities:
        router, worker = _worker(
            authorities=authorities,
            router_path=output / "model-router.sqlite",
            socket_path=args.socket_path,
            auth_key_path=args.auth_key_path,
        )
        try:
            recovered = worker.run_once(assessment_work)
            if recovered["status"] != "succeeded":
                raise Gate2CanaryError(
                    f"assessment recovery failed: {recovered.get('status')}, "
                    f"error={recovered.get('error_type') or recovered.get('output_error')}"
                )
            metadata = recovered["result"]["metadata"]
            if (
                recovered.get("route_replayed") is not True
                or metadata.get("broker_request_mode") != "replay_only"
                or metadata.get("broker_idempotency_status") != "duplicate"
            ):
                raise Gate2CanaryError("assessment did not use duplicate replay-only recovery")
            after_recovery = _gate2_rows(authorities.core)
            if _counts(after_recovery) != _counts(crash_rows):
                raise Gate2CanaryError("assessment recovery duplicated accounting rows")
            assessed = authorities.control.advance_assessment(
                plan_version_ref=plan_ref,
                thesis_ref=THESIS_REF,
            )
            assessment = assessed["assessment"]["assessment"]
            verifier_work = assessed["verifier_work_order"]
            if verifier_work["budget"] != VERIFIER_BUDGET:
                raise Gate2CanaryError("verifier WorkOrder budget drifted")
            if _total_cost_usd(after_recovery) + Decimal(
                str(VERIFIER_BUDGET["max_cost_usd"])
            ) > args.spend_cap_usd:
                raise Gate2CanaryError("verifier admission would exceed hard spend cap")
            print("assessment recovered; starting independent verifier", flush=True)
            verified_run = worker.run_once(verifier_work)
            if verified_run["status"] != "succeeded":
                raise Gate2CanaryError(
                    f"verifier failed: {verified_run.get('status')}, "
                    f"error={verified_run.get('error_type') or verified_run.get('output_error')}"
                )
            final = authorities.control.advance_verification(
                plan_version_ref=plan_ref,
                thesis_ref=THESIS_REF,
                assessment_ref=assessment["id"],
            )
            if final["status"] != "eligible":
                raise Gate2CanaryError(
                    f"verified assessment is not eligible: {final['status']}"
                )
            rows_before_replay = _gate2_rows(authorities.core)
            total_cost = _total_cost_usd(rows_before_replay)
            if total_cost > args.spend_cap_usd:
                raise Gate2CanaryError(
                    f"actual/estimated cost {total_cost} exceeded cap {args.spend_cap_usd}"
                )
            decisions = router.list_decisions()
            families = [item["selected_endpoint"]["family"] for item in decisions]
            if len(decisions) != 2 or len(set(families)) != 2:
                raise Gate2CanaryError(
                    f"producer/verifier family independence failed: {families}"
                )
            pointer_after = dict(authorities.core.current_pointer(THESIS_REF) or {})
            if pointer_after != pointer_before:
                raise Gate2CanaryError("thesis current pointer changed during impact canary")

            deny_adapter = OpenClawModelAdapter(
                output / "broker-must-not-be-called.sock",
                route_resolver=router.get_decision,
                expected_agent_id=BROKER_AGENT_ID,
                auth_client_id=BROKER_CLIENT_ID,
                auth_key_provider=lambda: (_ for _ in ()).throw(
                    Gate2CanaryError("broker key requested during formal replay")
                ),
                timeout_seconds=1.0,
                clock=_now,
            )
            deny_worker = ThesisImpactModelWorker(
                scheduler=authorities.scheduler,
                router=router,
                adapter=deny_adapter,
                impact=authorities.impact,
                observability=authorities.observability,
                routing_policy_ref=ROUTING_POLICY_REF,
                credential_slot_refs=(
                    "credential-slot:openclaw:openai",
                    "credential-slot:openclaw:claude-cli",
                ),
                clock=_now,
            )
            replayed = ResearchPlanThesisImpactRuntime(
                control=authorities.control,
                worker=deny_worker,
            ).run_once(plan_version_ref=plan_ref, thesis_ref=THESIS_REF)
            if (
                replayed["status"] != "eligible"
                or replayed["assessment_run"].get("replayed") is not True
                or replayed["verifier_run"].get("replayed") is not True
            ):
                raise Gate2CanaryError("formal end-to-end replay did not converge offline")
            rows_after_replay = _gate2_rows(authorities.core)
            if _counts(rows_after_replay) != _counts(rows_before_replay):
                raise Gate2CanaryError("formal replay added model accounting rows")
            integrity = _integrity(authorities, router)
            formal_claim = authorities.core.get_claim(
                started["claim_version_ref"]
            )["claim"]
            verification = final["eligible"]["verification"]
            result = {
                "schema_version": SCHEMA_VERSION,
                "status": "complete",
                "generated_at": _wire_time(),
                "repo_commit": head,
                "repo_dirty": dirty,
                "source_gate1_sample": str(source),
                "source_plan_ref": plan_ref,
                "formal_claim_ref": formal_claim["id"],
                "formal_claim_hash": formal_claim["content_hash"],
                "thesis_ref": THESIS_REF,
                "thesis_version_ref": pointer_before["version_id"],
                "thesis_pointer_unchanged": True,
                "owner_authorization": {
                    "owner_ref": args.policy_owner,
                    "scope": "isolated real thesis-impact canary",
                    "thesis_admission": admission,
                },
                "budget": budget,
                "broker_preflight": broker_preflight,
                "actual_or_estimated_total_cost_usd": format(total_cost, "f"),
                "model_accounting_counts": _counts(rows_before_replay),
                "model_calls": [
                    {
                        "invocation_ref": invocation["id"],
                        "work_order_ref": invocation["work_order_ref"],
                        "provider": invocation["provider"],
                        "model": invocation["model"],
                        "family": invocation["model_family"],
                        "usage": invocation["usage"],
                        "cost": rows_before_replay["costs"][index],
                    }
                    for index, invocation in enumerate(
                        rows_before_replay["invocations"]
                    )
                ],
                "assessment": assessment,
                "verification": verification,
                "crash_recovery": {
                    "worker_exit_code": CRASH_EXIT_CODE,
                    "lease": lease,
                    "provider_calls": 2,
                    "broker_socket_requests": 3,
                    "assessment_first_request": "execute/fresh",
                    "assessment_recovery_request": "replay_only/duplicate",
                    "recovery_duplicated_invocation": False,
                    "recovery_duplicated_usage": False,
                    "recovery_duplicated_cost": False,
                },
                "routes": [
                    {
                        "decision_ref": item["id"],
                        "capability": item["capability"],
                        "provider": item["selected_endpoint"]["provider"],
                        "model": item["selected_endpoint"]["model"],
                        "family": item["selected_endpoint"]["family"],
                        "producer_family_constraint": item["constraints"][
                            "producer_family"
                        ],
                    }
                    for item in decisions
                ],
                "router_install": router_install,
                "formal_replay": {
                    "status": replayed["status"],
                    "broker_access_blocked_by_test_adapter": True,
                    "accounting_counts_unchanged": True,
                },
                "integrity": integrity,
            }
            _write_object(output / "gate2-result.json", result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        finally:
            router.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy-owner", default="human:lumos-obliviate")
    parser.add_argument("--spend-cap-usd", type=Decimal, default=Decimal("1.00"))
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=Path("/Users/everflow/.openclaw/dalton-model-broker.sock"),
    )
    parser.add_argument(
        "--auth-key-path",
        type=Path,
        default=Path("/Users/everflow/.openclaw/dalton-model-broker.sock.key"),
    )
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--crash-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.policy_owner.startswith("human:"):
        parser.error("--policy-owner must use the human: namespace")
    if args.crash_child:
        return _crash_child(args)
    if args.source_dir is None:
        parser.error("--source-dir is required")
    try:
        return _parent(args)
    except Gate2CanaryError as exc:
        print(f"Gate 2 canary failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
