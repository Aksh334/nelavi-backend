"""Tavily search adapter (V1): research EVIDENCE only, never a recommendation. Offline-tested only.

Confirmed from Tavily's official documentation (docs.tavily.com, read on 2026-10-15): `POST
https://api.tavily.com/search`, `Authorization: Bearer <key>`, JSON request fields `query`,
`search_depth` (`basic` costs 1 credit), `topic`, `max_results` (0 to 20), `include_answer`,
`include_domains` (documented as a list; whether wildcard patterns work is NOT confirmed, so only plain
host names are accepted here), `include_published_date` (beta; may be null); response fields `results`
(each with `title`, `url`, `content`, `score`, optional `published_date`), `response_time`, `usage`,
`request_id`; errors 400, 401, 422, 429 (with `Retry-After`), 432, 433 and 500.

Not confirmed, and chosen by this project instead: the environment variable name `TAVILY_API_KEY`
(Tavily's pages pass the key directly), and every local limit below.

Design rules, all enforced and tested:
  * The search is always restricted to a NON-EMPTY list of host names that the owner supplies. There is
    no built-in "official sources" list, because this project has not verified one. The restriction is
    asked of Tavily (`include_domains`) AND re-checked locally: a result whose host is not on the list is
    dropped, whatever Tavily returned.
  * A result URL is kept only if it is an `https` URL, with no user-info and no port other than 443, whose parsed host
    is a configured host or a true subdomain of one. Anything else is dropped and counted. This says nothing about
    whether the page is correct, current or safe.
  * The request has a BEST-EFFORT time budget (`timeout_seconds`) when the built-in transport is used
    (`deadline_transport`). It is not a hard or end-to-end deadline; see that function for what is and is not bounded.
  * Appearing in a result is not evidence that a source is authoritative. Every result is labelled
    `research_evidence_only`. Nothing here produces a rule, a threshold, a dose or advice, and nothing
    here is connected to the advice engine.
  * Tavily's generated `answer` is never requested and never returned.
  * The query is bounded (`MAX_QUERY_CHARS`), snippets are cut (`MAX_SNIPPET_CHARS`), at most
    `max_results` results are kept, and the response body is limited to 4 MiB by the shared transport.
  * Source URL, title, snippet, Tavily's relevance score, Tavily's published date (kept as reported if it
    is short printable text of at most MAX_DATE_CHARS characters, else None; never cut or repaired) and the retrieval
    time are preserved so a report can cite them.
  * No retry: one `search()` call makes at most one transport call. Errors are closed codes and carry no
    provider text. The key goes only into the Authorization header and is never logged or returned.
  * It is used by the demo server only when started with `--research-provider tavily`, which restricts it to the agreed
    domains; nothing connects it to the advice engine. No live request has ever been made through it.
"""

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

# Shared with the image adapters on purpose (imported, never changed): the no-redirect handler, the size limits, the
# transport error types and the key check. The transport itself is local so that it can enforce a total time budget.
from image_analysis.nvidia import (
    _MAX_RESPONSE_BYTES,
    _READ_CHUNK_BYTES,
    ResponseTooLarge,
    TransportError,
    TransportTimeout,
    _key_is_usable,
    _NoRedirect,
)

ENDPOINT = "https://api.tavily.com/search"
API_KEY_ENV_VAR = "TAVILY_API_KEY"

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10          # a local cap; Tavily documents 0 to 20
MAX_QUERY_CHARS = 200
MAX_SNIPPET_CHARS = 1000
MAX_TITLE_CHARS = 300
MAX_DATE_CHARS = 64
MAX_DOMAINS = 50
MAX_URL_CHARS = 2048
HTTPS_PORT = 443

