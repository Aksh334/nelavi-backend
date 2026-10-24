"""Integrated offline workflow: image findings, research, a compute summary and a synthesis in ONE run.

It only composes existing parts: the step builders and `assemble_report` of `orchestration.pipeline_steps`, the
task steps of `orchestration.task_runner` (any runner, including `NodusTaskRunner` around an injected handle), and
the new synthesis boundary of `orchestration.synthesis`. Nothing here imports a provider SDK, opens a connection or
reads the environment. Which providers are used is decided entirely by the objects the caller injects.

Stages and the steps behind them (all in one `execute_run`):
    image_findings   <- step "image_findings"            (the image-analysis role)
    research         <- step "research_search"           (the research role)
    sensor_summary   <- steps "prepare_payload" + "summarize_record"   (the compute role)
    synthesis        <- step "synthesis", which DEPENDS ON image_findings AND research_search, so if either
                        failed or was skipped it is skipped and nothing is invented
    specialist       <- not a step: always recorded as skipped ("specialist_role_undefined"). No contract for that
                        role exists, so there is no way to enable it here.

The result has the shape of `pipeline_steps.run_report` plus three additive keys:
    "integration"    {"stages": [per-stage status, codes, provider_called, fake], "specialist": {...}}
    "synthesis"      the stored synthesis (generated, unverified text with citations) or None
    "sensor_summary" the stored compute summary or None
`report` is exactly what the existing pipeline builds from the validated image and research results; generated text
never changes it. `provider_called` means the injected object was invoked, not that a remote service was reached.
`check_integrated_result` re-checks a result for internal consistency.
"""

import copy

from orchestration import synthesis as synthesis_module
from orchestration.pipeline_steps import (
    IMAGE_STEP,
    RESEARCH_STEP,
    RUN_NOT_STARTED,
    SKIP_NO_PROVIDER,
    VERSION,
    _record_digest,
    assemble_report,
    check_report_inputs,
    image_findings_step,
    research_step,
)
from orchestration.run import (
    SCOPE_UNAVAILABLE,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_SKIPPED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INVALID_RECORD,
    RUN_PARTIAL,
    RUN_SKIPPED,
    Step,
    StepFailure,
    check_run_arguments,
    execute_run,
)
from orchestration.task_runner import (
    TASK_SUMMARIZE_RECORD,
    prepare_payload_step,
    task_step,
)
from record.validation import validate_record

PAYLOAD_STEP = "prepare_payload"
SUMMARY_STEP = "summarize_record"
SYNTHESIS_STEP = "synthesis"

STAGE_NOT_RUN = "not_run"
SKIP_SPECIALIST = "specialist_role_undefined"

# stage name -> (step name, provenance key)
STAGES = (("image_findings", IMAGE_STEP, IMAGE_STEP),
          ("research", RESEARCH_STEP, RESEARCH_STEP),
          ("sensor_summary", SUMMARY_STEP, "sensor_summary"),
          ("synthesis", SYNTHESIS_STEP, "synthesis"))

RESULT_KEYS = ("version", "run_id", "record_id", "record_id_scope", "run_status", "report", "run", "provenance",
               "integration", "synthesis", "sensor_summary")


def _specialist():
    return {"stage": "specialist", "status": STEP_SKIPPED, "skip_reason": SKIP_SPECIALIST, "provider_called": False}


def _implementation(obj):
    cls = type(obj)
    return synthesis_module.label("%s.%s" % (cls.__module__, cls.__qualname__))


def _skipped_step(name, provider, reason):
    return Step(name, provider, lambda context: None, skip_reason=reason)


# --------------------------------------------------------------------------- steps
class _NotingRunner:
    """Passes `run` through to the real runner and notes, in the caller-owned `notes` dict, that it was invoked."""

    def __init__(self, runner, notes):
        self._runner, self._notes = runner, notes
        self._implementation = _implementation(runner)

    def run(self, task, payload):
        self._notes["sensor_summary"] = {"implementation": self._implementation, "provider_called": True}
        return self._runner.run(task, payload)


def summary_steps(task_runner, notes):
    """The compute stage: payload preparation and the summary, or one skipped step when no runner was given."""
    if task_runner is None:
        return [_skipped_step(SUMMARY_STEP, "task_runner", SKIP_NO_PROVIDER)]
    if not callable(getattr(task_runner, "run", None)):
        raise TypeError("task_runner must have a run() method")
    return [prepare_payload_step(),
            task_step(SUMMARY_STEP, _NotingRunner(task_runner, notes), TASK_SUMMARIZE_RECORD, PAYLOAD_STEP)]


