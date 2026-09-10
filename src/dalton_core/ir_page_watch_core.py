"""S5: what changed on a covered company's investor-relations page.

An IR page is where a company puts the things that are not filings: a new
customer logo, a leadership page that lost a name, an events calendar that
gained a date, a "we have signed" press release the wire did not pick up.  It
is primary and it is free, and nobody was watching it.

**Dalton does not fetch these pages.**  A changedetection.io instance already
runs on this machine on ``127.0.0.1:5055``; it fetches on its own schedule,
keeps its own snapshots and computes its own diffs.  This connector asks it,
over loopback, what changed.  That is why the transport is ``host_tool`` and
not ``public_https``: there is no upstream Dalton is being polite to, no
credential, and no copy of anybody's website in Core -- only a diff, its hash,
and the fetched page spooled as the artifact that diff was computed from.

**Which pages, decided in advance.**  A shared local tool watches whatever its
user last pointed it at.  A connector that read all of them would be a
connector whose scope is set by somebody else's afternoon.  So the watched IR
URLs are declared per company in
``deploy/phase9/p9-us-it-services-ir-pages-v1.json``, and a watch whose URL is
not in that file is *listed and never read*: listed, because an operator
should be able to see that the tool is watching something Dalton ignores;
never read, because a host nobody declared is a host nobody approved.

**Off when the tool is absent, and that is not a failure.**  A Core with no
changedetection instance reports ``unconfigured`` and does nothing.  Most Cores
will be that Core.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "ir-page-watch"
CONNECTOR_SLUG = "ir-page-watch"
SOURCE_REF = "source:ir-page-watch"

LIST_OPERATION = "list_watches"
DIFF_OPERATION = "get_watch_diff"
OPERATIONS: tuple[str, ...] = (LIST_OPERATION, DIFF_OPERATION)

LIST_KIND = "ir-page-watch-list-watches"
DIFF_KIND = "ir-page-watch-get-diff"
KIND_BY_OPERATION: dict[str, str] = {
    LIST_OPERATION: LIST_KIND,
    DIFF_OPERATION: DIFF_KIND,
}
OPERATION_BY_KIND: dict[str, str] = {
    kind: operation for operation, kind in KIND_BY_OPERATION.items()
}
CAPABILITY_BY_OPERATION: dict[str, str] = {
    operation: f"capability:dalton:connector:{kind}"
    for operation, kind in KIND_BY_OPERATION.items()
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
ADAPTER_REF = "adapter:dalton-core-changedetection-client:0.1"
SIDE_EFFECT = "read:host-tool-loopback"

# The one address this connector talks to. Loopback and a fixed port: a host
# tool that could be pointed at a remote address would be a network connector
# wearing a host tool's permissions.
DEFAULT_BASE_URL = "http://127.0.0.1:5055"
DEFAULT_TIMEOUT_SECONDS = 20.0

# What a reader should believe. Not ``primary_filing`` -- nothing here was
# filed with anybody, and a marketing page is edited without a revision
# history. It is the company speaking in its own voice on its own site, which
# is what ``management_direct`` already means in the event ledger.
IR_EVIDENCE_TIER = "management_direct"
WRITE_SCOPE = "observation"

DECLARATION_FILENAME = "p9-us-it-services-ir-pages-v1.json"
DECLARATION_SCHEMA_VERSION = "0.1"
MAX_DECLARED_PAGES = 60


class IrPageWatchError(RuntimeError):
    """The IR-page-watch identity, declaration or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise IrPageWatchError(f"ir-page-watch has no frozen {operation!r} operation")
    return operation


# -- identity ---------------------------------------------------------------


def ir_page_watch_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise IrPageWatchError(
            f"packaged ir-page-watch template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def ir_page_watch_source_hash() -> str:
    template, _ = ir_page_watch_contract(LIST_OPERATION)
    return content_hash(dict(template["source_identity"]))


def ir_page_watch_schema_hash(operation: str) -> str:
    _, contract = ir_page_watch_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def ir_page_watch_adapter_hash(operation: str) -> str:
    template, _ = ir_page_watch_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
        "adapter": ADAPTER_REF,
    })


