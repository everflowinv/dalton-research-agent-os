"""Owner-authenticated Dalton Cockpit behind Tailscale Serve.

The service binds loopback only.  Tailscale Serve terminates HTTPS, strips
spoofed identity headers, and injects ``Tailscale-User-Login``.  The backend
adds one in-memory SameSite CSRF session before accepting any write.  Agenda,
research review, transcript correction, and confirmed intent share this shell
but retain their separate writer principals and fail-closed authority gates.
"""

from __future__ import annotations

from .shared_access_identity import is_shared_login as _is_shared_login

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

from .cockpit_plane import (
    MAX_CLAIMS_IN_VIEW,
    CockpitConfig,
    CockpitConflict,
    CockpitError,
    CockpitPlane,
    OVERVIEW_STALE_SECONDS,
    OVERVIEW_TTL_SECONDS,
)
from .governance_cli import GovernanceCliError, ephemeral_call
from .human_intent import (
    HumanIntentError,
    IntentComposerConfig,
    NaturalLanguageComposerPlane,
    build_cockpit_intent_context,
)
from .research_review import ResearchReviewError
from .research_review_control import (
    ResearchReviewControlConfig,
    ResearchReviewControlError,
    ResearchReviewControlPlane,
)
from .store import content_hash
from .writer_client import WriterClient
from .writer_protocol import RemoteError
from .writer_server import load_principals