STATUS_OK = "ok"
STATUS_ERROR = "error"
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
ERROR_NO_RESULTS = "no_results"
# The same three strings the image adapters use (image_analysis.boundary). `provider_unreachable` stays in the set for
# compatibility, but this adapter no longer produces it.
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
ERROR_PROVIDER_CONNECTION_FAILED = "provider_connection_failed"
ERROR_TRANSPORT_FAILED = "transport_failed"
ERROR_CODES = (
    ERROR_CREDENTIAL_MISSING, ERROR_CONFIGURATION_INVALID, ERROR_QUERY_INVALID, ERROR_RATE_LIMITED,
    ERROR_USAGE_LIMIT, ERROR_PROVIDER_REJECTED, ERROR_PROVIDER_UNAVAILABLE, ERROR_PROVIDER_UNREACHABLE,
    ERROR_RESPONSE_MALFORMED, ERROR_RESPONSE_TOO_LARGE, ERROR_NO_RESULTS,
    ERROR_PROVIDER_TIMEOUT, ERROR_PROVIDER_CONNECTION_FAILED, ERROR_TRANSPORT_FAILED,
)

EVIDENCE_KIND = "research_evidence_only"
CAVEATS = (
    ("not_a_recommendation",
     "These are search results, not agronomic recommendations. Nothing here is a rule, threshold or dose."),
    ("domains_are_owner_configured",
     "Results are limited to the host names the owner configured. Being listed does not by itself make a "
     "page correct, current or applicable to this crop and soil."),
    ("published_date_unreliable",
     "A published date is Tavily's own estimate and may be missing."),
)

_HOST_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@dataclass(frozen=True)
class ResearchItem:
    title: str
    url: str
    snippet: str
    published_date: object   # str as reported by Tavily, or None
    score: object            # float as reported by Tavily, or None


@dataclass(frozen=True)
class ResearchResult:
    status: str
    error_code: object       # None, or one of ERROR_CODES
    query: object
    items: tuple             # ResearchItem, in Tavily's order, only from configured hosts
    dropped: int             # results discarded (host not allowed, or unusable)
    retrieved_at: object     # ISO-8601 UTC time of the call, or None if no call was made
    kind: str = EVIDENCE_KIND
    caveats: tuple = CAVEATS


def key_present(environ=None):
    """Return True or False: whether a usable `TAVILY_API_KEY` is set. Never returns the value."""
    source = environ if environ is not None else os.environ
    return _key_is_usable(source.get(API_KEY_ENV_VAR))


def _error(code, query=None, retrieved_at=None):
    return ResearchResult(STATUS_ERROR, code, query, (), 0, retrieved_at)


def _query_is_valid(query):
    if not isinstance(query, str) or query.strip() == "" or len(query) > MAX_QUERY_CHARS:
        return False
    return not any(unicodedata.category(c).startswith("C") for c in query)


def _clean_domains(domains):
    """Return a tuple of lower-case host names, or None if the list is unusable."""
    if not isinstance(domains, (list, tuple)) or not 1 <= len(domains) <= MAX_DOMAINS:
        return None
    cleaned = []
    for domain in domains:
        if not isinstance(domain, str) or _HOST_PATTERN.fullmatch(domain.lower()) is None:
            return None  # also rejects wildcards, schemes, paths and ports
        cleaned.append(domain.lower())
    return tuple(dict.fromkeys(cleaned))


def _host_allowed(url, domains):
    """True only for a well-formed `https` URL on port 443 (or none), without user-info, whose parsed host is a
    configured host or a true subdomain of one. The URL is parsed, never fetched or matched by substring."""
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL_CHARS:
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        return False  # whitespace, control characters and backslashes make the authority ambiguous
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port   # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return False
    if parts.scheme != "https" or not host or "@" in parts.netloc:
        return False  # user-info in a URL is a classic way to disguise the real host
    if parts.username is not None or parts.password is not None or port not in (None, HTTPS_PORT):
        return False
    host = host.lower()
    if _HOST_PATTERN.fullmatch(host) is None:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


class TransportConnectionFailed(TransportError):
    """The transport failed before any HTTP response arrived (for example connection refused, host unreachable, name
    not resolved, TLS handshake failed, or the connection dropped while waiting for the response). The connection
    may or may not have been accepted first. Carries no provider or request detail."""


def _narrow_read_timeout(stream, seconds):
    """Best effort: shorten the blocking time of the next read on the underlying connection to `seconds`. This
    reaches into the standard library's response object, so it is skipped silently if that layout differs."""
    for path in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock")):
        target = stream
        try:
            for name in path:
                target = getattr(target, name)
            target.settimeout(seconds)
            return
        except Exception:
            continue


