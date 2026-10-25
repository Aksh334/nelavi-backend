"""Targeted offline tests for NELAVI M1 — Backend Security Readiness.

Covers:
  - Minimal health endpoint (/health and /api/health), unauthenticated in local and remote modes.
  - Remote mode opt-in flag and enforcement.
  - Bearer token authentication for all /api/* routes in remote mode (and constant-time comparison).
  - Rejection of X-Nelavi-Demo: 1 as authentication in remote mode.
  - Disabling of static web UI (/, /static/*) in remote mode while preserved in local mode.
  - Exact-origin CORS allowlisting and OPTIONS preflight for FlutterFlow web client.
  - Host header validation in remote mode.
  - Refusal of real provider stages by default in remote mode.
  - Server CLI validation, exit codes, and secret protection (no tokens or headers leaked).
  - Network tripwires (NoSockets base class).
"""

import base64
import io
import json
import unittest
from unittest import mock

from demo_app import server as server_module
from demo_app.app import DemoApp, MAX_JSON_BYTES, MAX_SECOND_OPINION_BYTES
from demo_app.service import DemoService
from image_analysis.boundary import AnalysisRequest, AnalysisResult, ImageInput
from storage.record_store import RecordStore

HOST = "127.0.0.1:8765"
REMOTE_HOST = "api.nelavi.app:8765"
ALLOWED_ORIGIN = "https://app.flutterflow.io"
DENIED_ORIGIN = "https://evil.example.com"
AUTH_TOKEN = "nelavi-secret-token-test-123"


class NoSockets(unittest.TestCase):
    """Base class: any attempt to open a socket during a test fails it."""

    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


def make_service(testcase, **kwargs):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    return DemoService(store, **kwargs), store


def make_local_app(service):
    return DemoApp(service, {HOST, "localhost:8765"})


def make_remote_app(service, auth_token=AUTH_TOKEN, allowed_hosts=(REMOTE_HOST,),
                    cors_origins=(ALLOWED_ORIGIN,), allow_remote_real_providers=False):
    return DemoApp(
        service,
        allowed_hosts=allowed_hosts,
        remote=True,
        auth_token=auth_token,
        cors_origins=cors_origins,
        allow_remote_real_providers=allow_remote_real_providers,
    )


def call(app, method, target, body=b"", headers=None, host=HOST):
    sent = {}
    if host is not None:
        sent["host"] = host
    if body and "content-length" not in (headers or {}):
        sent["content-length"] = str(len(body))
    sent.update(headers or {})
    return app.handle_request(method, target, sent, io.BytesIO(body).read)


def as_json(response):
    return json.loads(response.body.decode("utf-8"))


class FakeRealProvider:
    def analyze(self, analysis_request):
        return AnalysisResult("ok", "Keep the leaves dry.", None, "fake-model", ())


