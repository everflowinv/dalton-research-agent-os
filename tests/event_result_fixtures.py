"""Batch F fixtures: an evidence index written the way the indexer writes it.

``index_text`` stores one document through ``evidence_index.index_document``
(the same chunking, passage refs and company links production uses), so the
event-judgement tests read bodies, packs and passage refs exactly as the lane
does against a real state directory.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from dalton_core import evidence_index as ei
from dalton_core.evidence_corpus import DocumentBody

ACN = "company:sec-cik:0001467373"

#: event kind -> evidence-index kind of the document it carries.
INDEX_KIND = {"transcript": "transcript", "sales_note": "sales_note", "news": "web_page",
              "expert_excerpt": "expert_call", "filing": "sec_8k"}


class NamingContext:
    """The one ``CorpusContext`` method ``index_document`` calls: who a text names."""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.names = names or {ACN: r"Accenture|\bACN\b"}

    def companies_named(self, text: str) -> list[str]:
        return [ref for ref, pattern in self.names.items() if re.search(pattern, text)]


_COUNTER = {"n": 0}


def index_text(state_dir: Path | str, document_ref: str, kind: str, text: str, *, date: str,
               title: str | None = None, house: str | None = None, period: str | None = None,
               company: str | None = ACN, acquired_at: str = "2026-10-01T12:00:00+00:00",
               now: str | None = None) -> str:
    """Index one document; returns its ``document_ref``."""

    _COUNTER["n"] += 1
    body = DocumentBody(
        document_ref=document_ref, source_ref="source:test", kind=kind, text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), renderer="text", title=title, house=house,
        document_date=date, period=period, url=None, acquired_at=acquired_at,
        filed_company_refs=[company] if company else [], named_companies=[], metadata={})
    connection = ei.open_index(state_dir)
    try:
        epoch = ei._epoch(connection, "2026-01-01T00:00:00+00:00")
        connection.execute("BEGIN IMMEDIATE")
        ei.index_document(connection, NamingContext(), body, source_key=f"test:{_COUNTER['n']}:{document_ref}",
                          now=now or acquired_at, epoch=epoch)
        connection.execute("COMMIT")
    finally:
        connection.close()
    return document_ref


def index_event_body(state_dir: Path | str, kind: str, document_ref: str, *, date: str,
                     text: str | None = None) -> str:
    """A short body for a document event, so the judge reads it (batch F)."""

    return index_text(state_dir, document_ref, INDEX_KIND.get(kind, "research_other"),
                      text or (f"Accenture (ACN) document {document_ref}. Management said client demand "
                               "was steady and pricing was stable across regions this period. ") * 3,
                      date=date, title=f"Accenture {kind} {document_ref}")


__all__ = ["ACN", "INDEX_KIND", "NamingContext", "index_event_body", "index_text"]
