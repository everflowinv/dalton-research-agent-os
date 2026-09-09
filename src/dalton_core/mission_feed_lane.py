"""S1: turning two local feeds into mission discoveries and acquired documents.

The mission already has a shape for "a source was searched, here is what it
holds, acquire the ones we do not have": a source discovery record binding a
connector invocation, a row per discovered document, a bounded acquisition per
tick, and a review row when the bytes land. AlphaEngine, web search and the
SEC filings index all use it. These two feeds use the same one -- they are not
a second queue with its own rules.

**Attribution is where a local feed differs, and it differs in two ways.**

A sell-side note carries a subject line and nothing else at discovery time;
the body only exists after acquisition. So a note is attributed from what its
headers actually say. Notes about a covered company are queued for that
company. Notes about the macro -- rates, the Korea open, an index rebalance --
match nobody, and this lane does not queue them. That is the owner's rule
applied honestly: an industry document has no company tag, and inventing one
would fill a per-company queue with documents nobody should pay to read.

A wiki document is easier and stricter: a person already filed it under a
company or a sector, so the corpus's own tags are the attribution. A sector
note arrives with an empty company list and keeps it.

**What this module does not do.** It does not write the connector authority a
source discovery has to bind. ``record_source_discovery`` requires a real
``ConnectorInvocation`` and ``SourceEnvelope`` in Core, produced by the runner
for the transport in question -- and the host-tool runner does not exist yet.
So the recording step takes a receipt provider, and without one it refuses
with a reason rather than writing a discovery that binds nothing. Everything
downstream of a recorded discovery -- selection, the bounded acquisition
batch, settlement, the review row -- is here and is what the tick runs.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .company_wiki_core import SOURCE_REF as COMPANY_WIKI_SOURCE_REF
from .document_subject import COMPANY_NAMES
from .sales_notes_core import (
    DOCUMENT_REF_PREFIX as SALES_NOTE_REF_PREFIX,
    SOURCE_REF as SALES_NOTES_SOURCE_REF,
)
from .company_wiki_core import DOCUMENT_REF_PREFIX as WIKI_DOC_REF_PREFIX
from .store import content_hash

# The two rows `coverage_mission.DISCOVERY_SOURCES` needs before either feed
# can record a discovery. Declared here so the integration is one merge of a
# table this module already owns the truth for, and so the tests can bind the
# authority to exactly what the integrator will install.
FEED_DISCOVERY_SOURCES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    SALES_NOTES_SOURCE_REF: MappingProxyType({
        "connector_source_ref": SALES_NOTES_SOURCE_REF,
        "operation": "list_notes",
        "document_ref_prefix": SALES_NOTE_REF_PREFIX,
    }),
    COMPANY_WIKI_SOURCE_REF: MappingProxyType({
        "connector_source_ref": COMPANY_WIKI_SOURCE_REF,
        "operation": "list_documents",
        "document_ref_prefix": WIKI_DOC_REF_PREFIX,
    }),
})

SALES_NOTE_SPEC_REF = "sales-note"
WIKI_SPEC_PREFIX = "wiki-"
DISCOVERY_SCOPE = "source_discovery"
# Both feeds read local files, so a tick can afford a real batch; the bound is
# here to keep one tick finite, not to ration an upstream.
ACQUISITIONS_PER_TICK = 8
ACQUISITION_WAIT_SECONDS = 30.0
TICK_BUDGET_SECONDS = 20.0
ACQUISITION_RETRY_INTERVAL = timedelta(hours=1)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FeedLaneError(RuntimeError):
    """The feed lane cannot run as configured."""


class FeedLaneRejected(ValueError):
    """The feed lane refused a request before doing anything."""


# -- discovery parameters ----------------------------------------------


def feed_discovery_parameters(
    *, terms: str, since: str, as_of: date | str
) -> dict[str, Any]:
    """The closed ``{query, date_after, date_before}`` a discovery record takes.

    Deliberately the shape the mission authority already validates for a
    windowed search rather than a fourth one: a local feed asked for "these
    words, in this window" is the same request a web search is, and a new
    parameter shape would be a new branch in a table the feed does not own.
    """

    if not isinstance(terms, str) or not terms.strip():
        raise FeedLaneRejected("discovery terms are required")
    if not isinstance(since, str) or _DATE_RE.fullmatch(since) is None:
        raise FeedLaneRejected("since must be a YYYY-MM-DD date")
    before = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
    if _DATE_RE.fullmatch(before) is None:
        raise FeedLaneRejected("as_of must be a YYYY-MM-DD date")
    if before < since:
        raise FeedLaneRejected("discovery window ends before it starts")
    return {"query": terms.strip(), "date_after": since, "date_before": before}


def feed_query_hash(source_ref: str, parameters: Mapping[str, Any]) -> str:
    """Bind the query to the feed it was asked of, the way a search spec does."""

    return content_hash({"source_ref": source_ref, "parameters": dict(parameters)})


# -- attribution --------------------------------------------------------


def _universe_terms(universe: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Company ref and ticker for each covered name, refusing an unknown one.

    A ticker the name tables do not know still "matches" itself -- a subject
    line saying ZZZZ names ZZZZ -- and that is the trap: a subject line almost
    never says the ticker, so such a company would be silently attributed
    nothing at all for the whole run. It fails at the top instead, where the
    fix is one row in ``document_subject.COMPANY_NAMES``.
    """

    entries: list[tuple[str, str]] = []
    for item in universe:
        ticker = str(item["ticker"]).strip()
        if ticker not in COMPANY_NAMES:
            raise FeedLaneRejected(
                f"{ticker} has no company names to match a subject line against"
            )
        entries.append((str(item["company_ref"]), ticker))
    return entries


