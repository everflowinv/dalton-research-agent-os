"""P17d: what kind of failure this was, so a lane stops counting the wrong ones.

Task 62 of the predecessor project asked AlphaEngine's desktop module page for
a Linde read-across three times, got ``status=no_module_page`` three times, and
was permanently failed.  Nothing was wrong with the task.  The desktop session
was down, and the lane's failure budget -- "three strikes and hold" -- cannot
tell a broken dependency from a piece of work that cannot be done.  Every lane
in this repo carries its own copy of that budget (four counted, ten keyed on an
input digest, six on a clock), and every one of them makes the same mistake.

So a failure gets a **class** before it gets counted:

``dependency_unavailable``
    The source, the desktop session, the quota, the transport or the writer RPC
    was not there.  This is not the work item's fault and must not consume its
    budget indefinitely.  The item is **parked** against the named dependency
    and resumes when a later probe of that dependency succeeds -- the P14e
    hold/resume shape, generalised from the writer RPC to every dependency a
    lane names.
``content_refused``
    The source answered and the answer is unusable: unreadable bytes, an empty
    document, a hash that does not match, a refusal from a verifier.  Retrying
    reads the same bytes again.  **Terminal**, and it says so once.
``transient``
    Everything else, which is what a bounded retry was always for.  Unknown
    reasons land here **with their raw text preserved**, because the failure
    mode this module exists to prevent is a classifier that silently invents a
    terminal verdict for a sentence nobody taught it.

Two properties are load-bearing.

**Classification reads what the lanes already say.**  There is no new model
call and no new field for a lane to fill in.  The rules below match the status
words and reason strings the lanes emit today -- ``no_module_page``,
``probe_transport_unavailable:RuntimeError``, ``quota_exhausted``,
``f"{type(exc).__name__}: {exc}"``, and the Chinese prose two lanes write.
A lane that says something this table has never seen gets ``transient``.

**Park and resume are append-only and replayable.**  The in-process budget here
is a cache; the truth is the event log in :mod:`lane_failure_ledger`, and
"what is parked right now" is a fold over it.  A restart replays; it does not
guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
CONTENT_REFUSED = "content_refused"
NOT_PERMITTED = "not_permitted"
TRANSIENT = "transient"

FAILURE_CLASSES: tuple[str, ...] = (
    DEPENDENCY_UNAVAILABLE, CONTENT_REFUSED, NOT_PERMITTED, TRANSIENT,
)

# How much of a reason string is kept.  The lanes already truncate at 500
# (``MAX_FAILURE_DETAIL_CHARS``); this is the same number so that a reason does
# not change length by passing through here.
MAX_REASON_CHARS = 500

# The default bounded retry, unchanged from what every counted-budget lane
# already used.  Only ``transient`` failures spend it.
DEFAULT_MAX_TRANSIENT_FAILURES = 3

# How often a parked dependency is probed again.  Parking must not become the
# permanent suspension it exists to prevent: an item parked on a dead source is
# still admitted, just rarely, and the run that is admitted **is** the probe --
# if it succeeds, everything parked on that dependency resumes with it.
#
# Thirty minutes is the statements lane's own ``CONFIGURATION_HOLD_SECONDS``,
# picked there for the same reason: long enough that a broken source is not
# asked every five minutes, short enough that a fixed source is noticed inside
# one working session.
#
# The first attempt after a park is free.  That is P14e's shape exactly: the
# writer RPC that failed put the round on hold and *the next tick picked it up
# again*.  Only a dependency that fails its free probe as well goes into the
# interval.
PARK_PROBE_INTERVAL_SECONDS = 1800

UNKNOWN_DEPENDENCY = "unknown"


@dataclass(frozen=True)
class Classification:
    """One failure, classified, with the sentence it was classified from."""

    failure_class: str
    reason: str
    rule: str
    dependency: str | None = None
    status: str | None = None

    @property
    def terminal(self) -> bool:
        return self.failure_class == CONTENT_REFUSED

    @property
    def parks(self) -> bool:
        return self.failure_class == DEPENDENCY_UNAVAILABLE

    @property
    def awaits_permission(self) -> bool:
        return self.failure_class == NOT_PERMITTED

    def as_wire(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "reason": self.reason,
            "rule": self.rule,
            "dependency": self.dependency,
            "status": self.status,
        }


@dataclass(frozen=True)
class Rule:
    """One substring the lanes emit, and what it means.

    ``pattern`` is matched case-insensitively against ``"<status> <reason>"``.
    ``dependency`` is only meaningful for ``dependency_unavailable``; it is the
    name the parked item waits on and the name a later probe clears.
    """

    rule_id: str
    pattern: str
    failure_class: str
    dependency: str | None = None

    def matches(self, text: str) -> bool:
        return self.pattern in text


# Order matters and is the whole design.  ``budget_refused`` must be read as a
# quota before ``refused`` is read as a refusal; ``unreadable_last_run`` (the
# lane could not read its *own* bookkeeping) must be read as transient before
# ``unreadable`` is read as unusable content.  First match wins, so the
# narrower sentence is written first.
#
# A rule matches **outage vocabulary**, never a vendor's name on its own.
# "MarketDataAdapterError: Yahoo has no such ticker" names Yahoo and is not an
# outage; classifying it as one because the word "Yahoo" is in it would park a
# company forever on a source that is working.  Vendor names are used only to
# *name* a dependency once a rule has already decided there is one; that table
# is :data:`VENDORS`.
RULES: tuple[Rule, ...] = (
    # -- things that are not failures of the item, read before anything else --
    # Guidepoint could not read the record of its own last run.  Nothing is
    # wrong with the query and nothing is wrong with Guidepoint.
    Rule("own_bookkeeping_unreadable", "unreadable_last_run", TRANSIENT),
    Rule("ticket_missing", "ticket is missing", TRANSIENT),
    Rule("ticket_gone", "lane ticket is no longer on disk", TRANSIENT),
    Rule("child_busy", "child_slot_busy", TRANSIENT),
    Rule("child_conflict", "lanechildconflict", TRANSIENT),

    # Governance is a human/configuration boundary. Repeating the same work
    # cannot grant it and must consume neither a retry nor model budget.
    Rule("mission_not_granted", "gated:mission does not grant", NOT_PERMITTED),
    Rule("policy_not_listed", "gated:active governance policy does not list", NOT_PERMITTED),
    Rule("governance_not_approved", "gated:governance", NOT_PERMITTED),
    Rule("permission_denied", "gated:not permitted", NOT_PERMITTED),

    # -- dependency: quota and budget -------------------------------------
    Rule("pool_exhausted", "pool_exhausted", DEPENDENCY_UNAVAILABLE, "model_budget"),
    Rule("budget_refused", "budget_refused", DEPENDENCY_UNAVAILABLE, "model_budget"),
    Rule("budget_rejected", "budget_rejected", DEPENDENCY_UNAVAILABLE, "model_budget"),
    Rule("reservation_overrun", "model_reservation_overrun",
         DEPENDENCY_UNAVAILABLE, "model_budget"),
    Rule("quota_exhausted", "quota_exhausted", DEPENDENCY_UNAVAILABLE, "quota"),
    Rule("quota_exceeded", "quota exceeded", DEPENDENCY_UNAVAILABLE, "quota"),
    Rule("rate_limited", "rate_limit", DEPENDENCY_UNAVAILABLE, "quota"),
    Rule("too_many_requests", "429 too many requests",
         DEPENDENCY_UNAVAILABLE, "quota"),
    Rule("broker_capacity_recovery_exhausted", "capacity_recovery_exhausted", TRANSIENT),
    Rule("broker_capacity_busy", "capacity_busy",
         DEPENDENCY_UNAVAILABLE, "model_capacity"),

    # -- dependency: the AlphaEngine desktop session, which is Task 62 ------
    Rule("alphaengine_no_module_page", "no_module_page",
         DEPENDENCY_UNAVAILABLE, "alphaengine_desktop"),
    Rule("desktop_unreachable", "desktop status",
         DEPENDENCY_UNAVAILABLE, "alphaengine_desktop"),
    Rule("desktop_session", "desktop session",
         DEPENDENCY_UNAVAILABLE, "alphaengine_desktop"),
    Rule("not_signed_in", "not_signed_in",
         DEPENDENCY_UNAVAILABLE, "alphaengine_desktop"),
    Rule("session_expired", "session_expired",
         DEPENDENCY_UNAVAILABLE, "alphaengine_desktop"),

    # -- dependency: transport, writer RPC, spool --------------------------
    Rule("probe_transport", "probe_transport_unavailable",
         DEPENDENCY_UNAVAILABLE, "writer_rpc"),
    Rule("writer_unavailable", "writer_unavailable",
         DEPENDENCY_UNAVAILABLE, "writer_rpc"),
    Rule("spool_capacity", "spool_capacity", DEPENDENCY_UNAVAILABLE, "transport"),
    Rule("transport_unavailable", "transport_unavailable",
         DEPENDENCY_UNAVAILABLE, "transport"),
    Rule("connector_error", "connectorerror", DEPENDENCY_UNAVAILABLE, None),
    Rule("transport_error", "transporterror", DEPENDENCY_UNAVAILABLE, None),
    Rule("connection_error", "connectionerror", DEPENDENCY_UNAVAILABLE, None),
    Rule("timeout_error", "timeouterror", DEPENDENCY_UNAVAILABLE, None),
    Rule("timeout_expired", "timeout expired", DEPENDENCY_UNAVAILABLE, None),
    Rule("url_error", "urlerror", DEPENDENCY_UNAVAILABLE, None),
    Rule("http_error", "httperror", DEPENDENCY_UNAVAILABLE, None),
    Rule("remote_disconnected", "remotedisconnected",
         DEPENDENCY_UNAVAILABLE, None),
    Rule("lease_expired", "lease_expired_before_transport",
         DEPENDENCY_UNAVAILABLE, "transport"),

    # -- dependency: a source that is simply not there ---------------------
    Rule("source_unavailable", "source_unavailable", DEPENDENCY_UNAVAILABLE, None),
    Rule("vendor_unavailable", "vendor_unavailable", DEPENDENCY_UNAVAILABLE, None),
    Rule("not_connected", "is not connected in this mission",
         DEPENDENCY_UNAVAILABLE, None),
    # mission_stage: "<source> 还没有接入，这项拿不到" -- not wired up yet.
    Rule("not_wired_cn", "还没有接入", DEPENDENCY_UNAVAILABLE, None),
    Rule("not_attached", "not_attached", DEPENDENCY_UNAVAILABLE, None),
    Rule("openclaw_unavailable", "openclaw_executable_unavailable",
         DEPENDENCY_UNAVAILABLE, "openclaw"),
    Rule("model_unavailable", "model_unavailable", DEPENDENCY_UNAVAILABLE, "model"),
    Rule("interpreter_unavailable", "interpreter_unavailable",
         DEPENDENCY_UNAVAILABLE, "model"),

    # -- content the source did return and nobody can use -------------------
    Rule("content_refused", "content_refused", CONTENT_REFUSED),
    Rule("unreadable", "unreadable", CONTENT_REFUSED),
    # mission_sec_quarters: "原始件读不到或哈希不符，不据此排队"
    Rule("unreadable_cn", "读不到", CONTENT_REFUSED),
    Rule("hash_mismatch", "hash_mismatch", CONTENT_REFUSED),
    Rule("hash_mismatch_cn", "哈希不符", CONTENT_REFUSED),
    Rule("empty_document", "empty_document", CONTENT_REFUSED),
    Rule("no_xbrl", "returned no filing with xbrl", CONTENT_REFUSED),
    Rule("no_filing", "no filing found for this company", CONTENT_REFUSED),
    Rule("unparseable", "unparseable", CONTENT_REFUSED),
    Rule("parse_error", "parse_error", CONTENT_REFUSED),
    Rule("unsupported_content_type", "unsupported_content_type", CONTENT_REFUSED),
    Rule("refused_prefix", "refused:", CONTENT_REFUSED),
    Rule("rubric_refused", "rubric_refused", CONTENT_REFUSED),
    Rule("constitution_refused", "constitution_refused", CONTENT_REFUSED),
    Rule("unresolvable_refs", "unresolvable_refs", CONTENT_REFUSED),
)

# Once a rule has decided there *is* a dependency and has not named it, the
# name comes from whichever of these the sentence mentions.  This table can be
# as loose as it likes: it never changes a class, only the label an operator
# reads on the ops backlog, and a wrong label costs a wrong heading rather than
# a lost work item.
VENDORS: tuple[tuple[str, str], ...] = (
    ("no_module_page", "alphaengine_desktop"),
    ("alphaengine", "alphaengine"),
    ("guidepoint", "guidepoint"),
    ("openclaw", "openclaw"),
    ("yfinance", "market_data"),
    ("yahoo", "market_data"),
    ("sec.gov", "sec"),
    ("edgar", "sec"),
    ("company facts", "sec"),
    ("writer", "writer_rpc"),
)

# A lane may name reasons only it emits.  Keyed by the lane's tick-summary
# driver key, so the table reads the way the cockpit reads.  These are consulted
# **before** :data:`RULES`, which is what lets a lane overrule a general word.
#
# Deliberately almost empty.  A lane-specific rule is a claim that this lane's
# vocabulary means something the shared table would get wrong, and every entry
# here is a place the two can drift apart; the general table is the one to grow.
LANE_RULES: dict[str, tuple[Rule, ...]] = {
    # P13: the statements lane already drew this line, as "these three markers
    # mean the company, everything else is this Core's own configuration". Its
    # markers name work that cannot be done, not a source that is down, so they
    # must not be read as an outage by the transport rules above.
    "mission_statements": (
        Rule("statements_no_lane_ticket", "carries no lane ticket", TRANSIENT),
    ),
}

# A lane that talks to exactly one outside source, so an anonymous outage in it
# has a better name than "unknown".  Keyed by tick-summary driver key.
LANE_DEPENDENCIES: dict[str, str] = {
    "mission_market_prices": "market_data",
    "mission_statements": "sec",
    "mission_sec_dispatch": "sec",
    "mission_ownership": "sec",
    "guidepoint": "guidepoint",
}

_WHITESPACE = re.compile(r"\s+")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", str(value)).strip()


def name_dependency(text: str, *, lane: str | None = None) -> str:
    """What an outage without a name should be filed under.

    Only reached once a rule has decided the failure *is* a dependency outage,
    so a loose match here cannot promote an ordinary failure -- it can only put
    an already-parked item under the wrong heading, which is a heading an
    operator can read past.  ``lane`` is the last resort: a lane that talks to
    exactly one source is a better answer than ``unknown``.
    """

    lowered = text.lower()
    for marker, dependency in VENDORS:
        if marker in lowered:
            return dependency
    return LANE_DEPENDENCIES.get(lane or "", UNKNOWN_DEPENDENCY)


def classify(
    reason: Any = None, *, status: Any = None, lane: str | None = None,
) -> Classification:
    """Classify one lane failure from what the lane already said.

    ``reason`` is the lane's own ``failure_reason`` / ``reason`` string;
    ``status`` is its status word if there is one.  Neither is invented and
    neither is required -- a failure with nothing said about it is
    ``transient``, which is what it was before this module existed.
    """

    reason_text = _text(reason)[:MAX_REASON_CHARS]
    status_text = _text(status)[:MAX_REASON_CHARS]
    haystack = f"{status_text} {reason_text}".lower()
    candidates = tuple(
        (f"{lane}:{rule.rule_id}", rule)
        for rule in LANE_RULES.get(lane or "", ())
    ) + tuple((rule.rule_id, rule) for rule in RULES)
    for rule_id, rule in candidates:
        if not rule.matches(haystack):
            continue
        dependency = rule.dependency
        if rule.failure_class == DEPENDENCY_UNAVAILABLE and dependency is None:
            dependency = name_dependency(haystack, lane=lane)
        return Classification(
            failure_class=rule.failure_class,
            reason=reason_text or status_text,
            rule=rule_id,
            dependency=dependency,
            status=status_text or None,
        )
    # Nothing matched.  The raw sentence is kept verbatim rather than replaced
    # by a class name: an operator reading the ops backlog needs the words the
    # lane used, and the next person to extend this table needs them more.
    return Classification(
        failure_class=TRANSIENT,
        reason=reason_text or status_text,
        rule="unmapped",
        dependency=None,
        status=status_text or None,
    )


def classify_settled(
    settled: Mapping[str, Any] | None, *, lane: str | None = None,
) -> Classification:
    """Classify a settled lane-child ticket the way the lanes read one.

    The fallback chain is the one four lanes wrote out by hand: the child's own
    ``failure_reason``, else ``"last run: <status>"``.
    """

    record = settled or {}
    reason = record.get("failure_reason")
    status = record.get("status")
    if not reason:
        reason = f"last run: {status}"
    return classify(reason, status=status, lane=lane)


@dataclass(frozen=True)
class BudgetDecision:
    """What the budget did with one failure, and what the lane should say."""

    action: str                 # "retry" | "held" | "parked" | "terminal"
    classification: Classification
    failures: int = 0

    @property
    def blocks(self) -> bool:
        return self.action in {"held", "parked", "terminal", "not_permitted"}

    def as_wire(self) -> dict[str, Any]:
        return {
            "action": self.action, "failures": self.failures,
            **self.classification.as_wire(),
        }


class LaneFailureBudget:
    """One lane's failure budget, with the three classes kept apart.

    Replaces the ``self._failures: dict[str, int]`` /
    ``self._failure_reason: dict[str, str]`` pair four lanes were each carrying.
    The counted part is unchanged -- three ``transient`` failures and the item
    is ``held`` -- and the two new parts are:

    * a ``dependency_unavailable`` failure does not increment the count.  The
      item is parked against the dependency it named and it stays parked until
      that dependency answers, however long that is.  A budget cannot expire
      against a source that is down, which is the whole point.
    * a ``content_refused`` failure is terminal on the first one.  Two more
      attempts at bytes that did not parse are two more attempts at the same
      bytes.

    ``ledger`` is optional.  With one, every park, resume and terminal verdict
    is appended and the state here can be rebuilt from it; without one the lane
    behaves exactly as it does now, in-process and forgotten on restart.  A
    lane whose ledger write fails still runs: the bookkeeping is reported, not
    swallowed, and never fails the tick.
    """

    def __init__(
        self, lane: str, *,
        max_transient_failures: int = DEFAULT_MAX_TRANSIENT_FAILURES,
        probe_interval_seconds: int = PARK_PROBE_INTERVAL_SECONDS,
        ledger: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.lane = str(lane)
        self.max_transient_failures = int(max_transient_failures)
        self.probe_interval_seconds = int(probe_interval_seconds)
        self.ledger = ledger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._failures: dict[str, int] = {}
        self._reason: dict[str, str] = {}
        self._parked: dict[str, Classification] = {}
        self._terminal: dict[str, Classification] = {}
        self._not_permitted: dict[str, Classification] = {}
        # Per dependency: when it was last seen down, and whether the free
        # probe that follows a park has been spent.  Kept per dependency rather
        # than per item because a source is up or down for all of them, and
        # five parked companies must not mean five probes at a dead page.
        self._down_since: dict[str, datetime] = {}
        self._probe_spent: dict[str, bool] = {}
        self._ledger_status: str = "unused"

    # -- writing -----------------------------------------------------------

    def record(
        self, item_key: str, *, reason: Any = None, status: Any = None,
        classification: Classification | None = None,
    ) -> BudgetDecision:
        """Charge one failure to one work item and say what happens to it."""

        item = str(item_key)
        found = classification or classify(reason, status=status, lane=self.lane)
        if found.failure_class == NOT_PERMITTED:
            self._not_permitted[item] = found
            self._parked.pop(item, None)
            self._terminal.pop(item, None)
            self._failures.pop(item, None)
            self._reason[item] = found.reason
            self._append("not_permitted", item, found)
            return BudgetDecision("not_permitted", found, 0)
        if found.failure_class == CONTENT_REFUSED:
            self._terminal[item] = found
            self._parked.pop(item, None)
            self._failures.pop(item, None)
            self._reason[item] = found.reason
            self._append("terminal", item, found)
            return BudgetDecision("terminal", found, 0)
        if found.failure_class == DEPENDENCY_UNAVAILABLE:
            first = item not in self._parked
            dependency = found.dependency or UNKNOWN_DEPENDENCY
            self._parked[item] = found
            self._reason[item] = found.reason
            # The clock restarts on every park -- a source that just failed
            # again is not thirty minutes closer to being probed -- but the
            # free probe is *not* handed back.  Without that, a dependency
            # that fails its probe would be probed again next tick and the
            # interval would never take effect.
            self._down_since[dependency] = self.clock()
            self._probe_spent.setdefault(dependency, False)
            self._append("parked" if first else "parked_again", item, found)
            return BudgetDecision("parked", found, self._failures.get(item, 0))
        count = self._failures.get(item, 0) + 1
        self._failures[item] = count
        self._reason[item] = found.reason
        if count >= self.max_transient_failures:
            self._append("held", item, found)
            return BudgetDecision("held", found, count)
        return BudgetDecision("retry", found, count)

    def record_settled(
        self, item_key: str, settled: Mapping[str, Any] | None,
    ) -> BudgetDecision:
        """Charge the failure a settled child ticket describes."""

        return self.record(
            item_key, classification=classify_settled(settled, lane=self.lane))

    def clear(self, item_key: str) -> list[str]:
        """A run for this item succeeded.

        The item's transient count goes back to zero, exactly as before.  If it
        was parked, its dependency has just answered -- a successful run *is*
        the probe -- so every item of this lane parked on the same dependency
        resumes too.  A terminal verdict is not cleared by anything; it was a
        statement about content, and only new content can change it.
        """

        item = str(item_key)
        self._failures.pop(item, None)
        self._reason.pop(item, None)
        permission = self._not_permitted.pop(item, None)
        if permission is not None:
            self._append("permission_ok", item, permission)
        parked = self._parked.get(item)
        if parked is None:
            return []
        return self.dependency_answered(parked.dependency or UNKNOWN_DEPENDENCY)

    def retire(self, item_key: str, *, reason: str = "input_superseded") -> bool:
        """This work item is obsolete, without claiming its dependency recovered."""

        item = str(item_key)
        found = (self._parked.get(item) or self._not_permitted.get(item)
                 or self._terminal.get(item))
        if found is None and item not in self._failures:
            return False
        found = found or Classification(TRANSIENT, self._reason.get(item, ""), "held")
        self._append("superseded", item, Classification(
            found.failure_class, str(reason), "input_superseded", found.dependency,
            found.status))
        self._parked.pop(item, None)
        self._not_permitted.pop(item, None)
        self._terminal.pop(item, None)
        self._failures.pop(item, None)
        self._reason.pop(item, None)
        return True

    def dependency_answered(self, dependency: str) -> list[str]:
        """A probe of this dependency succeeded; resume everything waiting.

        This is the P14e shape.  There, a writer RPC that failed put the round
        on hold and the *next tick's* successful call resumed it.  Here the
        dependency is named, so any lane's successful read of that source is
        the probe, and every item parked on it comes back at once.
        """

        name = str(dependency or UNKNOWN_DEPENDENCY)
        resumed = self._release_dependency(name)
        if resumed or self.ledger is not None:
            self._append_dependency_ok(name, resumed)
        return resumed

    def _release_dependency(self, name: str) -> list[str]:
        """Rebuild recovery state without appending another historical event."""

        resumed = [
            item for item, found in self._parked.items()
            if (found.dependency or UNKNOWN_DEPENDENCY) == name
        ]
        for item in resumed:
            self._parked.pop(item, None)
            self._reason.pop(item, None)
        self._down_since.pop(name, None)
        self._probe_spent.pop(name, None)
        return sorted(resumed)

    # -- reading -----------------------------------------------------------

    def blocked(self, item_key: str) -> BudgetDecision | None:
        """Why this item must not be attempted this tick, or ``None``.

        The three answers are deliberately different words: ``terminal`` will
        never change, ``parked`` will change when a source comes back, and
        ``held`` will change on a deploy.  A lane that reported all three as
        "held" is the thing the Chem retrospective could not read.
        """

        item = str(item_key)
        found = self._not_permitted.get(item)
        if found is not None:
            return BudgetDecision("not_permitted", found, 0)
        found = self._terminal.get(item)
        if found is not None:
            return BudgetDecision("terminal", found, 0)
        found = self._parked.get(item)
        if found is not None:
            if self._probe_due(found.dependency or UNKNOWN_DEPENDENCY):
                # Admitted as this dependency's probe.  The attempt is a real
                # attempt: if it works the item is done and everything else
                # parked on the same dependency comes back with it, and if it
                # fails it parks again and the interval starts over.  A park
                # that could not be probed would be the permanent suspension
                # this class exists to abolish.
                return None
            return BudgetDecision("parked", found, self._failures.get(item, 0))
        count = self._failures.get(item, 0)
        if count >= self.max_transient_failures:
            return BudgetDecision(
                "held",
                Classification(TRANSIENT, self._reason.get(item, "repeated failures"),
                               "held", None, None),
                count,
            )
        return None

    def _probe_due(self, dependency: str) -> bool:
        """Whether this dependency may be asked again, and spend the answer."""

        if not self._probe_spent.get(dependency, False):
            self._probe_spent[dependency] = True
            return True
        since = self._down_since.get(dependency)
        if since is None:
            return True
        if (self.clock() - since).total_seconds() < self.probe_interval_seconds:
            return False
        self._down_since[dependency] = self.clock()
        return True

    def attempts(self, item_key: str) -> int:
        """How much of the transient budget this item has spent."""

        return self._failures.get(str(item_key), 0)

    def failure_reason(self, item_key: str, default: str = "repeated failures") -> str:
        return self._reason.get(str(item_key)) or default

    def parked_items(self) -> list[dict[str, Any]]:
        """Everything this lane is waiting on a dependency for."""

        rows = [
            {"lane": self.lane, "item_key": item,
             "dependency": found.dependency or UNKNOWN_DEPENDENCY,
             "reason": found.reason, "rule": found.rule}
            for item, found in self._parked.items()
        ]
        rows.sort(key=lambda row: (row["dependency"], row["item_key"]))
        return rows

    def terminal_items(self) -> list[dict[str, Any]]:
        rows = [
            {"lane": self.lane, "item_key": item, "reason": found.reason,
             "rule": found.rule}
            for item, found in self._terminal.items()
        ]
        rows.sort(key=lambda row: row["item_key"])
        return rows

    def permission_items(self) -> list[dict[str, Any]]:
        rows = [{"lane": self.lane, "item_key": item, "reason": found.reason,
                 "rule": found.rule} for item, found in self._not_permitted.items()]
        return sorted(rows, key=lambda row: row["item_key"])

    def dependencies(self) -> tuple[str, ...]:
        return tuple(sorted({
            found.dependency or UNKNOWN_DEPENDENCY
            for found in self._parked.values()
        }))

    @property
    def ledger_status(self) -> str:
        """What happened the last time this budget tried to write it down."""

        return self._ledger_status

    def summary(self) -> dict[str, Any]:
        """The block a lane puts in its tick result."""

        return {
            "lane": self.lane,
            "parked": len(self._parked),
            "terminal": len(self._terminal),
            "not_permitted": len(self._not_permitted),
            "held": sum(
                1 for count in self._failures.values()
                if count >= self.max_transient_failures
            ),
            "dependencies": list(self.dependencies()),
            "ledger": self._ledger_status,
        }

    # -- the append-only half ----------------------------------------------

    def _append(self, event: str, item: str, found: Classification) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.append_event(
                lane=self.lane, item_key=item, event=event,
                failure_class=found.failure_class,
                dependency=found.dependency, reason=found.reason,
                rule=found.rule, status=found.status, at=self.clock(),
            )
            self._ledger_status = "recorded"
        except Exception as exc:  # noqa: BLE001 - a bookkeeper never fails a tick
            self._ledger_status = f"unrecorded:{type(exc).__name__}"

    def _append_dependency_ok(self, dependency: str, resumed: Iterable[str]) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.append_dependency_ok(
                lane=self.lane, dependency=dependency,
                resumed=sorted(resumed), at=self.clock(),
            )
            self._ledger_status = "recorded"
        except Exception as exc:  # noqa: BLE001
            self._ledger_status = f"unrecorded:{type(exc).__name__}"

    # -- replay ------------------------------------------------------------

    def replay(self, events: Iterable[Mapping[str, Any]]) -> "LaneFailureBudget":
        """Rebuild parked and terminal state from the ledger's own events.

        Only the two durable classes are replayed.  The transient count is not:
        it was always in-process, its whole meaning is "since this writer
        started", and a restart is nearly always a deploy -- which is the most
        likely thing to have fixed whatever it was.
        """

        for row in events:
            if row.get("lane") != self.lane:
                continue
            event = row.get("event")
            item = str(row.get("item_key") or "")
            found = Classification(
                failure_class=str(row.get("failure_class") or TRANSIENT),
                reason=str(row.get("reason") or ""),
                rule=str(row.get("rule") or "replay"),
                dependency=row.get("dependency"),
                status=row.get("status"),
            )
            if event in {"parked", "parked_again"}:
                self._parked[item] = found
                self._reason[item] = found.reason
                # The probe allowance is not replayed: a writer that has just
                # restarted is the most likely thing to have fixed a
                # dependency, so the first tick after a restart probes.
                dependency = found.dependency or UNKNOWN_DEPENDENCY
                self._down_since[dependency] = self.clock()
                self._probe_spent[dependency] = False
            elif event == "terminal":
                self._terminal[item] = found
                self._parked.pop(item, None)
            elif event == "not_permitted":
                self._not_permitted[item] = found
                self._parked.pop(item, None)
                self._terminal.pop(item, None)
            elif event == "permission_ok":
                self._not_permitted.pop(item, None)
            elif event == "dependency_ok":
                self._release_dependency(
                    str(row.get("dependency") or UNKNOWN_DEPENDENCY))
            elif event == "resumed":
                self._parked.pop(item, None)
                self._reason.pop(item, None)
            elif event == "superseded":
                self._parked.pop(item, None)
                self._not_permitted.pop(item, None)
                self._terminal.pop(item, None)
                self._reason.pop(item, None)
        return self


__all__ = [
    "CONTENT_REFUSED",
    "DEFAULT_MAX_TRANSIENT_FAILURES",
    "DEPENDENCY_UNAVAILABLE",
    "NOT_PERMITTED",
    "FAILURE_CLASSES",
    "LANE_DEPENDENCIES",
    "LANE_RULES",
    "MAX_REASON_CHARS",
    "PARK_PROBE_INTERVAL_SECONDS",
    "RULES",
    "TRANSIENT",
    "UNKNOWN_DEPENDENCY",
    "VENDORS",
    "BudgetDecision",
    "Classification",
    "LaneFailureBudget",
    "Rule",
    "classify",
    "classify_settled",
    "name_dependency",
]
