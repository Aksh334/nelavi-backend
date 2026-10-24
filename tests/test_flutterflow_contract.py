"""Offline FlutterFlow Integration Contract Tests (Milestone M2.2).

Verifies:
1. OpenAPI specification file integrity, schema consistency, and route inventory alignment.
2. Actual DemoApp handler responses match OpenAPI response schemas for all 4 FlutterFlow screen workflows:
   - Screen 1: Dashboard / Field Overview (state, history, run scenario)
   - Screen 2: Integrated Field Analysis / Leaf Diagnostics (run-integrated, base64 PNG, second-opinion)
   - Screen 3: Agronomic Advisory & Voice Assistant (transcribe, assistant ask, synthesize)
   - Screen 4: Precision Agriculture Learning Academy (lessons catalog, lesson detail, progress)
3. Remote Bearer token authentication enforcement and non-leakage.
4. CORS preflight (OPTIONS) behavior and header matching.
5. Error handling: invalid payloads, missing required fields, bad content types, and size bounds.

100% offline: strict socket and urllib tripwires prevent any external network calls.
"""

import base64
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from demo_app.app import MAX_JSON_BYTES, MAX_SECOND_OPINION_BYTES, DemoApp
from demo_app.service import DemoService
from storage.record_store import RecordStore
from voice_assistant.boundary import SpeechResult, TranscriptionResult
from voice_assistant.elevenlabs import ElevenLabsClient

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO_ROOT / "docs" / "contracts" / "OPENAPI_SPEC.yaml"
HOST = "127.0.0.1:8765"
REMOTE_HOST = "api.cropsync.example.com"
TEST_AUTH_TOKEN = "ff-test-bearer-token-sec-9988"
CORS_ORIGIN = "https://cropsync.flutterflow.app"

# Small valid PNG fixture (16 bytes signature + IHDR + IDAT + IEND)
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


class FakeVoiceClient:
    """Offline test double for ElevenLabsClient."""

    def __init__(self):
        self.key_present = lambda: True

    def transcribe(self, request):
        return TranscriptionResult(
            status="ok",
            text="How is the soil moisture today?",
            language_code=request.language_code or "en",
        )

    def synthesize(self, request):
        return SpeechResult(
            status="ok",
            audio_bytes=b"fake-mp3-audio-bytes-4433",
            content_type="audio/mpeg",
        )


