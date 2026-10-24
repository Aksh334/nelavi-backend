"""Request routing for the Nelavi local demo. No sockets here.

`DemoApp.handle_request` takes plain values (method, target, lower-cased headers, a
body-reading function) and returns a `Response`, so it can be tested without a network.
`demo_app.server` is the thin layer that connects it to Python's http.server.

Safeguards for a server that only listens on this machine:
* the Host header must be the server's own local address (blocks DNS-rebinding);
* every POST must carry the custom header `X-Nelavi-Demo: 1`, which a web page on
  another origin cannot send without a pre-flight request this server never allows;
* request bodies are bounded and a too-large body is refused before it is read;
* static files come from a fixed whitelist, never from a path in the request.
"""

import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from demo_app.demo_analyzer import MAX_IMAGE_BYTES
from demo_app.service import DemoError

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_JSON_BYTES = 4096
# A Second Opinion request carries a base64 PNG: 1,398,104 characters for the largest
# allowed image (1 MiB), plus 4,096 bytes for the readings, crop, language and JSON syntax.
MAX_SECOND_OPINION_BYTES = 1_402_200
CSRF_HEADER = "x-nelavi-demo"


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    allow: str = ""
    extra_headers: tuple = ()


def _json(status, payload, extra_headers=()):
    return Response(status, "application/json; charset=utf-8",
                    json.dumps(payload).encode("utf-8"),
                    extra_headers=extra_headers)