def attribute_notes(
    notes: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Which covered company each note's headers name, if any.

    Only the subject line is read, because at discovery time it is the only
    text there is. That is a real limit and it is stated rather than papered
    over: a note whose subject says "Global Rates Weekly" is not evidence
    about Accenture even if Accenture appears in paragraph nine, and this lane
    will not learn otherwise until the body is acquired for some other reason.
    """

    from .document_subject import document_names_subject

    entries = _universe_terms(universe)
    by_company: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for note in notes:
        subject = str(note.get("subject") or "")
        matched = False
        for company_ref, ticker in entries:
            if document_names_subject(subject, ticker)["names_subject"]:
                by_company.setdefault(company_ref, []).append(note["note_id"])
                matched = True
        if not matched:
            unattributed.append(note["note_id"])
    return {
        "by_company": {ref: sorted(dict.fromkeys(refs)) for ref, refs in sorted(by_company.items())},
        "unattributed": sorted(dict.fromkeys(unattributed)),
    }


def attribute_wiki_documents(
    documents: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The corpus's own company tags, intersected with the mission universe.

    No text matching. A person filed the document under a ticker or under a
    sector, and that filing is a better attribution than anything a regex over
    the prose would produce. A sector note has no company tag; it stays
    unattributed and is not queued.
    """

    ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"]) for item in universe}
    by_company: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for document in documents:
        tags = [tag for tag in document.get("company_tags", ()) if tag in ticker_to_ref]
        if not tags:
            unattributed.append(document["document_id"])
            continue
        for tag in tags:
            by_company.setdefault(ticker_to_ref[tag], []).append(document["document_id"])
    return {
        "by_company": {ref: sorted(dict.fromkeys(refs)) for ref, refs in sorted(by_company.items())},
        "unattributed": sorted(dict.fromkeys(unattributed)),
    }


def wiki_spec_ref(doc_type_key: str) -> str:
    """One spec per document kind, because the kind is what sets the tier.

    Kept out of ``document_figure_grade.GRADE_BY_SPEC`` on purpose: none of
    these are a filed statement or a transcript, so no figure should ever be
    read out of one. A spec with no grade is read for prose and nothing else,
    which is exactly right for an expert call or a broker note.
    """

    key = str(doc_type_key or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        raise FeedLaneRejected("wiki document kind is not a usable spec key")
    return WIKI_SPEC_PREFIX + key.replace("_", "-")


# -- the lane -----------------------------------------------------------


class FeedDiscoveryCoordinator:
    """One feed, one mission: enumerate, attribute, record, acquire, settle.

    ``missions`` is a ``CoverageMissionAuthority``. ``launcher`` is one of the
    feed launchers. ``receipts`` is the seam this lane cannot close on its own:
    a callable that runs the enumeration through the connector runner and
    returns the invocation and envelope refs a discovery record must bind.
    Until the host-tool runner exists there is nothing to pass, and recording
    refuses rather than writing a discovery bound to nothing.
    """

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        source_ref: str,
        companies: Mapping[str, str],
        automation_principal: str = "automation:coverage-mission",
        receipts: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        acquisitions_per_tick: int = ACQUISITIONS_PER_TICK,
        acquisition_wait_seconds: float = ACQUISITION_WAIT_SECONDS,
    ) -> None:
        if source_ref not in FEED_DISCOVERY_SOURCES:
            raise FeedLaneRejected(f"{source_ref} is not a feed discovery source")
        self.missions = missions
        self.launcher = launcher
        self.source_ref = source_ref
        self.companies = dict(companies)
        self.automation_principal = automation_principal
        self.receipts = receipts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.acquisitions_per_tick = max(1, int(acquisitions_per_tick))
        self.acquisition_wait_seconds = float(acquisition_wait_seconds)

    # -- grants ---------------------------------------------------------

    def authorize(self, *, company_ref: str, requested_by: str | None = None) -> dict[str, Any]:
        """The mission's own grant check, unchanged.

        This is the check that refuses a feed the mission has not marked
        ``connected`` and an automation the mission has not granted both
        ``source_discovery`` and ``observation``. It is not re-implemented
        here; it is called.
        """

        return self.missions.authorize_source_discovery(
            company_ref=company_ref,
            source_ref=self.source_ref,
            requested_by=requested_by or self.automation_principal,
        )

    # -- enumeration ----------------------------------------------------

    def enumerate(self, *, caller_ref: str | None = None, **parameters: Any) -> dict[str, Any]:
        """Run the governed list child once and return its observation.

        The enumeration goes through the child, not through an in-process
        read, because that is where the governance record is checked and the
        raw bytes are spooled. A lane that read the directory itself would be
        a second, ungoverned way to reach the same files.
        """

        ticket = self.launcher.start_enumeration(
            caller_ref=caller_ref or self.automation_principal, **parameters
        )
        self.launcher.wait(timeout=self.acquisition_wait_seconds)
        settled = self.launcher.status(ticket["id"])
        summary = settled.get("summary")
        if settled["status"] != "succeeded" or not isinstance(summary, Mapping):
            reason = (summary or {}).get("failure_reason") if isinstance(summary, Mapping) else None
            raise FeedLaneError(
                f"{self.source_ref} enumeration {settled['status']}: {reason or 'no summary'}"
            )
        return dict(summary["observation"])

    def attribute(
        self, observation: Mapping[str, Any], universe: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return attribute_notes(observation["notes"], universe)
        return attribute_wiki_documents(observation["documents"], universe)

    def spec_refs(self, observation: Mapping[str, Any]) -> dict[str, str]:
        """Document ref to spec ref, which is what carries the evidence tier."""

        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return {note["note_id"]: SALES_NOTE_SPEC_REF for note in observation["notes"]}
        return {
            document["document_id"]: wiki_spec_ref(document["doc_type_key"])
            for document in observation["documents"]
        }

    # -- recording ------------------------------------------------------

    def record_discoveries(
        self,
        *,
        observation: Mapping[str, Any],
        universe: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, Any],
        discovery_plan_ref: str,
        discovery_plan_hash: str,
        requested_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """One discovery record per (company, spec) the enumeration attributed.

        Documents already in the mission's queue are partitioned out as
        in-authority rather than re-queued, so a second run over a feed that
        grew by one note discovers one note.
        """

        if self.receipts is None:
            raise FeedLaneError(
                "recording a feed discovery needs a connector receipt; the host-tool "
                "runner for this feed is not installed"
            )
        attribution = self.attribute(observation, universe)
        specs = self.spec_refs(observation)
        records: list[dict[str, Any]] = []
        for company_ref, document_refs in attribution["by_company"].items():
            authorization = self.authorize(
                company_ref=company_ref, requested_by=requested_by
            )
            known = {
                row["document_ref"]
                for row in self.missions.discovered_documents(
                    authorization["mission_version_ref"], company_ref=company_ref, limit=500
                )
            }
            by_spec: dict[str, list[str]] = {}
            for ref in document_refs:
                by_spec.setdefault(specs[ref], []).append(ref)
            for spec_ref, refs in sorted(by_spec.items()):
                receipt = self.receipts(
                    FEED_DISCOVERY_SOURCES[self.source_ref]["operation"], parameters
                )
                if list(receipt["document_refs"]) != refs:
                    raise FeedLaneError(
                        "the receipt's records are not the documents this spec attributed"
                    )
                records.append(self.missions.record_source_discovery(
                    authorization=authorization,
                    discovery_plan_ref=discovery_plan_ref,
                    discovery_plan_hash=discovery_plan_hash,
                    spec_ref=spec_ref,
                    query_hash=feed_query_hash(self.source_ref, parameters),
                    parameters=dict(parameters),
                    connector_invocation_ref=receipt["connector_invocation_ref"],
                    connector_invocation_hash=receipt["connector_invocation_hash"],
                    source_envelope_ref=receipt["source_envelope_ref"],
                    source_envelope_hash=receipt["source_envelope_hash"],
                    document_refs=refs,
                    in_authority_document_refs=[ref for ref in refs if ref in known],
                ))
        return records

    # -- acquisition ----------------------------------------------------

    def settle_documents(self) -> list[dict[str, Any]]:
        """Turn finished acquisition children into acquired rows and reviews."""

        settled: list[dict[str, Any]] = []
        for row in self.missions.launched_discovered_documents(
            source_ref=self.source_ref, limit=self.acquisitions_per_tick
        ):
            ticket_ref = row["ticket_ref"]
            if not ticket_ref:
                continue
            status = self.launcher.status(ticket_ref)
            if status["status"] == "running":
                continue
            if status["status"] != "succeeded":
                settled.append(self.missions.settle_discovered_document(
                    row["record_id"], status="acquisition_failed",
                    reason=f"child {status['status']}"[:500],
                ))
                continue
            try:
                self.launcher.read_completed_manifest(ticket_ref, row["document_ref"])
            except Exception as exc:  # noqa: BLE001 - a disagreement is a failure
                settled.append(self.missions.settle_discovered_document(
                    row["record_id"], status="acquisition_failed",
                    reason=f"{type(exc).__name__}: {exc}"[:500],
                ))
                continue
            settled.append(self.missions.settle_discovered_document(
                row["record_id"], status="acquired",
            ))
            self.missions.register_document_review(
                row["record_id"], requested_by=self.automation_principal
            )
        return settled

    def launch_acquisition(self) -> dict[str, Any]:
        """Start at most one acquisition child."""

        busy = self.missions.launched_discovered_documents(
            source_ref=self.source_ref, limit=1
        )
        if busy:
            return {"status": "busy"}
        row = self.missions.next_discovered_document(source_ref=self.source_ref)
        if row is None:
            return {"status": "idle"}
        ticket = self.launcher.start_bounded_probe(
            document_ref=row["document_ref"], caller_ref=self.automation_principal
        )
        self.missions.mark_discovered_document_launched(row["record_id"], ticket["id"])
        return {"status": "launched", "ticket_ref": ticket["id"],
                "document_ref": row["document_ref"]}

    def dispatch_once(self) -> dict[str, Any]:
        """One tick: settle what finished, then acquire a bounded batch.

        Settling first is not cosmetic. A tick that launched before settling
        would find the single slot occupied by its own previous child and
        report ``busy`` forever.
        """

        results = {"source_ref": self.source_ref, "settled": [], "launched": [], "status": "idle"}
        results["settled"].extend(self.settle_documents())
        for _ in range(self.acquisitions_per_tick):
            outcome = self.launch_acquisition()
            if outcome["status"] != "launched":
                break
            results["launched"].append(outcome)
            self.launcher.wait(timeout=self.acquisition_wait_seconds)
            results["settled"].extend(self.settle_documents())
        if results["launched"]:
            results["status"] = "dispatched"
        elif results["settled"]:
            results["status"] = "settled"
        return results


__all__ = [
    "ACQUISITIONS_PER_TICK",
    "ACQUISITION_RETRY_INTERVAL",
    "ACQUISITION_WAIT_SECONDS",
    "COMPANY_WIKI_SOURCE_REF",
    "DISCOVERY_SCOPE",
    "FEED_DISCOVERY_SOURCES",
    "SALES_NOTES_SOURCE_REF",
    "SALES_NOTE_SPEC_REF",
    "FeedDiscoveryCoordinator",
    "FeedLaneError",
    "FeedLaneRejected",
    "attribute_notes",
    "attribute_wiki_documents",
    "feed_discovery_parameters",
    "feed_query_hash",
    "wiki_spec_ref",
]
