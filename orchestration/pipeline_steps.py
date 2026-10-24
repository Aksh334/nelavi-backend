"""Offline bridge between `orchestration.run.execute_run` and `agri_pipeline.pipeline.run` (Milestones 1L, 1N).

What it does
  * `image_findings_step(...)` and `research_step(...)` build ordinary `orchestration.run.Step` objects that
    call an INJECTED image-findings provider and an INJECTED Tavily-Search-style client, and return
    JSON-compatible dicts (the runner stores step results through the standard `json` module).
  * `check_report_inputs(...)` asks the pipeline's own validation whether `origin`, `user` and `image_ref` are
    acceptable, WITHOUT calling any provider.
  * `assemble_report(...)` reads a finished run, checks that the run is well formed and belongs to the given
    Record where that can be shown, rebuilds the attribute-style objects that `agri_pipeline.pipeline.run`
    expects (`FindingsResult`, `ResearchResult`) from the stored dicts, and calls the pipeline once.
  * `run_report(...)` does all of it in one call: check inputs, run the steps, assemble the report.

Why the conversion is needed: the runner keeps only JSON, but `pipeline.run` reads `findings_result.status`,
`.findings`, `research_result.items` and so on as ATTRIBUTES. The smallest honest bridge is to store the fields
of the existing dataclasses as dicts and rebuild the same dataclasses (by keyword) afterwards. A test checks that
the rebuilt objects equal the originals and that the report equals one made by calling the pipeline directly.

Inputs the CALLER must supply (nothing is inferred)
  * `png_bytes`: PNG bytes. No path is derived from `photo_id`, no file is read, no camera or rover is used.
  * `query`: the research question as text. Nothing is derived from the Record or the image.
  * `image_ref`: optional label linking symptoms to a photo; never taken from `photo_id`.
  * `record_id`: optional local storage id, passed to `execute_run` (see its docstring for its limited scope).

Step outcomes and how they appear in the report
  * Provider or client not given, bytes or query not given, or `research_skip_reason` given: the step is SKIPPED
    (never "completed"). The pipeline gets no result for that stage and reports it as "not_run".
  * PNG bytes that are not a PNG (wrong type, empty, no signature): the step FAILS with `image_unsupported` and the
    provider is NOT called.
  * Provider says "unavailable" (no model configured), returns an error, returns findings that fail
    `validate_findings`, or raises: the step FAILS with a controlled code. The report shows the image stage as
    "unavailable" or "error" with no findings, so a failure is never shown as a success.
  * Research client returns an error result, or an unusable object, or a result that cannot be stored as JSON: the
    step FAILS with a controlled code and the report shows the research stage as an error with no sources.
  * Codes in the REPORT are limited to the existing closed sets (`image_analysis.boundary.ERROR_CODES` and
    `research.tavily.ERROR_CODES`). Any other code (for example `step_failed`, `result_not_serializable`) is shown
    in the report as `response_malformed`; the precise code stays in `run["steps"]`.
  * An invalid Record: no step runs (the runner's rule); the pipeline then reports `invalid_record`.

Report inputs are checked BEFORE any provider is called (`run_report`)
  `check_report_inputs` runs the pipeline once with a harmless probe symptom and no provider data. The pipeline's
  own rules therefore decide: `origin` must be one of its origins, `user` must pass its context rules, and
  `image_ref` must be None or a short printable text. Order matches a direct pipeline call: origin, then the
  Record, then the rest, so an invalid Record is still reported as `invalid_record`, not `invalid_input`.
  If the pipeline would report `invalid_input`, `run_report` returns at once with `run_status` "not_started",
  `run` None, `run_id` None, `record_id` None / scope "unavailable" (no run exists, so no identity is assigned) and
  the pipeline's own `invalid_input` report. No provider is called and no run id is generated. Note: a direct
  pipeline call ignores `image_ref` when there are no image findings; `run_report` checks it anyway, because a
  value the pipeline would reject if an image stage succeeded should not be found out after paying for that stage.
  The run arguments (`record_id`, `run_id`, `run_id_factory`, `clock`) are validated BEFORE this refusal with
  `orchestration.run.check_run_arguments`, which never calls the factory or the clock, so a bad argument raises
  (same type and fixed message as on a call that is not refused) instead of being silently ignored. The order is:
  provider objects (a wrong object raises first), then run arguments, then report inputs. A valid argument does not
  change the refusal result, and the factory and the clock are not called on a refusal.
  `query` is not a pipeline argument and is not validated here (the research client validates it).
  Manual users of `execute_run` + `assemble_report` can call `check_report_inputs` themselves first.

Provenance (what is kept about a stage, including a failed one)
  `execute_run` keeps only a closed error code for a failed step. To keep more WITHOUT changing the runner, the
  step builders accept an optional caller-owned `provenance` dict and write a few sanitised facts into it as the
  step runs; `run_report` creates the dict and returns the cleaned copy as `result["provenance"]`.
    image stage    `implementation` (module and class name of the injected provider), `provider_called` (False
                   when the bytes were refused before the provider was reached), `model` (only if the provider
                   result carries a short printable text).
    research stage `implementation`, `provider_called`, and, only if the client's result carries them, `query`,
                   `retrieved_at` and `dropped`.
    `record_sha256` a SHA-256 of the validated Record's canonical JSON, set by `run_report` (see below).
  `implementation` names the configured Python class. It does NOT say which vendor answered or whether any network
  contact happened (a fake client has a class name too).
  `provider_called` is True when the adapter INVOKED the injected object's method. It is NOT confirmation that an
  external service was contacted: a real client can refuse before sending anything (the Tavily search adapter does
  so for `query_invalid`, `credential_missing` and `configuration_invalid`), and a fake never contacts anything.
  It is False only when this module refused the input before invoking the object (PNG bytes that are not a PNG).
  Only evidence from the provider side, such as a request log, could show real contact.
  Two unrelated things are called "provenance" here: the caller-owned `provenance` dict described above (and
  returned as `result["provenance"]`), and the text label copied from `FindingsResult.provenance` (default
  "image_analysis") that the image step stores under the key `provenance` inside its own result.
  A skipped stage has no entry. Nothing here is copied from
  an exception, a request or a provider response body. On a failed stage the assembled `FindingsResult` /
  `ResearchResult` carry the model / query / retrieval time found here; nothing is invented when it is absent
  (for a failed image stage whose provider gave no model, `model` stays None).

Record-to-run consistency (`assemble_report`)
  Checked, and a mismatch raises ValueError with a fixed message (it is a programming error, like a bad argument):
    * the run has the shape `execute_run` returns (required keys, string run id, `record_id` / scope pair that
      agree, known statuses, unique step names, no steps for an `invalid_record` run, steps otherwise);
    * a run that executed steps belongs to a VALID Record, and an `invalid_record` run to an INVALID one;
    * if a `record_sha256` is supplied in `provenance` (as `run_report` does), the supplied Record's fingerprint
      must equal it.
  NOT established, and not claimed: `execute_run` stores nothing about which Record it ran, so for a run assembled
  WITHOUT the fingerprint two different valid Records cannot be told apart. The fingerprint compares CONTENT only:
  equal Records have equal fingerprints. It is not an identity, not a uniqueness guarantee and not a deduplication.
  `record_id` is caller-supplied and cannot be checked against the Record; `run_id` is never treated as a record id;
  `rover_id`, `sequence_number` and `photo_id` are not used as identity.

What is NOT preserved: a failed step still keeps nothing of the provider's response beyond what is listed above.
Result sizes. Only bounds that the repository itself establishes are enforced: a Search result may have at most
`research.tavily.MAX_RESULTS_LIMIT` items, titles up to `MAX_TITLE_CHARS`, snippets up to `MAX_SNIPPET_CHARS` and a
query up to `MAX_QUERY_CHARS` (the real search adapter cuts and caps to these, so only an injected client can
exceed them). A result over a bound FAILS the stage with the existing `response_too_large` code: nothing is
truncated, and nothing oversized is stored in `run` or shown in the report. Image findings are already bounded by
`validate_findings`. For fields with no field-level bound (a Search result's url, published date, kind and caveats,
the image stage's provenance label) only the runner's whole-result cap applies: `orchestration.run.
MAX_STEP_RESULT_BYTES` (provisional, 4 MiB of serialised JSON). A stage result over it fails the step with the
runner's `result_too_large`, which the report shows as the existing closed `response_too_large`. The runner also
caps the run's stored total (`MAX_RUN_RESULT_BYTES`, provisional, 8 MiB; OR-9): a stage that would pass it fails
the same way, so the second stage can fail if the first used up the room. The assembly result still holds
provider output in both `run` and `report`, and its shape is unchanged (`include_run` is deferred).

Imports, and why each is needed (all are existing modules; no provider client is created or called here)
  * `orchestration.run`: the runner, `Step`, `StepFailure` and its status constants.
  * `agri_pipeline.pipeline`: the one report builder, called in `check_report_inputs` and `assemble_report`.
  * `agri_pipeline.findings`: `FindingsResult` (rebuilt for the pipeline), its status and reason constants, and
    `validate_findings` (to re-check an injected provider's findings before they enter the report).
  * `image_analysis.boundary`: `is_supported_png`/`ImageInput` and the closed image error codes (provider-neutral).
  * `research.tavily`: ONLY the result types `ResearchItem`/`ResearchResult`, the closed error codes and the query
    length constant, because the pipeline's research input is that type. Importing the module loads its
    definitions (and, through it, the definitions in `image_analysis.nvidia`); it builds no client, reads no
    environment and makes no request.
  * `record.validation`: `validate_record`, the same validator the runner and the pipeline use.
  * standard library: `copy`, `hashlib`, `json`, `re`, `unicodedata`.

Not provided: Research Pro (asynchronous), photo resolution, persistence, retries, result-size limits, any provider
SDK, Nodus, Agent37 or OpenRouter.
"""

