# Nelavi — Edge Authentication Proxy Guide (Milestones 2B + 2C)

## 1. Architectural Overview & Security Boundary

This guide provides the complete operational and security specification for deploying the **Nelavi Cloudflare Worker Authentication Proxy**. The Worker resides at the Cloudflare network edge between client frontends (such as FlutterFlow Web and Mobile apps) and the Nelavi REST backend running on Render.

```
┌─────────────────────────────────┐
│       Client Applications       │
│  (FlutterFlow Web / iOS / And.) │
└────────────────┬────────────────┘
                 │ 1. User Authenticates with Firebase Auth
                 ▼
┌─────────────────────────────────┐
│     Firebase Authentication     │  ◄── Client receives signed RS256
│   (Google Identity Platform)    │      Firebase ID token (JWT)
└────────────────┬────────────────┘
                 │ 2. HTTPS Request with Bearer <Firebase_ID_Token>
                 ▼
┌────────────────────────────────────────────────────────┐
│         Cloudflare Worker Authentication Proxy         │
│                 (proxy/worker.js)                      │
│                                                        │
│  • Cryptographic RS256 verification (Web Crypto API)   │
│  • Google public JWKS fetching, caching & key rotation │
│  • Exact origin CORS handling (preflight 204 response) │
│  • Closed route allowlist & HTTP method enforcement    │
│  • Strict per-route payload bounds (4KB to 10MB)       │
│  • Hop-by-hop & client Host header stripping           │
│  • Inject Host: <upstream.host> (Render validation)    │
│  • Inject Authorization: Bearer <NELAVI_AUTH_TOKEN>    │
│  • 30-second upstream timeout guard                    │
└────────────────────────┬───────────────────────────────┘
                         │ 3. Authenticated HTTPS Request with
                         │    Authorization: Bearer <NELAVI_AUTH_TOKEN>
                         ▼
┌────────────────────────────────────────────────────────┐
│           Nelavi Backend Service (Render)              │
│                 (demo_app/server.py)                   │
│                                                        │
│  • Validates Host header against --allowed-hosts       │
│  • Validates NELAVI_AUTH_TOKEN server-to-server        │
│  • Executes business logic, rover telemetry & ML       │
└────────────────────────────────────────────────────────┘
```

### Why This Architecture?

1. **Secret Isolation**: The shared backend token (`NELAVI_AUTH_TOKEN`) is never placed in client-side FlutterFlow code, web bundles, or network requests originating from end-user devices. It remains strictly an encrypted Cloudflare secret injected server-to-server.
2. **Cold-Start Elimination**: Browser CORS preflight (`OPTIONS`) requests are answered directly at the Cloudflare edge in sub-millisecond time (`204 No Content`), preventing unauthenticated preflight calls from waking or loading the Render instance.
3. **DoS & Abuse Shield**: Malformed requests, unauthorized callers, unsupported routes, invalid HTTP methods, and oversized payloads are blocked at the edge before consuming backend compute or network bandwidth.
4. **End-User Identity Enforcement**: Every API request is tied to an authenticated end-user verified against Google's authoritative public keys.

---

## 2. Proxy Components & Configuration Files

All proxy assets are located in the repository:

