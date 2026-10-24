"""Pre-integration readiness checks (offline). They read code and import modules; they contact nothing.

What they establish (and what they do not):
  * importing every provider-facing and orchestration module is side-effect free: no socket, no urllib call, no
    environment read, and the Nodus SDK is not imported;
  * the facts the readiness report states about each adapter (endpoint scheme, credential variable NAMES, timeouts,
    bounds) are true of the code;
  * no code outside tests mentions Agent37 or OpenRouter, imports Nodus, or restores Nebius;
  * `photo_id` is not turned into a file read anywhere in production code;
  * the contract a real Nodus function handle would have to satisfy is pinned, and the logic a remote function would
    run is a pure, JSON-safe function.
They do NOT show that any real provider works. Fakes and static reads cannot show that.
"""

import ast
import collections.abc
import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from orchestration import integrated, nodus_adapter, synthesis, task_runner
from orchestration.nodus_adapter import NodusTaskRunner
from orchestration.task_runner import PAYLOAD_KEYS, SUMMARY_KEYS, expected_summary, minimal_payload, run_record_summary
from record.validation import validate_record

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PACKAGES = ("orchestration", "agri_pipeline", "demo_app", "second_opinion", "research", "image_analysis",
                       "crop_context", "advice", "synthesis_provider")
IMPORT_CHECKED = (
    "orchestration.run", "orchestration.pipeline_steps", "orchestration.task_runner", "orchestration.nodus_adapter",
    "orchestration.synthesis", "orchestration.integrated",
    "agri_pipeline.pipeline", "agri_pipeline.findings", "agri_pipeline.reply_facts", "research.tavily",
    "research.tavily_research",
    "image_analysis.boundary", "image_analysis.nvidia", "image_analysis.nvidia_headers", "second_opinion.second_opinion",
    "demo_app.service", "demo_app.app", "demo_app.fake_research", "demo_app.server",
    "synthesis_provider.chat",
)


class RecordingEnvironment(collections.abc.MutableMapping):
    """Stands in for os.environ and records every access. It holds nothing."""

    def __init__(self):
        self.accesses = []

    def __getitem__(self, key):
        self.accesses.append(("get", key))
        raise KeyError(key)

    def __setitem__(self, key, value):
        self.accesses.append(("set", key))

    def __delitem__(self, key):
        self.accesses.append(("del", key))

    def __iter__(self):
        self.accesses.append(("iter", None))
        return iter(())

    def __len__(self):
        self.accesses.append(("len", None))
        return 0


OPENROUTER_FILES = (ROOT / "synthesis_provider" / "openrouter.py", ROOT / "demo_app" / "server.py")


def production_files():
    for package in PRODUCTION_PACKAGES:
        yield from sorted((ROOT / package).glob("*.py"))


def make_record():
    rover = MockRover(RoverConfig())
    rover.startup()
    rover.insert_probe()
    rover.take_measurement()
    return rover.build_record(use_capture=False)


class ImportSafetyTests(unittest.TestCase):
    def test_importing_every_provider_facing_module_is_side_effect_free(self):
        environment = RecordingEnvironment()
        tops = {name.split(".")[0] for name in IMPORT_CHECKED}
        with mock.patch.dict(sys.modules), \
                mock.patch("socket.socket", side_effect=AssertionError("socket used")), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")), \
                mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used")), \
                mock.patch("os.environ", environment):
            for name in [n for n in sys.modules if n.split(".")[0] in tops]:
                del sys.modules[name]                      # force a genuine fresh import inside this block
            for name in IMPORT_CHECKED:
                module = importlib.import_module(name)
                self.assertEqual(module.__name__, name)
            if importlib.util.find_spec("nodus") is None:
                self.assertNotIn("nodus", sys.modules)
        self.assertEqual(environment.accesses, [])

    def test_the_new_modules_reference_no_provider_sdk(self):
        for module in (task_runner, nodus_adapter, synthesis, integrated):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], ("nodus", "openai", "requests", "httpx"), name)


