"""Connects DemoApp to Python's standard http.server, bound to this machine only."""

import argparse
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from demo_app.app import DemoApp
from demo_app.service import (
    ERROR_BODY_MAX_BYTES,
    fingerprint_of,
    OBS_BAD_STATUS,
    OBS_HTTP_RESPONSE,
    OBS_MAX_SECONDS,
    OBS_NO_HTTP_RESPONSE,
    OBS_NOT_SENT,
    DemoService,
)
from image_analysis.nvidia import (
    DEFAULT_TIMEOUT_SECONDS,
    NvidiaConfig,
    NvidiaImageAnalyzer,
    ResponseTooLarge,
    TransportError,
    TransportTimeout,
    api_key_present,
    urllib_transport,
)
from image_analysis.nvidia_headers import HeaderCapturingTransport
from research.tavily import TavilyResearch
from research.tavily import key_present as tavily_key_present
from research.tavily_research import APPROVED_DOMAIN, DEADLINE_SECONDS, TavilyResearchPro
from research.tavily_research import key_present as tavily_pro_key_present
from storage.record_store import RecordStore
from voice_assistant.elevenlabs import DEFAULT_KEY_ENV as ELEVENLABS_DEFAULT_KEY_ENV
from voice_assistant.elevenlabs import ElevenLabsClient
from synthesis_provider.chat import (
    DEFAULT_KEY_ENV_VAR as SYNTHESIS_DEFAULT_KEY_ENV,
)
from synthesis_provider.chat import (
    ChatSynthesisProvider,
    endpoint_host,
    endpoint_is_valid,
    key_env_name_is_valid,
    model_is_valid,
)
from synthesis_provider.chat import key_present as synthesis_key_present
from synthesis_provider.openrouter import (
    DEFAULT_KEY_ENV_VAR as OPENROUTER_DEFAULT_KEY_ENV,
)
from synthesis_provider.openrouter import (
    ENDPOINT as OPENROUTER_ENDPOINT,
)
from synthesis_provider.openrouter import (
    OpenRouterSynthesisProvider,
)
from synthesis_provider.openrouter import (
    SELECTED_MODEL as OPENROUTER_SELECTED_MODEL,
)
from synthesis_provider.openrouter import model_is_valid as openrouter_model_is_valid

HOST ="127.0.0.1"  # loopback only; there is deliberately no option to change it
DEFAULT_PORT = 8765
PROVIDER_SIMULATED = "simulated"
PROVIDER_NVIDIA = "nvidia"
PROVIDER_CHOICES = (PROVIDER_SIMULATED, PROVIDER_NVIDIA)
RESEARCH_NONE = "none"
RESEARCH_TAVILY = "tavily"
RESEARCH_TAVILY_PRO = "tavily-pro"
RESEARCH_CHOICES = (RESEARCH_NONE, RESEARCH_TAVILY, RESEARCH_TAVILY_PRO)
FINDINGS_NONE = "none"
FINDINGS_NVIDIA = "nvidia"
FINDINGS_CHOICES = (FINDINGS_NONE, FINDINGS_NVIDIA)
SYNTHESIS_NONE = "none"
SYNTHESIS_CHAT = "chat"
SYNTHESIS_OPENROUTER = "openrouter"
SYNTHESIS_CHOICES = (SYNTHESIS_NONE, SYNTHESIS_CHAT, SYNTHESIS_OPENROUTER)
# Upper bound for --findings-timeout. The server is single-threaded, so the whole demo waits while a real request is
# open; five minutes is a local choice, not an NVIDIA limit. The default (no flag) stays the adapter's 60 seconds.
FINDINGS_TIMEOUT_MAX_SECONDS = 300.0
# The only sources research may use: the owner-agreed list. `--research-domains agreed` expands to exactly this, and
# any other host name given to --research-domains is refused at startup (a true subdomain of one of these is fine).
AGREED_RESEARCH_DOMAINS = ("icar.org.in", "agritech.tnau.ac.in", "fao.org")
AGREED_SHORTCUT = "agreed"
THINKING_OFF_FIELD = "chat_template_kwargs"  # top level of the request body, as the findings smoke script adds it
THINKING_OFF_VALUE = {"enable_thinking": False}