def synthesis_step(synthesis_provider, question, notes):
    """The synthesis step. It depends on the image step AND the research step, and is given only their stored,
    already validated results. The provider's answer is checked against those same inputs; anything else fails
    with a closed code and no provider text is kept."""
    if synthesis_provider is None:
        return _skipped_step(SYNTHESIS_STEP, "synthesis", SKIP_NO_PROVIDER)
    if not callable(getattr(synthesis_provider, "synthesize", None)):
        raise TypeError("synthesis_provider must have a synthesize() method")
    implementation = _implementation(synthesis_provider)

    def call(context):
        inputs = synthesis_module.inputs_from_results(context["results"][IMAGE_STEP],
                                                      context["results"][RESEARCH_STEP], question)
        notes["synthesis"] = {"implementation": implementation, "provider_called": True}
        result = synthesis_provider.synthesize(copy.deepcopy(inputs))
        if not isinstance(result, synthesis_module.SynthesisResult):
            raise StepFailure(synthesis_module.ERROR_MALFORMED)
        if result.status != synthesis_module.STATUS_OK:
            code = result.error_code
            raise StepFailure(code if code in synthesis_module.ERROR_CODES else synthesis_module.ERROR_FAILED)
        checked = synthesis_module.check_synthesis(result.output, inputs)
        if checked is None:
            raise StepFailure(synthesis_module.ERROR_MALFORMED)
        model = synthesis_module.label(result.model)
        notes["synthesis"]["model"] = model
        stored = {"kind": synthesis_module.KIND, "model": model, "fake": result.fake is True}
        stored.update(checked)
        return stored

    return Step(SYNTHESIS_STEP, "synthesis", call, depends_on=(IMAGE_STEP, RESEARCH_STEP))


# --------------------------------------------------------------------------- the result
def _entry(run, name):
    if run is None:
        return None
    for entry in run["steps"]:
        if entry["name"] == name:
            return entry
    return None


def _stage(stage, entry, note):
    if entry is None:
        return {"stage": stage, "status": STAGE_NOT_RUN, "error_code": None, "skip_reason": None,
                "provider_called": False, "fake": None}
    ran = entry["status"] in (STEP_COMPLETED, STEP_FAILED)
    called = bool(ran and isinstance(note, dict) and note.get("provider_called") is True)
    fake = None
    if entry["status"] == STEP_COMPLETED and isinstance(entry["result"], dict) and "fake" in entry["result"]:
        fake = entry["result"]["fake"]
    return {"stage": stage, "status": entry["status"], "error_code": entry["error_code"],
            "skip_reason": entry["skip_reason"], "provider_called": called, "fake": fake}


def _completed_result(run, name):
    entry = _entry(run, name)
    if entry is not None and entry["status"] == STEP_COMPLETED:
        return copy.deepcopy(entry["result"])
    return None


def _extend(base, run, notes):
    """Add the additive keys to a run_report-shaped dict."""
    stages = [_stage(stage, _entry(run, step), notes.get(key)) for stage, step, key in STAGES]
    base["integration"] = {"stages": stages, "specialist": _specialist()}
    base["synthesis"] = _completed_result(run, SYNTHESIS_STEP)
    base["sensor_summary"] = _completed_result(run, SUMMARY_STEP)
    return base


def run_integrated(record, *, findings_provider=None, png_bytes=None, hints=None, research_client=None, query=None,
                   research_skip_reason=None, task_runner=None, synthesis_provider=None, origin="unknown",
                   user=None, image_ref=None, record_id=None, run_id=None, run_id_factory=None, clock=None):
    """Check the inputs, run all four stages with one `execute_run`, assemble the report and add the integration
    block. If the report inputs are refused, nothing runs and no injected object is called."""
    provenance, notes = {}, {}
    steps = [image_findings_step(findings_provider, png_bytes, hints=hints, provenance=provenance),
             research_step(research_client, query, skip_reason=research_skip_reason, provenance=provenance)]
    steps += summary_steps(task_runner, notes)
    steps.append(synthesis_step(synthesis_provider, query, notes))
    # Order: provider objects (above), run arguments, report inputs, then the run.
    check_run_arguments(record_id=record_id, run_id=run_id, run_id_factory=run_id_factory, clock=clock)
    rejected = check_report_inputs(record, origin=origin, user=user, image_ref=image_ref)
    if rejected is not None:
        refused = {"version": VERSION, "run_id": None, "record_id": None, "record_id_scope": SCOPE_UNAVAILABLE,
                   "run_status": RUN_NOT_STARTED, "report": rejected, "run": None, "provenance": {}}
        return _extend(refused, None, {})
    checked = validate_record(record)
    if checked.valid:
        provenance["record_sha256"] = _record_digest(checked.record)
    run = execute_run(record, steps, record_id=record_id, run_id=run_id, run_id_factory=run_id_factory,
                      clock=clock)
    result = assemble_report(record, run, origin=origin, user=user, image_ref=image_ref, query=query,
                             provenance=provenance)
    # assemble_report keeps only the provenance keys it knows, so the two additional entries are added after it.
    for key, note in notes.items():
        result["provenance"][key] = {k: v for k, v in note.items() if v is not None}
    return _extend(result, run, result["provenance"])


# --------------------------------------------------------------------------- consistency check
def _run_status(steps):
    statuses = [step["status"] for step in steps]
    if not statuses:
        return RUN_INVALID_RECORD
    if STEP_FAILED in statuses:
        return RUN_FAILED
    if all(status == STEP_SKIPPED for status in statuses):
        return RUN_SKIPPED
    if STEP_SKIPPED in statuses:
        return RUN_PARTIAL
    return RUN_COMPLETED