import copy
import hashlib
import json
import re
import unicodedata

from agri_pipeline import pipeline
from agri_pipeline.findings import (
    REASON_NO_MODEL,
    STATUS_FINDINGS_ERROR,
    STATUS_FINDINGS_OK,
    STATUS_FINDINGS_UNAVAILABLE,
    FindingsResult,
    validate_findings,
)
from image_analysis.boundary import ERROR_CODES as IMAGE_ERROR_CODES
from image_analysis.boundary import (
    ERROR_IMAGE_UNSUPPORTED,
    ERROR_RESPONSE_MALFORMED,
    ERROR_RESPONSE_TOO_LARGE,
    MEDIA_TYPE_PNG,
    ImageInput,
    is_supported_png,
)
from orchestration.run import (
    ERROR_RESULT_TOO_LARGE,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INVALID_RECORD,
    RUN_PARTIAL,
    RUN_SKIPPED,
    SCOPE_LOCAL_STORE,
    SCOPE_UNAVAILABLE,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_SKIPPED,
    Step,
    StepFailure,
    check_run_arguments,
    execute_run,
)
from record.validation import validate_record
from research.tavily import ERROR_CODES as RESEARCH_ERROR_CODES
from research.tavily import ERROR_RESPONSE_TOO_LARGE as RESEARCH_ERROR_TOO_LARGE
from research.tavily import MAX_QUERY_CHARS as RESEARCH_MAX_QUERY_CHARS
from research.tavily import MAX_RESULTS_LIMIT, MAX_SNIPPET_CHARS, MAX_TITLE_CHARS
from research.tavily import STATUS_ERROR as RESEARCH_STATUS_ERROR
from research.tavily import STATUS_OK as RESEARCH_STATUS_OK
from research.tavily import ResearchItem, ResearchResult