def _is_agreed_domain(host):
    """True if `host` (already lower-case and validated) is an agreed domain or a true subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in AGREED_RESEARCH_DOMAINS)


def parse_findings_timeout(text):
    """Return the timeout in seconds, or None if `text` is not a finite number above 0 and at most the maximum.
    NaN and infinity fail the comparison, so they are refused too."""
    if not isinstance(text, str):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 0 < value <= FINDINGS_TIMEOUT_MAX_SECONDS else None


# Fixed lower-case fragments, checked in this order. They are guesses at provider wording, not a documented contract (the
# only wording recorded in this project is PC-1's "ResourceExhausted ... limit reached"); a body that matches none is
# "unclassified". A fragment decides the class; no part of the body is kept.
_RESOURCE_EXHAUSTED = ("resourceexhausted", "resource_exhausted", "resource exhausted")
_CAPACITY_OR_RATE = ("request limit", "rate limit", "too many requests", "capacity", "overloaded", "queue")
_GATEWAY_OR_UNAVAILABLE = ("service unavailable", "bad gateway", "gateway", "upstream", "temporarily unavailable")


def classify_error_body(body):
    """Return {"body_class", "body_bytes"} for the body of an HTTP 5xx reply, or None if `body` is not bytes. Only a
    closed class word and the length are returned; the body is never stored, sliced or echoed."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    size = min(len(body), ERROR_BODY_MAX_BYTES)
    data = bytes(body)
    if data.strip() == b"":
        return {"body_class": "empty", "body_bytes": size}
    try:
        text = data.decode("utf-8").lower()
    except UnicodeDecodeError:
        return {"body_class": "not_text", "body_bytes": size}
    for name, fragments in (("resource_exhausted", _RESOURCE_EXHAUSTED), ("capacity_or_rate_limit", _CAPACITY_OR_RATE),
                            ("gateway_or_unavailable", _GATEWAY_OR_UNAVAILABLE)):
        if any(fragment in text for fragment in fragments):
            return {"body_class": name, "body_bytes": size}
    return {"body_class": "unclassified", "body_bytes": size}


def status_kind(status):
    """A closed word for an HTTP error status by the standard meaning of the code alone. It says nothing about why the
    service answered that way, and nothing about the account."""
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "authentication_or_permission"
    if status == 402:
        return "payment_required"
    if status == 408:
        return "request_timeout"
    if 400 <= status < 500:
        return "client_error"
    if status == 503:
        return "server_unavailable"
    if status in (502, 504):
        return "gateway_error"
    return "server_error"


