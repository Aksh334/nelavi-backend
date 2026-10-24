"""Tavily Research (model `pro`) adapter (V1): start a research task, then poll it. Offline-tested only.

This is a SEPARATE adapter from `research/tavily.py` (the Search adapter), which it does not import or change.
The two services have different contracts: Search answers in one call, Research is asynchronous.

Confirmed from Tavily's official documentation (docs.tavily.com, read on 2026-10-16):
  * `POST https://api.tavily.com/research` starts a task. Body fields used here: `input` (required), `model`
    (`mini`, `pro` or `auto`; this module always sends `pro`), `stream` (false), `citation_format`
    (`numbered`), `output_length` (`standard`) and `include_domains`. Success is HTTP 201 with `request_id`
    and `status` (`pending`).
  * `GET https://api.tavily.com/research/{request_id}` reads a task. HTTP 202 with `status` `pending` or
    `in_progress` means "not finished"; HTTP 200 with `status` `completed` carries `content` (a string unless an
    `output_schema` was sent, which this module never does) and `sources` (`title`, `url`, `favicon`); HTTP 200 with
    `status` `failed` carries no content and no reason. Other codes: 401, 404, 429 (with `Retry-After`), 432/433
    (plan or pay-as-you-go limit) and 500. Error bodies look like {"detail": {"error": "..."}}.
  * Authentication is `Authorization: Bearer <key>`. The variable name `TAVILY_API_KEY` is this project's choice.
  * `include_domains` is documented as a SOFT PREFERENCE: "URLs from other domains can still appear". It is not
    a restriction, so every returned source is checked here with `approved_source_url` and only approved ones
    are ever returned.
  * A `pro` task is charged between 15 and 250 credits.

NOT documented, so not assumed: how long a task takes, why it fails, how long results are kept, whether the
numbers in a `numbered` report match the order of `sources`, and whether the report text uses only sources from
the preferred domains. Filtering the source LIST therefore says nothing about the TEXT, which is generated and is
always labelled unreviewed. This module produces no rule, dose, threshold or recommendation.

Design rules, all enforced and tested:
  * Start-and-poll. `start()` makes one request and returns at once with a local task id. `poll()` makes at most
    one provider request per call, and none at all while a rate-limit wait or the minimum poll gap is running.
  * Every task has a hard 120-second deadline measured from `start()`. Past it the task is `timed_out` and the
    provider is not contacted again. A `Retry-After` that would end after the deadline also ends the task.
  * Nothing loops or sleeps here; the caller polls. At most MAX_TASKS tasks are kept in memory.
  * Only the question text is sent. No image, file, record, location or measurement is ever included.
  * The only approved source domain is `icar.org.in` and its true subdomains, HTTPS only (nothing in the
    documentation requires plain HTTP). Hosts are parsed with `urllib.parse`, never fetched.
  * No fallback: a failed or unavailable task is reported as such, never as a result.
  * Errors are closed codes and carry no provider text. The key goes only into the Authorization header and is
    never logged or returned. No live request has ever been made through this module.
"""

import json
import math
import os
import re
import secrets
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

# Shared with the other adapters on purpose (imported, never changed): the bounded body reader, the transport
# error types, the no-redirect handler and the key check.
from image_analysis.nvidia import (
    _MAX_RESPONSE_BYTES,
    ResponseTooLarge,
    TransportError,
    TransportTimeout,
    _key_is_usable,
    _NoRedirect,
    _read_limited,
)

ENDPOINT = "https://api.tavily.com/research"
API_KEY_ENV_VAR = "TAVILY_API_KEY"
MODEL = "pro"
APPROVED_DOMAIN = "icar.org.in"

