"""NVIDIA hosted-API adapter for the image-analysis boundary (V1).

The only module that knows NVIDIA's endpoint, model, credential variable and
request/response shape. It sends one chat-completions request per `analyze()`
call, with exactly the structure verified in the isolated tests (PC-1, PC-2): a
user message holding a text item and an `image_url` item whose URL is a PNG
base64 data URL. It reads the answer only from `choices[0].message.content`.
Nothing else in the response is used or interpreted.

Credential: `NVIDIA_API_KEY` is read from the process environment on each call.
It is placed only in the Authorization header. It is never logged, returned or
put in an error; errors are fixed codes and carry no provider text.

No retry: one `analyze()` call makes at most one transport call. The timeout
default is an implementation default, not an NVIDIA limit or SLA, and no
reliability, capacity, latency or cost is claimed. Only PNG is verified. An image over
`max_image_bytes` (default 1 MiB, a local limit and not an NVIDIA limit) is rejected as
`image_unsupported` before it is encoded or sent.

Transport seam: a callable `(url, headers, body, timeout) -> (status, body)` that
raises `TransportError` (or `TransportTimeout`) when no response was received, or
`ResponseTooLarge` when the response body exceeds the local 4 MiB limit. The default
transport reads every body, success or HTTP error, in bounded chunks. Each read asks for at
most the bytes still allowed, so its own reads receive at most one byte past the limit in
total and stop as soon as that byte arrives (this does not control buffering inside the HTTP
library). An oversized body is therefore reported as `response_too_large` and never treated
as a complete (truncated) response. The limit is a local limit, not an NVIDIA limit. Tests
supply a fake; the default uses the standard library.
"""

import base64
import json
import os
import socket
import urllib.error
import urllib.request

from image_analysis.boundary import (
    ERROR_CONFIGURATION_INVALID,
    ERROR_CREDENTIAL_MISSING,
    ERROR_IMAGE_UNSUPPORTED,
    ERROR_PROVIDER_CONNECTION_FAILED,
    ERROR_PROVIDER_REJECTED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_RESPONSE_EMPTY,
    ERROR_RESPONSE_MALFORMED,
    ERROR_RESPONSE_TOO_LARGE,
    ERROR_TRANSPORT_FAILED,
    MEDIA_TYPE_PNG,
    AnalysisRequest,
    error_result,
    is_supported_png,
    ok_result,
)

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
API_KEY_ENV_VAR = "NVIDIA_API_KEY"

# Implementation defaults only. DEFAULT_MAX_TOKENS is the value the isolated image
# test sent (PC-2); neither value is an NVIDIA limit or a tuned setting.
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TOKENS = 1024

# Local resource-protection limit on the raw PNG size, checked before the image is
# base64-encoded. It is our own limit, not an NVIDIA API limit, and is not tuned.
DEFAULT_MAX_IMAGE_BYTES = 1024 * 1024

# Local limit on a response body (success or HTTP error), not an NVIDIA limit. A body longer
# than this is reported as `response_too_large`, never silently truncated.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class TransportError(Exception):
    """No HTTP response was received. Carries no provider or request detail."""


class TransportTimeout(TransportError):
    """No HTTP response was received before the timeout."""


class ResponseTooLarge(TransportError):
    """A response body exceeded the local size limit. Carries no provider or response detail."""


