"""2026-09-26: ask again what the support check held only because it could not run.

From 00:00 UTC every call of ``claim_support_verifier`` was refused by the
router before any model saw it ("CockpitModelRouteUnavailable: no model route
is available right now": the purposes sat on the cheap chain, none of whose
links can serve a WorkOrder with a provider output contract).  The verifier
counted those refusals, and after three a window's statements were staged as
"held for human review: the independent support check could not be run" and
the review was closed as ``extraction_staged`` -- 105 statements on legacy and
94 on ws-7d by 04:36, every one of them waiting for a person although nobody
had judged it.

The verifier no longer counts a systemic failure (``systemic_failure``), so
nothing new is held that way.  This pass returns the ones already held to the
path they should have taken, without anybody clearing them one by one:

* **Which.**  A batch that stands exhausted with a systemic last reason was
  exhausted by the system.  Its statements are read back from the WorkOrders it
  was asked in (the Scheduler keeps each prompt: ``UNTRUSTED_ITEMS``, the
  statement and the exact cited text), and a staged candidate is one of them
  when its statement and its citation binding's ``source_sha256`` match an
  item's.  Nothing is inferred from a hold's wording, a run summary or a time
  window.
* **Only open ones.**  A candidate a person has decided, or that is already in
  the Ledger, is left alone.
* **The same check.**  The statement goes through the same
  ``ClaimSupportVerifier`` (its ceiling, its hourly retry, its independence
  rule), keyed by the same item key, so a verdict the admission path or the
  backfill already stored is read, not paid for again.
* **The same door.**  A supported, about-the-subject verdict commits the very
  candidate that was staged, through ``commit_policy_candidate`` under the
  active policy -- the call admission makes.  Anything else stays held for a
  person, now with a verdict that says why.  The review is not reopened: its
  windows are keyed by the review's hash, so a reopen would re-draft (and pay
  for) every window and stage the same statements a second time beside the
  held ones.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from .claim_support_verification import (
    CONTRACT_REF,
    MAX_ATTEMPTS,
    MAX_CITED_CHARS,
    MAX_STATEMENT_CHARS,
    PURPOSE,
    VERIFIED_SOURCE_REFS,
    ClaimSupportVerifier,
    admissible,
    support_item,
    systemic_failure,
)

PASS_REF = "claim-support-recheck:systemic-failure:v1"
BINDING_PREFIX = "transcript-claim-citation-binding:"
_ITEMS_MARKER = "UNTRUSTED_ITEMS="
# Statements asked per run: two forward calls' worth.  The backlog is a few
# hundred at most and the purpose's own daily ceiling still applies.
MAX_ITEMS_PER_RUN = 24
# 2026-09-27: two statements about one subject, citing overlapping spans of
# one document, are the same Claim when their normalised texts agree this
# closely (difflib ratio, 0..1) and carry the same numbers and negations.
# Measured on legacy's 79 recheck commits of 2026-09-27: the restatements of a
# live Claim of the same span scored 0.86-0.92; the closest pair that says
# something different scored 0.77 (DXC's Q4 revenue decline vs. what it
# attributed the miss to).  0.85 sits in that gap.
NEAR_DUPLICATE_RATIO = 0.85
_NEGATIONS = frozenset({"not", "no", "never", "none", "nor", "neither", "without", "cannot"})
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def outage_items(connection: sqlite3.Connection, scheduler_db: str | Path, *,
                 purpose: str = PURPOSE, max_attempts: int = MAX_ATTEMPTS
                 ) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """Every statement of a batch a systemic failure exhausted, and the earliest exhaustion.

    Keyed by ``(sha256(statement), sha256(cited_text))`` as the prompt carried
    them.  The Scheduler is opened read-only; a batch with no WorkOrder left
    contributes nothing (it is simply not rechecked).
    """

    rows = connection.execute(
        "SELECT request_key, last_reason, updated_at FROM claim_support_attempts "
        "WHERE purpose=? AND attempts>=? AND instr(request_key, ':')=0",
        (purpose, max_attempts),
    ).fetchall()
    exhausted = [row for row in rows if systemic_failure(row["last_reason"])]
    if not exhausted:
        return {}, None
    since = min(row["updated_at"] for row in exhausted)
    path = Path(scheduler_db).resolve()
    found: dict[tuple[str, str], dict[str, Any]] = {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as scheduler:
        for row in exhausted:
            for (question,) in scheduler.execute(
                "SELECT json_extract(work_order_json, '$.question') FROM scheduler_work_orders "
                "WHERE work_order_id LIKE ? "
                "AND json_extract(work_order_json, '$.metadata.request_id') LIKE ?",
                (f"work:cockpit-{purpose}-%", f"{row['request_key']}:%"),
            ):
                if not isinstance(question, str) or _ITEMS_MARKER not in question:
                    continue
                try:
                    payload = json.loads(question[question.rindex(_ITEMS_MARKER) + len(_ITEMS_MARKER):])
                except ValueError:
                    continue
                for item in payload if isinstance(payload, list) else ():
                    if (isinstance(item, Mapping) and isinstance(item.get("statement"), str)
                            and isinstance(item.get("cited_text"), str)):
                        found[(_sha256(item["statement"]), _sha256(item["cited_text"]))] = dict(item)
    return found, since


class ClaimSupportRecheck:
    """One bounded pass over the candidates a systemic failure held.

    2026-09-26b.  The pass ordered the held candidates by creation and asked
    the first 24; a candidate the verifier had already answered "not
    supported" stayed open, so the same 24 came first every run and were
    answered from the verdict cache -- legacy asked 24 and admitted 0 twice,
    163 left and 139 of them never asked.  Now every candidate the pass asks is
    marked under the verifier's contract ref (``claim_support_recheck_marks``)
    and is not asked again under it, so each run starts where the last one
    stopped; a verdict under an earlier contract is not an answer, so the ones
    it rejected are asked once more under the current one.  And a statement,
    subject and cited source is committed once: a held candidate that repeats
    a Claim already in the Ledger, or another held candidate (the September
    re-drafts staged the same text twice), is marked ``duplicate`` and never
    committed.

    2026-09-27: "the same text" was too narrow.  Of the 63 legacy committed in
    its first hour after the deploy, several restated a live Claim of the same
    document in other words -- IBM's "RBC names key competitors ..." was
    committed a fifth time beside four live versions of one span.  A held
    candidate is now also a duplicate when a live Claim (or an earlier
    candidate of the run) has its subject, cites the same document with an
    overlapping span, and says the same thing: :func:`near_duplicate`.
    """

    def __init__(self, *, store: Any, reviewer: Any, verifier: ClaimSupportVerifier,
                 scheduler_db: str | Path, max_items: int = MAX_ITEMS_PER_RUN,
                 spool: Any = None) -> None:
        self.store = store
        self.connection = store.connection
        self.reviewer = reviewer
        self.verifier = verifier
        self.scheduler_db = scheduler_db
        self.max_items = int(max_items)
        self.spool = spool
        self.rule_ref = CONTRACT_REF
        self._texts: dict[str, str | None] = {}

    def _binding(self, evidence: Mapping[str, Any]) -> dict[str, Any] | None:
        ref = next((item.get("ref") for item in evidence.get("artifact_refs") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("ref", "")).startswith(BINDING_PREFIX)), None)
        if ref is None:
            return None
        row = self.connection.execute(
            "SELECT correction_set_version_ref, source_content_hash, source_start, source_end, "
            "record_json FROM transcript_claim_citation_bindings WHERE binding_id=?", (ref,)).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        correction = self.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
            (row["correction_set_version_ref"],)).fetchone()
        document_ref = None
        if correction is not None:
            document_ref = json.loads(correction["record_json"]).get("document_ref")
        return {"source_sha256": record.get("source_sha256"),
                "correction_set_version_ref": row["correction_set_version_ref"],
                "document_sha256": row["source_content_hash"],
                "start": row["source_start"], "end": row["source_end"],
                "document_ref": document_ref}

    def _producer_route(self, correction_ref: str) -> str | None:
        from .claim_review import _ROUTE_REF_RE

        row = self.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
            (correction_ref,)).fetchone()
        if row is None:
            return None
        rationale = (json.loads(row["record_json"]).get("raw_review") or {}).get("rationale")
        match = _ROUTE_REF_RE.search(rationale) if isinstance(rationale, str) else None
        return None if match is None else match.group(1)

    def _committed(self, candidate_ref: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM reviewed_candidate_commits WHERE candidate_claim_ref=?",
            (candidate_ref,)).fetchone() is not None

    def _source_text(self, digest: Any) -> str | None:
        if self.spool is None or not isinstance(digest, str):
            return None
        if digest not in self._texts:
            try:
                raw = self.spool.read_object(digest)
                text = raw.decode("utf-8") if hashlib.sha256(raw).hexdigest() == digest else None
            except Exception:  # noqa: BLE001 - unreadable: the prompt's own text is used
                text = None
            self._texts[digest] = text
        return self._texts[digest]

    def _ledger_identities(self, statements: list[str]) -> dict[tuple[Any, ...], str]:
        """identity -> claim version ref, for every Ledger Claim with one of these statements."""

        found: dict[tuple[Any, ...], str] = {}
        for start in range(0, len(statements), 400):
            part = statements[start:start + 400]
            rows = self.connection.execute(
                "SELECT c.claim_version_id AS ref, "
                "json_extract(c.claim_json,'$.normalized_statement') AS statement, "
                "json_extract(c.claim_json,'$.subject_ref') AS subject_ref, e.evidence_json AS evidence "
                "FROM claim_versions c "
                "JOIN evidence_relations r ON r.claim_version_id=c.claim_version_id AND r.relation='supports' "
                "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
                "WHERE json_extract(c.claim_json,'$.normalized_statement') IN "
                f"({','.join('?' for _ in part)})", part).fetchall()
            for row in rows:
                binding = self._binding(json.loads(row["evidence"]))
                if binding is None:
                    continue
                found.setdefault(_identity(row["subject_ref"], row["statement"], binding), row["ref"])
        return found

    def pending(self) -> tuple[list[dict[str, Any]], int]:
        """Every open, unmarked held candidate to ask again, oldest first, and how many.

        Each entry is ``{"candidate_ref", "item", "identity"}`` plus
        ``duplicate_of`` when the Ledger or an earlier entry already has its
        statement, subject and cited source.
        """

        items, _since = outage_items(self.connection, self.scheduler_db,
                                     purpose=self.verifier.purpose,
                                     max_attempts=self.verifier.max_attempts)
        if not items:
            return [], 0
        # Narrowed in SQL by the statements the batches asked about; the
        # digest match below is what decides.
        statements = sorted({item["statement"] for item in items.values()})
        rows = []
        for start in range(0, len(statements), 400):
            part = statements[start:start + 400]
            rows.extend(self.reviewer.connection.execute(
                "SELECT c.version_id, c.created_at FROM candidate_claim_versions c "
                "WHERE json_extract(c.record_json, '$.normalized_statement') IN "
                f"({','.join('?' for _ in part)}) "
                "AND NOT EXISTS (SELECT 1 FROM human_review_decisions d "
                "  WHERE d.candidate_claim_version_ref=c.version_id)", part).fetchall())
        rows.sort(key=lambda row: (row["created_at"], row["version_id"]))
        marked = self.verifier.records.recheck_marks(self.rule_ref)
        ledger = self._ledger_identities(statements)
        seen: dict[tuple[Any, ...], str] = {}
        found: list[dict[str, Any]] = []
        for row in rows:
            candidate_ref = row["version_id"]
            if candidate_ref in marked:
                continue
            bundle = self.reviewer.candidate_bundle(candidate_ref)
            claim, evidence = bundle["claim"], bundle["evidence"]
            if (claim.get("claim_kind") != "qualitative"
                    or evidence.get("source_ref") not in VERIFIED_SOURCE_REFS):
                continue
            binding = self._binding(evidence)
            statement = str(claim.get("normalized_statement") or "")
            if binding is None or not statement:
                continue
            item = items.get((_sha256(statement[:MAX_STATEMENT_CHARS]), binding["source_sha256"]))
            if item is None:
                # Not a statement an exhausted batch asked about.  (A cited
                # text the prompt cut cannot match the binding's digest.)
                continue
            if self._committed(candidate_ref):
                continue
            identity = _identity(claim["subject_ref"], statement, binding)
            entry = {"candidate_ref": candidate_ref, "identity": identity,
                     "subject_ref": claim["subject_ref"], "statement": statement,
                     "binding": binding,
                     "item": self._item(claim, evidence, binding, item)}
            duplicate_of = ledger.get(identity) or seen.get(identity)
            if duplicate_of is not None:
                entry["duplicate_of"] = duplicate_of
            else:
                seen[identity] = candidate_ref
            found.append(entry)
        self._mark_near_duplicates(found)
        return found, len(found)

    def _mark_near_duplicates(self, found: list[dict[str, Any]]) -> None:
        """A restatement of a live Claim, or of an earlier entry, of the same span is a duplicate.

        In order, oldest first: an entry is checked against the live Claims
        citing its document and against every earlier entry that is not
        itself a duplicate, and only where the subject is the same and the
        cited spans overlap (:func:`spans_overlap`, :func:`near_duplicate`).
        """

        open_entries = [entry for entry in found if "duplicate_of" not in entry]
        if not open_entries:
            return
        ledger = live_claims_citing(self.connection, {
            str(entry["binding"].get("document_sha256")) for entry in open_entries
            if entry["binding"].get("document_sha256")})
        kept: list[dict[str, Any]] = []
        for entry in found:
            if "duplicate_of" in entry:
                continue
            document = entry["binding"].get("document_sha256")
            others = [(row["ref"], row) for row in ledger.get(document, ())] + \
                [(other["candidate_ref"], other) for other in kept]
            match = None
            for ref, other in others:
                if other["subject_ref"] != entry["subject_ref"] \
                        or not spans_overlap(entry["binding"], other["binding"]):
                    continue
                ratio = near_duplicate(entry["statement"], other["statement"])
                if ratio is not None and (match is None or ratio > match[1]):
                    match = (ref, ratio)
            if match is None:
                kept.append(entry)
                continue
            entry["duplicate_of"], entry["similarity"] = match

    def _item(self, claim: Mapping[str, Any], evidence: Mapping[str, Any],
              binding: Mapping[str, Any], asked: Mapping[str, Any]) -> dict[str, Any]:
        """The current contract's question: whole sentences and document facts when the original reads."""

        from .claim_support_context import cited_passage, document_facts

        cited, speaker = asked["cited_text"], None
        text = self._source_text(binding.get("document_sha256"))
        start, end = binding.get("start"), binding.get("end")
        if text is not None and isinstance(start, int) and isinstance(end, int) \
                and 0 <= start < end <= len(text):
            passage = cited_passage(text, start, end, max_chars=MAX_CITED_CHARS)
            cited, speaker = passage["cited_text"], passage["speaker"]
        try:
            facts = document_facts(
                self.connection, document_ref=binding.get("document_ref"),
                source_envelope_ref=evidence.get("source_envelope_ref"), spool=self.spool,
                period=claim.get("period"), speaker=speaker)
        except Exception:  # noqa: BLE001 - facts are context, never a gate
            facts = {}
        return support_item(
            subject_ref=claim["subject_ref"], subject_name=asked.get("subject"),
            statement=str(claim["normalized_statement"]), cited_text=cited, document=facts,
            producer_route_ref=self._producer_route(binding["correction_set_version_ref"]))

    def _mark(self, candidate_ref: str, outcome: str, *, item_key: str | None = None,
              detail: str | None = None) -> None:
        self.verifier.records.recheck_mark(
            candidate_claim_ref=candidate_ref, rule_ref=self.rule_ref, outcome=outcome,
            item_key=item_key, detail=detail)

    def run_once(self, *, mission: Mapping[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {"pass_ref": PASS_REF, "rule_ref": self.rule_ref,
                                   "status": "idle", "remaining_before": 0,
                                   "asked": 0, "admitted": [], "still_held": 0,
                                   "duplicates": [], "deferred": None, "calls": 0,
                                   "cost_micros": 0}
        entries, summary["remaining_before"] = self.pending()
        for entry in entries:
            if "duplicate_of" in entry:
                if "similarity" in entry:
                    detail = (f"restates {entry['duplicate_of']} (similarity {entry['similarity']}): "
                              "same subject, same document, overlapping cited span")
                else:
                    detail = f"same statement, subject and cited source as {entry['duplicate_of']}"
                self._mark(entry["candidate_ref"], "duplicate", detail=detail)
                summary["duplicates"].append({"candidate_claim_ref": entry["candidate_ref"],
                                              "duplicate_of": entry["duplicate_of"],
                                              **({"similarity": entry["similarity"]}
                                                 if "similarity" in entry else {})})
        pending = [entry for entry in entries if "duplicate_of" not in entry][:self.max_items]
        summary["remaining_after_marks"] = max(
            0, summary["remaining_before"] - len(summary["duplicates"]) - len(pending))
        if not pending:
            if summary["duplicates"]:
                summary["status"] = "examined"
            return summary
        summary["asked"] = len(pending)
        outcome = self.verifier.verify(mission=mission, items=[entry["item"] for entry in pending])
        summary["calls"] = outcome.get("calls", 0)
        summary["cost_micros"] = outcome.get("cost_micros", 0)
        if outcome["status"] == "deferred":
            summary["deferred"] = outcome.get("reason")
        committed: set[tuple[Any, ...]] = set()
        for entry in pending:
            candidate_ref, item = entry["candidate_ref"], entry["item"]
            verdict = outcome["verdicts"].get(item["item_key"])
            if verdict is None:
                if item["item_key"] in outcome["unverifiable"]:
                    self._mark(candidate_ref, "unverifiable", item_key=item["item_key"],
                               detail=outcome["unverifiable"][item["item_key"]])
                    summary["still_held"] += 1
                continue
            if not admissible(verdict):
                self._mark(candidate_ref, "verdict", item_key=item["item_key"])
                summary["still_held"] += 1
                continue
            if entry["identity"] in committed:  # never twice, even within one run
                self._mark(candidate_ref, "duplicate", item_key=item["item_key"],
                           detail="committed earlier in this run")
                continue
            detail = None
            try:
                promotion = self.store.commit_policy_candidate(
                    **self.reviewer.candidate_authority_bundle(candidate_ref),
                    idempotency_key=f"policy-ledger:{PASS_REF}:{candidate_ref}")
                if promotion.get("status") == "conflict":
                    raise RuntimeError("the policy commit conflicted")
            except Exception as exc:  # noqa: BLE001 - one refusal must not stop the rest
                detail = f"{type(exc).__name__}: {exc}"[:300]
                summary.setdefault("refused", []).append(
                    {"candidate_claim_ref": candidate_ref, "reason": detail})
            self._mark(candidate_ref, "verdict", item_key=item["item_key"], detail=detail)
            if detail is not None:
                continue
            committed.add(entry["identity"])
            summary["admitted"].append({
                "candidate_claim_ref": candidate_ref,
                "status": promotion.get("status"),
                "claim_version_ref": promotion.get("claim_version_ref")})
        summary["status"] = ("admitted" if summary["admitted"] else
                             "deferred" if summary["deferred"] else "examined")
        return summary


def _normalized(statement: str) -> str:
    text = unicodedata.normalize("NFKC", statement).casefold()
    text = re.sub("n['\u2019]t\\b", " not", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _numbers(statement: str) -> list[str]:
    text = unicodedata.normalize("NFKC", statement)
    return sorted(value.replace(",", "") for value in _NUMBER_RE.findall(text))


def near_duplicate(first: str, second: str) -> float | None:
    """The similarity of two statements when they say the same thing, else None.

    Deterministic and deliberately narrow: the normalised texts (NFKC,
    case-folded, punctuation dropped, "n't" read as "not") must match at
    ``NEAR_DUPLICATE_RATIO`` or better, *and* carry exactly the same numbers
    and the same number of negations -- so "grew 5%" never folds into "grew
    7%", nor "expects" into "does not expect", however alike they read.
    Whether the two cite the same span is the caller's question.
    """

    if _numbers(first) != _numbers(second):
        return None
    a, b = _normalized(first), _normalized(second)
    if sum(word in _NEGATIONS for word in a.split()) != sum(word in _NEGATIONS for word in b.split()):
        return None
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return round(ratio, 3) if ratio >= NEAR_DUPLICATE_RATIO else None


def spans_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Both bindings cite the same document and their spans share a character."""

    if not first.get("document_sha256") or first.get("document_sha256") != second.get("document_sha256"):
        return False
    try:
        return int(first["start"]) < int(second["end"]) and int(second["start"]) < int(first["end"])
    except (KeyError, TypeError, ValueError):
        return False


def live_claims_citing(connection: sqlite3.Connection,
                       document_hashes: set[str]) -> dict[str, list[dict[str, Any]]]:
    """document sha256 -> every live (not retired) Claim citing it: ref, subject, statement, span.

    Read through the citation bindings the Claims' supporting evidence names,
    which is the chain the recheck itself reads a candidate's binding from.
    """

    from .claim_retirement import retired_claim_version_refs

    if not document_hashes:
        return {}
    bindings: dict[str, tuple[str, int, int]] = {}
    hashes = sorted(document_hashes)
    for start in range(0, len(hashes), 400):
        part = hashes[start:start + 400]
        for row in connection.execute(
            "SELECT binding_id, source_content_hash, source_start, source_end "
            "FROM transcript_claim_citation_bindings WHERE source_content_hash IN "
            f"({','.join('?' for _ in part)})", part).fetchall():
            bindings[row[0]] = (row[1], row[2], row[3])
    if not bindings:
        return {}
    retired = retired_claim_version_refs(connection)
    found: dict[str, list[dict[str, Any]]] = {}
    for ref, evidence, claim in connection.execute(
        "SELECT r.claim_version_id, e.evidence_json, c.claim_json FROM evidence_relations r "
        "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
        "JOIN claim_versions c ON c.claim_version_id=r.claim_version_id "
        "WHERE r.relation='supports' AND instr(e.evidence_json, ?)>0", (BINDING_PREFIX,),
    ).fetchall():
        if ref in retired:
            continue
        record = json.loads(evidence)
        binding_ref = next((item.get("ref") for item in record.get("artifact_refs") or ()
                            if isinstance(item, Mapping) and item.get("ref") in bindings), None)
        if binding_ref is None:
            continue
        document, start, end = bindings[binding_ref]
        body = json.loads(claim)
        found.setdefault(document, []).append({
            "ref": ref, "subject_ref": body.get("subject_ref"),
            "statement": str(body.get("normalized_statement") or ""),
            "binding": {"document_sha256": document, "start": start, "end": end}})
    return found


def _identity(subject_ref: Any, statement: str, binding: Mapping[str, Any]) -> tuple[Any, ...]:
    """One Claim per statement, subject and cited source: the document and the span in it."""

    return (subject_ref, _sha256(statement), binding.get("document_sha256"),
            binding.get("start"), binding.get("end"))


__all__ = ["ClaimSupportRecheck", "MAX_ITEMS_PER_RUN", "NEAR_DUPLICATE_RATIO", "PASS_REF",
           "live_claims_citing", "near_duplicate", "outage_items", "spans_overlap"]