class DemoApp:
    def __init__(self, service, allowed_hosts, remote=False, auth_token=None,
                 cors_origins=None, allow_remote_real_providers=False):
        self._service = service
        self._allowed_hosts = frozenset(h.strip().lower() for h in allowed_hosts if h and h.strip())
        self._remote = bool(remote)
        self._auth_token = auth_token if auth_token else None
        self._cors_origins = frozenset(cors_origins or ())
        self._allow_remote_real_providers = bool(allow_remote_real_providers)
        self._static = {}
        for route, (filename, content_type) in STATIC_ROUTES.items():
            try:
                content = (STATIC_DIR / filename).read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    "demo file missing or unreadable: %s" % (filename,)) from exc
            self._static[route] = Response(200, content_type, content)

    def _attach_cors(self, headers, response):
        if not self._remote:
            return response
        origin = headers.get("origin")
        if origin and origin in self._cors_origins:
            cors_headers = (("Access-Control-Allow-Origin", origin), ("Vary", "Origin"))
            return Response(response.status, response.content_type, response.body,
                            allow=response.allow,
                            extra_headers=response.extra_headers + cors_headers)
        return response

    def handle_request(self, method, target, headers, read_body):
        try:
            return self._route(method, target, headers, read_body)
        except DemoError as exc:
            return self._attach_cors(headers, _json(exc.status, {"error": exc.message}))
        except TimeoutError:
            return self._attach_cors(headers, _json(408, {"error": "Request timed out."}))
        except Exception:
            # Never echo internals to the browser.
            return self._attach_cors(headers, _json(500, {"error": "Internal error in the demo application."}))

    # ---------------------------------------------------------- routing
    def _route(self, method, target, headers, read_body):
        req_host = (headers.get("host") or "").strip().lower()
        if req_host not in self._allowed_hosts:
            msg = "This demo only answers on its own local address." if not self._remote else "Host not allowed."
            return self._attach_cors(headers, _json(403, {"error": msg}))
        parts = urlsplit(target)
        path = parts.path

        if path in ("/health", "/api/health"):
            if method in ("GET", "HEAD"):
                return self._attach_cors(headers, Response(200, "application/json; charset=utf-8", b'{"status": "ok"}'))
            return self._attach_cors(headers, Response(405, "application/json; charset=utf-8",
                                                        json.dumps({"error": "Method not allowed."}).encode("utf-8"),
                                                        allow="GET, HEAD"))

        if method == "OPTIONS":
            if not self._remote:
                return Response(405, "application/json; charset=utf-8",
                                json.dumps({"error": "Method not allowed."}).encode("utf-8"),
                                allow="GET, POST")
            origin = headers.get("origin")
            if not origin or origin not in self._cors_origins:
                return _json(403, {"error": "CORS origin not allowed."})
            preflight_headers = (
                ("Access-Control-Allow-Origin", origin),
                ("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS"),
                ("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, X-Nelavi-Demo"),
                ("Access-Control-Max-Age", "86400"),
                ("Vary", "Origin"),
            )
            return Response(204, "text/plain; charset=utf-8", b"",
                            allow="GET, POST, HEAD, OPTIONS", extra_headers=preflight_headers)

        if self._remote and (path == "/" or path.startswith("/static/")):
            return self._attach_cors(headers, _json(404, {"error": "Not found."}))

        if self._remote:
            auth_header = headers.get("authorization")
            if not auth_header:
                return self._attach_cors(headers, _json(401, {"error": "Unauthorized: missing bearer token."}))
            scheme, sep, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not sep or not token.strip():
                return self._attach_cors(headers, _json(401, {"error": "Unauthorized: invalid authorization scheme."}))
            if not self._auth_token or not hmac.compare_digest(token.strip(), self._auth_token):
                return self._attach_cors(headers, _json(401, {"error": "Unauthorized: invalid bearer token."}))

        if method == "GET":
            if path in self._static:
                return self._static[path]
            if path == "/api/state":
                return self._attach_cors(headers, _json(200, self._service.state()))
            if path == "/api/history":
                return self._attach_cors(headers, _json(200, self._service.history(self._history_limit(parts.query))))
            if path == "/api/research-pro/status":
                return self._attach_cors(headers, self._research_pro_status(parts.query))
            if path == "/api/learning/lessons":
                return self._attach_cors(headers, self._learning_lessons(parts.query))
            if path == "/api/learning/lesson":
                return self._attach_cors(headers, self._learning_lesson(parts.query))
            return self._attach_cors(headers, _json(404, {"error": "Not found."}))
        if method == "POST":
            if path not in ("/api/run", "/api/analyze", "/api/second-opinion", "/api/advisory",
                            "/api/research-pro/start", "/api/run-orchestrated", "/api/run-integrated",
                            "/api/voice/transcribe", "/api/voice/synthesize", "/api/assistant/ask",
                            "/api/learning/progress"):
                return self._attach_cors(headers, _json(404, {"error": "Not found."}))
            if not self._remote and headers.get(CSRF_HEADER) != "1":
                return _json(403, {"error": "Missing the X-Nelavi-Demo header."})
            if path == "/api/run":
                return self._attach_cors(headers, self._run(headers, read_body))
            if path == "/api/run-orchestrated":
                return self._attach_cors(headers, self._run_orchestrated(headers, read_body))
            if path == "/api/run-integrated":
                return self._attach_cors(headers, self._run_integrated(headers, read_body))
            if path == "/api/research-pro/start":
                return self._attach_cors(headers, self._research_pro_start(headers, read_body))
            if path == "/api/advisory":
                return self._attach_cors(headers, self._advisory(headers, read_body))
            if path == "/api/second-opinion":
                return self._attach_cors(headers, self._opinion(headers, read_body))
            if path == "/api/analyze":
                return self._attach_cors(headers, self._analyze(headers, read_body))
            if path == "/api/voice/transcribe":
                return self._attach_cors(headers, self._voice_transcribe(headers, read_body, parts.query))
            if path == "/api/voice/synthesize":
                return self._attach_cors(headers, self._voice_synthesize(headers, read_body))
            if path == "/api/assistant/ask":
                return self._attach_cors(headers, self._assistant_ask(headers, read_body))
            if path == "/api/learning/progress":
                return self._attach_cors(headers, self._learning_progress(headers, read_body))
        return self._attach_cors(headers, Response(405, "application/json; charset=utf-8",
                                                    json.dumps({"error": "Method not allowed."}).encode("utf-8"),
                                                    allow="GET, POST"))

    @staticmethod
    def _history_limit(query):
        values = parse_qs(query).get("limit")
        if not values:
            return 20
        text = values[0]
        if not (text.isascii() and text.isdigit()):
            raise DemoError("limit must be a whole number.", 400)
        try:
            return int(text)
        except ValueError:
            # More digits than Python converts to an int (4,300 by default): not a usable limit, like any other.
            raise DemoError("limit must be a whole number.", 400) from None

    @staticmethod
    def _read_body(headers, read_body, limit):
        if "transfer-encoding" in headers:
            raise DemoError("Chunked uploads are not supported; send a Content-Length.", 411)
        raw = headers.get("content-length")
        if raw is None:
            raise DemoError("Content-Length is required.", 411)
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()):
            raise DemoError("Content-Length must be a whole number.", 400)
        try:
            length = int(raw)
        except ValueError:
            # More digits than Python converts to an int (4,300 by default): far above any body limit.
            raise DemoError("The request body is larger than the %d-byte limit." % limit, 413) from None
        if length > limit:
            raise DemoError("The request body is larger than the %d-byte limit." % limit, 413)
        body = read_body(length) if length else b""
        if len(body) != length:
            raise DemoError("The request body was shorter than its Content-Length.", 400)
        return body

    def _run(self, headers, read_body):
        body = self._read_body(headers, read_body, MAX_JSON_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        except RecursionError:  # same answer as the other JSON routes; the parser's depth limit is runtime-specific
            raise DemoError("The request body is nested too deeply.", 400) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("scenario"), str):
            raise DemoError('Send JSON like {"scenario": "<name>"}.', 400)
        # Optional statements by the user. Anything else in the body is ignored; the values are
        # validated by the service before anything runs.
        user = {}
        for key in ("crop", "region", "growth_stage"):
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise DemoError("%s must be text." % key.replace("_", " ").capitalize(), 400)
            if value.strip():
                user[key] = value
        return _json(200, self._service.run_scenario(payload["scenario"], user or None))

    def _run_orchestrated(self, headers, read_body):
        """The additive orchestration-based path (fake providers only). Status 200 whenever the workflow ran,
        even if a stage failed or was skipped: that is reported in the body. Bad input is 400."""
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, MAX_JSON_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        except RecursionError:
            raise DemoError("The request body is nested too deeply.", 400) from None
        return _json(200, self._service.run_orchestrated(payload))

    def _run_integrated(self, headers, read_body):
        """The additive integrated path: image findings, research, a compute summary and a synthesis in one run.
        The body may carry a base64 PNG, so it uses the same limit as the Second Opinion route. Status 200 whenever
        the workflow ran, even if a stage failed or was skipped: that is reported in the body. Bad input is 400."""
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, MAX_SECOND_OPINION_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        except RecursionError:
            raise DemoError("The request body is nested too deeply.", 400) from None
        if self._remote and not self._allow_remote_real_providers:
            stages = (payload.get("stages") or payload.get("providers")) if isinstance(payload, dict) else None
            if isinstance(stages, dict) and any(v == "real" for v in stages.values()):
                raise DemoError("Real provider stages are disabled in remote mode.", 403)
        return _json(200, self._service.run_integrated(payload))

    def _advisory(self, headers, read_body):
        if self._remote and not self._allow_remote_real_providers:
            if getattr(self._service, "_research", None) is not None:
                raise DemoError("Real provider execution is disabled in remote mode.", 403)
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, MAX_JSON_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        except RecursionError:
            raise DemoError("The request body is nested too deeply.", 400) from None
        report = self._service.get_advisory(payload)
        return _json(200 if report["status"] == "ok" else 422, report)

    def _research_pro_start(self, headers, read_body):
        if self._remote and not self._allow_remote_real_providers:
            if getattr(self._service, "_research_pro", None) is not None:
                raise DemoError("Real provider execution is disabled in remote mode.", 403)
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, MAX_JSON_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        except RecursionError:
            raise DemoError("The request body is nested too deeply.", 400) from None
        view = self._service.start_research_pro(payload)
        return _json(200 if view["status"] == "running" else 422, view)

    def _research_pro_status(self, query):
        values = parse_qs(query).get("task")
        if not values or len(values) != 1:
            raise DemoError("Send the task id as ?task=<id>.", 400)
        return _json(200, self._service.research_pro_status(values[0]))

    def _opinion(self, headers, read_body):
        if self._remote and not self._allow_remote_real_providers:
            if not getattr(self._service, "_second_opinion_simulated", True):
                raise DemoError("Real provider execution is disabled in remote mode.", 403)
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, MAX_SECOND_OPINION_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        except RecursionError:  # absurdly deep nesting; handled for this route only
            raise DemoError("The request body is nested too deeply.", 400) from None
        if not isinstance(payload, dict):
            raise DemoError("The request body must be a JSON object.", 400)
        result = self._service.get_second_opinion(payload)
        return _json(200 if result["error_code"] is None else 422, result)

    def _analyze(self, headers, read_body):
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "image/png":
            raise DemoError("Send the file as image/png.", 415)
        data = self._read_body(headers, read_body, MAX_IMAGE_BYTES)
        result = self._service.analyze_png(data, "image/png")
        return _json(200 if result["status"] == "ok" else 422, result)

    def _voice_transcribe(self, headers, read_body, query):
        if self._remote and not self._allow_remote_real_providers:
            if getattr(self._service, "_elevenlabs_client", None) is not None and getattr(self._service._elevenlabs_client, "key_present", lambda: False)():
                raise DemoError("Real provider execution is disabled in remote mode.", 403)
        content_type = (headers.get("content-type") or "audio/webm").split(";")[0].strip().lower()
        body = self._read_body(headers, read_body, 10 * 1024 * 1024)
        lang = parse_qs(query).get("lang", [None])[0] or headers.get("x-language")
        result = self._service.transcribe_speech(body, content_type, language_code=lang)
        status = 200 if result.get("status") == "ok" else 400
        return _json(status, result)

    def _voice_synthesize(self, headers, read_body):
        if self._remote and not self._allow_remote_real_providers:
            if getattr(self._service, "_elevenlabs_client", None) is not None and getattr(self._service._elevenlabs_client, "key_present", lambda: False)():
                raise DemoError("Real provider execution is disabled in remote mode.", 403)
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, 65536)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        if not isinstance(payload, dict) or "text" not in payload:
            raise DemoError('Send JSON like {"text": "...", "language": "en"}.', 400)
        result = self._service.synthesize_speech(
            text=payload["text"],
            voice_id=payload.get("voice_id"),
            language_code=payload.get("language")
        )
        status = 200 if result.get("status") == "ok" else 400
        return _json(status, result)

    def _assistant_ask(self, headers, read_body):
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, 65536)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        if not isinstance(payload, dict) or "question" not in payload:
            raise DemoError('Send JSON like {"question": "...", "language": "en"}.', 400)
        result = self._service.ask_assistant(
            question=payload["question"],
            language=payload.get("language", "en"),
            include_rover_data=bool(payload.get("include_rover_data", True))
        )
        status = 200 if result.get("status") == "ok" else 400
        return _json(status, result)

    def _learning_lessons(self, query):
        lang = parse_qs(query).get("lang", ["en"])[0]
        return _json(200, self._service.get_learning_lessons(language=lang))

    def _learning_lesson(self, query):
        params = parse_qs(query)
        lesson_id = params.get("id", [""])[0]
        lang = params.get("lang", ["en"])[0]
        if not lesson_id:
            raise DemoError("Query parameter 'id' is required.", 400)
        return _json(200, self._service.get_learning_lesson(lesson_id, language=lang))

    def _learning_progress(self, headers, read_body):
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise DemoError("Send the request as application/json.", 415)
        body = self._read_body(headers, read_body, 4096)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DemoError("The request body must be JSON.", 400) from None
        if not isinstance(payload, dict) or "lesson_id" not in payload:
            raise DemoError('Send JSON like {"lesson_id": "...", "completed": true}.', 400)
        result = self._service.update_lesson_progress(
            lesson_id=payload["lesson_id"],
            completed=bool(payload.get("completed", True))
        )
        return _json(200, result)