def _section_status(result, section):
    try:
        return result["report"]["sections"][section]["status"]
    except (KeyError, TypeError):
        return None


def check_integrated_result(result):
    """Problem tokens (an empty list means consistent) for a `run_integrated` result: missing keys, stage statuses
    that disagree with the steps, a run status that disagrees with the steps, results kept for steps that did not
    complete, a synthesis or summary without its completed step, citations outside the stored inputs, a completed
    synthesis that no longer passes the synthesis validator (`synthesis_invalid`), a completed
    stage whose provider was never invoked, a report showing success for an unfinished step, and a specialist that
    is not skipped. It reads only; it never raises for a malformed result."""
    try:
        if not isinstance(result, dict) or any(key not in result for key in RESULT_KEYS):
            return ["missing_keys"]
        return _problems(result)
    except Exception:
        return ["malformed_result"]


def _problems(result):
    problems = []
    run, integration = result["run"], result["integration"]
    steps = run["steps"] if isinstance(run, dict) else []
    if run is None:
        if result["run_status"] != RUN_NOT_STARTED:
            problems.append("run_status_mismatch")
    elif result["run_status"] != run["status"] or run["status"] != _run_status(steps):
        problems.append("run_status_mismatch")
    specialist = integration["specialist"]
    if specialist.get("status") != STEP_SKIPPED or specialist.get("provider_called") is not False:
        problems.append("specialist_not_skipped")
    by_name = {step["name"]: step for step in steps}
    for stage in integration["stages"]:
        name = stage["stage"]
        step_name = next(step for stage_name, step, _ in STAGES if stage_name == name)
        entry = by_name.get(step_name)
        expected = entry["status"] if entry is not None else STAGE_NOT_RUN
        if stage["status"] != expected:
            problems.append("stage_status_mismatch")
        if stage["status"] == STEP_COMPLETED and stage["provider_called"] is not True:
            problems.append("completed_without_invocation")
    if any(step["status"] != STEP_COMPLETED and step["result"] is not None for step in steps):
        problems.append("non_completed_step_has_result")
    for key, step_name, token in (("synthesis", SYNTHESIS_STEP, "synthesis_without_completed_step"),
                                  ("sensor_summary", SUMMARY_STEP, "sensor_summary_without_completed_step")):
        entry = by_name.get(step_name)
        completed = entry is not None and entry["status"] == STEP_COMPLETED
        if result[key] is not None and not completed:
            problems.append(token)
        elif completed and result[key] != entry["result"]:
            problems.append(key + "_differs_from_step")
    problems += _citation_problems(result, by_name)
    for section, step_name in (("image_inference", IMAGE_STEP), ("retrieved_sources", RESEARCH_STEP)):
        entry = by_name.get(step_name)
        if _section_status(result, section) == "ok" and (entry is None or entry["status"] != STEP_COMPLETED):
            problems.append("report_shows_success_for_unfinished_step")
    return problems


def _citation_problems(result, by_name):
    stored = result["synthesis"]
    if not isinstance(stored, dict):
        return []
    findings, research = by_name.get(IMAGE_STEP), by_name.get(RESEARCH_STEP)
    if (findings is None or research is None or findings["status"] != STEP_COMPLETED
            or research["status"] != STEP_COMPLETED):
        return []
    inputs = synthesis_module.inputs_from_results(findings["result"], research["result"], None)
    allowed = synthesis_module.allowed_refs(inputs)
    cited = [ref for statement in stored.get("statements", []) for ref in statement.get("cites", [])]
    if any(ref not in allowed for ref in cited):
        return ["synthesis_unknown_citation"]
    entry = by_name.get(SYNTHESIS_STEP)
    if entry is not None and entry["status"] == STEP_COMPLETED and not _stored_synthesis_is_valid(stored, inputs):
        return ["synthesis_invalid"]
    return []


_STORED_SYNTHESIS_KEYS = {"kind", "model", "fake", "statements", "uncertainties"}


def _stored_synthesis_is_valid(stored, inputs):
    """True if a stored synthesis still has exactly the shape the synthesis step stores: the wrapper keys, the
    unverified-generated-text kind, a boolean `fake`, a bounded label or None, and statements and uncertainties that
    pass the SAME validator the step used (`check_synthesis`: exact keys, bounds, no web address, cited references
    only and each at most once per statement)."""
    if set(stored) != _STORED_SYNTHESIS_KEYS or stored["kind"] != synthesis_module.KIND:
        return False
    if not isinstance(stored["fake"], bool):
        return False
    if stored["model"] is not None and synthesis_module.label(stored["model"]) is None:
        return False
    body = {"statements": stored["statements"], "uncertainties": stored["uncertainties"]}
    return synthesis_module.check_synthesis(body, inputs) is not None


__all__ = ("run_integrated", "check_integrated_result", "summary_steps", "synthesis_step")