def _read_limited(stream, limit):
    """Return the whole body of `stream` if it is at most `limit` bytes, else raise ResponseTooLarge.

    Reads in bounded chunks until the stream is exhausted, so a short read never ends the body
    early. Each read asks for at most the bytes still allowed (`limit + 1` minus what has
    already arrived), so a stream that honours the size it is asked for delivers at most
    `limit + 1` bytes in total and an oversized body is detected without being read in full.
    This bounds the reads made here, not buffering inside the HTTP library. A body of exactly
    `limit` bytes is returned.
    """
    chunks = []
    total = 0
    while total <= limit:
        chunk = stream.read(min(_READ_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise ResponseTooLarge()
    return b"".join(chunks)


class NvidiaConfig:
    """Adapter settings. Validated when the analyzer runs, not on construction."""

    __slots__ = ("endpoint", "model", "timeout_seconds", "max_tokens", "max_image_bytes")

    def __init__(self, endpoint=ENDPOINT, model=MODEL, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 max_tokens=DEFAULT_MAX_TOKENS, max_image_bytes=DEFAULT_MAX_IMAGE_BYTES):
        self.endpoint = endpoint
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.max_image_bytes = max_image_bytes

    def is_valid(self):
        return (
            isinstance(self.endpoint, str) and self.endpoint.startswith("https://")
            and isinstance(self.model, str) and self.model.strip() != ""
            and isinstance(self.timeout_seconds, (int, float))
            and not isinstance(self.timeout_seconds, bool) and self.timeout_seconds > 0
            and isinstance(self.max_tokens, int)
            and not isinstance(self.max_tokens, bool) and self.max_tokens > 0
            and isinstance(self.max_image_bytes, int)
            and not isinstance(self.max_image_bytes, bool) and self.max_image_bytes > 0
        )

    def __repr__(self):
        return "NvidiaConfig(endpoint=%r, model=%r)" % (self.endpoint, self.model)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. The 3xx response is returned to the caller as-is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(url, headers, body, timeout):
    """Default transport: one POST using the standard library. Redirects are not followed."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _read_limited(response, _MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        try:
            return error.code, _read_limited(error, _MAX_RESPONSE_BYTES)
        except ResponseTooLarge:
            raise  # an oversized error body is reported as such, not as an empty body
        except Exception:
            return error.code, b""
        finally:
            try:
                error.close()
            except Exception:
                pass  # cleanup must not replace the result already decided above
    except (socket.timeout, TimeoutError):
        raise TransportTimeout() from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise TransportTimeout() from None
        raise TransportError() from None
    except OSError:
        raise TransportError() from None


def _key_is_usable(key):
    return isinstance(key, str) and key.strip() != "" and not any(c in key for c in "\r\n\x00")


def api_key_present(environ=None):
    """Return True or False: whether a usable `NVIDIA_API_KEY` is set. Never returns the value.

    `environ` is for tests; None means the process environment.
    """
    source = environ if environ is not None else os.environ
    return _key_is_usable(source.get(API_KEY_ENV_VAR))


def _data_url(png_bytes):
    return "data:%s;base64,%s" % (MEDIA_TYPE_PNG, base64.b64encode(png_bytes).decode("ascii"))


def _build_body(config, prompt, png_bytes):
    payload = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_url(png_bytes)}},
            ],
        }],
    }
    return json.dumps(payload).encode("utf-8")


def _parse_content(body):
    """Return (text, None) or (None, error_code) from choices[0].message.content."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None, ERROR_RESPONSE_MALFORMED
    if not isinstance(document, dict):
        return None, ERROR_RESPONSE_MALFORMED
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, ERROR_RESPONSE_MALFORMED
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None, ERROR_RESPONSE_MALFORMED
    content = message.get("content")
    if not isinstance(content, str):
        return None, ERROR_RESPONSE_MALFORMED
    if content.strip() == "":
        return None, ERROR_RESPONSE_EMPTY
    return content, None


class NvidiaImageAnalyzer:
    """ImageAnalyzer backed by NVIDIA's hosted chat-completions API."""

    def __init__(self, config=None, transport=None, environ=None):
        self._config = config if config is not None else NvidiaConfig()
        self._transport = transport if transport is not None else urllib_transport
        self._environ = environ  # None means the process environment, read per call

    def analyze(self, request):
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        config = self._config
        if not isinstance(config, NvidiaConfig) or not config.is_valid():
            return error_result(ERROR_CONFIGURATION_INVALID)
        model = config.model
        if not is_supported_png(request.image):
            return error_result(ERROR_IMAGE_UNSUPPORTED, model)
        if len(request.image.data) > config.max_image_bytes:
            # Over the local size limit: rejected before any base64 encoding or request body
            # is built. No new error code; an image this adapter will not send is unsupported.
            return error_result(ERROR_IMAGE_UNSUPPORTED, model)
        if not isinstance(request.prompt, str) or request.prompt.strip() == "":
            # The closed error set has no prompt code; an unusable prompt is an
            # invalid request configuration.
            return error_result(ERROR_CONFIGURATION_INVALID, model)

        environ = self._environ if self._environ is not None else os.environ
        key = environ.get(API_KEY_ENV_VAR)
        if not _key_is_usable(key):
            return error_result(ERROR_CREDENTIAL_MISSING, model)

        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = _build_body(config, request.prompt, request.image.data)
        try:
            status, response_body = self._transport(
                config.endpoint, headers, body, config.timeout_seconds)
        except ResponseTooLarge:
            # Must come before the handlers below: ResponseTooLarge is a TransportError, so a later
            # handler would report an oversized reply as a connection failure.
            return error_result(ERROR_RESPONSE_TOO_LARGE, model)
        except (TransportTimeout, TimeoutError):
            # Only the exception class decides the code; no message, header or body is read.
            return error_result(ERROR_PROVIDER_TIMEOUT, model)
        except (TransportError, OSError):
            # No HTTP response arrived (DNS, refused, reset, TLS...). OSError is what the default
            # transport already treats as a connection failure; a custom transport may raise it directly.
            return error_result(ERROR_PROVIDER_CONNECTION_FAILED, model)
        except Exception:
            # Anything else raised while making the call (for example a header that cannot be
            # encoded, a malformed HTTP exchange or a defect in an injected transport).
            return error_result(ERROR_TRANSPORT_FAILED, model)

        if not isinstance(status, int) or isinstance(status, bool):
            return error_result(ERROR_RESPONSE_MALFORMED, model)
        if 400 <= status < 500:
            return error_result(ERROR_PROVIDER_REJECTED, model)
        if 500 <= status < 600:
            return error_result(ERROR_PROVIDER_UNAVAILABLE, model)
        if not 200 <= status < 300:
            return error_result(ERROR_RESPONSE_MALFORMED, model)
        if not isinstance(response_body, (bytes, bytearray)):
            return error_result(ERROR_RESPONSE_MALFORMED, model)

        text, error_code = _parse_content(bytes(response_body))
        if error_code is not None:
            return error_result(error_code, model)
        return ok_result(text, model)
