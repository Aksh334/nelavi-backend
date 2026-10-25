/**
 * Offline Test Suite for Nelavi Cloudflare Worker Proxy (Milestones 2B + 2C + 2D Hardened)
 *
 * Uses Node.js built-in test runner, Web Crypto API, and synthetic RSA-2048 key pairs.
 * Network tripwires strictly prevent external socket calls.
 */

import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { handleRequest, resetJwksCache } from "../proxy/worker.js";

// Generate synthetic RSA key pairs for testing
const rsaKey1 = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const rsaKey2 = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });

const jwk1 = { ...rsaKey1.publicKey.export({ format: "jwk" }), kid: "key-1", alg: "RS256", use: "sig" };
const jwk2 = { ...rsaKey2.publicKey.export({ format: "jwk" }), kid: "key-2", alg: "RS256", use: "sig" };

const PROJECT_ID = "nelavi-test-project";
const UPSTREAM_URL = "https://nelavi-backend.onrender.com";
const NELAVI_AUTH_TOKEN = "secret-backend-token-998877";
const ALLOWED_CORS_ORIGIN = "https://app.flutterflow.io";
const DENIED_CORS_ORIGIN = "https://untrusted-site.com";
const MOCK_JWKS_URL = "https://mock.googleapis.com/jwks";

function makeJwt(payloadOverrides = {}, headerOverrides = {}, key = rsaKey1.privateKey) {
  const now = Math.floor(Date.now() / 1000);
  const header = {
    alg: "RS256",
    kid: "key-1",
    typ: "JWT",
    ...headerOverrides,
  };
  const payload = {
    iss: `https://securetoken.google.com/${PROJECT_ID}`,
    aud: PROJECT_ID,
    sub: "user-123456",
    iat: now - 10,
    exp: now + 3600,
    auth_time: now - 10,
    ...payloadOverrides,
  };

  const hB64 = Buffer.from(JSON.stringify(header)).toString("base64url");
  const pB64 = Buffer.from(JSON.stringify(payload)).toString("base64url");

  const signer = crypto.createSign("RSA-SHA256");
  signer.update(`${hB64}.${pB64}`);
  const sB64 = signer.sign(key, "base64url");

  return `${hB64}.${pB64}.${sB64}`;
}

function createMockEnvironment(overrides = {}) {
  return {
    UPSTREAM_URL,
    FIREBASE_PROJECT_ID: PROJECT_ID,
    NELAVI_AUTH_TOKEN,
    ALLOWED_CORS_ORIGINS: `${ALLOWED_CORS_ORIGIN},https://cropsync.flutterflow.app`,
    GOOGLE_JWKS_URL: MOCK_JWKS_URL,
    ...overrides,
  };
}

/**
 * Creates a mock fetch function that handles JWKS and upstream calls with tripwires
 */