DEADLINE_SECONDS = 120          # hard limit per task, measured from start()
START_TIMEOUT_SECONDS = 15.0    # one provider request; also capped by nothing else because the task is new
POLL_TIMEOUT_SECONDS = 10.0     # one provider request; also capped by the time left before the deadline
MIN_POLL_GAP_SECONDS = 4.0      # local: the provider is not asked more often than this per task
DEFAULT_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 3600
MAX_TASKS = 20
MAX_QUERY_CHARS = 200
MAX_CONTENT_CHARS = 20000
MAX_SOURCES = 20
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 2048

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_ERROR = "error"
STATUSES = (STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMED_OUT, STATUS_ERROR)

ERROR_CREDENTIAL_MISSING = "credential_missing"
ERROR_CONFIGURATION_INVALID = "configuration_invalid"
ERROR_QUERY_INVALID = "query_invalid"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_USAGE_LIMIT = "usage_limit_exceeded"
ERROR_PROVIDER_REJECTED = "provider_rejected"
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERROR_PROVIDER_UNREACHABLE = "provider_unreachable"
ERROR_RESPONSE_MALFORMED = "response_malformed"
ERROR_RESPONSE_TOO_LARGE = "response_too_large"
ERROR_TASK_NOT_FOUND = "task_not_found"
ERROR_TASK_FAILED = "task_failed"
ERROR_TIMED_OUT = "deadline_exceeded"
ERROR_TOO_MANY_TASKS = "too_many_tasks"
ERROR_CODES = (
    ERROR_CREDENTIAL_MISSING, ERROR_CONFIGURATION_INVALID, ERROR_QUERY_INVALID, ERROR_RATE_LIMITED,
    ERROR_USAGE_LIMIT, ERROR_PROVIDER_REJECTED, ERROR_PROVIDER_UNAVAILABLE, ERROR_PROVIDER_UNREACHABLE,
    ERROR_RESPONSE_MALFORMED, ERROR_RESPONSE_TOO_LARGE, ERROR_TASK_NOT_FOUND, ERROR_TASK_FAILED,
    ERROR_TIMED_OUT, ERROR_TOO_MANY_TASKS,
)

KIND = "research_pro_unreviewed"
LABEL = "UNREVIEWED AI-GENERATED RESEARCH — NOT VERIFIED AGRICULTURAL ADVICE."
CAVEATS = (
    ("unreviewed_ai_generated", "The text is generated by an AI research service and has not been reviewed by a "
                               "person. It is not verified agricultural advice."),
    ("text_not_limited_to_approved_sources",
     "Only the source links are limited to icar.org.in and its subdomains. The text may use or mention other "
     "sources, and any numbered references in it may not match the links shown."),
    ("no_operational_advice",
     "Nothing here is a dose, rate, threshold or treatment recommendation. A person with agricultural expertise "
     "must review it before anything is done."),
)
TRUNCATED_CAVEAT = ("text_truncated", "The generated text was longer than this page shows and was cut.")

_HOST_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_RETRY_AFTER_PATTERN = re.compile(r"^[0-9]{1,6}$")


# --------------------------------------------------------------------------- source validation
def approved_source_url(url):
    """True only if `url` is a well-formed HTTPS URL whose parsed host is `icar.org.in` or a true subdomain.

    The URL is parsed, never fetched. User-info, other schemes, ports other than 443, whitespace, control
    characters, backslashes, empty hosts and look-alike hosts are all refused.
    """
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL_CHARS:
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not host or "@" in parts.netloc:
        return False
    if parts.username is not None or parts.password is not None or port not in (None, 443):
        return False
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if _HOST_PATTERN.fullmatch(host) is None:
        return False
    return host == APPROVED_DOMAIN or host.endswith("." + APPROVED_DOMAIN)


# --------------------------------------------------------------------------- transport
def _retry_after_of(headers):
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    return value if isinstance(value, str) else None


