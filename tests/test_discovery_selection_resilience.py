"""2026-10-02: one failed selection ticket stalled AlphaEngine for days.

Legacy's selection for one discovery failed once with ``OperationalError:
database is locked`` (2026-09-30), ws-7d's once with ``CandidateSelectionError:
selection has an invalid closed shape`` (2026-10-01, the model added a
``_note`` key).  ``max_recovery_epochs`` defaulted to 0, so the ticket was
``exhausted`` at once, the coordinator answered ``selection_pending:
exhausted`` for the same discovery on every tick, and nothing behind it was
acquired.  The identity only changes with the candidate view, the mission
version, the missing periods or the model config -- not as new candidates are
discovered -- so it never cleared by itself.

Now a failed ticket is retried (default two more epochs, the existing
cooldown), an exhausted one is quarantined with its ticket and reason so the
queue moves, an owner release (or a rules bump) grants one fresh attempt, and
harmless wrapping of a model reply is no longer a failure.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dalton_core.discovery_candidate_selection import (
    CONTRACT_REF, SELECTION_RULES_VERSION, CandidateSelectionError, validate_selection,
)
from dalton_core.discovery_selection_launcher import (
    DEFAULT_MAX_RECOVERY_EPOCHS, DiscoverySelectionLauncher,
)
from dalton_core.store import content_hash

OWNER = "human:owner"


class _Process:
    pid = 43210

    def __init__(self) -> None:
        self.code = None

    def poll(self):
        return self.code


def _view():
    base = {"schema_version": "0.1", "contract_ref": CONTRACT_REF,
            "source_envelope_ref": "source-envelope:meta", "source_envelope_hash": "a" * 64,
            "candidates": [
                {"document_ref": "alphaengine-doc:130000101576482", "rank": 1,
                 "title": "Meta Platforms Q2 2026 earnings call summary"},
                {"document_ref": "alphaengine-doc:130000101576483", "rank": 2,
                 "title": "Meta Platforms Q3 2025 earnings call transcript"}]}
    return {**base, "content_hash": content_hash(base)}


class LauncherRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "model.json"
        # The live configs carry no ``capacity_retry`` at all.
        self.config.write_text(json.dumps({"provider_retry": {}}))
        self.args = dict(discovery_ref="discovery:meta", view=_view(), mission_ref="mission:v8",
                         company={"company_ref": "c", "name": "META", "ticker": "META",
                                  "aliases": ["META"]},
                         missing_periods=["FY2025-Q3"])
        self.processes: list[_Process] = []

    def launcher(self) -> DiscoverySelectionLauncher:
        return DiscoverySelectionLauncher(state_dir=self.root, model_config_path=self.config,
                                          scheduler_db=self.root / "scheduler.sqlite")

    def _popen(self, *args, **kwargs):
        process = _Process()
        self.processes.append(process)
        return process

    def fail(self, launcher, ticket, reason="OperationalError: database is locked"):
        """The child writes its failure summary and exits 1, as the CLI does."""

        directory = launcher.root / ticket["id"].split(":", 1)[1]
        (directory / "summary.json").write_text(json.dumps({
            "schema_version": "0.1", "status": "failed",
            "identity_hash": ticket["identity_hash"], "failure_reason": reason}))
        self.processes[-1].code = 1
        return launcher.status(ticket["id"])

    def after_cooldown(self, launcher, ticket):
        completed = datetime.fromisoformat(ticket["completed_at"])
        launcher._now_datetime = lambda: completed + timedelta(seconds=301)

    def test_the_default_retries_a_transient_failure_and_the_override_still_holds(self) -> None:
        self.assertEqual(DEFAULT_MAX_RECOVERY_EPOCHS, 2)
        launcher = self.launcher()
        self.assertEqual((launcher.max_recovery_epochs, launcher.cooldown_seconds), (2, 300))
        with patch("dalton_core.discovery_selection_launcher.subprocess.Popen",
                   side_effect=self._popen):
            first = launcher.start(**self.args)
            failed = self.fail(launcher, first)
            self.assertEqual(launcher.start(**self.args)["status"], "cooldown")
            self.after_cooldown(launcher, failed)
            retry = launcher.start(**self.args)
        self.assertEqual((retry["status"], retry["recovery_epoch"]), ("running", 1))
        self.assertEqual(retry["base_identity_hash"], first["base_identity_hash"])

        self.config.write_text(json.dumps({"capacity_retry": {"max_recovery_epochs": 0}}))
        self.assertEqual(self.launcher().max_recovery_epochs, 0)
        for bad in (11, -1, True, "2"):
            self.config.write_text(json.dumps({"capacity_retry": {"max_recovery_epochs": bad}}))
            with self.assertRaises(ValueError):
                self.launcher()

    def test_a_ticket_failed_before_the_new_default_gets_its_next_epoch(self) -> None:
        """The two live stalls: epoch 0 failed days ago under the old default.

        Neither live config has ``capacity_retry``, so the config bytes -- and
        with them the identity -- are unchanged by the deploy; the next tick
        simply finds epoch 1 allowed and the cooldown long over.
        """

        with patch("dalton_core.discovery_selection_launcher.subprocess.Popen",
                   side_effect=self._popen):
            with patch("dalton_core.discovery_selection_launcher.DEFAULT_MAX_RECOVERY_EPOCHS", 0):
                before = self.launcher()
                failed = self.fail(before, before.start(**self.args),
                                   "CandidateSelectionError: selection has an invalid closed shape")
                before._now_datetime = (
                    lambda: datetime.fromisoformat(failed["completed_at"]) + timedelta(days=1))
                self.assertEqual(before.start(**self.args)["status"], "exhausted")
            after = self.launcher()
            after._now_datetime = before._now_datetime
            retry = after.start(**self.args)
        self.assertEqual((retry["status"], retry["recovery_epoch"]), ("running", 1))
        self.assertEqual(retry["base_identity_hash"], failed["base_identity_hash"])

    def test_an_exhausted_ticket_says_why_and_a_release_grants_one_fresh_attempt(self) -> None:
        launcher = self.launcher()
        with patch("dalton_core.discovery_selection_launcher.subprocess.Popen",
                   side_effect=self._popen):
            ticket = launcher.start(**self.args)
            for _ in range(DEFAULT_MAX_RECOVERY_EPOCHS):
                failed = self.fail(launcher, ticket)
                self.after_cooldown(launcher, failed)
                ticket = launcher.start(**self.args)
                self.assertEqual(ticket["status"], "running")
            failed = self.fail(launcher, ticket)
            self.after_cooldown(launcher, failed)
            exhausted = launcher.start(**self.args)
            self.assertEqual(exhausted["status"], "exhausted")
            self.assertEqual(exhausted["id"], failed["id"])
            self.assertEqual(exhausted["failure_reason"], "OperationalError: database is locked")
            self.assertEqual(exhausted["attempts"], DEFAULT_MAX_RECOVERY_EPOCHS + 1)

            # Quarantined (by the coordinator): still exhausted, nothing launched.
            launcher.quarantine.quarantine(
                discovery_ref="discovery:meta", reason="exhausted", error_type="X",
                rules_version=SELECTION_RULES_VERSION)
            self.assertEqual(launcher.start(**self.args)["status"], "exhausted")
            launched = len(self.processes)

            # The owner releases it: one fresh attempt at once, no cooldown.
            launcher.quarantine.release("discovery:meta", actor_ref=OWNER, reason="lock cleared")
            again = launcher.start(**self.args)
            self.assertEqual(again["status"], "running")
            self.assertEqual(len(self.processes), launched + 1)
            # ... and if it fails as well, it is exhausted again.
            failed = self.fail(launcher, again)
            self.after_cooldown(launcher, failed)
            self.assertEqual(launcher.start(**self.args)["status"], "exhausted")

    def test_a_rules_bump_lapses_the_quarantine_and_grants_one_attempt(self) -> None:
        self.config.write_text(json.dumps({"capacity_retry": {"max_recovery_epochs": 0}}))
        launcher = self.launcher()
        with patch("dalton_core.discovery_selection_launcher.subprocess.Popen",
                   side_effect=self._popen):
            failed = self.fail(launcher, launcher.start(**self.args))
            self.after_cooldown(launcher, failed)
            self.assertEqual(launcher.start(**self.args)["status"], "exhausted")
            launcher.quarantine.quarantine(discovery_ref="discovery:meta", reason="exhausted",
                                           error_type="X", rules_version="older-rules")
            self.assertEqual(launcher.start(**self.args)["status"], "running")


class ReplyUnwrappingTests(unittest.TestCase):
    """Replies of the kind the selection models actually give."""

    STRICT = ('{"selected":[{"document_ref":"alphaengine-doc:130000101576483",'
              '"reason":"Meta Q3 2025 earnings call transcript"}]}')

    def check(self, text):
        return validate_selection(text, _view(), missing_periods=["FY2025-Q3"],
                                  selection_context={"research_purpose": "earnings_call_transcript",
                                                     "research_question": "q"})

    def test_strict_json_is_unchanged(self) -> None:
        self.assertEqual(self.check(self.STRICT)["selected"][0]["document_ref"],
                         "alphaengine-doc:130000101576483")

    def test_fenced_and_prose_wrapped_replies_validate_to_the_same_selection(self) -> None:
        strict = self.check(self.STRICT)
        pretty = json.dumps(json.loads(self.STRICT), indent=2, ensure_ascii=False)
        for reply in (
            f"```json\n{pretty}\n```",
            f"```\n{pretty}\n```\n",
            f"Here is the selection:\n\n```json\n{pretty}\n```\n\nOnly the Q3 2025 call matches.",
            f"Here is the strict JSON you asked for:\n{pretty}\nLet me know if you need more.",
            f"  {self.STRICT}  \n",
        ):
            with self.subTest(reply=reply[:40]):
                self.assertEqual(self.check(reply), strict)

    def test_anything_but_the_closed_shape_is_still_refused(self) -> None:
        note = ('{\n  "selected": [\n    {\n      "document_ref": "alphaengine-doc:130000101576482",\n'
                '      "reason": "Q2 2026, not a missing period"\n    }\n  ],\n'
                '  "_note": "No candidate covers missing periods FY2025-Q3."\n}')
        for reply in (
            note,                                         # ws-7d 2026-10-01, verbatim shape
            f"```json\n{note}\n```",                      # wrapped, still not closed
            "```json\n{\"selected\": [}\n```",           # fenced, not JSON
            "I could not find any matching document.",    # no JSON at all
            f"{self.STRICT}\n{self.STRICT}",              # two objects: ambiguous
            f"```json\n{self.STRICT}\n```\n```json\n{self.STRICT}\n```",  # two fences
            '[{"document_ref":"alphaengine-doc:130000101576483","reason":"x"}]',
            '{"selected": {"document_ref": "alphaengine-doc:130000101576483"}}',
        ):
            with self.subTest(reply=reply[:40]):
                with self.assertRaises(CandidateSelectionError):
                    self.check(reply)


if __name__ == "__main__":
    unittest.main()