class TestHealthEndpoint(NoSockets):
    """Minimal health endpoint (/health, /api/health)."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)
        self.local_app = make_local_app(self.service)
        self.remote_app = make_remote_app(self.service)

    def test_health_unauthenticated_in_local_mode(self):
        for path in ("/health", "/api/health"):
            resp = call(self.local_app, "GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertEqual(as_json(resp), {"status": "ok"})
            self.assertEqual(resp.content_type, "application/json; charset=utf-8")

    def test_health_unauthenticated_in_remote_mode(self):
        for path in ("/health", "/api/health"):
            # No Authorization header sent; must still succeed
            resp = call(self.remote_app, "GET", path, host=REMOTE_HOST)
            self.assertEqual(resp.status, 200, path)
            self.assertEqual(as_json(resp), {"status": "ok"})

    def test_health_head_method(self):
        resp = call(self.local_app, "HEAD", "/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b'{"status": "ok"}')

    def test_health_disallowed_methods(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            resp = call(self.local_app, method, "/health")
            self.assertEqual(resp.status, 405)
            self.assertEqual(resp.allow, "GET, HEAD")
            self.assertEqual(as_json(resp), {"error": "Method not allowed."})

    def test_health_get_and_head_exempt_from_host_validation(self):
        unexpected_hosts = (
            "localhost",
            "127.0.0.1:10000",
            "evil.example.com",
            "render-internal-probe:8080",
            "",
            None,
        )
        for app, app_label in ((self.remote_app, "remote"), (self.local_app, "local")):
            for path in ("/health", "/api/health"):
                for method in ("GET", "HEAD"):
                    for host in unexpected_hosts:
                        resp = call(app, method, path, host=host)
                        self.assertEqual(resp.status, 200, f"{app_label} {method} {path} with host={host}")
                        self.assertEqual(resp.body, b'{"status": "ok"}')
                        self.assertEqual(resp.content_type, "application/json; charset=utf-8")

    def test_health_unsupported_methods_do_not_gain_host_exemption(self):
        # Unsupported methods with an unexpected Host header must still be rejected
        # by the normal Host validation (403), not bypassed to 405.
        for path in ("/health", "/api/health"):
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                # Remote mode with disallowed host -> 403 Host not allowed
                resp_remote = call(self.remote_app, method, path, host="evil.com")
                self.assertEqual(resp_remote.status, 403, f"remote {method} {path}")
                self.assertEqual(as_json(resp_remote), {"error": "Host not allowed."})

                # Local mode with disallowed host -> 403
                resp_local = call(self.local_app, method, path, host="evil.com")
                self.assertEqual(resp_local.status, 403, f"local {method} {path}")
                self.assertEqual(as_json(resp_local), {"error": "This demo only answers on its own local address."})

                # Allowed host with unsupported method -> 405 Method not allowed
                resp_remote_allowed = call(self.remote_app, method, path, host=REMOTE_HOST)
                self.assertEqual(resp_remote_allowed.status, 405, f"remote allowed {method} {path}")
                self.assertEqual(resp_remote_allowed.allow, "GET, HEAD")

                resp_local_allowed = call(self.local_app, method, path, host=HOST)
                self.assertEqual(resp_local_allowed.status, 405, f"local allowed {method} {path}")
                self.assertEqual(resp_local_allowed.allow, "GET, HEAD")


class TestStaticWebUIDisablement(NoSockets):
    """Static web UI (/, /static/*) must be disabled in remote mode, preserved in local mode."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)
        self.local_app = make_local_app(self.service)
        self.remote_app = make_remote_app(self.service)

    def test_local_mode_serves_static_files(self):
        for path, ctype in (("/", "text/html"),
                            ("/static/app.js", "text/javascript"),
                            ("/static/style.css", "text/css")):
            resp = call(self.local_app, "GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertTrue(resp.content_type.startswith(ctype))

    def test_remote_mode_disables_static_files(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            resp = call(self.remote_app, "GET", path, host=REMOTE_HOST,
                        headers={"authorization": "Bearer " + AUTH_TOKEN})
            self.assertEqual(resp.status, 404, path)
            self.assertEqual(as_json(resp), {"error": "Not found."})


class TestBearerAuthentication(NoSockets):
    """Bearer-token authentication for protected API routes in remote mode."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)
        self.app = make_remote_app(self.service)

    def test_missing_authorization_header_rejected(self):
        routes = (
            ("GET", "/api/state", b""),
            ("GET", "/api/history", b""),
            ("POST", "/api/run", b'{"scenario": "normal_operation"}'),
            ("POST", "/api/run-orchestrated", b'{"scenario": "normal_operation"}'),
            ("POST", "/api/run-integrated", b'{"scenario": "normal_operation"}'),
        )
        for method, route, body in routes:
            resp = call(self.app, method, route, body=body, host=REMOTE_HOST,
                        headers={"content-type": "application/json"})
            self.assertEqual(resp.status, 401, (method, route))
            self.assertEqual(as_json(resp), {"error": "Unauthorized: missing bearer token."})

    def test_invalid_authorization_scheme_rejected(self):
        bad_headers = (
            {"authorization": "Basic dXNlcjpwYXNz"},
            {"authorization": "Token " + AUTH_TOKEN},
            {"authorization": "Bearer"},
            {"authorization": "Bearer   "},
            {"authorization": "Bearer"},
            {"authorization": "badscheme"},
        )
        for h in bad_headers:
            resp = call(self.app, "GET", "/api/state", host=REMOTE_HOST, headers=h)
            self.assertEqual(resp.status, 401, h)
            self.assertEqual(as_json(resp), {"error": "Unauthorized: invalid authorization scheme."})

    def test_wrong_bearer_token_rejected(self):
        for wrong in ("wrong-token", AUTH_TOKEN + "x", AUTH_TOKEN[:-1], "Bearer"):
            resp = call(self.app, "GET", "/api/state", host=REMOTE_HOST,
                        headers={"authorization": "Bearer " + wrong})
            self.assertEqual(resp.status, 401, wrong)
            self.assertEqual(as_json(resp), {"error": "Unauthorized: invalid bearer token."})

    def test_valid_bearer_token_accepted_on_all_protected_routes(self):
        valid_auth = {"authorization": "Bearer " + AUTH_TOKEN, "content-type": "application/json"}
        # State
        self.assertEqual(call(self.app, "GET", "/api/state", host=REMOTE_HOST, headers=valid_auth).status, 200)
        # History
        self.assertEqual(call(self.app, "GET", "/api/history", host=REMOTE_HOST, headers=valid_auth).status, 200)
        # Run
        run_resp = call(self.app, "POST", "/api/run", b'{"scenario": "normal_operation"}',
                        host=REMOTE_HOST, headers=valid_auth)
        self.assertEqual(run_resp.status, 200)
        # Run orchestrated
        orch_resp = call(self.app, "POST", "/api/run-orchestrated", b'{"scenario": "normal_operation"}',
                         host=REMOTE_HOST, headers=valid_auth)
        self.assertEqual(orch_resp.status, 200)

    def test_bearer_case_insensitive_scheme(self):
        for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
            resp = call(self.app, "GET", "/api/state", host=REMOTE_HOST,
                        headers={"authorization": "%s %s" % (scheme, AUTH_TOKEN)})
            self.assertEqual(resp.status, 200, scheme)

    def test_x_nelavi_demo_header_alone_is_never_treated_as_auth_in_remote_mode(self):
        # Even with X-Nelavi-Demo: 1, remote mode MUST require a valid Bearer token
        headers = {"x-nelavi-demo": "1", "content-type": "application/json"}
        resp = call(self.app, "POST", "/api/run", b'{"scenario": "normal_operation"}',
                    host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 401)
        self.assertEqual(as_json(resp), {"error": "Unauthorized: missing bearer token."})

        # X-Nelavi-Demo: 1 with wrong token is still 401
        headers["authorization"] = "Bearer wrong-token"
        resp = call(self.app, "POST", "/api/run", b'{"scenario": "normal_operation"}',
                    host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 401)
        self.assertEqual(as_json(resp), {"error": "Unauthorized: invalid bearer token."})


class TestCORSAndPreflight(NoSockets):
    """Exact-origin CORS allowlisting and OPTIONS preflight."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)
        self.remote_app = make_remote_app(self.service, cors_origins=(ALLOWED_ORIGIN,))
        self.local_app = make_local_app(self.service)

    def test_options_preflight_allowed_origin(self):
        resp = call(self.remote_app, "OPTIONS", "/api/run", host=REMOTE_HOST,
                    headers={"origin": ALLOWED_ORIGIN,
                             "access-control-request-method": "POST",
                             "access-control-request-headers": "authorization, content-type"})
        self.assertEqual(resp.status, 204)
        header_dict = dict(resp.extra_headers)
        self.assertEqual(header_dict.get("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)
        self.assertIn("POST", header_dict.get("Access-Control-Allow-Methods", ""))
        self.assertIn("HEAD", header_dict.get("Access-Control-Allow-Methods", ""))
        self.assertIn("Authorization", header_dict.get("Access-Control-Allow-Headers", ""))
        self.assertIn("Accept", header_dict.get("Access-Control-Allow-Headers", ""))
        self.assertEqual(header_dict.get("Vary"), "Origin")

    def test_options_preflight_denied_origin(self):
        resp = call(self.remote_app, "OPTIONS", "/api/run", host=REMOTE_HOST,
                    headers={"origin": DENIED_ORIGIN})
        self.assertEqual(resp.status, 403)
        self.assertEqual(as_json(resp), {"error": "CORS origin not allowed."})
        header_dict = dict(resp.extra_headers)
        self.assertNotIn("Access-Control-Allow-Origin", header_dict)

    def test_options_preflight_missing_origin(self):
        resp = call(self.remote_app, "OPTIONS", "/api/run", host=REMOTE_HOST)
        self.assertEqual(resp.status, 403)
        self.assertEqual(as_json(resp), {"error": "CORS origin not allowed."})

    def test_options_in_local_mode_is_method_not_allowed(self):
        resp = call(self.local_app, "OPTIONS", "/api/run", host=HOST)
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.allow, "GET, POST")

    def test_actual_requests_attach_cors_headers_when_origin_matches(self):
        headers = {"authorization": "Bearer " + AUTH_TOKEN, "origin": ALLOWED_ORIGIN}
        resp = call(self.remote_app, "GET", "/api/state", host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 200)
        header_dict = dict(resp.extra_headers)
        self.assertEqual(header_dict.get("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)
        self.assertEqual(header_dict.get("Vary"), "Origin")

    def test_actual_requests_do_not_attach_cors_when_origin_mismatches(self):
        headers = {"authorization": "Bearer " + AUTH_TOKEN, "origin": DENIED_ORIGIN}
        resp = call(self.remote_app, "GET", "/api/state", host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 200)
        header_dict = dict(resp.extra_headers)
        self.assertNotIn("Access-Control-Allow-Origin", header_dict)

    def test_cors_does_not_bypass_authentication_on_actual_request(self):
        headers = {"origin": ALLOWED_ORIGIN}  # allowed origin, but NO token
        resp = call(self.remote_app, "GET", "/api/state", host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 401)
        self.assertEqual(as_json(resp), {"error": "Unauthorized: missing bearer token."})
        # Even on error, CORS header may be attached for browser to read the error
        header_dict = dict(resp.extra_headers)
        self.assertEqual(header_dict.get("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)


class TestHostHeaderValidation(NoSockets):
    """Host header validation in local vs remote mode."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)
        self.local_app = make_local_app(self.service)
        self.remote_app = make_remote_app(self.service, allowed_hosts={"api.nelavi.app:8765", "api.nelavi.app"})

    def test_local_mode_only_answers_local_host(self):
        # Non-health routes (/ and /api/state) must strictly enforce local Host
        self.assertEqual(call(self.local_app, "GET", "/", host=HOST).status, 200)
        self.assertEqual(call(self.local_app, "GET", "/", host="localhost:8765").status, 200)
        self.assertEqual(call(self.local_app, "GET", "/", host="evil.com").status, 403)
        self.assertEqual(call(self.local_app, "GET", "/", host=None).status, 403)

        self.assertEqual(call(self.local_app, "GET", "/api/state", host=HOST).status, 200)
        self.assertEqual(call(self.local_app, "GET", "/api/state", host="evil.com").status, 403)
        self.assertEqual(call(self.local_app, "GET", "/api/state", host=None).status, 403)

    def test_remote_mode_only_answers_allowed_hosts(self):
        headers = {"authorization": "Bearer " + AUTH_TOKEN}
        # Allowed hosts succeed on /api/state and /api/history
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="api.nelavi.app:8765", headers=headers).status, 200)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="api.nelavi.app", headers=headers).status, 200)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="API.NELAVI.APP:8765", headers=headers).status, 200)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="API.NELAVI.APP", headers=headers).status, 200)
        self.assertEqual(call(self.remote_app, "GET", "/api/history", host="api.nelavi.app:8765", headers=headers).status, 200)

        # Unexpected hosts rejected with 403 on /api/state
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="evil.com", headers=headers).status, 403)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host=HOST, headers=headers).status, 403)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="localhost", headers=headers).status, 403)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host="127.0.0.1:10000", headers=headers).status, 403)
        self.assertEqual(call(self.remote_app, "GET", "/api/state", host=None, headers=headers).status, 403)

        # Unexpected hosts rejected with 403 on other protected routes (/api/history and /api/run)
        self.assertEqual(call(self.remote_app, "GET", "/api/history", host="evil.com", headers=headers).status, 403)
        self.assertEqual(call(self.remote_app, "GET", "/api/history", host="localhost", headers=headers).status, 403)
        run_headers = {"authorization": "Bearer " + AUTH_TOKEN, "content-type": "application/json"}
        self.assertEqual(call(self.remote_app, "POST", "/api/run", b'{"scenario": "normal_operation"}', host="evil.com", headers=run_headers).status, 403)
        self.assertEqual(call(self.remote_app, "POST", "/api/run", b'{"scenario": "normal_operation"}', host="localhost", headers=run_headers).status, 403)


class TestRemoteRealProviderGuard(NoSockets):
    """Real provider stages disabled by default in remote mode."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)

    def test_run_integrated_refuses_real_stage_in_remote_mode_by_default(self):
        app = make_remote_app(self.service, allow_remote_real_providers=False)
        headers = {"authorization": "Bearer " + AUTH_TOKEN, "content-type": "application/json"}
        payload = {
            "scenario": "normal_operation",
            "providers": {"image": "real", "research": "skip", "compute": "skip", "synthesis": "skip"}
        }
        resp = call(app, "POST", "/api/run-integrated", json.dumps(payload).encode("utf-8"),
                    host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 403)
        self.assertEqual(as_json(resp), {"error": "Real provider stages are disabled in remote mode."})

    def test_run_integrated_allows_fake_and_skip_stages_in_remote_mode(self):
        app = make_remote_app(self.service, allow_remote_real_providers=False)
        headers = {"authorization": "Bearer " + AUTH_TOKEN, "content-type": "application/json"}
        png = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes"
        payload = {
            "scenario": "normal_operation",
            "image_png_base64": base64.b64encode(png).decode("ascii"),
            "providers": {"image": "fake", "research": "skip", "compute": "skip", "synthesis": "skip"}
        }
        resp = call(app, "POST", "/api/run-integrated", json.dumps(payload).encode("utf-8"),
                    host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 200)

    def test_second_opinion_refuses_real_provider_in_remote_mode_by_default(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        service = DemoService(store, second_opinion_provider=FakeRealProvider(), second_opinion_simulated=False)
        app = make_remote_app(service, allow_remote_real_providers=False)
        headers = {"authorization": "Bearer " + AUTH_TOKEN, "content-type": "application/json"}
        resp = call(app, "POST", "/api/second-opinion", b"{}", host=REMOTE_HOST, headers=headers)
        self.assertEqual(resp.status, 403)
        self.assertEqual(as_json(resp), {"error": "Real provider execution is disabled in remote mode."})


class TestServerCLIValidation(NoSockets):
    """Server CLI arguments and remote configuration validation."""

    def test_remote_requires_allowed_hosts(self):
        stderr = io.StringIO()
        code = server_module.main(["--remote", "--port", "8999"], stderr=stderr)
        self.assertEqual(code, 2)
        self.assertIn("requires --allowed-hosts", stderr.getvalue())

    def test_remote_requires_auth_token_in_env(self):
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True):
            code = server_module.main(["--remote", "--allowed-hosts", "api.example.com", "--port", "8999"],
                                      stderr=stderr)
            self.assertEqual(code, 2)
            self.assertIn("requires the NELAVI_AUTH_TOKEN variable to be set", stderr.getvalue())

    def test_allowed_hosts_disallowed_without_remote(self):
        stderr = io.StringIO()
        code = server_module.main(["--allowed-hosts", "api.example.com"], stderr=stderr)
        self.assertEqual(code, 2)
        self.assertIn("--allowed-hosts only applies with --remote", stderr.getvalue())

    def test_cors_origins_disallowed_without_remote(self):
        stderr = io.StringIO()
        code = server_module.main(["--cors-origins", "https://app.flutterflow.io"], stderr=stderr)
        self.assertEqual(code, 2)
        self.assertIn("--cors-origins only applies with --remote", stderr.getvalue())

    def test_cors_wildcard_rejected(self):
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"NELAVI_AUTH_TOKEN": "test-token"}):
            code = server_module.main(["--remote", "--allowed-hosts", "api.example.com",
                                       "--cors-origins", "*", "--port", "8999"], stderr=stderr)
            self.assertEqual(code, 2)
            self.assertIn("wildcards like '*' are not allowed", stderr.getvalue())

    def test_remote_real_providers_refused_without_opt_in(self):
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"NELAVI_AUTH_TOKEN": "test-token", "NVIDIA_API_KEY": "fake-key"}):
            code = server_module.main(["--remote", "--allowed-hosts", "api.example.com",
                                       "--second-opinion-provider", "nvidia", "--port", "8999"],
                                      stderr=stderr)
            self.assertEqual(code, 2)
            self.assertIn("real provider stages are disabled by default in remote mode", stderr.getvalue())

    def test_startup_banner_never_prints_token(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        secret_token = "ultra-secret-test-token-xyz"
        with mock.patch.dict("os.environ", {"NELAVI_AUTH_TOKEN": secret_token}), \
             mock.patch("demo_app.server.HTTPServer") as mock_server:
            instance = mock.MagicMock()
            instance.server_address = ("0.0.0.0", 8765)
            mock_server.return_value = instance
            code = server_module.main(["--remote", "--allowed-hosts", "api.example.com",
                                       "--cors-origins", "https://app.flutterflow.io"],
                                      stdout=stdout, stderr=stderr)
            out = stdout.getvalue()
            self.assertIn("NELAVI REMOTE ACCESS MODE", out)
            self.assertIn("NELAVI_AUTH_TOKEN", out)
            self.assertNotIn(secret_token, out)


class TestBodyAndTimeoutProtections(NoSockets):
    """Protections against malformed Content-Length, oversized bodies, and body read timeouts."""

    def setUp(self):
        super().setUp()
        self.service, _ = make_service(self)
        self.remote_app = make_remote_app(self.service)

    def test_content_length_whitespace_padded_is_accepted(self):
        headers = {
            "authorization": "Bearer " + AUTH_TOKEN,
            "content-type": "application/json",
            "content-length": "  32  ",
        }
        resp = self.remote_app.handle_request(
            "POST",
            "/api/run",
            {"host": REMOTE_HOST, **headers},
            io.BytesIO(b'{"scenario": "normal_operation"}').read,
        )
        self.assertEqual(resp.status, 200)

    def test_content_length_malformed_or_negative_rejected(self):
        for bad_cl in ("-1", "not-a-number", "+10", "10, 10", "1.5"):
            headers = {
                "authorization": "Bearer " + AUTH_TOKEN,
                "content-type": "application/json",
                "content-length": bad_cl,
            }
            resp = self.remote_app.handle_request(
                "POST",
                "/api/run",
                {"host": REMOTE_HOST, **headers},
                io.BytesIO(b'{"scenario": "normal_operation"}').read,
            )
            self.assertEqual(resp.status, 400, bad_cl)
            self.assertEqual(as_json(resp), {"error": "Content-Length must be a whole number."})

    def test_body_read_timeout_returns_http_408(self):
        def timing_out_reader(length):
            raise TimeoutError("Socket timed out reading client body")

        headers = {
            "authorization": "Bearer " + AUTH_TOKEN,
            "content-type": "application/json",
            "content-length": "32",
        }
        resp = self.remote_app.handle_request(
            "POST",
            "/api/run",
            {"host": REMOTE_HOST, **headers},
            timing_out_reader,
        )
        self.assertEqual(resp.status, 408)
        self.assertEqual(as_json(resp), {"error": "Request timed out."})


if __name__ == "__main__":
    unittest.main()
