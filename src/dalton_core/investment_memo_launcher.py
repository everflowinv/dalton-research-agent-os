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

    def start(self, *, signature: str, company_ref: str | None = None,
              recovery_ref: str | None = None) -> dict[str, Any]:
        if not signature:
            raise LaneChildRejected("a memo run needs an input signature")
        from .cockpit_model import verifier_provider_contract_fingerprint
        provider_contract = verifier_provider_contract_fingerprint(
            "investment_memo_verifier")
        digest = hashlib.sha256(
            f"memo|{company_ref or '-'}|{signature}|{provider_contract}|"
            f"{recovery_ref or '-'}".encode()
        ).hexdigest()[:24]
        return self.spawn(digest=digest, record={"signature": signature,
                          "company_ref": company_ref, "recovery_ref": recovery_ref},
                          company_ref=company_ref)


__all__ = ["InvestmentMemoLauncher"]