class ProviderFactsTests(unittest.TestCase):
    """The statements in the readiness matrix that come from the code."""

    def test_nvidia_adapter_facts(self):
        from image_analysis import nvidia
        self.assertTrue(nvidia.ENDPOINT.startswith("https://integrate.api.nvidia.com/"))
        self.assertEqual(nvidia.API_KEY_ENV_VAR, "NVIDIA_API_KEY")
        self.assertEqual(nvidia.DEFAULT_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(nvidia._MAX_RESPONSE_BYTES, 4 * 1024 * 1024)
        self.assertEqual(nvidia.DEFAULT_MAX_IMAGE_BYTES, 1024 * 1024)

    def test_tavily_search_adapter_facts(self):
        from research import tavily
        self.assertTrue(tavily.ENDPOINT.startswith("https://api.tavily.com/"))
        self.assertEqual(tavily.API_KEY_ENV_VAR, "TAVILY_API_KEY")
        self.assertEqual((tavily.DEFAULT_TIMEOUT_SECONDS, tavily.MAX_RESULTS_LIMIT, tavily.MAX_QUERY_CHARS),
                         (30.0, 10, 200))
        self.assertEqual((tavily.MAX_TITLE_CHARS, tavily.MAX_SNIPPET_CHARS), (300, 1000))

    def test_tavily_research_pro_adapter_facts(self):
        from research import tavily_research
        self.assertTrue(tavily_research.ENDPOINT.startswith("https://api.tavily.com/"))
        self.assertEqual(tavily_research.API_KEY_ENV_VAR, "TAVILY_API_KEY")
        self.assertEqual(tavily_research.APPROVED_DOMAIN, "icar.org.in")
        self.assertEqual((tavily_research.DEADLINE_SECONDS, tavily_research.START_TIMEOUT_SECONDS,
                          tavily_research.POLL_TIMEOUT_SECONDS), (120, 15.0, 10.0))

    def test_every_endpoint_constant_is_https_and_none_is_nebius(self):
        from image_analysis import nvidia
        from research import tavily, tavily_research
        for endpoint in (nvidia.ENDPOINT, tavily.ENDPOINT, tavily_research.ENDPOINT):
            self.assertTrue(endpoint.startswith("https://"))
            self.assertNotIn("nebius", endpoint.lower())
        for package in ("image_analysis", "research"):
            self.assertEqual([p.name for p in (ROOT / package).glob("*nebius*")], [])

    def test_no_code_outside_tests_names_agent37_or_openrouter_or_imports_nodus(self):
        for path in production_files():
            text = path.read_text(encoding="utf-8")
            if path.name == "pipeline_steps.py":
                text = text.replace("SDK, Nodus, Agent37 or OpenRouter.", "")     # a "not provided" docstring line
            lowered = text.lower()
            # OpenRouter's contract was read from its official documentation (SY-2): only the provider module and
            # the server entry point that selects it may name it. Agent37 still has no recorded identity anywhere.
            if path not in OPENROUTER_FILES:
                self.assertNotIn("openrouter", lowered, str(path))
            self.assertNotIn("agent37", lowered, str(path))
            for node in ast.walk(ast.parse(text)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertNotEqual(name.split(".")[0], "nodus", str(path))

    def test_credentials_are_read_by_name_per_call_and_never_at_import(self):
        from image_analysis import nvidia
        from research import tavily, tavily_research
        for module in (nvidia, tavily, tavily_research):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in tree.body:                      # top-level statements only: nothing runs on import
                if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Attribute):
                            self.assertNotEqual(sub.attr, "environ", module.__name__)
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                            self.assertNotIn(sub.func.attr, ("getenv", "open", "urlopen", "connect"), module.__name__)


class ImageIdentityTests(unittest.TestCase):
    def test_photo_id_is_never_turned_into_a_file_read_in_production_code(self):
        allowed = {("demo_app/app.py", "read_bytes"),            # the fixed static whitelist
                   ("image_analysis/nvidia.py", "open"), ("image_analysis/nvidia_headers.py", "open"),
                   ("research/tavily_research.py", "open"),
                   ("research/tavily.py", "open")}  # urllib openers
        found = set()
        for path in production_files():
            relative = path.relative_to(ROOT).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                    if name in ("open", "read_bytes", "read_text", "listdir", "scandir", "walk", "glob", "rglob"):
                        found.add((relative, name))
        self.assertEqual(found - allowed, set())

    def test_the_orchestration_report_path_takes_image_bytes_only_from_its_caller(self):
        from orchestration import pipeline_steps
        source = Path(pipeline_steps.__file__).read_text(encoding="utf-8")
        self.assertIn("png_bytes", source)
        self.assertNotIn('record["photo_id"]', source)
        self.assertNotIn("record['photo_id']", source)


class NodusContractTests(unittest.TestCase):
    """The exact interface a real handle must satisfy, and the pure function a remote task would run."""

    def test_the_handle_contract_is_spawn_then_get_with_a_timeout_and_a_json_payload(self):
        events = []

        class Call:
            def get(self, timeout=None):
                events.append(("get", timeout))
                return expected_summary(payload_seen[0])

        class Handle:
            def spawn(self, *args, **kwargs):
                events.append(("spawn", len(args), sorted(kwargs)))
                payload_seen.append(args[0])
                return Call()

        payload_seen = []
        run = run_record_summary(make_record(), NodusTaskRunner(lambda: Handle(), timeout_seconds=7), run_id="run-1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(events, [("spawn", 1, []), ("get", 7)])        # one positional payload, no keyword arguments
        self.assertEqual(tuple(payload_seen[0]), PAYLOAD_KEYS)
        json.dumps(payload_seen[0], allow_nan=False)                      # the payload is plain JSON

    def test_the_remote_logic_is_a_pure_json_safe_function_of_the_payload(self):
        payload = minimal_payload(validate_record(make_record()).record)
        through_json = json.loads(json.dumps(payload, allow_nan=False))
        self.assertEqual(expected_summary(through_json), expected_summary(payload))
        self.assertEqual(tuple(expected_summary(payload)), SUMMARY_KEYS)
        json.dumps(expected_summary(payload), allow_nan=False)
        source = ast.get_source_segment(Path(task_runner.__file__).read_text(encoding="utf-8"),
                                        next(n for n in ast.parse(Path(task_runner.__file__).read_text(
                                            encoding="utf-8")).body if getattr(n, "name", "") == "expected_summary"))
        self.assertNotIn("import", source)
        self.assertNotIn("open(", source)

    def test_there_is_no_retry_cancel_sleep_or_background_work_and_a_failed_call_is_spawned_once(self):
        tree = ast.parse(Path(nodus_adapter.__file__).read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("sleep", "retry", "Thread", "Timer", "submit", "create_task"):
            self.assertNotIn(banned, names)
        # N-3: a best-effort cancel after a confirmed local wait timeout is now allowed, and nothing else repeats a call.
        self.assertEqual(sum(1 for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "spawn"), 1)
        spawned = []

        class Handle:
            def spawn(self, payload):
                spawned.append(payload)
                raise RuntimeError("down")

        run_record_summary(make_record(), NodusTaskRunner(lambda: Handle()), run_id="run-1")
        self.assertEqual(len(spawned), 1)

    def test_a_timed_out_call_is_reported_but_the_adapter_cannot_stop_the_remote_work(self):
        # What is verified: the adapter reports a timeout and does nothing else. What is NOT verified: whether the
        # service keeps running (and billing) a spawned call after `get` gives up. No cancel call is documented
        # in the material read, so none is made.
        class Call:
            def get(self, timeout=None):
                raise TimeoutError("slow")

        class Handle:
            def spawn(self, payload):
                return Call()

        result = NodusTaskRunner(lambda: Handle()).run(task_runner.TASK_SUMMARIZE_RECORD, {})
        self.assertEqual((result.status, result.error_code), ("error", "task_timeout"))


class RobustnessTests(unittest.TestCase):
    def test_sdk_installed_never_raises_even_when_a_broken_stand_in_module_is_present(self):
        stub = types.ModuleType("nodus")
        stub.__spec__ = None                                    # what a hand-made module looks like
        with mock.patch.dict(sys.modules, {"nodus": stub}):
            self.assertIs(nodus_adapter.sdk_installed(), True)  # something named nodus is importable
        with mock.patch.dict(sys.modules, {"nodus": None}):     # None means "import is blocked"
            self.assertIs(nodus_adapter.sdk_installed(), False)

    def test_python_is_documented_never_raises_on_bad_input(self):
        for bad in ("3.12", 3, [3], (), ("3", "12"), (3, None), object()):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertIs(nodus_adapter.python_is_documented(bad), False)
        self.assertIs(nodus_adapter.python_is_documented((3, 12, 1)), True)
        self.assertIs(nodus_adapter.python_is_documented([3, 11]), True)


class ClaimsTests(unittest.TestCase):
    def test_the_documented_python_range_excludes_the_only_interpreter_this_machine_reports(self):
        self.assertEqual(nodus_adapter.DOCUMENTED_PYTHON_MIN, (3, 10))
        self.assertEqual(nodus_adapter.DOCUMENTED_PYTHON_MAX_EXCLUSIVE, (3, 14))
        if sys.version_info[:2] >= (3, 14):
            self.assertFalse(nodus_adapter.python_is_documented())


if __name__ == "__main__":
    unittest.main()
