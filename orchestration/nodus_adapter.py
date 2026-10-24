"""A thin Nodus-backed task runner that works only with an INJECTED function handle (Milestones N-1 to N-3).

Nothing here imports, installs or contacts the Nodus SDK, reads the environment, or opens a connection. The caller
supplies `handle_factory`, a function that returns a Nodus function handle when called. With the real SDK that
would be something like `lambda: nodus.Function.from_name("cropsync-summary", "summarize_record")`, written by the
caller, so this module loads (and its tests run) on a machine where the SDK is absent.

What the installed SDK source shows (nodus-compute 1.0.23, wheel SHA-256 ad203a70...ec29, read first-hand on
2026-10-18; `tests/test_nodus_sdk_contract.py` re-checks each fact against the real classes when the SDK can be
imported) and what this adapter uses
  * `Function.from_name(app, name)` gives a handle; `handle.spawn(*args)` starts one call and returns a
    `FunctionCall` handle without waiting; `call.get(timeout=None)` waits for the result and `call.cancel()` asks the
    service to cancel (it sets the call's state to Cancelled). These are the only SDK calls used.
  * `get(timeout)` waits with `asyncio.wait_for`, so a local wait timeout raises `TimeoutError` (on Python 3.10 the
    asyncio class of the same name, which does not inherit from the built-in). Timing out stops only the LOCAL wait.
  * An exception raised INSIDE the remote function is re-raised locally under its own type when that type can be
    imported, always chained (`__cause__`) from `nodus.errors.RemoteError`; otherwise `RemoteError` itself is raised.
    So the function's own `TimeoutError` (or a function that raises a class that merely looks like an SDK error) is
    told apart from a local wait timeout by that chain.
  * Service errors derive from `nodus.errors.NodusError` and live in `nodus.errors`. The adapter classifies an
    exception ONLY by class names found in its MRO for classes whose module starts with "nodus", plus the local wait
    timeout rule above; a class that merely borrows an SDK name is not trusted. It never reads or keeps a message,
    a traceback, a request id or a response body, and the SDK classes are not imported.

Mapping (closed codes of `orchestration.task_runner`)
  * limit (`task_limit_reached`): InsufficientCredits, BudgetExceeded, QuotaExceeded, PaymentMethodRequired,
    ArrearsOutstanding, PaymentDisputed, TrialLimitExceeded.
  * unavailable (`task_runner_unavailable`): TooManyRequests, Unavailable, CapacityUnavailable, AdmissionsPaused,
    InternalError, PaymentVerificationUnavailable, PaymentsNotConfigured, ComputeSuspended, RequestInProgress,
    and APIConnectionError (connection broke or the API could not be reached).
  * rejected (`task_rejected`): NotFound, FunctionNotFound, Invalid, BadRequest, Unsupported, UnsupportedMediaType,
    Unauthorized, Forbidden, RequestEntityTooLarge, MethodNotAllowed.
  * timeout (`task_timeout`): `APITimeoutError` (the SDK's client-side request timeout, checked before its parent
    APIConnectionError), `FunctionCallFailed` whose `reason` is exactly "DeadlineExceeded" (the function's own remote
    deadline), and a local wait timeout.
  * failed (`task_failed`): everything else, including RemoteError, any remote exception, other FunctionCallFailed
    reasons, NodusError itself and every exception the adapter does not know.

Timing: `timeout_seconds` bounds only the wait for the result (`get`). `spawn` itself can block before it returns:
the SDK's transport retries transient failures (and writes that carry an idempotency key) for up to 300 seconds, and
`cancel` is the same kind of call. So there is no total-time guarantee, and this adapter does not pretend to add one
(it makes one call, never retries, never sleeps and starts no background work).

Cancellation is best-effort: after a CONFIRMED local wait timeout (not a remote exception, not an API timeout), the
adapter calls `call.cancel()` once if the call object has one, ignores any failure of it, and keeps the original
timeout outcome. `last_cancel` records what happened ("requested", "unsupported", "failed", or None when no cancel was
attempted). A "requested" cancel does not show that remote execution or billing stopped; that is not established.

Data sent: only the payload of `orchestration.task_runner` (sensor_status, soil, air, battery). The SDK pickles it
with cloudpickle and sends it to the Nodus service; the result comes back pickled and is unpickled by the SDK on this
machine, so the service and the remote function must be trusted. This module makes no such call by itself.

Still unestablished: whether a cancelled or timed-out call stops billing; how `max_cost` is accounted; pricing and
data-retention terms beyond what the vendor pages say.

The interface a real handle must satisfy, exactly (pinned by tests/test_integration_readiness.py)
  * `handle.spawn(payload)` is called once, with ONE positional argument (a plain-JSON dict with the keys
    `task_runner.PAYLOAD_KEYS`) and no keyword arguments, and returns a call object;
  * `call.get(timeout=<seconds>)` is called once and must return a dict with exactly `task_runner.SUMMARY_KEYS` whose
    values equal `task_runner.expected_summary(payload)`.
The remote function lives OUTSIDE this repository (a standalone folder), because deploying uploads the whole folder
that holds the function.
"""

