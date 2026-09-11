"""Run the Core-hosted SEC company-facts lane on an existing Core state directory.

Importable CLI in the ``alphaengine_acquisition_cli`` style.  Two source
modes, mutually exclusive:

* ``--fixture-company-facts PATH``: serve the JSON bytes in PATH as the SEC
  ``companyfacts`` response (tests / rehearsal; no network).
* ``--allow-network``: real ``data.sec.gov`` reads.  Requires a committed
  approved governance record (``--governance``); ``--rehearsal-approved-by``
  is refused.

Writes ``<summary-dir>/summary.json`` (owner-only).  Exit 0 only when every
issuer is ``committed`` or ``duplicate``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import hashlib
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .sec_company_facts_lane import (
    DEFAULT_USER_AGENT,
    Issuer,
    LanePreconditionError,
    RehearsalGovernance,
    SecCompanyFactsLane,
    US_IT_SERVICES_ISSUERS,
)
from .sec_public_adapter import SEC_COMPANY_FACTS_FORMS
from .store import canonical_json


class _FixtureModelAdapter:
    """Deterministic rehearsal provider that still traverses Router/Scheduler."""

    def __init__(self, output: Any, clock) -> None:
        self.outputs = list(output) if isinstance(output, list) else [output]
        if not self.outputs:
            raise ValueError("annual fixture output sequence must not be empty")
        self.clock = clock

    def replay(self, *_args):
        raise AssertionError("fixture invocation must not be replayed")

    def execute(self, work, route, selected):
        from .contracts import InvocationGranularity, ModelInvocation, ResultEnvelope
        from .store import content_hash

        now = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        suffix = content_hash({"work": work.id, "attempt": route["attempt_number"]})[:20]
        invocation = ModelInvocation(
            schema_version="0.1", id="invocation:annual-fixture:" + suffix,
            created_at=now, work_order_ref=work.id,
            profile_ref=selected["profile_version_ref"],
            granularity=InvocationGranularity.TASK,
            capability=work.requested_capabilities[0], provider=selected["provider"],
            model=selected["model"], model_family=selected["family"],
            input_refs=work.input_refs, output_refs=(), started_at=now,
            completed_at=now,
            usage={"input_tokens": 100, "output_tokens": 40, "total_tokens": 140,
                   "cache_read_tokens": None, "cache_write_tokens": None,
                   "raw_provider_telemetry": {"cost": {"available": True, "usd": 0.002}}},
            side_effects=(), runtime_ref=selected["adapter_ref"],
            actor_ref="broker:annual-fixture", parent_ref=route["id"],
            environment_hash="environment:annual-fixture",
        )
        # Bind sequence selection to the formal Scheduler attempt so a killed
        # rehearsal child and its replacement observe the same deterministic
        # provider transcript instead of restarting the fixture at element 0.
        index = max(0, int(route["attempt_number"]) - 1)
        output = self.outputs[min(index, len(self.outputs) - 1)]
        failure_code = (
            output.get("fixture_provider_failure_code")
            if isinstance(output, dict) else None
        )
        text = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        broker_hash = hashlib.sha256(text.encode()).hexdigest()
        result = ResultEnvelope(
            schema_version="0.1", id="result:annual-fixture:" + suffix,
            created_at=now, work_order_ref=work.id, invocation_ref=invocation.id,
            status="failed" if failure_code is not None else "succeeded",
            outputs={} if failure_code is not None else {
                "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest()
            },
            actual_side_effects=(), usage_refs=("usage:" + invocation.id,),
            artifact_refs=(), error=(
                {"code": failure_code, "source": "openclaw-model-broker"}
                if failure_code is not None else None
            ),
            metadata={
                "route_decision_ref": route["id"],
                "profile_version_ref": selected["profile_version_ref"],
                **({
                    "broker_response_hash": broker_hash,
                    "broker_request_mode": "execute",
                    "dispatch_proof": {
                        "authority": "openclaw-model-adapter",
                        "state": "provider_completed_failure", "version": "0.1",
                    },
                } if failure_code is not None else {}),
            },
        )
        return invocation, result


def secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _fixture_adapter(payload: bytes, clock):
    from .public_http_transport import PublicHttpTransport
    from .sec_authority_harness import _Response
    from .sec_public_adapter import SecPublicRouterAdapter

    return SecPublicRouterAdapter(
        transport=PublicHttpTransport(
            resolver=lambda _host, _port: ("93.184.216.34",),
            exchange=lambda _t, _m, _h, _b, _timeout: _Response(payload),
        ),
        clock=clock,
    )


def _wait_for_annual_work(
    lane: Any, work_order_ref: str, *, realtime: bool,
    wait_marker: Path | None = None,
) -> bool:
    """Wait/advance to the exact Scheduler eligibility time for one plan node."""

    status = lane.scheduler.status(work_order_ref)
    claimable_at = status.get("not_before")
    if status.get("state") == "leased":
        row = lane.scheduler.connection.execute(
            "SELECT expires_at FROM scheduler_leases WHERE work_order_id=? "
            "ORDER BY lease_version DESC LIMIT 1",
            (work_order_ref,),
        ).fetchone()
        claimable_at = None if row is None else row["expires_at"]
    if claimable_at is None:
        return status.get("state") == "ready"
    target = datetime.fromisoformat(claimable_at).astimezone(timezone.utc)
    authority = lane.scheduler.work_order_authority(work_order_ref)
    if authority is None:
        return False
    maximum = authority["work_order"]["budget"].get("max_elapsed_seconds")
    if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
        history = lane.scheduler.attempt_history(work_order_ref)
        admitted = datetime.fromisoformat(history[0]["created_at"]).astimezone(timezone.utc)
        if target >= admitted + timedelta(seconds=maximum):
            return False
    delay = max(0.0, (target - lane.clock()).total_seconds())
    if realtime and delay > 0:
        if wait_marker is not None:
            _write_owner_only(wait_marker, {
                "work_order_ref": work_order_ref,
                "claimable_at": claimable_at,
            })
        time.sleep(delay)
        lane.clock.value = datetime.now(timezone.utc)
    elif delay > 0:
        lane.clock.advance(math.ceil(delay))
    return True


def load_governance(path: Path):
    try:
        from .connector_governance import load_connector_governance  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "dalton_core.connector_governance is not available in this checkout; "
            "the committed governance loader is a parallel slice. Use "
            "--rehearsal-approved-by human:<who> for an isolated rehearsal only."
        ) from exc
    return load_connector_governance(path)


def select_issuers(tickers: list[str] | None, overrides: dict[str, str]) -> tuple[Issuer, ...]:
    catalog = {issuer.ticker: issuer for issuer in US_IT_SERVICES_ISSUERS}
    for ticker, cik in overrides.items():
        catalog[ticker] = Issuer(ticker, cik, f"company:sec-cik:{int(cik):010d}", ticker)
    if not tickers:
        return US_IT_SERVICES_ISSUERS
    missing = [t for t in tickers if t not in catalog]
    if missing:
        raise SystemExit(f"unknown issuer ticker(s): {', '.join(missing)}; use --issuer-cik T=CIK")
    return tuple(catalog[t] for t in tickers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True, help="shared candidate staging sqlite")
    parser.add_argument("--governance", type=Path, help="committed connector governance record")
    parser.add_argument("--rehearsal-approved-by", help="in-memory approved governance (rehearsal only)")
    parser.add_argument("--issuer", action="append", help="ticker; repeat; default all four")
    parser.add_argument(
        "--issuer-cik", action="append", default=[],
        help="TICKER=CIK to add/override an issuer outside the default tuple",
    )
    parser.add_argument("--filed-from")
    parser.add_argument("--filed-to")
    parser.add_argument(
        "--form", choices=list(SEC_COMPANY_FACTS_FORMS), default="10-Q",
        help="company-facts form: 10-Q (default) or 10-K for issuers that report the "
             "fourth-quarter pair inside the annual filing",
    )
    parser.add_argument("--actor", required=True, help="human:<who> or automation:<mission>")
    parser.add_argument("--expected-accession")
    parser.add_argument("--mission-version-ref")
    parser.add_argument("--mission-version-hash")
    parser.add_argument("--mission-company-ref")
    parser.add_argument("--run-key", help="defaults to filed-to")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument("--spool-dir", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-company-facts", type=Path)
    parser.add_argument("--annual-plan-ref")
    parser.add_argument("--web-fetch-governance", type=Path)
    parser.add_argument("--annual-draft-model-config", type=Path)
    parser.add_argument("--annual-verifier-model-config", type=Path)
    parser.add_argument("--annual-draft-fixture", type=Path)
    parser.add_argument("--annual-verifier-fixture", type=Path)
    parser.add_argument("--annual-fixture-real-wait", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--annual-fixture-wait-marker", type=Path,
                        help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--stack-dump-seconds", type=int, default=0,
        help="periodically dump the Python stack to stderr (diagnostic for writer-hosted "
             "runs, which cannot be attached to without root)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    annual_mode = args.annual_plan_ref is not None
    if not annual_mode and args.fixture_company_facts is None and not args.allow_network:
        parser.error("choose --fixture-company-facts or --allow-network")
    if not annual_mode and args.fixture_company_facts is not None and args.allow_network:
        parser.error("--fixture-company-facts and --allow-network are mutually exclusive")
    if not annual_mode and (args.filed_from is None or args.filed_to is None):
        parser.error("company-facts mode requires --filed-from and --filed-to")
    if annual_mode:
        if any(value is None for value in (
            args.web_fetch_governance, args.annual_draft_model_config,
            args.annual_verifier_model_config, args.spool_dir,
        )):
            parser.error("annual mode requires web-fetch governance, spool and both model configurations")
        fixtures = (args.annual_draft_fixture, args.annual_verifier_fixture)
        if any(item is not None for item in fixtures) and not all(item is not None for item in fixtures):
            parser.error("annual rehearsal requires both draft and verifier fixtures")
    if args.rehearsal_approved_by and args.allow_network:
        parser.error("networked runs require --governance; --rehearsal-approved-by is rehearsal-only")
    if args.rehearsal_approved_by is None and args.governance is None:
        parser.error("--governance is required unless --rehearsal-approved-by is used")
    if not args.actor.startswith(("human:", "automation:")):
        parser.error("--actor must use the human: or automation: namespace")
    mission_values = (
        args.mission_version_ref, args.mission_version_hash, args.mission_company_ref
    )
    if args.actor.startswith("automation:"):
        if not args.expected_accession or any(value is None for value in mission_values):
            parser.error(
                "automation actor requires --expected-accession and all mission binding arguments"
            )
    elif args.expected_accession is not None or any(value is not None for value in mission_values):
        parser.error("human actor must not carry mission automation arguments")
    if args.stack_dump_seconds < 0:
        parser.error("--stack-dump-seconds must be >= 0")
    if args.stack_dump_seconds:
        import faulthandler

        # stderr is the launcher's run.log; a stalled child then leaves a
        # timestamped Python stack every interval instead of nothing.
        faulthandler.dump_traceback_later(args.stack_dump_seconds, repeat=True, file=sys.stderr)

    overrides = {}
    for item in args.issuer_cik:
        ticker, _, cik = item.partition("=")
        if not ticker or not cik.isdigit():
            parser.error("--issuer-cik expects TICKER=CIK")
        overrides[ticker] = cik
    issuers = select_issuers(args.issuer, overrides)
    governance = (
        RehearsalGovernance(approved_by=args.rehearsal_approved_by)
        if args.rehearsal_approved_by else load_governance(args.governance)
    )
    from .sec_authority_harness import MutableClock

    clock = MutableClock(datetime.now(timezone.utc))
    adapter = (
        _fixture_adapter(args.fixture_company_facts.read_bytes(), clock)
        if not annual_mode and args.fixture_company_facts is not None else None
    )
    summary_dir = secure_dir(args.summary_dir if args.summary_dir is not None else args.state_dir)
    try:
        annual_kwargs = {}
        router = None
        fetch_reader = None
        if annual_mode:
            from .annual_report_runtime import (
                adapter_for_config, load_annual_report_model_configs,
                plan_model_execution,
            )
            from .model_router import ModelRouter
            from .public_web_fetch_launcher import PublicWebFetchLauncher

            draft_config, verifier_config = load_annual_report_model_configs(
                args.state_dir, draft_path=args.annual_draft_model_config,
                verifier_path=args.annual_verifier_model_config,
            )
            router = ModelRouter(draft_config["model_router_db"])
            fetch_reader = PublicWebFetchLauncher(
                state_dir=args.state_dir, governance_path=args.web_fetch_governance,
                spool_dir=args.spool_dir,
            )
            if args.annual_draft_fixture is not None:
                draft_adapter = _FixtureModelAdapter(
                    json.loads(args.annual_draft_fixture.read_text(encoding="utf-8")), clock
                )
                verifier_adapter = _FixtureModelAdapter(
                    json.loads(args.annual_verifier_fixture.read_text(encoding="utf-8")), clock
                )
            else:
                draft_adapter = adapter_for_config(
                    draft_config, router=router,
                    purpose="registered_annual_report_draft",
                )
                verifier_adapter = adapter_for_config(
                    verifier_config, router=router,
                    purpose="registered_annual_report_verifier",
                )
            annual_kwargs = {
                "annual_report_manifest_reader": fetch_reader.read_completed_manifest,
                "annual_report_model_router": router,
                "annual_report_draft_adapter": draft_adapter,
                "annual_report_verifier_adapter": verifier_adapter,
                "annual_report_draft_model_execution": plan_model_execution(
                    draft_config, "registered_annual_report_draft"
                ),
                "annual_report_verifier_model_execution": plan_model_execution(
                    verifier_config, "registered_annual_report_verifier"
                ),
            }
        with SecCompanyFactsLane(
            state_dir=args.state_dir,
            staging_path=args.staging,
            governance=governance,
            issuers=issuers,
            catalog_db=args.catalog_db,
            spool_dir=args.spool_dir,
            user_agent=args.user_agent,
            adapter=adapter,
            clock=clock,
            **annual_kwargs,
        ) as lane:
            if annual_mode:
                # One call can complete one attempt and admit the next node.
                # Derive the finite driver budget from the immutable plan
                # instead of imposing an execution ceiling unrelated to the
                # owner's configured attempt budgets.
                plan = lane.plans.plan_version(args.annual_plan_ref)
                if (
                    plan["schema_version"] != "0.2"
                    or plan["execution_scope"]["operation"]
                    != "search_registered_annual_report"
                ):
                    raise LanePreconditionError(
                        "annual mode requires a registered annual-report 0.2 plan"
                    )
                transition_budget = sum(
                    int(step["max_attempts"])
                    for step in plan["execution_scope"]["steps"]
                )
                outcomes = []
                transitions = 0
                realtime = (
                    args.annual_draft_fixture is None
                    or args.annual_fixture_real_wait
                )
                while transitions < transition_budget:
                    if realtime:
                        lane.clock.value = datetime.now(timezone.utc)
                    outcome = lane.executor.run_once(plan_version_ref=args.annual_plan_ref)
                    outcomes.append(outcome)
                    if outcome.get("status") in {"complete", "blocked", "failed"}:
                        break
                    if outcome.get("status") not in {"pending", "waiting"}:
                        transitions += 1
                    if outcome.get("status") in {"retryable", "pending", "waiting"}:
                        work_ref = outcome.get("work_order_ref")
                        if not isinstance(work_ref, str) or not _wait_for_annual_work(
                            lane, work_ref, realtime=realtime,
                            wait_marker=args.annual_fixture_wait_marker,
                        ):
                            break
                summary = {
                    "ok": bool(outcomes and outcomes[-1].get("status") == "complete"),
                    "operation": "registered_annual_report",
                    "plan_version_ref": args.annual_plan_ref,
                    "outcomes": outcomes,
                }
            else:
                summary = lane.run_lane(
                filed_from=args.filed_from,
                filed_to=args.filed_to,
                actor_ref=args.actor,
                run_key=args.run_key or args.filed_to,
                form=args.form,
                expected_accession=args.expected_accession,
                mission_context=(
                    {
                        "mission_version_ref": args.mission_version_ref,
                        "mission_version_hash": args.mission_version_hash,
                        "company_ref": args.mission_company_ref,
                    }
                    if args.actor.startswith("automation:") else None
                ),
                )
    except LanePreconditionError as exc:
        print(f"lane precondition failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if fetch_reader is not None:
            fetch_reader.close()
        if router is not None:
            router.close()
    summary["transport"] = (
        "local-registered-sec-source" if annual_mode
        else "fixture" if adapter is not None else "data.sec.gov"
    )
    _write_owner_only(summary_dir / "summary.json", summary)
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
