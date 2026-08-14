"""Idempotent OpenClaw/Discord bridge for Agenda Shadow cards and feedback.

The bridge never opens an authority database. Delivery uses the scoped Core
writer principal; reaction ingestion uses a separate feedback-only principal.
A deterministic marker lets a restarted bridge reconcile a successful Discord
send that occurred just before the local delivery receipt was committed.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .store import content_hash
from .writer_client import WriterClient
from .writer_server import load_principals


BRIDGE_ACTOR = "core"
FEEDBACK_BRIDGE_ACTOR = "bridge:openclaw-discord"
MAX_COMMAND_OUTPUT_BYTES = 512 * 1024
MAX_DISCORD_MESSAGE_CHARS = 1900


class AgendaBridgeError(RuntimeError):
    pass


CommandRunner = Callable[[Sequence[str], float], Mapping[str, Any]]


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgendaBridgeError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, name: str, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise AgendaBridgeError(f"{name} must be 1..{upper}")
    return value


def _positive_number(value: Any, name: str, upper: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= upper
    ):
        raise AgendaBridgeError(f"{name} must be positive and <= {upper:g}")
    return float(value)


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AgendaBridgeError(f"{name} must be an absolute path")
    return Path(value)


@dataclass(frozen=True, slots=True)
class OpenClawAgendaBridgeConfig:
    openclaw_executable: Path
    writer_socket: Path
    token_config: Path
    account: str
    target: str
    guild_id: str
    channel_id: str
    endpoint_ref: str
    control_url: str
    company_labels: Mapping[str, str]
    feedback_user_ids: tuple[str, ...]
    timeout_seconds: float
    claim_ttl_seconds: int
    retry_seconds: int
    max_attempts: int
    batch_size: int
    feedback_limit: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OpenClawAgendaBridgeConfig":
        expected = {
            "openclaw_executable", "writer_socket", "token_config", "account",
            "target", "guild_id", "channel_id", "endpoint_ref", "control_url", "company_labels",
            "feedback_user_ids", "timeout_seconds", "claim_ttl_seconds",
            "retry_seconds", "max_attempts", "batch_size", "feedback_limit",
        }
        if set(raw) != expected:
            raise AgendaBridgeError("OpenClaw agenda bridge config has an invalid closed shape")
        labels = raw["company_labels"]
        if not isinstance(labels, Mapping) or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in labels.items()
        ):
            raise AgendaBridgeError("company_labels must be a string mapping")
        feedback_users = raw["feedback_user_ids"]
        if (
            not isinstance(feedback_users, list)
            or any(not isinstance(item, str) or not item.isdigit() for item in feedback_users)
            or len(set(feedback_users)) != len(feedback_users)
        ):
            raise AgendaBridgeError("feedback_user_ids must be unique Discord user ids")
        channel_id = _string(raw["channel_id"], "channel_id")
        target = _string(raw["target"], "target")
        if target != f"channel:{channel_id}":
            raise AgendaBridgeError("target must match channel_id")
        return cls(
            openclaw_executable=_path(raw["openclaw_executable"], "openclaw_executable"),
            writer_socket=_path(raw["writer_socket"], "writer_socket"),
            token_config=_path(raw["token_config"], "token_config"),
            account=_string(raw["account"], "account"),
            target=target,
            guild_id=_string(raw["guild_id"], "guild_id"),
            channel_id=channel_id,
            endpoint_ref=_string(raw["endpoint_ref"], "endpoint_ref"),
            control_url=_string(raw["control_url"], "control_url"),
            company_labels=dict(labels),
            feedback_user_ids=tuple(feedback_users),
            timeout_seconds=_positive_number(raw["timeout_seconds"], "timeout_seconds", 300),
            claim_ttl_seconds=_positive_int(raw["claim_ttl_seconds"], "claim_ttl_seconds", 3600),
            retry_seconds=_positive_int(raw["retry_seconds"], "retry_seconds", 86400),
            max_attempts=_positive_int(raw["max_attempts"], "max_attempts", 100),
            batch_size=_positive_int(raw["batch_size"], "batch_size", 10),
            feedback_limit=_positive_int(raw["feedback_limit"], "feedback_limit", 100),
        )


def _default_runner(argv: Sequence[str], timeout: float) -> Mapping[str, Any]:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        raise AgendaBridgeError("openclaw_timeout") from None
    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES:
        raise AgendaBridgeError("openclaw_output_limit")
    if completed.returncode != 0:
        raise AgendaBridgeError(f"openclaw_exit_{completed.returncode}")
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgendaBridgeError("openclaw_invalid_json") from None
    if not isinstance(value, Mapping):
        raise AgendaBridgeError("openclaw_invalid_response")
    return value


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip().replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def render_agenda_card(
    message: Mapping[str, Any], company_labels: Mapping[str, str], control_url: str
) -> tuple[str, str]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping) or payload.get("kind") != "agenda_shadow_card":
        raise AgendaBridgeError("unsupported_outbox_payload")
    selected = payload.get("selected")
    if not isinstance(selected, list) or not selected:
        raise AgendaBridgeError("agenda_card_has_no_selected_questions")
    payload_hash = _string(message.get("payload_hash"), "payload_hash")
    if len(payload_hash) != 64:
        raise AgendaBridgeError("payload_hash_is_invalid")
    marker = f"DALTON-OUTBOX-{payload_hash[:24]}"
    company_ref = _string(payload.get("company_ref"), "company_ref")
    label = company_labels.get(company_ref, company_ref)
    lines = [
        f"**Dalton Agenda Shadow｜{_clip(label, 80)}**",
        "本轮只选研究问题，不执行研究，也不修改正式观点。",
        "",
    ]
    for index, raw in enumerate(selected, start=1):
        if not isinstance(raw, Mapping):
            raise AgendaBridgeError("agenda_card_selected_item_is_invalid")
        lines.append(f"{index}. {_clip(raw.get('question'), 260)}")
        lines.append(f"   验收：{_clip(raw.get('answer_criteria'), 180)}")
    lines.extend(
        [
            "",
            f"延后 {int(payload.get('deferred_count', 0))} 个；拒绝 {int(payload.get('rejected_count', 0))} 个。",
            f"处理：请打开 Dalton 控制面 {_clip(control_url, 180)}。Discord 只发通知，不接收审批。",
            f"`{marker}`",
        ]
    )
    body = "\n".join(lines)
    if len(body) > MAX_DISCORD_MESSAGE_CHARS:
        # Keep all selected questions and the audit marker; shrink criteria first.
        compact = [
            f"**Dalton Agenda Shadow｜{_clip(label, 80)}**",
            "本轮只选问题，不执行研究或修改正式观点。",
            "",
        ]
        for index, raw in enumerate(selected, start=1):
            compact.append(f"{index}. {_clip(raw.get('question'), 240)}")
        compact.extend([
            "", f"处理：{_clip(control_url, 180)}", "Discord 只发通知，不接收审批。", f"`{marker}`",
        ])
        body = "\n".join(compact)
    if len(body) > MAX_DISCORD_MESSAGE_CHARS:
        raise AgendaBridgeError("agenda_card_exceeds_discord_limit")
    return body, marker


class OpenClawAgendaBridge:
    def __init__(
        self,
        config: OpenClawAgendaBridgeConfig,
        *,
        runner: CommandRunner | None = None,
        core_client: WriterClient | None = None,
        feedback_client: WriterClient | None = None,
    ) -> None:
        self.config = config
        self._runner = runner or _default_runner
        principals = None
        needs_feedback = bool(config.feedback_user_ids)
        if core_client is None or (needs_feedback and feedback_client is None):
            principals = load_principals(config.token_config)
        if core_client is None:
            core = (principals or {}).get("core")
            if core is None:
                raise AgendaBridgeError("core writer principal is unavailable")
            core_client = WriterClient(str(config.writer_socket), core.token, timeout=30)
        if needs_feedback and feedback_client is None:
            feedback = (principals or {}).get("feedback-bridge")
            if feedback is None:
                raise AgendaBridgeError("feedback bridge principal is unavailable")
            feedback_client = WriterClient(str(config.writer_socket), feedback.token, timeout=30)
        self.core = core_client
        self.feedback = feedback_client

    def _openclaw(self, *args: str) -> Mapping[str, Any]:
        executable = self.config.openclaw_executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise AgendaBridgeError("openclaw_executable_unavailable")
        return self._runner((str(executable), "message", *args), self.config.timeout_seconds)

    @staticmethod
    def _message_rows(value: Any) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        if isinstance(value, Mapping):
            if isinstance(value.get("id"), str) and isinstance(value.get("content"), str):
                rows.append(value)
            else:
                for nested in value.values():
                    rows.extend(OpenClawAgendaBridge._message_rows(nested))
        elif isinstance(value, list):
            for nested in value:
                rows.extend(OpenClawAgendaBridge._message_rows(nested))
        return rows

    def _search_receipt(self, marker: str) -> str | None:
        value = self._openclaw(
            "search", "--channel", "discord", "--account", self.config.account,
            "--guild-id", self.config.guild_id, "--channel-id", self.config.channel_id,
            "--query", marker, "--limit", "10", "--json",
        )
        payload = value.get("payload")
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise AgendaBridgeError("openclaw_search_failed")
        matches = [
            row for row in self._message_rows(payload.get("results"))
            if marker in str(row.get("content", ""))
            and isinstance(row.get("author"), Mapping)
            and row["author"].get("bot") is True
        ]
        if not matches:
            return None
        matches.sort(key=lambda row: (str(row.get("timestamp", "")), str(row.get("id", ""))))
        return _string(matches[0].get("id"), "Discord message id")

    def _send(self, body: str) -> str:
        value = self._openclaw(
            "send", "--channel", "discord", "--account", self.config.account,
            "--target", self.config.target, "--message", body, "--json",
        )
        message_id = value.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            raise AgendaBridgeError("openclaw_send_missing_receipt")
        return message_id

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        text = str(exc)
        if not text or any(char.isspace() for char in text) or len(text) > 80:
            return "bridge_error"
        return text

    def _record_delivered(self, claim: Mapping[str, Any], message_id: str) -> Mapping[str, Any]:
        attempt_id = _string(claim.get("delivery_attempt_id"), "delivery_attempt_id")
        return self.core.record_agenda_delivery(
            message_id=_string(claim.get("message_id"), "message_id"),
            state="delivered",
            delivery_attempt_id=attempt_id,
            delivery_receipt_id=f"discord:{message_id}",
            idempotency_key=f"agenda-delivery-complete:{attempt_id}",
        )

    def _record_failed(
        self, claim: Mapping[str, Any], exc: BaseException, now: datetime
    ) -> Mapping[str, Any]:
        attempt_id = _string(claim.get("delivery_attempt_id"), "delivery_attempt_id")
        retry_at = (now + timedelta(seconds=self.config.retry_seconds)).isoformat(
            timespec="microseconds"
        )
        return self.core.record_agenda_delivery(
            message_id=_string(claim.get("message_id"), "message_id"),
            state="failed",
            delivery_attempt_id=attempt_id,
            error_code=self._error_code(exc),
            retry_after=retry_at,
            idempotency_key=f"agenda-delivery-failed:{attempt_id}",
        )

    def _deliver(self, claim: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        body, marker = render_agenda_card(
            claim, self.config.company_labels, self.config.control_url
        )
        try:
            receipt = self._search_receipt(marker)
            recovered = receipt is not None
            if receipt is None:
                try:
                    receipt = self._send(body)
                except BaseException:
                    # The CLI may have sent successfully before its local process failed.
                    receipt = self._search_receipt(marker)
                    if receipt is None:
                        raise
                    recovered = True
            self._record_delivered(claim, receipt)
            return {
                "message_id": claim["message_id"], "state": "delivered",
                "receipt": f"discord:{receipt}", "recovered": recovered,
            }
        except BaseException as exc:
            self._record_failed(claim, exc, now)
            return {
                "message_id": claim.get("message_id"), "state": "failed",
                "error_code": self._error_code(exc),
            }

    def _reactions(self, message_id: str) -> Mapping[str, set[str]]:
        value = self._openclaw(
            "reactions", "--channel", "discord", "--account", self.config.account,
            "--target", self.config.target, "--message-id", message_id,
            "--limit", "100", "--json",
        )
        payload = value.get("payload")
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise AgendaBridgeError("openclaw_reactions_failed")
        result: dict[str, set[str]] = {}
        rows = payload.get("reactions")
        if not isinstance(rows, list):
            raise AgendaBridgeError("openclaw_reactions_invalid")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            emoji = row.get("emoji")
            if not isinstance(emoji, Mapping):
                continue
            name = emoji.get("raw") or emoji.get("name")
            if name not in {"✅", "❌"}:
                continue
            users = row.get("users")
            if not isinstance(users, list):
                continue
            for user in users:
                if not isinstance(user, Mapping):
                    continue
                user_id = user.get("id")
                if user_id in self.config.feedback_user_ids:
                    result.setdefault(str(user_id), set()).add(str(name))
        return result

    def _poll_feedback(self) -> dict[str, Any]:
        if not self.config.feedback_user_ids:
            return {"targets": 0, "checked": 0, "recorded": 0, "conflicts": 0}
        if self.feedback is None:
            raise AgendaBridgeError("feedback_bridge_unavailable")
        targets = self.feedback.list_agenda_feedback_targets(
            endpoint_ref=self.config.endpoint_ref, limit=self.config.feedback_limit
        )
        recorded = 0
        conflicts = 0
        checked = 0
        for target in targets:
            latest = target.get("latest_feedback")
            if not isinstance(latest, Mapping):
                latest = {}
            # A label is final for Phase 1 evaluation. The generic governance CLI
            # remains available if an owner needs to append a correction.
            if all(f"human:discord-{user_id}" in latest for user_id in self.config.feedback_user_ids):
                continue
            receipt = _string(target.get("delivery_receipt_id"), "delivery_receipt_id")
            if not receipt.startswith("discord:"):
                raise AgendaBridgeError("feedback target is not a Discord receipt")
            discord_message_id = receipt.removeprefix("discord:")
            reactions = self._reactions(discord_message_id)
            checked += 1
            for user_id, emojis in reactions.items():
                if emojis == {"✅"}:
                    verdict, emoji = "agree", "✅"
                elif emojis == {"❌"}:
                    verdict, emoji = "disagree", "❌"
                else:
                    conflicts += 1
                    continue
                subject_ref = f"human:discord-{user_id}"
                previous = latest.get(subject_ref)
                if isinstance(previous, Mapping) and previous.get("verdict") == verdict:
                    continue
                prior_ref = previous.get("feedback_id") if isinstance(previous, Mapping) else None
                decision_ref = _string(target["payload"].get("decision_ref"), "decision_ref")
                identity = {
                    "decision_ref": decision_ref, "subject_ref": subject_ref,
                    "prior_feedback_ref": prior_ref, "verdict": verdict,
                }
                digest = content_hash(identity)[:32]
                source_event_ref = (
                    f"discord-reaction:{self.config.channel_id}:{discord_message_id}:"
                    f"{user_id}:{verdict}:{digest}"
                )
                result = self.feedback.record_agenda_feedback(
                    decision_id=decision_ref,
                    verdict=verdict,
                    notes=f"Discord reaction {emoji}",
                    feedback_id=f"agenda-feedback:{digest}",
                    idempotency_key=f"agenda-feedback-ingest:{digest}",
                    subject_ref=subject_ref,
                    prior_feedback_ref=prior_ref,
                    source="openclaw_discord_reaction",
                    source_event_ref=source_event_ref,
                )
                if result.get("status") == "fresh":
                    recorded += 1
        return {"targets": len(targets), "checked": checked, "recorded": recorded, "conflicts": conflicts}

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise AgendaBridgeError("bridge clock must include timezone")
        now = now.astimezone(timezone.utc)
        claim_key = f"agenda-delivery-claim:{uuid.uuid4().hex}"
        claimed = self.core.claim_agenda_outbox(
            endpoint_ref=self.config.endpoint_ref,
            now=now.isoformat(timespec="microseconds"),
            claim_ttl_seconds=self.config.claim_ttl_seconds,
            max_attempts=self.config.max_attempts,
            limit=self.config.batch_size,
            idempotency_key=claim_key,
        )
        deliveries = [self._deliver(claim, now) for claim in claimed.get("claims", [])]
        feedback_error = None
        try:
            feedback = self._poll_feedback()
        except BaseException as exc:
            feedback = {"targets": 0, "checked": 0, "recorded": 0, "conflicts": 0}
            feedback_error = self._error_code(exc)
        failed = sum(item["state"] == "failed" for item in deliveries)
        return {
            "status": "degraded" if failed or feedback_error else "ready",
            "claimed": len(deliveries),
            "delivered": len(deliveries) - failed,
            "failed": failed,
            "deliveries": deliveries,
            "feedback": feedback,
            "feedback_error": feedback_error,
        }


__all__ = [
    "AgendaBridgeError", "OpenClawAgendaBridge", "OpenClawAgendaBridgeConfig",
    "render_agenda_card",
]
