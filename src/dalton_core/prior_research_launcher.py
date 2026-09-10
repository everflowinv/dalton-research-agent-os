"""W3: spawn the prior-research child, one document at a time.

A thin subclass of S1's ``FeedChildLauncher``, and thin on purpose. Everything
that makes a local feed safe -- the approved record checked before a slot is
spent, the one-child-at-a-time ticket, the content-addressed digest that makes
a re-run land on the same ticket rather than on a second child -- is the same
problem S1 already solved, and solving it a second time would mean two places
to get it wrong.

What differs is two argv flags and one refusal. The corpus root is a single
directory the owner declared through ``DALTON_PRIOR_RESEARCH_DIR``; there is no
index database beside it, because this feed's index is the ``manifest.json``
the owner writes in each company folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .feed_launcher import FeedChildLauncher
from .prior_research_core import (
    GET_OPERATION,
    LIST_OPERATION,
    SOURCE_REF,
)


class PriorResearchFeedLauncher(FeedChildLauncher):
    """The fund's own earlier work: a declared directory of company folders."""

    SOURCE_REF = SOURCE_REF
    LIST_OPERATION = LIST_OPERATION
    GET_OPERATION = GET_OPERATION
    TICKET_PREFIX = "prior-research-run"
    TICKETS_DIRNAME = "feed-acquisitions-prior-research"
    CHILD_MODULE = "dalton_core.prior_research_cli"

    def __init__(self, *, corpus_root: str | Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.corpus_root = Path(corpus_root).expanduser().resolve()

    def _feed_args(self) -> list[str]:
        return ["--corpus-root", str(self.corpus_root)]

    def _document_args(self, document_ref: str) -> dict[str, Any]:
        return {"document_id": document_ref}


__all__ = ["PriorResearchFeedLauncher"]