class OfflineTestBase(unittest.TestCase):
    """Guarantees network isolation for all contract tests."""

    def setUp(self):
        self._patchers = [
            mock.patch("socket.socket", side_effect=AssertionError("Socket tripwire triggered")),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS tripwire triggered")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("urllib tripwire triggered")),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib tripwire triggered")),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

        self.store = RecordStore(":memory:")
        self.addCleanup(self.store.close)
        self.fake_voice = FakeVoiceClient()
        self.service = DemoService(self.store, elevenlabs_client=self.fake_voice)

    def make_app(self, remote=False, auth_token=None, cors_origins=None, allowed_hosts=None):
        hosts = allowed_hosts or ({REMOTE_HOST} if remote else {HOST})
        return DemoApp(
            service=self.service,
            allowed_hosts=hosts,
            remote=remote,
            auth_token=auth_token,
            cors_origins=cors_origins,
        )

    def request(self, app, method, target, body=b"", headers=None):
        h = {"host": REMOTE_HOST if app._remote else HOST}
        if method == "POST":
            if not app._remote:
                h["x-nelavi-demo"] = "1"
            h["content-length"] = str(len(body))
        if headers:
            h.update({k.lower(): v for k, v in headers.items()})
        return app.handle_request(method, target, h, io.BytesIO(body).read)

    def json_post(self, app, target, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        h = {"content-type": "application/json"}
        if headers:
            h.update(headers)
        return self.request(app, "POST", target, body, h)


class TestOpenApiSpecificationIntegrity(OfflineTestBase):
    """Validates OPENAPI_SPEC.yaml exists, parses, and aligns with backend routes."""

    def test_spec_file_exists_and_is_non_empty(self):
        self.assertTrue(OPENAPI_PATH.exists(), f"OpenAPI spec missing at {OPENAPI_PATH}")
        content = OPENAPI_PATH.read_text(encoding="utf-8")
        self.assertGreater(len(content), 1000)
        self.assertIn("openapi: 3.1.0", content)
        self.assertIn("title: CropSync / Nelavi Backend API", content)
        self.assertIn("version: 2.0.0", content)

    def test_all_backend_routes_are_documented_in_openapi_spec(self):
        content = OPENAPI_PATH.read_text(encoding="utf-8")
        documented_paths = set(re.findall(r"^  (/\S*):", content, re.MULTILINE))

        expected_routes = {
            "/health",
            "/api/health",
            "/api/state",
            "/api/history",
            "/api/run",
            "/api/run-orchestrated",
            "/api/run-integrated",
            "/api/second-opinion",
            "/api/advisory",
            "/api/research-pro/start",
            "/api/research-pro/status",
            "/api/analyze",
            "/api/voice/transcribe",
            "/api/voice/synthesize",
            "/api/assistant/ask",
            "/api/learning/lessons",
            "/api/learning/lesson",
            "/api/learning/progress",
        }
        for route in expected_routes:
            self.assertIn(route, documented_paths, f"Route {route} not documented in OpenAPI spec")

    def test_ia13_findings_schema_preserves_crop_candidates_object(self):
        content = OPENAPI_PATH.read_text(encoding="utf-8")
        self.assertIn("CropFindings:", content)
        self.assertIn("CropCandidate:", content)
        self.assertIn("candidates:", content)
        self.assertIn("uncertainty:", content)


class TestFlutterFlowScreenWorkflows(OfflineTestBase):
    """Tests handler responses for FlutterFlow screens against documented schemas."""

    # Screen 1: Dashboard / Field Telemetry
    def test_screen1_dashboard_state_schema_conformance(self):
        app = self.make_app()
        resp = self.request(app, "GET", "/api/state")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        for required_key in (
            "scenarios",
            "max_image_bytes",
            "rover_id",
            "history_max",
            "second_opinion",
            "research",
            "research_pro",
            "orchestrated",
            "integrated",
            "voice_assistant",
        ):
            self.assertIn(required_key, data)
        self.assertIsInstance(data["history_max"], int)
        self.assertIsInstance(data["voice_assistant"]["available"], bool)
        self.assertIn("en", data["voice_assistant"]["supported_languages"])

    def test_screen1_dashboard_history_and_rover_record_schema(self):
        app = self.make_app()
        self.service.run_scenario("normal_operation")
        resp = self.request(app, "GET", "/api/history?limit=5")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("total", data)
        self.assertIn("records", data)
        self.assertGreaterEqual(len(data["records"]), 1)
        record_entry = data["records"][0]
        self.assertIn("id", record_entry)
        record = record_entry["record"]
        for field in ("time", "sequence_number", "rover_id", "battery", "soil", "air", "sensor_status", "versions"):
            self.assertIn(field, record)
        self.assertIn("voltage_v", record["battery"])
        self.assertIn("nitrogen", record["soil"])
        self.assertIn("moisture", record["soil"])
        self.assertIn("npk", record["sensor_status"])

    def test_screen1_dashboard_run_scenario_action(self):
        app = self.make_app()
        payload = {"scenario": "normal_operation", "crop": "Wheat", "region": "Punjab"}
        resp = self.json_post(app, "/api/run", payload)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        for k in ("stored_id", "record", "advice", "report"):
            self.assertIn(k, data)

    # Screen 2: Integrated Leaf Analysis / Multi-Modal
    def test_screen2_leaf_analysis_base64_integrated_workflow(self):
        app = self.make_app()
        payload = {
            "scenario": "normal_operation",
            "crop": "Tomato",
            "question": "how should soil moisture readings be read",
            "image_png_base64": PNG_B64,
            "providers": {"image": "fake", "research": "fake", "compute": "fake", "synthesis": "fake"},
        }
        resp = self.json_post(app, "/api/run-integrated", payload)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        for k in ("stored_id", "record", "integrated", "data_source", "label", "real_stages"):
            self.assertIn(k, data)
        self.assertIn("report", data["integrated"])
        self.assertIn("image", data["integrated"]["integration"]["stages"][0]["stage"])

    def test_screen2_second_opinion_schema_conformance(self):
        app = self.make_app()
        payload = {
            "image_png_base64": PNG_B64,
            "readings": {
                "soil": {"nitrogen": 25.0, "phosphorus": 12.0, "potassium": 80.0, "moisture": 30.0, "temperature": 22.0},
                "air": {"temperature": 24.0, "humidity": 60.0, "pressure": 1012.0},
            },
            "crop": "Tomato",
            "language": "English",
        }
        resp = self.json_post(app, "/api/second-opinion", payload)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        for k in ("opinion", "error_code", "simulated", "model", "label"):
            self.assertIn(k, data)
        self.assertIsNone(data["error_code"])
        self.assertIsInstance(data["opinion"], str)

    # Screen 3: Multilingual Voice Agronomist
    def test_screen3_voice_transcription_handler(self):
        app = self.make_app()
        audio_dummy = b"RIFF....WAVEfmt ...."
        resp = self.request(
            app,
            "POST",
            "/api/voice/transcribe?lang=en",
            body=audio_dummy,
            headers={"content-type": "audio/wav"},
        )
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("text", data)
        self.assertEqual(data["language_code"], "en")

    def test_screen3_assistant_ask_with_telemetry_fusion(self):
        app = self.make_app()
        payload = {"question": "What is the status of my soil moisture?", "language": "en"}
        resp = self.json_post(app, "/api/assistant/ask", payload)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("text", data)
        self.assertIn("advisory_warning", data)

    def test_screen3_voice_synthesize_handler(self):
        app = self.make_app()
        payload = {"text": "Soil moisture is adequate for growth.", "language": "en"}
        resp = self.json_post(app, "/api/voice/synthesize", payload)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("audio_base64", data)
        self.assertEqual(data.get("content_type"), "audio/mpeg")

    # Screen 4: Precision Agriculture Learning Academy
    def test_screen4_learning_lessons_catalog(self):
        app = self.make_app()
        for lang in ("en", "hi", "te"):
            with self.subTest(lang=lang):
                resp = self.request(app, "GET", f"/api/learning/lessons?lang={lang}")
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.body.decode("utf-8"))
                self.assertEqual(data["total"], 5)
                self.assertEqual(len(data["lessons"]), 5)
                self.assertIn("completed_count", data)
                first = data["lessons"][0]
                for k in ("id", "title", "summary", "category", "duration_minutes", "icon", "completed"):
                    self.assertIn(k, first)

    def test_screen4_learning_lesson_detail(self):
        app = self.make_app()
        resp = self.request(app, "GET", "/api/learning/lesson?id=crop_care_stages&lang=en")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        for k in ("id", "title", "summary", "category", "duration_minutes", "icon", "sections", "farmer_tips", "completed"):
            self.assertIn(k, data)
        self.assertGreaterEqual(len(data["sections"]), 2)
        self.assertIn("heading", data["sections"][0])
        self.assertIn("body", data["sections"][0])

    def test_screen4_learning_progress_update(self):
        app = self.make_app()
        payload = {"lesson_id": "crop_care_stages", "completed": True}
        resp = self.json_post(app, "/api/learning/progress", payload)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["lesson_id"], "crop_care_stages")
        self.assertTrue(data["completed"])
        self.assertEqual(data["total"], 5)


