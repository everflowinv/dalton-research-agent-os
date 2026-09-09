"""S1: writer-owned launchers for the two human / vendor feed children.

Both feeds run out of process, one child at a time, against an owner-approved
governance record on disk. That machinery -- a secure ticket directory, an
owner-only ticket file, a single slot, and a ``status`` that answers
``orphaned`` rather than guessing success from a stray summary -- is
``LaneChildLauncher``, so it is inherited rather than copied. The two older
acquisition launchers predate that class and each carry their own version of
it; these do not.

What a feed launcher adds on top is the part the review path needs: a settled
ticket can be turned back into a *verified* manifest. ``read_completed_manifest``
opens the three files the child wrote with ``O_NOFOLLOW``, refuses anything
that is not a bounded owner-only regular file, and then requires the ticket,
the summary and the manifest to agree about which document was acquired and
which manifest describes it. Disagreement is a refusal, not a warning: the
whole value of the ticket directory is that it is the durable record of which
launch produced which bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .company_wiki_core import (
    GET_OPERATION as WIKI_GET_OPERATION,
    LIST_OPERATION as WIKI_LIST_OPERATION,
    SOURCE_REF as COMPANY_WIKI_SOURCE_REF,
)
from .connector_governance import ConnectorGovernance
from .feed_acquisition import validate_feed_acquisition_manifest
from .lane_child_launcher import (
    TICKET_SCHEMA_VERSION,
    LaneChildLauncher,
    LaneChildRejected,
    secure_dir,
    wire_time,
    write_owner_only,
)
from .sales_notes_core import (
    GET_OPERATION as NOTES_GET_OPERATION,
    LIST_OPERATION as NOTES_LIST_OPERATION,
    SOURCE_REF as SALES_NOTES_SOURCE_REF,
)
from .store import canonical_json

MAX_TICKET_FILE_BYTES = 2_000_000
_HUMAN_PREFIX = "human:"
_AUTOMATION_PREFIX = "automation:"


class FeedLaunchRejected(LaneChildRejected):
    """The feed run was refused before any process started."""


class FeedChildLauncher(LaneChildLauncher):
    """One feed, one slot, tickets a restart cannot confuse."""

    SOURCE_REF = ""
    LIST_OPERATION = ""
    GET_OPERATION = ""
    CHILD_MODULE = ""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_paths: dict[str, str | Path],
        spool_dir: str | Path | None = None,
        python_executable: str | None = None,
        clock: Any = None,
    ) -> None:
        super().__init__(
            state_dir=state_dir, python_executable=python_executable, clock=clock
        )
        missing = {self.LIST_OPERATION, self.GET_OPERATION} - set(governance_paths)
        if missing:
            raise FeedLaunchRejected(
                f"feed launcher needs a governance record per operation; missing {sorted(missing)}"
            )
        self.governance_paths = {
            operation: Path(path).expanduser().resolve()
            for operation, path in governance_paths.items()
        }
        self.spool_dir = (
            None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        )

    # -- governance --------------------------------------------------------

    def load_governance(self, operation: str) -> ConnectorGovernance:
        """The approved record for one operation, or the run does not start.

        Checked here as well as in the child. The child is the authority --
        it re-checks the packaged hashes too -- but a launcher that spawns a
        process it already knows will refuse has spent a slot for nothing.
        """

        try:
            path = self.governance_paths[operation]
        except KeyError as exc:
            raise FeedLaunchRejected(f"no governance record for {operation}") from exc
        try:
            governance = ConnectorGovernance.load(path)
        except FileNotFoundError as exc:
            raise FeedLaunchRejected(
                f"{operation} governance record is missing; owner approval is required"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - an unusable record is a refusal
            raise FeedLaunchRejected(f"{operation} governance record is invalid: {exc}") from exc
        if not governance.approved:
            raise FeedLaunchRejected(
                f"{operation} governance record is not approved; owner approval is required"
            )
        if not governance.approved_by.startswith(_HUMAN_PREFIX):
            raise FeedLaunchRejected(f"{operation} governance record must be approved by a human")
        return governance

    # -- launching ---------------------------------------------------------

    def _feed_args(self) -> list[str]:
        raise NotImplementedError

    def _command(self, *, ticket_dir: Path, operation: str, **kwargs: Any) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_paths[operation]),
            "--operation", operation,
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]
        command += self._feed_args()
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        for name, value in kwargs.items():
            if value is None:
                continue
            flag = "--" + name.replace("_", "-")
            if isinstance(value, (list, tuple)):
                for item in value:
                    command += [flag, str(item)]
            else:
                command += [flag, str(value)]
        return command

    def _digest(self, identity: dict[str, Any]) -> str:
        return hashlib.sha256(
            canonical_json(identity).encode("utf-8")
        ).hexdigest()[:24]

    def start_enumeration(self, *, caller_ref: str, **parameters: Any) -> dict[str, Any]:
        """Enumerate the feed for one bounded window."""

        self._require_caller(caller_ref)
        governance = self.load_governance(self.LIST_OPERATION)
        started_at = self.clock().isoformat(timespec="microseconds")
        digest = self._digest({
            "operation": self.LIST_OPERATION, "parameters": parameters,
            "caller_ref": caller_ref, "started_at": started_at,
            "governance_hash": governance.content_hash,
        })
        return self.spawn(
            digest=digest,
            record={
                "source_ref": self.SOURCE_REF,
                "operation": self.LIST_OPERATION,
                "document_ref": None,
                "actor_ref": caller_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
            },
            operation=self.LIST_OPERATION,
            **parameters,
        )

    def start_bounded_probe(
        self, *, document_ref: str, caller_ref: str, **parameters: Any
    ) -> dict[str, Any]:
        """Acquire one discovered feed document for the mission automation."""

        self._require_caller(caller_ref)
        if not isinstance(document_ref, str) or not document_ref.strip():
            raise FeedLaunchRejected("document_ref is required")
        governance = self.load_governance(self.GET_OPERATION)
        started_at = self.clock().isoformat(timespec="microseconds")
        digest = self._digest({
            "operation": self.GET_OPERATION, "document_ref": document_ref,
            "caller_ref": caller_ref, "started_at": started_at,
            "governance_hash": governance.content_hash,
        })
        return self.spawn(
            digest=digest,
            record={
                "source_ref": self.SOURCE_REF,
                "operation": self.GET_OPERATION,
                "document_ref": document_ref,
                "actor_ref": caller_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
            },
            operation=self.GET_OPERATION,
            **self._document_args(document_ref),
            **parameters,
        )

    def _document_args(self, document_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    def child_command(
        self, *, operation: str, output_dir: str | Path,
        context: Mapping[str, str] | None = None, **parameters: Any
    ) -> list[str]:
        """The argv the host-tool runner executes for one operation.

        The same builder the launcher's own ``spawn`` uses, plus
        ``--emit-wire``: the runner treats stdout as the raw response, so the
        child must print the closed observation wire and nothing else.

        On the document operation the invocation the runner registered is
        passed down, so the manifest the child writes names it from the start.
        The listing operation writes no manifest and is given nothing.
        """

        extra: dict[str, Any] = {}
        if context and operation == self.GET_OPERATION:
            extra = {
                "connector_invocation_ref": context["connector_invocation_ref"],
                "connector_invocation_hash": context["connector_invocation_hash"],
            }
        return self._command(
            ticket_dir=Path(output_dir), operation=operation, **parameters, **extra
        ) + ["--emit-wire"]

    # -- runs this launcher did not spawn ---------------------------------

    def prepare_run(self, *, digest: str, record: Mapping[str, Any]) -> tuple[str, Path]:
        """A ticket directory for a run the host-tool runner will execute.

        The connector runner owns the process and the authority chain; the
        launcher owns tickets, because the ticket directory is what the review
        path searches to find which run produced a document's bytes. Splitting
        it that way keeps one run: the child writes its manifest here, and the
        same child's stdout is what the runner records as the raw response.
        """

        if not re.fullmatch(r"[0-9a-f]{24}", digest or ""):
            raise FeedLaunchRejected("ticket digest must be 24 hex characters")
        ticket_id = f"{self.TICKET_PREFIX}:{digest}"
        ticket_dir = secure_dir(self.tickets_dir / digest)
        write_owner_only(ticket_dir / "ticket.json", {
            "schema_version": TICKET_SCHEMA_VERSION,
            "id": ticket_id,
            **dict(record),
            "started_at": wire_time(self.clock()),
            "pid": os.getpid(),
            "status": "running",
            "exit_code": None,
            "completed_at": None,
        })
        return ticket_id, ticket_dir

    def settle_run(self, ticket_id: str, *, status: str, exit_code: int | None = None,
                   failure_reason: str | None = None) -> dict[str, Any]:
        """Close a prepared ticket. ``running`` is never a settled answer."""

        if status not in {"succeeded", "failed"}:
            raise FeedLaunchRejected("a settled feed run is succeeded or failed")
        path = self._ticket_path(ticket_id)
        record = self._read_owner_only(path)
        record.update({
            "status": status,
            "exit_code": exit_code,
            "completed_at": wire_time(self.clock()),
            "failure_reason": failure_reason,
        })
        write_owner_only(path, record)
        return record

    @staticmethod
    def _require_caller(caller_ref: Any) -> None:
        if not isinstance(caller_ref, str) or not (
            caller_ref.startswith(_AUTOMATION_PREFIX) or caller_ref.startswith(_HUMAN_PREFIX)
        ):
            raise FeedLaunchRejected("feed run must name a human or automation principal")

    # -- reading a settled run --------------------------------------------

    def _read_owner_only(self, path: Path) -> dict[str, Any]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                        or info.st_uid != os.getuid()
                        or info.st_size > MAX_TICKET_FILE_BYTES):
                    raise FeedLaunchRejected("feed run file must be bounded and owner-only")
                value = json.loads(handle.read(MAX_TICKET_FILE_BYTES + 1).decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise FeedLaunchRejected("completed feed run files are unavailable") from exc
        if not isinstance(value, dict):
            raise FeedLaunchRejected("completed feed run file is not an object")
        return value

    def read_completed_manifest(self, ticket_ref: str, document_ref: str) -> dict[str, Any]:
        """Read a settled acquisition without polling, spawning or mutating it."""

        if not isinstance(ticket_ref, str) or self._ticket_re.fullmatch(ticket_ref) is None:
            raise FeedLaunchRejected("invalid feed ticket reference")
        directory = self._ticket_path(ticket_ref).parent
        if directory.is_symlink() or directory.parent.is_symlink():
            raise FeedLaunchRejected("feed ticket directory cannot be a symlink")
        ticket = self._read_owner_only(directory / "ticket.json")
        summary = self._read_owner_only(directory / "summary.json")
        manifest = self._read_owner_only(directory / "manifest.json")
        if (ticket.get("id") != ticket_ref
                or ticket.get("status") != "succeeded"
                or ticket.get("document_ref") != document_ref
                or ticket.get("source_ref") != self.SOURCE_REF
                or summary.get("status") != "succeeded"
                or summary.get("document_ref") != document_ref
                or summary.get("source_ref") != self.SOURCE_REF
                or summary.get("manifest_ref") != manifest.get("id")
                or summary.get("manifest_hash") != manifest.get("content_hash")
                or manifest.get("document_ref") != document_ref):
            raise FeedLaunchRejected("ticket, summary and manifest disagree")
        return validate_feed_acquisition_manifest(manifest)

    def locate_completed_manifest(self, document_ref: str) -> dict[str, Any]:
        """The newest succeeded acquisition of this document, read the same way.

        Rows acquired before a ticket ref was recorded still have to be
        readable; the ticket directory is the durable record, so it is
        searched rather than trusted from the ledger.
        """

        if not isinstance(document_ref, str) or not document_ref:
            raise FeedLaunchRejected("document_ref is required")
        best: tuple[str, str] | None = None
        for ticket_path in self.tickets_dir.glob("*/ticket.json"):
            try:
                record = json.loads(ticket_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("status") != "succeeded" \
                    or record.get("document_ref") != document_ref:
                continue
            key = (str(record.get("started_at", "")), str(record.get("id", "")))
            if best is None or key > best:
                best = key
        if best is None:
            raise FeedLaunchRejected("no completed acquisition ticket for this document")
        return self.read_completed_manifest(best[1], document_ref)

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None


class SalesNotesFeedLauncher(FeedChildLauncher):
    """The sell-side note feed: one directory of digest runs."""

    SOURCE_REF = SALES_NOTES_SOURCE_REF
    LIST_OPERATION = NOTES_LIST_OPERATION
    GET_OPERATION = NOTES_GET_OPERATION
    TICKET_PREFIX = "sales-notes-run"
    TICKETS_DIRNAME = "feed-acquisitions-sales-notes"
    CHILD_MODULE = "dalton_core.sales_notes_cli"

    def __init__(self, *, digest_dir: str | Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.digest_dir = Path(digest_dir).expanduser().resolve()

    def _feed_args(self) -> list[str]:
        return ["--digest-dir", str(self.digest_dir)]

    def _document_args(self, document_ref: str) -> dict[str, Any]:
        return {"note_id": document_ref}


class CompanyWikiFeedLauncher(FeedChildLauncher):
    """The human wiki: a SQLite index beside a markdown corpus."""

    SOURCE_REF = COMPANY_WIKI_SOURCE_REF
    LIST_OPERATION = WIKI_LIST_OPERATION
    GET_OPERATION = WIKI_GET_OPERATION
    TICKET_PREFIX = "company-wiki-run"
    TICKETS_DIRNAME = "feed-acquisitions-company-wiki"
    CHILD_MODULE = "dalton_core.company_wiki_cli"

    def __init__(self, *, index_db: str | Path, corpus_root: str | Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.index_db = Path(index_db).expanduser().resolve()
        self.corpus_root = Path(corpus_root).expanduser().resolve()

    def _feed_args(self) -> list[str]:
        return [
            "--index-db", str(self.index_db),
            "--corpus-root", str(self.corpus_root),
        ]

    def _document_args(self, document_ref: str) -> dict[str, Any]:
        return {"document_id": document_ref}


__all__ = [
    "CompanyWikiFeedLauncher",
    "FeedChildLauncher",
    "FeedLaunchRejected",
    "MAX_TICKET_FILE_BYTES",
    "SalesNotesFeedLauncher",
    "secure_dir",
]