def ir_page_watch_identity(operation: str) -> dict[str, Any]:
    template, contract = ir_page_watch_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": ir_page_watch_source_hash(),
        "schema_hash": ir_page_watch_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": ir_page_watch_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def ir_page_watch_permissions() -> dict[str, Any]:
    """A host tool that holds nothing and reaches no site.

    ``network: False`` for the same reason the crowd host tools declare it:
    the tool owns the network, not this process, and the only address this
    process speaks to is a loopback port on the machine it is already running
    on. No credential slot at all -- changedetection.io is unauthenticated on
    loopback. The only write is into the raw sink.

    Which *pages* may be read is deliberately not expressible here. The
    permission layer has no field for "this URL and no other", which is why the
    declaration file exists and why it is checked in the adapter rather than
    hoped for in a permission.
    """

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def ir_page_watch_fixture_hash() -> str:
    template, _ = ir_page_watch_contract(LIST_OPERATION)
    return template["fixture_manifest_hash"]


def ir_page_watch_output_schema(operation: str) -> dict[str, Any]:
    template, contract = ir_page_watch_contract(operation)
    ref = contract["output_schema_ref"]
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise IrPageWatchError(
        f"packaged ir-page-watch template has no {operation} output contract"
    )


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_ir_page_watch_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise IrPageWatchError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise IrPageWatchError("approved_by must be a human: principal")
    kind = KIND_BY_OPERATION[operation]
    capability_id = CAPABILITY_BY_OPERATION[operation]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{kind}:v{version}",
        "status": status,
        "capability_id": capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{kind}:v{version}",
        "approval_ref": f"approval:connector-governance:{kind}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{kind}:v{version}",
        "registry_revision_ref": f"{capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{kind}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": copy.deepcopy(ir_page_watch_permissions()),
        "expected_source_hash": ir_page_watch_source_hash(),
        "expected_schema_hash": ir_page_watch_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


# -- the declaration --------------------------------------------------------


def normalise_url(value: Any) -> str:
    """One URL, in the one shape two spellings of it both reduce to.

    changedetection.io stores whatever was typed into it, and a person types
    a trailing slash about half the time. Matching a declaration against a
    watch has to survive that, and nothing else about the URL is touched: the
    query string stays, because ``?tab=events`` is a different page.
    """

    if not isinstance(value, str) or not value.strip():
        raise IrPageWatchError("a watched page needs a URL")
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise IrPageWatchError(f"{value!r} is not an http(s) URL")
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc.lower()}{path}{query}"


def host_of(url: str) -> str:
    return urlsplit(normalise_url(url)).netloc


def load_ir_page_declaration(path: str | Path) -> dict[str, Any]:
    """The declared IR pages, keyed by their normalised URL.

    Refuses rather than warns on a malformed entry: a declaration is the only
    thing standing between this connector and every page the local tool
    happens to watch, and a half-read declaration is worse than none.
    """

    path = Path(path).expanduser()
    try:
        wire = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IrPageWatchError(f"no IR page declaration at {path}") from exc
    except ValueError as exc:
        raise IrPageWatchError(f"IR page declaration is not JSON: {exc}") from exc
    if not isinstance(wire, Mapping):
        raise IrPageWatchError("IR page declaration is not an object")
    if wire.get("schema_version") != DECLARATION_SCHEMA_VERSION:
        raise IrPageWatchError("unsupported IR page declaration schema_version")
    companies = wire.get("companies")
    if not isinstance(companies, list) or not companies:
        raise IrPageWatchError("IR page declaration lists no companies")
    pages: dict[str, dict[str, Any]] = {}
    for entry in companies:
        if not isinstance(entry, Mapping):
            raise IrPageWatchError("an IR page declaration entry is not an object")
        company_ref = entry.get("company_ref")
        ticker = entry.get("ticker")
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise IrPageWatchError("an IR page declaration entry has no company_ref")
        declared = entry.get("pages")
        if not isinstance(declared, list) or not declared:
            raise IrPageWatchError(f"{company_ref} declares no IR pages")
        for page in declared:
            if not isinstance(page, Mapping):
                raise IrPageWatchError(f"{company_ref} has a malformed page entry")
            url = normalise_url(page.get("url"))
            if url in pages:
                raise IrPageWatchError(f"{url} is declared twice")
            pages[url] = {
                "url": url,
                "host": host_of(url),
                "company_ref": company_ref.strip(),
                "ticker": None if not isinstance(ticker, str) else ticker.strip().upper(),
                "page_kind": str(page.get("page_kind") or "investor_relations"),
                "label": str(page.get("label") or ""),
            }
    if len(pages) > MAX_DECLARED_PAGES:
        raise IrPageWatchError(
            f"{len(pages)} declared IR pages exceeds the ceiling of {MAX_DECLARED_PAGES}"
        )
    return {
        "id": str(wire.get("id") or "ir-page-map"),
        "pages": pages,
        "hosts": frozenset(page["host"] for page in pages.values()),
        "declaration_hash": content_hash(
            {"pages": [pages[url] for url in sorted(pages)]}
        ),
    }