class TestRemoteSecurityAndCors(OfflineTestBase):
    """Tests Bearer token authentication and CORS preflight in remote mode."""

    def test_remote_mode_rejects_missing_bearer_token(self):
        app = self.make_app(remote=True, auth_token=TEST_AUTH_TOKEN)
        resp = self.request(app, "GET", "/api/state")
        self.assertEqual(resp.status, 401)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("error", data)
        self.assertIn("missing bearer token", data["error"].lower())

    def test_remote_mode_rejects_invalid_bearer_token(self):
        app = self.make_app(remote=True, auth_token=TEST_AUTH_TOKEN)
        headers = {"authorization": "Bearer wrong-token-xyz"}
        resp = self.request(app, "GET", "/api/state", headers=headers)
        self.assertEqual(resp.status, 401)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("invalid bearer token", data["error"].lower())
        self.assertNotIn(TEST_AUTH_TOKEN, resp.body.decode("utf-8"))

    def test_remote_mode_accepts_valid_bearer_token(self):
        app = self.make_app(remote=True, auth_token=TEST_AUTH_TOKEN)
        headers = {"authorization": f"Bearer {TEST_AUTH_TOKEN}"}
        resp = self.request(app, "GET", "/api/state", headers=headers)
        self.assertEqual(resp.status, 200)
        self.assertNotIn(TEST_AUTH_TOKEN, resp.body.decode("utf-8"))

    def test_public_health_endpoints_require_no_auth_in_remote_mode(self):
        app = self.make_app(remote=True, auth_token=TEST_AUTH_TOKEN)
        for path in ("/health", "/api/health"):
            with self.subTest(path=path):
                resp = self.request(app, "GET", path)
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.body.decode("utf-8"))
                self.assertEqual(data["status"], "ok")

    def test_cors_options_preflight_for_allowed_origin(self):
        app = self.make_app(remote=True, auth_token=TEST_AUTH_TOKEN, cors_origins=[CORS_ORIGIN])
        headers = {
            "origin": CORS_ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "Authorization, Content-Type",
        }
        resp = self.request(app, "OPTIONS", "/api/run-integrated", headers=headers)
        self.assertEqual(resp.status, 204)
        extra = dict(resp.extra_headers)
        self.assertEqual(extra.get("Access-Control-Allow-Origin"), CORS_ORIGIN)
        self.assertIn("POST", extra.get("Access-Control-Allow-Methods", ""))
        self.assertIn("Authorization", extra.get("Access-Control-Allow-Headers", ""))

    def test_cors_options_preflight_rejects_disallowed_origin(self):
        app = self.make_app(remote=True, auth_token=TEST_AUTH_TOKEN, cors_origins=[CORS_ORIGIN])
        headers = {"origin": "https://malicious-site.example"}
        resp = self.request(app, "OPTIONS", "/api/run-integrated", headers=headers)
        self.assertEqual(resp.status, 403)