def _read_until_deadline(stream, limit, deadline, monotonic):
    """Return the whole body if it is at most `limit` bytes. Raises ResponseTooLarge past the limit and
    TransportTimeout if `deadline` (a `monotonic()` reading) has passed before a further read is started, including
    the final read that observes end of file. A read that is already blocked is not interrupted and can overrun it."""
    chunks = []
    total = 0

    def next_chunk():
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TransportTimeout()
        _narrow_read_timeout(stream, remaining)
        return stream.read(min(_READ_CHUNK_BYTES, limit + 1 - total))

    for chunk in iter(next_chunk, b""):
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge()
    return b"".join(chunks)


def deadline_transport(url, headers, body, timeout, monotonic=time.monotonic):
    """Default transport: one POST using the standard library, redirects not followed, never retried. `timeout` is a
    BEST-EFFORT time budget in seconds, measured on `monotonic`. It is NOT a hard or end-to-end deadline.

    Before the body: DNS resolution is unbounded (the standard library has no timeout for it). Connecting, the TLS
    handshake, sending the request and reading the status line and headers are each limited only by the socket
    timeout given to `opener.open` (the full `timeout`), per blocking operation and, for a host with several
    addresses, per connection attempt. There is no guaranteed overall bound for this phase, and a slow sender can
    keep it going well past `timeout`.

    The body: before each read the remaining budget is checked, and TransportTimeout is raised if it is spent. Each
    read is given only the remaining time by shortening the socket timeout through the standard library's response
    object (best effort, see `_narrow_read_timeout`; skipped silently if that layout differs). A blocking operation
    already in progress when the deadline passes is not interrupted and can overrun it. The body is accepted only if
    its end of file is read before the deadline, so a fully delivered body can still be discarded (TransportTimeout)
    if the final read finishes after the deadline.

    Tested only with fake clocks and fake responses, never with real sockets or live Tavily.
    """
    deadline = monotonic() + timeout
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            return error.code, _read_until_deadline(error, _MAX_RESPONSE_BYTES, deadline, monotonic)
        except ResponseTooLarge:
            raise  # an oversized error body is reported as such, not as an empty body
        except Exception:
            return error.code, b""
        finally:
            try:
                error.close()
            except Exception:
                pass  # cleanup must not replace the result already decided above
    except TimeoutError:
        raise TransportTimeout() from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise TransportTimeout() from None
        raise TransportConnectionFailed() from None
    except OSError:
        raise TransportConnectionFailed() from None
    # Any other exception here (for example a malformed status line from the standard library's HTTP layer) is not
    # an OSError and is left to propagate; the adapter reports it as a generic transport failure.
    try:
        with response:
            return response.status, _read_until_deadline(response, _MAX_RESPONSE_BYTES, deadline, monotonic)
    except TimeoutError:
        raise TransportTimeout() from None
    except OSError:
        raise TransportError() from None   # the connection was made and then failed: not a connection-establishment error


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _date(value):
    """Tavily's published date as reported if it is short printable text, else None. A date that is too long, empty,
    not text or contains control characters is dropped (the result itself is kept), never cut or repaired."""
    if isinstance(value, str) and value.strip() and len(value) <= MAX_DATE_CHARS and value.isprintable():
        return value
    return None


