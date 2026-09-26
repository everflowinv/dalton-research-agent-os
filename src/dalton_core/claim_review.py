"""P10b: the lane that reads admitted Claims back against their originals.

One pass per controller tick, bounded and idempotent:

1. take qualitative Claims that carry no challenge yet, oldest first;
2. resolve each Claim's exact original through its own citation chain
   (Claim → supports relation → Evidence → citation binding → correction set
   → source content hash → raw spool object), and verify the bytes hash to
   what the correction set recorded;
3. run the deterministic detectors and record a challenge for any hit;
4. when the mission grants ``claim_challenge`` to its automation principal,
   uphold each deterministic challenge; the authority re-runs the detector
   before it writes.

A Claim whose original cannot be read is never challenged: the check fails
closed, and the run reports how many were left alone and why.

2026-09-24 audit: every one of ws-7d's 2,605 qualitative Claims was
"unreadable".  The writer handed this lane its transcript spool only, and the
feed, wiki and Guidepoint children write their bytes into
``<state>/connector-spool`` (536 of 565 originals there, none in the transcript
spool).  The lane now reads the same roots the acquisition path reads
(``review_spool``), and a fetched web page -- whose citable original is the
deterministic *rendering* of its bytes, so no spool object carries that hash --
is re-rendered from its raw body and accepted only when the rendering hashes
to what the correction set recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .claim_retirement import (
    PRIOR_REREVIEW_RULE_REFS,
    REREVIEW_RULE_REF,
    SPAN_RATIONALE_PREFIX,
    SPAN_V2_DETECTOR_REF,
    ClaimRetirementAuthority,
    ClaimRetirementError,
    DETERMINISTIC_REASONS,
    detect,
    subject_absent,
    subject_needles,
)
from .claim_subject import (
    CONTEXT_CHARS,
    mission_subject_needles,
    name_needles,
    own_document_evidence,
    subject_named_for_reinstatement,
)
from .store import authorization_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA = Path(__file__).with_name("claim_review_schema.sql")
# The detector set an examination was made with.  Bump it and every Claim is
# re-examined once, which is the only thing that should re-open a clear one.
#
# v2 (2026-09-24): the subject-absent detector also reads the exact span a
# Claim cites, not only the whole original.  Bumping re-examines every Claim
# once -- including the ones a v1 pass could not read -- which is how the
# Claims admitted before the admission-time subject check get their look.
DETECTOR_SET_REF = "claim-detectors:boilerplate+subject-absent:v2-span"
# C2-5.  The original note said "a Claim that passes the detectors leaves no
# marker, so a claim-count cursor would never advance", and bounded the I/O
# instead -- forty distinct originals per pass, "and the rest wait for the next
# tick".  The next tick never came: without a marker the query returns the same
# oldest Claims for ever, so the same forty originals were re-read on every
# tick from 2026-09-07 to 2026-09-16 while 5,400 newer Claims were never looked
# at once.  The marker is now written (``claim_review_examinations``), so the
# set drains and the bound is a rate rather than a wall.
DEFAULT_MAX_DOCUMENTS = 40
# Examinations that failed to read the original are retried, slowly: a spool
# object can arrive late, and 815 permanently unreadable Claims must not eat
# the pass every tick.  This many per run, oldest attempt first.
DEFAULT_UNREADABLE_RETRIES = 4
# A fetched page is re-rendered to be read, and a large PDF takes seconds.
# The lane runs inside the writer tick, so renders are bounded per run the same
# way distinct originals are; the rest wait for the next tick.
DEFAULT_MAX_RENDERS = 6
# The re-review of past span retirements (2026-09-24 audit) reads originals
# too, under its own bound so it never starves the patrol's first look.
DEFAULT_REREVIEW_DOCUMENTS = 20
WRITE_SCOPE = "claim_challenge"
#: The state-directory spools an acquired original can be in, in the order the
#: other readers of acquired bytes already use (``mission_sec_quarters``).
SPOOL_ROOTS = ("transcript-spool", "connector-spool", "raw-spool")
_WEB_BODY_RE = re.compile(r":body-sha256:([0-9a-f]{64})$")
_ROUTE_REF_RE = re.compile(r"\bvia (route-decision:[0-9A-Za-z_-]+)")
# Rendering limits a stored rendering hash may have been produced under: the
# workspace-configured reading limits first, then the packaged defaults.  The
# hash decides which one is right; a wrong guess is simply "not this one".
_WEB_RENDER_LIMITS = (
    {"max_source_chars": 10_000_000, "max_pdf_pages": 2000,
     "max_decompressed_bytes": 100_000_000},
    {"max_source_chars": 600_000, "max_pdf_pages": 400,
     "max_decompressed_bytes": 8_000_000},
)
_WEB_MEDIA_FALLBACKS = ("text/html; charset=utf-8", "application/pdf",
                        "text/plain; charset=utf-8")
BINDING_PREFIX = "transcript-claim-citation-binding:"
# Words that name an industry rather than a company; a document mentioning
# "technology" tells us nothing about whether it is about DXC Technology.
_GENERIC_TERMS = frozenset({
    "technology", "technologies", "systems", "system", "group", "holdings",
    "international", "business", "machines", "company", "corp", "corporation",
    "inc", "plc", "ltd", "limited", "the", "and",
})


def needles_from_search_terms(terms: str) -> list[str]:
    """The company's own names, from the search terms the discovery plan uses."""

    found = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z.&-]{1,}", terms or "")
        if token.lower() not in _GENERIC_TERMS and len(token) >= 2
    }
    return sorted(found)


