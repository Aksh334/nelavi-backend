"""Tests for the Voice & Multilingual Learning Assistant HTTP routes in DemoApp.
Tests local mode (CSRF header), remote mode (Bearer token), bounds, and tripwires.
Zero network access.
"""

import json
import unittest
from unittest import mock

from demo_app.app import DemoApp
from demo_app.service import DemoService
from storage.record_store import RecordStore
from voice_assistant.boundary import (
    LANG_EN,
    LANG_HI,
    LANG_TE,
    TranscriptionRequest,
)
from voice_assistant.elevenlabs import ElevenLabsClient


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch("socket.socket", side_effect=AssertionError("socket used")),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class FakeStore:
    def history(self, limit=20):
        return [{
            "sequence_number": 1,
            "time": "sim:1",
            "soil": {"nitrogen": 18.0, "phosphorus": 25.0, "potassium": 160.0, "moisture": 42.0, "temperature": 23.0},
            "air": {"temperature": 26.0, "humidity": 50.0},
            "sensor_status": {"npk": "ok", "moisture": "ok"},
            "battery": {"voltage_v": 12.5},
        }]


class TestVoiceAssistantRoutes(NoNetwork):
    def setUp(self):
        super().setUp()
        self.store = FakeStore()
        self.service = DemoService(self.store)
        self.app = DemoApp(self.service, allowed_hosts=["127.0.0.1:8765", "localhost:8765"])

    def request(self, method, path, body=b"", headers=None):
        hdrs = {
            "host": "127.0.0.1:8765",
            "content-length": str(len(body)),
            "x-nelavi-demo": "1",
        }
        if headers:
            hdrs.update(headers)
        return self.app.handle_request(method, path, hdrs, lambda n: body[:n])

    # ------------------------------------------------ Learning Lessons Tests
    def test_get_lessons_catalog_returns_five_lessons(self):
        resp = self.request("GET", "/api/learning/lessons")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["lessons"]), 5)
        self.assertEqual(data["completed_count"], 0)

    def test_get_lessons_in_hindi_and_telugu(self):
        resp_hi = self.request("GET", "/api/learning/lessons?lang=hi")
        self.assertEqual(resp_hi.status, 200)
        data_hi = json.loads(resp_hi.body.decode("utf-8"))
        self.assertIn("फसल देखभाल", data_hi["lessons"][0]["title"])

        resp_te = self.request("GET", "/api/learning/lessons?lang=te")
        self.assertEqual(resp_te.status, 200)
        data_te = json.loads(resp_te.body.decode("utf-8"))
        self.assertIn("పంట సంరక్షణ", data_te["lessons"][0]["title"])

    def test_get_lesson_detail(self):
        resp = self.request("GET", "/api/learning/lesson?id=soil_health_testing&lang=en")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["id"], "soil_health_testing")
        self.assertIn("Soil Health", data["title"])
        self.assertGreaterEqual(len(data["sections"]), 2)

    def test_get_lesson_missing_id_returns_400(self):
        resp = self.request("GET", "/api/learning/lesson")
        self.assertEqual(resp.status, 400)

    def test_get_lesson_not_found_returns_404(self):
        resp = self.request("GET", "/api/learning/lesson?id=non_existent_lesson")
        self.assertEqual(resp.status, 404)

    def test_update_lesson_progress(self):
        payload = json.dumps({"lesson_id": "crop_care_stages", "completed": True}).encode("utf-8")
        resp = self.request("POST", "/api/learning/progress", body=payload,
                            headers={"content-type": "application/json"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(data["completed"])
        self.assertEqual(data["completed_count"], 1)

        # Verify catalog reflects completed status
        cat_resp = self.request("GET", "/api/learning/lessons")
        cat_data = json.loads(cat_resp.body.decode("utf-8"))
        self.assertEqual(cat_data["completed_count"], 1)
        completed_item = [item for item in cat_data["lessons"] if item["id"] == "crop_care_stages"][0]
        self.assertTrue(completed_item["completed"])

    # --------------------------------------------- Assistant Answering Tests
    def test_assistant_ask_requires_csrf_header_in_local_mode(self):
        payload = json.dumps({"question": "How to manage soil?"}).encode("utf-8")
        resp = self.app.handle_request("POST", "/api/assistant/ask",
                                       {"host": "127.0.0.1:8765", "content-length": str(len(payload))},
                                       lambda n: payload[:n])
        self.assertEqual(resp.status, 403)

    def test_assistant_ask_success_with_telemetry(self):
        payload = json.dumps({
            "question": "What is the status of my soil moisture?",
            "language": LANG_EN,
            "include_rover_data": True,
        }).encode("utf-8")
        resp = self.request("POST", "/api/assistant/ask", body=payload,
                            headers={"content-type": "application/json"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("Current CropSync Rover Telemetry:", data["text"])
        self.assertIn("Soil Moisture: 42.0%", data["text"])
        self.assertIn("Disclaimer:", data["advisory_warning"])
        self.assertIsNotNone(data["rover_context"])

    def test_assistant_ask_in_hindi(self):
        payload = json.dumps({
            "question": "मिट्टी की नमी कैसे जांचें?",
            "language": LANG_HI,
        }).encode("utf-8")
        resp = self.request("POST", "/api/assistant/ask", body=payload,
                            headers={"content-type": "application/json"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["language"], LANG_HI)
        self.assertIn("अस्वीकरण:", data["advisory_warning"])

    def test_assistant_ask_empty_question_returns_400(self):
        payload = json.dumps({"question": ""}).encode("utf-8")
        resp = self.request("POST", "/api/assistant/ask", body=payload,
                            headers={"content-type": "application/json"})
        self.assertEqual(resp.status, 400)

    # ------------------------------------------------- Voice Speech Endpoints
    def test_transcribe_when_elevenlabs_not_configured_returns_friendly_error(self):
        # Service without ElevenLabs configured
        body = b"fake-audio-bytes"
        resp = self.request("POST", "/api/voice/transcribe", body=body,
                            headers={"content-type": "audio/webm"})
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["error_code"], "credential_missing")
        self.assertIn("ElevenLabs is not configured", data["message"])

    def test_transcribe_with_configured_elevenlabs_client(self):
        def fake_transport(url, headers, body, timeout):
            return 200, json.dumps({"text": "How much water does corn need?", "language_code": "en"}).encode("utf-8")

        client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
        service = DemoService(self.store, elevenlabs_client=client)
        app = DemoApp(service, allowed_hosts=["127.0.0.1:8765"])

        body = b"valid-audio-bytes"
        hdrs = {
            "host": "127.0.0.1:8765",
            "content-length": str(len(body)),
            "content-type": "audio/webm",
            "x-nelavi-demo": "1",
        }
        resp = app.handle_request("POST", "/api/voice/transcribe", hdrs, lambda n: body[:n])
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["text"], "How much water does corn need?")

    def test_synthesize_when_elevenlabs_not_configured_returns_friendly_error(self):
        payload = json.dumps({"text": "Irrigate lightly."}).encode("utf-8")
        resp = self.request("POST", "/api/voice/synthesize", body=payload,
                            headers={"content-type": "application/json"})
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["error_code"], "credential_missing")

    def test_synthesize_with_configured_elevenlabs_client(self):
        def fake_transport(url, headers, body, timeout):
            return 200, b"ID3" + b"\x00" * 150

        client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
        service = DemoService(self.store, elevenlabs_client=client)
        app = DemoApp(service, allowed_hosts=["127.0.0.1:8765"])

        payload = json.dumps({"text": "Answer audio test."}).encode("utf-8")
        hdrs = {
            "host": "127.0.0.1:8765",
            "content-length": str(len(payload)),
            "content-type": "application/json",
            "x-nelavi-demo": "1",
        }
        resp = app.handle_request("POST", "/api/voice/synthesize", hdrs, lambda n: payload[:n])
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertIn("audio_base64", data)
        self.assertEqual(data["content_type"], "audio/mpeg")

    # ---------------------------------------------------- Remote Mode Security
    def test_remote_mode_requires_bearer_auth(self):
        remote_app = DemoApp(
            self.service,
            allowed_hosts=["api.example.com"],
            remote=True,
            auth_token="secret-token-12345",
            cors_origins=["https://flutterflow.io"],
        )
        payload = json.dumps({"question": "Test query"}).encode("utf-8")

        # No auth header -> 401
        hdrs = {"host": "api.example.com", "content-length": str(len(payload)), "content-type": "application/json"}
        resp = remote_app.handle_request("POST", "/api/assistant/ask", hdrs, lambda n: payload[:n])
        self.assertEqual(resp.status, 401)

        # Valid bearer token -> 200
        hdrs["authorization"] = "Bearer secret-token-12345"
        resp = remote_app.handle_request("POST", "/api/assistant/ask", hdrs, lambda n: payload[:n])
        self.assertEqual(resp.status, 200)