- [`proxy/worker.js`](file:///c:/Users/Akshaj%20dasam/OneDrive/Documents/CROPSYNC/proxy/worker.js): High-performance, zero-dependency Cloudflare Worker ES module.
- [`proxy/wrangler.toml`](file:///c:/Users/Akshaj%20dasam/OneDrive/Documents/CROPSYNC/proxy/wrangler.toml): Wrangler configuration file defining environment variables.
- [`tests/test_worker_proxy.js`](file:///c:/Users/Akshaj%20dasam/OneDrive/Documents/CROPSYNC/tests/test_worker_proxy.js): Comprehensive offline test suite (26 automated test cases, synthetic RSA keys, network tripwires).

### Environment Variables (`wrangler.toml`)

| Variable Name | Secret? | Purpose | Example Value |
| :--- | :---: | :--- | :--- |
| `UPSTREAM_URL` | No | Render backend URL | `https://nelavi-backend.onrender.com` |
| `FIREBASE_PROJECT_ID` | No | Target Firebase Project ID | `nelavi-prod-12345` |
| `ALLOWED_CORS_ORIGINS` | No | Comma-separated allowed origins | `https://app.flutterflow.io,https://cropsync.flutterflow.app` |
| `UPSTREAM_TIMEOUT_MS` | No | Upstream timeout in ms (default 55000) | `55000` |
| `NELAVI_AUTH_TOKEN` | **YES** | Backend server-to-server token | Stored encrypted via `wrangler secret put` |

---

## 3. Step-by-Step Setup: Firebase Authentication

### Step 3.1 — Obtain Project ID
1. Open the [Firebase Console](https://console.firebase.google.com/).
2. Select your project (or create one for Nelavi/CropSync).
3. Go to **Project Settings** (gear icon in left sidebar) -> **General**.
4. Copy the **Project ID** (e.g. `nelavi-app-prod`).
5. Update `FIREBASE_PROJECT_ID` in `proxy/wrangler.toml`.

### Step 3.2 — Enable Authentication Providers
1. In Firebase Console, navigate to **Build** -> **Authentication**.
2. Click **Get Started** (if not already enabled).
3. Under the **Sign-in method** tab, enable the desired identity providers (e.g., **Email/Password**, **Google**, or **Phone**).

### Step 3.3 — Authorize Client Domains
1. In **Authentication** -> **Settings** -> **Authorized domains**:
2. Add `app.flutterflow.io` (for FlutterFlow Test Mode / Run Mode).
3. Add your custom FlutterFlow web domain (e.g. `cropsync.flutterflow.app` or your production domain).

---

## 4. Step-by-Step Setup: Cloudflare Worker Deployment

### Prerequisites
- Node.js installed (v18.0.0 or later; tested with v24.19.0).
- Cloudflare account with Workers enabled.

### Step 4.1 — Configure `proxy/wrangler.toml`
Edit `proxy/wrangler.toml` to set your actual upstream Render service domain and Firebase Project ID:

```toml
name = "nelavi-auth-proxy"
main = "worker.js"
compatibility_date = "2024-04-01"

[vars]
UPSTREAM_URL = "https://nelavi-backend.onrender.com"
FIREBASE_PROJECT_ID = "your-actual-firebase-project-id"
ALLOWED_CORS_ORIGINS = "https://app.flutterflow.io,https://cropsync.flutterflow.app"
```

### Step 4.2 — Provision the Secret Token
Run Wrangler inside the `proxy` directory to set the backend shared token. **Never write this token into `wrangler.toml` or commit it to Git**:

```powershell
cd proxy
npx wrangler secret put NELAVI_AUTH_TOKEN
```
When prompted, paste the exact secret token configured in your Render service environment (`NELAVI_AUTH_TOKEN`).

### Step 4.3 — Deploy the Worker
Deploy the Worker to Cloudflare's global edge network:

```powershell
npx wrangler deploy
```

Wrangler will output the deployed Worker URL, for example:
`https://nelavi-auth-proxy.<your-subdomain>.workers.dev`

---

## 5. Step-by-Step Setup: FlutterFlow Client Integration

### Step 5.1 — Configure Firebase in FlutterFlow
1. In your FlutterFlow project, navigate to **Settings** -> **Firebase**.
2. Enter your Firebase Project ID and generate/upload the configuration files.
3. In **Settings** -> **Authentication**, check **Enable Authentication** and select **Firebase**.
4. Set the initial login page and logged-in home page.

### Step 5.2 — Configure the API Group
1. In FlutterFlow, navigate to **API Calls**.
2. Create an **API Group** named `NelaviBackend`.
3. Set **Base URL** to the Cloudflare Worker URL:
   `https://nelavi-auth-proxy.<your-subdomain>.workers.dev`
4. Under **Group Headers**, add:
   - Header Name: `Authorization`
   - Value: `Bearer [firebase_jwt]` (where `firebase_jwt` is a group-level variable).

### Step 5.3 — Retrieve the Firebase ID Token in FlutterFlow
To supply the fresh Firebase ID token to API calls, create a Custom Action in FlutterFlow:

**Action Name:** `getFirebaseIdToken`  
**Return Type:** `String?`

```dart
import 'package:firebase_auth/firebase_auth.dart';

Future<String?> getFirebaseIdToken() async {
  try {
    final user = FirebaseAuth.instance.currentUser;
    if (user == null) {
      return null;
    }
    // forceRefresh: false uses cached token unless expired
    return await user.getIdToken();
  } catch (e) {
    print('Error retrieving Firebase ID token: $e');
    return null;
  }
}
```

In your page or component logic:
1. Call `getFirebaseIdToken`.
2. Pass the resulting token string into the `firebase_jwt` parameter of your API call.

---

## 6. Edge Security Controls & Validation Logic

The Cloudflare Worker implements the following defense-in-depth measures:

### 1. Cryptographic RS256 Verification
- Fetches Google's authoritative public JWKS from:
  `https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com`
- Uses the standard Web Crypto API (`crypto.subtle.importKey` and `crypto.subtle.verify`) for zero-dependency, high-speed RS256 signature verification.
- Caches public keys in-memory honoring Google's HTTP `Cache-Control` header `max-age`.
- If an unknown `kid` is encountered, automatically refreshes keys once to support transparent Google key rotation.

### 2. Strict Firebase Claims Verification
Every token must satisfy all standard Firebase assertions:
- `exp`: Token must not be expired (current UTC timestamp < expiration). Enforces zero clock-skew tolerance (fail-closed).
- `iat`: Token issued-at timestamp must be in the past (clock skew allowance: 60s).
- `nbf`: If present, token Not-Before timestamp cannot be in the future (clock skew allowance: 60s).
- `aud`: Audience must exactly equal `FIREBASE_PROJECT_ID`.
- `iss`: Issuer must equal `https://securetoken.google.com/<FIREBASE_PROJECT_ID>`.
- `sub`: Subject (Firebase UID) must be a non-empty string.
- `auth_time`: Authentication timestamp must be in the past.

### 3. Closed Route Allowlist
Only routes defined in the backend OpenAPI 3.1 specification are permitted. Any unlisted route is rejected immediately at the edge with HTTP 404:

| Route Path | Allowed HTTP Methods | Health / Probe | Body Limit |
| :--- | :--- | :---: | :--- |
| `/health` | GET, HEAD | Yes (Unauthenticated) | — |
| `/api/health` | GET, HEAD | Yes (Unauthenticated) | — |
| `/api/state` | GET | No | — |
| `/api/history` | GET | No | — |
| `/api/run` | POST | No | 4 KB |
| `/api/run-orchestrated` | POST | No | 4 KB |
| `/api/run-integrated` | POST | No | 1.34 MB |
| `/api/second-opinion` | POST | No | 1.34 MB |
| `/api/advisory` | POST | No | 4 KB |
| `/api/research-pro/start` | POST | No | 4 KB |
| `/api/research-pro/status`| GET | No | — |
| `/api/analyze` | POST | No | 1 MB |
| `/api/voice/transcribe` | POST | No | 10 MB |
| `/api/voice/synthesize` | POST | No | 64 KB |
| `/api/assistant/ask` | POST | No | 64 KB |
| `/api/learning/lessons` | GET | No | — |
| `/api/learning/lesson` | GET | No | — |
| `/api/learning/progress`| POST | No | 4 KB |

### 4. Streamed Request-Body Byte Enforcement
- In addition to checking declared `Content-Length` headers before forwarding, the Worker wraps incoming request bodies in a streaming byte-counting `TransformStream` (`createBoundedStream`).
- Inspects bytes on the fly without buffering large images or audio streams in Worker memory.
- If the stream exceeds the route's maximum byte limit, it halts consumption, triggers source stream cancellation, drops the upstream connection, and returns HTTP 413 Payload Too Large.
- If the stream exceeds the declared `Content-Length`, or if the stream closes prematurely before reaching declared length, it returns HTTP 400 Bad Request.

### 5. Query Parameter Validation
- `/api/history`: Optional `limit` parameter must be an integer between 1 and 9999.
- `/api/learning/lessons`: Optional `lang` parameter must be one of `en`, `hi`, `te`.
- `/api/learning/lesson`: Required `id` (non-empty string); optional `lang` (`en`, `hi`, `te`).
- `/api/research-pro/status`: Required `task` parameter (or `task_id` normalized to `task`).

### 6. Hop-by-Hop & Host Header Sanitization
- Strips standard RFC 2616 / RFC 7230 static hop-by-hop headers: `Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`, `Trailers`, `Transfer-Encoding`, `Upgrade`.
- Dynamically strips any custom headers specified in incoming or outgoing `Connection` header tokens (RFC 7230 dynamic hop-by-hop parsing).
- Replaces incoming `Host` header with `upstreamTarget.host` (e.g. `nelavi-backend.onrender.com`). This ensures the Render backend's strict `--allowed-hosts` validation succeeds without needing to add wildcard hosts.
- Strips the incoming client `Authorization` header containing the Firebase ID token and replaces it with `Authorization: Bearer <NELAVI_AUTH_TOKEN>`.
- Enforces `redirect: "manual"` to prevent automatic redirect following and ensure upstream credentials are never disclosed to third-party redirect locations.

### 7. Timeout Strategy & Render Cold-Start Latency
- Render free/starter instances sleep after 15 minutes of inactivity; waking a sleeping instance typically takes **20 to 45 seconds**.
- Cloudflare Workers allow up to **100 seconds** of wall-clock fetch I/O on standard plans.
- The Worker enforces a default upstream timeout of **55 seconds** (`55000 ms`), configurable via `UPSTREAM_TIMEOUT_MS` up to 90 seconds. This avoids premature 504 Gateway Timeout failures during container wakeups while maintaining strict edge resource protection.
- Public health probes (`/health`, `/api/health`) use a fast 15-second timeout for monitoring responsiveness.
- Aborted upstream requests return HTTP 504 Gateway Timeout; connection errors return HTTP 502 Bad Gateway.
- Error response bodies never leak internal configurations or the `NELAVI_AUTH_TOKEN`.

---

## 7. Local Testing & Offline Verification

The proxy test suite is completely self-contained and runs offline without connecting to external networks:

```powershell
node --test tests/test_worker_proxy.js
```

### Test Suite Coverage (50 Tests)
1. **Firebase ID Token Verification**: Valid signature acceptance, invalid signature rejection, payload tampering detection, expired token rejection, future `iat` rejection, future/past `nbf` validation, audience mismatch rejection, issuer mismatch rejection, missing `sub` rejection, unsupported algorithm (HS256) rejection, key rotation retry on unknown `kid`, JWKS caching verification, malformed token segments, invalid base64url characters, non-object JSON headers/payloads, public key retrieval failure, corrupt JWKS JSON.
2. **Closed Route Allowlist**: Unlisted path rejection (HTTP 404), method rejection (HTTP 405), unauthenticated health probe pass-through (`/health`, `/api/health`), unauthenticated access rejection across all 16 protected endpoints.
3. **Query Parameters & MIME Checks**: Parameter type checking on limit/lang/id/task, `Content-Length` requirement on POST (HTTP 411), non-numeric length check (HTTP 400), `Content-Type` validation (HTTP 415), audio and image MIME validation.
4. **Streamed Body Limits & Content Handling**: Exact boundary and one-byte-over tests for all 5 payload classes (Default JSON, Extended JSON, Second Opinion JSON, Raw PNG, Audio); declared length smaller than streamed body rejection (HTTP 400); declared length larger than streamed body rejection (HTTP 400); stream exceeding limit across multiple chunks with source stream cancellation and HTTP 413.
5. **CORS Evaluation & Forwarding**: Allowed origin OPTIONS preflight (HTTP 204), disallowed origin OPTIONS rejection (HTTP 403), disallowed origin actual request rejection (HTTP 403), hop-by-hop static and dynamic RFC 7230 connection header removal, host replacement, origin pinning against path manipulation, upstream redirect non-following (`redirect: "manual"`).
6. **Upstream Error & Timeout Resilience**: Connection error handling (HTTP 502), timeout handling (HTTP 504), configurable `UPSTREAM_TIMEOUT_MS` validation, secret non-leakage verification.
