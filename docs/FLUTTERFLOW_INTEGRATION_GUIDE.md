# CropSync / Nelavi — FlutterFlow Client Integration Guide

## 1. Overview & Architectural Boundary

This guide provides the official implementation specification for integrating **FlutterFlow** (web and mobile clients) with the **CropSync / Nelavi** backend.

The backend exposes an OpenAPI 3.1 RESTful interface that services four primary operational views:
1. **Screen 1: Field Overview & Telemetry Dashboard** (in-field sensor telemetry, rover scenario execution, historical trends).
2. **Screen 2: Multi-Modal Leaf Analysis & Diagnostics** (base64 leaf photo upload, AI visual inspection via NVIDIA, official agronomic research via Tavily, and LLM agronomic synthesis via OpenRouter).
3. **Screen 3: Multilingual Voice Agronomist** (farmer speech-to-text, agronomic QA fusing live in-field rover readings, and audio synthesis in English, Hindi, and Telugu via ElevenLabs).
4. **Screen 4: Precision Agriculture Learning Academy** (trilingual curriculum modules, multi-stage interactive breakdowns, farmer tips, and progress tracking).

### Machine-Readable API Contract
The authoritative OpenAPI 3.1 specification is located at:
[`docs/contracts/OPENAPI_SPEC.yaml`](file:///c:/Users/Akshaj%20dasam/OneDrive/Documents/CROPSYNC/docs/contracts/OPENAPI_SPEC.yaml)

You can import this file directly into FlutterFlow under **API Calls -> Import from OpenAPI / Swagger**.

---

## 2. Backend Server Modes & Network Setup

The backend operates in two distinct operational modes:

### Pure Local Development Mode (Loopback)
- **Host**: `http://127.0.0.1:8765`
- **Authentication**: Unauthenticated for local testing (requires header `X-Nelavi-Demo: 1` on POST requests).
- **CORS Behavior**: **Disabled.** Preflight `OPTIONS` requests return HTTP 405 Method Not Allowed. The server strictly forbids `--cors-origins` in local mode without `--remote`.
- **Intended Client**: Direct loopback scripts, curl, Postman, or native mobile/desktop test runs that do not issue browser CORS preflights.
- **Startup Command**:
  ```powershell
  python -B -m demo_app.server --port 8765
  ```

### Remote / FlutterFlow Web Preview Mode (Local or Gateway)
- **Host**: Local loopback (`http://127.0.0.1:8765`) or remote domain (e.g. `https://api.cropsync.example.com`).
- **Authentication**: Strict Bearer token authentication required on all `/api/*` endpoints (`Authorization: Bearer <token>`).
- **CORS Behavior**: **Enabled.** Preflight `OPTIONS` requests return HTTP 204 No Content for origins listed in `--cors-origins`. `--cors-origins` requires `--remote` and `--allowed-hosts`.
- **Intended Client**: FlutterFlow Test Mode / Web Preview running in a browser (`https://app.flutterflow.io`), web deployments, or remote mobile clients.
- **Local Testing Startup Command (with FlutterFlow Web CORS)**:
  ```powershell
  $env:NELAVI_AUTH_TOKEN = "local-dev-bearer-token"
  python -B -m demo_app.server --remote --host 127.0.0.1 --port 8765 --allowed-hosts "127.0.0.1,localhost,127.0.0.1:8765" --cors-origins "https://app.flutterflow.io,https://cropsync.flutterflow.app"
  ```
- **Production Gateway Startup Command**:
  ```powershell
  $env:NELAVI_AUTH_TOKEN = "your-secure-shared-bearer-token"
  python -B -m demo_app.server --remote --host 0.0.0.0 --port 8443 --allowed-hosts "api.cropsync.example.com" --cors-origins "https://app.flutterflow.io,https://cropsync.flutterflow.app" --allow-remote-real-providers
  ```

### Health Check Endpoints
Public probes that do not require authentication or consume API credits:
- `GET /health` or `GET /api/health` -> returns `{"status": "ok"}` with HTTP 200.

---

## 3. Authentication & Security Configuration

### Setting Up Headers in FlutterFlow
In FlutterFlow, define the following default API headers for the CropSync API Group:

| Header Name | Value / Variable | Required In |
|---|---|---|
| `Content-Type` | `application/json` (or `audio/webm`, `audio/wav` for transcription) | All POST requests |
| `Authorization` | `Bearer [auth_token]` | Remote Mode (`--remote`) |
| `Accept` | `application/json` | All requests |

### Security Warning for Mobile and Web Applications
> [!WARNING]
> In production FlutterFlow applications (especially Web and public Android/iOS builds), static bearer tokens embedded in client application code can be reverse-engineered or inspected via browser developer tools.
> For production deployments, deploy an edge API gateway (e.g., Firebase Authentication Token Exchanger, Cloudflare Worker, or AWS API Gateway) that validates farmer identity before proxying requests with the backend Bearer token.

---

## 4. Screen-by-Screen Integration Contracts

### Screen 1: Field Overview & Telemetry Dashboard

#### 1. Fetch Server State & Capability Flags
- **Method**: `GET`
- **Path**: `/api/state`
- **FlutterFlow Action**: Page load query.
- **Example Response**:
  ```json
  {
    "rover_id": "nelavi-mock-1",
    "scenarios": [
      {"name": "normal_operation", "description": "Drive forward, stop, insert the probe, measure..."},
      {"name": "low_battery", "description": "Rover stops immediately when battery drops below 9.9 V."}
    ],
    "history_max": 100,
    "max_image_bytes": 1048576,
    "voice_assistant": {
      "supported_languages": ["en", "hi", "te"],
      "elevenlabs_configured": true,
      "lessons_count": 5
    }
  }
  ```

#### 2. Fetch Rover Telemetry History
- **Method**: `GET`
- **Path**: `/api/history?limit=20`
- **FlutterFlow Action**: Pull-to-refresh or chart data binding.
- **Example Response**:
  ```json
  {
    "count": 1,
    "records": [
      {
        "id": 1,
        "record": {
          "time": "sim:24.667",
          "sequence_number": 0,
          "rover_id": "nelavi-mock-1",
          "battery": {"voltage_v": 12.59},
          "soil": {
            "nitrogen": 51.31,
            "phosphorus": 22.10,
            "potassium": 166.46,
            "moisture": 32.25,
            "temperature": 22.76
          },
          "air": {
            "temperature": 24.58,
            "humidity": 58.07,
            "pressure": 1013.76
          },
          "sensor_status": {
            "npk": "ok",
            "moisture": "ok",
            "soil_temperature": "ok",
            "air": "ok",
            "battery": "ok"
          }
        }
      }
    ]
  }
  ```

#### 3. Run In-Field Sampling Scenario
- **Method**: `POST`
- **Path**: `/api/run`
- **Body**:
  ```json
  {
    "scenario": "normal_operation",
    "crop": "Tomato",
    "region": "Punjab"
  }
  ```

---

### Screen 2: Multi-Modal Leaf Analysis & Diagnostics

#### 1. Execute Multi-Modal Integrated Workflow
Runs NVIDIA vision analysis on leaf photo, Tavily research on crop symptoms, compute aggregation, and OpenRouter agronomic synthesis in a single coordinated pipeline.
- **Method**: `POST`
- **Path**: `/api/run-integrated`
- **Request Payload**:
  ```json
  {
    "scenario": "normal_operation",
    "crop": "Tomato",
    "region": "Karnataka",
    "question": "Assess leaves for fungal spots and advise on irrigation",
    "image_png_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    "providers": {
      "image": "fake",
      "research": "fake",
      "compute": "fake",
      "synthesis": "fake"
    }
  }
  ```
  *(In real production runs, set providers to `"real"` if authorized on backend)*.

- **Response Payload Mapping**:
  - `integrated.report.context.crop.candidates`: Array of detected crop candidate objects with uncertainty levels.
  - `integrated.report.context.symptoms`: Array of symptom observations.
  - `integrated.synthesis.statements`: Array of synthesized advice statements citing specific evidence keys.
  - `integrated.integration.stages`: Pipeline execution stages and status.

#### 2. Standalone Second Opinion
- **Method**: `POST`
- **Path**: `/api/second-opinion`
- **Body**:
  ```json
  {
    "image_png_base64": "...",
    "readings": {
      "soil": {"nitrogen": 45.0, "phosphorus": 20.0, "potassium": 150.0, "moisture": 30.0, "temperature": 23.0},
      "air": {"temperature": 25.0, "humidity": 60.0, "pressure": 1012.0}
    },
    "crop": "Tomato",
    "language": "English"
  }
  ```

---

### Screen 3: Multilingual Voice Agronomist

#### 1. Transcribe Farmer Audio (STT)
- **Method**: `POST`
- **Path**: `/api/voice/transcribe?lang=en`
- **Headers**: `Content-Type: audio/webm` (or `audio/wav`, `audio/mpeg`)
- **Body**: Raw audio bytes from FlutterFlow audio recorder widget.
- **Response**:
  ```json
  {
    "status": "ok",
    "text": "How much irrigation does my wheat field need today?",
    "language_code": "en",
    "error_code": null,
    "message": null
  }
  ```

#### 2. Agronomic Question Answering (Telemetry Fusion)
- **Method**: `POST`
- **Path**: `/api/assistant/ask`
- **Body**:
  ```json
  {
    "question": "How much irrigation does my wheat field need today?",
    "language": "en",
    "include_rover_data": true
  }
  ```
- **Response**:
  ```json
  {
    "status": "ok",
    "text": "Based on the latest rover readings, soil moisture is at 32.2%, which is optimal for vegetative wheat. No irrigation is needed today.",
    "language": "en",
    "rover_context": {
      "soil_moisture_pct": 32.25,
      "soil_nitrogen_mg_kg": 51.31,
      "soil_temp_c": 22.76,
      "rover_id": "nelavi-mock-1"
    },
    "sources": ["field_telemetry", "agronomic_knowledge_base"],
    "advisory_warning": "Advisory only. Always verify local field soil conditions before irrigation."
  }
  ```

#### 3. Spoken Audio Synthesis (TTS)
- **Method**: `POST`
- **Path**: `/api/voice/synthesize`
- **Body**:
  ```json
  {
    "text": "Based on the latest rover readings, soil moisture is optimal.",
    "language": "en"
  }
  ```
- **Response**:
  ```json
  {
    "status": "ok",
    "audio_base64": "//uQZ...",
    "content_type": "audio/mpeg"
  }
  ```

---

### Screen 4: Precision Agriculture Learning Academy

#### 1. Fetch Lessons Catalog
- **Method**: `GET`
- **Path**: `/api/learning/lessons?lang=en` (or `hi`, `te`)
- **Response**:
  ```json
  {
    "total": 5,
    "completed_count": 1,
    "lessons": [
      {
        "id": "crop_care_stages",
        "title": "Crop Care & Key Growth Stages",
        "summary": "Timely scouting and managing crop stress during vegetative and flowering transitions directly protects crop yield potential.",
        "category": "Crop Management",
        "duration_minutes": 5,
        "icon": "🌱",
        "objective": "Understand the critical physiological stages of field crops and targeted care at each phase.",
        "completed": true
      }
    ]
  }
  ```

#### 2. Fetch Lesson Module Details
- **Method**: `GET`
- **Path**: `/api/learning/lesson?id=crop_care_stages&lang=en`
- **Response**:
  ```json
  {
    "id": "crop_care_stages",
    "title": "Crop Care & Key Growth Stages",
    "summary": "Timely scouting and managing crop stress...",
    "category": "Crop Management",
    "duration_minutes": 5,
    "icon": "🌱",
    "language": "en",
    "objective": "Understand the critical physiological stages...",
    "sections": [
      {
        "heading": "1. Germination & Emergence",
        "body": "Ensure adequate soil moisture without standing water. Soil compaction or crusting impedes emergence."
      },
      {
        "heading": "2. Vegetative Phase",
        "body": "Rapid leaf and canopy development occurs here."
      }
    ],
    "farmer_tips": [
      "Scout fields at least twice a week during vegetative and flowering transitions.",
      "Look under leaf canopies for early signs of mites and fungal spore patches."
    ],
    "completed": true
  }
  ```

#### 3. Update Lesson Progress
- **Method**: `POST`
- **Path**: `/api/learning/progress`
- **Body**:
  ```json
  {
    "lesson_id": "crop_care_stages",
    "completed": true
  }
  ```
- **Response**:
  ```json
  {
    "status": "ok",
    "lesson_id": "crop_care_stages",
    "completed": true,
    "completed_count": 2,
    "total": 5
  }
  ```

---

## 5. Image & Audio Handling in FlutterFlow

### M2.3 Decision: Retaining Base64 JSON for Images
Per architectural evaluation (M2.3), the backend retains base64 JSON encoding for leaf image uploads (`image_png_base64`).
- **Standard FlutterFlow Integration**: When a farmer captures a leaf image via FlutterFlow's **Upload Media** action, `FFUploadedFile.bytes` contains raw image bytes.
- **Custom Function in FlutterFlow**: Convert `FFUploadedFile.bytes` to a base64 string using this standard single-line Dart function:
  ```dart
  import 'dart:convert';
  import '/flutter_flow/uploaded_file.dart';

  String? uploadedFileToBase64(FFUploadedFile? file) {
    if (file == null || file.bytes == null) {
      return null;
    }
    return base64Encode(file.bytes!);
  }
  ```
- **Why this is preferred**:
  1. Base64 JSON avoids unapproved stdlib parsers (`email`, `cgi`) in the security-audited backend.
  2. The image is bundled atomically with user crop metadata, question, and scenario flags.
  3. **Strict Size Limits**:
     - **Permitted Image Size**: The raw decoded leaf PNG is strictly capped at **1 MiB (1,048,576 bytes)** (`MAX_IMAGE_BYTES`). Oversized images are rejected before and after decoding.
     - **Request-Body Limit**: The enclosing JSON HTTP request body for `/api/run-integrated` and `/api/second-opinion` has its own separate limit of **1,402,200 bytes** (`MAX_SECOND_OPINION_BYTES`). This accommodates the base64-encoded 1 MiB image (up to 1,398,104 characters) plus up to 4,096 bytes for JSON syntax, telemetry readings, crop metadata, and user questions.

### Audio Recording & Playback in FlutterFlow
- **Recording (STT)**: Use FlutterFlow's Audio Record widget. Send `RecordAudioBytes` directly in the HTTP body with `Content-Type: audio/webm` or `audio/wav`.
- **Playback (TTS)**: When `/api/voice/synthesize` returns `audio_base64`, decode to audio player or write to temporary file:
  ```dart
  import 'dart:convert';
  import 'dart:typed_data';

  Uint8List base64ToAudioBytes(String base64String) {
    return base64Decode(base64String);
  }
  ```

---

## 6. CORS & Preflight Troubleshooting

When testing FlutterFlow apps from the browser or `https://app.flutterflow.io`:
1. **Preflight Failure (OPTIONS 403/405)**:
   - If the server returns **405 Method Not Allowed**, the backend was started in local mode without `--remote`. Pure local mode disables CORS and rejects `--cors-origins` at startup.
   - Restart the server with `--remote`, `--allowed-hosts`, and `--cors-origins`:
     ```powershell
     $env:NELAVI_AUTH_TOKEN = "local-dev-bearer-token"
     python -B -m demo_app.server --remote --host 127.0.0.1 --port 8765 --allowed-hosts "127.0.0.1,localhost,127.0.0.1:8765" --cors-origins "https://app.flutterflow.io,https://cropsync.flutterflow.app"
     ```
   - If the server returns **403 Forbidden**, the requesting `Origin` does not match any entry in `--cors-origins`.
2. **Wildcards (`*`)**: The CropSync backend strictly forbids `--cors-origins "*"` for security. Always specify exact client origins.
3. **Allowed Methods**: The preflight response allows `GET, POST, HEAD, OPTIONS`.
4. **Allowed Headers**: The preflight response allows `Authorization, Content-Type, Accept, X-Nelavi-Demo`.

---

## 7. Error Handling & Bounds Matrix

| Status Code | Meaning | Typical Trigger | Recommended FlutterFlow Action |
|---|---|---|---|
| **200 OK** | Success | Valid request processed | Update app state and UI. |
| **400 Bad Request** | Schema/syntax error | Missing `question`, bad JSON, unknown `lesson_id` | Display specific `message` in snackbar. |
| **401 Unauthorized** | Missing or invalid Bearer token | Expired or incorrect token in remote mode | Redirect user to Login or reconfigure API token. |
| **403 Forbidden** | Remote provider blocked / Disallowed CORS | Calling real providers in remote mode without `--allow-remote-real-providers` | Check backend server launch flags. |
| **404 Not Found** | Resource not found | Invalid `lesson_id` | Display empty state or return to catalog. |
| **408 Request Timeout** | Client body read timeout | Slow connection taking >30s to stream bytes | Retry request with user prompt. |
| **413 Payload Too Large** | Exceeded byte bounds | Raw image >1 MiB (1,048,576 B), base64 JSON body >1,402,200 B, or standard JSON >4,096 B | Prompt user to compress/resize image (<1 MiB) before uploading. |
| **415 Unsupported Media** | Wrong Content-Type | Sending audio as text or image without PNG | Verify FlutterFlow Content-Type headers. |
| **422 Unprocessable** | Provider closed error | NVIDIA/Tavily provider failure or empty transcript | Inform user to retry leaf capture or voice query. |
