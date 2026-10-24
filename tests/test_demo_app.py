import ast
import base64
import contextlib
import io
import json
import os
import re
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import demo_app
from demo_app import app as app_module
from demo_app import demo_analyzer
from demo_app import server as server_module
from demo_app.app import MAX_JSON_BYTES, DemoApp
from demo_app.demo_analyzer import DEMO_MODEL_LABEL, MAX_IMAGE_BYTES, DemoImageAnalyzer
from demo_app.service import (
    READING_UNITS,
    SECOND_OPINION_LABEL,
    SECOND_OPINION_REAL_LABEL,
    UNIT_NOT_DEFINED,
    DemoError,
    DemoService,
)
from image_analysis.boundary import ERROR_CODES, AnalysisRequest, AnalysisResult, ImageInput
from image_analysis.nvidia import (
    NvidiaConfig,
    NvidiaImageAnalyzer,
    ResponseTooLarge,
    TransportError,
    TransportTimeout,
)
from record.validation import AIR_KEYS, SOIL_KEYS, validate_record
from second_opinion.second_opinion import SecondOpinionService
from second_opinion.simulated_provider import SimulatedSecondOpinionProvider
from storage.record_store import RecordStore, StorageError

HOST = "127.0.0.1:8765"
PROMPT = "any prompt"