VERSION = "1"

IMAGE_STEP = "image_findings"
IMAGE_PROVIDER = "image"
RESEARCH_STEP = "research_search"
RESEARCH_PROVIDER = "research"

SKIP_NO_PROVIDER = "no_provider_configured"
SKIP_NO_IMAGE = "no_image_supplied"
SKIP_NO_QUERY = "no_query_supplied"

RUN_NOT_STARTED = "not_started"   # adapter-level only: no run exists because the report inputs were refused

MAX_LABEL_CHARS = 128
MAX_TIME_CHARS = 64

MALFORMED_RUN = "run is not a well-formed execute_run result"
MISMATCH = "the record and the run do not match"
MALFORMED_PROVENANCE = "provenance is malformed"

# The runner's own "result too large" code is shown in the report as the existing closed `response_too_large`
# (both report vocabularies have it); any other code that is not in the closed sets is still `response_malformed`.
_REPORT_CODE = {ERROR_RESULT_TOO_LARGE: ERROR_RESPONSE_TOO_LARGE}

_RUN_STATUSES = (RUN_COMPLETED, RUN_PARTIAL, RUN_SKIPPED, RUN_FAILED, RUN_INVALID_RECORD)
_STEP_STATUSES = (STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED)
_RUN_KEYS = ("version", "run_id", "record_id", "record_id_scope", "status", "started_at", "completed_at",
             "steps", "errors")