# Batch ad (2026-10-08, ADR-0016 addendum): the v2 workbench is the cockpit at
# "/" (and its administrator page at "/admin"); the previous cockpit page moved
# to "/legacy" with every /v1/cockpit/* route unchanged, and the ADR-0006
# operator review page that used to live at "/legacy" moved to "/legacy/review".
_HTML_PATH = Path(__file__).with_name("cockpit_control.html")
_LEGACY_HTML_PATH = Path(__file__).with_name("cockpit_control_legacy.html")
_WEEKLY_BRIEF_HTML_PATH = Path(__file__).with_name("cockpit_weekly_brief.html")
# Browsers ask for /favicon.ico on their own even when the page declares an
# inline icon; answering with the same tiny SVG keeps the console free of 404s.
_FAVICON_SVG = (
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    b"<rect width='64' height='64' rx='16' fill='#18a999'/>"
    b"<text x='32' y='43' text-anchor='middle' font-family='Arial' font-size='34' "
    b"font-weight='700' fill='white'>D</text></svg>"
)
MAX_BODY_BYTES = 16384
#: 2026-10-07: the one body that carries a file -- an owner's fund workbook
#: template, base64 in JSON (cockpit_fund_templates.MAX_UPLOAD_BODY_BYTES).
TEMPLATE_UPLOAD_PATH = "/v1/cockpit/fund_template_register"
MAX_TEMPLATE_UPLOAD_BODY_BYTES = 16 * 1024 * 1024 * 4 // 3 + 64 * 1024
SESSION_TTL_SECONDS = 3600
AUTOMATION_SUBJECT = "automation:timeout"
# ADR-0009: the Perception/Agenda plane is retired.  ``/legacy/review`` keeps serving
# the delivered decisions -- they are history and history is not deleted -- but
# the view says so rather than presenting itself as a live queue that someone
# is still filling.
AGENDA_PLANE_RETIREMENT_REF = "adr:0009-one-event-plane"
AGENDA_PLANE_RETIREMENT_NOTE = (
    "议程/感知平面已退役（ADR-0009）。这里只读已送达的历史，不会再有新的议程；"
    "事件平面是 ResearchEvent 与判断车道。"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AgendaControlError(RuntimeError):
    pass


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgendaControlError(f"{name} must be a non-empty string")
    return value.strip()


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AgendaControlError(f"{name} must be an absolute path")
    return Path(value)


def _positive_int(value: Any, name: str, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise AgendaControlError(f"{name} must be 1..{upper}")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _string(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise AgendaControlError(f"{name} must be lowercase SHA-256")
    return value


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise AgendaControlError(f"{name} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgendaControlError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AgendaControlError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


class AdminRoleRequired(PermissionError):
    """A signed-in analyst asked for something only an administrator may do."""


#: The cockpit writes only 管理员设置 sends (model routing, budgets, run
#: levers).  An analyst login is refused these even when it knows the route.
ADMIN_ONLY_COCKPIT_ACTIONS = frozenset({
    "model_select", "model_budget", "research_budget", "model_allow", "model_metadata",
    "model_notice_ack", "lever",
})


def is_admin_path(path: str) -> bool:
    """``/admin``, ``/v2/admin`` and every administrator read or write."""

    return (path in {"/admin", "/admin/", "/v2/admin", "/v2/admin/"}
            or path.startswith(("/v2/admin/", "/v2/data/admin/", "/v2/static/admin/")))


@dataclass(frozen=True, slots=True)
class AgendaControlConfig:
    host: str
    port: int
    tailscale_host: str
    tailscale_executable: Path
    allowed_tailscale_logins: tuple[str, ...]
    writer_socket: Path
    token_config: Path
    endpoint_ref: str
    feedback_timeout_seconds: int
    sweep_interval_seconds: int
    research_review: ResearchReviewControlConfig | None = None
    intent_composer: IntentComposerConfig | None = None
    cockpit: CockpitConfig | None = None
    # 2026-10-09 (UX review G02 / MUSE §8 / 4.15): who may open 管理员设置.
    # ``None`` (the key absent) means every allowed login -- one owner today,
    # and a new workspace works out of the box.  When set, it is a non-empty
    # subset of ``allowed_tailscale_logins``; any other login gets 403 on
    # /admin, /v2/admin, /v2/data/admin/* and the administrator-only cockpit
    # writes, and the analyst shell's manifest does not list the page.
    admin_logins: tuple[str, ...] | None = None
    shared_gateway: Mapping[str, Any] | None = None

    @property
    def public_url(self) -> str:
        return f"https://{self.tailscale_host}:{self.port}/"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AgendaControlConfig":
        expected = {
            "host", "port", "tailscale_host", "tailscale_executable", "allowed_tailscale_logins",
            "writer_socket", "token_config", "endpoint_ref",
            "feedback_timeout_seconds", "sweep_interval_seconds",
        }
        optional = {"research_review", "intent_composer", "cockpit", "admin_logins", "shared_gateway"}
        if set(raw) - expected - optional or not expected.issubset(raw):
            raise AgendaControlError("Agenda control config has an invalid closed shape")
        host = _string(raw["host"], "host")
        if host not in {"127.0.0.1", "::1"}:
            raise AgendaControlError("Agenda control must bind loopback")
        tailscale_host = _string(raw["tailscale_host"], "tailscale_host").rstrip(".")
        if any(char in tailscale_host for char in "/:@") or not tailscale_host.endswith(".ts.net"):
            raise AgendaControlError("tailscale_host must be a ts.net hostname")
        logins = raw["allowed_tailscale_logins"]
        if (
            not isinstance(logins, list) or not logins
            or any(not isinstance(item, str) or not item.strip() for item in logins)
            or len(set(logins)) != len(logins)
        ):
            raise AgendaControlError("allowed_tailscale_logins must be unique strings")
        admin_logins = raw.get("admin_logins")
        if admin_logins is not None:
            if (
                not isinstance(admin_logins, list) or not admin_logins
                or any(not isinstance(item, str) or not item.strip() for item in admin_logins)
                or len(set(admin_logins)) != len(admin_logins)
            ):
                raise AgendaControlError("admin_logins must be a non-empty list of unique strings")
            admin_logins = [item.strip() for item in admin_logins]
            if not set(admin_logins) <= {item.strip() for item in logins}:
                raise AgendaControlError("admin_logins must be a subset of allowed_tailscale_logins")
        review_config = None
        review_raw = raw.get("research_review")
        if review_raw is not None:
            if not isinstance(review_raw, Mapping):
                raise AgendaControlError("research_review must be an object or null")
            try:
                review_config = ResearchReviewControlConfig.from_mapping(review_raw)
            except ResearchReviewControlError as exc:
                raise AgendaControlError("embedded research review config is invalid") from exc
        intent_config = None
        intent_raw = raw.get("intent_composer")
        if intent_raw is not None:
            if not isinstance(intent_raw, Mapping):
                raise AgendaControlError("intent_composer must be an object or null")
            try:
                intent_config = IntentComposerConfig.from_mapping(intent_raw)
            except HumanIntentError as exc:
                raise AgendaControlError("embedded intent composer config is invalid") from exc
        cockpit_config = None
        cockpit_raw = raw.get("cockpit")
        if cockpit_raw is not None:
            if not isinstance(cockpit_raw, Mapping):
                raise AgendaControlError("cockpit must be an object or null")
            try:
                cockpit_config = CockpitConfig.from_mapping(cockpit_raw)
            except CockpitError as exc:
                raise AgendaControlError("embedded cockpit config is invalid") from exc
        from .shared_gateway_auth import validate_config
        try:
            shared_gateway = validate_config(raw.get("shared_gateway"))
        except (ValueError, TypeError) as exc:
            raise AgendaControlError("shared gateway config invalid") from exc
        return cls(
            host=host,
            port=_positive_int(raw["port"], "port", 65535),
            tailscale_host=tailscale_host,
            tailscale_executable=_path(raw["tailscale_executable"], "tailscale_executable"),
            allowed_tailscale_logins=tuple(item.strip() for item in logins),
            writer_socket=_path(raw["writer_socket"], "writer_socket"),
            token_config=_path(raw["token_config"], "token_config"),
            endpoint_ref=_string(raw["endpoint_ref"], "endpoint_ref"),
            feedback_timeout_seconds=_positive_int(
                raw["feedback_timeout_seconds"], "feedback_timeout_seconds", 31 * 86400
            ),
            sweep_interval_seconds=_positive_int(
                raw["sweep_interval_seconds"], "sweep_interval_seconds", 86400
            ),
            research_review=review_config,
            intent_composer=intent_config,
            cockpit=cockpit_config,
            admin_logins=None if admin_logins is None else tuple(admin_logins),
            shared_gateway=shared_gateway,
        )

    @classmethod
    def from_service_file(cls, path: str | Path) -> "AgendaControlConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            mode = config_path.stat().st_mode & 0o777
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("service config is unavailable") from exc
        if mode & 0o022 or not isinstance(raw, Mapping):
            raise AgendaControlError("service config permissions or shape are invalid")
        control = raw.get("control")
        if (
            not isinstance(control, Mapping)
            or set(control) != {"enabled", "config"}
            or control.get("enabled") is not True
            or not isinstance(control.get("config"), Mapping)
        ):
            raise AgendaControlError("Agenda control is not enabled")
        return cls.from_mapping(control["config"])


def _subject_for_login(login: str) -> str:
    from .shared_access_identity import shared_actor
    if shared_actor(login) is not None:
        return shared_actor(login)
    digest = hashlib.sha256(login.encode("utf-8")).hexdigest()[:32]
    return f"human:tailscale-{digest}"


class AgendaControlPlane:
    def __init__(
        self,
        config: AgendaControlConfig,
        *,
        dashboard_client: WriterClient | None = None,
        timeout_client: WriterClient | None = None,
        governance_call: Any = ephemeral_call,
        research_task_grant: Any = None,
    ) -> None:
        self.config = config
        # P14e: ``adhoc_research_enabled`` was the literal ``False``.  It is
        # now a question with an answer -- "does the active mission grant
        # research_task, and has the owner published an ad-hoc ProbeTemplate an
        # executor can run" -- asked of whoever can see the Core.  The cockpit
        # process holds no Core handle, so the resolver is injected; absent, the
        # honest answer is still no, and it is no because nothing said yes
        # rather than because a constant said so.
        self.research_task_grant = research_task_grant
        principals = None
        if dashboard_client is None or timeout_client is None:
            principals = load_principals(
                config.token_config,
                # A control-only rolling upgrade on the original Core may read
                # managed principals created by its older writer.  A workspace
                # runtime remains exact because its manifest is always bound.
                allow_managed_operation_subset=(
                    "DALTON_WORKSPACE_MANIFEST" not in os.environ),
            )
        if dashboard_client is None:
            principal = (principals or {}).get("dashboard-control")
            if principal is None:
                raise AgendaControlError("dashboard-control principal is unavailable")
            dashboard_client = WriterClient(str(config.writer_socket), principal.token, timeout=30)
        if timeout_client is None:
            principal = (principals or {}).get("agenda-timeout")
            if principal is None:
                raise AgendaControlError("agenda-timeout principal is unavailable")
            timeout_client = WriterClient(str(config.writer_socket), principal.token, timeout=30)
        self.dashboard = dashboard_client
        self.timeout = timeout_client
        self.governance_call = governance_call

    def _targets(self, client: WriterClient) -> list[dict[str, Any]]:
        value = client.list_agenda_feedback_targets(
            endpoint_ref=self.config.endpoint_ref, limit=200
        )
        if not isinstance(value, list):
            raise AgendaControlError("writer returned invalid Agenda targets")
        return value

    def intent_context_bindings(self) -> list[dict[str, Any]]:
        value = self.dashboard.intent_context_bindings()
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise AgendaControlError("writer returned invalid intent context bindings")
        return [dict(item) for item in value]

    def answer_view(self) -> dict[str, Any]:
        value = self.dashboard.answer_subjects()
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise AgendaControlError("writer returned invalid answer subjects")
        return {
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "subjects": [dict(item) for item in value],
            "routes": [
                "answer_direct", "answer_after_refresh", "recommend_agenda_item",
            ],
            "refresh_enabled": True,
            "adhoc_research_enabled": self.adhoc_research_enabled(),
        }

    def adhoc_research_enabled(self) -> bool:
        """Whether ad-hoc research is granted, never raising to say so.

        The authoritative gate is the research-task lane's own, which reads the
        mission and the probe catalogue in the writer.  This is the same
        predicate for display; a resolver that cannot answer is not a licence.
        """

        if self.research_task_grant is None:
            return False
        try:
            decision = self.research_task_grant()
        except Exception:  # noqa: BLE001 - a display flag never breaks the view
            return False
        return bool(isinstance(decision, Mapping) and decision.get("granted"))

    def route_answer(
        self, subject_binding: Mapping[str, Any], question: str
    ) -> dict[str, Any]:
        if not isinstance(subject_binding, Mapping):
            raise AgendaControlError("subject_binding must be an object")
        result = self.dashboard.route_answer(
            subject_binding=dict(subject_binding),
            question=_string(question, "question"),
        )
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("context_pack"), Mapping)
            or not isinstance(result.get("decision"), Mapping)
        ):
            raise AgendaControlError("writer returned invalid answer route")
        return dict(result)

    def dispatch_answer_refresh(
        self,
        login: str,
        *,
        subject_binding: Mapping[str, Any],
        question: str,
        route_decision_ref: str,
        route_decision_hash: str,
        route_as_of: str,
    ) -> dict[str, Any]:
        if not isinstance(subject_binding, Mapping):
            raise AgendaControlError("subject_binding must be an object")
        normalized_as_of = _parse_time(route_as_of, "route_as_of").isoformat(
            timespec="microseconds"
        )
        try:
            result = self.governance_call(
                self.config.token_config,
                self.config.writer_socket,
                actor_ref=_subject_for_login(login),
                operation="dispatch_answer_refresh",
                params={
                    "subject_binding": dict(subject_binding),
                    "question": _string(question, "question"),
                    "route_decision_ref": _string(
                        route_decision_ref, "route_decision_ref"
                    ),
                    "route_decision_hash": _sha256(
                        route_decision_hash, "route_decision_hash"
                    ),
                    "route_as_of": normalized_as_of,
                },
            )
        except (GovernanceCliError, RemoteError) as exc:
            raise AgendaControlError(
                "answer refresh dispatch was rejected"
            ) from exc
        if (
            not isinstance(result, Mapping)
            or result.get("status") not in {"fresh", "duplicate"}
            or not isinstance(result.get("reservation"), Mapping)
            or not isinstance(result.get("dispatch"), Mapping)
        ):
            raise AgendaControlError(
                "writer returned invalid answer refresh dispatch"
            )
        return dict(result)

    @staticmethod
    def _decision_ref(target: Mapping[str, Any]) -> str:
        payload = target.get("payload")
        if not isinstance(payload, Mapping):
            raise AgendaControlError("Agenda target payload is invalid")
        return _string(payload.get("decision_ref"), "decision_ref")

    @staticmethod
    def _human_feedback(latest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [
            value for subject, value in latest.items()
            if isinstance(subject, str) and subject.startswith("human:")
            and isinstance(value, Mapping)
        ]

    def view(self, login: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        subject = _subject_for_login(login)
        rows = []
        for target in self._targets(self.dashboard):
            payload = target.get("payload")
            latest = target.get("latest_feedback")
            if not isinstance(payload, Mapping) or not isinstance(latest, Mapping):
                raise AgendaControlError("Agenda target is invalid")
            delivered = _parse_time(target.get("delivered_at"), "delivered_at")
            deadline = delivered + timedelta(seconds=self.config.feedback_timeout_seconds)
            own = latest.get(subject)
            human = self._human_feedback(latest)
            automatic = latest.get(AUTOMATION_SUBJECT)
            if human:
                verdicts = {str(item.get("verdict")) for item in human}
                effective = next(iter(verdicts)) if len(verdicts) == 1 else "mixed"
                resolution = "explicit_human"
            elif isinstance(automatic, Mapping):
                effective = "agree"
                resolution = "auto_accept_timeout"
            else:
                effective = "pending"
                resolution = "pending"
            rows.append({
                "decision_ref": self._decision_ref(target),
                "message_ref": target.get("message_id"),
                "payload_hash": target.get("payload_hash"),
                "company_ref": payload.get("company_ref"),
                "selected": payload.get("selected", []),
                "deferred_count": int(payload.get("deferred_count", 0)),
                "rejected_count": int(payload.get("rejected_count", 0)),
                "delivered_at": delivered.isoformat(timespec="seconds"),
                "deadline_at": deadline.isoformat(timespec="seconds"),
                "seconds_remaining": max(0, math.ceil((deadline - now).total_seconds())),
                "own_verdict": own.get("verdict") if isinstance(own, Mapping) else None,
                "effective_verdict": effective,
                "resolution": resolution,
            })
        return {
            "as_of": now.isoformat(timespec="seconds"),
            "timeout_seconds": self.config.feedback_timeout_seconds,
            "items": rows,
            "retired": True,
            "retirement_ref": AGENDA_PLANE_RETIREMENT_REF,
            "retirement_note": AGENDA_PLANE_RETIREMENT_NOTE,
        }

    def record(self, login: str, decision_ref: str, verdict: str) -> dict[str, Any]:
        decision_ref = _string(decision_ref, "decision_ref")
        if verdict not in {"agree", "disagree"}:
            raise AgendaControlError("dashboard verdict must be agree or disagree")
        subject = _subject_for_login(login)
        target = next(
            (item for item in self._targets(self.dashboard) if self._decision_ref(item) == decision_ref),
            None,
        )
        if target is None:
            raise AgendaControlError("Agenda decision is not available for feedback")
        latest = target.get("latest_feedback")
        if not isinstance(latest, Mapping):
            raise AgendaControlError("Agenda feedback state is invalid")
        previous = latest.get(subject)
        if isinstance(previous, Mapping) and previous.get("verdict") == verdict:
            return {"status": "duplicate", "decision_ref": decision_ref, "verdict": verdict}
        prior_ref = previous.get("feedback_id") if isinstance(previous, Mapping) else None
        nonce = secrets.token_hex(16)
        identity = {
            "decision_ref": decision_ref, "subject_ref": subject,
            "prior_feedback_ref": prior_ref, "verdict": verdict, "nonce": nonce,
        }
        digest = content_hash(identity)[:32]
        result = self.dashboard.record_agenda_feedback(
            decision_id=decision_ref,
            verdict=verdict,
            notes="Tailscale dashboard action",
            feedback_id=f"agenda-feedback:{digest}",
            idempotency_key=f"agenda-dashboard-feedback:{digest}",
            subject_ref=subject,
            prior_feedback_ref=prior_ref,
            source=("shared_dashboard" if _is_shared_login(login) else "tailscale_dashboard"),
            source_event_ref=f"dashboard-feedback:{digest}",
        )
        return {"status": result.get("status"), "decision_ref": decision_ref, "verdict": verdict}

    def sweep(self, *, now: datetime | None = None) -> dict[str, int]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        checked = recorded = pending = explicit = existing = 0
        for target in self._targets(self.timeout):
            checked += 1
            latest = target.get("latest_feedback")
            if not isinstance(latest, Mapping):
                raise AgendaControlError("Agenda feedback state is invalid")
            if self._human_feedback(latest):
                explicit += 1
                continue
            if AUTOMATION_SUBJECT in latest:
                existing += 1
                continue
            delivered = _parse_time(target.get("delivered_at"), "delivered_at")
            deadline = delivered + timedelta(seconds=self.config.feedback_timeout_seconds)
            if now < deadline:
                pending += 1
                continue
            decision_ref = self._decision_ref(target)
            digest = content_hash({
                "decision_ref": decision_ref,
                "delivered_at": delivered.isoformat(timespec="microseconds"),
                "timeout_seconds": self.config.feedback_timeout_seconds,
            })[:32]
            result = self.timeout.record_agenda_feedback(
                decision_id=decision_ref,
                verdict="agree",
                notes=f"No explicit human response within {self.config.feedback_timeout_seconds} seconds",
                feedback_id=f"agenda-feedback:{digest}",
                idempotency_key=f"agenda-auto-accept:{digest}",
                subject_ref=AUTOMATION_SUBJECT,
                prior_feedback_ref=None,
                source="auto_accept_timeout",
                source_event_ref=f"agenda-timeout:{digest}",
            )
            if result.get("status") == "fresh":
                recorded += 1
        return {
            "checked": checked, "recorded": recorded, "pending": pending,
            "explicit": explicit, "existing": existing,
        }


@dataclass(slots=True)
class _Session:
    login: str
    csrf: str
    expires_at: float


class CockpitIntentDispatcher:
    """Route one confirmed effect through its existing writer principal."""

    def __init__(
        self,
        config: AgendaControlConfig,
        agenda_plane: AgendaControlPlane,
        review_plane: ResearchReviewControlPlane | None,
        *,
        governance_call: Any = ephemeral_call,
    ) -> None:
        self.config = config
        self.agenda_plane = agenda_plane
        self.review_plane = review_plane
        self.governance_call = governance_call

    def _governance(
        self, login: str, operation: str, params: Mapping[str, Any]
    ) -> Any:
        return self.governance_call(
            self.config.token_config,
            self.config.writer_socket,
            actor_ref=_subject_for_login(login),
            operation=operation,
            params=dict(params),
        )

    def dispatch(
        self,
        login: str,
        bundle: Mapping[str, Any],
        confirmation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        candidate = bundle["candidate"]
        utterance = bundle["utterance"]
        translated = candidate["candidate"]
        effect = translated["effect"]
        kind = effect["kind"]
        provenance = {
            "candidate_version_ref": candidate["id"],
            "candidate_version_hash": candidate["content_hash"],
            "confirmation_ref": confirmation["id"],
            "confirmation_hash": confirmation["content_hash"],
        }
        if kind == "research_question_draft":
            return self._governance(
                login,
                "admit_intent_question",
                {
                    "subject_binding": effect["subject_binding"],
                    "question": effect["question"],
                    "answer_criteria": effect["answer_criteria"],
                    **provenance,
                    "idempotency_key": f"intent-question:{confirmation['id']}",
                },
            )
        if kind == "research_directive_candidate":
            return self._governance(
                login,
                "issue_intent_directive",
                {
                    "loop_binding": effect["loop_binding"],
                    "target_coverage_item_binding": effect[
                        "target_coverage_item_binding"
                    ],
                    "verbatim_text": utterance["verbatim_text"],
                    "control_effect": effect["control_effect"],
                    **provenance,
                },
            )
        if kind == "priority_override_candidate":
            effective_from = _parse_time(
                confirmation["created_at"], "confirmation.created_at"
            )
            effective_until = effective_from + timedelta(
                days=effect["effective_for_days"]
            )
            identity = {
                "candidate_version_ref": candidate["id"],
                "confirmation_ref": confirmation["id"],
            }
            digest = content_hash(identity)[:32]
            return self._governance(
                login,
                "create_priority_override",
                {
                    "override_ref": f"priority-override:intent:{digest}",
                    "scope_refs": [item["ref"] for item in effect["scope_bindings"]],
                    "weight_deltas": effect["weight_deltas"],
                    "rationale": effect["rationale"],
                    "effective_from": effective_from.isoformat(timespec="microseconds"),
                    "effective_until": effective_until.isoformat(timespec="microseconds"),
                    "revoked": False,
                    "version_id": f"priority-override-version:{digest}",
                    "idempotency_key": f"intent-priority:{confirmation['id']}",
                },
            )
        if kind != "context_bound_approval_candidate":
            raise AgendaControlError("intent effect is not dispatchable")
        target = effect["target_binding"]
        if target["kind"] == "agenda_decision":
            return self.agenda_plane.record(login, target["ref"], effect["verdict"])
        if self.review_plane is None:
            raise AgendaControlError("research review is not enabled")
        request_id = content_hash({"confirmation_ref": confirmation["id"]})[:32]
        if target["kind"] == "candidate_claim":
            return self.review_plane.record(
                login,
                {
                    "request_id": request_id,
                    "candidate_claim_ref": target["ref"],
                    "candidate_claim_hash": target["hash"],
                    "verdict": effect["verdict"],
                    "rationale": translated["rationale"],
                    "findings": [f"Confirmed by {confirmation['id']}"],
                    "proposed_revisions": None,
                },
            )
        if target["kind"] == "transcript_review_packet":
            return self.review_plane.record_transcript(
                login,
                {
                    "request_id": request_id,
                    "packet_ref": target["ref"],
                    "packet_hash": target["hash"],
                    "action": effect["verdict"],
                },
            )
        raise AgendaControlError("approval target has no original writer route")


class AgendaControlApplication:
    def __init__(
        self,
        config: AgendaControlConfig,
        plane: AgendaControlPlane,
        review_plane: ResearchReviewControlPlane | None = None,
        intent_plane: NaturalLanguageComposerPlane | None = None,
        cockpit_plane: CockpitPlane | None = None,
    ) -> None:
        self.config = config
        self.plane = plane
        self.review_plane = review_plane
        self.intent_plane = intent_plane
        self.cockpit_plane = cockpit_plane
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        if config is None:  # narrow unit composition without an HTTP surface
            self.session_cookie_name = "dalton_session"
        else:
            namespace = content_hash({
                "tailscale_host": config.tailscale_host,
                "port": config.port,
                "writer_socket": str(config.writer_socket),
            })[:16]
            self.session_cookie_name = f"dalton_session_{namespace}"

    def allowed_login(self, value: str | None) -> str | None:
        if not isinstance(value, str) or value.startswith("shared-access:") or value not in self.config.allowed_tailscale_logins:
            return None
        return value

    def is_admin_login(self, login: str | None) -> bool:
        """May ``login`` open 管理员设置?  Default: every allowed login."""

        from .shared_access_identity import is_shared_login
        if is_shared_login(login):
            return True
        config = self.config
        if config is None:  # narrow unit composition: no HTTP surface, no roles
            return True
        if self.allowed_login(login) is None:
            return False
        admins = getattr(config, "admin_logins", None)
        return admins is None or login in admins

    def require_admin(self, login: str | None) -> None:
        if not self.is_admin_login(login):
            raise AdminRoleRequired("管理员设置只对管理员开放；请联系管理员处理。")

    def session(self, login: str, cookie_header: str | None) -> tuple[str, _Session, bool]:
        from .shared_access_identity import is_shared_login
        shared = is_shared_login(login)
        cookie_name = self.session_cookie_name + ("_shared" if shared else "")
        session_id = None
        if cookie_header:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
                held = cookie.get(cookie_name)
                # A legacy cookie can only resume a session already held by
                # this process. New sessions always receive the namespaced
                # cookie, so two workspaces on one hostname cannot overwrite
                # each other merely because their ports differ.
                if held is None and not shared:
                    held = cookie.get("dalton_session")
                session_id = held.value if held else None
            except Exception:
                session_id = None
        now = time.monotonic()
        with self._lock:
            for key in [key for key, value in self._sessions.items() if value.expires_at <= now]:
                self._sessions.pop(key, None)
            current = self._sessions.get(session_id or "")
            if current is not None and current.login == login:
                current.expires_at = now + SESSION_TTL_SECONDS
                return session_id or "", current, False
            session_id = secrets.token_urlsafe(32)
            current = _Session(login, secrets.token_urlsafe(32), now + SESSION_TTL_SECONDS)
            self._sessions[session_id] = current
            return session_id, current, True

    def post(self, login: str, session: _Session, csrf: str | None, body: bytes) -> dict[str, Any]:
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != {"decision_ref", "verdict"}:
            raise AgendaControlError("request body has an invalid closed shape")
        return self.plane.record(login, value["decision_ref"], value["verdict"])

    def _review_body(self, session: _Session, csrf: str | None, body: bytes) -> Mapping[str, Any]:
        if self.review_plane is None:
            raise AgendaControlError("research review is not enabled")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping):
            raise AgendaControlError("request body must be an object")
        return value

    def post_review(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        value = self._review_body(session, csrf, body)
        assert self.review_plane is not None
        return self.review_plane.record(login, value)

    def post_transcript_review(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        value = self._review_body(session, csrf, body)
        assert self.review_plane is not None
        return self.review_plane.record_transcript(login, value)

    def post_document_review(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        value = self._review_body(session, csrf, body)
        assert self.review_plane is not None
        return self.review_plane.record_document_review(login, value)

    def post_document_evidence(self, login, session, csrf, body):
        value = self._review_body(session, csrf, body)
        return self.review_plane.document_extraction(login, value)

    def post_document_staging(self, login, session, csrf, body):
        value = self._review_body(session, csrf, body)
        return self.review_plane.stage_document_extraction(login, value)

    def post_document_extraction(self, login, session, csrf, body):
        value = self._review_body(session, csrf, body)
        return self.review_plane.document_extraction(login, value, generate=True)

    def post_intent(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        if self.intent_plane is None:
            raise AgendaControlError("natural-language composer is not enabled")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping):
            raise AgendaControlError("request body must be an object")
        return self.intent_plane.compose(login, value)

    def post_cockpit(
        self, action: str, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        if self.cockpit_plane is None:
            raise CockpitError("the cockpit is not configured on this host")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        if action in ADMIN_ONLY_COCKPIT_ACTIONS:
            self.require_admin(login)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CockpitError("request body is invalid") from exc
        if not isinstance(value, Mapping):
            raise CockpitError("request body must be an object")
        plane = self.cockpit_plane
        # B1-5: this request is about to change something the overview shows.
        # Drop the cached snapshot first, so the refetch the page does next
        # cannot be answered from before the change.  ``getattr`` because a
        # test may pass a stand-in plane; a plane with no cache has nothing to
        # invalidate.
        invalidate = getattr(plane, "invalidate_overview", None)
        if callable(invalidate):
            invalidate()
        if action == "workspace_lifecycle":
            return plane.workspace_lifecycle(login, value)
        if action == "workspace_create":
            return plane.create_workspace(login, value)
        if action == "workspace_rename":
            return plane.rename_workspace(login, value)
        if action == "ask":
            return plane.ask(login, value)
        # Batch G: 「用新材料续答」 -- resume one answer once its MaterialRequest
        # resolved; the plane refuses a second resume of the same request.
        if action == "ask_resume":
            return plane.resume_ask(login, value)
        if action == "goal":
            return plane.draft(login, "goal", value)
        if action == "steer":
            return plane.draft(login, "steer", value)
        if action == "publish":
            return plane.publish_draft(login, value)
        if action == "decide":
            return plane.decide(login, value)
        # Q1: the five feedback buttons. The cockpit process has no Core write
        # handle, so this goes back out through the writer as the owner's
        # Tailscale-derived principal, like every other cockpit decision.
        if action == "feedback":
            return plane.record_feedback(login, value)
        # P14-M2: the three model decisions. All three go back out through the
        # writer as the owner's principal; the cockpit process holds no write
        # handle to the Core or to the host configuration (ADR-0006).
        if action == "model_select":
            return plane.select_model(login, value)
        if action == "model_budget":
            return plane.set_call_budget(login, value)
        if action == "research_budget":
            return plane.set_research_budget(login, value)
        if action == "model_allow":
            return plane.allow_model(login, value)
        if action == "model_metadata":
            return plane.declare_model_metadata(login, value)
        if action == "model_notice_ack":
            return plane.acknowledge_model_notice(login, value)
        # 2026-10-06 (vision-gap #11 / #12): the owner's levers and the
        # coverage-list draft.  All of them go out through the writer as the
        # owner's principal and each one leaves a receipt.
        if action in {"lever", "stop_line", "prioritize", "coverage_draft"}:
            from . import cockpit_owner_levers as levers
            return {"lever": levers.apply_lever, "stop_line": levers.stop_line,
                    "prioritize": levers.prioritize,
                    "coverage_draft": levers.coverage_draft}[action](plane, login, value)
        # ADR-0012: move a probability or retire an adopted scenario.
        # 2026-10-07 (vision-gap #4): register a fund Excel template, choose
        # which template a company / industry / the workspace exports with.
        if action in {"fund_template_register", "fund_template_select"}:
            from . import cockpit_fund_templates as templates
            return {"fund_template_register": templates.register,
                    "fund_template_select": templates.select}[action](plane, login, value)
        if action == "scenario_revise":
            from .cockpit_scenarios import revise as revise_scenario
            return revise_scenario(plane, login, value)
        # 2026-10-07 (BUG-02): 「重试语言质检」 on a draft product, through
        # the writer as the owner; the retry record is the receipt.
        if action == "language_retry":
            return plane.retry_language_review(login, value)
        # 2026-10-09: a held research task's recovery doors (放行重新进入 /
        # 再买一次 / 撤回) and the owner's acknowledgement of a card that has
        # nothing to press.  Both through the writer as the owner.
        if action == "owner_door":
            from .cockpit_owner_doors import owner_door
            return owner_door(plane, login, value)
        if action == "needs_human_ack":
            return plane.acknowledge_needs_human(login, value)
        # ADR-0013: publish the next Conviction v2 policy version.
        if action == "conviction_policy":
            from .cockpit_conviction import publish_policy
            return publish_policy(plane, login, value)
        # 2026-09-30 (d2): the weekly brief schedule, destination optional.
        if action == "weekly_brief_plan":
            from .cockpit_weekly_brief import publish_weekly_brief_plan
            return publish_weekly_brief_plan(plane, login, value)
        raise CockpitError("unknown cockpit action")

    def cockpit_view(self, path: str, login: str, query: Mapping[str, str]) -> dict[str, Any]:
        if self.cockpit_plane is None:
            return {"enabled": False, "reason": "the cockpit is not configured on this host"}
        plane = self.cockpit_plane
        if path == "/v1/cockpit/workspace_research":
            return plane.workspace_research(login, str(query.get("slug", "")))
        if path == "/v1/cockpit/workspaces":
            return plane.workspaces(login)
        if path == "/v1/cockpit/overview":
            return {**plane.overview(), "enabled": True}
        # The reviewed UI translations one view needs, by key.  Read-only and
        # bounded per response; see CockpitPlane.ui_texts.
        if path == "/v1/cockpit/ui-texts":
            return {**plane.ui_texts(query.get("keys", "")), "enabled": True}
        if path == "/v1/cockpit/log":
            limit = _int_param(query, "limit", default=150, minimum=1, maximum=500)
            return {**plane.log(since=query.get("since") or None, limit=limit),
                    "limit": limit, "enabled": True}
        if path == "/v1/cockpit/approvals":
            return {**plane.approvals(), "enabled": True}
        # D4: the same checkpoints the approvals page shows, plus the ones that
        # are not checkpoints at all -- an unapproved connector, a source the
        # mission needs, a provider that has been refusing work all day -- each
        # with the exact action that clears it. Read-only.
        if path == "/v1/cockpit/needs-human":
            return plane.needs_human()
        if path == "/v1/cockpit/decision-status":
            return {**plane.decision_status(query), "enabled": True}
        if path == "/v1/cockpit/job":
            if not query.get("id"):
                raise CockpitError("缺少参数 id：要查询哪一项后台任务？")
            return {**plane.job(login, query["id"]), "enabled": True}
        if path == "/v1/cockpit/deliverable":
            return {**plane.document(query.get("ref", "")), "enabled": True}
        # P12b: the Ledger's conclusions read through the Claim index, with
        # the three filters the index makes answerable. "canonical" defaults
        # to on, which is what the index is for.
        if path == "/v1/cockpit/claims":
            limit = _int_param(query, "limit", default=MAX_CLAIMS_IN_VIEW, minimum=1,
                               maximum=MAX_CLAIMS_IN_VIEW)
            return {**plane.claims(
                company_ref=query.get("company") or None,
                index_aspect=query.get("aspect") or None,
                importance=query.get("importance") or None,
                canonical_only=query.get("canonical", "1") not in {"0", "false", "no"},
                limit=limit,
                cursor=query.get("cursor") or None,
            ), "limit": limit, "enabled": True}
        # P13-M2: one company's forecast model, printed by the lane's own
        # renderer rather than re-laid-out here.
        if path == "/v1/cockpit/model":
            return {**plane.company_model(query.get("company", "")), "enabled": True}
        if path == "/v1/cockpit/export":
            return {**plane.export_research(query.get("company", ""), query.get("format", ""),
                                            template=query.get("template") or None),
                    "enabled": True}
        if path == "/v1/cockpit/fund-templates":
            from .cockpit_fund_templates import templates_view
            return templates_view(plane)
        if path == "/v1/cockpit/research":
            return {**plane.research_library(query.get("company", "")), "enabled": True}
        # INT2 / P14a: what every connector can actually hand over, at what
        # tier, under what quota and how often -- plus the same question about
        # the model side. The owner asked for the first table by name.
        if path == "/v1/cockpit/sources":
            return {**plane.sources(), "enabled": True}
        # P14-M2: which model each calling stage uses, what the gateway has that
        # Dalton has not been let at, and what changed underneath us.
        if path == "/v1/cockpit/models":
            return {**plane.models(), "enabled": True}
        # 2026-10-08: the drafting stages an independent verifier checks, and
        # whether today's chains let it, in every environment.
        if path == "/v1/cockpit/verifier_independence":
            return {**plane.verifier_independence(), "enabled": True}
        # 2026-09-15: the owner asked to SEE what the system is doing, in the
        # style of a harness trajectory view -- the steps, not a lane table.
        if path == "/v1/cockpit/trajectory":
            return plane.trajectory()
        # INT2 / Q2: 每周回头看 -- the latest week's "我们把时间花在哪".
        if path == "/v1/cockpit/ops":
            # P17d 运维待办: what is parked on which dependency. Read-only and
            # derived from the append-only lane failure ledger; no authority,
            # no new grant, nothing to press.  P1-7: stopped and historical
            # attempts come a page at a time; the page asks for the next one.
            paging = {name: _int_param(query, name, default=default,
                                       minimum=minimum, maximum=maximum)
                      for name, default, minimum, maximum in OPS_PAGE_PARAMS
                      if query.get(name) not in (None, "")}
            return {**plane.ops_backlog(**paging), "enabled": True}
        if path == "/v1/cockpit/reflection":
            return {**plane.cycle_reflection(), "enabled": True}
        if path == "/v1/cockpit/levers":
            from .cockpit_owner_levers import levers_view
            return levers_view(plane)
        if path == "/v1/cockpit/conviction-policy":
            from .cockpit_conviction import policy_view
            return policy_view(plane, query)
        if path == "/v1/cockpit/weekly-brief":
            from .cockpit_weekly_brief import weekly_brief_view
            return weekly_brief_view(plane)
        if path == "/v1/cockpit/weekly-brief-document":
            from .cockpit_weekly_brief import weekly_brief_document
            return weekly_brief_document(plane, query)
        if path == "/v1/cockpit/history":
            kind = query.get("kind") or "ask"
            if kind not in {"ask", "goal", "steer", "coverage"}:
                raise CockpitError(
                    f"参数 kind 只能是 ask、goal、steer 或 coverage，收到的是「{str(kind)[:20]}」")
            return {"kind": kind, "items": plane.history(login, kind), "drafts": plane.drafts(login, None if kind == "ask" else kind),
                    "enabled": True}
        raise CockpitError("没有这个页面：请从导航重新进入")

    def post_intent_confirm(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        if self.intent_plane is None:
            raise AgendaControlError("natural-language composer is not enabled")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping):
            raise AgendaControlError("request body must be an object")
        return self.intent_plane.confirm(login, value)

    def post_answer(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        del login
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != {
            "subject_binding", "question"
        }:
            raise AgendaControlError("request body has an invalid closed shape")
        return self.plane.route_answer(
            value["subject_binding"], value["question"]
        )

    def post_answer_refresh(
        self, login: str, session: _Session, csrf: str | None, body: bytes
    ) -> dict[str, Any]:
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, session.csrf):
            raise PermissionError("invalid CSRF token")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgendaControlError("request body is invalid") from exc
        expected = {
            "subject_binding", "question", "route_decision_ref",
            "route_decision_hash", "route_as_of",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise AgendaControlError("request body has an invalid closed shape")
        return self.plane.dispatch_answer_refresh(
            login,
            subject_binding=value["subject_binding"],
            question=value["question"],
            route_decision_ref=value["route_decision_ref"],
            route_decision_hash=value["route_decision_hash"],
            route_as_of=value["route_as_of"],
        )


# (query name, default, minimum, maximum) for the ops page's two pagers.
OPS_PAGE_PARAMS: tuple[tuple[str, int, int, int], ...] = (
    ("terminal_offset", 0, 0, 10**9), ("terminal_limit", 50, 1, 500),
    ("historical_offset", 0, 0, 10**9), ("historical_limit", 50, 1, 500),
)


def _int_param(query: Mapping[str, str], name: str, *, default: int,
               minimum: int, maximum: int) -> int:
    """One integer query parameter, read the same way on every cockpit route.

    P2-9: ``log?limit=abc`` silently used 150, ``claims?limit=999999`` silently
    became 300, and ``history?kind=zzz`` was a 400 in English.  Now: absent or
    empty means the default; anything that is not a whole number, or is below
    the minimum, is a 400 that says so in Chinese; above the maximum is capped
    (and the route reports the limit it applied).
    """

    raw = query.get(name)
    if raw is None or raw == "":
        return default
    text = str(raw).strip()
    if not re.fullmatch(r"\d{1,12}", text):
        raise CockpitError(f"参数 {name} 必须是不小于 {minimum} 的整数，收到的是「{text[:20]}」")
    value = int(text)
    if value < minimum:
        raise CockpitError(f"参数 {name} 必须是不小于 {minimum} 的整数，收到的是「{text[:20]}」")
    return min(value, maximum)


def _cockpit_error_body(error: str, exc: Exception) -> bytes:
    """One refusal, written once, addressed to whoever it was written for.

    The page's ``readableError`` shows ``display_message`` verbatim and
    otherwise guesses from English keywords, falling back to "保存决定暂时未
    完成，请稍后重试。".  So a refusal the plane already wrote *for the owner*
    -- "退回补充需要写明理由：哪一问不行、为什么。" -- arrived as the generic
    fallback and the one sentence that says how to proceed was thrown away.

    A message carrying Chinese was written for a person: it is promoted to
    ``display_message`` and shown as-is.  An English one is internal wording
    ("decision must be approve or reject") and stays in ``message`` only, which
    the page keeps as ``technical_detail`` -- a refusal the owner should not
    have to read twice, in a language they did not ask for.
    """

    message = str(exc)
    owner_facing = any(
        "㐀" <= character <= "鿿" or "豈" <= character <= "﫿"
        for character in message
    )
    payload: dict[str, Any] = {"error": error, "message": message}
    # P2-9: an English refusal still gets a sentence the owner can read --
    # the page otherwise guessed from English keywords and, for most of them,
    # showed the English.  The English stays in ``message`` for 技术详情.
    payload["display_message"] = message if owner_facing else _english_refusal(error, message)
    return json.dumps(payload, ensure_ascii=False).encode()


# The English refusals the cockpit routes still raise, in the owner's words.
_ENGLISH_REFUSALS: dict[str, str] = {
    "job not found": "找不到这项后台任务，它可能已经过期；请重新发起一次。",
    "unknown history kind": "没有这类历史记录。",
    "unknown cockpit view": "没有这个页面：请从导航重新进入。",
    "unknown cockpit action": "不支持这个操作。",
    "decision must be approve or reject": "这项决定只能是「批准」或「拒绝」。",
    "decision must be admit or reject": "这项决定只能是「接受」或「拒绝」。",
    "request body is invalid": "请求内容无法解析，没有执行。",
    "request body must be an object": "请求内容格式不对，没有执行。",
    # 2026-10-08: a fresh workspace -- nothing to read yet, not a bad request.
    "no research goal has been published yet": "还没有发布研究任务：先在「研究目标」里发布一个任务，这里才会有内容。",
}


def _english_refusal(error: str, message: str) -> str:
    known = _ENGLISH_REFUSALS.get(message.strip().rstrip(".").casefold())
    if known is not None:
        return known
    from .cockpit_plane import _reason_text

    translated = _reason_text(message)
    if translated is not None:
        return translated + "。"
    if error == "conflict":
        return "这项操作与当前状态冲突，没有执行；请刷新后再试。"
    return "请求没有被接受：参数不正确或已经过期，没有执行。"


def _handler(application: AgendaControlApplication) -> type[BaseHTTPRequestHandler]:
    # ADR-0016: the analyst workbench; since batch ad it is "/" (alias /v2).
    from .cockpit_v2.routes import V2_ALIASES, V2NotFound, register_v2_routes
    v2_router = register_v2_routes(application)

    class Handler(BaseHTTPRequestHandler):
        server_version = "DaltonCockpit/0.4"

        def _identity(self) -> str | None:
            from .shared_gateway_auth import authenticate
            shared = authenticate(getattr(application.config, "shared_gateway", None), self.headers, self.client_address[0])
            if shared is not None:
                self.shared_prefix = self.headers["X-Dalton-Public-Prefix"]
                return shared
            self.shared_prefix = ""
            return application.allowed_login(self.headers.get("Tailscale-User-Login"))

        def _host_ok(self) -> bool:
            value = self.headers.get("Host")
            if not isinstance(value, str) or any(char.isspace() for char in value):
                return False
            try:
                host = urlparse(f"//{value}").hostname
            except ValueError:
                return False
            return host in {application.config.tailscale_host, "127.0.0.1", "::1"}

        def _send(
            self, status: int, content_type: str, body: bytes, *, session_cookie: str | None = None
        ) -> None:
            if (getattr(self, "shared_prefix", "") and content_type.startswith("text/html")
                    and urlparse(self.path).path in {"/", "/v2", "/v2/", "/admin", "/admin/", "/legacy", "/legacy/review", *v2_router.pages}):
                from .shared_public_urls import html_urls
                body = html_urls(body, self.shared_prefix)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
            )
            if session_cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"{application.session_cookie_name}{'_shared' if getattr(self, 'shared_prefix', '') else ''}={session_cookie}; "
                    f"Path={getattr(self, 'shared_prefix', '') or '/'}; Secure; HttpOnly; SameSite=Strict",
                )
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as exc:
                # B1-3: the page gave up first.  That is a browser closing a
                # tab or a 30 s fetch timeout on the other end of the
                # tailnet, not a server fault, and letting it reach
                # ``BaseHTTPRequestHandler`` prints a full traceback per
                # abandoned request.  Live, the overview's slow responses
                # filled the log with them and buried the failures that
                # mattered.  One line, no stack, and the connection closes.
                self.close_connection = True
                print(
                    f"cockpit client disconnected path={self.path} "
                    f"status={int(status)} error={type(exc).__name__}",
                    file=sys.stderr, flush=True,
                )

        def _send_admin_only(self) -> None:
            self._send(HTTPStatus.FORBIDDEN, "application/json; charset=utf-8", json.dumps({
                "error": "admin_only",
                "display_message": "管理员设置只对管理员开放；请联系管理员处理。",
            }, ensure_ascii=False).encode())

        def _context(self) -> tuple[str, str, _Session, bool] | None:
            if not self._host_ok():
                self._send(HTTPStatus.MISDIRECTED_REQUEST, "application/json", b'{"error":"invalid_host"}')
                return None
            login = self._identity()
            if login is None:
                self._send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"forbidden"}')
                return None
            session_id, session, created = application.session(login, self.headers.get("Cookie"))
            return login, session_id, session, created

        def do_GET(self) -> None:  # noqa: N802
            context = self._context()
            if context is None:
                return
            login, session_id, session, created = context
            path = urlparse(self.path).path
            if is_admin_path(path) and not application.is_admin_login(login):
                self._send_admin_only()
                return
            try:
                if path == "/legacy":
                    # The previous cockpit, kept for what v2 has not taken over.
                    body = _HTML_PATH.read_bytes()
                    content_type = "text/html; charset=utf-8"
                elif path == "/legacy/review":
                    # ADR-0006/0009: the retired review planes, read-only history.
                    body = _LEGACY_HTML_PATH.read_bytes()
                    content_type = "text/html; charset=utf-8"
                elif path == "/weekly-brief":
                    body = _WEEKLY_BRIEF_HTML_PATH.read_bytes()
                    content_type = "text/html; charset=utf-8"
                elif path == "/favicon.ico":
                    body = _FAVICON_SVG
                    content_type = "image/svg+xml"
                elif path == "/" or path == "/admin" or path in V2_ALIASES \
                        or path == "/v2" or path.startswith("/v2/"):
                    # "/" and "/admin" are the v2 shell and administrator page,
                    # served identically to /v2 and /v2/admin (no redirect, so
                    # hash routes and every /v2/static|assets|data URL stay put).
                    query = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
                    try:
                        answer = v2_router.get(V2_ALIASES.get(path, path), login, query)
                    except V2NotFound:
                        self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}')
                        return
                    except CockpitError as exc:
                        self._send(HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8",
                                   _cockpit_error_body("cockpit", exc))
                        return
                    content_type, body = answer.content_type, answer.body
                    if answer.json_value is not None:
                        value = {**answer.json_value, "csrf_token": session.csrf}
                        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    elif answer.status != HTTPStatus.OK:
                        # An extension's own refusal (a raw GET) keeps its status.
                        self._send(answer.status, content_type, body)
                        return
                elif path.startswith("/v1/cockpit/"):
                    query = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
                    try:
                        value = application.cockpit_view(path, login, query)
                    except CockpitError as exc:
                        self._send(HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8",
                                   _cockpit_error_body("cockpit", exc))
                        return
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/agenda":
                    value = application.plane.view(login)
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/research-review":
                    value = (
                        {"as_of": datetime.now(timezone.utc).isoformat(), "items": [], "enabled": False}
                        if application.review_plane is None
                        else {**application.review_plane.view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/mission-document-review":
                    value = (
                        {"as_of": datetime.now(timezone.utc).isoformat(), "items": [], "enabled": False}
                        if application.review_plane is None
                        else {**application.review_plane.document_review_view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/transcript-review":
                    value = (
                        {"as_of": datetime.now(timezone.utc).isoformat(), "items": [], "enabled": False}
                        if application.review_plane is None
                        else {**application.review_plane.transcript_view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/research-trajectory":
                    value = (
                        {
                            "as_of": datetime.now(timezone.utc).isoformat(),
                            "items": [],
                            "projection_only": True,
                            "enabled": False,
                        }
                        if application.review_plane is None
                        else {**application.review_plane.trajectory_view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/intent":
                    value = (
                        {
                            "as_of": datetime.now(timezone.utc).isoformat(),
                            "items": [],
                            "candidate_only": True,
                            "confirmation_enabled": False,
                            "execution_enabled": False,
                            "enabled": False,
                        }
                        if application.intent_plane is None
                        else {**application.intent_plane.view(login), "enabled": True}
                    )
                    value["csrf_token"] = session.csrf
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                elif path == "/v1/answer":
                    value = application.plane.answer_view()
                    value["csrf_token"] = session.csrf
                    body = json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                    content_type = "application/json; charset=utf-8"
                else:
                    self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}')
                    return
            except Exception:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json", b'{"error":"unavailable"}')
                return
            self._send(
                HTTPStatus.OK, content_type, body,
                session_cookie=session_id if created else None,
            )

        def do_POST(self) -> None:  # noqa: N802
            context = self._context()
            if context is None:
                return
            login, session_id, session, created = context
            if getattr(self, "shared_prefix", "") and self.headers.get("Origin") != application.config.shared_gateway["public_origin"]:
                self._send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"origin"}')
                return
            path = urlparse(self.path).path
            if is_admin_path(path) and not application.is_admin_login(login):
                self._send_admin_only()
                return
            actions = {
                "/v1/agenda/feedback": application.post,
                "/v1/research-review/decision": application.post_review,
                "/v1/transcript-review/decision": application.post_transcript_review,
                "/v1/mission-document-review/decision": application.post_document_review,
                "/v1/mission-document-review/evidence": application.post_document_evidence,
                "/v1/mission-document-review/extract": application.post_document_extraction,
                "/v1/mission-document-review/stage": application.post_document_staging,
                "/v1/intent/compose": application.post_intent,
                "/v1/intent/confirm": application.post_intent_confirm,
                "/v1/answer/route": application.post_answer,
                "/v1/answer/refresh/dispatch": application.post_answer_refresh,
            }
            if path.startswith("/v1/cockpit/"):
                name = path.rsplit("/", 1)[1]
                actions[path] = lambda *args, _name=name: application.post_cockpit(_name, *args)
            # ADR-0016: a v2 write registered by an extension (the router
            # checks the CSRF token before its handler runs).
            if path.startswith("/v2/") and v2_router.handles_post(path):
                actions[path] = lambda *args, _path=path: v2_router.post(_path, *args)
            action = actions.get(path)
            if action is None:
                self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"not_found"}')
                return
            if self.headers.get_content_type() != "application/json":
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "application/json", b'{"error":"content_type"}')
                return
            length = self.headers.get("Content-Length")
            limit = (MAX_TEMPLATE_UPLOAD_BODY_BYTES if path == TEMPLATE_UPLOAD_PATH
                     else MAX_BODY_BYTES)
            if not isinstance(length, str) or not length.isdigit() or int(length) > limit:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "application/json", b'{"error":"body_size"}')
                return
            try:
                result = action(
                    login, session, self.headers.get("X-Dalton-CSRF"), self.rfile.read(int(length))
                )
            except AdminRoleRequired:
                self._send_admin_only()
                return
            except PermissionError:
                self._send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"csrf"}')
                return
            except CockpitConflict as exc:
                self._send(HTTPStatus.CONFLICT, "application/json; charset=utf-8",
                           _cockpit_error_body("conflict", exc))
                return
            except CockpitError as exc:
                self._send(HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8",
                           _cockpit_error_body("cockpit", exc))
                return
            except (
                AgendaControlError, HumanIntentError,
                ResearchReviewControlError, ResearchReviewError,
            ):
                self._send(HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8",
                           json.dumps({"error": "invalid_request",
                                       "display_message": "请求内容无效，没有执行。"},
                                      ensure_ascii=False).encode())
                return
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            self._send(
                HTTPStatus.OK, "application/json; charset=utf-8", body,
                session_cookie=session_id if created else None,
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def _research_task_grant(config: AgendaControlConfig) -> Any:
    """The resolver ``adhoc_research_enabled`` needs, or None when it cannot be.

    P14e made the flag a question and left nobody to ask: the plane accepted a
    resolver, no caller passed one, and ``adhoc_research_enabled`` therefore
    answered False for a reason that had nothing to do with the owner's
    grant.  The cockpit section is where this process learns the Core's path,
    so a Core configured without it still answers False -- but now because the
    path is unknown rather than because the wiring was never finished.
    """

    if config.cockpit is None:
        return None
    from .research_task import cockpit_grant_resolver

    return cockpit_grant_resolver(
        config.cockpit.core_db, mission_ref=config.cockpit.mission_ref,
    )


#: 2026-09-30 (P1-1): the GIL switch interval of the serving process.
#:
#: The overview rebuild runs on a background thread and is mostly Python.
#: Every SQLite step and file read on a request thread releases the GIL, and
#: getting it back waits up to one switch interval behind that thread.  At
#: CPython's default 5 ms, a model page that makes ~1,500 such calls went from
#: 0.2 s to 40 s next to one busy thread (offline, legacy data, 2026-09-30);
#: at 1 ms it takes ~2 s.  A thread running alone is unaffected (the interval
#: only matters while another thread is waiting), and one millisecond is still
#: far above the cost of a switch.
SERVE_GIL_SWITCH_INTERVAL_SECONDS = 0.001


def serve(config: AgendaControlConfig) -> None:
    if config.host not in {"127.0.0.1", "::1"}:
        raise AgendaControlError("Agenda control must bind loopback")
    sys.setswitchinterval(SERVE_GIL_SWITCH_INTERVAL_SECONDS)
    plane = AgendaControlPlane(
        config, research_task_grant=_research_task_grant(config),
    )
    review_plane = (
        None
        if config.research_review is None
        else ResearchReviewControlPlane(
            config.research_review,
            writer_socket=config.writer_socket,
            token_config=config.token_config,
        )
    )
    intent_plane = None
    if config.intent_composer is not None:
        def context_provider(login: str) -> Mapping[str, Any]:
            review = (
                {"items": []}
                if review_plane is None
                else review_plane.view(login)
            )
            transcript = (
                {"items": []}
                if review_plane is None
                else review_plane.transcript_view(login)
            )
            trajectory = (
                {"items": []}
                if review_plane is None
                else review_plane.trajectory_view(login)
            )
            return build_cockpit_intent_context(
                agenda=plane.view(login),
                research_review=review,
                transcript_review=transcript,
                trajectory=trajectory,
                extra_bindings=plane.intent_context_bindings(),
            )

        intent_plane = NaturalLanguageComposerPlane(
            config.intent_composer,
            context_provider=context_provider,
            dispatcher=CockpitIntentDispatcher(
                config, plane, review_plane
            ),
        )
    cockpit_plane = None
    if config.cockpit is not None:
        cockpit_plane = CockpitPlane(
            config.cockpit, writer_socket=config.writer_socket,
            token_config=config.token_config,
            # B1-5: this is the long-lived process with overlapping readers --
            # a browser tab polling, a second tab on the tailnet, the
            # workspace readiness probe -- and it is the one where a full
            # overview rebuild took 10 to 30 s.  Every other construction of
            # this plane is a test or a one-shot command and keeps exact
            # read-after-write by taking the zero default.
            overview_ttl_seconds=OVERVIEW_TTL_SECONDS,
            # 2026-09-25: past the TTL, answer from the last snapshot and
            # rebuild once in the background instead of making the page wait
            # out a rebuild longer than its own 30 s timeout.
            overview_stale_seconds=OVERVIEW_STALE_SECONDS,
        )
    application = AgendaControlApplication(
        config, plane, review_plane, intent_plane, cockpit_plane
    )
    stop = threading.Event()
    if cockpit_plane is not None:
        # 2026-09-25: build the first overview while the port opens, so the
        # first page load after a restart is not the one that waits for it.
        def warm_overview() -> None:
            try:
                cockpit_plane.overview()
            except Exception:
                pass

        threading.Thread(target=warm_overview, name="dalton-cockpit-overview-warm",
                         daemon=True).start()

    def sweep_loop() -> None:
        while not stop.is_set():
            try:
                plane.sweep()
            except Exception:
                pass
            stop.wait(config.sweep_interval_seconds)

    sweeper = threading.Thread(target=sweep_loop, name="dalton-agenda-timeout", daemon=True)
    sweeper.start()
    reconciler = None
    if review_plane is not None:
        def reconcile_loop() -> None:
            while not stop.is_set():
                try:
                    review_plane.reconcile()
                except Exception:
                    pass
                stop.wait(config.research_review.reconcile_interval_seconds)

        reconciler = threading.Thread(
            target=reconcile_loop,
            name="dalton-research-review-reconcile",
            daemon=True,
        )
        reconciler.start()
    server = ThreadingHTTPServer((config.host, config.port), _handler(application))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        sweeper.join(timeout=5)
        if reconciler is not None:
            reconciler.join(timeout=5)
        if review_plane is not None:
            review_plane.close()
        if cockpit_plane is not None:
            cockpit_plane.close()
        if intent_plane is not None:
            intent_plane.close()


def main(argv: Iterable[str] | None = None) -> int:
    # P3 (2026-09-30): every stderr line of this daemon carries its time.
    from .stderr_timestamps import timestamped_stderr

    with timestamped_stderr():
        return _main(argv)


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the owner-only Dalton Cockpit")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(AgendaControlConfig.from_service_file(args.config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ADMIN_ONLY_COCKPIT_ACTIONS", "AdminRoleRequired", "AgendaControlApplication",
    "AgendaControlConfig", "AgendaControlError", "AgendaControlPlane",
    "CockpitIntentDispatcher", "is_admin_path", "serve",
]