def needles_from_plans(plans: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """company_ref → its names, collected from every discovery and feed plan.

    Discovery plans carry ``search_terms``; feed plans carry ``names`` (W7),
    which is where a workspace says "GOOGL is Alphabet".  ws-7d's discovery
    plans search by ticker alone, so without the feed plan's names the only
    needle for Alphabet was ``googl``.
    """

    result: dict[str, set[str]] = {}
    for plan in plans:
        for company_ref, entry in ((plan or {}).get("companies") or {}).items():
            if not isinstance(entry, Mapping):
                continue
            terms = entry.get("search_terms")
            if isinstance(terms, str):
                result.setdefault(company_ref, set()).update(needles_from_search_terms(terms))
            names = entry.get("names")
            if isinstance(names, str):
                names = [names]
            if isinstance(names, Sequence):
                found = name_needles(names)
                if found:
                    result.setdefault(company_ref, set()).update(found)
    return {ref: sorted(values) for ref, values in result.items() if values}


def review_spool(state_dir: Any, primary: Any = None, extra_roots: Sequence[Any] = ()) -> Any:
    """Every root an acquired original may be in, read-only, primary first.

    ``primary`` is the writer's own transcript spool; ``extra_roots`` are the
    directories lane children were told to write to.  Roots that do not exist
    are skipped.  Which root answers is not a question about what is true: the
    caller re-hashes every object against the hash the citation chain names.
    """

    from pathlib import Path as _Path

    from .raw_spool import MultiRootRawSpoolReader, RawSpoolError, RawSpoolReader

    roots: list[Any] = [] if primary is None else [primary]
    seen: set[str] = set()
    candidates: list[Any] = list(extra_roots)
    if state_dir is not None:
        candidates.extend(_Path(state_dir) / name for name in SPOOL_ROOTS)
    for candidate in candidates:
        try:
            resolved = _Path(candidate).expanduser().resolve()
        except (OSError, TypeError):
            continue
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        try:
            roots.append(RawSpoolReader(resolved))
        except RawSpoolError:
            continue
    if not roots:
        return None
    return roots[0] if len(roots) == 1 else MultiRootRawSpoolReader(roots)


class ClaimReviewDriver:
    def __init__(
        self,
        *,
        store: Any,
        missions: Any,
        challenges: ClaimRetirementAuthority,
        spool: Any,
        needles: Mapping[str, Sequence[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
        reattributions: Any = None,
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.missions = missions
        self.challenges = challenges
        # 2026-09-25: the industry reattribution authority, when the writer
        # has one; without it the tick only reports what it would append.
        self.reattributions = reattributions
        self.spool = spool
        self.needles = {ref: list(values) for ref, values in (needles or {}).items()}
        self._document_refs: dict[str, str] = {}
        self._renders_left = DEFAULT_MAX_RENDERS
        self._render_deferred = False
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._authorization = authorization_flag(
            self.connection, "dalton_claim_review_authorized")
        self.connection.executescript(_SCHEMA.read_text(encoding="utf-8"))

    @classmethod
    def read_only(
        cls, *, connection: Any, missions: Any, spool: Any,
        needles: Mapping[str, Sequence[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> "ClaimReviewDriver":
        """A driver over a ``mode=ro`` connection, for dry runs only.

        Creates no schema and holds no write authority: ``run_once`` must not
        be called on it; ``rereview_retirements(dry_run=True)`` may.
        """

        driver = cls.__new__(cls)
        driver.store = type("_ReadOnlyStore", (), {"connection": connection})()
        driver.connection = connection
        driver.missions = missions
        driver.challenges = None
        driver.reattributions = None
        driver.spool = spool
        driver.needles = {ref: list(values) for ref, values in (needles or {}).items()}
        driver._document_refs = {}
        driver._renders_left = 10 ** 6
        driver._render_deferred = False
        driver.clock = clock or (lambda: datetime.now(timezone.utc))
        driver._authorization = None
        return driver

    # -- resolution ----------------------------------------------------------

    def _citation_chain(self) -> dict[str, str]:
        """claim_version_ref → the source content hash of the original it cites."""

        return {ref: item["digest"] for ref, item in self._citations().items()}

    def _citations(self) -> dict[str, dict[str, Any]]:
        """claim_version_ref → the exact citation: original hash, span, document.

        The span is the one the citation binding bound, read from the binding
        row itself; the document ref is the correction set's, which is what a
        fetched page's rendering is re-derived from.
        """

        evidence_by_claim = {
            row["claim_version_id"]: row["evidence_version_id"]
            for row in self.connection.execute(
                "SELECT claim_version_id, evidence_version_id FROM evidence_relations "
                "WHERE relation='supports'"
            ).fetchall()
        }
        bindings: dict[str, tuple[Any, Any, Any]] = {}
        for row in self.connection.execute(
            "SELECT binding_id, correction_set_version_ref, source_start, source_end "
            "FROM transcript_claim_citation_bindings"
        ).fetchall():
            bindings[row["binding_id"]] = (
                row["correction_set_version_ref"], row["source_start"], row["source_end"])
        corrections = {}
        for row in self.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions"
        ).fetchall():
            record = json.loads(row["record_json"])
            corrections[record["id"]] = record
        evidence = {}
        for row in self.connection.execute(
            "SELECT evidence_json FROM evidence_versions"
        ).fetchall():
            record = json.loads(row["evidence_json"])
            evidence[record["id"]] = record
        chain: dict[str, dict[str, Any]] = {}
        for claim_ref, evidence_ref in evidence_by_claim.items():
            record = evidence.get(evidence_ref)
            if record is None:
                continue
            binding_ref = next(
                (
                    item["ref"] for item in record.get("artifact_refs", ())
                    if isinstance(item, Mapping) and str(item.get("ref", "")).startswith(BINDING_PREFIX)
                ),
                None,
            )
            binding = bindings.get(binding_ref or "")
            correction = None if binding is None else corrections.get(binding[0])
            if correction is not None:
                digest = correction["source_content_hash"]
                rationale = (correction.get("raw_review") or {}).get("rationale")
                route = _ROUTE_REF_RE.search(rationale) if isinstance(rationale, str) else None
                chain[claim_ref] = {
                    "digest": digest, "start": binding[1], "end": binding[2],
                    "document_ref": correction.get("document_ref"),
                    # The route decision that drafted the statement, as the
                    # automation correction set recorded it: the producer an
                    # independent check must differ from.
                    "route_decision_ref": None if route is None else route.group(1),
                    # 2026-09-26b: where a sales note's own header (subject,
                    # send date, house) is read from, for the support check.
                    "source_envelope_ref": record.get("source_envelope_ref"),
                }
                if isinstance(correction.get("document_ref"), str):
                    self._document_refs.setdefault(digest, correction["document_ref"])
        return chain

    # -- reading the original ----------------------------------------------

    def source_text(self, content_sha256: str) -> str | None:
        """The exact original text, verified against the hash the chain names.

        Either a spool object with exactly that hash, or -- for a fetched page,
        whose citable original is the rendering of its body -- the rendering
        of the body the document ref names, when it hashes to exactly that.
        """

        raw = self._read(content_sha256)
        if raw is not None:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return self._rendered_web_text(content_sha256)

    def _read(self, content_sha256: str) -> bytes | None:
        try:
            raw = self.spool.read_object(content_sha256)
        except Exception:  # noqa: BLE001 - a missing object is "cannot read", not a crash
            return None
        if hashlib.sha256(raw).hexdigest() != content_sha256:
            return None
        return raw

    def _web_media_types(self, body_sha256: str, body: bytes) -> list[str]:
        """What the fetch recorded the body as, then the renderable fallbacks."""

        found: list[str] = []
        try:
            rows = self.connection.execute(
                "SELECT record_json FROM observability_artifact_versions_v2 "
                "WHERE artifact_content_hash=?", (body_sha256,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            try:
                media = json.loads(row["record_json"]).get("media_type")
            except (TypeError, ValueError):
                continue
            if isinstance(media, str) and media and media not in found:
                found.append(media)
        if found:
            return found
        # Nothing recorded: guess from the bytes, the hash decides.
        if body.startswith(b"%PDF-"):
            return ["application/pdf"]
        return [media for media in _WEB_MEDIA_FALLBACKS if media != "application/pdf"]

    def _rendered_web_text(self, content_sha256: str) -> str | None:
        document_ref = self._document_refs.get(content_sha256)
        match = _WEB_BODY_RE.search(document_ref or "")
        if match is None:
            return None
        if self._renders_left <= 0:
            self._render_deferred = True
            return None
        self._renders_left -= 1
        body = self._read(match.group(1))
        if body is None:
            return None
        from .public_web_extraction_source import render_public_web_text

        for media in self._web_media_types(match.group(1), body):
            for limits in _WEB_RENDER_LIMITS:
                try:
                    text = render_public_web_text(body, raw_media_type=media, **limits)["text"]
                except Exception:  # noqa: BLE001 - "not this rendering", try the next
                    break
                if hashlib.sha256(text.encode("utf-8")).hexdigest() == content_sha256:
                    return text
        return None

    def _document_facts(self, document_ref: Any) -> dict[str, Any]:
        """The recorded title and whether the kind is the issuer's own filing."""

        facts: dict[str, Any] = {"title": None, "issuer_document": False}
        if not isinstance(document_ref, str):
            return facts
        cache = self.__dict__.setdefault("_facts_cache", {})
        if document_ref in cache:
            return cache[document_ref]
        cache[document_ref] = facts
        try:
            row = self.connection.execute(
                "SELECT title FROM document_provenance_records WHERE document_ref=?",
                (document_ref,),
            ).fetchone()
            if row is not None:
                facts["title"] = row["title"]
        except sqlite3.Error:
            pass
        try:
            row = self.connection.execute(
                "SELECT 1 FROM coverage_mission_discovered_documents "
                "WHERE document_ref=? AND source_ref='source:sec-edgar' LIMIT 1",
                (document_ref,),
            ).fetchone()
            facts["issuer_document"] = row is not None
        except sqlite3.Error:
            pass
        return facts

    def _span_inputs(
        self, citation: Mapping[str, Any] | None, text: str | None,
        needles: Sequence[str], *, subject_ref: Any = None,
        peer_needles: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Everything the span rule reads: the span, the text around it,
        whether the document is the subject's own, and the other covered
        companies' names (v3, see ``claim_subject``)."""

        if citation is None or text is None:
            return {"cited_span": None, "document_is_own": False}
        start, end = citation.get("start"), citation.get("end")
        span = None
        before = after = None
        if (isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(text)):
            span = text[start:end]
            before = text[max(0, start - CONTEXT_CHARS):start]
            after = text[end:end + CONTEXT_CHARS]
        facts = self._document_facts(citation.get("document_ref"))
        own = own_document_evidence(
            title=facts["title"], text=text, needles=needles,
            issuer_document=facts["issuer_document"], subject_ref=subject_ref,
            peer_needles=peer_needles,
        ) is not None
        return {"cited_span": span, "document_is_own": own,
                "context_before": before, "context_after": after,
                "peer_needles": list(peer_needles)}

    def _strict_own(self, citation: Mapping[str, Any] | None, text: str | None,
                    needles: Sequence[str], *, subject_ref: Any,
                    peer_needles: Sequence[str]) -> str | None:
        """Why the document is the subject's own, without the head test.

        What a reinstatement may rest on (2026-09-25): a head that names the
        subject in a list is not the subject's document.
        """

        if citation is None or text is None:
            return None
        facts = self._document_facts(citation.get("document_ref"))
        return own_document_evidence(
            title=facts["title"], text=text, needles=needles,
            issuer_document=facts["issuer_document"], subject_ref=subject_ref,
            peer_needles=peer_needles, include_head=False,
        )

    def _named_for_reinstatement(self, claim: Mapping[str, Any], citation: Any,
                                 text: str | None, needles: Sequence[str],
                                 inputs: Mapping[str, Any], *, subject_ref: Any,
                                 peers: Sequence[str]) -> tuple[str | None, str | None]:
        strict_own = self._strict_own(citation, text, needles, subject_ref=subject_ref,
                                      peer_needles=peers)
        named_by = subject_named_for_reinstatement(
            statement=claim["normalized_statement"], span=inputs.get("cited_span"),
            needles=needles, peer_needles=peers,
            context_before=inputs.get("context_before"),
            context_after=inputs.get("context_after"), own_document=strict_own,
        )
        return named_by, strict_own

    def _peer_needles(self, subject_ref: Any, roster: Mapping[str, Sequence[str]]) -> list[str]:
        """Every other covered company's names: what "another company" means."""

        mine = set(self._needles_for(subject_ref, roster))
        found: set[str] = set()
        for ref in set(self.needles) | set(roster):
            if ref != subject_ref:
                found.update(self._needles_for(ref, roster))
        return sorted(found - mine)

    # -- the pass ------------------------------------------------------------

    def _grant(self) -> tuple[str | None, str | None]:
        """The automation principal allowed to retire, or the reason there is none."""

        rows = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall()
        hold: str | None = None
        for row in rows:
            mission = self.missions.mission(row["mission_version_id"])
            if WRITE_SCOPE in mission["autonomy"]["may_write"]:
                return mission["autonomy"]["automation_principal"], None
            # Keep looking: returning on the first mission made a second
            # mission's grant unreachable.
            hold = (
                f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                "已标记的结论等你确认后才会退役"
            )
        return None, hold or "没有生效中的任务"

    def _roster_needles(self) -> dict[str, list[str]]:
        """The mission universe's own tickers and names, as a fallback.

        ``needles_from_plans`` keys on the discovery plan's company entries. A
        Claim whose ``subject_ref`` is not exactly one of those keys gets an
        empty needle list, and ``subject_absent_from_source`` returns False for
        an empty list by design -- so the misattribution detector silently
        cannot fire for that company. The mission roster always knows the
        ticker; use it wherever the plan does not.
        """

        roster: dict[str, list[str]] = {}
        try:
            rows = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall()
            for row in rows:
                mission = self.missions.mission(row["mission_version_id"])
                universe = list(mission.get("universe") or ())
                for member in universe:
                    found = subject_needles(member)
                    if found:
                        roster.setdefault(member["company_ref"], []).extend(found)
                # The packaged alias table too (document_subject.COMPANY_NAMES).
                for ref, found in mission_subject_needles(universe).items():
                    roster.setdefault(ref, []).extend(found)
        except Exception:  # noqa: BLE001 - a missing roster is not a gate
            return {}
        return {ref: sorted(set(values)) for ref, values in roster.items()}

    def _needles_for(self, subject_ref: Any, roster: Mapping[str, Sequence[str]]) -> list[str]:
        """Every name the plans and the roster know, as one set.

        Was "the plan's names, else the roster's".  Every check built on these
        asks whether *some* name is present, so an extra alias can only make it
        keep more; a precedence meant a plan that searched by ticker alone
        ("GOOGL") hid the name the roster knew ("Alphabet").
        """

        return sorted(set(self.needles.get(subject_ref, ())) | set(roster.get(subject_ref, ())))

    # -- the examination marker ---------------------------------------------

    def _examinations(self) -> dict[str, dict[str, Any]]:
        return {
            row["claim_version_ref"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM claim_review_examinations").fetchall()
        }

    def _record_examination(
        self, *, claim_version_ref: str, claim_version_hash: str,
        source_content_hash: str | None, outcome: str, attempts: int = 1,
    ) -> None:
        at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        self._authorization.authorized = True
        try:
            self.connection.execute(
                "INSERT INTO claim_review_examinations("
                "claim_version_ref,claim_version_hash,source_content_hash,detector_ref,"
                "outcome,attempts,examined_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(claim_version_ref) DO UPDATE SET "
                "outcome=excluded.outcome, source_content_hash=excluded.source_content_hash, "
                # A re-examination under a new detector set is recorded as
                # made with it; without this the row kept the old ref and the
                # Claim was re-examined on every tick for ever.
                "detector_ref=excluded.detector_ref, claim_version_hash=excluded.claim_version_hash, "
                "attempts=claim_review_examinations.attempts+1, "
                "examined_at=excluded.examined_at",
                (claim_version_ref, claim_version_hash, source_content_hash,
                 DETECTOR_SET_REF, outcome, attempts, at),
            )
            self.connection.commit()
        except Exception:  # noqa: BLE001 - a marker failure must not lose the pass
            self.connection.rollback()
        finally:
            self._authorization.authorized = False

    def run_once(
        self,
        *,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        max_claims: int | None = None,
        unreadable_retries: int = DEFAULT_UNREADABLE_RETRIES,
        max_renders: int = DEFAULT_MAX_RENDERS,
    ) -> dict[str, Any]:
        """Detect, then write only under the mission's grant.

        Detection is free and always reported; persisting a challenge or a
        decision is a write, and ADR-0004 says automation writes only what the
        mission grants.  Without the grant the run says exactly what it found
        and changes nothing, which is what the owner needs to decide.

        ``max_claims`` is accepted as a spelling of ``max_documents`` because
        the lane registry advertises that name and the writer passes it
        through; the two have always meant the same bound.
        """

        if max_claims is not None:
            max_documents = int(max_claims)
        summary: dict[str, Any] = {
            "status": "idle", "schema_version": SCHEMA_VERSION, "scanned": 0,
            "detected": [], "challenged": [], "retired": [], "unreadable": 0,
            "deferred": 0, "documents_read": 0, "skipped": [],
            # C2-5: how far the patrol has got through the Ledger, which is the
            # number that was silently zero for nine days.
            "examined": 0, "already_examined": 0, "reexamined_unreadable": 0,
            "remaining": 0,
        }
        principal, hold = self._grant()
        summary["grant"] = {"principal": principal, "hold": hold}
        examined = self._examinations()
        # A clear or challenged examination is final until the detectors change.
        settled = {
            ref for ref, item in examined.items()
            if item["outcome"] in ("clear", "challenged")
            and item["detector_ref"] == DETECTOR_SET_REF
        }
        # Unreadable ones come back slowly, oldest attempt first.
        retryable = sorted(
            (ref for ref, item in examined.items()
             if item["outcome"] == "unreadable"
             and item["detector_ref"] == DETECTOR_SET_REF),
            key=lambda ref: (examined[ref]["attempts"], examined[ref]["examined_at"]),
        )[:max(0, int(unreadable_retries))]
        # Only an unreadable verdict of *this* detector set is deferred; one
        # made under an older set is re-examined like a Claim never seen, which
        # is what lets a fixed reader drain the backlog the old one left.
        deferred_unreadable = {
            ref for ref, item in examined.items()
            if item["outcome"] == "unreadable" and ref not in set(retryable)
            and item["detector_ref"] == DETECTOR_SET_REF
        }
        # Newest first: a wrong Claim committed this morning should be caught
        # today, and the backlog drains from the top because an examined Claim
        # never comes back.
        rows = self.connection.execute(
            "SELECT c.claim_version_id AS ref, c.claim_json AS claim_json, c.content_hash AS hash "
            "FROM claim_versions c LEFT JOIN claim_retirement_challenges h "
            "ON h.claim_version_ref=c.claim_version_id "
            "WHERE h.challenge_id IS NULL ORDER BY c.created_at DESC, c.claim_version_id"
        ).fetchall()
        self._renders_left = max(0, int(max_renders))
        citations = self._citations() if rows else {}
        chain = {ref: item["digest"] for ref, item in citations.items()}
        texts: dict[str, str | None] = {}
        detections: list[dict[str, Any]] = []
        roster = self._roster_needles()
        outcomes: list[tuple[str, str, str | None, str]] = []
        for row in rows:
            claim = json.loads(row["claim_json"])
            if claim.get("claim_kind") != "qualitative":
                continue
            if row["ref"] in settled:
                summary["already_examined"] += 1
                continue
            if row["ref"] in deferred_unreadable:
                summary["deferred"] += 1
                continue
            summary["scanned"] += 1
            digest = chain.get(row["ref"])
            text: str | None = None
            if digest is not None:
                if digest not in texts:
                    if len(texts) >= max(1, int(max_documents)):
                        summary["deferred"] += 1
                        summary["remaining"] += 1
                        continue  # this original waits for the next tick
                    self._render_deferred = False
                    text = self.source_text(digest)
                    if text is None and self._render_deferred:
                        # Out of renders for this run, not unreadable: the
                        # page waits for the next tick unmarked.
                        summary["deferred"] += 1
                        summary["remaining"] += 1
                        continue
                    texts[digest] = text
                text = texts[digest]
            if row["ref"] in set(retryable):
                summary["reexamined_unreadable"] += 1
            if digest is None or text is None:
                summary["unreadable"] += 1
                outcomes.append((row["ref"], row["hash"], digest, "unreadable"))
                continue
            needles = self._needles_for(claim["subject_ref"], roster)
            hit = detect(
                statement=claim["normalized_statement"], source_text=text,
                needles=needles,
                **self._span_inputs(
                    citations.get(row["ref"]), text, needles,
                    subject_ref=claim["subject_ref"],
                    peer_needles=self._peer_needles(claim["subject_ref"], roster)),
            )
            if hit is None:
                outcomes.append((row["ref"], row["hash"], digest, "clear"))
                continue
            reason, rationale = hit
            outcomes.append((row["ref"], row["hash"], digest, "challenged"))
            detections.append({
                "claim_version_ref": row["ref"], "claim_version_hash": row["hash"],
                "reason_code": reason, "rationale": rationale,
                "subject_ref": claim["subject_ref"],
                "statement": claim["normalized_statement"][:160],
                "source_content_hash": digest,
            })
        # The marker is what makes the next pass a different pass.  Written
        # whether or not the mission grants the write scope: examining is a
        # read, and remembering that it was done costs nobody anything.
        for claim_version_ref, claim_version_hash, digest, outcome in outcomes:
            self._record_examination(
                claim_version_ref=claim_version_ref,
                claim_version_hash=claim_version_hash,
                source_content_hash=digest, outcome=outcome,
            )
            summary["examined"] += 1
        summary["documents_read"] = len(texts)
        summary["detected"] = [
            {k: v for k, v in item.items() if k in ("claim_version_ref", "reason_code", "subject_ref", "statement")}
            for item in detections
        ]
        if principal is None:
            summary["status"] = "held" if detections else "idle"
            # Report-only: what the re-review would withdraw under a grant.
            summary["rereview"] = self.rereview_retirements(
                principal=None, roster=roster, citations=citations, texts=texts)
            summary["reinstatement_recheck"] = self.recheck_reinstatements(
                principal=None, roster=roster, citations=citations, texts=texts)
            summary["industry_reattribution"] = self.reattribute_industry_findings(
                principal=None, citations=citations, texts=texts)
            if (summary["rereview"]["would_reinstate"]
                    or summary["reinstatement_recheck"]["would_withdraw"]
                    or summary["industry_reattribution"]["would_reattribute"]):
                summary["status"] = "held"
            return summary
        for item in detections:
            try:
                record = self.challenges.challenge(
                    claim_version_ref=item["claim_version_ref"],
                    claim_version_hash=item["claim_version_hash"],
                    reason_code=item["reason_code"], rationale=item["rationale"],
                    actor_ref=principal,
                )
            except ClaimRetirementError as exc:
                summary["skipped"].append({"claim_version_ref": item["claim_version_ref"], "reason": str(exc)})
                continue
            if record.get("status") == "fresh":
                summary["challenged"].append({
                    "claim_version_ref": item["claim_version_ref"], "reason_code": item["reason_code"],
                    "challenge_ref": record["id"], "subject_ref": item["subject_ref"],
                })
        for challenge in self.challenges.challenges(open_only=True, limit=200):
            if challenge["reason_code"] not in DETERMINISTIC_REASONS:
                continue
            text = None
            needles = self._needles_for(challenge["subject_ref"], roster)
            span_inputs: dict[str, Any] = {}
            if challenge["reason_code"] == "subject_absent_from_source":
                citation = citations.get(challenge["claim_version_ref"])
                if citation is None:
                    citation = self._citations().get(challenge["claim_version_ref"])
                if citation is not None:
                    digest = citation["digest"]
                    text = texts.get(digest) or self.source_text(digest)
                    span_inputs = self._span_inputs(
                        citation, text, needles, subject_ref=challenge["subject_ref"],
                        peer_needles=self._peer_needles(challenge["subject_ref"], roster))
            try:
                self.challenges.decide(
                    challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
                    decision="retired", actor_ref=principal, rationale=challenge["rationale"],
                    subject_needles=needles,
                    source_text=text, **span_inputs,
                )
            except ClaimRetirementError as exc:
                summary["skipped"].append({"challenge_ref": challenge["id"], "reason": str(exc)})
                continue
            summary["retired"].append({
                "claim_version_ref": challenge["claim_version_ref"],
                "reason_code": challenge["reason_code"], "subject_ref": challenge["subject_ref"],
            })
        summary["rereview"] = self.rereview_retirements(
            principal=principal, roster=roster, citations=citations, texts=texts)
        # 2026-09-25: reinstatements the v3 rule made too loosely are withdrawn
        # here, so the retirement stands again before the industry pass reads.
        summary["reinstatement_recheck"] = self.recheck_reinstatements(
            principal=principal, roster=roster, citations=citations, texts=texts)
        # 2026-09-25b: reattributions an earlier industry rule made and today's
        # refuses (a lone "capex") are withdrawn before the backfill runs.
        summary["industry_reattribution_recheck"] = self.recheck_industry_reattributions(
            principal=principal, citations=citations, texts=texts)
        # After the re-review, so a retirement withdrawn this tick is its
        # company's again and never also the industry's.
        summary["industry_reattribution"] = self.reattribute_industry_findings(
            principal=principal, citations=citations, texts=texts)
        if (summary["challenged"] or summary["retired"] or summary["rereview"]["reinstated"]
                or summary["reinstatement_recheck"]["withdrawn"]
                or summary["industry_reattribution_recheck"].get("withdrawn")
                or summary["industry_reattribution"]["reattributed"]):
            summary["status"] = "acted"
        return summary

    # -- re-check of standing automatic reinstatements (2026-09-25) ---------

    def reinstatements_to_recheck(self) -> list[dict[str, Any]]:
        """Automatic reinstatements made under an earlier rule, still standing."""

        try:
            rows = self.connection.execute(
                "SELECT r.claim_version_ref AS ref, r.content_hash AS reinstatement_hash, "
                "r.rule_ref AS rule_ref, v.claim_json AS claim_json "
                "FROM claim_retirement_reinstatements r "
                "JOIN claim_versions v ON v.claim_version_id=r.claim_version_ref "
                "WHERE r.reason_code='subject_named_under_current_rule' "
                "ORDER BY r.created_at, r.claim_version_ref"
            ).fetchall()
        except sqlite3.Error:
            return []
        from .claim_retirement import withdrawn_reinstatement_claim_version_refs

        withdrawn = withdrawn_reinstatement_claim_version_refs(self.connection)
        return [
            {**dict(row), "subject_ref": json.loads(row["claim_json"]).get("subject_ref")}
            for row in rows
            if row["rule_ref"] in PRIOR_REREVIEW_RULE_REFS and row["ref"] not in withdrawn
        ]

    def recheck_reinstatements(
        self,
        *,
        principal: str | None,
        roster: Mapping[str, Sequence[str]] | None = None,
        citations: Mapping[str, Mapping[str, Any]] | None = None,
        texts: dict[str, str | None] | None = None,
        max_documents: int = DEFAULT_REREVIEW_DOCUMENTS,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-judge the automatic reinstatements under today's stricter rule.

        The v3 re-review put Claims back on a subject named anywhere in a long
        span or in a document's head (2026-09-25 audit).  Each automatic
        reinstatement made under an earlier rule and still standing is read
        again against its exact original; when the v4 rule finds the subject
        not positively named, the automation principal appends a withdrawal
        (the authority re-runs the rule) and the retirement stands again.
        Without a principal -- or on ``dry_run`` -- it reports only.

        Idempotent: a withdrawn reinstatement leaves the query, and a confirmed
        one is marked (``claim_review_rereviews``, outcome ``reinstated`` under
        the v4 rule ref and the needles hash) and read again only when the
        rule or the alias table changes.
        """

        roster = self._roster_needles() if roster is None else roster
        summary: dict[str, Any] = {
            "rule_ref": REREVIEW_RULE_REF, "candidates": 0, "examined": 0,
            "withdrawn": [], "would_withdraw": [], "confirmed": 0, "unreadable": 0,
            "deferred": 0, "already_reviewed": 0, "skipped": [],
        }
        candidates = self.reinstatements_to_recheck()
        summary["candidates"] = len(candidates)
        if not candidates:
            return summary
        markers = self._rereviews()
        if not citations or any(row["ref"] not in citations for row in candidates):
            citations = {**self._citations(), **(citations or {})}
        texts = {} if texts is None else texts
        read_here = 0
        for row in candidates:
            needles = self._needles_for(row["subject_ref"], roster)
            peers = self._peer_needles(row["subject_ref"], roster)
            needles_hash = content_hash({"needles": needles, "peers": peers})
            marker = markers.get(row["ref"])
            if (marker is not None and marker["rule_ref"] == REREVIEW_RULE_REF
                    and marker["needles_hash"] == needles_hash
                    and marker["outcome"] == "reinstated"):
                summary["already_reviewed"] += 1
                continue
            citation = citations.get(row["ref"])
            text = None
            if citation is not None:
                digest = citation["digest"]
                if digest not in texts:
                    if read_here >= max(1, int(max_documents)):
                        summary["deferred"] += 1
                        continue
                    read_here += 1
                    texts[digest] = self.source_text(digest)
                text = texts[digest]
            summary["examined"] += 1
            if text is None:
                summary["unreadable"] += 1
                continue
            claim = json.loads(row["claim_json"])
            inputs = self._span_inputs(citation, text, needles,
                                       subject_ref=row["subject_ref"], peer_needles=peers)
            named_by, strict_own = self._named_for_reinstatement(
                claim, citation, text, needles, inputs,
                subject_ref=row["subject_ref"], peers=peers)
            if named_by is not None:
                summary["confirmed"] += 1
                if not dry_run and self._authorization is not None:
                    self._record_rereview(row["ref"], needles_hash, "reinstated")
                continue
            item = {"claim_version_ref": row["ref"], "subject_ref": row["subject_ref"],
                    "statement": claim["normalized_statement"][:200]}
            if dry_run or principal is None or self.challenges is None:
                summary["would_withdraw"].append(item)
                continue
            try:
                record = self.challenges.withdraw_reinstatement(
                    claim_version_ref=row["ref"], actor_ref=principal,
                    reinstatement_hash=row["reinstatement_hash"],
                    rationale=(
                        "按 v4 撤销规则重判：结论本身、其高管、所依附的上文或本公司文件"
                        "（不含仅开头提到）都没有指向这家公司；只是长片段里某处出现了名字，"
                        "当初的撤销不成立，退役恢复。"),
                    subject_needles=needles, cited_span=inputs.get("cited_span"),
                    context_before=inputs.get("context_before"),
                    context_after=inputs.get("context_after"),
                    peer_needles=peers, strict_own_document=strict_own,
                )
            except ClaimRetirementError as exc:
                summary["skipped"].append({"claim_version_ref": row["ref"], "reason": str(exc)})
                continue
            self._record_rereview(row["ref"], needles_hash, "still_retired")
            summary["withdrawn"].append({**item, "withdrawal_ref": record["id"],
                                         "status": record["status"]})
        return summary

    # -- industry-level findings among the retirements (2026-09-25) ---------

    def reattribute_industry_findings(
        self,
        *,
        principal: str | None,
        citations: Mapping[str, Mapping[str, Any]] | None = None,
        texts: dict[str, str | None] | None = None,
        max_documents: int | None = None,
        max_writes: int | None = None,
        dry_run: bool = False,
        show: int | None = 20,
    ) -> dict[str, Any]:
        """Record retired industry-level Claims against the mission's industry.

        The bounded, idempotent backfill of ``claim_industry_reattribution``
        (``run_backfill``): under the mission's ``claim_challenge`` grant it
        appends a reattribution for each retired subject-absent Claim the
        deterministic industry-level rule keeps; without the grant (or on
        ``dry_run``) it reports what it would append.  A failure here never
        costs the patrol its pass.
        """

        from .claim_industry_reattribution import (
            DEFAULT_MAX_DOCUMENTS as REATTRIBUTION_DOCUMENTS,
            DEFAULT_MAX_WRITES as REATTRIBUTION_WRITES,
            RULE_REF as REATTRIBUTION_RULE_REF,
            run_backfill,
        )

        try:
            return run_backfill(
                self, authority=getattr(self, "reattributions", None), principal=principal,
                citations=citations, texts=texts,
                max_documents=REATTRIBUTION_DOCUMENTS if max_documents is None else max_documents,
                max_writes=REATTRIBUTION_WRITES if max_writes is None else max_writes,
                dry_run=dry_run, show=show,
            )
        except Exception as exc:  # noqa: BLE001 - the patrol's own pass stands
            return {"rule_ref": REATTRIBUTION_RULE_REF, "reattributed": [],
                    "would_reattribute": [],
                    "skipped": [{"reason": f"{type(exc).__name__}: {exc}"}]}

    def recheck_industry_reattributions(
        self,
        *,
        principal: str | None,
        citations: Mapping[str, Mapping[str, Any]] | None = None,
        texts: dict[str, str | None] | None = None,
        max_documents: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Withdraw automatic reattributions today's industry rule refuses.

        ``claim_industry_reattribution.run_recheck`` under the mission's
        ``claim_challenge`` grant; without it (or on ``dry_run``) it reports
        what it would withdraw.  A failure here never costs the patrol its
        pass.
        """

        from .claim_industry_reattribution import (
            DEFAULT_MAX_DOCUMENTS as REATTRIBUTION_DOCUMENTS,
            RULE_REF as REATTRIBUTION_RULE_REF,
            run_recheck,
        )

        try:
            return run_recheck(
                self, authority=getattr(self, "reattributions", None), principal=principal,
                citations=citations, texts=texts,
                max_documents=REATTRIBUTION_DOCUMENTS if max_documents is None else max_documents,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - the patrol's own pass stands
            return {"rule_ref": REATTRIBUTION_RULE_REF, "withdrawn": [], "would_withdraw": [],
                    "skipped": [{"reason": f"{type(exc).__name__}: {exc}"}]}

    # -- re-review of past span retirements (2026-09-24 audit) ---------------

    def _rereviews(self) -> dict[str, dict[str, Any]]:
        try:
            return {
                row["claim_version_ref"]: dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM claim_review_rereviews").fetchall()
            }
        except sqlite3.Error:
            return {}

    def _record_rereview(self, claim_version_ref: str, needles_hash: str, outcome: str) -> None:
        at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        self._authorization.authorized = True
        try:
            self.connection.execute(
                "INSERT INTO claim_review_rereviews(claim_version_ref,rule_ref,needles_hash,"
                "outcome,attempts,reviewed_at) VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(claim_version_ref) DO UPDATE SET rule_ref=excluded.rule_ref, "
                "needles_hash=excluded.needles_hash, outcome=excluded.outcome, "
                "attempts=claim_review_rereviews.attempts+1, reviewed_at=excluded.reviewed_at",
                (claim_version_ref, REREVIEW_RULE_REF, needles_hash, outcome, at),
            )
            self.connection.commit()
        except Exception:  # noqa: BLE001 - a marker failure must not lose the pass
            self.connection.rollback()
        finally:
            self._authorization.authorized = False

    def span_retirements_to_rereview(self) -> list[dict[str, Any]]:
        """Span retirements made before the v3 rule and not yet withdrawn.

        Exactly one class: reason ``subject_absent_from_source``, detector v2,
        and the span form of the rationale.  Whole-document retirements, the
        boilerplate filter, human judgments and model verdicts are never
        re-judged here.
        """

        reinstated = (
            "AND NOT EXISTS (SELECT 1 FROM claim_retirement_reinstatements r "
            "WHERE r.decision_ref=d.decision_id) "
        )
        try:
            self.connection.execute("SELECT 1 FROM claim_retirement_reinstatements LIMIT 1")
        except sqlite3.Error:
            reinstated = ""
        rows = self.connection.execute(
            "SELECT c.claim_version_ref AS ref, c.subject_ref AS subject_ref, "
            "d.content_hash AS decision_hash, v.claim_json AS claim_json "
            "FROM claim_retirement_challenges c "
            "JOIN claim_retirement_decisions d ON d.challenge_ref=c.challenge_id "
            "JOIN claim_versions v ON v.claim_version_id=c.claim_version_ref "
            "WHERE d.decision='retired' AND c.reason_code='subject_absent_from_source' "
            "AND c.detector_ref=? AND substr(c.rationale,1,?)=? " + reinstated +
            "ORDER BY d.created_at, c.claim_version_ref",
            (SPAN_V2_DETECTOR_REF, len(SPAN_RATIONALE_PREFIX), SPAN_RATIONALE_PREFIX),
        ).fetchall()
        return [dict(row) for row in rows]

    def rereview_retirements(
        self,
        *,
        principal: str | None,
        roster: Mapping[str, Sequence[str]] | None = None,
        citations: Mapping[str, Mapping[str, Any]] | None = None,
        texts: dict[str, str | None] | None = None,
        max_documents: int = DEFAULT_REREVIEW_DOCUMENTS,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-judge the pre-audit span retirements under today's rule.

        The v2 detector ran before the alias table named AWS, Gemini or an
        issuer's products, and before the rule looked past the span; the audit
        found retirements it made that the current rule would not.  Each
        still-standing v2 span retirement is re-read against its exact
        original with the current needles and the v3 rule; when the rule no
        longer fires, the automation principal appends a reinstatement (the
        authority re-runs the rule before writing).  Without a principal --
        or on ``dry_run`` -- it only reports what it would withdraw.

        Idempotent: a reinstated retirement leaves the query; a still-retired
        one is marked with the rule ref and the hash of the needles it was
        judged with, and is looked at again only when either changes.
        """

        roster = self._roster_needles() if roster is None else roster
        summary: dict[str, Any] = {
            "rule_ref": REREVIEW_RULE_REF, "candidates": 0, "examined": 0,
            "reinstated": [], "would_reinstate": [], "still_retired": 0,
            "unreadable": 0, "deferred": 0, "already_reviewed": 0, "skipped": [],
        }
        try:
            candidates = self.span_retirements_to_rereview()
        except sqlite3.Error as exc:
            summary["skipped"].append({"reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["candidates"] = len(candidates)
        if not candidates:
            return summary
        markers = self._rereviews()
        # The patrol's pass only resolves the Claims it examined; a retired
        # Claim was examined long ago, so resolve the whole chain once here.
        if not citations or any(row["ref"] not in citations for row in candidates):
            citations = {**self._citations(), **(citations or {})}
        texts = {} if texts is None else texts
        read_here = 0
        for row in candidates:
            needles = self._needles_for(row["subject_ref"], roster)
            peers = self._peer_needles(row["subject_ref"], roster)
            needles_hash = content_hash({"needles": needles, "peers": peers})
            marker = markers.get(row["ref"])
            if (marker is not None and marker["rule_ref"] == REREVIEW_RULE_REF
                    and marker["needles_hash"] == needles_hash
                    and marker["outcome"] == "still_retired"):
                summary["already_reviewed"] += 1
                continue
            citation = citations.get(row["ref"])
            text = None
            if citation is not None:
                digest = citation["digest"]
                if digest not in texts:
                    if read_here >= max(1, int(max_documents)):
                        summary["deferred"] += 1
                        continue
                    read_here += 1
                    texts[digest] = self.source_text(digest)
                text = texts[digest]
            summary["examined"] += 1
            if text is None:
                summary["unreadable"] += 1
                if not dry_run:
                    self._record_rereview(row["ref"], needles_hash, "unreadable")
                continue
            claim = json.loads(row["claim_json"])
            inputs = self._span_inputs(citation, text, needles,
                                       subject_ref=row["subject_ref"], peer_needles=peers)
            named_by, strict_own = self._named_for_reinstatement(
                claim, citation, text, needles, inputs,
                subject_ref=row["subject_ref"], peers=peers)
            if subject_absent(
                statement=claim["normalized_statement"], source_text=text,
                needles=needles, **inputs,
            ) is not None or named_by is None:
                # v4: the retirement rule no longer firing is not enough; the
                # subject must be positively named to put the Claim back.
                summary["still_retired"] += 1
                if not dry_run:
                    self._record_rereview(row["ref"], needles_hash, "still_retired")
                continue
            item = {"claim_version_ref": row["ref"], "subject_ref": row["subject_ref"],
                    "statement": claim["normalized_statement"][:160], "named_by": named_by}
            if dry_run or principal is None:
                summary["would_reinstate"].append(item)
                continue
            try:
                record = self.challenges.reinstate(
                    claim_version_ref=row["ref"], decision_hash=row["decision_hash"],
                    actor_ref=principal,
                    rationale=(
                        "按当前别名表和 v3 退役规则重判：所引片段、结论本身、紧邻上下文或"
                        "文档本身（封面/标题/密度）其实指向这家公司，当初的退役不成立。"),
                    subject_needles=needles, source_text=text,
                    strict_own_document=strict_own, **inputs,
                )
            except ClaimRetirementError as exc:
                summary["skipped"].append({"claim_version_ref": row["ref"], "reason": str(exc)})
                continue
            self._record_rereview(row["ref"], needles_hash, "reinstated")
            summary["reinstated"].append({**item, "reinstatement_ref": record["id"],
                                          "status": record["status"]})
        return summary


__all__ = [
    "ClaimReviewDriver",
    "DEFAULT_MAX_DOCUMENTS",
    "DEFAULT_REREVIEW_DOCUMENTS",
    "DEFAULT_MAX_RENDERS",
    "DEFAULT_UNREADABLE_RETRIES",
    "DETECTOR_SET_REF",
    "WRITE_SCOPE",
    "needles_from_plans",
    "needles_from_search_terms",
    "review_spool",
    "SPOOL_ROOTS",
]
