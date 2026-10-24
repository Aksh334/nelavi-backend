"""Minimal, offline, synchronous run orchestration (Milestone 1B).

`execute_run` runs an explicitly supplied, ordered list of `Step`s for one Record and returns a
JSON-serialisable dict describing the run. That is all it does. It is not a framework: there is no
provider code, no network, no clock other than the injected one, no thread, no queue and no storage.

    execute_run(record, steps, *, record_id=None, run_id=None, run_id_factory=None, clock=None)

Identity
  * `run_id` is the identity of ONE execution. It is the caller's `run_id`, or one made by `run_id_factory`,
    or (default) `secrets.token_urlsafe(16)`. It is never derived from the Record. Passing both `run_id` and
    `run_id_factory` is an error. Running the same Record twice makes two runs: nothing here deduplicates,
    and nothing here is idempotent.
  * `check_run_arguments(record_id=, run_id=, run_id_factory=, clock=)` validates those four arguments without
    calling the factory or the clock; `execute_run` starts with it, and callers that must refuse something else
    first (such as `pipeline_steps.run_report`) can use it so a bad argument is never silently ignored.
  * `record_id` is optional and is the storage id (`StoredRecord.id`) the CALLER supplies. It is a positive
    whole number or None. It is never inferred from `rover_id`, `sequence_number`, `photo_id` or `time`.
    The result says `record_id_scope: "local_store"` when one was supplied, else `"unavailable"`.
    A local store id is unique only within the one SQLite database that issued it. It is not a global,
    rover-wide or contract-level identity (frozen contract items R-3 and R-4 stay open).

Steps (see `Step`)
  * Steps run strictly in the supplied order. A step's `call` is called with ONE argument, a dict:
    {"run_id", "record_id", "record" (a private copy), "results" (the results of the steps it depends on)}.
    It returns a JSON-serialisable result. Returning anything else makes the step fail.
  * `steps` must be a `list` or `tuple`, in the order to run. A set, frozenset, string, bytes, dict, generator
    or any other container is a TypeError, because its order would not be guaranteed. An EMPTY list is a
    ValueError: a run with no steps is rejected, never reported as "skipped" or "completed".
  * Every Step is validated and then COPIED into an immutable snapshot (its `depends_on` becomes a tuple)
    before the first step runs. Later changes to the caller's `Step` objects or lists, including changes made
    by a running step, do not affect this run.
  * `depends_on` names EARLIER steps. A step runs only if every step it depends on completed. Otherwise it
    is `skipped` with `skip_reason` "dependency_not_completed". A step with no `depends_on` is independent
    and runs even after an earlier failure. A dependency that was itself skipped counts as not completed.
  * `skip_reason` on a Step skips it on purpose (for example "provider_not_configured"). It is reported as
    `skipped`, never as `completed`, and its dependants are skipped too.
  * A step that raises fails with a controlled `error_code`: the `code` of a `StepFailure` if it is a
    short lower-case token, else "step_failed". A result that is not JSON-serialisable fails with
    "result_not_serializable". Exception messages, exception type names and tracebacks are never kept.
  * Step statuses move pending -> running -> completed | failed, or pending -> skipped. The run is
    synchronous, so the returned dict only ever shows the final states.

Run status, decided from the final step statuses in this order:
  "failed"          at least one step failed (other steps may have completed or been skipped);
  "skipped"         no step failed and EVERY step was skipped, so nothing ran;
  "partial"         no step failed, at least one step completed and at least one was skipped;
  "completed"       every step completed;
  "invalid_record"  the Record failed validation; no step ran.

Step authors are responsible for what they return and raise:
  * A step must return sanitised, JSON-compatible data. The result is stored and returned as given: this
    module cannot tell a secret, provider text or personal data from anything else.
  * The result is normalised through the standard `json` module, so Python-specific types change: tuples
    become lists, integer dict keys become strings (and `{1: "a", "1": "b"}` collapses to one key), and
    anything JSON cannot hold (sets, objects, NaN, circular structures) fails the step
    (`result_not_serializable`, which takes precedence over the size check).
  * SIZE: a step's serialised result may be at most `MAX_STEP_RESULT_BYTES` (provisional, 4 MiB) long, measured as
    the length of the JSON text this module produces (ASCII-escaped: one character is one byte, so non-ASCII text
    counts at up to 6 per character, and a value is never smaller than its UTF-8 encoding). This is serialised
    size, not Python memory, and it is checked after the text exists, so it bounds what is STORED and returned, not
    the transient memory of a step that builds a huge object. A result over the limit FAILS the step with
    `result_too_large`: nothing is truncated, dropped or kept, and the step's `result` stays None. Independent
    steps continue and dependants are skipped, as for any failed step.
  * RUN TOTAL: the serialised lengths of the results a run actually STORES are added up (provisional limit
    `MAX_RUN_RESULT_BYTES`, 8 MiB; exactly the limit is allowed). A step whose result would take the total over it
    fails with `result_too_large`, stores nothing and adds nothing. Failed and skipped steps count zero. Steps run
    in order, so an earlier large result can leave too little room for a LATER INDEPENDENT step, which is still
    called and then fails; this is deterministic but order-dependent. Like the step limit this bounds what is
    stored, not peak memory: the text and the returned object exist before the check.
  * `StepFailure` codes and `skip_reason` values must be FIXED, non-sensitive tokens written in the code, never
    built from provider output, input values or secrets. A token shape is checked; its meaning is not.
  * Exception messages, exception type names and tracebacks are never kept.
  * The `errors` list of an "invalid_record" run holds the validator's paths, which for an unknown field are the
    submitted key names (the same as `agri_pipeline.pipeline.run`). Values are never included.
  * The clock must keep returning strings: a clock that fails or returns a non-string after the run has started
    aborts the call with an exception and the run is lost.

Not provided, on purpose: retries, timeouts (a step that never returns blocks the caller), cancellation,
asynchronous execution, persistence and recovery. A crash loses the run.

Boundary with `agri_pipeline.pipeline.run`: this module does not call it and does not build a report. A later,
separately approved step decides how step results become the pipeline's `findings_result`, `research_result`
and `evidence` arguments.
"""

