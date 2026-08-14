import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest

from dalton_core.contracts import RuntimeProfile, WorkOrder
from dalton_core.process_runtime import (
    OutputLimitExceeded,
    ProcessContractError,
    ProcessProtocolError,
    ProcessRuntimeAdapter,
    ProcessTimeout,
)


TIME = "2026-01-01T00:00:00Z"


def make_profile(*, profile_id="profile-1", caps=("format.records",), side_effects=(), limits=None, identity=None):
    identity = identity or {
        "provider": "fixture", "model": "fixture",
        "model_family": "fixture-family", "actor_ref": "fixture:test",
    }
    return RuntimeProfile(
        schema_version="0.1", id=profile_id, created_at=TIME, version="1",
        capabilities=tuple(caps), isolation_level="temp-process", allowed_tools=(),
        network="disabled", filesystem="temp", side_effects=tuple(side_effects),
        limits=dict(limits or {}), supported_input_versions=("0.1",),
        supported_result_versions=("0.1",), runtime_version="test", environment_hash="env-test",
        metadata={"invocation_identity": identity},
    )


def make_work(*, profile_ref="profile-1", cap="format.records", declared=(), metadata=None, budget=None):
    return WorkOrder(
        schema_version="0.1", id="work-1", created_at=TIME, updated_at=TIME,
        question="format records", requested_capabilities=(cap,), runtime_profile_ref=profile_ref,
        budget=dict(budget or {"max_tokens": 10}), idempotency_key="work-key",
        declared_side_effects=tuple(declared), status="ready", metadata=dict(metadata or {}),
    )


def script_adapter(code, **kwargs):
    kwargs.setdefault("invocation_identity", {
        "provider": "fixture", "model": "fixture",
        "model_family": "fixture-family", "actor_ref": "fixture:test",
    })
    return ProcessRuntimeAdapter((sys.executable, "-c", textwrap.dedent(code)), **kwargs)


def valid_response(work, profile, *, invocation=None, result=None):
    invocation = dict(invocation or {
        "schema_version": "0.1", "id": "inv-1", "created_at": TIME,
        "work_order_ref": work.id, "profile_ref": profile.id, "granularity": "task",
        "capability": work.requested_capabilities[0], "provider": "fixture",
        "model": "fixture", "model_family": "fixture-family",
        "input_refs": list(work.input_refs), "output_refs": ["artifact:1"],
        "started_at": TIME, "completed_at": TIME, "usage": {"tokens": 1},
        "side_effects": list(work.declared_side_effects), "runtime_ref": profile.id,
        "actor_ref": "fixture:test", "parent_ref": None, "environment_hash": profile.environment_hash,
    })
    result = dict(result or {
        "schema_version": "0.1", "id": "result-1", "created_at": TIME,
        "work_order_ref": work.id, "invocation_ref": invocation["id"], "status": "completed",
        "outputs": {"ok": True}, "actual_side_effects": list(work.declared_side_effects),
        "usage_refs": ["usage:inv-1"], "artifact_refs": ["artifact:1"],
        "error": None, "metadata": {},
    })
    return {"protocol_version": "0.1", "invocation": invocation, "result": result}


def print_response(response):
    return f"print({json.dumps(response, ensure_ascii=False)!r})"


