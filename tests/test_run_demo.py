"""Tests for the Nodus launcher `scripts/run_demo.py`.

The SDK is never imported for real: a stub `nodus` module stands in for it (or is blocked), so these tests show the
launcher's wiring and its fail-closed checks, not that the real SDK or service works. No socket is opened and
`demo_app.server.main` is replaced, so no server starts and no port is used.
"""

import ast
import io
import sys
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from orchestration import nodus_adapter
from orchestration.task_runner import PAYLOAD_KEYS, expected_summary
from scripts import run_demo

ROOT = Path(__file__).resolve().parents[1]


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


def stub_sdk(handle=None):
    """A stand-in `nodus` module whose Function.from_name records its arguments and returns `handle`."""
    module = types.ModuleType("nodus")
    module.calls = []

    class Function:
        @staticmethod
        def from_name(app, name):
            module.calls.append((app, name))
            return handle

    module.Function = Function
    return module


class Handle:
    def __init__(self):
        self.spawned = []

    def spawn(self, payload):
        self.spawned.append(payload)
        outer = self

        class Call:
            def get(self, timeout=None):
                return expected_summary(outer.spawned[-1])

        return Call()


def payload():
    return {"sensor_status": {"npk": "ok", "moisture": "ok", "soil_temperature": "ok", "air": "ok", "battery": "ok"},
            "soil": {"nitrogen": 1.0, "phosphorus": 2.0, "potassium": 3.0, "moisture": 4.0, "temperature": 5.0},
            "air": {"temperature": 6.0, "humidity": 7.0, "pressure": 8.0}, "battery": {"voltage_v": 12.0}}


