"""Owner recovery doors after the 2026-10-02/03 legacy incident.

* A: an owner-bought unproved-send retry came back within hours with the same
  bare reason.  Four were glm-5.3 drafts that spent the admitted 4096-token
  output cap thinking and returned "" (INVALID_HOST_RESULT, charged); one was
  a verifier the host refused before sending (HOST_CONTROL_PROOF_MISSING, not
  charged).  The hold reason now names the cause and profile, the CLI refuses
  to buy a retry the evidence says is pointless, and names ``withdraw``.
* B: ``authorize-unproved`` died in the writer on "model authority refresh has
  an unresolved prior execution epoch"; the door now answers with a refusal
  and the next step, and ``authorize-all-escalated`` reports every admission.
* C: the door re-verified the same document search proof on every derivation
  of the stage chain, on the single store thread; a started request is now
  watched for longer before the CLI sends it again.

Everything here runs on synthetic state; nothing touches a live directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core import document_recovery_cli as cli
from dalton_core.mission_document_research_lane import (
    HOLDS_FILE, MissionDocumentResearchCoordinator, UNPROVED_SEND_ESCALATION_NOTE,
    _write_holds, authorize_owner_recovery, unproved_send_hold_reason,
)
from dalton_core.mission_document_research_executor import (
    MissionDocumentResearchExecutorError,
)

UNPROVED = "unproved_send_failed_after_automatic_retry"
GLM = "model-profile-version:broker-zai-glm-5-3-077b71628858e04f:4"
GEMINI = "model-profile-version:broker-gemini-3-8-flash-a552094a54755d7d:6"


class HoldReasonCauseTests(unittest.TestCase):
    def test_the_reason_keeps_its_prefix_and_names_code_cause_and_profile(self):
        reason = unproved_send_hold_reason(UNPROVED, "HOST_CONTROL_PROOF_MISSING", GEMINI)
        self.assertTrue(reason.startswith(UNPROVED + ":"))
        self.assertIn(":HOST_CONTROL_PROOF_MISSING:not_sent_host_preflight_refused:", reason)
        self.assertTrue(reason.endswith("profile=" + GEMINI))
        self.assertEqual(
            unproved_send_hold_reason(UNPROVED, "INVALID_HOST_RESULT", GLM),
            f"{UNPROVED}:INVALID_HOST_RESULT:empty_or_invalid_reply:profile={GLM}")
        # Unknown codes are kept verbatim without a cause; junk is dropped.
        self.assertEqual(unproved_send_hold_reason(UNPROVED, "NEW_CODE", None),
                         UNPROVED + ":NEW_CODE")
        self.assertEqual(unproved_send_hold_reason(UNPROVED, "a b:c", None), UNPROVED)
        self.assertEqual(unproved_send_hold_reason(UNPROVED, None, None), UNPROVED)

    def test_the_lane_reads_the_cause_from_the_failed_works_formal_result(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE scheduler_formal_results("
                           "work_order_id TEXT, result_envelope_json TEXT)")
        connection.execute("INSERT INTO scheduler_formal_results VALUES(?,?)", (
            "work:retry", json.dumps({
                "error": {"code": "HOST_CONTROL_PROOF_MISSING"},
                "metadata": {"profile_version_ref": GEMINI}})))
        fake = SimpleNamespace(store=SimpleNamespace(connection=connection))
        method = MissionDocumentResearchCoordinator._stopped_reason_with_cause
        reason = method(fake, "work:retry", {"reason": UNPROVED, "proof": {}})
        self.assertIn("HOST_CONTROL_PROOF_MISSING:not_sent_host_preflight_refused", reason)
        self.assertIn(GEMINI, reason)
        # No formal row: the recovery proof's error code is the fallback.
        reason = method(fake, "work:other", {
            "reason": UNPROVED, "proof": {"error_code": "INVALID_HOST_RESULT"}})
        self.assertEqual(reason, UNPROVED + ":INVALID_HOST_RESULT:empty_or_invalid_reply")
        # Every other stopped reason is passed through unchanged.
        self.assertEqual(method(fake, "work:retry", {
            "reason": "contract_failed_after_automatic_retry"}),
            "contract_failed_after_automatic_retry")

    def test_every_matcher_still_recognizes_a_suffixed_reason(self):
        from dalton_core.mission_document_research_withdrawal import withdrawable_hold
        from dalton_core.needs_human import hold_reason_label

        reason = unproved_send_hold_reason(UNPROVED, "INVALID_HOST_RESULT", GLM)
        self.assertEqual(cli.door_for(reason), cli.UNPROVED_OPERATION)
        self.assertEqual(withdrawable_hold(
            {"disposition": "recovery_required", "reason": reason}), (True, "escalated"))
        self.assertIn("回复为空", hold_reason_label(reason))
        self.assertIn("发送前", hold_reason_label(
            unproved_send_hold_reason(UNPROVED, "HOST_CONTROL_PROOF_MISSING", GEMINI)))
        from dalton_core.store import DaltonStore
        from dalton_core.mission_document_research import _SCHEMA_PATH
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        store.connection.executescript(_SCHEMA_PATH.read_text())
        fake = SimpleNamespace(store=store, _started=lambda _ref: True)
        detail = MissionDocumentResearchCoordinator._hold_detail(
            fake, {"mission-document-research-admission:" + "1" * 32: {
                "disposition": "recovery_required", "reason": reason,
                "ticket_ref": None}}, [])
        self.assertEqual(detail[0]["owner_action"], UNPROVED_SEND_ESCALATION_NOTE)

    def test_the_owner_is_told_to_withdraw_a_verifier_refused_before_send(self):
        from dalton_core.needs_human import _retry_words

        what, todo = _retry_words(
            unproved_send_hold_reason(UNPROVED, "HOST_CONTROL_PROOF_MISSING", GEMINI))
        self.assertIn("没有计费", what)
        self.assertIn("withdraw", todo)
        self.assertNotIn("authorize-unproved", todo)


class _Executor:
    def __init__(self, method):
        self.authority = SimpleNamespace(
            connection=None,
            resolve_for_execution=lambda ref: {"id": ref, "content_hash": "h" * 64})
        self.scheduler = SimpleNamespace(formal_result=lambda _ref: None)
        self.draft_worker = object()
        self.verifier_worker = object()
        self.connection = None
        self.authorize_unproved_send_recovery = method


class OwnerDoorRefusalTests(unittest.TestCase):
    REF = "mission-document-research-admission:49d92fa70b8ef1b2e3163f5d8dbd8449"

    def test_an_unresolved_prior_epoch_is_a_refusal_with_the_withdraw_step(self):
        method = mock.Mock()
        with mock.patch(
                "dalton_core.mission_document_research_lane._escalated_stage_ordinal",
                return_value=3), \
                mock.patch(
                    "dalton_core.mission_document_research_executor._effective_stage",
                    side_effect=MissionDocumentResearchExecutorError(
                        "model authority refresh has an unresolved prior execution epoch")):
            result = authorize_owner_recovery(
                _Executor(method), self.REF, door="unproved", actor_ref="human:owner")
        method.assert_not_called()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "model_authority_epoch_unresolved")
        self.assertEqual(result["stage_ordinal"], 3)
        self.assertEqual(result["next_step"], "withdraw")
        self.assertIn("document_recovery_cli withdraw", result["next_command"])
        self.assertIn(self.REF, result["next_command"])
        self.assertEqual(result["model_calls"], 0)

    def test_an_executor_refusal_before_any_write_is_answered_not_raised(self):
        stored = {"id": "mission-document-unproved-send-recovery-authorization:x"}
        work = {"id": "work:failed", "budget": {"max_cost_usd": 0.5}}
        method = mock.Mock(side_effect=MissionDocumentResearchExecutorError(
            "unproved send recovery target was already owner-recovered"))
        common = [
            mock.patch("dalton_core.mission_document_research_lane."
                       "_escalated_stage_ordinal", return_value=2),
            mock.patch("dalton_core.mission_document_research_executor."
                       "_effective_stage", return_value=(work, [])),
        ]
        with contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            stack.enter_context(mock.patch(
                "dalton_core.mission_document_research_lane."
                "_existing_owner_authorization", side_effect=[stored, None]))
            result = authorize_owner_recovery(
                _Executor(method), self.REF, door="unproved", actor_ref="human:owner")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "executor_refused_owner_recovery")
        self.assertIn("already owner-recovered", result["detail"])
        # Once a row of this door exists the door has (partly) opened: raise.
        with contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            stack.enter_context(mock.patch(
                "dalton_core.mission_document_research_lane."
                "_existing_owner_authorization", side_effect=[stored, stored]))
            with self.assertRaises(MissionDocumentResearchExecutorError):
                authorize_owner_recovery(
                    _Executor(method), self.REF, door="unproved",
                    actor_ref="human:owner")


class SearchProofReplayTests(unittest.TestCase):
    def test_one_door_verifies_each_proof_once(self):
        from dalton_core.document_research import (
            SEARCH_OPERATION, SEARCH_PROOF_SCHEMA_VERSION, DocumentResearchConflict,
            DocumentResearchRegistry,
        )

        proof = {"schema_version": SEARCH_PROOF_SCHEMA_VERSION,
                 "operation": SEARCH_OPERATION, "request": {"q": 1}, "matches": []}
        registry = object.__new__(DocumentResearchRegistry)
        registry.search = mock.Mock(side_effect=lambda _request: json.loads(
            json.dumps(proof)))
        with registry.replayed_search_proofs():
            first = registry.verify_search_proof(proof)
            first["matches"].append("mutated by a caller")
            second = registry.verify_search_proof(proof)
            with registry.replayed_search_proofs():  # nested: same memo
                registry.verify_search_proof(proof)
        self.assertEqual(registry.search.call_count, 1)
        self.assertEqual(second, proof)
        # Outside the block nothing is remembered.
        registry.verify_search_proof(proof)
        self.assertEqual(registry.search.call_count, 2)
        # A proof that does not replay is never remembered as one that did.
        registry.search = mock.Mock(return_value={"other": True})
        with registry.replayed_search_proofs():
            for _ in range(2):
                with self.assertRaises(DocumentResearchConflict):
                    registry.verify_search_proof(proof)
        self.assertEqual(registry.search.call_count, 2)


class _State:
    """A synthetic legacy-shaped state directory with escalated holds."""

    def __init__(self, case: unittest.TestCase):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.runs = self.state / "mission-document-research-runs"
        self.runs.mkdir()
        self.core = sqlite3.connect(self.state / "core.sqlite")
        self.core.executescript("""
            CREATE TABLE mission_document_research_admissions (
              admission_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
            CREATE TABLE mission_document_research_observations (
              observation_id TEXT PRIMARY KEY, admission_ref TEXT, outcome TEXT,
              record_json TEXT, created_at TEXT);
            CREATE TABLE mission_document_research_controlled_recovery_authorizations (
              authorization_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
              stage_ordinal INTEGER NOT NULL, failed_work_order_ref TEXT NOT NULL,
              record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE mission_document_research_recovery_links (
              recovery_link_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
              stage_ordinal INTEGER NOT NULL, recovery_number INTEGER NOT NULL,
              failed_work_order_ref TEXT NOT NULL, recovery_work_order_ref TEXT NOT NULL,
              record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE mission_document_research_model_authority_epoch_rebinds (
              rebind_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
              stage_ordinal INTEGER NOT NULL, recovery_link_ref TEXT NOT NULL,
              authorized_recovery_work_ref TEXT NOT NULL, current_base_work_ref TEXT NOT NULL,
              rebound_work_order_ref TEXT NOT NULL, record_json TEXT NOT NULL,
              content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE scheduler_formal_results (
              result_record_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL UNIQUE,
              terminal_state TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        self.holds: dict[str, dict] = {}
        self._tickets = 0

    def admission(self, name: str, *, stage: str, reason: str, started: str,
                  outcomes: list[tuple[str, dict]]) -> str:
        ref = "mission-document-research-admission:" + name.ljust(32, "0")
        self.core.execute(
            "INSERT INTO mission_document_research_admissions VALUES(?,?)",
            (ref, json.dumps({"model_execution": {"draft": {"max_output_tokens": 4096}}})))
        self.core.execute(
            "INSERT INTO mission_document_research_observations VALUES(?,?,?,?,?)",
            ("obs:" + name, ref, "recovery_required", json.dumps({"stage": stage}),
             started))
        self.core.commit()
        digest = None
        for created_at, outcome in outcomes:
            self._tickets += 1
            digest = f"{name[:12]}{self._tickets:012d}"
            directory = self.runs / digest
            directory.mkdir()
            (directory / "summary.json").write_text(json.dumps({
                "admission_ref": ref, "status": "blocked", "created_at": created_at,
                "outcomes": [{"stage": stage, **outcome}]}), encoding="utf-8")
            (directory / "ticket.json").write_text(json.dumps({
                "id": "mission-document-research:" + digest, "started_at": started}),
                encoding="utf-8")
        self.holds[ref] = {
            "admission_hash": "a" * 64,
            "ticket_ref": None if digest is None else "mission-document-research:" + digest,
            "reason": reason, "disposition": "recovery_required", "retry_at": None}
        _write_holds(self.runs / HOLDS_FILE, self.holds)
        return ref


def _failed(code, *, output_tokens=None, model="glm-5.3", status="failed"):
    return {"status": status, "result": {"error": {"code": code}},
            "accounting": {"usage": {"output_tokens": output_tokens, "model": model}}}


class OwnerDoorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _State(self)
        self.state = self.fixture.state
        # Live f3cdba70: an early contract rejection well under the cap kept
        # it off the hopeless list; the retry the owner bought came back empty.
        self.glm = self.fixture.admission(
            "f3cdba70", stage="qualitative_model_draft",
            reason=unproved_send_hold_reason(UNPROVED, "INVALID_HOST_RESULT", GLM),
            started="2026-09-26T08:29:25+00:00", outcomes=[
                ("2026-09-26T08:32:00+00:00",
                 _failed("MODEL_OUTPUT_CONTRACT_REJECTED", output_tokens=1645,
                         status="retryable")),
                ("2026-10-02T15:11:05+00:00", _failed("INVALID_HOST_RESULT")),
            ])
        # Live 131c8936: the verifier refused before sending, twice.
        self.verifier = self.fixture.admission(
            "131c8936", stage="independent_qualitative_verifier",
            reason=unproved_send_hold_reason(
                UNPROVED, "HOST_CONTROL_PROOF_MISSING", GEMINI),
            started="2026-10-02T16:25:34+00:00", outcomes=[
                ("2026-10-02T16:30:35+00:00",
                 _failed("HOST_CONTROL_PROOF_MISSING", model="gemini-3.8-flash")),
                ("2026-10-02T16:32:49+00:00",
                 _failed("HOST_CONTROL_PROOF_MISSING", model="gemini-3.8-flash")),
            ])
        # An ordinary unproved send: nothing says a retry is pointless.
        self.plain = self.fixture.admission(
            "e2712605", stage="qualitative_model_draft", reason=UNPROVED,
            started="2026-10-01T00:00:00+00:00", outcomes=[
                ("2026-10-01T00:05:00+00:00", _failed("HOST_COMPLETION_FAILED")),
            ])

    def _classes(self):
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000):
            return {item["admission_ref"]: item["class"]
                    for item in cli.unrecoverable_holds(self.state)}

    def test_the_latest_failure_decides_and_a_verifier_refused_before_send_is_listed(self):
        self.assertEqual(self._classes(), {
            self.glm: "output_cap_exhausted_below_current_budget",
            self.verifier: "verifier_refused_before_send",
        })
        # One refusal could be a hiccup of the token count; two is the input.
        sorted(self.fixture.runs.glob("131c8936*/summary.json"))[-1].unlink()
        self.assertNotIn(self.verifier, self._classes())

    def test_the_single_door_refuses_to_buy_a_pointless_retry_and_names_withdraw(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000), \
                mock.patch.object(cli, "_ephemeral_call") as call, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["authorize-unproved", "--state-dir", str(self.state),
                             "--admission-ref", self.verifier, "--apply",
                             "--actor", "human:owner"])
        self.assertEqual(code, 1)
        call.assert_not_called()
        self.assertIn("verifier_refused_before_send", out.getvalue())
        self.assertIn("document_recovery_cli withdraw", out.getvalue())
        self.assertIn("--even-if-unrecoverable", err.getvalue())
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000), \
                mock.patch.object(cli, "_ephemeral_call",
                                  return_value={"status": "admitted"}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["authorize-unproved", "--state-dir", str(self.state),
                             "--admission-ref", self.verifier, "--apply",
                             "--actor", "human:owner", "--even-if-unrecoverable"])
        self.assertEqual(code, 0)
        call.assert_called_once()

    def test_a_writer_refusal_exits_nonzero_with_the_state_filled_in(self):
        refused = {"status": "refused", "reason": "model_authority_epoch_unresolved",
                   "next_command": "python -m dalton_core.document_recovery_cli "
                                   "withdraw --state-dir <state> --admission-ref x"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_ephemeral_call", return_value=refused), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["authorize-unproved", "--state-dir", str(self.state),
                             "--admission-ref", self.plain, "--apply",
                             "--actor", "human:owner"])
        self.assertEqual(code, 1)
        self.assertNotIn("<state>", out.getvalue())
        self.assertIn(str(self.state), out.getvalue())
        self.assertIn("model_authority_epoch_unresolved", err.getvalue())

    def test_all_escalated_reports_every_admission_and_survives_one_failure(self):
        from dalton_core.writer_protocol import RemoteError

        extra = self.fixture.admission(
            "ffff0000", stage="qualitative_model_draft", reason=UNPROVED,
            started="2026-10-01T06:00:00+00:00", outcomes=[
                ("2026-10-01T06:05:00+00:00", _failed("HOST_COMPLETION_FAILED"))])
        calls = []

        def _call(_state, *, operation, actor, params):
            calls.append(params["admission_ref"])
            if params["admission_ref"] == self.plain:
                raise RemoteError("internal_error", "writer op failed")
            return {"status": "admitted", "max_cost_usd": 0.1}

        out = io.StringIO()
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000), \
                mock.patch.object(cli, "_ephemeral_call", side_effect=_call), \
                contextlib.redirect_stdout(out):
            code = cli.authorize_all_escalated(
                self.state, actor="human:owner", apply=True, max_total_cost_usd=5.0)
        self.assertEqual(code, 1)
        # The failing one did not stop the walk; the hopeless ones were not bought.
        self.assertEqual(calls, [self.plain, extra])
        lines = [json.loads(line) for line in out.getvalue().splitlines()
                 if line.startswith("{\"admission_ref\"")]
        by_ref = {line["admission_ref"]: line["result"] for line in lines}
        self.assertEqual(by_ref[self.plain]["status"], "error")
        self.assertEqual(by_ref[self.plain]["code"], "internal_error")
        self.assertEqual(by_ref[extra]["status"], "admitted")
        for ref in (self.glm, self.verifier):
            self.assertEqual(by_ref[ref]["status"], "unrecoverable_withdraw_instead")
            self.assertIn("withdraw", by_ref[ref]["withdraw"])
        summary = json.loads(out.getvalue()[out.getvalue().rindex("\n{"):])
        self.assertEqual(summary["results"], {
            "unrecoverable_withdraw_instead": 2, "error": 1, "admitted": 1})

    def test_holds_offers_the_withdraw_command_beside_an_unrecoverable_hold(self):
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000):
            view = cli.holds(self.state)["mission_document_research"]
        by_ref = {item["admission_ref"]: item for item in view["escalated"]}
        self.assertIn("withdraw", by_ref[self.verifier]["withdraw_instead"])
        self.assertNotIn("withdraw_instead", by_ref[self.plain])