def urllib_transport(method, url, headers, body, timeout):
    """Default transport: one request using the standard library. Redirects are not followed.

    Returns (status, body_bytes, retry_after_text_or_None). The body is read in bounded chunks and an oversized
    body raises ResponseTooLarge. No response at all raises TransportError (or TransportTimeout).
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _read_limited(response, _MAX_RESPONSE_BYTES), _retry_after_of(response.headers)
    except urllib.error.HTTPError as error:
        try:
            return error.code, _read_limited(error, _MAX_RESPONSE_BYTES), _retry_after_of(error.headers)
        except ResponseTooLarge:
            raise
        except Exception:
            return error.code, b"", None
        finally:
            try:
                error.close()
            except Exception:
                pass
    except (socket.timeout, TimeoutError):
        raise TransportTimeout() from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise TransportTimeout() from None
        raise TransportError() from None
    except OSError:
        raise TransportError() from None


def key_present(environ=None):
    """Return True or False: whether a usable `TAVILY_API_KEY` is set. Never returns the value."""
    source = environ if environ is not None else os.environ
    return _key_is_usable(source.get(API_KEY_ENV_VAR))


# --------------------------------------------------------------------------- helpers
def _query_is_valid(query):
    if not isinstance(query, str) or query.strip() == "" or len(query) > MAX_QUERY_CHARS:
        return False
    return not any(unicodedata.category(c).startswith("C") for c in query)


def _clean_text(value, limit):
    """Strip control characters other than line breaks and tabs, then cut to `limit`. Returns (text, cut)."""
    if not isinstance(value, str):
        return "", False
    cleaned = "".join(c for c in value if c in "\n\t" or not unicodedata.category(c).startswith("C")).strip()
    return cleaned[:limit], len(cleaned) > limit


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _retry_after_seconds(text):
    """Whole seconds from a Retry-After value, or the default if it is missing or not a plain number."""
    if isinstance(text, str) and _RETRY_AFTER_PATTERN.fullmatch(text.strip()):
        return min(int(text.strip()), MAX_RETRY_AFTER_SECONDS)
    return DEFAULT_RETRY_AFTER_SECONDS


def _error_view(code, query=None):
    return {
        "kind": KIND, "task_id": None, "status": STATUS_ERROR, "error_code": code, "query": query,
        "elapsed_seconds": 0, "deadline_seconds": DEADLINE_SECONDS, "retry_after_seconds": None,
        "retrieved_at": None, "label": LABEL, "content": None, "sources": [], "sources_dropped": 0,
        "sources_state": "not_available", "caveats": [{"code": c, "message": m} for c, m in CAVEATS],
    }


class _Task:
    __slots__ = ("task_id", "request_id", "query", "started", "next_check", "status", "error_code",
                 "retry_after", "retrieved_at", "content", "sources", "dropped", "truncated")

    def __init__(self, task_id, request_id, query, started):
        self.task_id, self.request_id, self.query, self.started = task_id, request_id, query, started
        self.next_check = started + MIN_POLL_GAP_SECONDS
        self.status, self.error_code, self.retry_after = STATUS_RUNNING, None, None
        self.retrieved_at, self.content, self.sources, self.dropped, self.truncated = None, None, [], 0, False


# --------------------------------------------------------------------------- the client
class TavilyResearchPro:
    """Starts and polls Tavily Research tasks (model `pro`), restricted by policy to icar.org.in sources."""

    max_query_chars = MAX_QUERY_CHARS
    domain = APPROVED_DOMAIN
    deadline_seconds = DEADLINE_SECONDS
    model = MODEL

    def __init__(self, transport=None, environ=None, clock=None, now=None, id_factory=None,
                 start_timeout=START_TIMEOUT_SECONDS, poll_timeout=POLL_TIMEOUT_SECONDS):
        self._transport = transport if transport is not None else urllib_transport
        self._environ = environ  # None means the process environment, read per call
        self._clock = clock if clock is not None else time.monotonic
        self._now = now if now is not None else _now_iso
        self._id_factory = id_factory if id_factory is not None else (lambda: secrets.token_urlsafe(16))
        self._start_timeout = start_timeout
        self._poll_timeout = poll_timeout
        self._tasks = OrderedDict()

    def configuration_ok(self):
        return all(isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0
                   for t in (self._start_timeout, self._poll_timeout))

    def _key(self):
        environ = self._environ if self._environ is not None else os.environ
        key = environ.get(API_KEY_ENV_VAR)
        return key if _key_is_usable(key) else None

    @staticmethod
    def _headers(key, with_body):
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ----------------------------------------------------------------- start
    def start(self, question):
        """Submit one research task. Returns a view dict: `running` with a local task id, or `error`."""
        if not self.configuration_ok():
            return _error_view(ERROR_CONFIGURATION_INVALID)
        if not _query_is_valid(question):
            return _error_view(ERROR_QUERY_INVALID)
        key = self._key()
        if key is None:
            return _error_view(ERROR_CREDENTIAL_MISSING, question)
        if len(self._tasks) >= MAX_TASKS and not self._evict_one():
            return _error_view(ERROR_TOO_MANY_TASKS, question)

        body = json.dumps({
            "input": question,
            "model": MODEL,
            "stream": False,
            "citation_format": "numbered",
            "output_length": "standard",
            "include_domains": [APPROVED_DOMAIN],
        }).encode("utf-8")
        started = self._clock()
        try:
            status, response_body, retry_after = self._transport(
                "POST", ENDPOINT, self._headers(key, True), body, self._start_timeout)
        except ResponseTooLarge:
            return _error_view(ERROR_RESPONSE_TOO_LARGE, question)
        except Exception:
            return _error_view(ERROR_PROVIDER_UNREACHABLE, question)

        code = self._code_for_status(status)
        if code is not None:
            return _error_view(code, question)
        document = self._json_object(response_body)
        request_id = document.get("request_id") if document is not None else None
        if (document is None or document.get("status") not in ("pending", "in_progress")
                or not isinstance(request_id, str) or _REQUEST_ID_PATTERN.fullmatch(request_id) is None):
            return _error_view(ERROR_RESPONSE_MALFORMED, question)

        task_id = self._id_factory()
        if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None or task_id in self._tasks:
            return _error_view(ERROR_CONFIGURATION_INVALID, question)
        task = _Task(task_id, request_id, question, started)
        self._tasks[task_id] = task
        return self._view(task)

    # ------------------------------------------------------------------ poll
    def poll(self, task_id):
        """Return the current view of a local task, asking the provider at most once. None if unknown."""
        if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None:
            return None
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.status != STATUS_RUNNING:
            return self._view(task)
        now = self._clock()
        if now - task.started >= DEADLINE_SECONDS:
            return self._finish(task, STATUS_TIMED_OUT, ERROR_TIMED_OUT)
        if now < task.next_check:
            task.retry_after = max(1, math.ceil(task.next_check - now))
            return self._view(task)
        key = self._key()
        if key is None:
            return self._finish(task, STATUS_ERROR, ERROR_CREDENTIAL_MISSING)

        remaining = DEADLINE_SECONDS - (now - task.started)
        timeout = min(self._poll_timeout, remaining)
        url = ENDPOINT + "/" + quote(task.request_id, safe="")
        try:
            status, response_body, retry_after = self._transport("GET", url, self._headers(key, False), None, timeout)
        except ResponseTooLarge:
            return self._finish(task, STATUS_ERROR, ERROR_RESPONSE_TOO_LARGE)
        except Exception:
            return self._finish(task, STATUS_ERROR, ERROR_PROVIDER_UNREACHABLE)

        after = self._clock()
        if status == 429:
            wait = max(_retry_after_seconds(retry_after), int(MIN_POLL_GAP_SECONDS))
            task.next_check = after + wait
            if task.next_check - task.started >= DEADLINE_SECONDS:
                return self._finish(task, STATUS_TIMED_OUT, ERROR_TIMED_OUT)
            task.retry_after = wait
            return self._view(task)
        code = self._code_for_status(status)
        if code is not None:
            return self._finish(task, STATUS_ERROR, code)
        document = self._json_object(response_body)
        state = document.get("status") if document is not None else None
        if state in ("pending", "in_progress"):
            task.next_check = after + MIN_POLL_GAP_SECONDS
            task.retry_after = None
            if after - task.started >= DEADLINE_SECONDS:
                return self._finish(task, STATUS_TIMED_OUT, ERROR_TIMED_OUT)
            return self._view(task)
        if state == "failed":
            return self._finish(task, STATUS_FAILED, ERROR_TASK_FAILED)
        if state == "completed" and self._take_result(task, document):
            return self._finish(task, STATUS_COMPLETED, None)
        return self._finish(task, STATUS_ERROR, ERROR_RESPONSE_MALFORMED)

    # --------------------------------------------------------------- internals
    @staticmethod
    def _code_for_status(status):
        """None for HTTP 200 to 299, else the closed error code for that status."""
        if not isinstance(status, int) or isinstance(status, bool):
            return ERROR_RESPONSE_MALFORMED
        if 200 <= status < 300:
            return None
        if status == 404:
            return ERROR_TASK_NOT_FOUND
        if status == 429:
            return ERROR_RATE_LIMITED
        if status in (432, 433):
            return ERROR_USAGE_LIMIT
        if 400 <= status < 500:
            return ERROR_PROVIDER_REJECTED
        if 500 <= status < 600:
            return ERROR_PROVIDER_UNAVAILABLE
        return ERROR_RESPONSE_MALFORMED

    @staticmethod
    def _json_object(body):
        if not isinstance(body, (bytes, bytearray)):
            return None
        try:
            document = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        return document if isinstance(document, dict) else None

    @staticmethod
    def _take_result(task, document):
        """Store the validated text and approved sources on `task`. False if the document is unusable."""
        content = document.get("content")
        sources = document.get("sources")
        if not isinstance(content, str) or content.strip() == "" or not isinstance(sources, list):
            return False
        task.content, task.truncated = _clean_text(content, MAX_CONTENT_CHARS)
        approved, dropped = [], 0
        seen = set()
        for raw in sources:
            url = raw.get("url") if isinstance(raw, dict) else None
            if not approved_source_url(url) or url in seen:
                dropped += 1
            elif len(approved) >= MAX_SOURCES:
                dropped += 1
            else:
                seen.add(url)
                title, _ = _clean_text(raw.get("title"), MAX_TITLE_CHARS)
                approved.append({"title": title, "url": url})
        task.sources, task.dropped = approved, dropped
        return task.content != ""

    def _evict_one(self):
        """Drop the oldest finished task. False if every kept task is still running."""
        for task_id, task in self._tasks.items():
            if task.status != STATUS_RUNNING:
                del self._tasks[task_id]
                return True
        return False

    def _finish(self, task, status, error_code):
        task.status, task.error_code, task.retry_after = status, error_code, None
        if status == STATUS_COMPLETED:
            task.retrieved_at = self._now()
        else:
            task.content, task.sources, task.dropped = None, [], 0
        return self._view(task)

    def _view(self, task):
        completed = task.status == STATUS_COMPLETED
        elapsed = max(0, int(self._clock() - task.started))
        caveats = [{"code": c, "message": m} for c, m in CAVEATS]
        if completed and task.truncated:
            caveats.append({"code": TRUNCATED_CAVEAT[0], "message": TRUNCATED_CAVEAT[1]})
        return {
            "kind": KIND, "task_id": task.task_id, "status": task.status, "error_code": task.error_code,
            "query": task.query, "elapsed_seconds": min(elapsed, DEADLINE_SECONDS),
            "deadline_seconds": DEADLINE_SECONDS, "retry_after_seconds": task.retry_after,
            "retrieved_at": task.retrieved_at, "label": LABEL,
            "content": task.content if completed else None,
            "sources": [dict(s) for s in task.sources] if completed else [],
            "sources_dropped": task.dropped if completed else 0,
            "sources_state": ("approved" if task.sources else "none_approved") if completed else "not_available",
            "caveats": caveats,
        }
