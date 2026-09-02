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
from datetime import datetime, timezone
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
    parser.add_argument("--filed-from", required=True)
    parser.add_argument("--filed-to", required=True)
    parser.add_argument(
        "--form", choices=list(SEC_COMPANY_FACTS_FORMS), default="10-Q",
        help="company-facts form: 10-Q (default) or 10-K for issuers that report the "
             "fourth-quarter pair inside the annual filing",
    )
    parser.add_argument("--actor", required=True, help="human:<who>")
    parser.add_argument("--run-key", help="defaults to filed-to")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument("--spool-dir", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-company-facts", type=Path)
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
    if args.fixture_company_facts is None and not args.allow_network:
        parser.error("choose --fixture-company-facts or --allow-network")
    if args.fixture_company_facts is not None and args.allow_network:
        parser.error("--fixture-company-facts and --allow-network are mutually exclusive")
    if args.rehearsal_approved_by and args.allow_network:
        parser.error("networked runs require --governance; --rehearsal-approved-by is rehearsal-only")
    if args.rehearsal_approved_by is None and args.governance is None:
        parser.error("--governance is required unless --rehearsal-approved-by is used")
    if not args.actor.startswith("human:"):
        parser.error("--actor must use the human: namespace")
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
        if args.fixture_company_facts is not None else None
    )
    summary_dir = secure_dir(args.summary_dir if args.summary_dir is not None else args.state_dir)
    try:
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
        ) as lane:
            summary = lane.run_lane(
                filed_from=args.filed_from,
                filed_to=args.filed_to,
                actor_ref=args.actor,
                run_key=args.run_key or args.filed_to,
                form=args.form,
            )
    except LanePreconditionError as exc:
        print(f"lane precondition failed: {exc}", file=sys.stderr)
        return 1
    summary["transport"] = "fixture" if adapter is not None else "data.sec.gov"
    _write_owner_only(summary_dir / "summary.json", summary)
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
