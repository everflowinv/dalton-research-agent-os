"""Read-only discovery worker for hash-bound research presentation preparation."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from .cockpit_research_library import research_library

SCHEMA_VERSION = "research-publication-worker-state:0.1"
#: A product whose check-only preparation used its one deterministic fallback
#: and still could not be reviewed.  Nothing was published.  Like ``pending``
#: it is not prepared again for the same hash -- a new source hash or an owner
#: retry reopens it -- but unlike ``pending`` it is not owed work, so it does
#: not keep the worker "pending" for ever.
EXHAUSTED_STATUS = "exhausted"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _identity(product: Mapping[str, Any]) -> dict[str, str]:
    fields = {key: product.get(key) for key in ("kind", "subject_ref", "version_ref")}
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise ValueError("available research product lacks a stable identity")
    return {key: str(value) for key, value in fields.items()}


def _state_path(state_dir: Path, identity: Mapping[str, str]) -> Path:
    return state_dir / (_hash(identity) + ".json")


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"publication worker state is unreadable: {path.name}") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or set(value) != {"schema_version", "status", "identity", "product_hash",
                              "result", "content_hash"}):
        raise ValueError(f"publication worker state is invalid: {path.name}")
    body = dict(value); digest = body.pop("content_hash")
    if digest != _hash(body):
        raise ValueError(f"publication worker state hash drifted: {path.name}")
    return value


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# retries (E7): append-only records, never a deleted state file
# ---------------------------------------------------------------------------

RETRY_SCHEMA = "research-publication-product-retry:0.1"
_RETRY_FIELDS = {"schema_version", "identity", "identity_hash", "product_hash",
                 "sequence", "trigger", "actor_ref", "reason", "retry_class",
                 "auto_round", "prior_status", "prior_state_hash", "prior_reason",
                 "created_at", "content_hash"}
RETRY_TRIGGERS = ("owner", "automatic")
#: Who writes the one automatic retry of a product that failed for a reason
#: that has since gone away.
AUTOMATIC_RETRY_ACTOR = "automation:research-publication-worker"
#: The idempotency mark of the automatic retry.  A product is retried
#: automatically at most once per (identity, source hash, round); a later
#: release that fixes another class of failure bumps the round.
AUTO_RETRY_ROUND = "2026-09-25a"
#: Failures that are about the moment, not the text, and the ones this round
#: fixed.  A pending product is retried automatically only when *every*
#: failure it recorded is in one of these classes.
RETRY_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 2026-10-02: before ``check_only_content`` -- its message embeds the
    # first failure, which carries that class's markers.  Exhausted only
    # because the kept-source rule cannot apply to a source that is not
    # Chinese; it now gets one fresh cheap draft instead (see
    # ``research_output_preparation.run_chunk``).  77 cycle reflections and
    # 8 NO_CHANGE judgements ended this way 09-25..10-02.
    ("check_only_not_chinese", ("check-only fallback unavailable (the source text is "
                                "not predominantly Simplified Chinese)",)),
    ("budget_refused", ("research budget refused", "BUDGET_REFUSED",
                        "mission_budget_exceeded", "owner_budget_exceeded")),
    ("daily_cap", ("purpose_daily_cap_reached", "pool_exhausted", "POOL_EXHAUSTED",
                   "coverage pool is spent")),
    ("database_locked", ("database is locked",)),
    # 2026-10-02: the cheap chain on cooldown.  Live the dossier
    # 0001058290 v31 sat pending on it with no retry, ever.
    # 2026-10-09: and every link failing on transport -- live, ACN's debate
    # map sat pending on "(transport_failure)" with no retry ahead of it.
    ("provider_unavailable", ("(provider_cooldown)", "no model route is available right now",
                              "(transport_failure)")),
    ("already_running", ("this request is already running",)),
    ("attachment_size", ("display attachment exceeds the size limit",)),
    # Fixed in this round: a checker quote that differs from its section only
    # in spacing is re-anchored, a minority of unlocatable ones set aside.
    ("checker_quote_unanchored", ("language checker quote is not in its source section",)),
    # 2026-10-08 (batch w), before ``check_only_content`` for the same reason
    # as above: a check-only draft refused only by the numeric check, which
    # now accepts a faithful unit conversion the target states.
    ("check_only_numeric", ("check-only draft failed validation: localized section "
                            "changed financial number tokens",)),
    # Fixed in this round: small counts written in Chinese, and the one
    # deterministic fallback of a check-only product.
    ("check_only_content", ("check-only draft failed validation",
                            "independent localization verifier did not pass cleanly")),
    # 2026-10-08 (batch w): 104863000 shown as 1.04863 亿美元 and friends.
    ("numeric_tokens_changed", ("localized section changed financial number tokens",)),
    # 2026-10-09: a minority of malformed checker suggestions is set aside.
    ("checker_suggestion_malformed", ("language checker suggestion has an invalid shape",)),
    # 2026-10-08 (batch w): the verifier chain held only the producer's model
    # family.  Not a moment's failure -- it lasts until the owner changes the
    # model selection -- so it is retried once a UTC day, never with back-off,
    # and the owner sees it once on the to-do list.  The second marker is how
    # the same refusal read when replayed before this batch.
    ("route_refused", ("verifier_not_independent",
                       "the model call did not succeed (MODEL_ROUTE_REJECTED)")),
)
ROUTE_REFUSED_CLASS = "route_refused"
#: Failures about the moment, not the text or the code: retried automatically
#: once per UTC day rather than once per round.  Live 2026-10-01 the dossier
#: 0001058290 v31 failed on "database is locked", got its one automatic retry,
#: hit the same lock and was then parked for good.
TRANSIENT_RETRY_CLASSES = frozenset({"budget_refused", "daily_cap", "database_locked",
                                     "already_running", "provider_unavailable",
                                     "route_refused"})
#: 2026-10-07: failures that are about a lock or a route *right now*: a
#: product that failed only for these is retried with a bounded exponential
#: back-off (with jitter) instead of once per UTC day.  Live 10-07 the worker
#: logged 152 "gave up on database is locked" admissions in its last 400
#: stderr lines; each such product sat parked until the next UTC day, so one
#: busy minute cost a product a day.  Nothing is spent by a lost admission or
#: a missing route, so retrying it is free; the cap per day bounds the rest.
BACKOFF_RETRY_CLASSES = frozenset({"database_locked", "already_running",
                                   "provider_unavailable"})
BACKOFF_RETRY_BASE_SECONDS = 15 * 60
BACKOFF_RETRY_MAX_SECONDS = 6 * 60 * 60
BACKOFF_RETRY_MAX_PER_DAY = 8
#: The round a class was fixed in; classes not listed are AUTO_RETRY_ROUND's.
RETRY_CLASS_ROUNDS = {"check_only_not_chinese": "2026-10-02a",
                      "check_only_numeric": "2026-10-08w",
                      "numeric_tokens_changed": "2026-10-08w",
                      "checker_suggestion_malformed": "2026-10-09ag"}
#: Statuses an automatic retry may reopen: ``pending`` for every class,
#: ``exhausted`` only for the class whose fallback was missing.
_EXHAUSTED_RETRY_CLASSES = frozenset({"check_only_not_chinese", "check_only_numeric"})
_IDENTITY_PREFIX_RE = __import__("re").compile(r"[0-9a-f]{8,64}")


def failure_reasons(result: Any) -> list[str]:
    """Every failure a product state recorded, as text."""

    if not isinstance(result, Mapping):
        return []
    reasons = []
    if isinstance(result.get("reason"), str):
        reasons.append(result["reason"])
    receipt = result.get("receipt")
    if isinstance(receipt, Mapping):
        for failure in receipt.get("failures") or ():
            if isinstance(failure, Mapping) and isinstance(failure.get("error"), str):
                reasons.append(failure["error"])
    return reasons


def retry_class(result: Any) -> str | None:
    """The one retry class covering every recorded failure, or ``None``."""

    reasons = failure_reasons(result)
    if not reasons:
        return None
    classes = set()
    for reason in reasons:
        found = next((name for name, markers in RETRY_CLASSES
                      if any(marker in reason for marker in markers)), None)
        if found is None:
            return None
        classes.add(found)
    return "+".join(sorted(classes))


def _retry_directory(state_dir: Path, identity_hash: str) -> Path:
    return Path(state_dir) / "retries" / identity_hash


def retry_chain(state_dir: Path, identity_hash: str) -> list[dict[str, Any]]:
    """Every retry record of one product, after checking the whole chain."""

    directory = _retry_directory(state_dir, identity_hash)
    for path in (directory.parent, directory):
        if path.is_symlink():
            raise ValueError("publication retry directory must not be a symlink")
    if not directory.is_dir():
        return []
    records = []
    for number, path in enumerate(sorted(directory.glob("*.json")), start=1):
        if path.is_symlink():
            raise ValueError("publication retry record must not be a symlink")
        value = json.loads(path.read_text("utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != RETRY_SCHEMA
                or set(value) != _RETRY_FIELDS):
            raise ValueError(f"publication retry record is invalid: {path.name}")
        body = dict(value); digest = body.pop("content_hash")
        if (digest != _hash(body) or value["identity_hash"] != identity_hash
                or value["sequence"] != number or value["trigger"] not in RETRY_TRIGGERS
                or path.name != f"{number:04d}.json"):
            raise ValueError("publication retry chain drifted")
        records.append(value)
    return records


def _consumed_sequence(state: Mapping[str, Any] | None) -> int:
    """The retry sequence a state was prepared under (0: none)."""

    result = (state or {}).get("result")
    retry = result.get("retry") if isinstance(result, Mapping) else None
    value = retry.get("sequence") if isinstance(retry, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _write_retry(state_dir: Path, *, identity: Mapping[str, str], prior: Mapping[str, Any],
                 trigger: str, actor_ref: str, reason: str, retry_class_name: str | None,
                 auto_round: str | None, created_at: str,
                 chain: list[dict[str, Any]]) -> dict[str, Any]:
    identity_hash = _hash(identity)
    reasons = failure_reasons(prior.get("result"))
    body = {
        "schema_version": RETRY_SCHEMA, "identity": dict(identity),
        "identity_hash": identity_hash, "product_hash": prior["product_hash"],
        "sequence": len(chain) + 1, "trigger": trigger, "actor_ref": actor_ref,
        "reason": reason, "retry_class": retry_class_name, "auto_round": auto_round,
        "prior_status": prior["status"], "prior_state_hash": prior["content_hash"],
        "prior_reason": (reasons[0][:300] if reasons else None), "created_at": created_at,
    }
    record = {**body, "content_hash": _hash(body)}
    directory = _retry_directory(state_dir, identity_hash)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{record['sequence']:04d}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical(record)); stream.flush(); os.fsync(stream.fileno())
    return record


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def retry_round(name: str) -> str:
    """The idempotency round of a (possibly joined) retry class."""

    return max(RETRY_CLASS_ROUNDS.get(part, AUTO_RETRY_ROUND) for part in name.split("+"))


def _parse_moment(value: Any) -> float | None:
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def backoff_retry_delay(identity_hash: str, retries_so_far: int) -> float:
    """Seconds to wait before the next back-off retry of one product.

    Doubles from :data:`BACKOFF_RETRY_BASE_SECONDS` up to
    :data:`BACKOFF_RETRY_MAX_SECONDS`, with "equal jitter" (half fixed, half
    spread) so the products that lost the same lock do not all come back on
    the same poll.  The jitter is derived from the product and the retry
    number, not from a random source: every poll computes the same moment.
    """

    base = min(BACKOFF_RETRY_BASE_SECONDS * (2 ** max(0, retries_so_far)),
               BACKOFF_RETRY_MAX_SECONDS)
    spread = int(sha256(f"{identity_hash}:{retries_so_far}".encode()).hexdigest()[:8], 16)
    return base / 2 + (base / 2) * (spread / 0xFFFFFFFF)


def backoff_retry_after(prior: Mapping[str, Any], chain: list[dict[str, Any]],
                        identity_hash: str) -> float | None:
    """When a product that failed only for a lock or a route may retry."""

    mine = [record for record in chain if record["trigger"] == "automatic"
            and record["product_hash"] == prior["product_hash"]]
    result = prior.get("result") if isinstance(prior.get("result"), Mapping) else {}
    moments = [moment for moment in (
        [_parse_moment(record.get("created_at")) for record in mine]
        + [_parse_moment(result.get("failed_at"))]) if moment is not None]
    if not moments:
        return None
    return max(moments) + backoff_retry_delay(identity_hash, len(mine))


def automatic_retry_due(prior: Mapping[str, Any], chain: list[dict[str, Any]],
                        *, now: str | None = None,
                        identity_hash: str | None = None) -> str | None:
    """The retry class of a parked product owed an automatic retry.

    A product that failed only for a lock or a route of the moment
    (:data:`BACKOFF_RETRY_CLASSES`) is retried with a bounded exponential
    back-off, at most :data:`BACKOFF_RETRY_MAX_PER_DAY` times a UTC day; one
    whose failures include a spent budget is retried at most once per UTC day
    (``now``'s date); one with a fixed failure, once per round.
    """

    status = prior.get("status")
    if status not in {"pending", EXHAUSTED_STATUS}:
        return None
    name = retry_class(prior.get("result"))
    if name is None:
        return None
    parts = set(name.split("+"))
    if status == EXHAUSTED_STATUS and not parts <= _EXHAUSTED_RETRY_CLASSES:
        return None
    mine = [record for record in chain if record["trigger"] == "automatic"
            and record["product_hash"] == prior["product_hash"]]
    if parts <= BACKOFF_RETRY_CLASSES:
        moment = now or _now()
        today = [record for record in mine if str(record["created_at"])[:10] == moment[:10]]
        if len(today) >= BACKOFF_RETRY_MAX_PER_DAY:
            return None
        after = backoff_retry_after(
            prior, chain, identity_hash or _hash(prior.get("identity") or {}))
        current = _parse_moment(moment)
        if after is not None and current is not None and current < after:
            return None
        return name
    # 2026-10-09: a refused route beside a failure already retried on its
    # round (live: IBM's initial screen, one chunk refused, one chunk on a
    # changed number) was never retried again, so the owner's chain change
    # could not reach it.  The route part is about today's configuration, so
    # the whole product is offered once a UTC day like a route-only one; the
    # other chunks replay their cached stages without buying anything new.
    if parts <= TRANSIENT_RETRY_CLASSES or ROUTE_REFUSED_CLASS in parts:
        day = (now or _now())[:10]
        if any(str(record["created_at"])[:10] == day for record in mine):
            return None
        return name
    if any(record["auto_round"] == retry_round(name) for record in mine):
        return None
    return name


def awaiting_owner(prior: Mapping[str, Any], chain: list[dict[str, Any]]) -> bool:
    """Is this unfinished product parked for good unless a person retries it?

    ``True`` when no automatic retry will ever reopen it: a failure no retry
    class covers, or a fixed class whose one retry for its round was spent.
    A transient, back-off or route class is retried by the worker itself, and
    a deferred product is offered on every poll, so neither waits on anyone.
    """

    status = prior.get("status")
    if status not in {"pending", EXHAUSTED_STATUS}:
        return False
    name = retry_class(prior.get("result"))
    if name is None:
        return True
    parts = set(name.split("+"))
    if status == EXHAUSTED_STATUS and not parts <= _EXHAUSTED_RETRY_CLASSES:
        return True
    if parts <= BACKOFF_RETRY_CLASSES or parts <= TRANSIENT_RETRY_CLASSES \
            or ROUTE_REFUSED_CLASS in parts:
        return False
    mine = [record for record in chain if record["trigger"] == "automatic"
            and record["product_hash"] == prior.get("product_hash")]
    return any(record["auto_round"] == retry_round(name) for record in mine)


#: 2026-10-07: how long a product version must have been the current one
#: before the worker pays to prepare it.  Live 10-04 ~$10.7 went on versions
#: replaced the same day (the dossier 0001058290 had six versions in one day,
#: every one localized and reviewed).  A version younger than this is
#: ``settling``: owed, offered again on the next poll, never prepared while it
#: is likely to be replaced.  ``stable_window_seconds`` in the worker config.
#: Four hours, not two: on legacy 10-04 the dossier versions that were
#: replaced the same day were replaced after 1.5, 3.05, 3.05, 3.1, 3.3, 4.25 and
#: 7.8 hours -- a two-hour window would have skipped one of the seven, four
#: hours skips five.
DEFAULT_STABLE_WINDOW_SECONDS = 4 * 60 * 60
SETTLING_STATUS = "settling"
SUPERSEDED_STATUS = "superseded"
#: 2026-10-09 (batch am, coordinator): a product with a section whose source
#: body is empty is not prepared -- no Chinese attachment is drafted for it,
#: so a text revision can never become a way to publish an empty section.
#: Nothing is written and nothing is spent; the next version (a new hash) is
#: considered afresh.
EMPTY_SOURCE_STATUS = "empty_source_section"
#: 2026-10-09 (batch C2): the skip is recorded.  ``build_prompt`` refuses
#: every ``localization_blockers`` hit -- an empty body *and*, for a
#: batch-D contract product, a heading-only / gap-only / placeholder
#: section -- but the worker only screened empty bodies, so the rest raised
#: inside ``prepare``, landed as ``pending`` with no retry class and came
#: back as a needs-human 「重新进行语言质检」 card whose retry raised again.
#: Now every blocker is screened before any spend and the product's state is
#: written as ``skipped`` / ``source_section_empty``: never retried (no
#: retry class reopens it; an owner retry is refused), counted and listed in
#: the poll summary, and parked for the owner with a redraft card.  A new
#: source version (a new hash) is considered afresh.
SKIPPED_STATUS = "skipped"
SOURCE_SECTION_EMPTY = "source_section_empty"
_BLOCKED_MESSAGE = "a product with a failed section is not localised"


def source_blockers(product: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every reason ``build_prompt`` would refuse this product, before any spend."""

    blockers = [{"code": "empty_source_section", "title": title}
                for title in empty_source_sections(product)]
    try:
        from .research_localization import localization_blockers

        found = localization_blockers(product)
    except Exception:  # noqa: BLE001 - the body screen above still holds
        found = []
    seen = {(item["code"], item["title"]) for item in blockers}
    for item in found:
        if not isinstance(item, Mapping):
            continue
        row = {"code": str(item.get("code") or "blocked"), "title": str(item.get("title") or "")}
        if (row["code"], row["title"]) not in seen:
            seen.add((row["code"], row["title"]))
            blockers.append(row)
    return blockers


def _skip_state(identity: Mapping[str, str], product_hash: str,
                blockers: list[dict[str, Any]], *, detail: str | None = None) -> dict[str, Any]:
    titles = list(dict.fromkeys(item["title"] or "（无标题）" for item in blockers))
    reason = (f"{SOURCE_SECTION_EMPTY}：源稿有 {len(titles)} 节没有可发布的正文"
              f"（{'、'.join(titles[:8])}），未生成中文版、未花费、不重试；需要重新起草源稿")
    state = {"schema_version": SCHEMA_VERSION, "status": SKIPPED_STATUS,
             "identity": dict(identity), "product_hash": product_hash,
             "result": {"skip_reason": SOURCE_SECTION_EMPTY, "reason": reason,
                        "empty_sections": titles, "blockers": blockers[:40],
                        **({"detail": detail[:600]} if detail else {}),
                        "skipped_at": _now()}}
    state["content_hash"] = _hash(state)
    return state


def _skip_row(identity: Mapping[str, str], product_hash: str,
              state: Mapping[str, Any]) -> dict[str, Any]:
    result = state.get("result") or {}
    return {"identity": identity, "product_hash": product_hash, "status": SKIPPED_STATUS,
            "skip_reason": SOURCE_SECTION_EMPTY, "empty_sections": list(result.get("empty_sections") or []),
            "blockers": list(result.get("blockers") or []), "parked": True}


def empty_source_sections(product: Mapping[str, Any]) -> list[str]:
    """Titles of the product's sections whose source body is empty."""

    out = []
    for index, section in enumerate(product.get("sections") or ()):
        if not isinstance(section, Mapping):
            continue
        body = section.get("body")
        if not isinstance(body, str) or not body.strip():
            out.append(str(section.get("title") or f"第 {index + 1} 节"))
    return out


def _product_age_seconds(product: Mapping[str, Any], now: float) -> float | None:
    from datetime import datetime, timezone

    value = product.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return now - moment.timestamp()


def _takes_redraft(prepare: Callable[..., Any]) -> bool:
    import inspect

    try:
        return "redraft_generation" in inspect.signature(prepare).parameters
    except (TypeError, ValueError):
        return False


def poll_once(
    connection: Any,
    mission: Mapping[str, Any],
    *,
    state_dir: Path,
    prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    library_reader: Callable[[Any, Mapping[str, Any], str], Mapping[str, Any]] | None = None,
    extra_reader: Callable[[Any, Mapping[str, Any], str], list[Mapping[str, Any]]] | None = None,
    priority: Callable[[Mapping[str, Any]], int] | None = None,
    stable_window_seconds: float = 0.0,
    still_current: Callable[[Mapping[str, Any], str], bool] | None = None,
    clock: Callable[[], float] = time.time,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Discover current products and prepare each unseen hash independently.

    Library reads never call ``prepare`` for missing products or completed
    hashes. The injected prepare callback is the only place allowed to spend.

    2026-09-24: every current product is discovered first and then prepared
    in one deterministic order -- ``priority(product)`` (lower first), then
    kind, subject, version and hash -- so high-value work reaches a spend
    ceiling before backlog does.  A ``deferred`` outcome (a spend ceiling, not
    a fault) is kept as ``deferred`` and, unlike ``pending``, offered again on
    every poll until it finds room.

    2026-10-07: a version younger than ``stable_window_seconds`` is
    ``settling`` and is not prepared yet; and right before paying for one,
    ``still_current(product, company_ref)`` is asked whether it is still the
    current version -- a version replaced while this poll was preparing
    others is ``superseded`` and skipped.  Neither writes a state file: the
    newest version is prepared once it has been stable for the window, in
    the same priority order as before.
    """

    from .research_publication_spend import DEFERRED_STATUS, order_key
    from .research_publication_limits import publication_scope, PublicationParked

    candidates: list[tuple[tuple[Any, ...], dict[str, str], str, Mapping[str, Any], str]] = []
    seen: set[tuple[str, str]] = set()
    for member in mission.get("universe") or []:
        company_ref = member.get("company_ref") if isinstance(member, Mapping) else None
        if not isinstance(company_ref, str) or not company_ref:
            continue
        library = (
            research_library(connection, mission, company_ref, localize=False)
            if library_reader is None
            else library_reader(connection, mission, company_ref)
        )
        found = list(library.get("products") or [])
        if extra_reader is not None:
            found.extend(extra_reader(connection, mission, company_ref))
        for product in found:
            if not isinstance(product, Mapping) or product.get("status") != "available":
                continue
            identity = _identity(product)
            product_hash = _hash(product)
            key = (_hash(identity), product_hash)
            if key in seen:
                continue
            seen.add(key)
            rank = 0 if priority is None else int(priority(product))
            candidates.append(((*order_key(rank, product), product_hash),
                               identity, product_hash, product, company_ref))
    candidates.sort(key=lambda row: row[0])
    summaries = []
    import uuid
    tick = uuid.uuid4().hex
    quiesced = False
    for index, (_, identity, product_hash, product, company_ref) in enumerate(candidates):
        # A prior prepare has returned and its state is already atomic here.
        # Never interrupt its concurrent chunks or mutate its retry/fee ledger.
        if stop_requested is not None and stop_requested():
            quiesced = True
            summaries.extend({"identity": item[1], "product_hash": item[2],
                              "status": "quiesced"} for item in candidates[index:])
            break
        path = _state_path(state_dir, identity)
        prior = _read_state(path)
        if (prior is not None and prior.get("status") == "completed"
                and not source_blockers(product)
                and prior.get("product_hash") == product_hash):
            summaries.append({"identity": identity, "product_hash": product_hash,
                              "status": "unchanged"})
            continue
        prior_result = (prior or {}).get("result") or {}
        if (prior is not None and prior.get("product_hash") == product_hash
                and isinstance(prior_result.get("retry_after"), (int, float))
                and prior_result["retry_after"] > clock()):
            summaries.append({"identity":identity,"product_hash":product_hash,
                              "status":DEFERRED_STATUS,"parked":True,
                              "reason":prior_result.get("reason"),"retry_after":prior_result["retry_after"]})
            continue
        blockers = source_blockers(product)
        if blockers:
            # Recorded once per source hash; nothing is prepared or spent.
            if (prior is not None and prior.get("status") == SKIPPED_STATUS
                    and prior.get("product_hash") == product_hash):
                state = prior
            else:
                state = _skip_state(identity, product_hash, blockers)
                _atomic_write(path, state)
            summaries.append(_skip_row(identity, product_hash, state))
            continue
        if stable_window_seconds > 0:
            age = _product_age_seconds(product, clock())
            if age is not None and age < stable_window_seconds:
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": SETTLING_STATUS,
                                  "stable_in_seconds": int(stable_window_seconds - age)})
                continue
        retry = None
        if (prior is not None and (prior.get("status") in {"pending", EXHAUSTED_STATUS}
                or prior.get('status')==DEFERRED_STATUS and str(prior_result.get('reason')).startswith('publication_'))
                and prior.get("product_hash") == product_hash):
            # E7: a retry record newer than the state reopens it -- the
            # owner's (``request_retry``), or the one automatic retry of a
            # product whose every failure was transient or has been fixed.
            try:
                chain = retry_chain(state_dir, _hash(identity))
            except (OSError, ValueError) as exc:  # one product must not stop the rest
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": prior["status"], "retry_error": str(exc)})
                continue
            latest = chain[-1] if chain else None
            if (latest is not None and latest["product_hash"] == product_hash
                    and latest["sequence"] > _consumed_sequence(prior)):
                retry = latest
            else:
                when = _now()
                automatic = automatic_retry_due(prior, chain, now=when,
                                                identity_hash=_hash(identity))
                if automatic is not None:
                    parts = set(automatic.split("+"))
                    transient = parts <= TRANSIENT_RETRY_CLASSES
                    retry = _write_retry(
                        state_dir, identity=identity, prior=prior, trigger="automatic",
                        actor_ref=AUTOMATIC_RETRY_ACTOR,
                        reason=(f"failed only for {automatic}; retried with back-off"
                                if parts <= BACKOFF_RETRY_CLASSES else
                                f"failed only for {automatic}; retried once a UTC day"
                                if transient else
                                f"failed only for {automatic}; retried once after deploy"),
                        retry_class_name=automatic, auto_round=retry_round(automatic),
                        created_at=when, chain=chain)
            if retry is None:
                # Parked: not owed work until a retry, a new source or (for a
                # transient failure) the next UTC day.
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": prior["status"], "parked": True})
                continue
        if still_current is not None:
            try:
                current = bool(still_current(product, company_ref))
            except Exception:  # noqa: BLE001 - unreadable: prepare as before
                current = True
            if not current:
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": SUPERSEDED_STATUS})
                continue
        if stop_requested is not None and stop_requested():
            quiesced = True
            summaries.extend({"identity": item[1], "product_hash": item[2],
                              "status": "quiesced"} for item in candidates[index:])
            break
        try:
            # 2026-10-09: an owner's retry is prepared from a fresh draft.  The
            # stage cache replays the draft, review and route decisions of the
            # failed attempt by request id, so a retry that reused them failed
            # the same way every time; each owner retry is its own generation.
            generation = (sum(1 for record in chain if record["trigger"] == "owner")
                          if retry is not None and retry["trigger"] == "owner" else 0)
            limits_root=state_dir.parent if state_dir.name=='products' else state_dir
            from .research_source_projection import publication_source_hash
            limiter_source = publication_source_hash(product)
            with publication_scope(limits_root/'publication-limits.sqlite3', limiter_source, tick=tick) as scope:
                scope[0].clock = clock
                trigger = ('daily_retry' if retry is not None and retry['trigger']=='automatic'
                           else 'owner_retry' if retry is not None
                           else 'verifier_rejected' if 'semantic' in str(prior_result)
                           else 'language_review_failed' if prior and prior.get('product_hash')==product_hash
                           else 'new_version')
                scope[0].run_reason = trigger
                scope[0].admit(limiter_source,tick,"pipeline",kind="run",reason=trigger)
                outcome = (prepare(product, redraft_generation=generation)
                           if generation and _takes_redraft(prepare) else prepare(product))
            status = outcome.get("status") if isinstance(outcome, Mapping) else None
            state = {"schema_version": SCHEMA_VERSION,
                     "status": (status if status in {"completed", DEFERRED_STATUS, EXHAUSTED_STATUS}
                                else "pending"),
                     "identity": identity, "product_hash": product_hash,
                     "result": dict(outcome) if isinstance(outcome, Mapping) else {
                         "reason": "prepare returned no result"}}
        except PublicationParked as exc:
            state = {"schema_version":SCHEMA_VERSION,"status":DEFERRED_STATUS,
                     "identity":identity,"product_hash":product_hash,
                     "result":{"reason":exc.reason,"parked":True,"retry_after":exc.retry_after}}
        except Exception as exc:  # one product must not stop the rest
            if _BLOCKED_MESSAGE in str(exc):
                # The prepared product hit a blocker the screen above missed:
                # still a recorded skip, never a pending failure to retry.
                state = _skip_state(identity, product_hash,
                                    source_blockers(product) or [{"code": "blocked", "title": ""}],
                                    detail=f"{type(exc).__name__}: {exc}")
                _atomic_write(path, state)
                summaries.append(_skip_row(identity, product_hash, state))
                continue
            state = {"schema_version": SCHEMA_VERSION, "status": "pending",
                     "identity": identity, "product_hash": product_hash,
                     "result": {"reason": f"{type(exc).__name__}: {exc}"}}
        if state["status"] in {"pending", EXHAUSTED_STATUS}:
            # When it failed: the back-off of a lock or route failure counts
            # from here (``backoff_retry_after``).
            state["result"]["failed_at"] = _now()
        if retry is not None and not state['result'].get('parked'):
            # Consumed: the same retry never reopens the state it produced.
            state["result"]["retry"] = {key: retry[key] for key in (
                "sequence", "trigger", "content_hash")}
        elif state['result'].get('parked') and prior_result.get('retry'):
            state['result']['retry']=prior_result['retry']
        state["content_hash"] = _hash(state)
        _atomic_write(path, state)
        summaries.append({"identity": identity, "product_hash": product_hash,
                          "status": state["status"],
                          **({"parked":True,"reason":state["result"].get("reason"),
                              "retry_after":state["result"].get("retry_after")}
                             if state["result"].get("parked") else {}),
                          **({"retry": retry["trigger"]} if retry is not None else {})})
    deferred = sum(row["status"] == DEFERRED_STATUS for row in summaries)
    settling = sum(row["status"] == SETTLING_STATUS for row in summaries)
    return {"schema_version": "research-publication-worker-poll:0.1",
            "products": summaries,
            "completed": sum(row["status"] == "completed" for row in summaries),
            # Deferred work is still owed, so it is pending to every reader
            # that asks "is there more to do"; ``deferred`` says how much of it
            # is only waiting for tomorrow's ceiling.
            "pending": (sum(row["status"] == "pending" for row in summaries)
                        + deferred + settling
                        + sum(row["status"] == "quiesced" for row in summaries)),
            "deferred": deferred,
            **({"quiesced": quiesced,
                "unprocessed": sum(row["status"] == "quiesced" for row in summaries)}
               if stop_requested is not None else {}),
            # Owed but not yet stable for the window; prepared on a later poll.
            "settling": settling,
            # Replaced by a newer version before this poll reached it.
            "superseded": sum(row["status"] == SUPERSEDED_STATUS for row in summaries),
            "exhausted": sum(row["status"] == EXHAUSTED_STATUS for row in summaries),
            # Refused before any spend: a source section is empty (batch am).
            "empty_source": sum(row["status"] == SKIPPED_STATUS for row in summaries),
            # Batch C2: the recorded skips, by name, so the summary says which
            # products have an empty source section (``source_section_empty``).
            "skipped": sum(row["status"] == SKIPPED_STATUS for row in summaries),
            "skipped_products": [
                {"identity": row["identity"], "identity_hash": _hash(dict(row["identity"])),
                 "product_hash": row["product_hash"], "skip_reason": row.get("skip_reason"),
                 "empty_sections": row.get("empty_sections") or []}
                for row in summaries
                if row["status"] == SKIPPED_STATUS and isinstance(row.get("identity"), Mapping)],
            # 2026-10-09: not a bare count -- which products gave up, so the
            # administrator page can name each one and offer its retry
            # (cockpit ``language_retry`` / ``retry-products``).
            "exhausted_products": [
                {"identity": row["identity"], "identity_hash": _hash(dict(row["identity"])),
                 "product_hash": row["product_hash"]}
                for row in summaries
                if row["status"] == EXHAUSTED_STATUS and isinstance(row.get("identity"), Mapping)],
            "retried": sum("retry" in row for row in summaries),
            # ``pending`` products not prepared this poll: they wait for an
            # owner retry, a new source hash or the next day's transient
            # retry; they are not in flight.
            "parked": sum(bool(row.get("parked")) for row in summaries),
            "unchanged": sum(row["status"] == "unchanged" for row in summaries)}


def run_periodic(
    connection: Any, mission: Mapping[str, Any], *, state_dir: Path,
    prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    stop_event: Any, interval_seconds: float, max_loops: int | None = None,
    library_reader: Callable[[Any, Mapping[str, Any], str], Mapping[str, Any]] | None = None,
    extra_reader: Callable[[Any, Mapping[str, Any], str], list[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    if interval_seconds <= 0 or max_loops is not None and max_loops < 1:
        raise ValueError("periodic worker bounds are invalid")
    results = []
    while not stop_event.is_set() and (max_loops is None or len(results) < max_loops):
        results.append(poll_once(connection, mission, state_dir=state_dir,
                                 prepare=prepare, library_reader=library_reader,
                                 extra_reader=extra_reader))
        if max_loops is not None and len(results) >= max_loops:
            break
        stop_event.wait(interval_seconds)
    return results


_HUMAN_ACTOR_RE = __import__("re").compile(r"human:[A-Za-z0-9._-]+\Z")


def _resolve_product(state_dir: Path, wanted: str) -> tuple[Path, dict[str, Any]]:
    """One product state by identity hash or source hash, or a unique prefix."""

    wanted = wanted.strip().rstrip(".…").lower()
    if not _IDENTITY_PREFIX_RE.fullmatch(wanted):
        raise ValueError("--identity must be at least 8 hex digits of an identity or product hash")
    found = []
    for path in sorted(Path(state_dir).glob("*.json")):
        state = _read_state(path)
        if state is None:
            continue
        if path.stem.startswith(wanted) or str(state["product_hash"]).startswith(wanted):
            found.append((path, state))
    if len(found) != 1:
        raise ValueError(f"{wanted} matches {len(found)} publication products; give more digits")
    return found[0]


def request_retry(state_dir: Path, wanted: str, *, actor_ref: str, reason: str,
                  apply: bool = False, now: Callable[[], str] = _now) -> dict[str, Any]:
    """Reopen one pending or exhausted product for the next worker poll.

    The formal replacement for deleting ``products/<identity>.json``: the
    state stays on disk and the retry is an append-only record naming who
    asked, why, and the state it supersedes.  ``poll_once`` prepares the
    product again when the record is newer than the state.  Dry run unless
    ``apply``.
    """

    if not isinstance(actor_ref, str) or not _HUMAN_ACTOR_RE.fullmatch(actor_ref):
        raise ValueError("actor_ref must be a human: principal")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("reason must be 1..1000 characters")
    root = Path(state_dir)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"not a publication product state directory: {root}")
    path, prior = _resolve_product(root, wanted)
    if prior["status"] == "completed":
        raise ValueError(f"{path.stem[:12]} already completed")
    if prior["status"] == SKIPPED_STATUS:
        raise ValueError(f"{path.stem[:12]} was skipped ({SOURCE_SECTION_EMPTY}): its source has an "
                         "empty section, so a language retry cannot produce it; redraft the source")
    if prior["status"] == "deferred":
        raise ValueError(f"{path.stem[:12]} is deferred to a spend ceiling and is "
                         "offered again on every poll without a retry")
    chain = retry_chain(root, path.stem)
    latest = chain[-1] if chain else None
    plan = {"identity": prior["identity"], "identity_hash": path.stem,
            "product_hash": prior["product_hash"], "prior_status": prior["status"],
            "last_failure": (failure_reasons(prior["result"]) or [None])[0],
            "retry_class": retry_class(prior["result"]), "sequence": len(chain) + 1}
    if (latest is not None and latest["product_hash"] == prior["product_hash"]
            and latest["sequence"] > _consumed_sequence(prior)):
        return {"status": "already_requested", **plan, "retry": latest}
    if not apply:
        return {"status": "dry_run", **plan}
    record = _write_retry(root, identity=prior["identity"], prior=prior, trigger="owner",
                          actor_ref=actor_ref, reason=reason.strip(),
                          retry_class_name=plan["retry_class"], auto_round=None,
                          created_at=now(), chain=chain)
    return {"status": "requested", **plan, "retry": record}


def show_product(state_dir: Path, wanted: str) -> dict[str, Any]:
    path, state = _resolve_product(Path(state_dir), wanted)
    return {"identity_hash": path.stem, "state": state,
            "retry_class": retry_class(state["result"]),
            "retries": retry_chain(Path(state_dir), path.stem)}


def add_retry_arguments(parser: Any) -> None:
    """``retry-products``: ``--identity``, ``--actor``, ``--reason``, ``--apply``."""

    parser.add_argument("--identity", required=True,
                        help="identity or product hash of the product, or a unique prefix (8+ hex)")
    parser.add_argument("--actor", required=True, help="human:<owner>")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write the retry record (default: dry run)")
    parser.add_argument("--show", action="store_true",
                        help="print the product's state and retry chain instead")


def run_retry_command(state_dir: Path, args: Any) -> dict[str, Any]:
    if getattr(args, "show", False):
        return show_product(state_dir, args.identity)
    return request_retry(state_dir, args.identity, actor_ref=args.actor,
                         reason=args.reason, apply=bool(args.apply))


__all__ = [
    "AUTOMATIC_RETRY_ACTOR", "AUTO_RETRY_ROUND", "BACKOFF_RETRY_CLASSES",
    "DEFAULT_STABLE_WINDOW_SECONDS", "backoff_retry_delay",
    "EXHAUSTED_STATUS", "RETRY_CLASSES", "SETTLING_STATUS", "SUPERSEDED_STATUS",
    "SKIPPED_STATUS", "SOURCE_SECTION_EMPTY", "source_blockers",
    "RETRY_CLASS_ROUNDS", "RETRY_SCHEMA", "SCHEMA_VERSION", "TRANSIENT_RETRY_CLASSES",
    "automatic_retry_due", "failure_reasons", "retry_round",
    "awaiting_owner", "poll_once", "request_retry", "retry_chain", "retry_class", "run_periodic",
    "show_product",
]
