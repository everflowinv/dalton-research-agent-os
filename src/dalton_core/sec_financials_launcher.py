"""P13aj: launch the SEC statements child, one company at a time.

The child parses a filing and validates an observation; this decides when one
runs and keeps the ticket. It refuses before spawning when the governance
record on disk is not approved, because a launch that was always going to be
refused still costs a process and a ticket, and the reason arrives further from
the cause.

The run is named by what it asks for -- company, form, how many filings -- so
asking twice for the same thing is the same ticket rather than a second child
doing identical work.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from .lane_child_launcher import (
    LaneChildLauncher,
    LaneChildRejected,
)

TICKET_PREFIX = "sec-financials-run"
LIVE_MODE_ARGS: tuple[str, ...] = ("--allow-network",)
MAX_FILINGS = 8


class SecFinancialsLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.sec_financials_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "sec-financials-runs"
    CHILD_MODULE = "dalton_core.sec_financials_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        user_agent: str | None = None,
        scheduling_forms: Sequence[str] = ("10-K", "10-Q"),
        scheduling_limits: dict[str, int] | None = None,
        profile_spool_dir: str | Path | None = None,
        governance_loader: Callable[[Path], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.mode_args = tuple(str(item) for item in mode_args)
        self.user_agent = user_agent
        self.scheduling_forms = tuple(scheduling_forms)
        self.scheduling_limits = dict(
            {"10-K": 1} if scheduling_limits is None else scheduling_limits
        )
        self.profile_spool_dir = Path(profile_spool_dir).resolve() if profile_spool_dir else self.state_dir/'connector-spool'
        self._governance_loader = governance_loader
        self._reparse_lock = threading.Lock()

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def load_governance(self) -> Any:
        from .connector_governance import ConnectorGovernance

        loader = self._governance_loader or ConnectorGovernance.load
        return loader(self.governance_path)

    def reparse_governance_path(self) -> Path:
        """Retain the approved v5 local replay contract under a v6 selector."""
        from .sec_financials_cli import _load_governance
        _, version = _load_governance(self.governance_path)
        path = (self.governance_path.parent / "sec-financial-statements-v5.json"
                if version == 6 else self.governance_path)
        if path.is_symlink() or not path.is_file():
            raise LaneChildRejected("cached SEC reparse retained v5 approval is unavailable")
        _, replay_version = _load_governance(path)
        if replay_version != 5:
            raise LaneChildRejected("cached SEC reparse retained contract differs")
        return path

    def observation_governance(self, summary):
        from .sec_financials_cli import _load_governance
        path = (self.reparse_governance_path()
                if summary.get("transport") == "local-original-replay" else self.governance_path)
        governance, _ = _load_governance(path)
        if (governance.id != summary.get("governance_ref")
                or governance.content_hash != summary.get("governance_hash")):
            raise LaneChildRejected("statement observation approval differs from its original")
        return governance

    def _command(self, *, ticket_dir: Path, ticker: str, form: str,
                 limit: int, replay_request_path: str | None = None, native_request_path: str | None = None) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--ticker", ticker, "--form", form, "--limit", str(limit),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.user_agent:
            command += ["--user-agent", self.user_agent]
        if native_request_path is not None:
            command += ["--native-request", native_request_path, "--profile-spool-dir", str(self.profile_spool_dir)]
        if replay_request_path is not None:
            command[command.index("--governance") + 1] = str(self.reparse_governance_path())
            return command + ["--replay-request", replay_request_path]
        return command + list(self.mode_args)

    def start_reparse(self, *, replay_request_path, missions, actor_ref):
        from .sec_statement_cached_reparse import validate_request, CONTRACT
        from .sec_financials_cli import _load_governance
        governance, version = _load_governance(self.reparse_governance_path())
        try:
            replay, _ = validate_request(replay_request_path, self.state_dir, missions, governance)
        except ValueError as exc:
            raise LaneChildRejected(str(exc)) from exc
        if version != 5 or actor_ref != replay["authorization"]["actor_ref"]:
            raise LaneChildRejected("cached SEC reparse actor or contract differs")
        proof = replay["original"]
        digest = hashlib.sha256(f"{CONTRACT}|{proof['ticket_ref']}".encode()).hexdigest()[:24]
        ticket_ref = self.TICKET_PREFIX + ":" + digest
        with self._reparse_lock:
            path = self._ticket_path(ticket_ref)
            if path.exists() or path.is_symlink():
                if path.is_symlink():
                    raise LaneChildRejected("cached SEC reparse ticket must be regular")
                prior = self.status(ticket_ref)
                if (prior.get("transport") != "local-original-replay"
                        or prior.get("original_artifact_hash") != proof["artifact_hash"]
                        or prior.get("original_ticket_ref") != proof["ticket_ref"]
                        or prior.get("governance_hash") != governance.content_hash):
                    raise LaneChildRejected("cached SEC reparse ticket authority differs")
                return prior  # Failed, unknown, orphaned or success: never a second child.
            return self.spawn(digest=digest, record={
                "ticker": proof["ticker"], "form": proof["form"], "limit": proof["limit"],
                "actor_ref": actor_ref, "governance_ref": governance.id, "governance_hash": governance.content_hash,
                "transport": "local-original-replay", "replay_request_hash": replay["content_hash"],
                "original_ticket_ref": proof["ticket_ref"], "original_artifact_hash": proof["artifact_hash"]},
                ticker=proof["ticker"], form=proof["form"], limit=proof["limit"],
                replay_request_path=str(replay_request_path))

    def native_enabled(self) -> bool:
        from .sec_financials_cli import _load_governance
        governance, version = _load_governance(self.governance_path)
        if version == 6:
            from .sec_native_annual import verify_native_approval
            verify_native_approval(governance)
        return version == 6

    def start_native(self, *, missions, dispatch, actor_ref):
        import json
        from .store import content_hash, canonical_json
        from .sec_native_annual import verify_request, load_admitted_request, verify_native_approval
        from .raw_spool import RawSpoolReader
        from .sec_financials_cli import _load_governance
        governance, version = _load_governance(self.governance_path)
        if version != 6 or actor_ref != dispatch["actor_ref"]:
            raise LaneChildRejected("native annual requires exact approved v6 and dispatch actor")
        verify_native_approval(governance)
        value=missions.native_statement_request(dispatch["dispatch_id"])
        auth=missions.sec_lane_authorization_for_company(dispatch["company_ref"])
        try:
            verify_request(missions.connection, RawSpoolReader(self.profile_spool_dir), value, current_authorization=auth)
        except (ValueError, KeyError, TypeError) as exc:
            raise LaneChildRejected("native annual immutable source authority differs: "+str(exc)) from exc
        directory=self.state_dir/'sec-native-statement-requests'
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(dispatch["dispatch_id"].split(":",1)[1]+'.json')
        body={"dispatch_ref":dispatch["dispatch_id"],"request":value}
        packet={**body,"content_hash":content_hash(body)}
        if path.exists() or path.is_symlink():
            if path.is_symlink() or json.loads(path.read_bytes())!=packet:
                raise LaneChildRejected("native annual frozen request file differs")
        else:
            from .sec_financials_cli import _write_owner_only
            _write_owner_only(path,packet)
        # An admission written by the owner is required; a self-hashed file
        # alone never grants a child or replaces a source proof.
        load_admitted_request(self.state_dir,path,spool_dir=self.profile_spool_dir,require_current=True)
        digest=content_hash({"request":value["content_hash"],"governance":governance.content_hash})[:24]
        return self.spawn(digest=digest,record={"ticker":dispatch["ticker"],"form":dispatch["form"],
            "limit":dispatch["filing_limit"],"actor_ref":actor_ref,"governance_ref":governance.id,
            "governance_hash":governance.content_hash,"native_request_hash":value["content_hash"],
            "transport":"public-https" if self.networked else "fixture"},
            ticker=dispatch["ticker"],form=dispatch["form"],limit=dispatch["filing_limit"],native_request_path=str(path))

    def start(self, *, ticker: str, form: str = "10-Q", limit: int = 1,
              actor_ref: str) -> dict[str, Any]:
        if not isinstance(ticker, str) or not ticker.strip():
            raise LaneChildRejected("a statements run needs a ticker")
        if form not in ("10-Q", "10-K"):
            raise LaneChildRejected("form must be 10-Q or 10-K")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_FILINGS:
            raise LaneChildRejected(f"limit must be 1..{MAX_FILINGS}")
        if not isinstance(actor_ref, str) or not actor_ref.startswith(
            ("human:", "automation:")
        ):
            raise LaneChildRejected("actor must use the human: or automation: namespace")
        # Refuse here rather than after a process exists: the child would reach
        # the same conclusion, having cost a spawn and a ticket to say so.
        governance = self.load_governance()
        if not getattr(governance, "approved", False):
            raise LaneChildRejected(
                "SEC financial-statements governance record is not approved"
            )
        ticker = ticker.strip().upper()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{ticker}|{form}|{limit}|{governance.content_hash}"
            .encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "ticker": ticker, "form": form, "limit": limit,
                "actor_ref": actor_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "public-https" if self.networked else "fixture",
            },
            ticker=ticker, form=form, limit=limit,
        )


__all__ = ["LIVE_MODE_ARGS", "MAX_FILINGS", "TICKET_PREFIX", "SecFinancialsLauncher"]
