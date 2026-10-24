"""The NVIDIA request transport with one addition: it remembers the FORM of a `Retry-After` reply header (IA-9).

`image_analysis.nvidia.urllib_transport` returns only `(status, body)`, so a `Retry-After` header never reaches any
diagnostic. `image_analysis/nvidia.py` is owner-protected, so this module does not change it: it provides a second
transport with the same contract (`(url, headers, body, timeout) -> (status, body)`, raising `TransportError`,
`TransportTimeout` or `ResponseTooLarge`) that the demo server can pass to `NvidiaImageAnalyzer(transport=...)`. The
shared helpers (the no-redirect handler, the bounded reader and the local size limit) are imported from the adapter,
never copied or changed, in the same way `research/tavily.py` imports them.

What it keeps, and what it never keeps:
  * kept: whether a `Retry-After` header was present, and its form: a whole number of seconds up to a day (and that
    number), an HTTP date (presence only; it is not compared with any clock) or something unusable;
  * never kept or returned: the header's text, any other header, the body, the request or the key.

It makes one POST per call, never follows a redirect and never retries. A test proves it behaves exactly like
`urllib_transport` (same results, same exceptions) on a battery of replies and failures.
"""

import email.utils
import socket
import urllib.error
import urllib.request

from image_analysis.nvidia import (
    _MAX_RESPONSE_BYTES,
    ResponseTooLarge,
    TransportError,
    TransportTimeout,
    _NoRedirect,
    _read_limited,
)

RETRY_AFTER_MAX_SECONDS = 86400
RETRY_AFTER_KINDS = ("absent", "seconds", "date", "unusable")


def parse_retry_after(headers):
    """{"retry_after": absent | seconds | date | unusable, "retry_after_seconds": int or None} for a response's
    headers (an object with `.get`). Only the form of the value and, for a whole number of seconds up to a day, that
    number are returned: never the header's text."""
    unusable = {"retry_after": "unusable", "retry_after_seconds": None}
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except Exception:
        return unusable
    if value is None:
        return {"retry_after": "absent", "retry_after_seconds": None}
    if not isinstance(value, str):
        return unusable
    text = value.strip()
    if text.isascii() and text.isdigit() and len(text) <= 9:
        seconds = int(text)
        return ({"retry_after": "seconds", "retry_after_seconds": seconds} if seconds <= RETRY_AFTER_MAX_SECONDS
                else unusable)
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except Exception:
        return unusable
    return {"retry_after": "date", "retry_after_seconds": None} if parsed else unusable


class HeaderCapturingTransport:
    """`urllib_transport` plus a note of the `Retry-After` form of the LAST reply (see `retry_after_info`)."""

    def __init__(self):
        self._info = None

    def retry_after_info(self):
        """A copy of the last reply's `parse_retry_after` result, or None if no reply arrived."""
        return dict(self._info) if self._info is not None else None

    def __call__(self, url, headers, body, timeout):
        self._info = None
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                self._info = parse_retry_after(getattr(response, "headers", None))
                return response.status, _read_limited(response, _MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            self._info = parse_retry_after(getattr(error, "headers", None))
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