_SHA256 = re.compile(r"[0-9a-f]{64}")

# A harmless symptom, used only so the pipeline's own validation also looks at `image_ref`.
_PROBE_FINDINGS = {
    "crop": {"candidates": []},
    "growth_stage": {"candidate": None, "evidence": [], "uncertainty": "unspecified"},
    "symptoms": [{"observation": "probe", "possible_interpretations": []}],
    "needs_more": [],
}


# --------------------------------------------------------------------------- small sanitisers
def _safe_text(value, limit):
    """`value` if it is a non-blank str of at most `limit` characters with no control characters, else None."""
    if (isinstance(value, str) and value.strip() != "" and len(value) <= limit
            and not any(unicodedata.category(c).startswith("C") for c in value)):
        return value
    return None


def _label(value):
    return _safe_text(value, MAX_LABEL_CHARS)


def _query_text(value):
    return _safe_text(value, RESEARCH_MAX_QUERY_CHARS)


def _timestamp(value):
    return _safe_text(value, MAX_TIME_CHARS)


def _flag(value):
    return value if isinstance(value, bool) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _implementation(obj):
    cls = type(obj)
    return _label("%s.%s" % (cls.__module__, cls.__qualname__))


def _note(provenance, stage, **fields):
    """Record the non-None `fields` for `stage` in the caller-owned `provenance` dict (if one was given)."""
    if provenance is None:
        return
    entry = provenance.setdefault(stage, {})
    entry.update({key: value for key, value in fields.items() if value is not None})


def _check_provenance_arg(provenance):
    if provenance is not None and not isinstance(provenance, dict):
        raise TypeError("provenance must be a dict or None")


# --------------------------------------------------------------------------- steps
def _not_called(context):  # a skipped step is never called; this only gives Step a callable
    return None


def image_findings_step(findings_provider, png_bytes, *, hints=None, provenance=None):
    """Build the image step. `findings_provider` needs `analyze_image(png_bytes, hints)` returning a
    `FindingsResult` (for example `PromptedFindingsProvider(analyzer)`); `png_bytes` is supplied by the caller.
    `provenance` is an optional dict the step fills with sanitised facts (see the module docstring)."""
    _check_provenance_arg(provenance)
    if findings_provider is None:
        return Step(IMAGE_STEP, IMAGE_PROVIDER, _not_called, skip_reason=SKIP_NO_PROVIDER)
    if not callable(getattr(findings_provider, "analyze_image", None)):
        raise TypeError("findings_provider must have an analyze_image() method")
    if png_bytes is None:
        return Step(IMAGE_STEP, IMAGE_PROVIDER, _not_called, skip_reason=SKIP_NO_IMAGE)
    implementation = _implementation(findings_provider)

    def call(context):
        _note(provenance, IMAGE_STEP, implementation=implementation, provider_called=False)
        if not isinstance(png_bytes, bytes) or not is_supported_png(ImageInput(png_bytes, MEDIA_TYPE_PNG)):
            raise StepFailure(ERROR_IMAGE_UNSUPPORTED)  # the provider is not called
        _note(provenance, IMAGE_STEP, provider_called=True)
        result = findings_provider.analyze_image(png_bytes, hints)
        model = _label(getattr(result, "model", None))
        _note(provenance, IMAGE_STEP, model=model)
        status = getattr(result, "status", None)
        if status == STATUS_FINDINGS_OK:
            findings = getattr(result, "findings", None)
            if not validate_findings(findings):
                raise StepFailure(ERROR_RESPONSE_MALFORMED)
            provenance_text = getattr(result, "provenance", None)
            return {"status": STATUS_FINDINGS_OK, "model": model,
                    "provenance": provenance_text if isinstance(provenance_text, str) else "image_analysis",
                    "findings": copy.deepcopy(findings)}
        if status == STATUS_FINDINGS_UNAVAILABLE:
            raise StepFailure(REASON_NO_MODEL)
        if status == STATUS_FINDINGS_ERROR:
            code = getattr(result, "error_code", None)
            raise StepFailure(code if code in IMAGE_ERROR_CODES else ERROR_RESPONSE_MALFORMED)
        raise StepFailure(ERROR_RESPONSE_MALFORMED)

    return Step(IMAGE_STEP, IMAGE_PROVIDER, call)


