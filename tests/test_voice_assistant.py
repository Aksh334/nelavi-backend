"""Offline unit tests for the CropSync Voice & Multilingual Learning Assistant.
Includes socket, DNS, and urllib tripwires to guarantee no live external requests occur.
"""

import json
import unittest
from unittest import mock

from voice_assistant.boundary import (
    ERROR_AUDIO_EMPTY,
    ERROR_AUDIO_TOO_LARGE,
    ERROR_AUDIO_UNSUPPORTED,
    ERROR_CREDENTIAL_MISSING,
    ERROR_EMPTY_TRANSCRIPT,
    ERROR_PROVIDER_REJECTED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_TIMEOUT,
    LANG_EN,
    LANG_HI,
    LANG_TE,
    AssistantQuestion,
    SpeechRequest,
    TranscriptionRequest,
)
from voice_assistant.elevenlabs import (
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_VOICE_ID,
    ElevenLabsClient,
)
from voice_assistant.assistant import CropSyncAssistant
from voice_assistant.lessons import get_lesson_detail, get_lessons_catalog, LESSONS


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch("socket.socket", side_effect=AssertionError("socket used")),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class TestElevenLabsSTT(NoNetwork):
    def test_missing_credentials_fails_closed(self):
        client = ElevenLabsClient(key_env_var="UNSET_ELEVENLABS_KEY")
        with mock.patch.dict("os.environ", {}, clear=True):
            result = client.transcribe(TranscriptionRequest(b"dummy audio", "audio/webm"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_CREDENTIAL_MISSING)

    def test_empty_audio_is_rejected(self):
        client = ElevenLabsClient(api_key="fake-key")
        result = client.transcribe(TranscriptionRequest(b"", "audio/webm"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_AUDIO_EMPTY)

    def test_oversized_audio_is_rejected_before_network(self):
        client = ElevenLabsClient(api_key="fake-key")
        result = client.transcribe(TranscriptionRequest(b"x" * (10 * 1024 * 1024 + 1), "audio/webm"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_AUDIO_TOO_LARGE)

    def test_unsupported_audio_media_type_is_rejected(self):
        client = ElevenLabsClient(api_key="fake-key")
        result = client.transcribe(TranscriptionRequest(b"bytes", "image/png"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_AUDIO_UNSUPPORTED)

    def test_successful_transcription_returns_text(self):
        def fake_transport(url, headers, body, timeout):
            self.assertEqual(headers["xi-api-key"], "fake-key")
            self.assertIn("multipart/form-data", headers["Content-Type"])
            self.assertIn(DEFAULT_STT_MODEL.encode(), body)
            return 200, json.dumps({"text": "How do I manage soil moisture?", "language_code": "en"}).encode("utf-8")

        client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
        result = client.transcribe(TranscriptionRequest(b"audio-bytes", "audio/webm", language_code="en"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "How do I manage soil moisture?")
        self.assertEqual(result.language_code, "en")

    def test_empty_model_transcript_is_handled(self):
        def fake_transport(url, headers, body, timeout):
            return 200, json.dumps({"text": "   "}).encode("utf-8")

        client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
        result = client.transcribe(TranscriptionRequest(b"audio-bytes", "audio/wav"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_EMPTY_TRANSCRIPT)

    def test_provider_http_errors_map_to_closed_codes(self):
        for status_code, expected_err in ((401, ERROR_PROVIDER_REJECTED), (429, ERROR_PROVIDER_UNAVAILABLE), (503, ERROR_PROVIDER_UNAVAILABLE)):
            def fake_transport(url, headers, body, timeout):
                return status_code, b"error"

            client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
            result = client.transcribe(TranscriptionRequest(b"audio-bytes", "audio/mp3"))
            self.assertEqual(result.status, "error")
            self.assertEqual(result.error_code, expected_err)


class TestElevenLabsTTS(NoNetwork):
    def test_missing_credentials_fails_closed(self):
        client = ElevenLabsClient(key_env_var="UNSET_ELEVENLABS_KEY")
        with mock.patch.dict("os.environ", {}, clear=True):
            result = client.synthesize(SpeechRequest("Hello world"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_CREDENTIAL_MISSING)

    def test_empty_text_is_rejected(self):
        client = ElevenLabsClient(api_key="fake-key")
        result = client.synthesize(SpeechRequest(""))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, ERROR_AUDIO_EMPTY)

    def test_successful_speech_synthesis_returns_audio_bytes(self):
        sent_payload = []

        def fake_transport(url, headers, body, timeout):
            self.assertEqual(headers["xi-api-key"], "fake-key")
            self.assertEqual(headers["Accept"], "audio/mpeg")
            self.assertIn(DEFAULT_VOICE_ID, url)
            sent_payload.append(json.loads(body.decode("utf-8")))
            return 200, b"ID3" + b"\x00" * 200  # synthetic MP3 bytes

        client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
        result = client.synthesize(SpeechRequest("Crop care guidance.", language_code="en"))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.audio_bytes.startswith(b"ID3"))
        self.assertEqual(result.content_type, "audio/mpeg")
        self.assertEqual(sent_payload[0]["model_id"], DEFAULT_TTS_MODEL)
        self.assertEqual(sent_payload[0]["text"], "Crop care guidance.")

    def test_long_text_is_bounded(self):
        sent_payload = []

        def fake_transport(url, headers, body, timeout):
            sent_payload.append(json.loads(body.decode("utf-8")))
            return 200, b"ID3" + b"\x00" * 150

        client = ElevenLabsClient(api_key="fake-key", transport=fake_transport)
        result = client.synthesize(SpeechRequest("A" * 3000))
        self.assertEqual(result.status, "ok")
        self.assertLessEqual(len(sent_payload[0]["text"]), 2000)


class TestAgriculturalLessons(unittest.TestCase):
    def test_five_lessons_exist_in_catalog(self):
        catalog = get_lessons_catalog()
        self.assertEqual(len(catalog), 5)
        ids = [item["id"] for item in catalog]
        self.assertEqual(ids, [
            "crop_care_stages",
            "soil_health_testing",
            "irrigation_water_mgmt",
            "nutrient_fertilizer_mgmt",
            "rover_telemetry_sensing",
        ])

    def test_multilingual_catalog_titles(self):
        en_catalog = get_lessons_catalog(LANG_EN)
        hi_catalog = get_lessons_catalog(LANG_HI)
        te_catalog = get_lessons_catalog(LANG_TE)
        self.assertEqual(en_catalog[0]["title"], "Crop Care & Key Growth Stages")
        self.assertIn("फसल देखभाल", hi_catalog[0]["title"])
        self.assertIn("పంట సంరక్షణ", te_catalog[0]["title"])

    def test_lesson_detail_structure_and_fallback(self):
        detail = get_lesson_detail("soil_health_testing", LANG_HI)
        self.assertIsNotNone(detail)
        self.assertIn("मिट्टी का स्वास्थ्य", detail["title"])
        self.assertGreaterEqual(len(detail["sections"]), 3)
        self.assertGreaterEqual(len(detail["farmer_tips"]), 2)

        # Non-existent lesson returns None
        self.assertIsNone(get_lesson_detail("non_existent"))


class TestAgriculturalAssistant(unittest.TestCase):
    def test_empty_question_is_handled(self):
        assistant = CropSyncAssistant()
        ans = assistant.ask(AssistantQuestion(""))
        self.assertEqual(ans.status, "error")

    def test_question_answering_without_rover_data(self):
        assistant = CropSyncAssistant()
        ans = assistant.ask(AssistantQuestion("How should I manage soil moisture in dry soil?", language=LANG_EN, include_rover_data=False))
        self.assertEqual(ans.status, "ok")
        self.assertEqual(ans.language, LANG_EN)
        self.assertIn("Irrigation & Soil Moisture", ans.text)
        self.assertIn("Disclaimer:", ans.advisory_warning)
        self.assertIsNone(ans.rover_context)

    def test_question_answering_with_rover_telemetry(self):
        class FakeRecordStore:
            def history(self, limit=1):
                return [{
                    "sequence_number": 42,
                    "time": "sim:42",
                    "soil": {"nitrogen": 14.5, "phosphorus": 22.0, "potassium": 180.0, "moisture": 18.2, "temperature": 24.5},
                    "air": {"temperature": 28.0, "humidity": 45.0},
                    "sensor_status": {"npk": "ok", "moisture": "ok"},
                    "battery": {"voltage_v": 12.4},
                }]

        assistant = CropSyncAssistant(record_store=FakeRecordStore())
        ans = assistant.ask(AssistantQuestion("When should I irrigate my crops?", language=LANG_EN))
        self.assertEqual(ans.status, "ok")
        self.assertIn("Current CropSync Rover Telemetry:", ans.text)
        self.assertIn("Soil Moisture: 18.2% (low - irrigation advised)", ans.text)
        self.assertIn("Soil NPK: N=14.5, P=22.0, K=180.0 mg/kg", ans.text)
        self.assertIn("Moisture Alert:", ans.text)

    def test_multilingual_answers_in_hindi_and_telugu(self):
        class FakeRecordStore:
            def history(self, limit=1):
                return [{
                    "sequence_number": 10,
                    "time": "sim:10",
                    "soil": {"nitrogen": 25.0, "phosphorus": 30.0, "potassium": 150.0, "moisture": 45.0, "temperature": 22.0},
                    "air": {"temperature": 25.0, "humidity": 55.0},
                    "sensor_status": {"npk": "ok", "moisture": "ok"},
                    "battery": {"voltage_v": 12.6},
                }]

        assistant = CropSyncAssistant(record_store=FakeRecordStore())

        # Hindi question
        hi_ans = assistant.ask(AssistantQuestion("यूरिया और नाइट्रोजन की कमी के क्या लक्षण हैं?", language=LANG_HI))
        self.assertEqual(hi_ans.status, "ok")
        self.assertEqual(hi_ans.language, LANG_HI)
        self.assertIn("नाइट्रोजन", hi_ans.text)
        self.assertIn("अस्वीकरण:", hi_ans.advisory_warning)

        # Telugu question
        te_ans = assistant.ask(AssistantQuestion("నేలలో తేమ మరియు నీటి యాజమాన్యం ఎలా చేయాలి?", language=LANG_TE))
        self.assertEqual(te_ans.status, "ok")
        self.assertEqual(te_ans.language, LANG_TE)
        self.assertIn("నీటి యాజమాన్యం", te_ans.text)
        self.assertIn("నిరాకరణ:", te_ans.advisory_warning)
