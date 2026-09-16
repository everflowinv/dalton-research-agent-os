"""Schema changes that are neither a table nor a column: indexes.

Dalton has no numbered migration ledger and does not want one.  A packaged
``*_schema.sql`` is idempotent and is re-applied by its authority's
constructor, and an ``ALTER``-and-rebuild upgrade is a guarded private method
on that same constructor.  Both run at open, inside the process that is about
to use the database, and both are allowed to fail the open: a table that is
not there is not a database this code can work with.

An index is different in exactly one way, and the difference is why this
package exists.  A missing index is a slow query, not a wrong one, so
building one must never be able to fail a writer start.  On the live Core the
builds measure between 0.01 s and 1.2 s, which is nothing -- but they need a
write lock on a database a live writer holds, and a lock wait that outlives
the busy timeout raises ``database is locked``.  Putting these statements in
``schema.sql`` would therefore trade a slow projection for a writer that will
not start, which is a bad trade at any speed.

So: declared here, applied out of band, idempotent, and never fatal.  The
same statements are runnable from the command line, which is what an operator
does in a quiet window before a deploy.
"""

from .projection_indexes import (
    CORE_PROJECTION_INDEXES,
    OPEN_BUDGET_SECONDS,
    SCHEDULER_PROJECTION_INDEXES,
    IndexSpec,
    apply_projection_indexes,
    ensure_indexes,
)

__all__ = [
    "CORE_PROJECTION_INDEXES",
    "OPEN_BUDGET_SECONDS",
    "SCHEDULER_PROJECTION_INDEXES",
    "IndexSpec",
    "apply_projection_indexes",
    "ensure_indexes",
]