def _within_search_bounds(result, items):
    """False if a Search result exceeds the bounds the search adapter itself sets (`research.tavily`: at most
    MAX_RESULTS_LIMIT items, titles up to MAX_TITLE_CHARS, snippets up to MAX_SNIPPET_CHARS, a query up to
    MAX_QUERY_CHARS). The real adapter cuts and caps to these, so only an injected client can exceed them.
    Only text lengths are compared; a non-text value is left to the existing malformed handling."""
    if len(items) > MAX_RESULTS_LIMIT:
        return False
    query = result.query
    if isinstance(query, str) and len(query) > RESEARCH_MAX_QUERY_CHARS:
        return False
    for item in items:
        title, snippet = item.title, item.snippet
        if isinstance(title, str) and len(title) > MAX_TITLE_CHARS:
            return False
        if isinstance(snippet, str) and len(snippet) > MAX_SNIPPET_CHARS:
            return False
    return True


def _research_to_json(result):
    try:
        raw_items = list(result.items)
        if not _within_search_bounds(result, raw_items):
            raise StepFailure(RESEARCH_ERROR_TOO_LARGE)   # explicit failure; nothing is truncated or kept
        items = [{"title": i.title, "url": i.url, "snippet": i.snippet, "published_date": i.published_date,
                  "score": i.score} for i in raw_items]
        return {"status": RESEARCH_STATUS_OK, "kind": result.kind, "query": result.query,
                "retrieved_at": result.retrieved_at, "dropped": result.dropped, "items": items,
                "caveats": [[c[0], c[1]] for c in result.caveats]}
    except (AttributeError, TypeError, IndexError):
        raise StepFailure(ERROR_RESPONSE_MALFORMED) from None


def research_step(research_client, query, *, skip_reason=None, provenance=None):
    """Build the synchronous research step. `research_client` needs `search(query)` returning a
    `research.tavily.ResearchResult`-like object; `query` is the caller's text. `skip_reason` (a lower-case
    token) skips the step on purpose. `provenance` is an optional dict the step fills (see the module docstring)."""
    _check_provenance_arg(provenance)
    if skip_reason is not None:
        return Step(RESEARCH_STEP, RESEARCH_PROVIDER, _not_called, skip_reason=skip_reason)
    if research_client is None:
        return Step(RESEARCH_STEP, RESEARCH_PROVIDER, _not_called, skip_reason=SKIP_NO_PROVIDER)
    if not callable(getattr(research_client, "search", None)):
        raise TypeError("research_client must have a search() method")
    if query is None:
        return Step(RESEARCH_STEP, RESEARCH_PROVIDER, _not_called, skip_reason=SKIP_NO_QUERY)
    implementation = _implementation(research_client)

    def call(context):
        _note(provenance, RESEARCH_STEP, implementation=implementation, provider_called=True)
        result = research_client.search(query)
        _note(provenance, RESEARCH_STEP, query=_query_text(getattr(result, "query", None)),
              retrieved_at=_timestamp(getattr(result, "retrieved_at", None)),
              dropped=_count(getattr(result, "dropped", None)))
        if getattr(result, "status", None) == RESEARCH_STATUS_OK:
            return _research_to_json(result)
        code = getattr(result, "error_code", None)
        raise StepFailure(code if code in RESEARCH_ERROR_CODES else ERROR_RESPONSE_MALFORMED)

    return Step(RESEARCH_STEP, RESEARCH_PROVIDER, call)


# --------------------------------------------------------------------------- report inputs (A)
def check_report_inputs(record, *, origin="unknown", user=None, image_ref=None):
    """Return None if the pipeline accepts these report inputs (or would report the Record itself as invalid),
    else the pipeline's own `invalid_input` report. No provider is called and no run is made.

    This reuses `agri_pipeline.pipeline.run` with a harmless probe symptom, so the pipeline's rules are the only
    rules: nothing is copied here."""
    probe = FindingsResult(status=STATUS_FINDINGS_OK, error_code=None, reason=None,
                           findings=copy.deepcopy(_PROBE_FINDINGS), model=None)
    report = pipeline.run(record, origin=origin, user=user, image_ref=image_ref, findings_result=probe)
    return report if report.get("status") == "invalid_input" else None


