"""A provider-neutral "task runner" boundary, a deterministic fake, and a minimal Record-summary chain (N-1).

Purpose: the first integration boundary for a remote compute service (Nodus is the first candidate), built so that
everything can be tested offline. It reuses the existing runner (`orchestration.run.execute_run`, `Step`,
`StepFailure`); it is not a second orchestration engine and has no scheduler, queue, retry or agent loop.

The boundary
  * A task runner is any object with `run(task, payload) -> TaskResult`.
  * `TaskResult` carries a status ("ok" or "error"), a closed `error_code`, the `output`, a label saying which
    runner produced it, and a `fake` flag so fake output can never be mistaken for real output.
  * Closed error codes (`ERROR_CODES`): task_failed, task_timeout, task_rejected, task_limit_reached,
    task_response_malformed, task_runner_unavailable. A runner's own exception text is never kept.

The chain `run_record_summary(record, runner)`
  1. `execute_run` validates the Record first (an invalid Record runs nothing and never reaches the runner).
  2. Step `prepare_payload` (local, deterministic) copies ONLY the sections a summary needs: sensor_status, soil,
     air and battery. The rover id, photo id, leaf label, confidence, versions, time and sequence number are not
     sent.
  3. Step `summarize_record` (depends on step 1) gives that payload to the runner, then checks the answer with
     `validate_summary` before storing it. A failed first step skips this step.

What `validate_summary` / `checked_summary` do: the answer must have exactly the expected keys, whole-number counts
(a bool or a float is refused even if it compares equal), and must EQUAL the counts the payload implies, including
`faulty_sensors` in sorted order. A wrong answer is refused even when it is internally consistent, and the answer is
checked against the payload the first step produced, never against a copy a runner could have edited. Because the
counts are fully determined by the payload, a remote service adds no information to this demonstration task; the task
exists to exercise the boundary, the failure handling and the checking. The summary is counts only. It is not a
diagnosis or advice. The runner label stored in the run is kept only if it is a short printable text (at most 128
characters), else it is stored as None.

Size: a step result is limited by the runner (4 MiB per step, 8 MiB per run, provisional), and the strict
validation keeps a summary far below that.
"""

import copy
from dataclasses import dataclass

from orchestration.run import Step, StepFailure, execute_run

TASK_SUMMARIZE_RECORD = "summarize_record"

STATUS_OK = "ok"
STATUS_ERROR = "error"

ERROR_TASK_FAILED = "task_failed"
ERROR_TASK_TIMEOUT = "task_timeout"
ERROR_TASK_REJECTED = "task_rejected"
ERROR_TASK_LIMIT = "task_limit_reached"
ERROR_TASK_MALFORMED = "task_response_malformed"
ERROR_TASK_UNAVAILABLE = "task_runner_unavailable"
ERROR_CODES = (ERROR_TASK_FAILED, ERROR_TASK_TIMEOUT, ERROR_TASK_REJECTED, ERROR_TASK_LIMIT,
               ERROR_TASK_MALFORMED, ERROR_TASK_UNAVAILABLE)

PAYLOAD_KEYS = ("sensor_status", "soil", "air", "battery")
SUMMARY_KEYS = ("readings_present", "readings_missing", "sensors_ok", "sensors_fault", "faulty_sensors")
_MAX_COUNT = 1000  # far above any Record; keeps a malformed answer from carrying absurd numbers
MAX_LABEL_CHARS = 128


@dataclass(frozen=True)
class TaskResult:
    status: str
    error_code: object   # None, or one of ERROR_CODES
    output: object       # the summary dict when status is "ok", else None
    runner: str          # a short label of the implementation that produced this result
    fake: bool           # True only for output made up by a fake


def error_result(code, runner, fake=False):
    return TaskResult(STATUS_ERROR, code, None, runner, fake)


# --------------------------------------------------------------------------- payload and summary
def minimal_payload(valid_record):
    """A copy of only the sections a summary needs, taken from a VALIDATED Record."""
    return {key: copy.deepcopy(valid_record[key]) for key in PAYLOAD_KEYS}


def _readings(payload):
    values = []
    for section in ("soil", "air", "battery"):
        values += list(payload[section].values())
    return values


def expected_summary(payload):
    """The counts a correct summary of `payload` contains. Used by the fake, and by tests as a reference."""
    values = _readings(payload)
    status = payload["sensor_status"]
    faulty = sorted(name for name, state in status.items() if state == "fault")
    return {"readings_present": sum(1 for v in values if v is not None),
            "readings_missing": sum(1 for v in values if v is None),
            "sensors_ok": sum(1 for state in status.values() if state == "ok"),
            "sensors_fault": len(faulty),
            "faulty_sensors": faulty}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT


