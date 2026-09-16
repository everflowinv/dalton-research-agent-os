"""One deterministic contract check, one repair call, then a refusal.

Every drafting lane in this repository asks a model for a closed JSON shape and
refuses the whole reply on any deviation.  That rule is right -- a reply that
invented a tag did not read the table, and the rows it happened to get right
came out of the same reply -- but it was written when the drafting models held
the shape.  They do not any more: a whole run is thrown away because one slot
carried five sentences where the cap is three, or because the reply carried a
``gaps`` key it was told not to send.  Nothing about the *evidence* was wrong.

So this module draws the line one step later.  A reply is checked
**deterministically** -- key sets, item counts, character caps, enumerations,
references against the exact set that was shown, and "one JSON object and
nothing else" -- and every violation is collected rather than the first one
raised.  That list, verbatim, is fed back to the *same* model once.  The repair
reply is checked by the same code.  If it still deviates the draft is refused
exactly as before, and the refusal now carries the list rather than one line.

Three properties this deliberately keeps:

* **One repair, not a loop.**  A model that cannot satisfy a contract it was
  just shown, with the violations enumerated, is not going to satisfy it on the
  fourth try; it is going to spend the run's budget finding out.
* **The repair is paid for out of the same run budget.**  The caller passes
  what is left and what a call reserves.  When the repair would not fit, the
  draft is refused for budget -- readably, with the numbers -- and no call is
  made.  A repair that silently overdrew the run bound would move the failure
  to the next unit rather than remove it.
* **No model substitution.**  The repair goes to the caller's own ``call``
  closure, which is the same purpose, the same configuration and the same
  ledger as the original.  This module never picks a model.

The checks are declarative because the three lanes that use them disagree about
everything except their shape.  A ``Contract`` names paths in a tiny language --
``slots[]``, ``slots[].sentences[].text``, ``bear.statement`` -- so the same
evaluator serves a dossier unit, a debate map and a model specification.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .store import content_hash

__all__ = [
    "Contract",
    "ContractRepairError",
    "MAX_ALLOWED_SHOWN",
    "MAX_REPAIR_PROMPT_BYTES",
    "MAX_VIOLATIONS_SHOWN",
    "RepairOutcome",
    "Violation",
    "build_repair_prompt",
    "check_contract",
    "repair_request_id",
    "run_with_contract_repair",
    "single_json_object",
    "violations_of",
]

# How many violations are put in front of the model.  A reply with forty
# violations is not a reply with a formatting problem, and listing forty of
# them costs prompt bytes that the repair needs for the reply itself.
MAX_VIOLATIONS_SHOWN = 12
# How many members of an allowed set are named when a reference misses it.
MAX_ALLOWED_SHOWN = 40
# The repair prompt is bounded on its own account: it is sent to the same
# router with the same input bound as the draft it repairs, and the draft was
# already near that bound or it would not have been this hard to satisfy.
MAX_REPAIR_PROMPT_BYTES = 24_000
# One.  Stated as a constant so the number is readable in a summary rather
# than implied by the shape of a loop.
MAX_REPAIR_ATTEMPTS = 1


class ContractRepairError(ValueError):
    """The repair machinery was asked for something it cannot do."""


@dataclass(frozen=True)
class Violation:
    """One deterministic deviation, at one path, against one named rule."""

    path: str
    rule: str
    detail: str

    def as_wire(self) -> dict[str, str]:
        return {"path": self.path, "rule": self.rule, "detail": self.detail}

    def line(self) -> str:
        return f"{self.path}: {self.detail} [{self.rule}]"


def _render(path: str) -> str:
    return path or "(root)"


def _walk(value: Any, path: str) -> list[tuple[str, Any]]:
    """Resolve one path into every concrete node it names.

    ``""`` is the root.  A segment ending in ``[]`` iterates a list; a node
    that is missing or of the wrong container kind simply yields nothing here,
    because the rule that owns that node reports it.  Silently skipping is
    correct only because *some* rule always owns it: a ``Contract`` with a
    ``keys`` entry for a path is what makes "absent" a violation.
    """

    found: list[tuple[str, Any]] = []

    def descend(node: Any, tokens: Sequence[str], shown: str) -> None:
        if not tokens:
            found.append((shown, node))
            return
        token, rest = tokens[0], tokens[1:]
        if token.endswith("[]"):
            key = token[:-2]
            if key:
                if not isinstance(node, Mapping) or key not in node:
                    return
                child = node[key]
                shown = f"{shown}.{key}" if shown else key
            else:
                child = node
            if not isinstance(child, list):
                return
            for index, item in enumerate(child):
                descend(item, rest, f"{shown}[{index}]")
            return
        if not isinstance(node, Mapping) or token not in node:
            return
        descend(node[token], rest, f"{shown}.{token}" if shown else token)

    descend(value, [] if path == "" else path.split("."), "")
    return found


@dataclass(frozen=True)
class Contract:
    """The deterministic half of a drafting contract, as data.

    Only the parts a model actually gets wrong are here.  Semantic rules --
    "a side with no claim behind it is not a side", "a derived line cannot
    claim a filed concept" -- stay in their own validators, which is why
    ``run_with_contract_repair`` also accepts the exception those raise and
    feeds its message back beside these.
    """

    name: str
    # path -> (required keys, optional keys)
    keys: Mapping[str, tuple[Collection[str], Collection[str]]] = field(
        default_factory=dict)
    # path -> the exact key sets a node may have.  Some contracts are a choice
    # between two closed shapes rather than a required set with optional
    # extras: a dossier slot is ``slot_id`` with *either* ``sentences`` or
    # ``unknown``, and a reply carrying both is the commonest violation there
    # is.  ``keys`` cannot say that; this can.
    shapes: Mapping[str, tuple[Collection[str], ...]] = field(default_factory=dict)
    max_items: Mapping[str, int] = field(default_factory=dict)
    min_items: Mapping[str, int] = field(default_factory=dict)
    max_chars: Mapping[str, int] = field(default_factory=dict)
    enums: Mapping[str, Collection[str]] = field(default_factory=dict)
    # path -> the exact set of reference ids that were shown
    allowed_refs: Mapping[str, Collection[str]] = field(default_factory=dict)
    nonempty: Collection[str] = ()

    def fingerprint(self) -> str:
        return content_hash({
            "name": self.name,
            "keys": {path: [sorted(required), sorted(optional)]
                     for path, (required, optional) in sorted(self.keys.items())},
            "shapes": {path: [sorted(shape) for shape in shapes]
                       for path, shapes in sorted(self.shapes.items())},
            "max_items": dict(sorted(self.max_items.items())),
            "min_items": dict(sorted(self.min_items.items())),
            "max_chars": dict(sorted(self.max_chars.items())),
            "enums": {path: sorted(allowed)
                      for path, allowed in sorted(self.enums.items())},
            "allowed_ref_paths": sorted(self.allowed_refs),
            "nonempty": sorted(self.nonempty),
        })


def single_json_object(text: Any) -> tuple[dict[str, Any] | None, list[Violation]]:
    """The one JSON object the contract asks for, or why there is not one.

    Fenced code and a leading apology are unwrapped, because "return raw JSON"
    is an instruction about the envelope and the reply inside a fence is still
    the reply.  Anything else -- an array, two objects, a truncated one -- is a
    violation with the parser's own message, which is the most specific thing
    anybody has about it.
    """

    from .cockpit_model import unwrap_json_object

    if not isinstance(text, str) or not text.strip():
        return None, [Violation("(root)", "single_json_object",
                                "the reply is empty")]
    value = unwrap_json_object(text)
    if value is None:
        body = text.strip()
        start, end = body.find("{"), body.rfind("}")
        detail = "the reply is not one JSON object"
        if start >= 0 and end > start:
            try:
                json.loads(body[start:end + 1])
            except json.JSONDecodeError as exc:
                detail = f"the reply is not valid JSON: {exc}"
        return None, [Violation("(root)", "single_json_object", detail)]
    return value, []


def check_contract(value: Any, contract: Contract) -> list[Violation]:
    """Every deterministic violation in one reply, not merely the first.

    Collecting them all is the whole point.  A model told "slots[0] has the
    wrong keys" fixes slots[0], returns a reply whose slots[3] is still over
    the sentence cap, and buys a second refusal.  A model told both fixes both.
    """

    found: list[Violation] = []
    for path, (required, optional) in contract.keys.items():
        nodes = _walk(value, path) if path else [("", value)]
        # "No node at this path" is a violation only where the path names one
        # node.  A path that iterates a list -- ``answers[].sentences[]`` --
        # naming nothing means the list was empty, and an empty list is the
        # right answer whenever every answer was ``unknown``.  Reporting it as
        # a missing object is how a clean reply acquires a violation it cannot
        # fix.
        if path and "[]" not in path and not nodes and required:
            found.append(Violation(_render(path), "keys",
                                   f"is missing; the contract needs {sorted(required)}"))
            continue
        for shown, node in nodes:
            if not isinstance(node, Mapping):
                found.append(Violation(_render(shown), "keys", "must be a JSON object"))
                continue
            present = set(node)
            missing = sorted(set(required) - present)
            unknown = sorted(present - set(required) - set(optional))
            if missing or unknown:
                parts = []
                if missing:
                    parts.append(f"missing {missing}")
                if unknown:
                    parts.append(f"unknown {unknown}")
                found.append(Violation(
                    _render(shown), "keys",
                    f"has keys {sorted(present)}; {' and '.join(parts)}; the "
                    f"contract is {sorted(set(required) | set(optional))}"))
    for path, shapes in contract.shapes.items():
        for shown, node in _walk(value, path):
            if not isinstance(node, Mapping):
                found.append(Violation(_render(shown), "shape", "must be a JSON object"))
                continue
            if any(set(node) == set(shape) for shape in shapes):
                continue
            allowed = " or ".join(str(sorted(shape)) for shape in shapes)
            found.append(Violation(
                _render(shown), "shape",
                f"has keys {sorted(node)}; the contract is exactly {allowed}"))
    for path, cap in contract.max_items.items():
        for shown, node in _walk(value, path):
            if isinstance(node, list) and len(node) > cap:
                found.append(Violation(
                    _render(shown), "max_items",
                    f"carries {len(node)} items; the cap is {cap}"))
    for path, floor in contract.min_items.items():
        for shown, node in _walk(value, path):
            if isinstance(node, list) and len(node) < floor:
                found.append(Violation(
                    _render(shown), "min_items",
                    f"carries {len(node)} items; at least {floor} is required"))
    for path, cap in contract.max_chars.items():
        for shown, node in _walk(value, path):
            if isinstance(node, str) and len(node) > cap:
                found.append(Violation(
                    _render(shown), "max_chars",
                    f"is {len(node)} characters long; the cap is {cap}, spaces "
                    "included"))
    for path, allowed in contract.enums.items():
        for shown, node in _walk(value, path):
            if node not in set(allowed):
                found.append(Violation(
                    _render(shown), "enum",
                    f"is {node!r}; the contract allows exactly "
                    + ", ".join(repr(item) for item in
                               sorted(allowed, key=lambda item: (item is None, str(item))))))
    for path, allowed in contract.allowed_refs.items():
        names = set(allowed)
        for shown, node in _walk(value, path):
            items = node if isinstance(node, list) else [node]
            for item in items:
                if item in names:
                    continue
                found.append(Violation(
                    _render(shown), "allowed_refs",
                    f"names {item!r}, which was not shown; choose only from "
                    f"{_allowed_sample(names)}"))
    for path in contract.nonempty:
        for shown, node in _walk(value, path):
            if isinstance(node, (str, list, dict)) and not node:
                found.append(Violation(_render(shown), "nonempty", "must not be empty"))
    return found


def _allowed_sample(names: Collection[str]) -> str:
    ordered = sorted(str(name) for name in names)
    if not ordered:
        return "(nothing was shown for this field)"
    if len(ordered) <= MAX_ALLOWED_SHOWN:
        return ", ".join(ordered)
    head = ", ".join(ordered[:MAX_ALLOWED_SHOWN])
    return f"{head} (and {len(ordered) - MAX_ALLOWED_SHOWN} more shown above)"


def violations_of(
    text: Any, contract: Contract | None, *, parse_error: BaseException | None = None,
) -> list[Violation]:
    """The deterministic list, with the semantic validator's own message last.

    The semantic message is kept even when the deterministic pass already found
    something.  The two say different things and the model needs both: "slots[2]
    is over the cap" does not tell it that the sentence it wrote cites a tag
    nobody showed it.
    """

    value, found = single_json_object(text)
    if value is not None and contract is not None:
        found = found + check_contract(value, contract)
    if parse_error is not None:
        message = str(parse_error).strip() or type(parse_error).__name__
        if all(item.detail != message for item in found):
            found = found + [Violation("(reply)", "validator", message)]
    return found


def repair_request_id(
    original_request_id: str, *, contract_name: str, violations: Sequence[Violation],
    attempt: int = 1,
) -> str:
    """A content-addressed identity for the repair call.

    Derived from what is being repaired rather than from a counter, so the same
    repair of the same reply replays instead of being bought twice, and two
    different repairs can never collide on one WorkOrder.
    """

    return "contract-repair:" + content_hash({
        "original_request_id": original_request_id,
        "contract": contract_name,
        "attempt": attempt,
        "violations": [item.as_wire() for item in violations],
    })[:32]


def build_repair_prompt(
    *,
    original_prompt: str,
    reply_text: Any,
    violations: Sequence[Violation],
    contract_reminder: str = "",
    context: str = "",
    max_bytes: int = MAX_REPAIR_PROMPT_BYTES,
) -> str:
    """What the model is shown to repair its own reply, bounded.

    The original prompt is *not* repeated.  It was already paid for once, it is
    what put the call near the router's input bound in the first place, and the
    model is not being asked to draft again -- it is being asked to return the
    same content in the shape it was given.  What it does get is the exact
    violation list, the caller's short reminder of the rules those violations
    are against, whatever bounded context makes the violations fixable (the
    allowed reference ids, usually), and its own previous reply.
    """

    if not violations:
        raise ContractRepairError("a repair prompt needs at least one violation")
    shown = list(violations)[:MAX_VIOLATIONS_SHOWN]
    listed = "\n".join(f"  {index + 1}. {item.line()}"
                       for index, item in enumerate(shown))
    if len(violations) > len(shown):
        listed += f"\n  ... and {len(violations) - len(shown)} more of the same kind"
    head = (
        "Your previous reply broke the fixed output contract. Repair it.\n\n"
        "Do not research again, do not add or drop content, do not change any "
        "judgement, figure, citation or wording that is not named below. Change "
        "only what the violations name, and return the whole answer.\n\n"
        "VIOLATIONS -- every one of these must be gone from your next reply:\n"
        f"{listed}\n\n"
    )
    if contract_reminder:
        head += f"CONTRACT:\n{contract_reminder.strip()}\n\n"
    if context:
        head += f"{context.strip()}\n\n"
    tail = (
        "Return one raw JSON object and nothing else: no prose, no explanation, "
        "no code fence, no key the contract does not name.\n\n"
        "YOUR PREVIOUS REPLY:\n"
    )
    body = reply_text if isinstance(reply_text, str) else json.dumps(
        reply_text, ensure_ascii=False, sort_keys=True)
    room = max_bytes - len((head + tail).encode("utf-8"))
    encoded = body.encode("utf-8")
    if room <= 0:
        # The rules and the violations are the part that cannot be cut: a
        # repair prompt without them is a redraft, and a redraft is a second
        # full-price call that was not authorized.
        raise ContractRepairError(
            "the repair contract and violation list alone exceed the repair "
            f"prompt bound of {max_bytes} bytes")
    if len(encoded) > room:
        marker = b"\n... (previous reply truncated here; return the whole answer)"
        encoded = encoded[:max(0, room - len(marker))]
        # Never split a UTF-8 sequence: a half character is not text.
        while encoded and (encoded[-1] & 0xC0) == 0x80:
            encoded = encoded[:-1]
        if encoded and (encoded[-1] & 0xC0) == 0xC0:
            encoded = encoded[:-1]
        body = encoded.decode("utf-8") + marker.decode("utf-8")
    return head + tail + body + "\n"


@dataclass
class RepairOutcome:
    """What one bounded draft-and-repair exchange produced.

    ``status`` is one of:

    ``ok``              the first reply satisfied the contract;
    ``repaired``        the repair reply satisfied it;
    ``refused``         it still did not, and the violations say how;
    ``budget_refused``  the repair would not fit the run's remaining budget;
    ``unavailable``     a call could not be made at all.
    """

    status: str
    value: Any = None
    violations: list[Violation] = field(default_factory=list)
    reason: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    cost_micros: int = 0
    error: BaseException | None = None

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"

    def wire_violations(self) -> list[dict[str, str]]:
        return [item.as_wire() for item in self.violations]

    def summary(self) -> dict[str, Any]:
        """The operator-readable shape a lane puts in its run summary."""

        return {
            "status": self.status,
            "repair_attempts": max(0, len(self.calls) - 1),
            "cost_micros": self.cost_micros,
            "violations": self.wire_violations()[:MAX_VIOLATIONS_SHOWN],
            "reason": self.reason,
        }


def _reason(violations: Sequence[Violation], *, repaired: bool) -> str:
    listed = "; ".join(item.line() for item in list(violations)[:3])
    prefix = ("the draft still broke its output contract after one repair"
              if repaired else "the draft broke its output contract")
    return f"{prefix}: {listed}"[:500]


def run_with_contract_repair(
    *,
    call: Callable[..., Mapping[str, Any]],
    parse: Callable[[str], Any],
    prompt: str,
    request_id: str,
    contract: Contract | None = None,
    refusal_errors: tuple[type[BaseException], ...] = (),
    passthrough_errors: tuple[type[BaseException], ...] = (),
    unavailable_errors: tuple[type[BaseException], ...] = (),
    contract_reminder: str = "",
    repair_context: str = "",
    budget_remaining_micros: int | None = None,
    repair_reserve_micros: int = 0,
    max_repair_prompt_bytes: int = MAX_REPAIR_PROMPT_BYTES,
) -> RepairOutcome:
    """Draft once, repair at most once, then refuse with the whole list.

    ``call(prompt=..., request_id=...)`` is the caller's own bounded model call
    -- same purpose, same configuration, same ledger.  This module never
    chooses a model, which is why a run that was routed to a degraded family
    stays on that family for its repair: the repair is a second look at the
    same reply, not a second opinion from somewhere better.

    ``passthrough_errors`` are raised, not repaired.  "The material does not
    answer the question" is an honest answer to the question asked, and buying
    a second call to hear it again would be the expensive way to learn nothing.
    """

    calls: list[dict[str, Any]] = []
    spent = 0

    def record(result: Mapping[str, Any]) -> int:
        nonlocal spent
        cost = int(result.get("cost_micros") or 0)
        spent += cost
        calls.append(dict(result))
        return cost

    try:
        first = call(prompt=prompt, request_id=request_id)
    except unavailable_errors as exc:
        return RepairOutcome(status="unavailable", reason=f"{type(exc).__name__}: {exc}",
                             error=exc)
    record(first)
    try:
        value = parse(first.get("text"))
        return RepairOutcome(status="ok", value=value, calls=calls, cost_micros=spent)
    except passthrough_errors:
        raise
    except refusal_errors as exc:
        first_error: BaseException = exc

    violations = violations_of(first.get("text"), contract, parse_error=first_error)
    if not violations:  # pragma: no cover - defensive; a refusal always says why
        violations = [Violation("(reply)", "validator", str(first_error))]

    if (budget_remaining_micros is not None
            and budget_remaining_micros - spent < repair_reserve_micros):
        return RepairOutcome(
            status="budget_refused", violations=violations, calls=calls,
            cost_micros=spent, error=first_error,
            reason=("run cost bound reached before the contract repair: "
                    f"{max(0, budget_remaining_micros - spent)} micros left, "
                    f"{repair_reserve_micros} reserved for one repair call; "
                    + _reason(violations, repaired=False)))

    try:
        repair_prompt = build_repair_prompt(
            original_prompt=prompt, reply_text=first.get("text"),
            violations=violations, contract_reminder=contract_reminder,
            context=repair_context, max_bytes=max_repair_prompt_bytes)
    except ContractRepairError as exc:
        return RepairOutcome(status="refused", violations=violations, calls=calls,
                             cost_micros=spent, error=first_error,
                             reason=f"{exc}; " + _reason(violations, repaired=False))

    try:
        second = call(
            prompt=repair_prompt,
            request_id=repair_request_id(
                request_id,
                contract_name=(contract.name if contract is not None else "unnamed"),
                violations=violations),
        )
    except unavailable_errors as exc:
        return RepairOutcome(
            status="refused", violations=violations, calls=calls, cost_micros=spent,
            error=first_error,
            reason=(f"the contract repair call was unavailable ({type(exc).__name__}: "
                    f"{exc}); " + _reason(violations, repaired=False)))
    record(second)
    try:
        value = parse(second.get("text"))
        return RepairOutcome(status="repaired", value=value, calls=calls,
                             cost_micros=spent, violations=violations)
    except passthrough_errors:
        raise
    except refusal_errors as exc:
        remaining = violations_of(second.get("text"), contract, parse_error=exc)
        return RepairOutcome(
            status="refused", violations=remaining or violations, calls=calls,
            cost_micros=spent, error=exc,
            reason=_reason(remaining or violations, repaired=True))


def contract_reminder_lines(lines: Iterable[str]) -> str:
    """Join a caller's rule reminders into the block the repair prompt shows."""

    return "\n".join(f"* {line}" for line in lines if line)