import importlib.util
import sys

from orchestration.task_runner import (
    ERROR_TASK_FAILED,
    ERROR_TASK_LIMIT,
    ERROR_TASK_REJECTED,
    ERROR_TASK_TIMEOUT,
    ERROR_TASK_UNAVAILABLE,
    STATUS_OK,
    TASK_SUMMARIZE_RECORD,
    TaskResult,
    error_result,
)

SDK_DISTRIBUTION = "nodus-compute"
SDK_IMPORT_NAME = "nodus"
# Names only. Their values are never read here; the SDK resolves them itself.
AUTH_ENV_VAR_NAMES = ("NODUS_API_KEY", "NODUS_API_URL", "NODUS_ORG", "NODUS_PROJECT", "NODUS_CONTEXT",
                      "NODUS_CONFIG")
DOCUMENTED_PYTHON_MIN = (3, 10)
DOCUMENTED_PYTHON_MAX_EXCLUSIVE = (3, 14)   # the guide says 3.10 to 3.13; the PyPI metadata sets only a minimum

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600

_LIMIT = ("InsufficientCredits", "BudgetExceeded", "QuotaExceeded", "PaymentMethodRequired", "ArrearsOutstanding",
          "PaymentDisputed", "TrialLimitExceeded")
_UNAVAILABLE = ("TooManyRequests", "Unavailable", "CapacityUnavailable", "AdmissionsPaused", "InternalError",
                "PaymentVerificationUnavailable", "PaymentsNotConfigured", "ComputeSuspended", "RequestInProgress",
                "APIConnectionError")
_REJECTED = ("NotFound", "FunctionNotFound", "Invalid", "BadRequest", "Unsupported", "UnsupportedMediaType",
             "Unauthorized", "Forbidden", "RequestEntityTooLarge", "MethodNotAllowed")
_ERROR_NAMES = {
    **{name: ERROR_TASK_LIMIT for name in _LIMIT},
    **{name: ERROR_TASK_UNAVAILABLE for name in _UNAVAILABLE},
    **{name: ERROR_TASK_REJECTED for name in _REJECTED},
    "APITimeoutError": ERROR_TASK_TIMEOUT,      # a subclass of APIConnectionError: found first in the MRO
    "FunctionCallFailed": ERROR_TASK_FAILED,    # refined below by its `reason`
    "RemoteError": ERROR_TASK_FAILED,
    "NodusError": ERROR_TASK_FAILED,
}
_DEADLINE_REASON = "DeadlineExceeded"

CANCEL_REQUESTED = "requested"
CANCEL_UNSUPPORTED = "unsupported"
CANCEL_FAILED = "failed"


def sdk_installed():
    """Whether a module named `nodus` is available. It is NOT imported, so nothing of it runs. Never raises: a
    module already present in `sys.modules` counts as available unless its entry is None (import blocked), and a
    lookup error means "not available"."""
    if SDK_IMPORT_NAME in sys.modules:
        return sys.modules[SDK_IMPORT_NAME] is not None
    try:
        return importlib.util.find_spec(SDK_IMPORT_NAME) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def python_is_documented(version=None):
    """Whether `version` (a tuple or list of ints; default: this interpreter) is inside the range the vendor
    documents. Anything that is not a sequence of at least two whole numbers is not documented (False)."""
    version = sys.version_info[:3] if version is None else version
    if (not isinstance(version, (tuple, list)) or len(version) < 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in version[:2])):
        return False
    return DOCUMENTED_PYTHON_MIN <= tuple(version[:2]) < DOCUMENTED_PYTHON_MAX_EXCLUSIVE