class StartedRequestTests(unittest.TestCase):
    def test_a_started_request_is_watched_before_it_is_sent_again(self):
        from dalton_core.writer_protocol import RemoteError

        now = [0.0]
        landed_at = 100.0  # well past the old 30 s window

        def _opened(*_args, **_kwargs):
            return {"status": "admitted"} if now[0] >= landed_at else None

        def _sleep(seconds):
            now[0] += seconds

        with mock.patch.object(cli, "_ephemeral_call",
                               side_effect=RemoteError("store_timeout", "slow")) as call, \
                mock.patch.object(cli, "door_opened_since", side_effect=_opened), \
                contextlib.redirect_stderr(io.StringIO()):
            result = cli._call(Path("/nonexistent"), operation=cli.UNPROVED_OPERATION,
                               actor="human:owner",
                               params={"admission_ref": "x"}, sleep=_sleep,
                               clock=lambda: now[0])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["confirmed_after"], "store_timeout")

    def test_a_request_the_queue_never_reached_is_sent_again_at_once(self):
        from dalton_core.writer_protocol import RemoteError

        with mock.patch.object(cli, "_ephemeral_call", side_effect=[
                RemoteError("queued_timeout", "queued"), {"status": "admitted"}]) as call, \
                mock.patch.object(cli, "door_opened_since", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            result = cli._call(Path("/nonexistent"), operation=cli.UNPROVED_OPERATION,
                               actor="human:owner", params={"admission_ref": "x"},
                               sleep=mock.Mock(), clock=lambda: 0.0)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result, {"status": "admitted"})


if __name__ == "__main__":
    unittest.main()