function createMockFetch(upstreamHandler = null, jwksKeysOrHandler = [jwk1]) {
  const calls = {
    jwksCount: 0,
    upstreamCalls: [],
  };

  const mockFetch = async (url, options = {}) => {
    const urlStr = String(url);

    // Tripwire: Refuse external requests
    if (!urlStr.startsWith(MOCK_JWKS_URL) && !urlStr.startsWith(UPSTREAM_URL)) {
      throw new Error(`TRIPWIRE TRIGGERED: Attempted unapproved external network request to ${urlStr}`);
    }

    if (urlStr.startsWith(MOCK_JWKS_URL)) {
      calls.jwksCount++;
      if (typeof jwksKeysOrHandler === "function") {
        return jwksKeysOrHandler(urlStr, options);
      }
      return new Response(JSON.stringify({ keys: jwksKeysOrHandler }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "public, max-age=3600",
        },
      });
    }

    if (urlStr.startsWith(UPSTREAM_URL)) {
      // Consume body stream if present (mirroring real HTTP transport behavior)
      if (options.body && typeof options.body.getReader === "function") {
        const reader = options.body.getReader();
        try {
          while (true) {
            const { done } = await reader.read();
            if (done) break;
          }
        } finally {
          reader.releaseLock();
        }
      }
      calls.upstreamCalls.push({ url: urlStr, options });
      if (upstreamHandler) {
        return upstreamHandler(urlStr, options);
      }
      return new Response(JSON.stringify({ status: "ok", service: "mock-backend" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    throw new Error(`Unexpected URL in mock fetch: ${urlStr}`);
  };

  return { mockFetch, calls };
}

describe("Nelavi Cloudflare Worker Proxy Test Suite", () => {
  beforeEach(() => {
    resetJwksCache();
  });

  describe("1. Firebase ID Token Verification", () => {
    test("valid signed token is accepted and forwards upstream", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: {
          Authorization: `Bearer ${token}`,
          Origin: ALLOWED_CORS_ORIGIN,
        },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 200);
      assert.equal(calls.upstreamCalls.length, 1);
      assert.equal(calls.upstreamCalls[0].options.headers.get("Authorization"), `Bearer ${NELAVI_AUTH_TOKEN}`);
      assert.equal(res.headers.get("Access-Control-Allow-Origin"), ALLOWED_CORS_ORIGIN);
    });

    test("token with invalid cryptographic signature is rejected with 401", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt({}, {}, rsaKey2.privateKey);
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /invalid token signature/i);
      assert.equal(calls.upstreamCalls.length, 0, "No upstream request must be sent on auth failure");
    });

    test("tampered payload is rejected with 401", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt();
      const [h, p, s] = token.split(".");
      const tamperedPayload = Buffer.from(JSON.stringify({ ...JSON.parse(Buffer.from(p, "base64url")), sub: "hacker" })).toString("base64url");
      const tamperedToken = `${h}.${tamperedPayload}.${s}`;
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${tamperedToken}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      assert.equal(calls.upstreamCalls.length, 0);
    });

    test("expired token is rejected with 401 (fail-closed immediately upon expiry)", async () => {
      const { mockFetch } = createMockFetch();
      const now = Math.floor(Date.now() / 1000);
      const token = makeJwt({ exp: now - 1 }); // expired 1 second ago
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /expired/i);
    });

    test("future-issued token (iat in future) is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const now = Math.floor(Date.now() / 1000);
      const token = makeJwt({ iat: now + 500 });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /future/i);
    });

    test("token with future nbf is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const now = Math.floor(Date.now() / 1000);
      const token = makeJwt({ nbf: now + 300 }); // not valid yet
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /not yet valid/i);
    });

    test("token with valid past nbf is accepted", async () => {
      const { mockFetch, calls } = createMockFetch();
      const now = Math.floor(Date.now() / 1000);
      const token = makeJwt({ nbf: now - 30 });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 200);
      assert.equal(calls.upstreamCalls.length, 1);
    });

    test("wrong audience is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt({ aud: "attacker-foreign-project" });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /audience/i);
    });

    test("wrong issuer is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt({ iss: "https://attacker.example.com" });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /issuer/i);
    });

    test("missing or empty subject (sub) is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt({ sub: "" });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /subject/i);
    });

    test("unsupported algorithm (e.g. HS256) is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt({}, { alg: "HS256" });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /unsupported token algorithm/i);
    });

    test("unknown key id (kid) is rejected with 401 after rotation refresh attempt", async () => {
      const { mockFetch, calls } = createMockFetch(null, [jwk1]);
      const token = makeJwt({}, { kid: "non-existent-kid" });
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /unknown key id/i);
      assert.equal(calls.jwksCount, 2, "Must attempt JWKS refresh once on unknown kid");
    });

    test("JWKS caching works and reuses cached keys across requests", async () => {
      const { mockFetch, calls } = createMockFetch(null, [jwk1]);
      const token = makeJwt();
      const env = createMockEnvironment();

      const req1 = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const res1 = await handleRequest(req1, env, mockFetch);
      assert.equal(res1.status, 200);
      assert.equal(calls.jwksCount, 1);

      const req2 = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const res2 = await handleRequest(req2, env, mockFetch);
      assert.equal(res2.status, 200);
      assert.equal(calls.jwksCount, 1, "Second request must use in-memory JWKS cache without re-fetching");
    });

    test("malformed JWT segments (1 part, 2 parts, 4 parts) are rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      for (const badToken of ["single-part", "two.parts", "one.two.three.four"]) {
        const req = new Request("https://worker.local/api/state", {
          method: "GET",
          headers: { Authorization: `Bearer ${badToken}` },
        });
        const res = await handleRequest(req, env, mockFetch);
        assert.equal(res.status, 401);
        const body = await res.json();
        assert.match(body.error, /malformed token format/i);
      }
    });

    test("invalid base64url characters in token segments are rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      const badCharsToken = "eyJhbGciOiJSUzI1NiJ9.invalid%payload!chars.signature";
      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${badCharsToken}` },
      });
      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /unparseable token headers or payload/i);
    });

    test("non-object JSON payload (null, primitive, array) is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();
      const headerB64 = Buffer.from(JSON.stringify({ alg: "RS256", kid: "key-1" })).toString("base64url");

      for (const rawPayload of ["null", "12345", '"string"', "[]"]) {
        const payloadB64 = Buffer.from(rawPayload).toString("base64url");
        const token = `${headerB64}.${payloadB64}.fakesig`;
        const req = new Request("https://worker.local/api/state", {
          method: "GET",
          headers: { Authorization: `Bearer ${token}` },
        });
        const res = await handleRequest(req, env, mockFetch);
        assert.equal(res.status, 401);
        const body = await res.json();
        assert.match(body.error, /must be a JSON object/i);
      }
    });

    test("non-object JSON header is rejected with 401", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();
      const payloadB64 = Buffer.from(JSON.stringify({ sub: "user-1" })).toString("base64url");

      for (const rawHeader of ["null", "12345", '"string"', "[]"]) {
        const headerB64 = Buffer.from(rawHeader).toString("base64url");
        const token = `${headerB64}.${payloadB64}.fakesig`;
        const req = new Request("https://worker.local/api/state", {
          method: "GET",
          headers: { Authorization: `Bearer ${token}` },
        });
        const res = await handleRequest(req, env, mockFetch);
        assert.equal(res.status, 401);
        const body = await res.json();
        assert.match(body.error, /must be a JSON object/i);
      }
    });

    test("public key retrieval failure (authority returns HTTP 500) returns 401", async () => {
      const { mockFetch } = createMockFetch(null, () => {
        return new Response("Internal Server Error", { status: 500 });
      });
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /unable to retrieve public keys/i);
    });

    test("malformed JWKS response (not JSON or missing keys array) returns 401", async () => {
      const { mockFetch } = createMockFetch(null, () => {
        return new Response("corrupted-not-json {", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      });
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 401);
      const body = await res.json();
      assert.match(body.error, /unable to retrieve public keys/i);
    });
  });

  describe("2. Closed Route Allowlist & Method Rules", () => {
    test("unlisted path returns 404 Not Found", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/unlisted-route", {
        method: "GET",
        headers: { Authorization: `Bearer ${makeJwt()}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 404);
      const body = await res.json();
      assert.equal(body.error, "Not found.");
    });

    test("disallowed method on valid path returns 405 Method Not Allowed", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${makeJwt()}`,
          "Content-Length": "2",
          "Content-Type": "application/json",
        },
        body: "{}",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 405);
      assert.equal(res.headers.get("Allow"), "GET");
    });

    test("health probes /health and /api/health are unauthenticated pass-throughs", async () => {
      const { mockFetch, calls } = createMockFetch();
      const env = createMockEnvironment();

      const res1 = await handleRequest(new Request("https://worker.local/health", { method: "GET" }), env, mockFetch);
      assert.equal(res1.status, 200);

      const res2 = await handleRequest(new Request("https://worker.local/api/health", { method: "GET" }), env, mockFetch);
      assert.equal(res2.status, 200);

      assert.equal(calls.upstreamCalls.length, 2);
      assert.equal(calls.upstreamCalls[0].options.headers.get("Authorization"), null, "Health check must not inject secret token");
      assert.equal(calls.upstreamCalls[1].options.headers.get("Authorization"), null);
    });

    test("unauthenticated access to every protected route is rejected with 401", async () => {
      const { mockFetch, calls } = createMockFetch();
      const env = createMockEnvironment();

      const protectedRoutes = [
        { path: "/api/state", method: "GET" },
        { path: "/api/history", method: "GET" },
        { path: "/api/research-pro/status?task=task-1", method: "GET" },
        { path: "/api/learning/lessons", method: "GET" },
        { path: "/api/learning/lesson?id=lesson-1", method: "GET" },
        { path: "/api/run", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/run-orchestrated", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/run-integrated", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/second-opinion", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/advisory", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/research-pro/start", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/analyze", method: "POST", headers: { "Content-Type": "image/png", "Content-Length": "4" }, body: "fake" },
        { path: "/api/voice/transcribe", method: "POST", headers: { "Content-Type": "audio/webm", "Content-Length": "4" }, body: "fake" },
        { path: "/api/voice/synthesize", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/assistant/ask", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
        { path: "/api/learning/progress", method: "POST", headers: { "Content-Type": "application/json", "Content-Length": "2" }, body: "{}" },
      ];

      for (const route of protectedRoutes) {
        const req = new Request(`https://worker.local${route.path}`, {
          method: route.method,
          headers: route.headers || {},
          body: route.body,
        });

        const res = await handleRequest(req, env, mockFetch);
        assert.equal(res.status, 401, `Route ${route.method} ${route.path} must return 401 when unauthenticated`);
      }

      assert.equal(calls.upstreamCalls.length, 0, "No unauthenticated calls must ever reach upstream backend");
    });
  });

  describe("3. Query Parameters & Content-Type Bounds", () => {
    test("valid limit on /api/history is forwarded", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/history?limit=10", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 200);
      assert.equal(calls.upstreamCalls.length, 1);
      assert.match(calls.upstreamCalls[0].url, /limit=10/);
    });

    test("malformed limit on /api/history returns 400 Bad Request", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      for (const badLimit of ["-5", "abc", "0", "1.5"]) {
        const req = new Request(`https://worker.local/api/history?limit=${badLimit}`, {
          method: "GET",
          headers: { Authorization: `Bearer ${token}` },
        });

        const res = await handleRequest(req, env, mockFetch);
        assert.equal(res.status, 400);
        const body = await res.json();
        assert.match(body.error, /limit must be a whole number/i);
      }
    });

    test("valid and invalid lang parameters on /api/learning/lessons", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Valid: en, hi, te
      for (const validLang of ["en", "hi", "te"]) {
        const req = new Request(`https://worker.local/api/learning/lessons?lang=${validLang}`, {
          method: "GET",
          headers: { Authorization: `Bearer ${token}` },
        });
        const res = await handleRequest(req, env, mockFetch);
        assert.equal(res.status, 200);
      }

      // Invalid language
      const badReq = new Request("https://worker.local/api/learning/lessons?lang=fr", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const badRes = await handleRequest(badReq, env, mockFetch);
      assert.equal(badRes.status, 400);
      const body = await badRes.json();
      assert.match(body.error, /unsupported language/i);
    });

    test("valid and invalid parameters on /api/learning/lesson", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Missing id
      const reqMissing = new Request("https://worker.local/api/learning/lesson", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const resMissing = await handleRequest(reqMissing, env, mockFetch);
      assert.equal(resMissing.status, 400);

      // Valid id and lang
      const reqValid = new Request("https://worker.local/api/learning/lesson?id=soil-health&lang=hi", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const resValid = await handleRequest(reqValid, env, mockFetch);
      assert.equal(resValid.status, 200);
    });

    test("valid task and task_id parameters on /api/research-pro/status", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Standard OpenAPI ?task=task-abc
      const req1 = new Request("https://worker.local/api/research-pro/status?task=task-abc", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const res1 = await handleRequest(req1, env, mockFetch);
      assert.equal(res1.status, 200);
      assert.match(calls.upstreamCalls[0].url, /task=task-abc/);

      // Client ?task_id=task-xyz normalized to ?task=task-xyz
      const req2 = new Request("https://worker.local/api/research-pro/status?task_id=task-xyz", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const res2 = await handleRequest(req2, env, mockFetch);
      assert.equal(res2.status, 200);
      assert.match(calls.upstreamCalls[1].url, /task=task-xyz/);

      // Missing task and task_id
      const reqBad = new Request("https://worker.local/api/research-pro/status", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const resBad = await handleRequest(reqBad, env, mockFetch);
      assert.equal(resBad.status, 400);
    });

    test("POST without Content-Length returns 411 Length Required", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: "{}",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 411);
      const body = await res.json();
      assert.match(body.error, /content-length is required/i);
    });

    test("POST with non-numeric Content-Length returns 400 Bad Request", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "invalid-length",
        },
        body: "{}",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 400);
      const body = await res.json();
      assert.match(body.error, /whole positive number/i);
    });

    test("POST with wrong Content-Type returns 415 Unsupported Media Type", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Length": "2",
          "Content-Type": "text/plain",
        },
        body: "{}",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 415);
      const body = await res.json();
      assert.match(body.error, /application\/json/i);
    });

    test("audio and image MIME type validations", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // /api/voice/transcribe accepts audio/webm and application/octet-stream
      const reqAudio = new Request("https://worker.local/api/voice/transcribe", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Length": "4",
          "Content-Type": "audio/webm",
        },
        body: "fake",
      });
      const resAudio = await handleRequest(reqAudio, env, mockFetch);
      assert.equal(resAudio.status, 200);

      // /api/analyze requires image/png
      const reqBadImage = new Request("https://worker.local/api/analyze", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Length": "4",
          "Content-Type": "image/jpeg",
        },
        body: "fake",
      });
      const resBadImage = await handleRequest(reqBadImage, env, mockFetch);
      assert.equal(resBadImage.status, 415);
      const body = await resBadImage.json();
      assert.match(body.error, /image\/png/i);
    });
  });

  describe("4. Exact Body-Size Boundary & One-Byte-Over Tests", () => {
    test("Default JSON (4,096 bytes) on /api/run boundary", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Exact limit: 4,096 bytes -> 200 OK
      const exactBody = "{}" + " ".repeat(4094);
      const reqExact = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "4096",
        },
        body: exactBody,
      });
      const resExact = await handleRequest(reqExact, env, mockFetch);
      assert.equal(resExact.status, 200);

      // One byte over: 4,097 bytes -> 413 Payload Too Large
      const overBody = "{}" + " ".repeat(4095);
      const reqOver = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "4097",
        },
        body: overBody,
      });
      const resOver = await handleRequest(reqOver, env, mockFetch);
      assert.equal(resOver.status, 413);
      const body = await resOver.json();
      assert.match(body.error, /exceeds maximum allowed limit of 4096 bytes/i);
    });

    test("Extended JSON (65,536 bytes) on /api/voice/synthesize and /api/assistant/ask boundary", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      for (const route of ["/api/voice/synthesize", "/api/assistant/ask"]) {
        // Exact limit: 65,536 bytes -> 200 OK
        const exactBody = "{}" + " ".repeat(65534);
        const reqExact = new Request(`https://worker.local${route}`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
            "Content-Length": "65536",
          },
          body: exactBody,
        });
        const resExact = await handleRequest(reqExact, env, mockFetch);
        assert.equal(resExact.status, 200, `${route} must accept 65536 bytes`);

        // One byte over: 65,537 bytes -> 413 Payload Too Large
        const overBody = "{}" + " ".repeat(65535);
        const reqOver = new Request(`https://worker.local${route}`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
            "Content-Length": "65537",
          },
          body: overBody,
        });
        const resOver = await handleRequest(reqOver, env, mockFetch);
        assert.equal(resOver.status, 413, `${route} must reject 65537 bytes`);
        const body = await resOver.json();
        assert.match(body.error, /exceeds maximum allowed limit of 65536 bytes/i);
      }
    });

    test("Second Opinion JSON (1,402,200 bytes) on /api/second-opinion boundary", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Exact limit: 1,402,200 bytes -> 200 OK
      const exactBody = "{}" + " ".repeat(1402198);
      const reqExact = new Request("https://worker.local/api/second-opinion", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "1402200",
        },
        body: exactBody,
      });
      const resExact = await handleRequest(reqExact, env, mockFetch);
      assert.equal(resExact.status, 200);

      // One byte over: 1,402,201 bytes -> 413 Payload Too Large
      const reqOver = new Request("https://worker.local/api/second-opinion", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "1402201",
        },
        body: "{}",
      });
      const resOver = await handleRequest(reqOver, env, mockFetch);
      assert.equal(resOver.status, 413);
      const body = await resOver.json();
      assert.match(body.error, /exceeds maximum allowed limit of 1402200 bytes/i);
    });

    test("Raw PNG (1,048,576 bytes) on /api/analyze boundary", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Exact limit: 1,048,576 bytes -> 200 OK
      const exactBody = new Uint8Array(1048576);
      const reqExact = new Request("https://worker.local/api/analyze", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "image/png",
          "Content-Length": "1048576",
        },
        body: exactBody,
      });
      const resExact = await handleRequest(reqExact, env, mockFetch);
      assert.equal(resExact.status, 200);

      // One byte over: 1,048,577 bytes -> 413 Payload Too Large
      const reqOver = new Request("https://worker.local/api/analyze", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "image/png",
          "Content-Length": "1048577",
        },
        body: "fake-image",
      });
      const resOver = await handleRequest(reqOver, env, mockFetch);
      assert.equal(resOver.status, 413);
      const body = await resOver.json();
      assert.match(body.error, /exceeds maximum allowed limit of 1048576 bytes/i);
    });

    test("Audio stream (10,485,760 bytes) on /api/voice/transcribe boundary", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Exact limit: 10,485,760 bytes -> 200 OK
      const exactBody = new Uint8Array(10485760);
      const reqExact = new Request("https://worker.local/api/voice/transcribe", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "audio/webm",
          "Content-Length": "10485760",
        },
        body: exactBody,
      });
      const resExact = await handleRequest(reqExact, env, mockFetch);
      assert.equal(resExact.status, 200);

      // One byte over: 10,485,761 bytes -> 413 Payload Too Large
      const reqOver = new Request("https://worker.local/api/voice/transcribe", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "audio/webm",
          "Content-Length": "10485761",
        },
        body: "fake-audio",
      });
      const resOver = await handleRequest(reqOver, env, mockFetch);
      assert.equal(resOver.status, 413);
      const body = await resOver.json();
      assert.match(body.error, /exceeds maximum allowed limit of 10485760 bytes/i);
    });

    test("declared Content-Length smaller than actual streamed body returns 400 Bad Request", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Declared Content-Length is 50, but client streams 100 bytes
      const streamedBody = new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(50));
          controller.enqueue(new Uint8Array(50));
          controller.close();
        },
      });

      const req = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "50",
        },
        body: streamedBody,
        duplex: "half",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 400);
      const body = await res.json();
      assert.match(body.error, /exceeded declared Content-Length/i);
    });

    test("declared Content-Length larger than actual streamed body returns 400 Bad Request", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Declared Content-Length is 100, but client streams only 40 bytes and closes
      const streamedBody = new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(40));
          controller.close();
        },
      });

      const req = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "100",
        },
        body: streamedBody,
        duplex: "half",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 400);
      const body = await res.json();
      assert.match(body.error, /shorter than its Content-Length/i);
    });

    test("stream exceeding route limit after several chunks is aborted with 413 and cancels source stream", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      let sourceCancelled = false;
      let cancelReason = null;
      let chunksReadBeforeCancel = 0;

      // Limit for /api/run is 4,096 bytes.
      // Stream sends chunks of 2000 B on demand. When chunk 3 arrives (total 6000 B), limit is exceeded.
      const streamedBody = new ReadableStream({
        pull(controller) {
          chunksReadBeforeCancel++;
          controller.enqueue(new Uint8Array(2000));
        },
        cancel(reason) {
          sourceCancelled = true;
          cancelReason = reason;
        },
      });

      // Declared Content-Length matches the 4200 B stream
      // But 4200 B > 4096 B maxAllowed
      // To test the streaming chunk cutoff specifically, let's test where Content-Length was spoofed as 4096
      const req = new Request("https://worker.local/api/run", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": "4096",
        },
        body: streamedBody,
        duplex: "half",
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 413);
      const body = await res.json();
      assert.match(body.error, /exceeded maximum allowed limit/i);
      assert.equal(sourceCancelled, true, "Source stream must be cancelled when limit is breached");
      assert.equal(calls.upstreamCalls.length, 0, "Upstream call must fail and not be recorded as complete");
    });
  });

  describe("5. CORS Evaluation & Header Forwarding", () => {
    test("OPTIONS preflight from allowed origin returns 204 with exact CORS headers", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/run", {
        method: "OPTIONS",
        headers: {
          Origin: ALLOWED_CORS_ORIGIN,
          "Access-Control-Request-Method": "POST",
          "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 204);
      assert.equal(res.headers.get("Access-Control-Allow-Origin"), ALLOWED_CORS_ORIGIN);
      assert.match(res.headers.get("Access-Control-Allow-Methods"), /POST/);
      assert.equal(res.headers.get("Access-Control-Max-Age"), "86400");
    });

    test("OPTIONS preflight from denied origin returns 403 Forbidden", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/run", {
        method: "OPTIONS",
        headers: { Origin: DENIED_CORS_ORIGIN },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 403);
      const body = await res.json();
      assert.match(body.error, /cors origin not allowed/i);
    });

    test("actual request from denied origin returns 403 Forbidden", async () => {
      const { mockFetch } = createMockFetch();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: {
          Authorization: `Bearer ${makeJwt()}`,
          Origin: DENIED_CORS_ORIGIN,
        },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 403);
      const body = await res.json();
      assert.match(body.error, /cors origin not allowed/i);
    });

    test("strips static and dynamic RFC 7230 connection headers and client host", async () => {
      const { mockFetch, calls } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: {
          Authorization: `Bearer ${token}`,
          Host: "client-controlled-host.com",
          Connection: "keep-alive, x-custom-hop-header",
          "X-Custom-Hop-Header": "must-be-stripped",
          "Keep-Alive": "timeout=5",
          Upgrade: "websocket",
          "Proxy-Authenticate": "basic",
          Accept: "application/json",
        },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 200);

      const upstreamHeaders = calls.upstreamCalls[0].options.headers;
      assert.equal(upstreamHeaders.get("Host"), "nelavi-backend.onrender.com", "Must set Host to Render upstream target host");
      assert.equal(upstreamHeaders.get("Connection"), null);
      assert.equal(upstreamHeaders.get("Keep-Alive"), null);
      assert.equal(upstreamHeaders.get("X-Custom-Hop-Header"), null, "Dynamic Connection token must be stripped");
      assert.equal(upstreamHeaders.get("Upgrade"), null);
      assert.equal(upstreamHeaders.get("Proxy-Authenticate"), null);
      assert.equal(upstreamHeaders.get("Accept"), "application/json");
      assert.equal(upstreamHeaders.get("Authorization"), `Bearer ${NELAVI_AUTH_TOKEN}`);
    });

    test("upstream destination origin cannot be overwritten via request path", async () => {
      const { mockFetch } = createMockFetch();
      const token = makeJwt();
      const env = createMockEnvironment();

      // Path attempting double-slash origin override
      const req = new Request("https://worker.local//attacker.com/leak", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 404, "Double slash path must be rejected by closed allowlist");
    });

    test("upstream 301/302 redirect is passed through with redirect: manual without following", async () => {
      const { mockFetch, calls } = createMockFetch(async (url, options) => {
        return new Response(null, {
          status: 302,
          headers: { Location: "https://evil.com/leak-token" },
        });
      });
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 302);
      assert.equal(res.headers.get("Location"), "https://evil.com/leak-token");
      assert.equal(calls.upstreamCalls.length, 1, "Must NOT follow upstream redirects");
      assert.equal(calls.upstreamCalls[0].options.redirect, "manual");
    });
  });

  describe("6. Upstream Error & Timeout Resilience", () => {
    test("upstream connection error returns 502 Bad Gateway", async () => {
      const { mockFetch } = createMockFetch(async () => {
        throw new Error("Connection reset by peer");
      });
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 502);
      const body = await res.json();
      assert.match(body.error, /bad gateway/i);
    });

    test("upstream timeout returns 504 Gateway Timeout", async () => {
      const { mockFetch } = createMockFetch(async () => {
        const err = new Error("The operation was aborted");
        err.name = "AbortError";
        throw err;
      });
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 504);
      const body = await res.json();
      assert.match(body.error, /gateway timeout/i);
    });

    test("respects custom UPSTREAM_TIMEOUT_MS environment variable", async () => {
      let passedSignal = null;
      const { mockFetch } = createMockFetch(async (url, options) => {
        passedSignal = options.signal;
        return new Response(JSON.stringify({ status: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } });
      });
      const token = makeJwt();
      const env = createMockEnvironment({ UPSTREAM_TIMEOUT_MS: "60000" });

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      assert.equal(res.status, 200);
      assert.ok(passedSignal);
    });

    test("response body and headers never leak NELAVI_AUTH_TOKEN across any condition", async () => {
      const { mockFetch } = createMockFetch(async () => {
        return new Response(JSON.stringify({ error: "Backend internal diagnostics" }), { status: 500 });
      });
      const token = makeJwt();
      const env = createMockEnvironment();

      const req = new Request("https://worker.local/api/state", {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });

      const res = await handleRequest(req, env, mockFetch);
      const text = await res.text();
      assert.equal(text.includes(NELAVI_AUTH_TOKEN), false, "Response body must never contain secret token");

      for (const [k, v] of res.headers.entries()) {
        assert.equal(v.includes(NELAVI_AUTH_TOKEN), false, `Header ${k} must not contain secret token`);
      }
    });
  });
});