def declared_page(declaration: Mapping[str, Any], url: Any) -> dict[str, Any] | None:
    """The declaration entry for one watch's URL, or None if nobody declared it."""

    try:
        key = normalise_url(url)
    except IrPageWatchError:
        return None
    entry = declaration["pages"].get(key)
    return None if entry is None else dict(entry)


def host_is_declared(declaration: Mapping[str, Any], url: Any) -> bool:
    """Whether this URL's *host* was declared at all.

    Weaker than :func:`declared_page` on purpose and used for a different
    question: a diff is only ever read for a declared page, but an operator's
    "the tool is watching a host nobody approved" warning is about hosts.
    """

    try:
        return host_of(url) in declaration["hosts"]
    except IrPageWatchError:
        return False


def diff_hash(
    *,
    url: str,
    previous_snapshot_hash: str | None,
    current_snapshot_hash: str,
) -> str:
    """The name of one change: this page, from these bytes to those bytes.

    Not a function of when it was noticed. A watcher re-read tomorrow reports
    the same pair of snapshots, so the event ledger sees the same diff and
    answers ``duplicate`` -- which is the whole of "one event per (url, diff
    hash)".
    """

    return content_hash({
        "url": normalise_url(url),
        "previous_snapshot_hash": previous_snapshot_hash,
        "current_snapshot_hash": current_snapshot_hash,
    })


def line_change_counts(
    previous: str | None, current: str
) -> tuple[int, int]:
    """How many lines the page gained and lost, as a multiset difference.

    Not a real diff algorithm and not pretending to be one: the question a
    tick summary answers is "how much moved", and counting lines that are in
    one snapshot and not the other answers it without this module acquiring an
    opinion about line ordering.
    """

    from collections import Counter

    before = Counter(
        line.strip() for line in (previous or "").splitlines() if line.strip()
    )
    after = Counter(line.strip() for line in current.splitlines() if line.strip())
    added = sum((after - before).values())
    removed = sum((before - after).values())
    return added, removed


def excerpt_of(previous: str | None, current: str, *, limit: int = 600) -> str | None:
    """The first lines that are new, for a person reading a tick summary."""

    from collections import Counter

    before = Counter(
        line.strip() for line in (previous or "").splitlines() if line.strip()
    )
    new_lines: list[str] = []
    seen: Counter = Counter()
    for line in current.splitlines():
        text = line.strip()
        if not text:
            continue
        seen[text] += 1
        if seen[text] > before.get(text, 0):
            new_lines.append(text)
    if not new_lines:
        return None
    joined = " / ".join(new_lines)
    return joined[:limit]


def event_key(url: str, digest: str) -> str:
    """One (url, diff hash) pair, named once."""

    return "ir-page-change:" + content_hash({
        "url": normalise_url(url), "diff_hash": digest,
    })[:32]


