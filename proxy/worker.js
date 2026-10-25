/**
 * Nelavi Cloudflare Worker Authentication Proxy (Milestones 2B + 2C)
 *
 * Sits at the edge between FlutterFlow clients and the Nelavi backend on Render.
 * Validates Firebase ID tokens cryptographically (RS256 via Web Crypto API),
 * enforces closed route allowlists and per-endpoint payload limits, strips
 * hop-by-hop headers, terminates CORS for approved FlutterFlow origins,
 * and securely injects the shared NELAVI_AUTH_TOKEN server-to-server.
 */

// Authoritative Google Firebase Auth JWKS endpoint
const DEFAULT_GOOGLE_JWKS_URL =
  "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com";

// Closed Route Allowlist matching backend OpenAPI 3.1 specification
const ROUTE_ALLOWLIST = {
  "/health": ["GET", "HEAD"],
  "/api/health": ["GET", "HEAD"],
  "/api/state": ["GET"],
  "/api/history": ["GET"],
  "/api/run": ["POST"],
  "/api/run-orchestrated": ["POST"],
  "/api/run-integrated": ["POST"],
  "/api/second-opinion": ["POST"],
  "/api/advisory": ["POST"],
  "/api/research-pro/start": ["POST"],
  "/api/research-pro/status": ["GET"],
  "/api/analyze": ["POST"],
  "/api/voice/transcribe": ["POST"],
  "/api/voice/synthesize": ["POST"],
  "/api/assistant/ask": ["POST"],
  "/api/learning/lessons": ["GET"],
  "/api/learning/lesson": ["GET"],
  "/api/learning/progress": ["POST"],
};

// Per-route payload size limits (bytes)
const LIMIT_JSON_DEFAULT = 4096;
const LIMIT_EXTENDED_JSON = 65536; // 64 KiB for voice synthesize and assistant ask
const LIMIT_SECOND_OPINION = 1402200; // ~1.34 MiB for base64 image + JSON
const LIMIT_RAW_IMAGE = 1048576; // 1 MiB for raw PNG
const LIMIT_AUDIO = 10485760; // 10 MiB for speech audio

const ROUTE_BODY_LIMITS = {
  "/api/run": LIMIT_JSON_DEFAULT,
  "/api/run-orchestrated": LIMIT_JSON_DEFAULT,
  "/api/advisory": LIMIT_JSON_DEFAULT,
  "/api/research-pro/start": LIMIT_JSON_DEFAULT,
  "/api/learning/progress": LIMIT_JSON_DEFAULT,
  "/api/voice/synthesize": LIMIT_EXTENDED_JSON,
  "/api/assistant/ask": LIMIT_EXTENDED_JSON,
  "/api/run-integrated": LIMIT_SECOND_OPINION,
  "/api/second-opinion": LIMIT_SECOND_OPINION,
  "/api/analyze": LIMIT_RAW_IMAGE,
  "/api/voice/transcribe": LIMIT_AUDIO,
};

// Hop-by-hop headers to strip before forwarding (RFC 2616 / RFC 7230)
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
]);

// In-memory public key cache across Worker invocations
let jwksCache = {
  keys: null,
  expiresAt: 0,
};

/**
 * Helper to build JSON responses with consistent headers
 */
function jsonResponse(status, bodyObj, extraHeaders = {}) {
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  return new Response(JSON.stringify(bodyObj), { status, headers });
}

/**
 * Base64URL decoder converting to Uint8Array with strict character validation
 */