class DefaultTests(NoNetwork):
    def test_the_default_passes_every_argument_to_the_demo_and_builds_nothing(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo, \
                mock.patch.dict(sys.modules, {"nodus": None}):          # None blocks any import of the SDK
            code = run_demo.main(["--port", "8800", "--findings-provider", "none"], stdout=out, stderr=err)
        self.assertEqual(code, 0)
        args, kwargs = demo.call_args
        self.assertEqual(args[0], ["--port", "8800", "--findings-provider", "none"])
        self.assertNotIn("compute_runner", kwargs)

    def test_an_explicit_none_is_the_same_and_does_not_reach_the_sdk(self):
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo, \
                mock.patch.dict(sys.modules, {"nodus": None}):
            run_demo.main(["--compute-provider", "none", "--port", "8800"])
        self.assertEqual(demo.call_args.args[0], ["--port", "8800"])
        self.assertNotIn("compute_runner", demo.call_args.kwargs)

    def test_help_and_unknown_options_belong_to_the_demo(self):
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo:
            run_demo.main(["--help"])
        self.assertEqual(demo.call_args.args[0], ["--help"])

    def test_an_invalid_compute_choice_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", io.StringIO()):
                run_demo.main(["--compute-provider", "something-else"])


class PausedTests(NoNetwork):
    """Real Nodus is paused by the owner's no-payment decision: the integration exists but needs a deliberate flag."""

    def test_nodus_without_the_acceptance_flag_starts_nothing_and_never_reaches_the_sdk(self):
        err = io.StringIO()
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo, \
                mock.patch.dict(sys.modules, {"nodus": None}), \
                mock.patch.object(nodus_adapter, "python_is_documented", return_value=True), \
                mock.patch.object(nodus_adapter, "sdk_installed", return_value=True):
            code = run_demo.main(["--compute-provider", "nodus", "--port", "8800"], stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, 2)
        self.assertIn("PAUSED", err.getvalue())
        self.assertIn("no fallback to a fake runner", err.getvalue())
        demo.assert_not_called()

    def test_the_acceptance_flag_alone_does_nothing_and_starts_nothing(self):
        err = io.StringIO()
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo:
            code = run_demo.main([run_demo.ACCEPT_FLAG, "--port", "8800"], stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, 2)
        self.assertIn("has no effect", err.getvalue())
        demo.assert_not_called()

    def test_the_default_launch_is_unaffected_by_the_pause(self):
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo:
            self.assertEqual(run_demo.main(["--port", "8800"]), 0)
        self.assertNotIn("compute_runner", demo.call_args.kwargs)


class FailClosedTests(NoNetwork):
    def run_main(self, documented, installed):
        err = io.StringIO()
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo, \
                mock.patch.object(nodus_adapter, "python_is_documented", return_value=documented), \
                mock.patch.object(nodus_adapter, "sdk_installed", return_value=installed):
            code = run_demo.main(["--compute-provider", "nodus", run_demo.ACCEPT_FLAG], stdout=io.StringIO(),
                                 stderr=err)
        return code, err.getvalue(), demo

    def test_an_undocumented_python_starts_nothing(self):
        code, message, demo = self.run_main(documented=False, installed=True)
        self.assertEqual(code, 2)
        self.assertIn("Nothing was started", message)
        self.assertIn("no fallback", message)
        demo.assert_not_called()

    def test_a_missing_sdk_starts_nothing_and_installs_nothing(self):
        code, message, demo = self.run_main(documented=True, installed=False)
        self.assertEqual(code, 2)
        self.assertIn("installs nothing", message)
        demo.assert_not_called()


class NodusWiringTests(NoNetwork):
    def run_main(self, sdk):
        out = io.StringIO()
        with mock.patch.object(run_demo.server, "main", return_value=0) as demo, \
                mock.patch.object(nodus_adapter, "python_is_documented", return_value=True), \
                mock.patch.dict(sys.modules, {"nodus": sdk}):
            code = run_demo.main(["--compute-provider", "nodus", run_demo.ACCEPT_FLAG, "--port", "8800"], stdout=out,
                                 stderr=io.StringIO())
        return code, demo

    def test_the_runner_is_injected_and_the_sdk_is_not_touched_until_the_stage_runs(self):
        handle = Handle()
        sdk = stub_sdk(handle)
        code, demo = self.run_main(sdk)
        self.assertEqual(code, 0)
        self.assertEqual(demo.call_args.args[0], ["--port", "8800"])
        runner = demo.call_args.kwargs["compute_runner"]
        self.assertIsInstance(runner, nodus_adapter.NodusTaskRunner)
        self.assertIn("REAL Nodus", demo.call_args.kwargs["compute_note"])
        self.assertEqual(sdk.calls, [])                                   # lazy: nothing looked up at start-up
        with mock.patch.dict(sys.modules, {"nodus": sdk}):
            result = runner.run("summarize_record", payload())
        self.assertEqual(result.status, "ok")
        self.assertEqual(sdk.calls, [(run_demo.APP_NAME, run_demo.FUNCTION_NAME)])
        self.assertEqual((run_demo.APP_NAME, run_demo.FUNCTION_NAME), ("cropsync-summary", "summarize_record"))
        self.assertEqual(len(handle.spawned), 1)
        self.assertEqual(tuple(handle.spawned[0]), PAYLOAD_KEYS)

    def test_a_failure_to_reach_the_function_is_a_closed_code_and_not_a_fake(self):
        sdk = stub_sdk(handle=None)                  # from_name returns something that cannot spawn

        class Broken:
            @staticmethod
            def from_name(app, name):
                raise RuntimeError("secret detail that must not surface")

        sdk.Function = Broken
        _, demo = self.run_main(sdk)
        runner = demo.call_args.kwargs["compute_runner"]
        with mock.patch.dict(sys.modules, {"nodus": sdk}):
            result = runner.run("summarize_record", payload())
        self.assertEqual((result.status, result.error_code), ("error", "task_runner_unavailable"))
        self.assertIs(result.fake, False)
        self.assertNotIn("secret", repr(result))


class SourceTests(unittest.TestCase):
    def test_the_sdk_is_imported_only_inside_a_function_and_only_here(self):
        tree = ast.parse((ROOT / "scripts" / "run_demo.py").read_text(encoding="utf-8"))
        top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        for node in top_level:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            for name in names:
                self.assertNotEqual(name.split(".")[0], "nodus")        # never at import time
        inside = [n for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) for n in ast.walk(f)
                  if isinstance(n, ast.Import) and any(a.name == "nodus" for a in n.names)]
        self.assertEqual(len(inside), 1)

    def test_the_launcher_reads_no_environment_and_makes_no_request(self):
        source = (ROOT / "scripts" / "run_demo.py").read_text(encoding="utf-8").lower()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "'")))
        for banned in ("environ", "getenv", "urlopen", "socket", "subprocess", "open("):
            self.assertNotIn(banned, code, banned)


if __name__ == "__main__":
    unittest.main()
