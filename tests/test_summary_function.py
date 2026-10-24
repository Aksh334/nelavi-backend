"""Tests for the standalone Nodus summary function that lives OUTSIDE this repository (N-3).

The module is loaded from `%LOCALAPPDATA%\\CropSyncNodus\\summary-function\\cropsync_summary.py` by file path. If the
folder does not exist (another machine) these tests are skipped, not failed. Nothing is deployed, uploaded or sent: the
module is imported and its pure functions are called.
"""

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from orchestration import task_runner
from orchestration.task_runner import checked_summary, expected_summary, minimal_payload
from record import validation
from record.validation import validate_record

FOLDER = Path.home() / "AppData" / "Local" / "CropSyncNodus" / "summary-function"
MODULE_FILE = FOLDER / "cropsync_summary.py"
CANARY = "canary-not-a-real-key-1234567890"


def load():
    spec = importlib.util.spec_from_file_location("cropsync_summary", MODULE_FILE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch("socket.socket", side_effect=AssertionError("socket used")):
        spec.loader.exec_module(module)
    return module


def entry_point(module):
    """The plain function behind `summarize_record`: with the SDK present it is wrapped in a Function object that is
    not callable (`.remote` would contact the service), so the undecorated function is taken from it instead."""
    get_raw = getattr(module.summarize_record, "get_raw_f", None)
    return get_raw() if callable(get_raw) else module.summarize_record


def record(*faults, capture=False):
    rover = MockRover(RoverConfig())
    rover.startup()
    rover.insert_probe()
    for sensor in faults:
        rover.inject_fault(sensor)
    rover.take_measurement()
    return rover.build_record(use_capture=capture)


def payload_of(*faults):
    return minimal_payload(validate_record(record(*faults)).record)


@unittest.skipUnless(MODULE_FILE.is_file(), "the standalone function folder is not on this machine")
class SummaryFunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load()

    def test_it_returns_exactly_what_the_repository_expected_summary_returns(self):
        shapes = [payload_of(), payload_of("moisture"), payload_of("npk", "moisture"),
                  payload_of("air", "battery", "soil_temperature", "npk", "moisture")]
        for payload in shapes:
            with self.subTest(faults=[k for k, v in payload["sensor_status"].items() if v == "fault"]):
                result = self.module.summarize(payload)
                self.assertEqual(result, expected_summary(payload))
                self.assertEqual(tuple(result), task_runner.SUMMARY_KEYS)
                self.assertEqual(entry_point(self.module)(payload), result)
                self.assertIsNotNone(checked_summary(result, payload))     # the repository's checker accepts it

    def test_it_works_on_a_payload_that_went_through_json(self):
        payload = payload_of("moisture")
        through_json = json.loads(json.dumps(payload, allow_nan=False))
        self.assertEqual(self.module.summarize(through_json), expected_summary(payload))

    def test_hand_built_payloads_match_the_repository_function(self):
        base = payload_of()
        cases = [dict(base, soil={k: None for k in base["soil"]}),
                 dict(base, air={k: 0 for k in base["air"]}),
                 dict(base, battery={"voltage_v": 12}),
                 dict(base, sensor_status={k: "fault" for k in base["sensor_status"]}),
                 dict(base, sensor_status={k: "ok" for k in base["sensor_status"]})]
        for payload in cases:
            self.assertEqual(self.module.summarize(payload), expected_summary(payload))

    def test_the_counts_are_whole_numbers_and_the_faulty_list_is_sorted_text(self):
        result = self.module.summarize(payload_of("npk", "moisture"))
        for key in ("readings_present", "readings_missing", "sensors_ok", "sensors_fault"):
            self.assertIs(type(result[key]), int)
        self.assertEqual(result["faulty_sensors"], sorted(result["faulty_sensors"]))
        self.assertTrue(all(type(name) is str for name in result["faulty_sensors"]))

    def test_it_does_not_change_its_input(self):
        payload = payload_of("moisture")
        before = json.dumps(payload, sort_keys=True)
        self.module.summarize(payload)
        self.assertEqual(json.dumps(payload, sort_keys=True), before)

    # ---- the constants are the Record's own, so a change to the Record cannot silently drift
    def test_the_approved_keys_match_the_record_definition_and_the_repository_contract(self):
        m = self.module
        self.assertEqual(m.PAYLOAD_KEYS, task_runner.PAYLOAD_KEYS)
        self.assertEqual(m.SUMMARY_KEYS, task_runner.SUMMARY_KEYS)
        self.assertEqual(m.SECTION_KEYS["soil"], validation.SOIL_KEYS)
        self.assertEqual(m.SECTION_KEYS["air"], validation.AIR_KEYS)
        self.assertEqual(m.SECTION_KEYS["battery"], validation.BATTERY_KEYS)
        self.assertEqual(m.SECTION_KEYS["sensor_status"], validation.STATUS_KEYS)
        self.assertEqual(m.STATUS_VALUES, validation.STATUS_VALUES)
        self.assertEqual((m.APP_NAME, m.FUNCTION_NAME), ("cropsync-summary", "summarize_record"))

    # ---- strict rejection, with a fixed message that repeats nothing
    def invalid_payloads(self):
        base = payload_of()

        def with_(**changes):
            return dict(base, **changes)

        def drop(key):
            return {k: v for k, v in base.items() if k != key}

        sensor = dict(base["sensor_status"])
        soil = dict(base["soil"])
        return [
            None, [], "text", 5, {}, [base], drop("soil"), drop("sensor_status"),
            dict(base, rover_id="nelavi-mock-1"), dict(base, photo_id="p"), dict(base, leaf_label="l"),
            dict(base, confidence=1.0), dict(base, time="t"), dict(base, sequence_number=1),
            dict(base, secret=CANARY), dict(base, versions={}),
            with_(soil=dict(soil, extra_reading=1.0)), with_(soil={k: v for k, v in soil.items() if k != "moisture"}),
            with_(soil=[1, 2]), with_(soil=None), with_(air="wet"), with_(battery={}),
            with_(battery={"voltage_v": True}), with_(soil=dict(soil, moisture=False)),
            with_(soil=dict(soil, moisture="12")), with_(soil=dict(soil, moisture=[1])),
            with_(soil=dict(soil, moisture={"v": 1})), with_(soil=dict(soil, moisture=float("nan"))),
            with_(soil=dict(soil, moisture=float("inf"))), with_(soil=dict(soil, moisture=-float("inf"))),
            with_(soil=dict(soil, moisture=10 ** 400)), with_(soil=dict(soil, moisture=-(10 ** 400))),
            with_(sensor_status=dict(sensor, npk="OK")), with_(sensor_status=dict(sensor, npk=True)),
            with_(sensor_status=dict(sensor, npk=None)), with_(sensor_status=dict(sensor, npk="unknown")),
            with_(sensor_status=dict(sensor, npk=1)), with_(sensor_status=dict(sensor, extra="ok")),
            with_(sensor_status={k: v for k, v in sensor.items() if k != "air"}), with_(sensor_status="ok"),
            with_(sensor_status=dict(sensor, **{CANARY: "ok"})),
        ]

    def test_every_invalid_payload_is_refused_with_a_fixed_message(self):
        for payload in self.invalid_payloads():
            with self.subTest(payload=repr(payload)[:50]):
                with self.assertRaises(ValueError) as raised:
                    self.module.summarize(payload)
                self.assertEqual(str(raised.exception), "invalid payload")
                with self.assertRaises(ValueError):
                    entry_point(self.module)(payload)
                with self.assertRaises(ValueError):
                    self.module.check_payload(payload)

    def test_a_refusal_never_repeats_part_of_the_input(self):
        for payload in (dict(payload_of(), secret=CANARY), {CANARY: 1}, {"soil": CANARY}):
            with self.assertRaises(ValueError) as raised:
                self.module.summarize(payload)
            self.assertNotIn(CANARY, str(raised.exception))
            self.assertNotIn(CANARY, repr(raised.exception))

    def test_the_boundary_values_that_are_allowed_are_accepted(self):
        base = payload_of()
        for value in (0, -1, 1.5, 1e308, 10 ** 15, None):
            payload = dict(base, soil=dict(base["soil"], moisture=value))
            self.assertEqual(self.module.summarize(payload), expected_summary(payload))

    # ---- purity: nothing but math, no repository import, no I/O
    def test_the_module_has_no_io_environment_network_process_or_repository_dependency(self):
        tree = ast.parse(MODULE_FILE.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module)
        self.assertEqual(sorted(imported), ["math", "nodus"])           # nodus: optional, guarded by try/except
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("open", "environ", "getenv", "socket", "urlopen", "subprocess", "system", "eval", "exec",
                       "__import__", "read_text", "read_bytes", "orchestration", "record", "mock_rover", "Path"):
            self.assertNotIn(banned, names)

    def test_the_folder_holds_only_the_module_and_the_readme(self):
        self.assertEqual(sorted(p.name for p in FOLDER.iterdir() if p.name != "__pycache__"),
                         ["README.md", "cropsync_summary.py"])

    def test_the_readme_states_the_function_is_not_deployed_and_names_the_contract(self):
        text = (FOLDER / "README.md").read_text(encoding="utf-8")
        self.assertIn("Not deployed", text)
        for fragment in ("cropsync-summary", "summarize_record", "sensor_status", "faulty_sensors",
                         "outside the CropSync repository", 'ValueError("invalid payload")'):
            self.assertIn(fragment, text)

    @unittest.skipUnless(importlib.util.find_spec("nodus") is not None, "the Nodus SDK is not importable here")
    def test_with_the_sdk_the_function_is_registered_under_the_expected_names_and_nothing_is_deployed(self):
        module = self.module
        self.assertIsNotNone(module.app)
        self.assertEqual(module.app.name, "cropsync-summary")
        self.assertEqual(list(module.app.registered_functions), ["summarize-record"])

    def test_nothing_in_the_repository_depends_on_the_external_folder(self):
        root = Path(task_runner.__file__).resolve().parents[1]
        for package in ("orchestration", "demo_app", "agri_pipeline", "research", "image_analysis"):
            for path in (root / package).glob("*.py"):
                self.assertNotIn("CropSyncNodus", path.read_text(encoding="utf-8"), str(path))

    def test_without_the_sdk_the_function_is_the_plain_function(self):
        if "nodus" in sys.modules and sys.modules["nodus"] is not None:
            self.skipTest("the SDK is importable here, so the function is registered; see the contract tests")
        self.assertIsNone(self.module.nodus)
        self.assertIsNone(self.module.app)
        self.assertTrue(callable(self.module.summarize_record))


if __name__ == "__main__":
    unittest.main()