def _is_sdk_class(cls):
    return str(getattr(cls, "__module__", "")).split(".")[0] == SDK_IMPORT_NAME


def _chained_from_remote(error):
    """True if the exception was raised inside the remote function: the SDK chains those from `RemoteError`."""
    seen = 0
    cause = getattr(error, "__cause__", None)
    while cause is not None and seen < 8:
        if any(c.__name__ == "RemoteError" and _is_sdk_class(c) for c in type(cause).__mro__):
            return True
        cause = getattr(cause, "__cause__", None)
        seen += 1
    return False


def _is_local_wait_timeout(error):
    """The timeout `call.get(timeout=...)` raises when the local wait runs out: the built-in `TimeoutError`
    (Python 3.11 and later, where asyncio's is the same class) or asyncio's own class of that name (3.10)."""
    if isinstance(error, TimeoutError):
        return True
    cls = type(error)
    return cls.__name__ == "TimeoutError" and cls.__module__ == "asyncio.exceptions"


def _classify(error):
    """A closed error code from the exception's class and chain only. The message and traceback are not used."""
    if _chained_from_remote(error):
        return ERROR_TASK_FAILED                      # the function's own failure, whatever type it borrowed
    for cls in type(error).__mro__:
        code = _ERROR_NAMES.get(cls.__name__)
        if code is not None and _is_sdk_class(cls):
            if cls.__name__ == "FunctionCallFailed" and getattr(error, "reason", None) == _DEADLINE_REASON:
                return ERROR_TASK_TIMEOUT
            return code
    if _is_local_wait_timeout(error):
        return ERROR_TASK_TIMEOUT
    return ERROR_TASK_FAILED


class NodusTaskRunner:
    """Runs the summary task through an injected Nodus function handle (see the module docstring)."""

    LABEL = "nodus-task-runner (through an injected function handle)"

    def __init__(self, handle_factory, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
        if not callable(handle_factory):
            raise TypeError("handle_factory must be callable")
        # Comparisons only: they are false for NaN, and valid for any size of whole number or infinity.
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise ValueError("timeout_seconds must be a number above 0 and at most %d" % MAX_TIMEOUT_SECONDS)
        self._factory = handle_factory
        self._timeout = timeout_seconds
        self.last_cancel = None   # what the last run did about cancellation (see the module docstring)

    def _cancel_best_effort(self, call):
        """Ask the call to cancel, once. Never raises and never changes the outcome the caller reports."""
        cancel = getattr(call, "cancel", None)
        if not callable(cancel):
            self.last_cancel = CANCEL_UNSUPPORTED
            return
        try:
            cancel()
            self.last_cancel = CANCEL_REQUESTED
        except Exception:
            self.last_cancel = CANCEL_FAILED

    def run(self, task, payload):
        self.last_cancel = None
        if task != TASK_SUMMARIZE_RECORD:
            return error_result(ERROR_TASK_REJECTED, self.LABEL)
        try:
            handle = self._factory()
        except Exception:
            return error_result(ERROR_TASK_UNAVAILABLE, self.LABEL)
        if not callable(getattr(handle, "spawn", None)):
            return error_result(ERROR_TASK_UNAVAILABLE, self.LABEL)
        call = None
        try:
            call = handle.spawn(payload)       # may block and retry inside the SDK before it returns
            output = call.get(timeout=self._timeout)   # bounds only this wait
        except Exception as error:
            code = _classify(error)
            if (call is not None and code == ERROR_TASK_TIMEOUT and not _chained_from_remote(error)
                    and _is_local_wait_timeout(error)):
                self._cancel_best_effort(call)
            return error_result(code, self.LABEL)
        # The answer is checked by the step (`task_runner.checked_summary`) before anything is stored.
        return TaskResult(STATUS_OK, None, output, self.LABEL, False)