import copy
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from record.validation import validate_record

VERSION = "1"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

RUN_COMPLETED = "completed"
RUN_PARTIAL = "partial"
RUN_SKIPPED = "skipped"
RUN_FAILED = "failed"
RUN_INVALID_RECORD = "invalid_record"

SCOPE_LOCAL_STORE = "local_store"
SCOPE_UNAVAILABLE = "unavailable"

ERROR_STEP_FAILED = "step_failed"
ERROR_RESULT_NOT_SERIALIZABLE = "result_not_serializable"
ERROR_RESULT_TOO_LARGE = "result_too_large"

# PROVISIONAL limits on SERIALISED results (see ARCHITECTURE_DECISIONS.md, OR-8 and OR-9). Both measure the length
# of the JSON text this module itself produces with `json.dumps` (ASCII-escaped, so one character is one byte):
# serialised size, NOT Python memory. They are owner-approved provisional policy, not frozen contracts.
# Per step: the number reuses the project's existing local limit for a provider response body
# (`image_analysis.nvidia._MAX_RESPONSE_BYTES`, shared by the Tavily adapters).
MAX_STEP_RESULT_BYTES = 4 * 1024 * 1024
# Per run: the sum of the serialised lengths of the results the run STORED (completed steps only). Exactly this
# many is allowed; one more is not.
MAX_RUN_RESULT_BYTES = 8 * 1024 * 1024
SKIP_DEPENDENCY = "dependency_not_completed"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StepFailure(Exception):
    """Raised by a step to fail with a controlled `code` (a short lower-case token). The message is ignored."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Step:
    name: str           # stable, unique, lower-case token
    provider: str       # label only, same token rules, for example "local" or "nvidia"; never imported here
    call: object        # callable taking the one context dict
    depends_on: tuple = ()
    skip_reason: object = None   # None, or a token: skip this step on purpose


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_steps(steps):
    """Validate `steps` and return an immutable snapshot: a tuple of NEW Step objects whose `depends_on`
    is a tuple. Each attribute of each caller Step is read once, so later changes cannot affect the run."""
    if not isinstance(steps, (list, tuple)):
        raise TypeError("steps must be a list or tuple of Step objects, in the order to run")
    if not steps:
        raise ValueError("steps must not be empty")
    seen = set()
    snapshot = []
    for step in steps:
        if not isinstance(step, Step):
            raise TypeError("every step must be a Step")
        name, provider, call = step.name, step.provider, step.call
        depends_on, skip_reason = step.depends_on, step.skip_reason
        for label, value in (("name", name), ("provider", provider)):
            if not isinstance(value, str) or _NAME.fullmatch(value) is None:
                raise ValueError("step %s must be a lower-case token" % label)
        if name in seen:
            raise ValueError("duplicate step name")
        if not callable(call):
            raise TypeError("step call must be callable")
        if not isinstance(depends_on, (tuple, list)) or not all(isinstance(d, str) for d in depends_on):
            raise TypeError("depends_on must be a tuple of step names")
        depends_on = tuple(depends_on)
        if any(d not in seen for d in depends_on):
            raise ValueError("depends_on must name earlier steps only")
        if skip_reason is not None and (not isinstance(skip_reason, str)
                                        or _CODE.fullmatch(skip_reason) is None):
            raise ValueError("skip_reason must be None or a lower-case token")
        seen.add(name)
        snapshot.append(Step(name, provider, call, depends_on, skip_reason))
    return tuple(snapshot)


def _check_record_id(record_id):
    if record_id is not None and (isinstance(record_id, bool) or not isinstance(record_id, int)
                                  or record_id < 1):
        raise ValueError("record_id must be None or a positive whole number (a local storage id)")


def _valid_run_id(run_id):
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must be 1 to 64 letters, digits, '-' or '_'")
    return run_id


def check_run_arguments(*, record_id=None, run_id=None, run_id_factory=None, clock=None):
    """Raise TypeError or ValueError (fixed messages, no submitted values) if a run argument is unusable.

    Checks, in this order: `record_id`; `run_id` (and that `run_id` and `run_id_factory` are not both given);
    that a given `run_id_factory` and a given `clock` are callable. It NEVER calls the factory or the clock, so
    it is safe to use before anything is started. What a factory returns can only be checked when it is called,
    and `execute_run` does that. `execute_run` itself starts with this check, so the two always agree."""
    _check_record_id(record_id)
    if run_id is not None and run_id_factory is not None:
        raise ValueError("give run_id or run_id_factory, not both")
    if run_id is not None:
        _valid_run_id(run_id)
    if run_id_factory is not None and not callable(run_id_factory):
        raise TypeError("run_id_factory must be callable")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable")


def _choose_run_id(run_id, run_id_factory):
    # The arguments were already checked by `check_run_arguments`.
    if run_id is None:
        run_id = run_id_factory() if run_id_factory is not None else secrets.token_urlsafe(16)
    return _valid_run_id(run_id)


def _stamp(clock):
    value = clock()
    if not isinstance(value, str):
        raise TypeError("clock must return a string")
    return value


class _ResultTooLarge(Exception):
    """Internal signal: a step's serialised result is longer than MAX_STEP_RESULT_BYTES. Carries nothing."""


