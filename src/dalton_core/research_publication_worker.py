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
    ("budget_refused", ("research budget refused", "BUDGET_REFUSED",
                        "mission_budget_exceeded", "owner_budget_exceeded")),
    ("daily_cap", ("purpose_daily_cap_reached", "pool_exhausted", "POOL_EXHAUSTED",
                   "coverage pool is spent")),
    ("database_locked", ("database is locked",)),
    ("already_running", ("this request is already running",)),
    ("attachment_size", ("display attachment exceeds the size limit",)),
    # Fixed in this round: a checker quote that differs from its section only
    # in spacing is re-anchored, a minority of unlocatable ones set aside.
    ("checker_quote_unanchored", ("language checker quote is not in its source section",)),
    # Fixed in this round: small counts written in Chinese, and the one
    # deterministic fallback of a check-only product.
    ("check_only_content", ("check-only draft failed validation",
                            "independent localization verifier did not pass cleanly")),
)
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


def automatic_retry_due(prior: Mapping[str, Any],
                        chain: list[dict[str, Any]]) -> str | None:
    """The retry class of a pending product owed its one automatic retry."""

    if prior.get("status") != "pending":
        return None
    name = retry_class(prior.get("result"))
    if name is None:
        return None
    if any(record["trigger"] == "automatic" and record["auto_round"] == AUTO_RETRY_ROUND
           and record["product_hash"] == prior["product_hash"] for record in chain):
        return None
    return name


def poll_once(
    connection: Any,
    mission: Mapping[str, Any],
    *,
    state_dir: Path,
    prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    library_reader: Callable[[Any, Mapping[str, Any], str], Mapping[str, Any]] | None = None,
    extra_reader: Callable[[Any, Mapping[str, Any], str], list[Mapping[str, Any]]] | None = None,
    priority: Callable[[Mapping[str, Any]], int] | None = None,
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
    """

    from .research_publication_spend import DEFERRED_STATUS, order_key

    candidates: list[tuple[tuple[Any, ...], dict[str, str], str, Mapping[str, Any]]] = []
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
                               identity, product_hash, product))
    candidates.sort(key=lambda row: row[0])
    summaries = []
    for _, identity, product_hash, product in candidates:
        path = _state_path(state_dir, identity)
        prior = _read_state(path)
        if (prior is not None and prior.get("status") == "completed"
                and prior.get("product_hash") == product_hash):
            summaries.append({"identity": identity, "product_hash": product_hash,
                              "status": "unchanged"})
            continue
        retry = None
        if (prior is not None and prior.get("status") in {"pending", EXHAUSTED_STATUS}
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
                automatic = automatic_retry_due(prior, chain)
                if automatic is not None:
                    retry = _write_retry(
                        state_dir, identity=identity, prior=prior, trigger="automatic",
                        actor_ref=AUTOMATIC_RETRY_ACTOR,
                        reason=f"failed only for {automatic}; retried once after deploy",
                        retry_class_name=automatic, auto_round=AUTO_RETRY_ROUND,
                        created_at=_now(), chain=chain)
            if retry is None:
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": prior["status"]})
                continue
        try:
            outcome = prepare(product)
            status = outcome.get("status") if isinstance(outcome, Mapping) else None
            state = {"schema_version": SCHEMA_VERSION,
                     "status": (status if status in {"completed", DEFERRED_STATUS, EXHAUSTED_STATUS}
                                else "pending"),
                     "identity": identity, "product_hash": product_hash,
                     "result": dict(outcome) if isinstance(outcome, Mapping) else {
                         "reason": "prepare returned no result"}}
        except Exception as exc:  # one product must not stop the rest
            state = {"schema_version": SCHEMA_VERSION, "status": "pending",
                     "identity": identity, "product_hash": product_hash,
                     "result": {"reason": f"{type(exc).__name__}: {exc}"}}
        if retry is not None:
            # Consumed: the same retry never reopens the state it produced.
            state["result"]["retry"] = {key: retry[key] for key in (
                "sequence", "trigger", "content_hash")}
        state["content_hash"] = _hash(state)
        _atomic_write(path, state)
        summaries.append({"identity": identity, "product_hash": product_hash,
                          "status": state["status"],
                          **({"retry": retry["trigger"]} if retry is not None else {})})
    deferred = sum(row["status"] == DEFERRED_STATUS for row in summaries)
    return {"schema_version": "research-publication-worker-poll:0.1",
            "products": summaries,
            "completed": sum(row["status"] == "completed" for row in summaries),
            # Deferred work is still owed, so it is pending to every reader
            # that asks "is there more to do"; ``deferred`` says how much of it
            # is only waiting for tomorrow's ceiling.
            "pending": sum(row["status"] == "pending" for row in summaries) + deferred,
            "deferred": deferred,
            "exhausted": sum(row["status"] == EXHAUSTED_STATUS for row in summaries),
            "retried": sum("retry" in row for row in summaries),
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
    "AUTOMATIC_RETRY_ACTOR", "AUTO_RETRY_ROUND", "EXHAUSTED_STATUS", "RETRY_CLASSES",
    "RETRY_SCHEMA", "SCHEMA_VERSION", "automatic_retry_due", "failure_reasons",
    "poll_once", "request_retry", "retry_chain", "retry_class", "run_periodic",
    "show_product",
]