def _item(raw, domains):
    """Return a ResearchItem, or None if the raw result is unusable or off the allowed list."""
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not _host_allowed(url, domains):
        return None
    snippet = _text(raw.get("content"), MAX_SNIPPET_CHARS)
    if snippet == "":
        return None
    score = raw.get("score")
    return ResearchItem(
        title=_text(raw.get("title"), MAX_TITLE_CHARS),
        url=url,
        snippet=snippet,
        published_date=_date(raw.get("published_date")),
        score=float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
    )


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TavilyResearch:
    """Runs one bounded, domain-restricted Tavily search and returns evidence, never advice."""

    def __init__(self, domains, transport=None, environ=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 max_results=DEFAULT_MAX_RESULTS, clock=None, monotonic=None):
        self._domains = _clean_domains(domains)
        # `timeout_seconds` is handed to the transport unchanged. The built-in transport treats it as a best-effort
        # budget measured on `monotonic` (default: time.monotonic); an injected transport decides for itself.
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._transport = transport if transport is not None else self._default_transport
        self._environ = environ  # None means the process environment, read per call
        self._timeout = timeout_seconds
        self._max_results = max_results
        self._clock = clock if clock is not None else _now

    def _default_transport(self, url, headers, body, timeout):
        return deadline_transport(url, headers, body, timeout, self._monotonic)

    def _configuration_ok(self):
        return (
            self._domains is not None
            and isinstance(self._timeout, (int, float)) and not isinstance(self._timeout, bool)
            and self._timeout > 0
            and isinstance(self._max_results, int) and not isinstance(self._max_results, bool)
            and 1 <= self._max_results <= MAX_RESULTS_LIMIT
        )

    def configuration_ok(self):
        """True if the host list, timeout and result limit are usable. It says nothing about the key."""
        return self._configuration_ok()

    max_query_chars = MAX_QUERY_CHARS

    @property
    def domains(self):
        """The owner-configured host names (a tuple), or None if the list was unusable."""
        return self._domains

    def search(self, query):
        if not self._configuration_ok():
            return _error(ERROR_CONFIGURATION_INVALID)
        if not _query_is_valid(query):
            return _error(ERROR_QUERY_INVALID)
        environ = self._environ if self._environ is not None else os.environ
        key = environ.get(API_KEY_ENV_VAR)
        if not _key_is_usable(key):
            return _error(ERROR_CREDENTIAL_MISSING, query)

        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                   "Accept": "application/json"}
        body = json.dumps({
            "query": query,
            "search_depth": "basic",
            "topic": "general",
            "max_results": self._max_results,
            "include_answer": False,
            "include_domains": list(self._domains),
            "include_published_date": True,
        }).encode("utf-8")
        retrieved_at = self._clock()
        try:
            status, response_body = self._transport(ENDPOINT, headers, body, self._timeout)
        except ResponseTooLarge:
            # Must come first: ResponseTooLarge is a TransportError, so a later handler would mislabel it.
            return _error(ERROR_RESPONSE_TOO_LARGE, query, retrieved_at)
        except (TransportTimeout, TimeoutError):
            return _error(ERROR_PROVIDER_TIMEOUT, query, retrieved_at)
        except TransportConnectionFailed:
            return _error(ERROR_PROVIDER_CONNECTION_FAILED, query, retrieved_at)
        except Exception:
            # Everything else (a failure after the connection was made, a protocol error, an unexpected exception, an
            # OSError from an injected transport) is deliberately NOT called a connection failure.
            return _error(ERROR_TRANSPORT_FAILED, query, retrieved_at)

        if not isinstance(status, int) or isinstance(status, bool):
            return _error(ERROR_RESPONSE_MALFORMED, query, retrieved_at)
        if status == 429:
            return _error(ERROR_RATE_LIMITED, query, retrieved_at)
        if status in (432, 433):
            return _error(ERROR_USAGE_LIMIT, query, retrieved_at)
        if 400 <= status < 500:
            return _error(ERROR_PROVIDER_REJECTED, query, retrieved_at)
        if 500 <= status < 600:
            return _error(ERROR_PROVIDER_UNAVAILABLE, query, retrieved_at)
        if not 200 <= status < 300 or not isinstance(response_body, (bytes, bytearray)):
            return _error(ERROR_RESPONSE_MALFORMED, query, retrieved_at)

        try:
            document = json.loads(bytes(response_body).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _error(ERROR_RESPONSE_MALFORMED, query, retrieved_at)
        raw_results = document.get("results") if isinstance(document, dict) else None
        if not isinstance(raw_results, list):
            return _error(ERROR_RESPONSE_MALFORMED, query, retrieved_at)

        items, dropped = [], 0
        for raw in raw_results:
            item = _item(raw, self._domains)
            if item is None:
                dropped += 1
            elif len(items) < self._max_results:
                items.append(item)
            else:
                dropped += 1
        if not items:
            return ResearchResult(STATUS_ERROR, ERROR_NO_RESULTS, query, (), dropped, retrieved_at)
        return ResearchResult(STATUS_OK, None, query, tuple(items), dropped, retrieved_at)