class ProcessRuntimeTests(unittest.TestCase):
    def test_formatter_success_is_deterministic_and_complete(self):
        work = make_work(metadata={"formatter_records": [{"z": 2, "a": "x"}, {"a": 1}]})
        profile = make_profile(identity={
            "provider": "dalton-fixture", "model": "deterministic-formatter-v1",
            "model_family": "dalton-deterministic", "actor_ref": "fixture:formatter",
        })
        adapter = ProcessRuntimeAdapter.formatter()
        first = adapter.execute(work, profile)
        second = adapter.execute(work, profile)
        self.assertEqual(first[0].to_dict(), second[0].to_dict())
        self.assertEqual(first[1].to_dict(), second[1].to_dict())
        self.assertEqual(first[1].outputs["format"], "canonical-jsonl-v1")
        self.assertEqual(first[1].outputs["formatted"], '{"a":"x","z":2}\n{"a":1}\n')
        self.assertTrue(first[0].usage)
        self.assertTrue(first[1].usage_refs)
        self.assertTrue(first[0].output_refs)

    def test_timeout_terminates_child(self):
        adapter = script_adapter("import time; time.sleep(2)", timeout_seconds=0.1)
        with self.assertRaises(ProcessTimeout):
            adapter.execute(make_work(), make_profile())

    def test_work_and_profile_wall_clock_limits_shorten_adapter_timeout(self):
        slow = script_adapter("import time; time.sleep(2)", timeout_seconds=2)
        started = time.monotonic()
        with self.assertRaises(ProcessTimeout):
            slow.execute(
                make_work(budget={"max_seconds": 0.1}),
                make_profile(limits={"max_seconds": 1}),
            )
        self.assertLess(time.monotonic() - started, 0.8)

        started = time.monotonic()
        with self.assertRaises(ProcessTimeout):
            slow.execute(
                make_work(),
                make_profile(limits={"max_seconds": 0.1}),
            )
        self.assertLess(time.monotonic() - started, 0.8)

    def test_timeout_covers_blocked_stdin_and_wait_after_pipe_close(self):
        large_work = make_work(metadata={"blob": "x" * 256_000})
        blocked = script_adapter(
            "import time; time.sleep(2)",
            timeout_seconds=0.1,
            max_frame_bytes=400_000,
            max_stdout_bytes=400_000,
        )
        with self.assertRaises(ProcessTimeout):
            blocked.execute(large_work, make_profile())

        closed_pipes = script_adapter(
            "import os, time; os.close(1); os.close(2); time.sleep(2)",
            timeout_seconds=0.1,
        )
        with self.assertRaises(ProcessTimeout):
            closed_pipes.execute(make_work(), make_profile())

    def test_timeout_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as outer:
            marker = Path(outer) / "orphan-ran"
            grandchild = (
                "import pathlib, time; time.sleep(0.35); "
                f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(2)"
            )
            with self.assertRaises(ProcessTimeout):
                script_adapter(parent, timeout_seconds=0.1).execute(make_work(), make_profile())
            time.sleep(0.45)
            self.assertFalse(marker.exists())

    def test_stdout_oversize_is_rejected(self):
        adapter = script_adapter("print('x' * 1000)", max_stdout_bytes=128, max_frame_bytes=128)
        with self.assertRaises(OutputLimitExceeded):
            adapter.execute(make_work(), make_profile())

    def test_stderr_oversize_is_rejected(self):
        adapter = script_adapter("import sys; sys.stderr.write('x' * 1000); sys.stderr.flush()", max_stderr_bytes=128)
        with self.assertRaises(OutputLimitExceeded):
            adapter.execute(make_work(), make_profile())

    def test_bad_envelope_is_rejected(self):
        adapter = script_adapter("print('{\\\"wrong\\\":true}')")
        with self.assertRaises(ProcessProtocolError):
            adapter.execute(make_work(), make_profile())

    def test_side_effect_and_usage_are_checked(self):
        work = make_work()
        profile = make_profile()
        response = valid_response(work, profile)
        response["invocation"]["side_effects"] = ["network"]
        response["result"]["actual_side_effects"] = ["network"]
        with self.assertRaises(ProcessContractError):
            script_adapter(print_response(response)).execute(work, profile)
        response = valid_response(work, profile)
        response["result"]["artifact_refs"] = ["artifact:different"]
        with self.assertRaises(ProcessContractError):
            script_adapter(print_response(response)).execute(work, profile)
        response = valid_response(work, profile)
        response["invocation"]["usage"] = {"tokens": 11}
        with self.assertRaises(ProcessContractError):
            script_adapter(print_response(response)).execute(work, profile)

    def test_profile_and_work_order_mismatch_are_checked(self):
        work = make_work()
        profile = make_profile()
        response = valid_response(work, profile)
        response["invocation"]["profile_ref"] = "other-profile"
        with self.assertRaises(ProcessContractError):
            script_adapter(print_response(response)).execute(work, profile)
        response = valid_response(work, profile)
        response["result"]["work_order_ref"] = "other-work"
        with self.assertRaises(ProcessContractError):
            script_adapter(print_response(response)).execute(work, profile)
        with self.assertRaises(ProcessContractError):
            ProcessRuntimeAdapter.formatter().execute(work, make_profile(profile_id="other-profile"))

    def test_invocation_identity_and_environment_are_trusted_launcher_fields(self):
        work = make_work()
        profile = make_profile()
        for field, forged in (
            ("provider", "forged-provider"),
            ("model", "forged-model"),
            ("model_family", "forged-family"),
            ("actor_ref", "human:forged"),
            ("environment_hash", "forged-environment"),
        ):
            response = valid_response(work, profile)
            response["invocation"][field] = forged
            with self.subTest(field=field):
                with self.assertRaises(ProcessContractError):
                    script_adapter(print_response(response)).execute(work, profile)
        mismatched_profile = make_profile(identity={
            "provider": "other", "model": "fixture",
            "model_family": "fixture-family", "actor_ref": "fixture:test",
        })
        response = valid_response(work, mismatched_profile)
        with self.assertRaises(ProcessContractError):
            script_adapter(print_response(response)).execute(work, mismatched_profile)

    def test_environment_is_scrubbed_and_cwd_is_temporary(self):
        # The child writes a valid response whose output reports selected
        # environment names and cwd.  The adapter does not pass parent secrets.
        work = make_work()
        profile = make_profile()
        response = valid_response(work, profile)
        response["result"]["outputs"] = {"env": "", "cwd": ""}
        code = (
            "import json, os; "
            f"r=json.loads({json.dumps(response)!r}); "
            "r['result']['outputs']={'env': json.dumps({k: os.environ.get(k) for k in ('HOME','CODEX_HOME','OPENCLAW_HOME','SECRET_TOKEN')}, sort_keys=True), 'cwd': os.getcwd()}; "
            "print(json.dumps(r))"
        )
        old = os.environ.get("SECRET_TOKEN")
        os.environ["SECRET_TOKEN"] = "must-not-cross"
        try:
            _, result = script_adapter(code).execute(work, profile)
        finally:
            if old is None:
                os.environ.pop("SECRET_TOKEN", None)
            else:
                os.environ["SECRET_TOKEN"] = old
        env = json.loads(result.outputs["env"])
        self.assertEqual(env, {"CODEX_HOME": None, "HOME": None, "OPENCLAW_HOME": None, "SECRET_TOKEN": None})
        self.assertTrue(
            Path(result.outputs["cwd"]).resolve().is_relative_to(
                Path(tempfile.gettempdir()).resolve()
            )
        )

    def test_source_has_no_live_or_authority_path_reference(self):
        source_dir = Path(__file__).parents[1] / "src" / "dalton_core"
        source = "\n".join(path.read_text(encoding="utf-8") for path in (source_dir / "process_runtime.py", source_dir / "formatter_worker.py"))
        self.assertNotIn("workspace-chem", source)
        self.assertNotIn("coverage.db", source)
        self.assertNotIn("/data/coverage", source)


if __name__ == "__main__":
    unittest.main()