def checked_summary(output, payload):
    """A plain copy of `output` if it is a well-formed answer that EQUALS the counts `payload` implies, else None.

    The types are checked first (a bool or a float is not a count even if it compares equal), then the values are
    compared with `expected_summary(payload)`, including the sorted order of `faulty_sensors`. The output is read
    once into a plain dict, and nothing this function does can raise: an object that misbehaves when read is simply
    refused."""
    try:
        if not isinstance(output, dict) or set(output) != set(SUMMARY_KEYS):
            return None
        plain = {key: output[key] for key in SUMMARY_KEYS}
        faulty = plain["faulty_sensors"]
        if not isinstance(faulty, list) or len(faulty) > _MAX_COUNT or not all(isinstance(n, str) for n in faulty):
            return None
        if not all(_count(plain[key]) for key in SUMMARY_KEYS if key != "faulty_sensors"):
            return None
        plain["faulty_sensors"] = list(faulty)
        return plain if plain == expected_summary(payload) else None
    except Exception:
        return None


def validate_summary(output, payload):
    """True only for an answer `checked_summary` accepts."""
    return checked_summary(output, payload) is not None


def _label(value):
    """A short printable text, or None. The label is stored in the run, so it is bounded and must not carry
    control characters or blank text."""
    if isinstance(value, str) and 0 < len(value) <= MAX_LABEL_CHARS and value.isprintable() and value.strip():
        return value
    return None


# --------------------------------------------------------------------------- the fake
class FakeTaskRunner:
    """A deterministic runner that makes up nothing random, uses no network and needs no credential.

    `mode` chooses the behaviour: "ok" returns the expected counts; "fail", "timeout", "rejected" and "limit"
    return those closed errors; "malformed" and "wrong_counts" return answers that must be refused; "raise"
    raises an exception whose message contains the payload (to prove that nothing leaks). `calls` records each
    (task, payload) it was given."""

    LABEL = "fake-task-runner (deterministic; no service was contacted)"
    MODES = ("ok", "fail", "timeout", "rejected", "limit", "malformed", "wrong_counts", "raise")

    def __init__(self, mode="ok"):
        if mode not in self.MODES:
            raise ValueError("mode must be one of the fake runner's modes")
        self._mode = mode
        self.calls = []

    def run(self, task, payload):
        self.calls.append((task, copy.deepcopy(payload)))
        if task != TASK_SUMMARIZE_RECORD:
            return error_result(ERROR_TASK_REJECTED, self.LABEL, True)
        failures = {"fail": ERROR_TASK_FAILED, "timeout": ERROR_TASK_TIMEOUT, "rejected": ERROR_TASK_REJECTED,
                    "limit": ERROR_TASK_LIMIT}
        if self._mode in failures:
            return error_result(failures[self._mode], self.LABEL, True)
        if self._mode == "raise":
            raise RuntimeError("the fake runner exploded with %r" % (payload,))
        try:
            output = expected_summary(payload)
        except (KeyError, TypeError, AttributeError):
            return error_result(ERROR_TASK_REJECTED, self.LABEL, True)
        if self._mode == "malformed":
            output = ["not", "a", "summary"]
        elif self._mode == "wrong_counts":
            output["readings_missing"] += 1
        return TaskResult(STATUS_OK, None, output, self.LABEL, True)


# --------------------------------------------------------------------------- steps and the chain
def prepare_payload_step():
    """Local, deterministic step: the minimal payload from the (already validated) Record."""
    def call(context):
        return minimal_payload(context["record"])
    return Step("prepare_payload", "local", call)


def task_step(name, runner, task, payload_from):
    """A step that gives the result of step `payload_from` to `runner` as the payload of `task`, checks the
    answer, and stores a summary. Any failure is a closed code; no provider text or exception text is kept."""
    if not callable(getattr(runner, "run", None)):
        raise TypeError("runner must have a run() method")

    def call(context):
        original = context["results"][payload_from]
        # The runner gets its own copy, so nothing it does to that copy can change what the answer is checked
        # against.
        result = runner.run(task, copy.deepcopy(original))
        if not isinstance(result, TaskResult):
            raise StepFailure(ERROR_TASK_MALFORMED)
        if result.status != STATUS_OK:
            raise StepFailure(result.error_code if result.error_code in ERROR_CODES else ERROR_TASK_FAILED)
        summary = checked_summary(result.output, original)
        if summary is None:
            raise StepFailure(ERROR_TASK_MALFORMED)
        return {"task": task, "runner": _label(result.runner), "fake": result.fake is True, "summary": summary}

    return Step(name, "task_runner", call, depends_on=(payload_from,))


def run_record_summary(record, runner, *, record_id=None, run_id=None, run_id_factory=None, clock=None):
    """Validate `record`, prepare the minimal payload, ask `runner` for a summary, and return the run dict
    (the same shape as `execute_run`). Nothing else is added to the run."""
    steps = [prepare_payload_step(), task_step("summarize_record", runner, TASK_SUMMARIZE_RECORD, "prepare_payload")]
    return execute_run(record, steps, record_id=record_id, run_id=run_id, run_id_factory=run_id_factory,
                       clock=clock)