# --------------------------------------------------------------------------- consistency (D)
def _record_digest(valid_record):
    """SHA-256 of the canonical JSON of a VALIDATED Record. A content comparison only, not an identity."""
    text = json.dumps(valid_record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_run(run):
    if not isinstance(run, dict) or not isinstance(run.get("steps"), list):
        raise TypeError("run must be the dict returned by execute_run")
    if any(key not in run for key in _RUN_KEYS):
        raise ValueError(MALFORMED_RUN)
    if _safe_text(run["run_id"], 64) is None or run["status"] not in _RUN_STATUSES:
        raise ValueError(MALFORMED_RUN)
    record_id, scope = run["record_id"], run["record_id_scope"]
    has_id = isinstance(record_id, int) and not isinstance(record_id, bool) and record_id >= 1
    if not ((record_id is None and scope == SCOPE_UNAVAILABLE) or (has_id and scope == SCOPE_LOCAL_STORE)):
        raise ValueError(MALFORMED_RUN)
    names = set()
    for entry in run["steps"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or entry.get("status") not in _STEP_STATUSES or entry["name"] in names):
            raise ValueError(MALFORMED_RUN)
        names.add(entry["name"])
    if (run["status"] == RUN_INVALID_RECORD) != (len(run["steps"]) == 0):
        raise ValueError(MALFORMED_RUN)


def _clean_provenance(provenance):
    """A sanitised copy holding only the recognised, well-formed entries. A malformed fingerprint is an error."""
    _check_provenance_arg(provenance)
    clean = {}
    if provenance is None:
        return clean
    digest = provenance.get("record_sha256")
    if digest is not None:
        if not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
            raise ValueError(MALFORMED_PROVENANCE)
        clean["record_sha256"] = digest
    fields = {
        IMAGE_STEP: (("implementation", _label), ("provider_called", _flag), ("model", _label)),
        RESEARCH_STEP: (("implementation", _label), ("provider_called", _flag), ("query", _query_text),
                        ("retrieved_at", _timestamp), ("dropped", _count)),
    }
    for stage, allowed in fields.items():
        raw = provenance.get(stage)
        if not isinstance(raw, dict):
            continue
        entry = {}
        for key, clean_value in allowed:
            value = clean_value(raw.get(key))
            if value is not None:
                entry[key] = value
        clean[stage] = entry
    return clean


# --------------------------------------------------------------------------- rebuilding pipeline inputs (B)
def _entry(run, name):
    for entry in run["steps"]:
        if entry["name"] == name:
            return entry
    return None


def _findings_result(entry, note):
    """Rebuild a `FindingsResult` from a finished step entry, or None if the stage did not run."""
    if entry is None:
        return None
    if entry["status"] == STEP_COMPLETED:
        result = entry["result"]
        return FindingsResult(status=STATUS_FINDINGS_OK, error_code=None, reason=None,
                              findings=copy.deepcopy(result["findings"]), model=result["model"],
                              provenance=result["provenance"])
    if entry["status"] == STEP_FAILED:
        code = _REPORT_CODE.get(entry["error_code"], entry["error_code"])
        model = note.get("model") if note else None
        if code == REASON_NO_MODEL:
            return FindingsResult(status=STATUS_FINDINGS_UNAVAILABLE, error_code=None, reason=REASON_NO_MODEL,
                                  findings=None, model=model)
        return FindingsResult(status=STATUS_FINDINGS_ERROR,
                              error_code=code if code in IMAGE_ERROR_CODES else ERROR_RESPONSE_MALFORMED,
                              reason=None, findings=None, model=model)
    return None  # skipped (or never started): the pipeline reports "not run"


def _research_result(entry, note, query):
    """Rebuild a `ResearchResult` from a finished step entry, or None if the stage did not run."""
    if entry is None:
        return None
    if entry["status"] == STEP_COMPLETED:
        result = entry["result"]
        items = tuple(ResearchItem(title=i["title"], url=i["url"], snippet=i["snippet"],
                                   published_date=i["published_date"], score=i["score"])
                      for i in result["items"])
        return ResearchResult(status=RESEARCH_STATUS_OK, error_code=None, query=result["query"], items=items,
                              dropped=result["dropped"], retrieved_at=result["retrieved_at"],
                              kind=result["kind"], caveats=tuple((c[0], c[1]) for c in result["caveats"]))
    if entry["status"] == STEP_FAILED:
        code = _REPORT_CODE.get(entry["error_code"], entry["error_code"])
        if note is not None:  # the step ran: use only what the client itself reported
            failed_query, retrieved_at, dropped = note.get("query"), note.get("retrieved_at"), note.get("dropped", 0)
        else:
            failed_query, retrieved_at, dropped = _query_text(query), None, 0
        return ResearchResult(status=RESEARCH_STATUS_ERROR,
                              error_code=code if code in RESEARCH_ERROR_CODES else ERROR_RESPONSE_MALFORMED,
                              query=failed_query, items=(), dropped=dropped, retrieved_at=retrieved_at)
    return None


# --------------------------------------------------------------------------- assembly
def assemble_report(record, run, *, origin="unknown", user=None, image_ref=None, query=None, provenance=None):
    """Build the report for a finished `execute_run` result and return
    {"version", "run_id", "record_id", "record_id_scope", "run_status", "report", "run", "provenance"}.

    `record` is the same Record given to `execute_run`; the pipeline validates it again. `report` is exactly what
    `agri_pipeline.pipeline.run` returns for the rebuilt results. `run_id` and `record_id` are beside the report,
    never inside the Record or the report. `provenance` is the dict filled by the step builders (optional); it is
    sanitised and returned. `query` only labels a failed research stage when `provenance` has no entry for it.

    Raises TypeError if `run` is not a dict with a `steps` list, and ValueError (fixed message, no values) if the
    run is malformed or provably does not belong to `record` (see the module docstring for exactly what is
    checked and what cannot be)."""
    _check_run(run)
    clean = _clean_provenance(provenance)
    checked = validate_record(record)
    if (run["status"] == RUN_INVALID_RECORD) == checked.valid:
        raise ValueError(MISMATCH)
    expected = clean.get("record_sha256")
    if expected is not None and (not checked.valid or _record_digest(checked.record) != expected):
        raise ValueError(MISMATCH)
    try:
        findings_result = _findings_result(_entry(run, IMAGE_STEP), clean.get(IMAGE_STEP))
        research_result = _research_result(_entry(run, RESEARCH_STEP), clean.get(RESEARCH_STEP), query)
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ValueError(MALFORMED_RUN) from None
    report = pipeline.run(record, origin=origin, user=user, image_ref=image_ref,
                          findings_result=findings_result, research_result=research_result)
    return {"version": VERSION, "run_id": run["run_id"], "record_id": run["record_id"],
            "record_id_scope": run["record_id_scope"], "run_status": run["status"], "report": report, "run": run,
            "provenance": clean}


def run_report(record, *, findings_provider=None, png_bytes=None, hints=None, research_client=None, query=None,
               research_skip_reason=None, origin="unknown", user=None, image_ref=None, record_id=None,
               run_id=None, run_id_factory=None, clock=None):
    """Check the report inputs, run the image step and the research step with `execute_run`, then
    `assemble_report`. If the report inputs are refused, nothing runs (see the module docstring)."""
    provenance = {}
    steps = [image_findings_step(findings_provider, png_bytes, hints=hints, provenance=provenance),
             research_step(research_client, query, skip_reason=research_skip_reason, provenance=provenance)]
    # The run arguments are checked first (without calling the factory or the clock), so a bad one raises even
    # when the report inputs are about to be refused. Order: provider objects, run arguments, report inputs.
    check_run_arguments(record_id=record_id, run_id=run_id, run_id_factory=run_id_factory, clock=clock)
    rejected = check_report_inputs(record, origin=origin, user=user, image_ref=image_ref)
    if rejected is not None:
        return {"version": VERSION, "run_id": None, "record_id": None, "record_id_scope": SCOPE_UNAVAILABLE,
                "run_status": RUN_NOT_STARTED, "report": rejected, "run": None, "provenance": {}}
    checked = validate_record(record)
    if checked.valid:
        provenance["record_sha256"] = _record_digest(checked.record)
    run = execute_run(record, steps, record_id=record_id, run_id=run_id, run_id_factory=run_id_factory,
                      clock=clock)
    return assemble_report(record, run, origin=origin, user=user, image_ref=image_ref, query=query,
                           provenance=provenance)