def png_bytes(width=4, height=2):
    """A small valid PNG built in memory."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\x20\x80\x30" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def second_opinion_payload(**overrides):
    payload = {
        "image_png_base64": base64.b64encode(png_bytes()).decode("ascii"),
        "readings": {"soil": {key: 1.5 for key in SOIL_KEYS},
                     "air": {key: 2.5 for key in AIR_KEYS}},
        "crop": "Tomato",
        "language": "English",
    }
    payload.update(overrides)
    return payload


class FakeRealProvider:
    """A stand-in that makes no network call and reads no credential. It has no SIMULATED
    marker, so a service built around it must be declared simulated=False."""

    def analyze(self, analysis_request):
        return AnalysisResult("ok", "Keep the leaves dry.", None, "fake-model", ())


def request(data, media_type="image/png", prompt=PROMPT):
    return AnalysisRequest(ImageInput(data, media_type), prompt)


class NoSockets(unittest.TestCase):
    """Base class: any attempt to open a socket during a test fails it."""

    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


def make_service(testcase):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    return DemoService(store), store


def make_app(service):
    return DemoApp(service, {HOST, "localhost:8765"})


def call(app, method, target, body=b"", headers=None, host=HOST, csrf=True, length=True):
    sent = {}
    if host is not None:
        sent["host"] = host
    if method == "POST":
        if csrf:
            sent["x-nelavi-demo"] = "1"
        if length:
            sent["content-length"] = str(len(body))
    sent.update(headers or {})
    return app.handle_request(method, target, sent, io.BytesIO(body).read)


def as_json(response):
    return json.loads(response.body.decode("utf-8"))


def post_json(app, target, payload):
    return call(app, "POST", target, json.dumps(payload).encode("utf-8"),
                {"content-type": "application/json"})


class TestDemoAnalyzer(unittest.TestCase):
    def test_returns_a_clearly_labelled_simulated_result(self):
        result = DemoImageAnalyzer().analyze(request(png_bytes()))
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual((result.status, result.error_code), ("ok", None))
        self.assertEqual(result.model, DEMO_MODEL_LABEL)
        self.assertTrue(result.text.startswith("DEMO ONLY - SIMULATED OUTPUT."))
        self.assertIn("No AI model was run", result.text)
        codes = {c.code for c in result.caveats}
        self.assertTrue({"simulated_demo_output", "not_an_assessment",
                         "image_source_unresolved"} <= codes)
        self.assertNotIn("descriptive_text_only", codes)  # that caveat describes model output

    def test_is_deterministic(self):
        analyzer = DemoImageAnalyzer()
        self.assertEqual(analyzer.analyze(request(png_bytes())),
                         analyzer.analyze(request(png_bytes())))
        self.assertNotEqual(analyzer.analyze(request(png_bytes(4, 2))).text,
                            analyzer.analyze(request(png_bytes(5, 3))).text)

    def test_text_reports_only_facts_about_the_file(self):
        data = png_bytes(4, 2)
        text = DemoImageAnalyzer().analyze(request(data)).text
        self.assertIn("%d bytes" % len(data), text)
        self.assertIn("4 x 2 pixels", text)
        lowered = text.lower()
        for forbidden in ("nvidia", "nemotron", "nebius", "tavily", "confidence",
                          "disease", "healthy", "diagnos", "recommend"):
            self.assertNotIn(forbidden, lowered, forbidden)

    def test_unreadable_header_is_reported_not_guessed(self):
        data = b"\x89PNG\r\n\x1a\n" + b"xxxxx"
        text = DemoImageAnalyzer().analyze(request(data)).text
        self.assertIn("pixel size not readable", text)

    def test_invalid_images_use_the_existing_error_codes(self):
        analyzer = DemoImageAnalyzer(max_image_bytes=len(png_bytes()))
        bad = (request(b"not a png"), request(b""), request(png_bytes(), media_type="image/jpeg"),
               request(png_bytes() + b"\x00" * 10), request("text"), AnalysisRequest(None, PROMPT))
        for item in bad:
            result = analyzer.analyze(item)
            self.assertEqual((result.status, result.error_code, result.text),
                             ("error", "image_unsupported", None))
            self.assertEqual(result.model, DEMO_MODEL_LABEL)
            self.assertIn("simulated_demo_output", {c.code for c in result.caveats})

    def test_size_limit_is_inclusive(self):
        data = png_bytes()
        self.assertEqual(DemoImageAnalyzer(len(data)).analyze(request(data)).status, "ok")
        self.assertEqual(DemoImageAnalyzer(len(data) - 1).analyze(request(data)).error_code,
                         "image_unsupported")

    def test_unusable_prompt_and_programmer_errors(self):
        for prompt in ("", "  ", None, 5):
            self.assertEqual(DemoImageAnalyzer().analyze(request(png_bytes(), prompt=prompt)).error_code,
                             "configuration_invalid")
        with self.assertRaises(TypeError):
            DemoImageAnalyzer().analyze("not a request")
        for bad in (0, -1, True, "1", 1.5):
            with self.assertRaises(ValueError):
                DemoImageAnalyzer(bad)

    def test_default_limit_is_one_mebibyte(self):
        self.assertEqual(MAX_IMAGE_BYTES, 1048576)


class TestDemoService(NoSockets):
    def test_state_lists_the_three_existing_scenarios(self):
        service, _ = make_service(self)
        state = service.state()
        self.assertEqual([s["name"] for s in state["scenarios"]],
                         ["normal_operation", "probe_measurement", "sensor_fault"])
        self.assertEqual(state["max_image_bytes"], 1048576)

    def test_every_scenario_gives_a_valid_stored_record(self):
        service, store = make_service(self)
        for name in ("normal_operation", "probe_measurement", "sensor_fault"):
            outcome = service.run_scenario(name)
            self.assertTrue(outcome["validation"]["valid"], name)
            self.assertEqual(outcome["validation"]["errors"], [])
            self.assertIsInstance(outcome["stored_id"], int)
            self.assertTrue(validate_record(outcome["record"]).valid)
            self.assertEqual(store.get(outcome["stored_id"]).record, outcome["record"])
            self.assertEqual(len(outcome["readings"]), 9)
            self.assertIn("build_record", outcome["steps"])

    def test_units_are_only_shown_where_the_project_defines_one(self):
        service, _ = make_service(self)
        rows = {r["name"]: r for r in service.run_scenario("probe_measurement")["readings"]}
        self.assertEqual(rows["battery.voltage_v"]["unit"], "V")
        self.assertEqual(READING_UNITS, {("battery", "voltage_v"): "V"})
        for name, row in rows.items():
            if name != "battery.voltage_v":
                self.assertEqual(row["unit"], UNIT_NOT_DEFINED, name)

    def test_photo_reference_is_a_placeholder_and_only_when_a_photo_was_captured(self):
        service, _ = make_service(self)
        with_photo = service.run_scenario("normal_operation")
        self.assertIsInstance(with_photo["photo"]["photo_id"], str)
        self.assertIn("No image exists", with_photo["photo"]["note"])
        without = service.run_scenario("probe_measurement")
        self.assertIsNone(without["photo"])
        self.assertEqual([without["record"][k] for k in ("leaf_label", "confidence", "photo_id")],
                         [None, None, None])

    def test_sensor_fault_scenario_flags_the_fault_and_stays_valid(self):
        service, _ = make_service(self)
        outcome = service.run_scenario("sensor_fault")
        rows = {r["name"]: r for r in outcome["readings"]}
        self.assertEqual(rows["soil.moisture"]["status"], "fault")
        self.assertIsNone(rows["soil.moisture"]["value"])
        self.assertEqual(outcome["record"]["sensor_status"]["moisture"], "fault")
        self.assertTrue(outcome["validation"]["valid"])

    def test_scenarios_are_independent_of_each_other(self):
        service, _ = make_service(self)
        service.run_scenario("sensor_fault")
        service.run_scenario("normal_operation")
        later = service.run_scenario("probe_measurement")
        self.assertEqual(set(later["record"]["sensor_status"].values()), {"ok"})
        self.assertIsNone(later["photo"])  # the earlier capture did not leak into this Record

    def test_history_is_newest_first_and_sequences_advance(self):
        service, _ = make_service(self)
        for _ in range(3):
            service.run_scenario("probe_measurement")
        history = service.history(2)
        self.assertEqual(history["total"], 3)
        self.assertEqual([r["id"] for r in history["records"]], [3, 2])
        self.assertEqual([r["record"]["sequence_number"] for r in history["records"]], [2, 1])

    def test_same_requests_give_identical_records(self):
        first, _ = make_service(self)
        second, _ = make_service(self)
        names = ("normal_operation", "probe_measurement", "sensor_fault")
        self.assertEqual([first.run_scenario(n)["record"] for n in names],
                         [second.run_scenario(n)["record"] for n in names])

    def test_unknown_scenario_is_a_clear_error(self):
        service, _ = make_service(self)
        for bad in ("nope", "", "obstacle"):  # "obstacle" exists upstream but is not offered
            with self.assertRaises(DemoError) as caught:
                service.run_scenario(bad)
            self.assertEqual(caught.exception.status, 400)

    def test_an_invalid_record_is_reported_and_not_stored(self):
        service, store = make_service(self)
        record = service.run_scenario("probe_measurement")["record"]
        broken = {k: v for k, v in record.items() if k != "rover_id"}
        outcome = service.process_record(broken)
        self.assertFalse(outcome["valid"])
        self.assertIsNone(outcome["stored_id"])
        self.assertEqual(outcome["errors"][0]["path"], "rover_id")
        self.assertEqual(len(store.list_records()), 1)

    def test_a_storage_failure_is_reported(self):
        service, store = make_service(self)
        with mock.patch.object(store, "add", side_effect=StorageError("disk")):
            with self.assertRaises(DemoError) as caught:
                service.run_scenario("probe_measurement")
        self.assertEqual(caught.exception.status, 500)

    def test_history_limit_is_validated(self):
        service, _ = make_service(self)
        for bad in (0, 101, True, "5", -1, 1.5):
            with self.assertRaises(DemoError):
                service.history(bad)

    def test_history_survives_closing_and_reopening_a_file_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.sqlite3")
            store = RecordStore(path)
            DemoService(store).run_scenario("probe_measurement")
            store.close()
            reopened = RecordStore(path)
            try:
                self.assertEqual(DemoService(reopened).history()["total"], 1)
            finally:
                reopened.close()

    def test_analysis_of_a_png_is_demo_labelled_and_not_attached_to_a_record(self):
        service, store = make_service(self)
        result = service.analyze_png(png_bytes())
        self.assertEqual(result["status"], "ok")
        self.assertIn("DEMO ONLY", result["label"])
        self.assertEqual(store.list_records(), [])
        bad = service.analyze_png(b"not a png")
        self.assertEqual((bad["status"], bad["error_code"], bad["text"]),
                         ("error", "image_unsupported", None))

    def test_state_has_an_additive_second_opinion_block_from_existing_definitions(self):
        service, _ = make_service(self)
        state = service.state()
        self.assertEqual(state["max_image_bytes"], 1048576)  # existing keys are unchanged
        block = state["second_opinion"]
        self.assertEqual(block, {
            "simulated": True,
            "max_image_bytes": 1048576,
            "crop_max_chars": 64,
            "language_max_chars": 32,
            "reading_groups": [{"name": "soil", "keys": list(SOIL_KEYS)},
                               {"name": "air", "keys": list(AIR_KEYS)}],
        })

    def test_second_opinion_defaults_to_the_simulated_provider(self):
        service, store = make_service(self)
        result = service.get_second_opinion(second_opinion_payload())
        self.assertIsNone(result["error_code"])
        self.assertTrue(result["simulated"])
        self.assertIn("SIMULATED", result["opinion"])
        self.assertIn("SIMULATED", result["label"])
        self.assertEqual(store.list_records(), [])  # nothing is stored

    def test_second_opinion_uses_an_injected_provider(self):
        calls = []

        class Recorder:
            def analyze(self, analysis_request):
                calls.append(analysis_request)
                return AnalysisResult("ok", "Water early.", None, "m", ())

        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        service = DemoService(store, second_opinion_provider=Recorder(),
                              second_opinion_simulated=False)
        result = service.get_second_opinion(second_opinion_payload())
        self.assertEqual((len(calls), result["opinion"], result["model"]), (1, "Water early.", "m"))
        self.assertEqual(calls[0].image.media_type, "image/png")

    def test_the_mode_is_explicit_and_simulated_by_default(self):
        service, _ = make_service(self)
        self.assertIs(service.state()["second_opinion"]["simulated"], True)
        result = service.get_second_opinion(second_opinion_payload())
        self.assertIs(result["simulated"], True)
        self.assertEqual(result["label"], SECOND_OPINION_LABEL)
        self.assertIn("no AI model was run", result["label"])
        self.assertIn("SIMULATED RESPONSE: no AI model was run", result["opinion"])
        self.assertIn("agriculture officer", result["opinion"])

    def test_an_injected_provider_must_state_its_mode(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        for mode in (None, 1, 0, "True"):
            with self.assertRaises(TypeError):
                DemoService(store, second_opinion_provider=FakeRealProvider(),
                            second_opinion_simulated=mode)
        with self.assertRaises(ValueError):  # the default provider can only be simulated
            DemoService(store, second_opinion_simulated=False)
        DemoService(store, second_opinion_simulated=True)  # restating the default is fine

    def test_the_mode_is_declared_once_and_a_disagreeing_provider_fails_at_construction(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):  # a simulated provider declared real
            DemoService(store, second_opinion_provider=SimulatedSecondOpinionProvider(),
                        second_opinion_simulated=False)
        with self.assertRaises(ValueError):  # a real provider declared simulated
            DemoService(store, second_opinion_provider=FakeRealProvider(),
                        second_opinion_simulated=True)
        # the matching declarations are accepted
        DemoService(store, second_opinion_provider=SimulatedSecondOpinionProvider(),
                    second_opinion_simulated=True)
        DemoService(store, second_opinion_provider=FakeRealProvider(), second_opinion_simulated=False)

    def test_a_non_simulated_mode_is_reported_and_labelled_as_real(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        service = DemoService(store, second_opinion_provider=FakeRealProvider(),
                              second_opinion_simulated=False)
        self.assertIs(service.state()["second_opinion"]["simulated"], False)
        result = service.get_second_opinion(second_opinion_payload())
        self.assertIs(result["simulated"], False)
        self.assertEqual((result["opinion"], result["model"], result["error_code"]),
                         ("Keep the leaves dry.", "fake-model", None))
        self.assertEqual(result["label"], SECOND_OPINION_REAL_LABEL)
        self.assertNotIn("simulated", result["label"].lower())
        self.assertEqual(store.list_records(), [])

    def test_a_result_that_contradicts_the_configured_mode_is_never_returned(self):
        # Construction already rejects a disagreeing provider, so this is the second line of
        # defence: swap in a service of the other mode after construction.
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        simulated_core = SecondOpinionService(SimulatedSecondOpinionProvider(), simulated=True)
        real_core = SecondOpinionService(FakeRealProvider(), simulated=False)
        for configured, other in ((False, simulated_core), (True, real_core)):
            wrong = DemoService(store, second_opinion_provider=(
                FakeRealProvider() if not configured else SimulatedSecondOpinionProvider()),
                second_opinion_simulated=configured)
            with mock.patch.object(wrong, "_second_opinion", other):
                with self.assertRaises(RuntimeError):
                    wrong.get_second_opinion(second_opinion_payload())
                # through HTTP it is the generic 500 and no opinion text
                response = post_json(make_app(wrong), "/api/second-opinion",
                                     second_opinion_payload())
            self.assertEqual(response.status, 500)
            self.assertNotIn(b"Keep the leaves dry", response.body)
            self.assertNotIn(b"SIMULATED RESPONSE", response.body)
            self.assertNotIn(b"does not match", response.body)

    def test_an_invalid_second_opinion_request_is_a_400_that_does_not_echo_input(self):
        service, _ = make_service(self)
        for payload in (None, {}, second_opinion_payload(crop="bad\nvalue-canary"),
                        second_opinion_payload(image_png_base64="!!!!")):
            with self.assertRaises(DemoError) as caught:
                service.get_second_opinion(payload)
            self.assertEqual(caught.exception.status, 400)
            self.assertNotIn("canary", caught.exception.message)
            self.assertNotIn("!!!!", caught.exception.message)

    def test_no_environment_value_reaches_any_output(self):
        service, _ = make_service(self)
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "canary-not-a-real-key"}):
            outputs = [json.dumps(service.run_scenario("normal_operation")),
                       json.dumps(service.get_second_opinion(second_opinion_payload())),
                       json.dumps(service.analyze_png(png_bytes())),
                       json.dumps(service.history()), json.dumps(service.state())]
        for text in outputs:
            self.assertNotIn("canary-not-a-real-key", text)


class TestDemoApp(NoSockets):
    def setUp(self):
        super().setUp()
        self.service, self.store = make_service(self)
        self.app = make_app(self.service)

    def test_pages_are_served_from_the_fixed_whitelist(self):
        page = call(self.app, "GET", "/")
        self.assertEqual(page.status, 200)
        self.assertTrue(page.content_type.startswith("text/html"))
        # the shipped page is neutral: it claims no mode until the server has confirmed one
        self.assertIn(b"Checking application mode", page.body)
        self.assertNotIn(b"SIMULATED DEMONSTRATION", page.body)
        self.assertTrue(call(self.app, "GET", "/static/app.js").content_type.startswith("text/javascript"))
        self.assertTrue(call(self.app, "GET", "/static/style.css").content_type.startswith("text/css"))
        for path in ("/static/../app.py", "/static/", "/app.py", "/etc/passwd", "/%2e%2e/x"):
            self.assertEqual(call(self.app, "GET", path).status, 404, path)

    def test_state_and_history_endpoints(self):
        self.assertEqual(as_json(call(self.app, "GET", "/api/state"))["max_image_bytes"], 1048576)
        self.assertEqual(as_json(call(self.app, "GET", "/api/history"))["total"], 0)
        post_json(self.app, "/api/run", {"scenario": "probe_measurement"})
        self.assertEqual(as_json(call(self.app, "GET", "/api/history?limit=1"))["total"], 1)
        for bad in ("abc", "0", "101", "-1", "1.5"):
            self.assertEqual(call(self.app, "GET", "/api/history?limit=" + bad).status, 400, bad)

    def test_host_header_must_be_the_local_address(self):
        self.assertEqual(call(self.app, "GET", "/", host="localhost:8765").status, 200)
        for host in ("evil.example:8765", "127.0.0.1:9999", "127.0.0.1", "", None):
            self.assertEqual(call(self.app, "GET", "/", host=host).status, 403, host)

    def test_posts_need_the_custom_header(self):
        response = call(self.app, "POST", "/api/run", b'{"scenario": "probe_measurement"}', csrf=False)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.store.list_records(), [])

    def test_run_endpoint(self):
        response = post_json(self.app, "/api/run", {"scenario": "normal_operation"})
        data = as_json(response)
        self.assertEqual(response.status, 200)
        self.assertTrue(data["validation"]["valid"])
        self.assertEqual(data["stored_id"], 1)
        self.assertEqual(post_json(self.app, "/api/run", {"scenario": "x"}).status, 400)
        self.assertEqual(post_json(self.app, "/api/run", {"scenario": 5}).status, 400)
        self.assertEqual(post_json(self.app, "/api/run", ["scenario"]).status, 400)
        self.assertEqual(call(self.app, "POST", "/api/run", b"{not json").status, 400)
        self.assertEqual(call(self.app, "POST", "/api/run", b"\xff\xfe").status, 400)

    def test_request_bodies_are_bounded_and_checked_before_reading(self):
        def explode(n):
            raise AssertionError("body was read")
        headers = {"host": HOST, "x-nelavi-demo": "1"}
        too_big = dict(headers, **{"content-length": str(MAX_JSON_BYTES + 1)})
        self.assertEqual(self.app.handle_request("POST", "/api/run", too_big, explode).status, 413)
        png = dict(headers, **{"content-length": str(MAX_IMAGE_BYTES + 1), "content-type": "image/png"})
        self.assertEqual(self.app.handle_request("POST", "/api/analyze", png, explode).status, 413)
        for extra, status in (({}, 411), ({"content-length": "abc"}, 400), ({"content-length": "-1"}, 400),
                              ({"content-length": "1.5"}, 400), ({"content-length": "²"}, 400),
                              ({"content-length": "5", "transfer-encoding": "chunked"}, 411)):
            self.assertEqual(self.app.handle_request("POST", "/api/run", dict(headers, **extra),
                                                     io.BytesIO(b"12345").read).status, status, extra)
        short = dict(headers, **{"content-length": "10"})
        self.assertEqual(self.app.handle_request("POST", "/api/run", short, io.BytesIO(b"12345").read).status, 400)

    def test_absurdly_long_numbers_are_client_errors_not_internal_errors(self):
        """Python refuses to turn more than 4,300 digits into an int by default. Such a number is bad client
        input and must not surface as an internal error (HTTP 500). The three lengths straddle that limit."""
        for digits in (4300, 4301, 5000):
            number = "9" * digits
            # a `limit` query value: not a usable limit, so 400 like every other bad limit
            response = call(self.app, "GET", "/api/history?limit=" + number)
            self.assertEqual(response.status, 400, digits)
            self.assertIn("limit", as_json(response)["error"])
            # a Content-Length: a whole number far above every body limit, so 413 like any too-large body
            for route, content_type in (("/api/run", "application/json"), ("/api/second-opinion", "application/json"),
                                        ("/api/analyze", "image/png")):
                reader = mock.Mock(side_effect=AssertionError("body was read"))
                headers = {"host": HOST, "x-nelavi-demo": "1", "content-type": content_type,
                           "content-length": number}
                response = self.app.handle_request("POST", route, headers, reader)
                self.assertEqual(response.status, 413, (route, digits))
                self.assertEqual(list(as_json(response)), ["error"])
                reader.assert_not_called()
        self.assertEqual(self.store.list_records(), [])

    def test_normal_numbers_keep_their_behaviour_next_to_the_oversize_handling(self):
        post_json(self.app, "/api/run", {"scenario": "probe_measurement"})
        self.assertEqual(call(self.app, "GET", "/api/history?limit=1").status, 200)
        self.assertEqual(call(self.app, "GET", "/api/history?limit=100").status, 200)
        self.assertEqual(call(self.app, "GET", "/api/history?limit=101").status, 400)
        self.assertEqual(call(self.app, "GET", "/api/history?limit=0").status, 400)
        body = b'{"scenario": "probe_measurement"}'
        self.assertEqual(call(self.app, "POST", "/api/run", body, {"content-type": "application/json"}).status, 200)
        self.assertEqual(len(self.store.list_records()), 2)

    def test_deeply_nested_json_on_the_run_route_is_a_400_at_the_current_size_limit(self):
        """The longest body /api/run accepts is MAX_JSON_BYTES. Nesting that fills it must be a client
        error (400), never an internal error. (Whether a deeper parse could raise RecursionError depends on the
        Python runtime; at this size limit it is a documented hardening consideration.)"""
        half = MAX_JSON_BYTES // 2
        for body in (b"[" * half + b"]" * half, b"[" * MAX_JSON_BYTES, b'{"a":' * (MAX_JSON_BYTES // 5)):
            self.assertLessEqual(len(body), MAX_JSON_BYTES)
            response = call(self.app, "POST", "/api/run", body)
            self.assertEqual(response.status, 400, body[:12])
            self.assertEqual(list(as_json(response)), ["error"])
        self.assertEqual(self.store.list_records(), [])

    def test_a_recursion_error_while_parsing_any_json_body_is_a_400(self):
        """The test above shows the parser does not recurse too deeply at the size limit on this runtime. Another
        runtime might, so every JSON route must turn a RecursionError into the same 400 (audit F-02). The error
        is simulated by replacing the parser; no deep structure is built."""
        body = b'{"scenario": "probe_measurement"}'
        routes = (("/api/run", {}), ("/api/advisory", {"content-type": "application/json"}),
                  ("/api/second-opinion", {"content-type": "application/json"}),
                  ("/api/research-pro/start", {"content-type": "application/json"}))
        for route, headers in routes:
            with self.subTest(route=route):
                with mock.patch.object(app_module.json, "loads", side_effect=RecursionError):
                    response = call(self.app, "POST", route, body, headers)
                self.assertEqual(response.status, 400)
                self.assertEqual(list(as_json(response)), ["error"])
                self.assertIn("nested too deeply", as_json(response)["error"])
        self.assertEqual(self.store.list_records(), [])

    def test_analyze_endpoint(self):
        png = {"content-type": "image/png"}
        ok = call(self.app, "POST", "/api/analyze", png_bytes(), png)
        data = as_json(ok)
        self.assertEqual((ok.status, data["status"]), (200, "ok"))
        self.assertIn("DEMO ONLY", data["label"])
        bad = call(self.app, "POST", "/api/analyze", b"definitely not a png", png)
        self.assertEqual((bad.status, as_json(bad)["error_code"]), (422, "image_unsupported"))
        self.assertEqual(call(self.app, "POST", "/api/analyze", png_bytes(),
                              {"content-type": "image/jpeg"}).status, 415)
        self.assertEqual(call(self.app, "POST", "/api/analyze", png_bytes()).status, 415)
        self.assertEqual(self.store.list_records(), [])  # analysis never creates a Record

    # ----------------------------------------------- /api/second-opinion
    def so_call(self, body, content_type="application/json", app=None, **kwargs):
        headers = {} if content_type is None else {"content-type": content_type}
        return call(app or self.app, "POST", "/api/second-opinion", body, headers, **kwargs)

    def so_body(self, **overrides):
        return json.dumps(second_opinion_payload(**overrides)).encode("utf-8")

    def test_second_opinion_returns_the_simulated_opinion_and_stores_nothing(self):
        for content_type in ("application/json", "application/json; charset=utf-8",
                             "Application/JSON"):
            response = self.so_call(self.so_body(), content_type)
            data = as_json(response)
            self.assertEqual(response.status, 200, content_type)
            self.assertIsNone(data["error_code"])
            self.assertTrue(data["simulated"])
            self.assertIn("SIMULATED", data["opinion"])
            self.assertIn("SIMULATED", data["label"])
        self.assertEqual(self.store.list_records(), [])

    def test_second_opinion_keeps_the_host_and_custom_header_checks(self):
        body = self.so_body()
        for host in ("evil.example:8765", "127.0.0.1", None):
            self.assertEqual(self.so_call(body, host=host).status, 403, host)
        self.assertEqual(self.so_call(body, csrf=False).status, 403)
        self.assertEqual(self.so_call(body, host="localhost:8765").status, 200)
        self.assertEqual(self.store.list_records(), [])

    def test_second_opinion_requires_a_json_content_type_before_reading(self):
        def explode(n):
            raise AssertionError("body was read")
        base = {"host": HOST, "x-nelavi-demo": "1", "content-length": "10"}
        for extra in ({}, {"content-type": "text/plain"}, {"content-type": "image/png"},
                      {"content-type": "application/jsonx"}, {"content-type": ""},
                      {"content-type": "application/x-www-form-urlencoded"}):
            response = self.app.handle_request("POST", "/api/second-opinion",
                                               dict(base, **extra), explode)
            self.assertEqual(response.status, 415, extra)

    def test_second_opinion_body_framing_errors(self):
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-type": "application/json"}
        for extra, status in (({}, 411), ({"content-length": "abc"}, 400),
                              ({"content-length": "-1"}, 400), ({"content-length": "1.5"}, 400),
                              ({"content-length": "²"}, 400),
                              ({"content-length": "5", "transfer-encoding": "chunked"}, 411),
                              ({"content-length": "10"}, 400),   # shorter than declared
                              ({"content-length": "0"}, 400)):   # empty body is not JSON
            response = self.app.handle_request("POST", "/api/second-opinion",
                                               dict(headers, **extra), io.BytesIO(b"12345").read)
            self.assertEqual(response.status, status, extra)

    def test_second_opinion_limit_is_exactly_1402200_bytes(self):
        self.assertEqual(app_module.MAX_SECOND_OPINION_BYTES, 1_402_200)
        # the longest base64 text for a 1 MiB image, plus 4,096 bytes for everything else
        self.assertEqual(app_module.MAX_SECOND_OPINION_BYTES,
                         4 * ((MAX_IMAGE_BYTES + 2) // 3) + 4096)
        self.assertEqual(MAX_JSON_BYTES, 4096)  # the other JSON endpoint is unchanged

    def test_a_valid_request_of_exactly_the_maximum_size_is_accepted(self):
        limit = app_module.MAX_SECOND_OPINION_BYTES
        body = self.so_body()
        body += b" " * (limit - len(body))  # trailing JSON whitespace keeps it a valid request
        self.assertEqual(len(body), limit)
        self.assertEqual(json.loads(body)["crop"], "Tomato")
        response = self.so_call(body)
        self.assertEqual(response.status, 200)
        self.assertIn("SIMULATED", as_json(response)["opinion"])
        self.assertEqual(self.store.list_records(), [])

    def test_one_byte_over_the_maximum_is_rejected_before_the_reader_is_called(self):
        reader = mock.Mock(side_effect=AssertionError("body was read"))
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-type": "application/json",
                   "content-length": str(app_module.MAX_SECOND_OPINION_BYTES + 1)}
        response = self.app.handle_request("POST", "/api/second-opinion", headers, reader)
        self.assertEqual(response.status, 413)
        reader.assert_not_called()

    def test_run_endpoint_keeps_its_4096_byte_limit(self):
        reader = mock.Mock(side_effect=AssertionError("body was read"))
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": "4097"}
        self.assertEqual(self.app.handle_request("POST", "/api/run", headers, reader).status, 413)
        reader.assert_not_called()

    def test_second_opinion_malformed_bodies_are_safe_400s(self):
        deep = b"[" * 200000 + b"]" * 200000
        for body in (b"{not json", b"\xff\xfe", b"", b"null", b"[]", b'"text"', b"5", b"true",
                     deep, b'{"image_png_base64": 1e999999999999}', b"NaN"):
            response = self.so_call(body)
            self.assertEqual(response.status, 400, body[:20])
            self.assertEqual(list(as_json(response)), ["error"])
        self.assertEqual(self.store.list_records(), [])

    def test_second_opinion_invalid_requests_do_not_echo_submitted_values(self):
        hostile = "<script>canary-hostile</script>"
        payloads = (
            second_opinion_payload(**{hostile: 1}),
            second_opinion_payload(readings={hostile: {}}),
            second_opinion_payload(readings={"soil": {hostile: 1}, "air": {}}),
            second_opinion_payload(crop=hostile),
            second_opinion_payload(language="canary-hostile\n"),
            second_opinion_payload(image_png_base64="canary-hostile!"),
            second_opinion_payload(image_png_base64=base64.b64encode(b"canary-hostile").decode()),
            {"canary-hostile": 1},
        )
        for payload in payloads:
            response = post_json(self.app, "/api/second-opinion", payload)
            self.assertEqual(response.status, 400, payload.keys())
            self.assertNotIn(b"canary", response.body)
            self.assertNotIn(b"script", response.body)

    def test_second_opinion_provider_errors_are_422(self):
        class FailingProvider:
            def analyze(self, analysis_request):
                return AnalysisResult("error", None, "provider_unavailable", "stub-model", ())

        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        service = DemoService(store, second_opinion_provider=FailingProvider(),
                              second_opinion_simulated=False)
        response = self.so_call(self.so_body(), app=make_app(service))
        data = as_json(response)
        self.assertEqual((response.status, data["error_code"], data["opinion"]),
                         (422, "provider_unavailable", None))
        # the error response carries the label of the mode it was produced in
        self.assertEqual((data["simulated"], data["label"]), (False, SECOND_OPINION_REAL_LABEL))
        self.assertEqual(store.list_records(), [])

    def real_app(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        service = DemoService(store, second_opinion_provider=FakeRealProvider(),
                              second_opinion_simulated=False)
        return make_app(service), store

    def test_state_endpoint_reports_the_real_mode_and_keeps_every_other_field(self):
        real, _ = self.real_app()
        real_state = as_json(call(real, "GET", "/api/state"))
        default_state = as_json(call(self.app, "GET", "/api/state"))
        self.assertIs(real_state["second_opinion"]["simulated"], False)
        self.assertIs(default_state["second_opinion"]["simulated"], True)
        # only the mode differs
        real_state["second_opinion"]["simulated"] = True
        self.assertEqual(real_state, default_state)

    def test_a_real_mode_request_returns_the_real_labelled_response_over_http(self):
        real, store = self.real_app()
        response = self.so_call(self.so_body(), app=real)
        data = as_json(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(sorted(data), ["error_code", "label", "model", "opinion", "simulated"])
        self.assertEqual((data["opinion"], data["model"], data["error_code"], data["simulated"]),
                         ("Keep the leaves dry.", "fake-model", None, False))
        self.assertEqual(data["label"], SECOND_OPINION_REAL_LABEL)
        self.assertNotIn("simulated", data["label"].lower())
        self.assertNotIn(b"SIMULATED", response.body)
        self.assertEqual(store.list_records(), [])
        # the same request in the default mode is still the simulated one
        default = as_json(self.so_call(self.so_body()))
        self.assertIs(default["simulated"], True)
        self.assertEqual(default["label"], SECOND_OPINION_LABEL)

    def test_second_opinion_unexpected_errors_never_leak_details(self):
        with mock.patch.object(self.service, "get_second_opinion",
                               side_effect=RuntimeError("secret detail")):
            response = self.so_call(self.so_body())
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"secret detail", response.body)
        self.assertEqual(as_json(response), {"error": "Internal error in the demo application."})

    def test_second_opinion_adds_to_the_routes_without_changing_the_others(self):
        self.assertEqual(call(self.app, "GET", "/api/second-opinion").status, 404)
        self.assertEqual(call(self.app, "POST", "/api/second-opinion/x").status, 404)
        self.assertEqual(call(self.app, "POST", "/api/other").status, 404)
        self.assertEqual(call(self.app, "PUT", "/api/second-opinion").status, 405)
        self.assertEqual(post_json(self.app, "/api/run", {"scenario": "probe_measurement"}).status, 200)
        self.assertEqual(call(self.app, "POST", "/api/analyze", png_bytes(),
                              {"content-type": "image/png"}).status, 200)
        self.assertEqual(call(self.app, "GET", "/api/state").status, 200)
        self.assertEqual(as_json(call(self.app, "GET", "/api/history"))["total"], 1)  # only /api/run

    def test_second_opinion_response_contains_no_environment_value(self):
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "canary-not-a-real-key"}):
            response = self.so_call(self.so_body())
        self.assertNotIn(b"canary-not-a-real-key", response.body)

    def test_a_png_exactly_at_the_upload_limit_is_accepted(self):
        data = png_bytes() + b"\x00" * (MAX_IMAGE_BYTES - len(png_bytes()))
        self.assertEqual(len(data), MAX_IMAGE_BYTES)
        response = call(self.app, "POST", "/api/analyze", data, {"content-type": "image/png"})
        self.assertEqual(response.status, 200)

    def test_other_methods_and_paths(self):
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            response = call(self.app, method, "/")
            self.assertEqual((response.status, response.allow), (405, "GET, POST"), method)
        self.assertEqual(call(self.app, "POST", "/api/other").status, 404)
        self.assertEqual(call(self.app, "GET", "/api/other").status, 404)

    def test_internal_errors_never_leak_details(self):
        with mock.patch.object(self.service, "run_scenario", side_effect=RuntimeError("secret detail")):
            response = post_json(self.app, "/api/run", {"scenario": "probe_measurement"})
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"secret detail", response.body)

    def test_no_environment_value_reaches_any_response(self):
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "canary-not-a-real-key"}):
            responses = [call(self.app, "GET", p) for p in ("/", "/static/app.js", "/api/state", "/api/history")]
            responses.append(post_json(self.app, "/api/run", {"scenario": "normal_operation"}))
            responses.append(call(self.app, "POST", "/api/analyze", png_bytes(), {"content-type": "image/png"}))
        for response in responses:
            self.assertNotIn(b"canary-not-a-real-key", response.body)

    def test_a_missing_static_file_is_a_startup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(app_module, "STATIC_DIR", Path(directory)):
                with self.assertRaises(RuntimeError):
                    DemoApp(self.service, {HOST})

    def test_the_page_script_never_uses_innerhtml_or_eval(self):
        script = call(self.app, "GET", "/static/app.js").body.decode("utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write",
                       "Function("):
            self.assertNotIn(banned, script, banned)
        page = call(self.app, "GET", "/").body.decode("utf-8")
        scripts = re.findall(r"<script\b[^>]*>", page)
        self.assertTrue(scripts and all("src=" in tag for tag in scripts))  # no inline script


class TestSecondOpinionPage(NoSockets):
    """Static checks on the served page. They read the files; they do not run the script.

    Browser behaviour (validation, in-flight handling, rendering) needs a JavaScript runtime
    and a DOM, which this suite deliberately does not require.
    """

    def setUp(self):
        super().setUp()
        service, _ = make_service(self)
        app = make_app(service)
        self.html = call(app, "GET", "/").body.decode("utf-8")
        self.script = call(app, "GET", "/static/app.js").body.decode("utf-8")
        self.css = call(app, "GET", "/static/style.css").body.decode("utf-8")
        self.state = as_json(call(app, "GET", "/api/state"))

    def test_the_new_section_follows_history_and_the_four_others_are_intact(self):
        order = [self.html.index('<section id="%s">' % name)
                 for name in ("rover", "record", "image", "history", "second-opinion")]
        self.assertEqual(order, sorted(order))
        for heading in ("1. Mock rover", "2. Readings and Record validation",
                        "3. Image (demo-only analysis)", "4. Saved Record history",
                        "5. Second Opinion"):
            self.assertIn("<h2>%s</h2>" % heading, self.html)
        for element in ("run", "analyze", "refresh", "file", "scenario"):
            self.assertIn('id="%s"' % element, self.html)

    def test_the_form_has_labelled_controls_and_starts_disabled(self):
        for control in ("so-file", "so-crop", "so-language"):
            self.assertIn('id="%s"' % control, self.html)
            self.assertIn('for="%s"' % control, self.html)
        self.assertIn('accept="image/png"', self.html)
        self.assertRegex(self.html, r'<button id="so-submit" type="submit" disabled>')
        self.assertRegex(self.html, r'id="so-status" class="status" role="status"')

    def test_limits_and_reading_names_come_from_the_server_not_the_page(self):
        block = self.state["second_opinion"]
        for key in ("simulated", "max_image_bytes", "crop_max_chars", "language_max_chars",
                    "reading_groups"):
            self.assertIn(key, block)
            self.assertIn(key, self.script, key)
        self.assertIn("state.second_opinion", self.script)
        for group in block["reading_groups"]:
            for key in group["keys"]:
                for source in (self.html, self.script):
                    self.assertNotIn('"%s"' % key, source, key)
                    self.assertNotIn("'%s'" % key, source, key)
        for number in (str(block["crop_max_chars"]), str(block["language_max_chars"]),
                       str(block["max_image_bytes"])):
            self.assertNotIn('maxlength="%s"' % number, self.html)

    def test_the_request_uses_the_shared_helper_endpoint_and_exact_field_names(self):
        self.assertEqual(self.script.count("fetch("), 1)  # only the existing api() helper
        self.assertIn('api("/api/second-opinion"', self.script)
        for name in ("image_png_base64", "readings:", "crop:", "language:"):
            self.assertIn(name, self.script, name)
        self.assertIn('"Content-Type": "application/json"', self.script)
        self.assertGreaterEqual(self.script.count("SEND"), 4)  # the X-Nelavi-Demo header object

    def test_each_reading_label_is_grouped_with_its_own_input(self):
        # A flat label/input pair inside a multi-column grid puts labels beside inputs.
        body = self.script[self.script.index("function buildReadingFields"):]
        body = body[:body.index("\n}\n")]
        for fragment in ('field.className = "reading-field"', "field.appendChild(label)",
                         "field.appendChild(input)", "grid.appendChild(field)"):
            self.assertIn(fragment, body, fragment)
        self.assertNotIn("grid.appendChild(label)", body)
        self.assertNotIn("grid.appendChild(input)", body)
        self.assertIn(".reading-field", self.css)

    def test_the_client_side_checks_are_present(self):
        for fragment in ("file.type !== \"image/png\"", "file.size > soConfig.maxImageBytes",
                         "Number.isFinite", "NUMBER_PATTERN", "readAsDataURL", "reader.onerror",
                         "DATA_URL_PREFIX", "soBusy", "button.disabled", "finally"):
            self.assertIn(fragment, self.script, fragment)

    def test_an_opinion_is_only_shown_when_the_response_matches_the_announced_mode(self):
        body = self.script[self.script.index("function renderSecondOpinion"):]
        body = body[:body.index("\n}\n")]
        for fragment in ('typeof result.simulated !== "boolean"',
                         "result.simulated !== soConfig.simulated"):
            self.assertIn(fragment, body, fragment)
            self.assertLess(body.index(fragment), body.index("so-opinion"), fragment)
        self.assertNotIn("simulated !== true", self.script)  # the mode is no longer hardcoded
        # the announced mode must itself be a real boolean from the server
        config = self.script[self.script.index("function parseSecondOpinionConfig"):]
        self.assertIn('typeof raw.simulated !== "boolean"', config[:config.index("\n}\n")])

    def test_rendering_is_text_only_with_no_inline_code_or_external_requests(self):
        self.assertIn("white-space: pre-wrap", self.css)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function",
                       "document.write", "XMLHttpRequest", "WebSocket", "sendBeacon",
                       "localStorage", "sessionStorage", "document.cookie", "http://", "https://"):
            self.assertNotIn(banned, self.script, banned)
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", self.html))  # no inline event handlers
        self.assertNotIn("<style", self.html)
        self.assertNotIn("style=", self.html)  # the CSP allows stylesheets from this origin only
        self.assertNotIn("http://", self.html + self.css)
        self.assertNotIn("https://", self.html + self.css)
        self.assertEqual(re.findall(r"<script\b[^>]*>", self.html), ['<script src="/static/app.js">'])

    # ---- mode disclosure: the page claims a mode only after the server has confirmed it
    def function_text(self, name):
        """The source of one top-level script function, with adjacent string literals joined
        and lower-cased, so wording split across a `+` can still be searched."""
        body = self.script[self.script.index("function %s(" % name):]
        body = body[:body.index("\n}\n")]
        literals = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        return body, " ".join("".join(literals).lower().split())

    SIMULATED_ONLY_CLAIMS = ("no network", "no ai model", "simulated demonstration",
                             "only to this local server", "does not run an ai model",
                             "fixed demonstration text", "simulated.")

    def test_the_shipped_page_makes_no_mode_claim_and_keeps_submission_off(self):
        # Section 3 (the demo image analysis) is always local, so its own "only to this local
        # server" sentence is true in every mode and is deliberately outside this check.
        lowered_page = " ".join(self.html.lower().split())
        for claim in ("simulated demonstration", "real model", "mixed mode", "no ai model",
                      "does not run an ai model", "fixed demonstration text", "no network"):
            self.assertNotIn(claim, lowered_page, claim)
        for element in ("banner", "so-notice", "so-help", "so-status"):
            piece = self.html[self.html.index('id="%s"' % element):]
            text = " ".join(piece[:piece.index("</p>")].split()).lower()
            self.assertIn("application mode", text, element)
            for claim in self.SIMULATED_ONLY_CLAIMS + ("simulated.", "real model"):
                self.assertNotIn(claim, text, (element, claim))
        self.assertRegex(self.html, r'<button id="so-submit" type="submit" disabled>Get second opinion</button>')

    def test_simulated_wording_is_written_only_after_the_server_confirms_simulated_true(self):
        body, lowered = self.function_text("showSimulatedDisclosures")
        for text in ("simulated demonstration.", "no ai model", "no network",
                     "fixed demonstration text", "only to this local server"):
            self.assertIn(text, lowered, text)
        for element in ("setBanner(", '$("so-notice").textContent', '$("so-help").textContent'):
            self.assertIn(element, body, element)
        # the simulated-only claims exist nowhere else in the script
        elsewhere = " ".join(self.script.replace(body, "").lower().split())
        for claim in ("no network", "only to this local server", "fixed demonstration text"):
            self.assertNotIn(claim, elsewhere, claim)
        configure =self.script[self.script.index("function configureSecondOpinion("):]
        configure = configure[:configure.index("\n}\n")]
        self.assertIn("if (config.simulated) showSimulatedDisclosures();", configure)
        self.assertIn("else showRealModeDisclosures();", configure)

    def test_real_mode_wording_discloses_the_cloud_and_makes_no_simulated_claim(self):
        body, lowered = self.function_text("showRealModeDisclosures")
        for element in ("setBanner(", '$("so-notice").textContent', '$("so-help").textContent'):
            self.assertIn(element, body, element)
        for disclosure in ("cloud ai provider", "sends the png", "crop, language and readings",
                           "internet", "not a diagnosis", "has not been verified"):
            self.assertIn(disclosure, lowered, disclosure)
        for claim in self.SIMULATED_ONLY_CLAIMS:
            self.assertNotIn(claim, lowered, claim)

    def test_an_unconfirmed_mode_shows_an_unavailable_message_and_disables_submission(self):
        body, lowered = self.function_text("showModeUnavailable")
        self.assertIn("application mode unavailable.", lowered)
        self.assertIn("disabled", lowered)
        for claim in self.SIMULATED_ONLY_CLAIMS + ("mixed mode", "real model"):
            self.assertNotIn(claim, lowered, claim)
        # every failure path goes through disableSecondOpinion, which disables and explains
        disable, _ = self.function_text("disableSecondOpinion")
        self.assertIn('$("so-submit").disabled = true', disable)
        self.assertIn("showModeUnavailable();", disable)
        configure = self.script[self.script.index("function configureSecondOpinion("):]
        configure = configure[:configure.index("\n}\n")]
        self.assertLess(configure.index("if (!config)"), configure.index("showSimulatedDisclosures"))
        self.assertIn("disableSecondOpinion(", configure[:configure.index("return;")])
        start = self.script[self.script.index("async function start("):]
        start = start[:start.index("\n}\n")]
        # the submit handler exists before the mode is known; it refuses to act without one
        self.assertLess(start.index('$("so-form").addEventListener("submit", submitSecondOpinion)'),
                        start.index('await api("/api/state")'))
        self.assertIn("if (soBusy || !soConfig) return;", self.script)
        self.assertIn("catch (e)", start)
        self.assertIn("disableSecondOpinion(", start[start.index("catch (e)"):])

    def test_the_submit_button_is_only_enabled_by_a_confirmed_configuration(self):
        enabled = [m.start() for m in re.finditer(r'\$\("so-submit"\)\.disabled = false', self.script)]
        self.assertEqual(len(enabled), 1)  # exactly one place enables the button...
        configure_at = self.script.index("function configureSecondOpinion(")
        self.assertGreater(enabled[0], configure_at)  # ...inside configureSecondOpinion...
        self.assertGreater(enabled[0], self.script.index("if (!config) {", configure_at))  # ...after validation
        # and a busy-state restore can never enable it without a configuration
        self.assertIn("button.disabled = soConfig === null;", self.script)

    def test_it_does_not_touch_the_record_history(self):
        body = self.script[self.script.index("async function submitSecondOpinion"):]
        body = body[:body.index("\n}\n")]
        self.assertNotIn("refreshHistory", body)
        self.assertNotIn("/api/history", body)


class FakeConnection:
    """Just enough of a socket to drive the real request handler without a network."""

    def __init__(self, raw):
        self._raw = raw
        self.sent = bytearray()

    def makefile(self, mode="r", *args, **kwargs):
        return io.BytesIO(self._raw)

    def sendall(self, data):
        self.sent += data


def exchange(app, raw_request):
    holder = server_module._AppHolder()
    holder.app = app
    connection = FakeConnection(raw_request)
    server_module.make_handler(holder)(connection, ("127.0.0.1", 0), None)
    head, _, body = bytes(connection.sent).partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip() for line in lines[1:]}
    return lines[0], headers, body


class TestServerAdapter(NoSockets):
    def setUp(self):
        super().setUp()
        self.service, self.store = make_service(self)
        self.app = make_app(self.service)

    def test_it_listens_on_loopback_only(self):
        self.assertEqual(server_module.HOST, "127.0.0.1")

    def test_get_through_the_real_handler(self):
        status, headers, body = exchange(self.app, b"GET / HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n\r\n")
        self.assertTrue(status.endswith("200 OK"), status)
        self.assertIn(b"Checking application mode", body)
        self.assertNotIn(b"SIMULATED DEMONSTRATION", body)
        self.assertEqual(headers["content-length"], str(len(body)))
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertNotIn("python", headers.get("server", "").lower())

    def test_post_through_the_real_handler_runs_and_stores(self):
        payload = b'{"scenario": "probe_measurement"}'
        raw = (b"POST /api/run HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\nX-Nelavi-Demo: 1\r\n"
               b"Content-Type: application/json\r\nContent-Length: " + str(len(payload)).encode()
               + b"\r\n\r\n" + payload)
        status, headers, body = exchange(self.app, raw)
        self.assertTrue(status.endswith("200 OK"), status)
        self.assertEqual(json.loads(body)["stored_id"], 1)
        self.assertEqual(len(self.store.list_records()), 1)

    def test_png_upload_through_the_real_handler(self):
        data = png_bytes()
        raw = (b"POST /api/analyze HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\nX-Nelavi-Demo: 1\r\n"
               b"Content-Type: image/png\r\nContent-Length: " + str(len(data)).encode()
               + b"\r\n\r\n" + data)
        status, _, body = exchange(self.app, raw)
        self.assertTrue(status.endswith("200 OK"), status)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_bad_host_and_wrong_method_through_the_real_handler(self):
        status, _, _ = exchange(self.app, b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n")
        self.assertIn("403", status)
        status, headers, body = exchange(self.app, b"PUT / HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n\r\n")
        self.assertIn("405", status)
        self.assertEqual(headers["allow"], "GET, POST")
        status, _, body = exchange(self.app, b"HEAD / HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n\r\n")
        self.assertEqual(body, b"")

    def test_an_unexpected_exception_becomes_a_generic_500(self):
        class Broken:
            def handle_request(self, *args):
                raise RuntimeError("secret detail")
        status, _, body = exchange(Broken(), b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn("500", status)
        self.assertNotIn(b"secret detail", body)

    def test_main_binds_to_loopback_and_reports_the_address(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_module, "HTTPServer") as fake_server:
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main([], stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(fake_server.call_args.args[0], ("127.0.0.1", server_module.DEFAULT_PORT))
        instance.server_close.assert_called_once()
        self.assertIn("http://127.0.0.1:12345/", out.getvalue())
        self.assertIn("SIMULATED", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_main_reports_startup_problems_clearly(self):
        for argv in (["--port", "0"], ["--port", "70000"]):
            err = io.StringIO()
            self.assertEqual(server_module.main(argv, stdout=io.StringIO(), stderr=err), 2)
            self.assertIn("--port", err.getvalue())
        err = io.StringIO()
        with mock.patch.object(server_module, "HTTPServer", side_effect=OSError("in use")):
            self.assertEqual(server_module.main([], stdout=io.StringIO(), stderr=err), 2)
        self.assertIn("could not listen", err.getvalue())
        self.assertIn("--port", err.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            err = io.StringIO()
            missing = os.path.join(directory, "no_such_folder", "history.sqlite3")
            self.assertEqual(server_module.main(["--db", missing], stdout=io.StringIO(), stderr=err), 2)
            self.assertIn("history database", err.getvalue())


class TestAdviceInTheDemo(NoSockets):
    def setUp(self):
        super().setUp()
        self.service, self.store = make_service(self)
        self.app = make_app(self.service)

    def run_scenario(self, name="probe_measurement"):
        return call(self.app, "POST", "/api/run", json.dumps({"scenario": name}).encode(),
                    {"content-type": "application/json"})

    def test_a_run_reports_that_no_rules_are_loaded_and_invents_no_advice(self):
        for name in ("normal_operation", "probe_measurement", "sensor_fault"):
            with self.subTest(scenario=name):
                response = self.run_scenario(name)
                data = as_json(response)
                self.assertEqual(response.status, 200)
                advice = data["advice"]
                self.assertEqual((advice["outcome"], advice["reason"]), ("insufficient_evidence", "no_rules"))
                self.assertEqual((advice["items"], advice["not_assessed"], advice["errors"]), ([], [], []))
                self.assertEqual(advice["rules_version"], "none")
                self.assertIn("units_unresolved", [c["code"] for c in advice["caveats"]])

    def test_advice_is_not_stored_and_does_not_change_the_record_or_history(self):
        data = as_json(self.run_scenario())
        stored = self.store.list_records()
        self.assertEqual(len(stored), 1)
        self.assertNotIn("advice", stored[0].record)
        self.assertEqual(stored[0].record, data["record"])
        self.assertNotIn("advice", json.dumps(as_json(call(self.app, "GET", "/api/history"))))

    def test_the_run_response_keeps_every_earlier_field(self):
        data = as_json(self.run_scenario())
        for key in ("scenario", "description", "steps", "record", "readings", "photo",
                    "validation", "stored_id"):
            self.assertIn(key, data)

    def test_the_service_runs_the_pipeline_as_simulated_with_no_rules_sources_or_provider(self):
        with mock.patch("demo_app.service.run_pipeline") as fake:
            fake.return_value = {"advice": {"outcome": "x"}, "status": "ok"}
            self.service.run_scenario("probe_measurement")
        (record,), kwargs = fake.call_args
        self.assertEqual(kwargs, {"origin": "simulated", "user": None})  # the default (empty) ruleset
        self.assertTrue(validate_record(record).valid)

    def run_with(self, **fields):
        payload = dict({"scenario": "probe_measurement"}, **fields)
        return call(self.app, "POST", "/api/run", json.dumps(payload).encode(), {"content-type": "application/json"})

    def test_the_run_returns_a_simulated_farmer_report_with_nothing_unsupported(self):
        data = as_json(self.run_with())
        report = data["report"]
        self.assertEqual((report["status"], report["origin"]), ("ok", "simulated"))
        self.assertTrue(report["origin_note"].startswith("SIMULATED"))
        sections = report["sections"]
        self.assertEqual(sections["image_inference"]["status"], "not_run")
        self.assertEqual(sections["retrieved_sources"]["entries"], [])
        self.assertEqual(sections["recommendations"]["supported"], [])
        self.assertEqual(data["advice"], report["advice"])
        self.assertNotIn("report", data["record"])  # the Record itself is unchanged
        self.assertNotIn("report", json.dumps(self.store.list_records()[0].record))

    def test_a_stated_crop_region_and_stage_reach_the_report_and_are_never_guessed(self):
        data = as_json(self.run_with(crop="Tomato", region="India", growth_stage=""))
        context = data["report"]["context"]
        self.assertEqual((context["crop"]["status"], context["crop"]["candidates"][0]["name"]), ("user_provided", "Tomato"))
        self.assertEqual((context["location"]["kind"], context["location"]["region"]), ("region", "India"))
        self.assertEqual(context["growth_stage"]["status"], "unknown")
        self.assertIn("growth_stage", context["missing"])
        self.assertIn("State the growth stage", " ".join(data["report"]["sections"]["next_steps"]))

    def test_blank_values_and_unknown_keys_are_ignored(self):
        data = as_json(self.run_with(crop="   ", region=None, unknown_key="x"))
        self.assertEqual(data["report"]["context"]["crop"]["status"], "unknown")

    def test_invalid_user_values_are_refused_before_anything_runs_and_are_not_echoed(self):
        for fields in ({"crop": "<script>alert(1)</script>"}, {"crop": 5}, {"region": "x" * 65},
                       {"growth_stage": "line\nbreak"}, {"crop": ["Tomato"]}):
            with self.subTest(fields=str(fields)[:30]):
                response = self.run_with(**fields)
                self.assertEqual(response.status, 400)
                self.assertNotIn(b"script", response.body)
                self.assertNotIn(b"xxxx", response.body)
        self.assertEqual(self.store.list_records(), [])  # nothing was stored by a refused request

    def test_repeated_runs_each_store_one_record_and_report_independently(self):
        first = as_json(self.run_with(crop="Tomato"))
        second = as_json(self.run_with())
        self.assertEqual(len(self.store.list_records()), 2)
        self.assertEqual(first["report"]["context"]["crop"]["status"], "user_provided")
        self.assertEqual(second["report"]["context"]["crop"]["status"], "unknown")  # nothing carried over

    def test_the_report_area_and_inputs_are_text_only_and_state_what_is_unavailable(self):
        static = Path(demo_app.__file__).resolve().parent / "static"
        page = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        for element in ('id="user-crop"', 'id="user-region"', 'id="user-stage"', 'id="report-body"', 'id="report-origin"'):
            self.assertIn(element, page)
        body = script[script.index("function addSection"):script.index("function renderAnalysis")]
        for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval("):
            self.assertNotIn(forbidden, body)
        for heading in ("What the rover observed", "What the AI inferred from images", "What the sensors measured",
                        "What was retrieved from agricultural sources", "Recommendations that are supported",
                        "Uncertain or unavailable", "What would help next"):
            self.assertIn(heading, script)
        self.assertIn("Research is not configured on this server", script)
        self.assertIn("Section 6 can search for sources separately", script)
        self.assertNotIn("not connected to this page", script)
        self.assertIn("unit not defined", script)
        self.assertIn("none confirmed", script)
        self.assertIn("user-crop", script[script.index("async function runScenario"):])

    def test_the_page_has_a_text_only_advice_area_with_fixed_wording(self):
        page = (Path(demo_app.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        script = (Path(demo_app.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="advice"', page)
        self.assertIn('id="advice-items"', page)
        body = script[script.index("function renderAdvice"):]
        body = body[:body.index("function renderAnalysis")]
        self.assertNotIn("innerHTML", body)
        self.assertNotIn("insertAdjacentHTML", body)
        self.assertIn("no agronomic rules have been loaded", script)
        for outcome in ("insufficient_evidence", "no_rule_fired", "invalid_record", "invalid_ruleset"):
            self.assertIn(outcome, script)


CANARY_KEY = "canary-not-a-real-key-1234567890"


def nvidia_chat_body(text):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


class ScriptedTransport:
    """A fake NVIDIA transport. It records each call and never touches the network."""

    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self.body = nvidia_chat_body("Keep the leaves dry.") if body is None else body
        self.error = error
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


class TestNvidiaSecondOpinionOverHttp(NoSockets):
    """The real-provider path through the whole app, with a fake transport. This proves the
    wiring and the error handling offline; it does not prove that a real NVIDIA request works."""

    def build(self, transport, environ=None, config=None):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        analyzer = NvidiaImageAnalyzer(
            config=config, transport=transport,
            environ={"NVIDIA_API_KEY": CANARY_KEY} if environ is None else environ)
        service = DemoService(store, second_opinion_provider=analyzer,
                              second_opinion_simulated=False)
        return make_app(service), store

    def post(self, app, **overrides):
        return post_json(app, "/api/second-opinion", second_opinion_payload(**overrides))

    def test_success_is_labelled_real_and_the_request_follows_the_defined_payload(self):
        transport = ScriptedTransport()
        app, store = self.build(transport)
        response = self.post(app)
        data = as_json(response)
        self.assertEqual(response.status, 200)
        self.assertEqual((data["opinion"], data["error_code"], data["simulated"]),
                         ("Keep the leaves dry.", None, False))
        self.assertEqual(data["label"], SECOND_OPINION_REAL_LABEL)
        self.assertNotIn("simulated", data["label"].lower())
        self.assertEqual(data["model"], "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
        self.assertEqual(as_json(call(app, "GET", "/api/state"))["second_opinion"]["simulated"],
                         False)
        self.assertEqual(len(transport.calls), 1)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://integrate.api.nvidia.com/v1/chat/completions")
        self.assertEqual(timeout, 60.0)
        self.assertEqual(headers["Authorization"], "Bearer " + CANARY_KEY)
        content = json.loads(body)["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image_url"])
        self.assertIn("Tomato", content[0]["text"])
        self.assertIn("English", content[0]["text"])
        self.assertIn("soil.", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(store.list_records()), 0)

    def assert_error(self, app, store, expected_code, before):
        response = self.post(app)
        data = as_json(response)
        self.assertEqual(response.status, 422)
        self.assertEqual(data["error_code"], expected_code)
        self.assertIsNone(data["opinion"])
        self.assertIs(data["simulated"], False)
        self.assertEqual(data["label"], SECOND_OPINION_REAL_LABEL)
        self.assertNotIn(CANARY_KEY, response.body.decode("utf-8"))
        self.assertNotIn("SIMULATED", response.body.decode("utf-8"))
        self.assertEqual(len(store.list_records()), before)  # history is untouched

    def run_one_record(self, app, store):
        self.assertEqual(call(app, "POST", "/api/run", b'{"scenario": "probe_measurement"}',
                              {"content-type": "application/json"}).status, 200)
        self.assertEqual(len(store.list_records()), 1)
        return 1

    def test_all_producible_error_codes_are_reported_without_an_answer_and_leave_history_alone(self):
        small_limit = NvidiaConfig(max_image_bytes=10)  # the demo PNG is larger than this
        invalid_config = NvidiaConfig(timeout_seconds=0)
        cases = {
            "credential_missing": dict(transport=ScriptedTransport(), environ={}),
            "configuration_invalid": dict(transport=ScriptedTransport(), config=invalid_config),
            "image_unsupported": dict(transport=ScriptedTransport(), config=small_limit),
            "provider_rejected": dict(transport=ScriptedTransport(status=401, body=b"secret " + CANARY_KEY.encode())),
            "provider_unavailable": dict(transport=ScriptedTransport(status=503, body=b"busy")),
            "provider_timeout": dict(transport=ScriptedTransport(error=TransportTimeout())),
            "provider_connection_failed": dict(transport=ScriptedTransport(error=TransportError())),
            "transport_failed": dict(transport=ScriptedTransport(error=RuntimeError("x"))),
            "response_malformed": dict(transport=ScriptedTransport(body=b"not json")),
            "response_empty": dict(transport=ScriptedTransport(body=nvidia_chat_body("   "))),
            "response_too_large": dict(transport=ScriptedTransport(error=ResponseTooLarge())),
        }
        # `provider_unreachable` stays in the closed set for compatibility (IA-6) but the NVIDIA
        # adapter no longer produces it, so it cannot be provoked here.
        self.assertEqual(set(cases), set(ERROR_CODES) - {"provider_unreachable"})
        for code, arguments in cases.items():
            with self.subTest(code=code):
                app, store = self.build(**arguments)
                before = self.run_one_record(app, store)
                self.assert_error(app, store, code, before)

    def test_a_plain_transport_failure_is_a_connection_failure(self):
        app, store = self.build(ScriptedTransport(error=TransportError()))
        self.assert_error(app, store, "provider_connection_failed", 0)

    def test_an_answer_that_breaks_the_length_rule_is_never_shown(self):
        for text in ("word " * 80, "word " * 500):
            with self.subTest(words=len(text.split())):
                app, store = self.build(ScriptedTransport(body=nvidia_chat_body(text)))
                self.assert_error(app, store, "response_malformed", 0)

    def test_an_answer_just_under_the_length_rule_is_shown(self):
        text = " ".join(["word"] * 79)
        app, _ = self.build(ScriptedTransport(body=nvidia_chat_body(text)))
        self.assertEqual(as_json(self.post(app))["opinion"], text)

    def test_other_non_success_statuses_and_wrong_shapes_are_malformed(self):
        shapes = (ScriptedTransport(status=302, body=b""),
                  ScriptedTransport(body=b'{"choices": []}'),
                  ScriptedTransport(body=json.dumps({"choices": [{"message": {"content": 5}}]}).encode()))
        for transport in shapes:
            app, store = self.build(transport)
            self.assert_error(app, store, "response_malformed", 0)

    def test_an_invalid_request_never_reaches_the_provider(self):
        transport = ScriptedTransport()
        app, _ = self.build(transport)
        response = self.post(app, crop="")
        self.assertEqual(response.status, 400)
        self.assertEqual(transport.calls, [])
        response = self.post(app, image_png_base64="not base64!")
        self.assertEqual(response.status, 400)
        self.assertEqual(transport.calls, [])

    def test_a_service_cannot_report_a_simulated_result_when_declared_real(self):
        class Wrong:
            def analyze(self, request):
                from second_opinion.simulated_provider import SIMULATED_CAVEAT
                return AnalysisResult("ok", "Looks fine.", None, "x", (SIMULATED_CAVEAT,))
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        app = make_app(DemoService(store, second_opinion_provider=Wrong(),
                                   second_opinion_simulated=False))
        data = as_json(self.post(app))
        self.assertIsNone(data["opinion"])
        self.assertEqual(data["error_code"], "response_malformed")

    def test_the_default_service_is_still_simulated_and_never_calls_the_adapter(self):
        service, _ = make_service(self)
        with mock.patch.object(NvidiaImageAnalyzer, "analyze",
                               side_effect=AssertionError("real adapter used")):
            data = as_json(self.post(make_app(service)))
        self.assertIs(data["simulated"], True)
        self.assertEqual(data["label"], SECOND_OPINION_LABEL)

    def test_the_key_never_appears_in_any_response_or_state(self):
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": CANARY_KEY}):
            app, _ = self.build(ScriptedTransport(status=500), environ=os.environ)
            for response in (self.post(app), call(app, "GET", "/api/state"),
                             call(app, "GET", "/api/history"), call(app, "GET", "/")):
                self.assertNotIn(CANARY_KEY.encode(), response.body)


class TestProviderSelectionAtStartup(NoSockets):
    def run_main(self, argv, key_present=True, environ_value=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_module, "HTTPServer") as fake_server, \
                mock.patch.object(server_module, "DemoApp") as fake_app, \
                mock.patch.object(server_module, "api_key_present", return_value=key_present), \
                mock.patch.object(server_module, "NvidiaImageAnalyzer",
                                  wraps=NvidiaImageAnalyzer) as fake_adapter:
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_server, fake_app, fake_adapter

    def configured_mode(self, fake_app):
        service = fake_app.call_args.args[0]
        return service.state()["second_opinion"]["simulated"]

    def test_default_is_simulated_and_the_adapter_is_not_built(self):
        code, out, err, _, fake_app, fake_adapter = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIs(self.configured_mode(fake_app), True)
        fake_adapter.assert_not_called()
        self.assertIn("SIMULATED DATA ONLY", out)
        self.assertEqual(err, "")

    def test_explicit_simulated_matches_the_default(self):
        code, out, _, _, fake_app, fake_adapter = self.run_main(
            ["--second-opinion-provider", "simulated"], key_present=False)
        self.assertEqual(code, 0)
        self.assertIs(self.configured_mode(fake_app), True)
        fake_adapter.assert_not_called()

    def test_explicit_nvidia_selects_the_real_provider_and_says_so(self):
        code, out, err, _, fake_app, fake_adapter = self.run_main(
            ["--second-opinion-provider", "nvidia"])
        self.assertEqual(code, 0)
        self.assertIs(self.configured_mode(fake_app), False)
        fake_adapter.assert_called_once_with()
        self.assertIn("REAL", out)
        self.assertIn("NVIDIA", out)
        self.assertIn("MIXED MODE", out)
        self.assertNotIn("SIMULATED DATA ONLY", out)
        self.assertEqual(err, "")

    def test_nvidia_without_a_key_refuses_to_start_and_does_not_fall_back(self):
        code, out, err, fake_server, fake_app, fake_adapter = self.run_main(
            ["--second-opinion-provider", "nvidia"], key_present=False)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("NVIDIA_API_KEY", err)
        self.assertIn("no fallback", err)
        fake_server.assert_not_called()  # nothing was bound
        fake_app.assert_not_called()
        fake_adapter.assert_not_called()

    def test_an_invalid_provider_name_is_rejected_by_the_parser(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(server_module, "HTTPServer") as fake:
            with self.assertRaises(SystemExit) as raised:
                server_module.main(["--second-opinion-provider", "openai"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())
        fake.assert_not_called()

    def test_the_key_value_is_never_printed_even_when_set(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": CANARY_KEY}), \
                mock.patch.object(server_module, "HTTPServer") as fake_server:
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main(["--second-opinion-provider", "nvidia"],
                                      stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertNotIn(CANARY_KEY, out.getvalue() + err.getvalue())
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": ""}):
            err = io.StringIO()
            self.assertEqual(server_module.main(["--second-opinion-provider", "nvidia"],
                                                stdout=io.StringIO(), stderr=err), 2)

    def test_the_help_documents_both_choices_and_the_no_fallback_rule(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                server_module.main(["--help"])
        text = " ".join(out.getvalue().split())
        for expected in ("--second-opinion-provider", "simulated", "nvidia", "no automatic fallback"):
            self.assertIn(expected, text)


class TestSecondOpinionErrorWording(unittest.TestCase):
    SCRIPT = (Path(demo_app.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")

    def test_every_closed_error_code_has_fixed_wording(self):
        block = self.SCRIPT[self.SCRIPT.index("const SECOND_OPINION_ERRORS"):]
        block = block[:block.index("};")]
        for code in ERROR_CODES:
            self.assertIn(code + ":", block, code)

    def test_the_real_request_warns_about_the_wait_and_no_answer_is_fabricated(self):
        self.assertIn("this can take up to a minute", self.SCRIPT)
        self.assertIn("No answer was produced", self.SCRIPT)
        self.assertNotIn("innerHTML", self.SCRIPT)


class TestIsolation(unittest.TestCase):
    PACKAGE = Path(demo_app.__file__).resolve().parent
    STDLIB = {"argparse", "base64", "binascii", "dataclasses", "hashlib", "hmac", "http", "json", "os", "pathlib", "sqlite3",
              "struct", "sys", "time", "urllib.parse"}
    PROJECT = {"agri_pipeline", "demo_app", "image_analysis", "mock_rover", "orchestration", "record", "research",
               "second_opinion", "storage", "synthesis_provider", "voice_assistant"}

    def sources(self):
        return {p.name: p.read_text(encoding="utf-8") for p in self.PACKAGE.glob("*.py")}

    def test_imports_are_standard_library_or_existing_project_modules_only(self):
        for name, source in self.sources().items():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    top = module.split(".")[0]
                    self.assertTrue(module in self.STDLIB or top in self.STDLIB | self.PROJECT,
                                    (name, module))
                    if top == "image_analysis":
                        # Only the server entry point may name the NVIDIA adapter (to build it
                        # when explicitly selected); every other demo module stays on the boundary.
                        allowed = ["image_analysis.boundary"]
                        if name == "server.py":
                            # IA-9: the second module is the NVIDIA transport that also notes Retry-After; it lives
                            # in image_analysis so that this package still opens no connection of its own.
                            allowed += ["image_analysis.nvidia", "image_analysis.nvidia_headers"]
                        self.assertIn(module, allowed, name)
                    if top == "research":
                        # Only the server entry point may build the research client; the service and the
                        # routes only ever see an object with a search() method.
                        self.assertIn((name, module), (("server.py", "research.tavily"),
                                                       ("server.py", "research.tavily_research")))
                    if top == "synthesis_provider":
                        # Only the server entry point may build the real synthesis adapter (when explicitly
                        # selected); the service and the routes only ever see an object with synthesize().
                        self.assertIn((name, module), (("server.py", "synthesis_provider.chat"),
                                                       ("server.py", "synthesis_provider.openrouter")))
                    if top == "agri_pipeline":
                        # Only the service may use the pipeline, and only its pipeline module (plus, for the
                        # integrated path, the findings module that wraps an injected image analyzer). The demo
                        # never imports the rules engine, the context schema or a provider itself.
                        # IA-11: also the closed-word reply inspection, imported by the service only.
                        self.assertIn((name, module), (("service.py", "agri_pipeline.pipeline"),
                                                       ("service.py", "agri_pipeline.findings"),
                                                       ("service.py", "agri_pipeline.reply_facts")))
                    if top == "orchestration":
                        # IM-2: only the service may use orchestration, and only its adapter module (the exact
                        # imported names are pinned in test_demo_orchestrated.py).
                        self.assertEqual((name, module), ("service.py", "orchestration"))
                    if top == "second_opinion":
                        self.assertIn(module, ("second_opinion", "second_opinion.second_opinion",
                                               "second_opinion.simulated_provider"), name)
                    if top == "voice_assistant":
                        self.assertIn(module, ("voice_assistant.boundary", "voice_assistant.assistant",
                                               "voice_assistant.lessons", "voice_assistant.elevenlabs"), name)
                    self.assertNotIn(module, ("socket", "ssl", "http.client", "urllib.request"), name)

    def test_no_provider_credentials_or_environment_access(self):
        for name, source in self.sources().items():
            if name == "server.py":
                # The entry point may import the key-presence check and may name the variable in
                # its messages. It still must not read provider keys or build provider requests.
                # M1: server.py may read the server's own bearer authentication token from the environment.
                source = (source.replace("api_key_present", "").replace("NVIDIA_API_KEY", "")
                          .replace("TAVILY_API_KEY", "").replace("os.environ.get(env_var_name)", ""))
            # (The analyzer's caveat text names the providers only to say none was used.)
            for banned in ("environ", "getenv", "NVIDIA_API_KEY", "urlopen", "api_key",
                           "subprocess", "eval(", "exec("):
                self.assertNotIn(banned, source.lower() if banned.islower() else source, (name, banned))

    def test_existing_modules_do_not_depend_on_the_demo(self):
        import image_analysis.boundary
        import mock_rover.simulator
        import record.validation
        import storage.record_store
        for module in (image_analysis.boundary, mock_rover.simulator, record.validation,
                       storage.record_store):
            self.assertNotIn("demo_app", Path(module.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