def _serialisable(result, stored_so_far=0):
    """`(copy, length)`: a private JSON round-trip copy of `result` and the length of its serialised text. Raises
    ValueError/TypeError if it is not JSON, or `_ResultTooLarge` if the text is over MAX_STEP_RESULT_BYTES or
    would take the run's stored total (`stored_so_far`) over MAX_RUN_RESULT_BYTES. The text is produced once;
    its length is checked BEFORE it is parsed into the stored copy, so no second serialisation is needed and an
    oversized value is never turned into a stored copy."""
    text = json.dumps(result, allow_nan=False)
    if len(text) > MAX_STEP_RESULT_BYTES or stored_so_far + len(text) > MAX_RUN_RESULT_BYTES:
        raise _ResultTooLarge()
    return json.loads(text), len(text)


def _entry(step):
    return {"name": step.name, "provider": step.provider, "status": STEP_PENDING,
            "depends_on": list(step.depends_on), "started_at": None, "completed_at": None,
            "result": None, "error_code": None, "skip_reason": None}


def execute_run(record, steps, *, record_id=None, run_id=None, run_id_factory=None, clock=None):
    """Run `steps` in order for one Record and return the run dict (see the module docstring).

    Bad arguments (steps, record_id, run_id, clock) raise TypeError or ValueError; so does an empty `steps`.
    A Record that fails
    validation is NOT an exception: the run is returned with status "invalid_record", the validator's
    {"path", "code"} pairs in `errors`, and no step is run, like `agri_pipeline.pipeline.run`.
    """
    steps = _check_steps(steps)
    check_run_arguments(record_id=record_id, run_id=run_id, run_id_factory=run_id_factory, clock=clock)
    run_id = _choose_run_id(run_id, run_id_factory)
    clock = clock if clock is not None else _now

    started_at = _stamp(clock)
    scope = SCOPE_LOCAL_STORE if record_id is not None else SCOPE_UNAVAILABLE
    run = {"version": VERSION, "run_id": run_id, "record_id": record_id, "record_id_scope": scope,
           "status": None, "started_at": started_at, "completed_at": None, "steps": [], "errors": []}

    checked = validate_record(record)
    if not checked.valid:
        run["status"] = RUN_INVALID_RECORD
        run["errors"] = [{"path": e.path, "code": e.code} for e in checked.errors]
        run["completed_at"] = _stamp(clock)
        return run
    valid_record = checked.record

    entries = {step.name: _entry(step) for step in steps}
    run["steps"] = [entries[step.name] for step in steps]
    stored_total = 0  # serialised length of the results stored so far in THIS run
    for step in steps:
        entry = entries[step.name]
        if step.skip_reason is not None:
            entry["status"], entry["skip_reason"] = STEP_SKIPPED, step.skip_reason
            continue
        if any(entries[d]["status"] != STEP_COMPLETED for d in step.depends_on):
            entry["status"], entry["skip_reason"] = STEP_SKIPPED, SKIP_DEPENDENCY
            continue
        context = {"run_id": run_id, "record_id": record_id, "record": copy.deepcopy(valid_record),
                   "results": {d: copy.deepcopy(entries[d]["result"]) for d in step.depends_on}}
        entry["status"], entry["started_at"] = STEP_RUNNING, _stamp(clock)
        try:
            result = step.call(context)
        except StepFailure as failure:
            code = failure.code if isinstance(failure.code, str) and _CODE.fullmatch(failure.code) else None
            entry["status"], entry["error_code"] = STEP_FAILED, code or ERROR_STEP_FAILED
        except Exception:  # the message and type are discarded on purpose
            entry["status"], entry["error_code"] = STEP_FAILED, ERROR_STEP_FAILED
        else:
            try:
                entry["result"], size = _serialisable(result, stored_total)
                entry["status"] = STEP_COMPLETED
                stored_total += size  # only a result that was actually stored counts, and only once
            except _ResultTooLarge:  # nothing is truncated or kept; the step simply fails with a closed code
                entry["status"], entry["error_code"] = STEP_FAILED, ERROR_RESULT_TOO_LARGE
            except (ValueError, TypeError, RecursionError):
                entry["status"], entry["error_code"] = STEP_FAILED, ERROR_RESULT_NOT_SERIALIZABLE
        entry["completed_at"] = _stamp(clock)

    statuses = [e["status"] for e in run["steps"]]
    if STEP_FAILED in statuses:
        run["status"] = RUN_FAILED
    elif all(status == STEP_SKIPPED for status in statuses):
        run["status"] = RUN_SKIPPED  # `statuses` is never empty: an empty `steps` is rejected up front
    elif STEP_SKIPPED in statuses:
        run["status"] = RUN_PARTIAL
    else:
        run["status"] = RUN_COMPLETED
    run["completed_at"] = _stamp(clock)
    return run
