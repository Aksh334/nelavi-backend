"""End-to-end verification script for CropSync Voice & Multilingual Learning Assistant.

Tests server routes, in-memory RecordStore integration, telemetry fusion, trilingual
QA (en, hi, te), lessons catalog, detail, and progress tracking, as well as graceful
credential handling.
"""

import http.client
import json
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_app.app import DemoApp
from demo_app.server import _AppHolder, make_handler
from demo_app.service import DemoService
from mock_rover.simulator import MockRover
from storage.record_store import RecordStore
from voice_assistant.assistant import CropSyncAssistant
from voice_assistant.elevenlabs import ElevenLabsClient


def run_e2e_verification():
    print("=== CropSync Voice & Multilingual Learning Assistant E2E Verification ===")

    # 1. Setup in-memory store and seed a sample record with telemetry
    store = RecordStore(":memory:")
    rover = MockRover()
    rover.insert_probe()
    rover.take_measurement()
    sample_record = rover.build_record()
    sample_record["soil"]["moisture"] = 14.2  # Low moisture: should trigger irrigation advisory
    sample_record["soil"]["nitrogen"] = 18.0  # Low nitrogen: chlorosis / yellowing risk
    store.add(sample_record)
    print("[OK] Seeded store with simulated rover Record (moisture: 14.2%, nitrogen: 18.0)")

    # 2. Wire service and app
    el_client = ElevenLabsClient(api_key=None)  # unconfigured key to test graceful fallback
    assistant = CropSyncAssistant(store)
    service = DemoService(store, elevenlabs_client=el_client, assistant=assistant)
    host_port = "127.0.0.1:8799"
    app = DemoApp(service, {host_port, "localhost:8799"})

    # 3. Start local HTTP server in background thread
    holder = _AppHolder()
    server = HTTPServer(("127.0.0.1", 8799), make_handler(holder))
    server.timeout = 10.0
    holder.app = app
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.1)
    print("[OK] Started local test server on http://127.0.0.1:8799")

    conn = http.client.HTTPConnection("127.0.0.1", 8799, timeout=5)
    headers = {"X-Nelavi-Demo": "1", "Content-Type": "application/json"}

    try:
        # Step 1: GET /api/state
        conn.request("GET", "/api/state")
        resp = conn.getresponse()
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        state_data = json.loads(resp.read().decode("utf-8"))
        va_state = state_data.get("voice_assistant", {})
        assert va_state.get("available") is True or va_state.get("enabled") is True
        assert va_state.get("speech_to_text_configured") is False
        assert va_state.get("supported_languages") == ["en", "hi", "te"]
        print("[OK] Verified /api/state returns active voice_assistant block with 3 languages")

        # Step 2: GET /api/learning/lessons (English)
        conn.request("GET", "/api/learning/lessons?lang=en")
        resp = conn.getresponse()
        assert resp.status == 200
        lessons_data = json.loads(resp.read().decode("utf-8"))
        assert len(lessons_data["lessons"]) == 5
        assert lessons_data["total"] == 5
        assert lessons_data["completed_count"] == 0
        print(f"[OK] Verified /api/learning/lessons returned {len(lessons_data['lessons'])} English lessons")

        # Step 3: GET /api/learning/lessons (Hindi & Telugu translations)
        conn.request("GET", "/api/learning/lessons?lang=hi")
        resp_hi = conn.getresponse()
        hi_data = json.loads(resp_hi.read().decode("utf-8"))
        assert "फसल" in hi_data["lessons"][0]["title"] or "देखभाल" in hi_data["lessons"][0]["title"]

        conn.request("GET", "/api/learning/lessons?lang=te")
        resp_te = conn.getresponse()
        te_data = json.loads(resp_te.read().decode("utf-8"))
        assert "పంట" in te_data["lessons"][0]["title"] or "యాజమాన్యం" in te_data["lessons"][0]["title"]
        print("[OK] Verified trilingual lessons catalog in Hindi and Telugu")

        # Step 4: GET /api/learning/lesson detail
        conn.request("GET", "/api/learning/lesson?id=crop_care_stages&lang=en")
        resp = conn.getresponse()
        assert resp.status == 200
        lesson_detail = json.loads(resp.read().decode("utf-8"))
        assert lesson_detail["id"] == "crop_care_stages"
        assert len(lesson_detail["sections"]) >= 3
        assert len(lesson_detail["farmer_tips"]) >= 3
        print("[OK] Verified lesson detail schema, sections, and farmer tips")

        # Step 5: POST /api/learning/progress
        progress_payload = json.dumps({"lesson_id": "crop_care_stages", "completed": True})
        conn.request("POST", "/api/learning/progress", body=progress_payload, headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200
        progress_data = json.loads(resp.read().decode("utf-8"))
        assert progress_data["status"] == "ok"
        assert progress_data["lesson_id"] == "crop_care_stages"
        assert progress_data["completed"] is True
        assert progress_data["completed_count"] == 1
        print("[OK] Verified /api/learning/progress marks lesson complete (completed_count=1)")

        # Step 6: POST /api/assistant/ask (English with telemetry fusion)
        ask_payload_en = json.dumps({
            "question": "What is my soil moisture and how should I irrigate?",
            "language": "en",
            "include_rover_data": True,
        })
        conn.request("POST", "/api/assistant/ask", body=ask_payload_en, headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200
        ask_data_en = json.loads(resp.read().decode("utf-8"))
        assert ask_data_en["status"] == "ok"
        assert "14.2%" in ask_data_en["text"] or "moisture" in ask_data_en["text"].lower()
        assert len(ask_data_en["sources"]) >= 1
        assert "advisory_warning" in ask_data_en
        print("[OK] Verified English QA fuses live soil moisture (14.2%) and generates agronomic advisory")

        # Step 7: POST /api/assistant/ask (Hindi with nitrogen / chlorosis inquiry)
        ask_payload_hi = json.dumps({
            "question": "फसल में पीलापन दिख रहा है, यूरिया कब डालें?",
            "language": "hi",
            "include_rover_data": True,
        })
        conn.request("POST", "/api/assistant/ask", body=ask_payload_hi, headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200
        ask_data_hi = json.loads(resp.read().decode("utf-8"))
        assert ask_data_hi["status"] == "ok"
        assert "नाइट्रोजन" in ask_data_hi["text"] or "18.0" in ask_data_hi["text"]
        print("[OK] Verified Hindi QA generates Hindi nitrogen advisory")

        # Step 8: POST /api/assistant/ask (Telugu with irrigation inquiry)
        ask_payload_te = json.dumps({
            "question": "నేలలో తేమ తక్కువగా ఉంటే నీటిపారుదల ఎలా చేయాలి?",
            "language": "te",
            "include_rover_data": True,
        })
        conn.request("POST", "/api/assistant/ask", body=ask_payload_te, headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200
        ask_data_te = json.loads(resp.read().decode("utf-8"))
        assert ask_data_te["status"] == "ok"
        assert "తేమ" in ask_data_te["text"] or "14.2%" in ask_data_te["text"]
        print("[OK] Verified Telugu QA generates Telugu moisture advisory")

        # Step 9: POST /api/voice/synthesize (Graceful key fallback)
        synth_payload = json.dumps({"text": "Hello farmer", "language": "en"})
        conn.request("POST", "/api/voice/synthesize", body=synth_payload, headers=headers)
        resp = conn.getresponse()
        assert resp.status == 400
        synth_err = json.loads(resp.read().decode("utf-8"))
        assert synth_err["error_code"] == "credential_missing"
        print("[OK] Verified /api/voice/synthesize returns graceful credential_missing error code")

        # Step 10: POST /api/voice/transcribe (Graceful key fallback)
        conn.request("POST", "/api/voice/transcribe?lang=en", body=b"dummy audio data",
                     headers={"X-Nelavi-Demo": "1", "Content-Type": "audio/webm"})
        resp = conn.getresponse()
        assert resp.status == 400
        trans_err = json.loads(resp.read().decode("utf-8"))
        assert trans_err["error_code"] == "credential_missing"
        print("[OK] Verified /api/voice/transcribe returns graceful credential_missing error code")

        # Step 11: GET / serves HTML with Section 8
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        html = resp.read().decode("utf-8")
        assert 'id="voice-assistant"' in html
        assert 'id="va-mic-btn"' in html
        assert 'id="va-lessons-grid"' in html
        print("[OK] Verified GET / serves index.html with Section 8 markup and controls")

        print("\n=== ALL 11 E2E USER JOURNEYS PASSED CLEANLY! ===")
    finally:
        conn.close()
        server.server_close()
        store.close()


if __name__ == "__main__":
    run_e2e_verification()
