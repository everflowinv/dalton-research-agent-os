"""S5: launch the ownership child, one at a time, named by the filing it reads.

The run is named by the accession rather than by the company and the day. That
is the difference that matters here: a company files a dozen Form 4s in a week
and each one is a separate document, so a ticket keyed by the day would collapse
them and a tick that fired twice would start a second child for a filing already
read. One accession, one ticket, one child.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .sec_ownership_core import (
    FORM13F_OPERATION,
    FORMS_BY_OPERATION,
    KIND_BY_OPERATION,
    OPERATIONS,
)

TICKET_PREFIX = "sec-ownership-run"
# The approved record's filename under the live state's governance directory,
# per operation. Its presence is what turns that operation on: a Core that has
# been approved for Form 4 and not for 13F reads Form 4 and reports the other
# as unapproved, rather than refusing to start.
GOVERNANCE_FILENAME_BY_OPERATION = {
    operation: f"{kind}-v1.json" for operation, kind in KIND_BY_OPERATION.items()
}


class SecOwnershipLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.sec_ownership_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "sec-ownership-runs"
    CHILD_MODULE = "dalton_core.sec_ownership_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_dir: str | Path | None = None,
        actor_ref: str = "automation:coverage-mission",
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_dir = (
            None if governance_dir is None
            else Path(governance_dir).expanduser().resolve()
        )
        self.actor_ref = actor_ref

    def governance_path(self, operation: str) -> Path | None:
        """The approved record for one operation, if this Core has one."""

        if self.governance_dir is None or operation not in OPERATIONS:
            return None
        path = self.governance_dir / GOVERNANCE_FILENAME_BY_OPERATION[operation]
        return path if path.is_file() else None

    def approved_operations(self) -> tuple[str, ...]:
        """Which of the four this Core may run at all, in the frozen order."""

        return tuple(
            operation for operation in OPERATIONS
            if self.governance_path(operation) is not None
        )

    @property
    def configured(self) -> bool:
        return bool(self.approved_operations())

    def _command(
        self, *, ticket_dir: Path, operation: str, company_ref: str, accession: str,
        form_type: str, issuer: str | None, holder_cik: str | None,
        quarter: str | None, filed_at: str | None, company_cusips: str | None,
        cover_file: str | None, prior_file: str | None,
        prior_cover_file: str | None, prior_accession: str | None,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path(operation)),
            "--operation", operation,
            "--company-ref", company_ref,
            "--accession", accession,
            "--form-type", form_type,
            "--actor-ref", self.actor_ref,
            "--allow-network",
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        for flag, value in (
            ("--issuer", issuer), ("--holder-cik", holder_cik),
            ("--quarter", quarter), ("--filed-at", filed_at),
            ("--company-cusips", company_cusips), ("--cover-file", cover_file),
            ("--prior-file", prior_file), ("--prior-cover-file", prior_cover_file),
            ("--prior-accession", prior_accession),
        ):
            if value:
                command.extend([flag, str(value)])
        return command

    def start(
        self,
        *,
        operation: str,
        company_ref: str,
        accession: str,
        form_type: str,
        issuer: str | None = None,
        holder_cik: str | None = None,
        quarter: str | None = None,
        filed_at: str | None = None,
        company_cusips: str | None = None,
        cover_file: str | None = None,
        prior_file: str | None = None,
        prior_cover_file: str | None = None,
        prior_accession: str | None = None,
    ) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise LaneChildRejected(f"{operation!r} is not an ownership operation")
        if self.governance_path(operation) is None:
            raise LaneChildRejected(
                f"an ownership run needs an approved {KIND_BY_OPERATION[operation]} record"
            )
        for name, value in (
            ("company_ref", company_ref), ("accession", accession),
            ("form_type", form_type),
        ):
            if not isinstance(value, str) or not value.strip():
                raise LaneChildRejected(f"an ownership run needs a {name}")
        # The same check the child makes, made here too, so a mismatch costs
        # nothing rather than a process.
        if form_type.strip() not in FORMS_BY_OPERATION[operation]:
            raise LaneChildRejected(
                f"form {form_type!r} is not read by {operation}"
            )
        if operation == FORM13F_OPERATION and not holder_cik:
            raise LaneChildRejected("a 13F run needs the filing manager's CIK")
        if operation != FORM13F_OPERATION and not issuer:
            raise LaneChildRejected(f"a {operation} run needs the issuer CIK")
        company_ref, accession = company_ref.strip(), accession.strip()
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{operation}|{company_ref}|{accession}"
            .encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref, "operation": operation,
                "accession": accession, "form_type": form_type.strip(),
                "issuer": issuer, "holder_cik": holder_cik, "quarter": quarter,
                "filed_at": filed_at,
                "governance_configured": self.governance_path(operation) is not None,
            },
            operation=operation, company_ref=company_ref, accession=accession,
            form_type=form_type.strip(), issuer=issuer, holder_cik=holder_cik,
            quarter=quarter, filed_at=filed_at, company_cusips=company_cusips,
            cover_file=cover_file, prior_file=prior_file,
            prior_cover_file=prior_cover_file, prior_accession=prior_accession,
        )


__all__ = [
    "GOVERNANCE_FILENAME_BY_OPERATION",
    "TICKET_PREFIX",
    "SecOwnershipLauncher",
]