class TestPayloadBoundsAndErrorResponses(OfflineTestBase):
    """Tests bad payloads, content-type checks, and size boundaries."""

    def test_malformed_json_returns_400(self):
        app = self.make_app()
        resp = self.request(
            app, "POST", "/api/run", body=b"not-json-content", headers={"content-type": "application/json"}
        )
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("must be JSON", data["error"])

    def test_missing_required_scenario_returns_400(self):
        app = self.make_app()
        resp = self.json_post(app, "/api/run", {"crop": "Wheat"})
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("scenario", data["error"])

    def test_unsupported_media_type_returns_415(self):
        app = self.make_app()
        resp = self.request(
            app,
            "POST",
            "/api/run-integrated",
            body=b"{}",
            headers={"content-type": "text/plain"},
        )
        self.assertEqual(resp.status, 415)

    def test_oversized_payload_returns_413(self):
        app = self.make_app()
        oversized = b"x" * (MAX_SECOND_OPINION_BYTES + 1)
        resp = self.request(
            app,
            "POST",
            "/api/run-integrated",
            body=oversized,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(resp.status, 413)

    def test_invalid_base64_in_image_returns_400(self):
        app = self.make_app()
        payload = {
            "scenario": "normal_operation",
            "image_png_base64": "not-valid-base64!!!",
            "providers": {"image": "fake"},
        }
        resp = self.json_post(app, "/api/run-integrated", payload)
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("base64", data["error"])


if __name__ == "__main__":
    unittest.main()