class ObservingTransport:
    """Wraps the real NVIDIA transport for diagnostics only. It passes the request through (optionally adding the
    reasoning-off field when asked to), returns the result or raises the very same exception, and remembers ONLY:
    whether a request reached it, the elapsed time, the numeric HTTP status when a response arrived, or a closed
    failure category chosen from the exception class when none did. It never stores or returns a body, header, key,
    prompt or exception message, and it adds no retry.

    A timeout or connection error that happens while the reply body is being read is seen as "no HTTP response",
    because the real transport raises instead of returning the status."""

    def __init__(self, inner=None, thinking_off=False, clock=time.monotonic):
        self._inner = inner if inner is not None else urllib_transport
        self._thinking_off = bool(thinking_off)
        self._clock = clock
        self._observation = None
        self._error_class = None
        self._fingerprint = None
        self._reply_inspector = None
        self._reply_facts = None
        self.reset()

    def reset(self):
        self._observation = self._blank()
        self._error_class = None
        self._fingerprint = None
        self._reply_facts = None

    def set_reply_inspector(self, inspector):
        """Install a callable that is given the raw bytes of an HTTP success reply and returns closed facts about it
        (IA-11). It is called inside the request, its return value is the only thing kept, and the reply is never
        stored. The service installs it, because only the service may import the findings module."""
        self._reply_inspector = inspector if callable(inspector) else None

    def reply_facts(self):
        """For the last request only: the closed facts the inspector returned for an HTTP success reply, or None. A
        copy; it holds no text of the reply."""
        return dict(self._reply_facts) if isinstance(self._reply_facts, dict) else None

    def request_fingerprint(self):
        """For the last request only: {"bytes", "sha256_prefix"} of the body this observer handed to the transport
        (after the optional reasoning-off field was added), or None if no request reached it. A length and 16 hex
        digits only; a copy."""
        return dict(self._fingerprint) if self._fingerprint is not None else None

    def error_class(self):
        """For the last request only: what an HTTP error reply (status 400 or above) was, as closed words and bounded
        numbers: the status kind, the class and length of its body (see `classify_error_body`) and the form of any
        `Retry-After` header; None if no error reply was seen. It is a copy and never holds any text of the reply."""
        return dict(self._error_class) if self._error_class is not None else None

    def _describe_error(self, status, body):
        found = classify_error_body(body)
        if found is None:
            return None
        getter = getattr(self._inner, "retry_after_info", None)
        info = getter() if callable(getter) else None
        if not isinstance(info, dict):
            info = {"retry_after": "not_captured", "retry_after_seconds": None}
        return {"status_kind": status_kind(status), "body_class": found["body_class"],
                "body_bytes": found["body_bytes"], "retry_after": info.get("retry_after"),
                "retry_after_seconds": info.get("retry_after_seconds")}

    def _blank(self):
        return {"request_made": False, "outcome": OBS_NOT_SENT, "http_status": None, "failure": None,
                "elapsed_seconds": None, "timeout_seconds": None, "thinking_off": self._thinking_off,
                "thinking_off_applied": False}

    def snapshot(self):
        return dict(self._observation)

    def _add_thinking_off(self, body):
        """Return (body, applied). Anything but a JSON object without the field is passed on unchanged."""
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            return body, False
        if not isinstance(document, dict) or THINKING_OFF_FIELD in document:
            return body, False
        document[THINKING_OFF_FIELD] = dict(THINKING_OFF_VALUE)
        return json.dumps(document).encode("utf-8"), True

    def _finish(self, started, outcome, status=None, failure=None):
        elapsed = self._clock() - started
        elapsed = round(min(max(elapsed, 0.0), OBS_MAX_SECONDS), 1)
        self._observation.update(outcome=outcome, http_status=status, failure=failure, elapsed_seconds=elapsed)

    def __call__(self, url, headers, body, timeout):
        applied = False
        self._error_class = None
        self._fingerprint = None
        self._reply_facts = None
        if self._thinking_off:
            body, applied = self._add_thinking_off(body)
        try:
            self._fingerprint = fingerprint_of(body)   # of exactly what the transport below is handed
        except Exception:
            self._fingerprint = None                    # a diagnostic must never change the request
        limit = float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) \
            and 0 <= timeout <= OBS_MAX_SECONDS else None
        self._observation = dict(self._blank(), request_made=True, timeout_seconds=limit, thinking_off_applied=applied)
        started = self._clock()
        try:
            status, response_body = self._inner(url, headers, body, timeout)
        except ResponseTooLarge:
            self._finish(started, OBS_NO_HTTP_RESPONSE, failure="response_too_large")
            raise
        except (TransportTimeout, TimeoutError):
            self._finish(started, OBS_NO_HTTP_RESPONSE, failure="timeout")
            raise
        except (TransportError, OSError):
            self._finish(started, OBS_NO_HTTP_RESPONSE, failure="connection_failed")
            raise
        except Exception:
            self._finish(started, OBS_NO_HTTP_RESPONSE, failure="other_transport_error")
            raise
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            if status >= 400:
                try:
                    self._error_class = self._describe_error(status, response_body)
                except Exception:
                    self._error_class = None   # a diagnostic must never change the result
            elif 200 <= status < 300 and self._reply_inspector is not None:
                try:
                    self._reply_facts = self._reply_inspector(response_body)
                except Exception:
                    self._reply_facts = None   # a diagnostic must never change the result
            self._finish(started, OBS_HTTP_RESPONSE, status=status)
        else:
            self._finish(started, OBS_BAD_STATUS)
        return status, response_body

SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
     "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
)


def _read_auth_token(env_var_name):
    """Read the server's own bearer authentication token from the terminal variables.
    Never prints, logs, or echoes the value. Returns the non-empty stripped token or None."""
    if not isinstance(env_var_name, str) or not env_var_name.strip():
        return None
    val = os.environ.get(env_var_name)
    if val is None or not val.strip():
        return None
    return val.strip()


