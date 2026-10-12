"""Out-of-process launcher for the Investment Memo producer."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected


class InvestmentMemoLauncher(LaneChildLauncher):
    TICKET_PREFIX = "investment-memo-run"
    TICKETS_DIRNAME = "investment-memo-runs"
    CHILD_MODULE = "dalton_core.investment_memo_cli"

    def __init__(self, *, state_dir: str | Path, model_config_path: str | Path,
                 verifier_model_config_path: str | Path, scheduler_db: str | Path | None = None,
                 **kwargs: Any) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config_path = Path(model_config_path).expanduser().resolve()
        self.verifier_model_config_path = Path(verifier_model_config_path).expanduser().resolve()
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db).expanduser().resolve()

    def _command(self, *, ticket_dir: Path, company_ref: str | None = None) -> list[str]:
        command = [self.python_executable, "-m", self.CHILD_MODULE,
                   "--state-dir", str(self.state_dir), "--summary-dir", str(ticket_dir), "--quiet",
                   "--model-config", str(self.model_config_path),
                   "--verifier-model-config", str(self.verifier_model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        if company_ref:
            command += ["--company-ref", company_ref]
        return command

    def start(self, *, signature: str, company_ref: str | None = None) -> dict[str, Any]:
        if not signature:
            raise LaneChildRejected("a memo run needs an input signature")
        from .cockpit_model import verifier_provider_contract_fingerprint
        provider_contract = verifier_provider_contract_fingerprint(
            "investment_memo_verifier")
        digest = hashlib.sha256(
            f"memo|{company_ref or '-'}|{signature}|{provider_contract}".encode()
        ).hexdigest()[:24]
        from .investment_memo_quiesce import quiesced_summary_matches
        # A paused run's immutable request remains in its own directory. A
        # successor gets a fresh directory; it replays the same formal calls.
        # Do not delete the request or overwrite the parent's pause receipt.
        resumed_from = None
        for _ in range(64):
            path = self.tickets_dir / digest
            if not (path / "quiesce-request.json").exists():
                if (path / 'ticket.json').exists():
                    ticket=self.status(f"{self.TICKET_PREFIX}:{digest}")
                    if ticket['status']=='running':
                        return ticket
                    if (ticket.get('summary') or {}).get('memo_status')=='macro_accounting_pending':
                        from .investment_memo_accounting_progress import resume_checkpoint
                        try:
                            checkpoint=resume_checkpoint(ticket['summary'],ticket,
                                self.scheduler_db or self.state_dir / 'scheduler.sqlite')
                        except (ValueError,OSError) as exc:
                            raise LaneChildRejected('memo accounting progress is unproved: '+str(exc)) from exc
                        resumed_from=ticket['id']
                        # Read current owner progress directly rather than
                        # walking an unbounded chain of free paging children.
                        digest=hashlib.sha256((resumed_from+'|accounting|'+checkpoint).encode()).hexdigest()[:24]
                break
            ticket = self.status(f"{self.TICKET_PREFIX}:{digest}")
            if ticket["status"] == "running":
                return ticket
            summary = ticket.get("summary")
            if not quiesced_summary_matches(summary, ticket):
                raise LaneChildRejected("memo pause is unproved; no automatic resume")
            resumed_from = ticket["id"]
            digest = hashlib.sha256((resumed_from + "|" + summary["quiesce"]["content_hash"]).encode()).hexdigest()[:24]
        else:
            raise LaneChildRejected("memo pause chain exceeds the bounded 64-run lookup")
        try:
            fingerprints = [hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (self.model_config_path, self.verifier_model_config_path)]
        except OSError as exc:
            raise LaneChildRejected("memo configuration cannot be frozen") from exc
        record = {"signature": signature, "company_ref": company_ref,
                  "model_config_path": str(self.model_config_path),
                  "model_config_fingerprint": fingerprints[0],
                  "verifier_model_config_path": str(self.verifier_model_config_path),
                  "verifier_model_config_fingerprint": fingerprints[1]}
        if resumed_from is not None:
            record["resumed_from"] = resumed_from
        return self.spawn(digest=digest, record=record,
                          company_ref=company_ref)


__all__ = ["InvestmentMemoLauncher"]