function base64UrlToBytes(base64Url) {
  if (typeof base64Url !== "string") {
    throw new Error("Invalid base64url input: not a string");
  }
  // Enforce valid base64url characters only (A-Z, a-z, 0-9, -, _)
  if (!/^[A-Za-z0-9\-_]*$/.test(base64Url)) {
    throw new Error("Invalid base64url characters");
  }
  let base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
  while (base64.length % 4 !== 0) {
    base64 += "=";
  }
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/**
 * Base64URL string decoder
 */
function base64UrlDecode(str) {
  const bytes = base64UrlToBytes(str);
  return new TextDecoder().decode(bytes);
}

/**
 * Fetches Google's authoritative JWKS, respecting Cache-Control headers
 */
async function getGoogleJwks(jwksUrl, fetchFn = fetch, forceRefresh = false) {
  const now = Date.now();
  if (!forceRefresh && jwksCache.keys && jwksCache.expiresAt > now) {
    return jwksCache.keys;
  }

  const response = await fetchFn(jwksUrl, {
    method: "GET",
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch JWKS from authority (HTTP ${response.status})`);
  }

  let data;
  try {
    data = await response.json();
  } catch (err) {
    throw new Error("Failed to parse JWKS JSON response");
  }

  if (!data || typeof data !== "object" || !Array.isArray(data.keys)) {
    throw new Error("Invalid JWKS payload structure: missing 'keys' array");
  }

  // Parse max-age from Cache-Control header, default to 6 hours (21600 seconds)
  let maxAgeSeconds = 21600;
  const cacheControl = response.headers.get("cache-control");
  if (cacheControl) {
    const match = cacheControl.match(/max-age=(\d+)/i);
    if (match && match[1]) {
      const parsed = parseInt(match[1], 10);
      if (!isNaN(parsed) && parsed > 0) {
        maxAgeSeconds = Math.min(parsed, 86400); // cap at 24 hours
      }
    }
  }

  jwksCache = {
    keys: data.keys,
    expiresAt: now + maxAgeSeconds * 1000,
  };

  return jwksCache.keys;
}

/**
 * Clears in-memory key cache (primarily for tests)
 */
export function resetJwksCache() {
  jwksCache = { keys: null, expiresAt: 0 };
}

/**
 * Cryptographically verifies a Firebase ID Token using Web Crypto (RS256)
 */
async function verifyFirebaseIdToken(token, projectId, jwksUrl, fetchFn = fetch) {
  try {
    if (typeof token !== "string" || !token.trim()) {
      return { valid: false, error: "Unauthorized: missing bearer token." };
    }

    const parts = token.split(".");
    if (parts.length !== 3) {
      return { valid: false, error: "Unauthorized: malformed token format." };
    }

    const [headerB64, payloadB64, signatureB64] = parts;
    if (!headerB64 || !payloadB64 || !signatureB64) {
      return { valid: false, error: "Unauthorized: malformed token format." };
    }

    let header, payload;
    try {
      header = JSON.parse(base64UrlDecode(headerB64));
      payload = JSON.parse(base64UrlDecode(payloadB64));
    } catch (err) {
      return { valid: false, error: "Unauthorized: unparseable token headers or payload." };
    }

    if (!header || typeof header !== "object" || Array.isArray(header)) {
      return { valid: false, error: "Unauthorized: token header must be a JSON object." };
    }

    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return { valid: false, error: "Unauthorized: token payload must be a JSON object." };
    }

    // Algorithm MUST be RS256
    if (header.alg !== "RS256") {
      return { valid: false, error: `Unauthorized: unsupported token algorithm '${header.alg}'. Must be RS256.` };
    }

    if (!header.kid || typeof header.kid !== "string") {
      return { valid: false, error: "Unauthorized: missing key id (kid) in token header." };
    }

    // Retrieve public keys
    let keys;
    try {
      keys = await getGoogleJwks(jwksUrl, fetchFn, false);
    } catch (err) {
      return { valid: false, error: "Unauthorized: unable to retrieve public keys from authority." };
    }

    let matchingKey = keys.find((k) => k && typeof k === "object" && k.kid === header.kid);

    // If not found, attempt forced refresh once to handle Google key rotation
    if (!matchingKey) {
      try {
        keys = await getGoogleJwks(jwksUrl, fetchFn, true);
        matchingKey = keys.find((k) => k && typeof k === "object" && k.kid === header.kid);
      } catch (err) {
        // ignore refresh error and fall through
      }
    }

    if (!matchingKey) {
      return { valid: false, error: "Unauthorized: unknown key id (kid) not present in authority keys." };
    }

    // Cryptographic RS256 signature verification via Web Crypto
    try {
      const cryptoKey = await crypto.subtle.importKey(
        "jwk",
        matchingKey,
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
        false,
        ["verify"]
      );

      const signedData = new TextEncoder().encode(`${headerB64}.${payloadB64}`);
      const signatureBytes = base64UrlToBytes(signatureB64);

      const isValidSignature = await crypto.subtle.verify(
        "RSASSA-PKCS1-v1_5",
        cryptoKey,
        signatureBytes,
        signedData
      );

      if (!isValidSignature) {
        return { valid: false, error: "Unauthorized: invalid token signature." };
      }
    } catch (err) {
      return { valid: false, error: "Unauthorized: cryptographic signature verification failed." };
    }

    // Validate Standard Firebase Claims
    const now = Math.floor(Date.now() / 1000);
    const CLOCK_SKEW_SECONDS = 60; // 60-second tolerance for issuer clock skew

    // Expiration: Fail-closed. Token must not be expired.
    if (typeof payload.exp !== "number" || payload.exp <= now) {
      return { valid: false, error: "Unauthorized: token has expired." };
    }

    // Issued-At: Cannot be in the future (with 60s clock skew tolerance)
    if (typeof payload.iat !== "number" || payload.iat > now + CLOCK_SKEW_SECONDS) {
      return { valid: false, error: "Unauthorized: token issued in the future." };
    }

    // Not-Before: If present, token cannot be used before nbf (with 60s tolerance)
    if (typeof payload.nbf === "number" && payload.nbf > now + CLOCK_SKEW_SECONDS) {
      return { valid: false, error: "Unauthorized: token not yet valid (nbf in future)." };
    }

    // Audience: Must match Firebase project ID exactly
    if (payload.aud !== projectId) {
      return { valid: false, error: `Unauthorized: token audience '${payload.aud}' does not match project '${projectId}'.` };
    }

    // Issuer: Must match Google SecureToken URL for this project
    const expectedIssuer = `https://securetoken.google.com/${projectId}`;
    if (payload.iss !== expectedIssuer) {
      return { valid: false, error: `Unauthorized: token issuer '${payload.iss}' does not match expected '${expectedIssuer}'.` };
    }

    // Subject: Must be non-empty string UID
    if (!payload.sub || typeof payload.sub !== "string" || !payload.sub.trim() || payload.sub.length > 128) {
      return { valid: false, error: "Unauthorized: missing or empty subject (sub) claim." };
    }

    // Auth Time: Cannot be in the future (with 60s clock skew tolerance)
    if (typeof payload.auth_time === "number" && payload.auth_time > now + CLOCK_SKEW_SECONDS) {
      return { valid: false, error: "Unauthorized: auth_time cannot be in the future." };
    }

    return { valid: true, payload };
  } catch (err) {
    return { valid: false, error: "Unauthorized: token validation error." };
  }
}

/**
 * Validates request query parameters against endpoint requirements
 */
function validateQueryParameters(pathname, searchParams) {
  if (pathname === "/api/history") {
    const limit = searchParams.get("limit");
    if (limit !== null) {
      if (!/^\d{1,4}$/.test(limit) || parseInt(limit, 10) <= 0) {
        return "limit must be a whole number between 1 and 9999.";
      }
    }
  }

  if (pathname === "/api/learning/lessons" || pathname === "/api/voice/transcribe") {
    const lang = searchParams.get("lang");
    if (lang !== null && !["en", "hi", "te"].includes(lang.toLowerCase())) {
      return `Unsupported language '${lang}'. Must be one of: en, hi, te.`;
    }
  }

  if (pathname === "/api/learning/lesson") {
    const id = searchParams.get("id");
    if (!id || !id.trim() || !/^[a-zA-Z0-9_\-]+$/.test(id)) {
      return "Missing or invalid lesson id.";
    }
    const lang = searchParams.get("lang");
    if (lang !== null && !["en", "hi", "te"].includes(lang.toLowerCase())) {
      return `Unsupported language '${lang}'. Must be one of: en, hi, te.`;
    }
  }

  if (pathname === "/api/research-pro/status") {
    const task = searchParams.get("task") || searchParams.get("task_id");
    if (!task || !task.trim() || !/^[a-zA-Z0-9_\-]+$/.test(task)) {
      return "Missing or invalid task parameter.";
    }
  }

  return null;
}

/**
 * Validates Content-Type for POST endpoints
 */
function validateContentType(pathname, contentTypeHeader) {
  const mime = (contentTypeHeader || "").split(";")[0].trim().toLowerCase();

  if (pathname === "/api/voice/transcribe") {
    if (!mime || (!mime.startsWith("audio/") && mime !== "application/octet-stream")) {
      return "Content-Type must be an audio format (e.g. audio/webm, audio/wav, audio/mpeg).";
    }
    return null;
  }

  if (pathname === "/api/analyze") {
    if (mime !== "image/png") {
      return "Content-Type must be image/png.";
    }
    return null;
  }

  // All other POST endpoints require JSON
  if (mime !== "application/json") {
    return "Content-Type must be application/json.";
  }

  return null;
}

/**
 * Creates a bounded ReadableStream that counts incoming bytes on the fly.
 * Enforces both maxAllowed route limits and declared Content-Length integrity without
 * buffering the stream in memory. If limits are exceeded or the stream terminates
 * prematurely, it errors the stream and updates streamState, triggering source cancellation.
 */
function createBoundedStream(sourceStream, maxAllowed, declaredLength, streamState) {
  if (!sourceStream) {
    if (declaredLength > 0) {
      streamState.error = "The request body was shorter than its Content-Length.";
      streamState.status = 400;
    }
    return null;
  }

  let bytesRead = 0;
  const transform = new TransformStream({
    transform(chunk, controller) {
      bytesRead += chunk.byteLength;
      if (bytesRead > maxAllowed) {
        streamState.error = `Request body stream exceeded maximum allowed limit of ${maxAllowed} bytes (received at least ${bytesRead}).`;
        streamState.status = 413;
        const err = new Error(streamState.error);
        err.name = "PayloadTooLargeError";
        controller.error(err);
        return;
      }
      if (bytesRead > declaredLength) {
        streamState.error = `Request body stream exceeded declared Content-Length of ${declaredLength} bytes.`;
        streamState.status = 400;
        const err = new Error(streamState.error);
        err.name = "ContentLengthMismatchError";
        controller.error(err);
        return;
      }
      controller.enqueue(chunk);
    },
    flush(controller) {
      if (bytesRead < declaredLength) {
        streamState.error = "The request body was shorter than its Content-Length.";
        streamState.status = 400;
        const err = new Error(streamState.error);
        err.name = "PrematureStreamCloseError";
        controller.error(err);
      }
    },
  });

  return sourceStream.pipeThrough(transform);
}

/**
 * Main Worker request handler
 */
export async function handleRequest(request, env, customFetch = fetch) {
  const url = new URL(request.url);
  const pathname = url.pathname;
  const method = request.method.toUpperCase();

  // Parse allowed CORS origins from environment
  const allowedOrigins = new Set(
    (env.ALLOWED_CORS_ORIGINS || "")
      .split(",")
      .map((o) => o.trim().toLowerCase())
      .filter((o) => Boolean(o))
  );

  const requestOrigin = (request.headers.get("origin") || "").trim().toLowerCase();
  const isOriginAllowed = requestOrigin && allowedOrigins.has(requestOrigin);

  // Helper to attach CORS headers to responses
  function attachCors(res) {
    if (isOriginAllowed) {
      res.headers.set("Access-Control-Allow-Origin", requestOrigin);
      res.headers.set("Vary", "Origin");
    }
    return res;
  }

  // 1. Handle CORS Preflight (OPTIONS)
  if (method === "OPTIONS") {
    if (!requestOrigin || !isOriginAllowed) {
      return jsonResponse(403, { error: "CORS origin not allowed." });
    }

    const reqMethod = request.headers.get("access-control-request-method");
    const validMethods = ["GET", "POST", "HEAD", "OPTIONS"];
    if (reqMethod && !validMethods.includes(reqMethod.toUpperCase())) {
      return jsonResponse(405, { error: "Requested CORS method not allowed." });
    }

    const preflightHeaders = {
      "Access-Control-Allow-Origin": requestOrigin,
      "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
      "Access-Control-Max-Age": "86400",
      Vary: "Origin",
    };

    return new Response(null, { status: 204, headers: preflightHeaders });
  }

  // If request has an Origin header from an untrusted origin, reject early
  if (requestOrigin && !isOriginAllowed) {
    return jsonResponse(403, { error: "CORS origin not allowed." });
  }

  // 2. Closed Route Allowlist Check
  const allowedMethods = ROUTE_ALLOWLIST[pathname];
  if (!allowedMethods) {
    return attachCors(jsonResponse(404, { error: "Not found." }));
  }

  if (!allowedMethods.includes(method)) {
    const res = jsonResponse(405, { error: "Method not allowed." }, { Allow: allowedMethods.join(", ") });
    return attachCors(res);
  }

  // 3. Query Parameter Validation
  const queryError = validateQueryParameters(pathname, url.searchParams);
  if (queryError) {
    return attachCors(jsonResponse(400, { error: queryError }));
  }

  // 4. Request Payload Size & Content-Type Bounds (POST requests)
  if (method === "POST") {
    const rawLength = request.headers.get("content-length");
    if (!rawLength) {
      return attachCors(jsonResponse(411, { error: "Content-Length is required." }));
    }

    const contentLength = parseInt(rawLength, 10);
    if (isNaN(contentLength) || contentLength < 0 || String(contentLength) !== rawLength.trim()) {
      return attachCors(jsonResponse(400, { error: "Content-Length must be a whole positive number." }));
    }

    const maxAllowed = ROUTE_BODY_LIMITS[pathname] || LIMIT_JSON_DEFAULT;
    if (contentLength > maxAllowed) {
      return attachCors(
        jsonResponse(413, {
          error: `Request body exceeds maximum allowed limit of ${maxAllowed} bytes (received ${contentLength}).`,
        })
      );
    }

    const contentTypeError = validateContentType(pathname, request.headers.get("content-type"));
    if (contentTypeError) {
      return attachCors(jsonResponse(415, { error: contentTypeError }));
    }
  }

  // 5. Authentication Verification
  // /health and /api/health are public probes; all other routes require Firebase ID Token
  const isHealthProbe = pathname === "/health" || pathname === "/api/health";
  if (!isHealthProbe) {
    const authHeader = request.headers.get("authorization") || "";
    const [scheme, token] = authHeader.split(" ");

    if (!authHeader || !token) {
      return attachCors(jsonResponse(401, { error: "Unauthorized: missing bearer token." }));
    }

    if (scheme.toLowerCase() !== "bearer") {
      return attachCors(jsonResponse(401, { error: "Unauthorized: invalid authorization scheme. Must be Bearer." }));
    }

    const jwksUrl = env.GOOGLE_JWKS_URL || DEFAULT_GOOGLE_JWKS_URL;
    const authResult = await verifyFirebaseIdToken(token.trim(), env.FIREBASE_PROJECT_ID, jwksUrl, customFetch);

    if (!authResult.valid) {
      return attachCors(jsonResponse(401, { error: authResult.error }));
    }
  }

  // 6. Upstream Forwarding
  if (!env.UPSTREAM_URL || !env.UPSTREAM_URL.startsWith("http")) {
    return attachCors(jsonResponse(500, { error: "Internal error: UPSTREAM_URL is not configured." }));
  }

  if (!isHealthProbe && (!env.NELAVI_AUTH_TOKEN || !env.NELAVI_AUTH_TOKEN.trim())) {
    return attachCors(jsonResponse(500, { error: "Internal error: NELAVI_AUTH_TOKEN is not configured." }));
  }

  let upstreamTarget;
  try {
    const upstreamBase = new URL(env.UPSTREAM_URL);
    if (!upstreamBase.protocol.startsWith("http")) {
      return attachCors(jsonResponse(500, { error: "Internal error: UPSTREAM_URL must use HTTP or HTTPS." }));
    }
    // Pin to upstreamBase origin to prevent any path-based origin override
    upstreamTarget = new URL(upstreamBase.origin);
    upstreamTarget.pathname = pathname;
    upstreamTarget.search = url.search;

    // For /api/research-pro/status, ensure backend receives ?task=<id> if ?task_id=<id> was provided
    if (pathname === "/api/research-pro/status" && !upstreamTarget.searchParams.has("task")) {
      const taskId = url.searchParams.get("task_id");
      if (taskId) {
        upstreamTarget.searchParams.set("task", taskId);
      }
    }
  } catch (err) {
    return attachCors(jsonResponse(500, { error: "Internal error: malformed upstream URL." }));
  }

  // Extract dynamic hop-by-hop headers from incoming request Connection header (RFC 7230)
  const clientConnHeader = request.headers.get("connection");
  const dynamicHopByHop = new Set(HOP_BY_HOP_HEADERS);
  if (clientConnHeader) {
    clientConnHeader.split(",").forEach((token) => {
      const trimmed = token.trim().toLowerCase();
      if (trimmed) dynamicHopByHop.add(trimmed);
    });
  }

  // Prepare sanitized upstream headers
  const forwardHeaders = new Headers();

  for (const [key, value] of request.headers.entries()) {
    const lowerKey = key.toLowerCase();
    // Strip hop-by-hop headers, connection-named headers, client authorization, and client host
    if (dynamicHopByHop.has(lowerKey) || lowerKey === "authorization" || lowerKey === "host") {
      continue;
    }
    forwardHeaders.set(key, value);
  }

  // Forward exact Host header expected by Render's allowed-hosts check
  forwardHeaders.set("Host", upstreamTarget.host);

  // Inject backend bearer token server-to-server for protected routes
  if (!isHealthProbe) {
    forwardHeaders.set("Authorization", `Bearer ${env.NELAVI_AUTH_TOKEN.trim()}`);
  }

  // Prepare streamed body with byte enforcement for POST requests
  const streamState = { error: null, status: null };
  let forwardBody = undefined;
  if (method === "POST") {
    const rawLength = request.headers.get("content-length");
    const declaredLength = parseInt(rawLength, 10);
    const maxAllowed = ROUTE_BODY_LIMITS[pathname] || LIMIT_JSON_DEFAULT;
    forwardBody = createBoundedStream(request.body, maxAllowed, declaredLength, streamState);
    if (streamState.status) {
      return attachCors(jsonResponse(streamState.status, { error: streamState.error }));
    }
  }

  // Determine upstream timeout:
  // Public health probes fail fast (15s); operational routes default to 55s (accommodates Render cold-starts).
  // Capped at 90s to stay safely within Cloudflare Worker 100s wall-clock limit.
  let timeoutMs = isHealthProbe ? 15000 : 55000;
  if (env.UPSTREAM_TIMEOUT_MS) {
    const parsedTimeout = parseInt(env.UPSTREAM_TIMEOUT_MS, 10);
    if (!isNaN(parsedTimeout) && parsedTimeout > 0) {
      timeoutMs = Math.min(parsedTimeout, 90000);
    }
  }

  // Execute Upstream Fetch with timeout guard and redirect: "manual"
  let upstreamResponse;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    upstreamResponse = await customFetch(upstreamTarget.toString(), {
      method,
      headers: forwardHeaders,
      body: forwardBody,
      signal: controller.signal,
      redirect: "manual", // Do not follow redirects automatically; avoid credential leakage
    });
  } catch (err) {
    if (streamState.status) {
      return attachCors(jsonResponse(streamState.status, { error: streamState.error }));
    }
    if (err.name === "AbortError" || (err.message && err.message.includes("abort"))) {
      return attachCors(jsonResponse(504, { error: "Gateway timeout waiting for upstream backend." }));
    }
    return attachCors(jsonResponse(502, { error: "Bad gateway: unable to reach upstream backend service." }));
  } finally {
    clearTimeout(timeoutId);
  }

  // Extract dynamic hop-by-hop headers from upstream response Connection header (RFC 7230)
  const upstreamConnHeader = upstreamResponse.headers.get("connection");
  const upstreamHopByHop = new Set(HOP_BY_HOP_HEADERS);
  if (upstreamConnHeader) {
    upstreamConnHeader.split(",").forEach((token) => {
      const trimmed = token.trim().toLowerCase();
      if (trimmed) upstreamHopByHop.add(trimmed);
    });
  }

  // Forward upstream response, stripping hop-by-hop headers and injecting CORS
  const responseHeaders = new Headers();
  for (const [key, value] of upstreamResponse.headers.entries()) {
    if (!upstreamHopByHop.has(key.toLowerCase())) {
      responseHeaders.set(key, value);
    }
  }

  if (isOriginAllowed) {
    responseHeaders.set("Access-Control-Allow-Origin", requestOrigin);
    responseHeaders.set("Vary", "Origin");
  }

  // Enforce standard security headers
  responseHeaders.set("X-Content-Type-Options", "nosniff");
  responseHeaders.set("Cache-Control", "no-store");

  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: responseHeaders,
  });
}

export default {
  async fetch(request, env, ctx) {
    return handleRequest(request, env, fetch);
  },
};