def make_handler(app_holder):
    """Build a request-handler class. `app_holder.app` is the DemoApp to call."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "NelaviDemo"
        sys_version = ""

        def setup(self):
            super().setup()
            if hasattr(self.connection, "settimeout"):
                try:
                    self.connection.settimeout(30.0)
                except Exception:
                    pass

        def log_message(self, format, *args):  # keep the console quiet
            pass

        def _dispatch(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                response = app_holder.app.handle_request(
                    self.command, self.path, headers, self.rfile.read)
            except Exception:
                response = None
            if response is None:
                body = b'{"error": "Internal error in the demo application."}'
                status, content_type, allow = 500, "application/json; charset=utf-8", ""
                extra_headers = ()
            else:
                body, status = response.body, response.status
                content_type, allow = response.content_type, response.allow
                extra_headers = getattr(response, "extra_headers", ())
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if allow:
                    self.send_header("Allow", allow)
                for name, value in extra_headers:
                    self.send_header(name, value)
                for name, value in SECURITY_HEADERS:
                    self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (OSError, TimeoutError):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    return Handler


class _AppHolder:
    app = None


def main(argv=None, stdout=None, stderr=None, compute_runner=None, compute_note=None):
    """Run the demo. `compute_runner` (an object with run(task, payload)) and `compute_note` (one line of text for the
    start-up banner) are for a launcher that builds a real compute provider outside this package; the demo itself
    never imports one."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(
        prog="python -m demo_app",
        description="Nelavi local demo: simulated rover, Record validation and history, "
                    "and a demo-only image analyzer. Listens on 127.0.0.1 only.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="local port (default %d)" % DEFAULT_PORT)
    parser.add_argument("--db", default=None,
                        help="SQLite file to keep the history between runs "
                             "(default: in memory, forgotten when the server stops)")
    parser.add_argument("--second-opinion-provider", choices=PROVIDER_CHOICES,
                        default=PROVIDER_SIMULATED,
                        help="who answers Section 5 (Second Opinion): 'simulated' (default; fixed "
                             "text, no model, no network), 'nvidia' (sends the image, crop, "
                             "language and readings to the NVIDIA cloud API; needs the "
                             "NVIDIA_API_KEY variable set in the terminal). There "
                             "is no automatic fallback between providers.")
    parser.add_argument("--research-provider", choices=RESEARCH_CHOICES, default=RESEARCH_NONE,
                        help="Section 6 (Research): 'none' (default; no research, no network), 'tavily' "
                             "(sends the question you type to the Tavily search service; needs the "
                             "TAVILY_API_KEY variable set in the terminal and --research-domains) or "
                             "'tavily-pro' (starts a Tavily Research task with the pro model for the question "
                             "you type and polls it for up to %d seconds; needs TAVILY_API_KEY; source links "
                             "are limited to %s and its subdomains; each task can use 15 to 250 credits). "
                             "Results are evidence to read, never recommendations. There is no fallback."
                             % (DEADLINE_SECONDS, APPROVED_DOMAIN))
    parser.add_argument("--research-domains", default=None,
                        help="comma-separated host names that research is limited to (required with "
                             "--research-provider tavily; not allowed with tavily-pro, whose domain is fixed). "
                             "Use 'agreed' for the agreed list (%s). Only those host names, or their true "
                             "subdomains, are accepted; anything else stops the server from starting."
                             % ", ".join(AGREED_RESEARCH_DOMAINS))
    parser.add_argument("--findings-provider", choices=FINDINGS_CHOICES, default=FINDINGS_NONE,
                        help="Section 8 (Integrated workflow), image stage: 'none' (default; only skipped or fake "
                             "stages can be chosen) or 'nvidia' (sends the PNG image you upload, with a fixed "
                             "findings prompt, to the NVIDIA cloud API; needs the NVIDIA_API_KEY variable set in the "
                             "terminal and may incur usage cost). The research stage uses the Tavily client chosen "
                             "with --research-provider tavily. There is no fallback between providers.")
    parser.add_argument("--findings-timeout", default=None, metavar="SECONDS",
                        help="only with --findings-provider nvidia: how long the image request may wait for NVIDIA, in "
                             "seconds (above 0, at most %d). Default: the adapter's own 60 seconds. The whole demo "
                             "waits meanwhile because the server is single-threaded." % FINDINGS_TIMEOUT_MAX_SECONDS)
    parser.add_argument("--findings-thinking-off", action="store_true",
                        help="only with --findings-provider nvidia: add the field chat_template_kwargs = "
                             "{enable_thinking: false} to the image request, as the findings smoke script does. "
                             "Default: off, so the request is unchanged. Whether the hosted service honours the "
                             "field is not established; it accepted the request in one earlier test.")
    parser.add_argument("--synthesis-provider", choices=SYNTHESIS_CHOICES, default=SYNTHESIS_NONE,
                        help="Section 8 (Integrated workflow), synthesis stage: 'none' (default; only skipped or fake "
                             "choices), 'openrouter' (sends the validated image findings, the retrieved evidence "
                             "text (not the links) and the question to OpenRouter's documented chat-completions "
                             "endpoint; needs --synthesis-model and a key in the variable named by "
                             "--synthesis-key-env, default %s; may incur usage cost) or 'chat' (the same data to "
                             "the generic chat-completions endpoint named by --synthesis-endpoint). The answer is "
                             "unverified model text. There is no fallback."
                             % OPENROUTER_DEFAULT_KEY_ENV)
    parser.add_argument("--synthesis-endpoint", default=None, metavar="URL",
                        help="only with --synthesis-provider chat: the full https URL of the chat-completions "
                             "endpoint (port 443, no user-info). No default. Not allowed with openrouter, whose "
                             "endpoint is fixed to the documented one.")
    parser.add_argument("--synthesis-model", default=None, metavar="ID",
                        help="only with --synthesis-provider chat or openrouter: the model identifier to request "
                             "(for openrouter it needs the organisation prefix, for example vendor/model). For "
                             "openrouter the default is the owner-selected %s; for chat there is no default. There "
                             "is no fallback model." % OPENROUTER_SELECTED_MODEL)
    parser.add_argument("--synthesis-key-env", default=None, metavar="NAME",
                        help="only with --synthesis-provider chat or openrouter: the NAME of the terminal variable "
                             "that holds the key (capital letters, digits, underscore). Default: %s for openrouter, "
                             "%s for chat. The value is never printed."
                             % (OPENROUTER_DEFAULT_KEY_ENV, SYNTHESIS_DEFAULT_KEY_ENV))
    parser.add_argument("--synthesis-structured-output", action="store_true",
                        help="only with --synthesis-provider openrouter: also ask for a strict JSON-schema reply and "
                             "require that the routed endpoint supports it. Only some models support this, and an "
                             "unsupported model makes the request fail. The reply is still checked locally. "
                             "Default: off.")
    parser.add_argument("--remote", action="store_true", default=False,
                        help="enable opt-in remote access mode for FlutterFlow clients "
                             "(requires --allowed-hosts and an authentication token in the terminal variable)")
    parser.add_argument("--host", default=None,
                        help="host/interface to bind (default: 127.0.0.1 in local mode; 0.0.0.0 in remote mode)")
    parser.add_argument("--allowed-hosts", default=None,
                        help="comma-separated list of allowed Host header values (required with --remote)")
    parser.add_argument("--cors-origins", default=None,
                        help="comma-separated list of exact allowed origins for FlutterFlow web client (with --remote)")
    parser.add_argument("--auth-token-env", default="NELAVI_AUTH_TOKEN",
                        help="name of the terminal variable that holds the bearer authentication token "
                             "(default: NELAVI_AUTH_TOKEN)")
    parser.add_argument("--allow-remote-real-providers", action="store_true", default=False,
                        help="explicit opt-in to allow real provider stages in remote mode (disabled by default)")
    parser.add_argument("--elevenlabs-key-env", default=ELEVENLABS_DEFAULT_KEY_ENV,
                        help="name of the terminal variable that holds the ElevenLabs key "
                             "(default: %s)" % ELEVENLABS_DEFAULT_KEY_ENV)
    args = parser.parse_args(argv)

    if not 1 <= args.port <= 65535:
        print("Error: --port must be from 1 to 65535.", file=stderr)
        return 2

    if not args.remote:
        if args.allowed_hosts is not None:
            print("Error: --allowed-hosts only applies with --remote. Nothing was started.", file=stderr)
            return 2
        if args.cors_origins is not None:
            print("Error: --cors-origins only applies with --remote. Nothing was started.", file=stderr)
            return 2
        if args.allow_remote_real_providers:
            print("Error: --allow-remote-real-providers only applies with --remote. Nothing was started.", file=stderr)
            return 2
        if args.host is not None and args.host not in ("127.0.0.1", "localhost"):
            print("Error: local mode only binds to loopback (127.0.0.1). Use --remote to enable remote mode. "
                  "Nothing was started.", file=stderr)
            return 2
        bind_host = "127.0.0.1"
        auth_token = None
        cors_origins = set()
        token_env = None
        allowed_hosts_list = []
    else:
        if not args.allowed_hosts or not args.allowed_hosts.strip():
            print("Error: --remote requires --allowed-hosts with at least one allowed Host value "
                  "(e.g. api.example.com). Nothing was started.", file=stderr)
            return 2
        allowed_hosts_list = [h.strip() for h in args.allowed_hosts.split(",") if h.strip()]
        if not allowed_hosts_list:
            print("Error: --allowed-hosts must name at least one valid host. Nothing was started.", file=stderr)
            return 2

        token_env = args.auth_token_env.strip() if isinstance(args.auth_token_env, str) and args.auth_token_env.strip() else "NELAVI_AUTH_TOKEN"
        auth_token = _read_auth_token(token_env)
        if auth_token is None:
            print("Error: --remote requires the %s variable to be set in the terminal with a non-empty bearer token. "
                  "Nothing was started." % token_env, file=stderr)
            return 2

        cors_origins = set()
        if args.cors_origins:
            for origin in args.cors_origins.split(","):
                origin = origin.strip()
                if not origin:
                    continue
                if origin == "*" or not (origin.startswith("http://") or origin.startswith("https://")):
                    print("Error: --cors-origins must be exact http or https origins (wildcards like '*' are not allowed). "
                          "Offending origin: %s. Nothing was started." % origin, file=stderr)
                    return 2
                cors_origins.add(origin)

        bind_host = args.host if args.host is not None else "0.0.0.0"

    use_tavily = args.research_provider == RESEARCH_TAVILY
    use_pro = args.research_provider == RESEARCH_TAVILY_PRO
    research_client = None
    research_pro_client = None
    if not use_tavily and not use_pro and args.research_domains is not None:
        print("Error: --research-domains only applies with --research-provider tavily. Nothing was started.",
              file=stderr)
        return 2
    if use_pro:
        if args.research_domains is not None:
            print("Error: --research-domains cannot be used with --research-provider tavily-pro; its only "
                  "domain is fixed to %s. Nothing was started." % APPROVED_DOMAIN, file=stderr)
            return 2
        if not tavily_pro_key_present():
            print("Error: --research-provider tavily-pro needs the TAVILY_API_KEY variable to be set in the "
                  "terminal. Nothing was started; there is no fallback.", file=stderr)
            return 2
        research_pro_client = TavilyResearchPro()
    if use_tavily:
        if (args.research_domains or "").strip().lower() == AGREED_SHORTCUT:
            domains = list(AGREED_RESEARCH_DOMAINS)
        else:
            domains = [d.strip() for d in (args.research_domains or "").split(",") if d.strip()]
        research_client = TavilyResearch(domains)
        if not research_client.configuration_ok():
            print("Error: --research-provider tavily needs --research-domains with plain host names, "
                  "for example %s or the word 'agreed' (no wildcards, schemes or paths). Nothing was "
                  "started." % ",".join(AGREED_RESEARCH_DOMAINS), file=stderr)
            return 2
        outside = [d for d in research_client.domains if not _is_agreed_domain(d)]
        if outside:
            print("Error: --research-domains may only name the agreed domains (%s) or their subdomains; not "
                  "allowed: %s. Nothing was started."
                  % (", ".join(AGREED_RESEARCH_DOMAINS), ", ".join(outside)), file=stderr)
            return 2
        if not tavily_key_present():
            print("Error: --research-provider tavily needs the TAVILY_API_KEY variable to be set in the "
                  "terminal. Nothing was started; there is no fallback.", file=stderr)
            return 2

    provider = args.second_opinion_provider
    use_nvidia = provider == PROVIDER_NVIDIA
    # Checked before anything is opened or bound. Only variable names are printed, never values.
    if use_nvidia and not api_key_present():
        print("Error: --second-opinion-provider nvidia needs the NVIDIA_API_KEY "
              "variable to be set in the terminal. Nothing was started; there is no fallback to the simulated "
              "provider.", file=stderr)
        return 2

    use_findings = args.findings_provider == FINDINGS_NVIDIA
    if use_findings and not api_key_present():
        print("Error: --findings-provider nvidia needs the NVIDIA_API_KEY variable to be set in the terminal. "
              "Nothing was started; there is no fallback to a fake provider.", file=stderr)
        return 2
    if (args.findings_timeout is not None or args.findings_thinking_off) and not use_findings:
        print("Error: --findings-timeout and --findings-thinking-off only apply with --findings-provider nvidia. "
              "Nothing was started.", file=stderr)
        return 2
    findings_timeout = None
    if args.findings_timeout is not None:
        findings_timeout = parse_findings_timeout(args.findings_timeout)
        if findings_timeout is None:
            print("Error: --findings-timeout must be a number of seconds above 0 and at most %d (not NaN or "
                  "infinity). Nothing was started." % FINDINGS_TIMEOUT_MAX_SECONDS, file=stderr)
            return 2

    use_chat = args.synthesis_provider == SYNTHESIS_CHAT
    use_openrouter = args.synthesis_provider == SYNTHESIS_OPENROUTER
    use_synthesis = use_chat or use_openrouter
    if not use_synthesis and (args.synthesis_endpoint is not None or args.synthesis_model is not None
                              or args.synthesis_key_env is not None or args.synthesis_structured_output):
        print("Error: --synthesis-endpoint, --synthesis-model, --synthesis-key-env and --synthesis-structured-output "
              "only apply with --synthesis-provider chat or openrouter. Nothing was started.", file=stderr)
        return 2
    if use_openrouter and args.synthesis_endpoint is not None:
        print("Error: --synthesis-endpoint cannot be used with --synthesis-provider openrouter; its endpoint is "
              "fixed to the documented one. Nothing was started.", file=stderr)
        return 2
    if use_chat and args.synthesis_structured_output:
        print("Error: --synthesis-structured-output only applies with --synthesis-provider openrouter. Nothing was "
              "started.", file=stderr)
        return 2
    if use_synthesis:
        key_env = args.synthesis_key_env or (OPENROUTER_DEFAULT_KEY_ENV if use_openrouter
                                             else SYNTHESIS_DEFAULT_KEY_ENV)
        synthesis_model = args.synthesis_model
        if synthesis_model is None and use_openrouter:
            synthesis_model = OPENROUTER_SELECTED_MODEL
        if use_chat and not endpoint_is_valid(args.synthesis_endpoint):
            print("Error: --synthesis-provider chat needs --synthesis-endpoint set to an https URL on port 443 "
                  "without user-info, query or fragment. Nothing was started.", file=stderr)
            return 2
        if use_openrouter and not openrouter_model_is_valid(synthesis_model):
            print("Error: --synthesis-model must be a model identifier with its organisation prefix, for example "
                  "vendor/model. Nothing was started.", file=stderr)
            return 2
        if use_chat and not model_is_valid(synthesis_model):
            print("Error: --synthesis-provider chat needs --synthesis-model set to a model identifier (letters, "
                  "digits and . _ : / @ + -; there is no default). Nothing was started.", file=stderr)
            return 2
        if not key_env_name_is_valid(key_env):
            print("Error: --synthesis-key-env must be a variable NAME of capital letters, digits and underscores. "
                  "Nothing was started.", file=stderr)
            return 2
        if not synthesis_key_present(key_env):
            print("Error: --synthesis-provider %s needs the %s variable to be set in the terminal. Nothing was "
                  "started; there is no fallback to a fake provider." % (args.synthesis_provider, key_env),
                  file=stderr)
            return 2

    has_real_provider = (
        use_nvidia or use_findings or use_tavily or use_pro or use_synthesis or (compute_runner is not None)
    )
    if args.remote and has_real_provider and not args.allow_remote_real_providers:
        print("Error: real provider stages are disabled by default in remote mode. Pass --allow-remote-real-providers "
              "to explicitly enable them, or remove real provider flags. Nothing was started.", file=stderr)
        return 2

    try:
        store = RecordStore(args.db if args.db is not None else ":memory:")
    except (sqlite3.Error, OSError, ValueError) as exc:
        print("Error: could not open the history database: %s" % (exc,), file=stderr)
        return 2

    holder = _AppHolder()
    try:
        server = HTTPServer((bind_host, args.port), make_handler(holder))
        server.timeout = 30.0
    except OSError as exc:
        store.close()
        print("Error: could not listen on %s:%d (%s). Try another port with --port."
              % (bind_host, args.port, exc), file=stderr)
        return 2

    port = server.server_address[1]
    try:
        research = ({"research_client": research_client, "research_domains": research_client.domains}
                    if use_tavily else {})
        if use_pro:
            research = {"research_pro_client": research_pro_client}
        if use_findings:
            # Always observed (diagnostics only, no change to the request). With neither flag the analyzer is built
            # with the default configuration, so the timeout and the request body are exactly as before.
            observer = ObservingTransport(HeaderCapturingTransport(), thinking_off=args.findings_thinking_off)
            config = NvidiaConfig(timeout_seconds=findings_timeout) if findings_timeout is not None else None
            research = dict(research, findings_analyzer=NvidiaImageAnalyzer(config=config, transport=observer),
                            findings_observer=observer)
        if compute_runner is not None:
            research = dict(research, compute_runner=compute_runner)
        if use_openrouter:
            research = dict(research, synthesis_provider=OpenRouterSynthesisProvider(
                synthesis_model, key_env, structured_output=args.synthesis_structured_output))
        elif use_chat:
            research = dict(research, synthesis_provider=ChatSynthesisProvider(
                args.synthesis_endpoint, synthesis_model, key_env))
        elevenlabs_client = ElevenLabsClient(key_env_var=args.elevenlabs_key_env)
        research = dict(research, elevenlabs_client=elevenlabs_client)
        if use_nvidia:
            service = DemoService(store, second_opinion_provider=NvidiaImageAnalyzer(),
                                  second_opinion_simulated=False, **research)
        else:
            service = DemoService(store, **research)
        if not args.remote:
            app_allowed_hosts = {"%s:%d" % (bind_host, port), "localhost:%d" % port}
        else:
            app_allowed_hosts = set()
            for h in allowed_hosts_list:
                h_clean = h.strip().lower()
                app_allowed_hosts.add(h_clean)
                if ":" not in h_clean:
                    app_allowed_hosts.add("%s:%d" % (h_clean, port))
        holder.app = DemoApp(
            service,
            app_allowed_hosts,
            remote=args.remote,
            auth_token=auth_token,
            cors_origins=cors_origins,
            allow_remote_real_providers=args.allow_remote_real_providers,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        server.server_close()
        store.close()
        print("Error: %s" % (exc,), file=stderr)
        return 2

    if args.remote:
        print("NELAVI REMOTE ACCESS MODE (OPT-IN ENABLED)", file=stdout)
        print("Listening on %s:%d" % (bind_host, port), file=stdout)
        print("Bearer authentication required on all /api/* routes (configured via %s)." % token_env, file=stdout)
        print("CORS allowed origins: %s" % (", ".join(sorted(cors_origins)) if cors_origins else "none configured"), file=stdout)
        print("Static demo web UI: DISABLED (API only).", file=stdout)
        print("Health check endpoint: http://%s:%d/health (unauthenticated)." % (bind_host, port), file=stdout)
        if args.allow_remote_real_providers:
            print("WARNING: Real provider stages are ENABLED remotely (--allow-remote-real-providers).", file=stdout)
        else:
            print("Real provider stages: DISABLED (safe default).", file=stdout)
        print("Press Ctrl+C to stop the remote server.", file=stdout)
    else:
        if use_nvidia:
            print("Nelavi local demo - MIXED MODE: Section 5 (Second Opinion) uses the REAL NVIDIA "
                  "cloud model; the rover, Records and Section 3 are simulated.", file=stdout)
            print("Second Opinion sends the chosen image, crop, language and readings to NVIDIA "
                  "and may incur usage cost.", file=stdout)
        elif use_tavily:
            print("Nelavi local demo - MIXED MODE: Section 5 (Second Opinion) is simulated; Section 6 "
                  "(Research) uses the Tavily search service; the rover, Records and Section 3 are simulated.",
                  file=stdout)
        elif use_pro:
            print("Nelavi local demo - MIXED MODE: Section 5 (Second Opinion) is simulated; Section 6 "
                  "(Research) uses Tavily Research with the pro model; the rover, Records and Section 3 are "
                  "simulated.", file=stdout)
        else:
            print("Nelavi local demo (SIMULATED DATA ONLY - no AI model, no network, no hardware)",
                  file=stdout)
        if compute_runner is not None:
            print("INTEGRATED WORKFLOW: the compute stage can use a REAL compute provider. %s"
                  % (compute_note if isinstance(compute_note, str) else "Choosing 'real' for it sends data to that provider."),
                  file=stdout)
        if use_findings:
            print("INTEGRATED WORKFLOW: the image stage can use the REAL NVIDIA cloud model. Choosing 'real' for it sends "
                  "the PNG image you upload to NVIDIA and may incur usage cost. %s the compute stage is real only when a "
                  "launcher supplies one." % ("The synthesis stage is real only as announced below;" if use_synthesis
                                              else "The synthesis stage has no real provider (fake or skipped only);"),
                  file=stdout)
            print("Image request options: timeout %s; reasoning-off field %s. Each real image request is timed and its "
                  "HTTP status or failure category is shown on the page (no text from NVIDIA or the network)."
                  % ("%g seconds (--findings-timeout)" % findings_timeout if findings_timeout is not None
                     else "default (%g seconds)" % DEFAULT_TIMEOUT_SECONDS,
                     "ADDED (--findings-thinking-off)" if args.findings_thinking_off else "not added (default)"),
                  file=stdout)
        if use_synthesis:
            print("INTEGRATED WORKFLOW: the synthesis stage can use a REAL %s service at %s (model %s, key variable "
                  "%s%s). Choosing 'real' for it sends the validated image findings, the retrieved evidence text "
                  "(titles, snippets and dates, not the links) and the question to that host and may incur usage cost. "
                  "The answer is unverified model text, never evidence; a failure is shown as a failure, with no "
                  "fallback."
                  % ("OpenRouter chat-completions" if use_openrouter else "chat-completions",
                     endpoint_host(OPENROUTER_ENDPOINT if use_openrouter else args.synthesis_endpoint),
                     synthesis_model, key_env,
                     "; strict structured output requested" if args.synthesis_structured_output else ""),
                  file=stdout)
        if use_pro:
            print("Research sends only the question you type to Tavily and may use 15 to 250 credits per task. "
                  "Source links are limited to %s and its subdomains. The generated text is unreviewed AI output, "
                  "not verified advice. A task is abandoned after %d seconds." % (APPROVED_DOMAIN, DEADLINE_SECONDS),
                  file=stdout)
        if use_tavily:
            print("Research sends the question you type to Tavily, limited to: %s. Results are evidence, "
                  "not recommendations, and may use search credits." % ", ".join(research_client.domains),
                  file=stdout)
        print("Open http://%s:%d/ in your browser. Press Ctrl+C to stop." % (bind_host, port),
              file=stdout)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", file=stdout)
    finally:
        server.server_close()
        store.close()
    return 0