def ir_page_change_payload(
    diff: Mapping[str, Any], *, artifact_hash: str, invocation_ref: str
) -> dict[str, Any]:
    """The typed ``ir_page_change`` payload one diff produces."""

    return {
        "watch_ref": diff["watch_id"],
        "url": diff["url"],
        "host": diff["host"],
        "diff_hash": diff["diff_hash"],
        "previous_snapshot_hash": diff["previous_snapshot_hash"],
        "current_snapshot_hash": diff["current_snapshot_hash"],
        "changed_at": diff["changed_at"],
        "added_line_count": diff["added_line_count"],
        "removed_line_count": diff["removed_line_count"],
        "title": diff["title"],
        "excerpt": diff["excerpt"],
        "artifact_hash": artifact_hash,
        "invocation_ref": invocation_ref,
        "event_key": event_key(diff["url"], diff["diff_hash"]),
    }


def declared_watches(
    declaration: Mapping[str, Any], watches: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Only the watches somebody declared, in declaration order."""

    order = {url: index for index, url in enumerate(sorted(declaration["pages"]))}
    found = [dict(watch) for watch in watches if watch.get("declared")]
    found.sort(key=lambda watch: order.get(normalise_url(watch["url"]), 1 << 30))
    return found


# -- one sweep --------------------------------------------------------------

MAX_DIFFS_PER_SWEEP = 10
GOVERNANCE_FILENAME_BY_OPERATION = {
    operation: f"{kind}-v1.json" for operation, kind in KIND_BY_OPERATION.items()
}


def changed_at_rfc3339(value: Any, *, fallback: datetime) -> str:
    """When changedetection says the page moved, as a moment with a zone on it.

    The tool stamps epoch seconds. A stamp it did not give -- a watch that has
    never changed, a field a version of the tool does not serve -- becomes the
    sweep's own clock rather than a guess at a past instant, because an event
    ledger whose ordering is invented cannot answer "what did we know when".
    """

    text = str(value or "").strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc).isoformat(
            timespec="microseconds"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if parsed.tzinfo is None:
        return fallback.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def sweep_ir_pages(
    *,
    store: Any,
    connectors: Any,
    observability: Any,
    spool: Any,
    declaration_path: Any,
    governance_dir: Any,
    state_dir: Any,
    clock: Callable[[], datetime] | None = None,
    max_diffs: int = MAX_DIFFS_PER_SWEEP,
) -> dict[str, Any]:
    """List the watches, read the declared ones that moved, and report.

    Two governed operations and therefore two approvals: listing what the tool
    holds and reading one page's diff are different permissions, and a Core
    approved for the first is not thereby approved for the second.

    Everything here is bounded. At most :data:`MAX_DIFFS_PER_SWEEP` pages are
    read in one sweep, because a tool that was left paused for a month will
    report ten declared pages as changed at once and the tick that notices
    should not become the tick that runs ten children.
    """

    from pathlib import Path as _Path

    from .connector_governance import ConnectorGovernance
    from .host_tool_runner import HostToolRunner

    now = (clock or (lambda: datetime.now(timezone.utc)))()
    governance_dir = _Path(governance_dir).expanduser()
    declaration_path = _Path(declaration_path).expanduser()
    records: dict[str, Any] = {}
    for operation in OPERATIONS:
        path = governance_dir / GOVERNANCE_FILENAME_BY_OPERATION[operation]
        if not path.is_file():
            return {
                "status": "unconfigured",
                "reason": (
                    f"no approved {KIND_BY_OPERATION[operation]} record at {path}; "
                    "the watcher needs both of its approvals"
                ),
                "watch_count": 0, "undeclared_count": 0, "changes": [],
            }
        record = ConnectorGovernance.load(path)
        if not record.approved:
            return {
                "status": "unconfigured",
                "reason": f"the {KIND_BY_OPERATION[operation]} record is not approved",
                "watch_count": 0, "undeclared_count": 0, "changes": [],
            }
        records[operation] = record

    declaration = load_ir_page_declaration(declaration_path)

    def runner(operation: str) -> Any:
        def command(
            parameters: Mapping[str, Any], output_dir: Any, context: Mapping[str, str]
        ) -> list[str]:
            import sys as _sys

            argv = [
                _sys.executable, "-m", "dalton_core.ir_page_watch_cli",
                "--operation", operation,
                "--declaration", str(declaration_path),
                "--output-dir", str(output_dir),
            ]
            for flag, key in (("--watch-id", "watch_id"), ("--since", "since")):
                if parameters.get(key):
                    argv.extend([flag, str(parameters[key])])
            return argv

        return HostToolRunner(
            store=store, connectors=connectors, observability=observability,
            spool=spool, template_key=TEMPLATE_KEY,
            identity=ir_page_watch_identity(operation),
            governance=records[operation], command=command,
            connector_slug=CONNECTOR_SLUG,
        )

    work_ref = "work-order:ir-page-watch:" + content_hash({
        "declaration": declaration["declaration_hash"],
        "day": now.astimezone(timezone.utc).date().isoformat(),
    })[:24]
    listing = runner(LIST_OPERATION).run(
        parameters={"since": now.astimezone(timezone.utc).date().isoformat()},
        work_ref=work_ref,
    )
    watches = listing.observation["watches"]
    changes: list[dict[str, Any]] = []
    read = 0
    for watch in declared_watches(declaration, watches):
        if watch["paused"] or not watch["last_changed"] or watch["snapshot_count"] < 2:
            continue
        if read >= max_diffs:
            break
        read += 1
        receipt = runner(DIFF_OPERATION).run(
            parameters={"watch_id": watch["watch_id"]},
            work_ref=work_ref,
            output_dir=_Path(state_dir) / "ir-page-snapshots",
        )
        diff = receipt.observation
        changes.append({
            "company_ref": diff["company_ref"],
            "occurred_at": changed_at_rfc3339(diff["changed_at"], fallback=now),
            "source_refs": [
                f"ir-watch:{diff['watch_id']}",
                f"raw-sink:{receipt.raw_response_hash}",
            ],
            "payload": ir_page_change_payload(
                diff,
                artifact_hash=receipt.raw_response_hash,
                invocation_ref=receipt.connector_invocation_ref,
            ),
        })
    return {
        "status": "watched",
        "reason": None,
        "watch_count": len(watches),
        "undeclared_count": listing.observation["undeclared_count"],
        "changes": changes,
    }


__all__ = [
    "ADAPTER_REF",
    "GOVERNANCE_FILENAME_BY_OPERATION",
    "MAX_DIFFS_PER_SWEEP",
    "changed_at_rfc3339",
    "sweep_ir_pages",
    "CAPABILITY_BY_OPERATION",
    "CONNECTOR_SLUG",
    "DECLARATION_FILENAME",
    "DECLARATION_SCHEMA_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIFF_KIND",
    "DIFF_OPERATION",
    "SIDE_EFFECT",
    "GOVERNANCE_SCHEMA_VERSION",
    "IR_EVIDENCE_TIER",
    "KIND_BY_OPERATION",
    "LIST_KIND",
    "LIST_OPERATION",
    "MAX_DECLARED_PAGES",
    "OPERATIONS",
    "OPERATION_BY_KIND",
    "SOURCE_REF",
    "TEMPLATE_KEY",
    "WRITE_SCOPE",
    "IrPageWatchError",
    "build_ir_page_watch_governance_record",
    "declared_page",
    "declared_watches",
    "diff_hash",
    "event_key",
    "excerpt_of",
    "host_is_declared",
    "host_of",
    "ir_page_change_payload",
    "ir_page_watch_adapter_hash",
    "ir_page_watch_contract",
    "ir_page_watch_fixture_hash",
    "ir_page_watch_identity",
    "ir_page_watch_output_schema",
    "ir_page_watch_permissions",
    "ir_page_watch_schema_hash",
    "ir_page_watch_source_hash",
    "line_change_counts",
    "load_ir_page_declaration",
    "normalise_url",
]
