"""Synthesis boundary (the text-synthesis role): offline, provider-neutral, no network, no credential.

This module defines WHAT the workflow expects from a text-synthesis service, never HOW one is reached. There is no
adapter for any real service here: no API contract for one is recorded in this repository, so none was invented.
Only a deterministic fake exists.

What a provider may receive (`inputs`): only data that already passed validation earlier in the same run:
    {"findings": <validated image findings>,
     "evidence": [{"id": "E1", "title", "url", "snippet", "published_date"}, ...],
     "question": <the research question text, or None>}
It never receives record fields, identifiers, sensor values, image bytes, scores or the Nodus payload.

What it must return: a SynthesisResult whose `output` is
    {"statements": [{"text": str, "cites": [ref, ...]}, ...], "uncertainties": [str, ...]}
where every ref is one of `allowed_refs(inputs)`: "E<n>" (an evidence item), "F:crop", "F:growth_stage" or
"F:symptom:<i>" (a part of the validated findings). `check_synthesis` accepts only that shape and returns a plain
copy, or None. A synthesis is generated text: it is never verified evidence and never replaces a finding.
"""

import copy
from dataclasses import dataclass

STATUS_OK = "ok"
STATUS_ERROR = "error"

ERROR_FAILED = "synthesis_failed"
ERROR_UNAVAILABLE = "synthesis_unavailable"
ERROR_REJECTED = "synthesis_rejected"
ERROR_MALFORMED = "synthesis_response_malformed"
ERROR_CODES = (ERROR_FAILED, ERROR_UNAVAILABLE, ERROR_REJECTED, ERROR_MALFORMED)

KIND = "model_generated_unverified"

MAX_STATEMENTS = 8
MAX_CITES = 6
MAX_UNCERTAINTIES = 6
MAX_STATEMENT_CHARS = 400
MAX_UNCERTAINTY_CHARS = 300
MAX_QUESTION_CHARS = 200
MAX_LABEL_CHARS = 128
MAX_EVIDENCE_TITLE_CHARS = 300      # the same bounds the research adapter applies
MAX_EVIDENCE_SNIPPET_CHARS = 1000
MAX_EVIDENCE_DATE_CHARS = 64


@dataclass(frozen=True)
class SynthesisResult:
    status: str
    error_code: object   # None, or one of ERROR_CODES
    output: object       # the answer when status is "ok", else None
    model: object        # a short label of the model or implementation
    fake: bool           # True only for output made up by a fake


def error_result(code, model, fake=False):
    return SynthesisResult(STATUS_ERROR, code, None, model, fake)


# --------------------------------------------------------------------------- inputs
def _text(value, limit):
    if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and value.strip():
        return value
    return None


def label(value):
    """A short printable text, or None. Labels are stored, so they are bounded."""
    return _text(value, MAX_LABEL_CHARS)


def inputs_from_results(findings_result, research_result, question):
    """Build the provider inputs from the STORED results of the image step and the research step (both already
    validated by their steps). Scores and every other field are left out."""
    evidence = []
    for number, item in enumerate(research_result["items"], start=1):
        # The adapter bounds title and snippet but not the provider-reported date, so everything that can reach a
        # provider is bounded here again: a date that is not short printable text is dropped, never passed on.
        title, snippet = item["title"], item["snippet"]
        evidence.append({"id": "E%d" % number,
                         "title": title[:MAX_EVIDENCE_TITLE_CHARS] if isinstance(title, str) else "",
                         "url": item["url"],
                         "snippet": snippet[:MAX_EVIDENCE_SNIPPET_CHARS] if isinstance(snippet, str) else "",
                         "published_date": _text(item["published_date"], MAX_EVIDENCE_DATE_CHARS)})
    return {"findings": copy.deepcopy(findings_result["findings"]), "evidence": evidence,
            "question": _text(question, MAX_QUESTION_CHARS)}


def allowed_refs(inputs):
    """The set of references a statement may cite, derived from `inputs` only."""
    findings = inputs["findings"]
    refs = set()
    if findings["crop"]["candidates"]:
        refs.add("F:crop")
    if findings["growth_stage"]["candidate"] is not None:
        refs.add("F:growth_stage")
    for index in range(len(findings["symptoms"])):
        refs.add("F:symptom:%d" % index)
    for item in inputs["evidence"]:
        refs.add(item["id"])
    return refs


# --------------------------------------------------------------------------- output check
def _statement_text(value):
    text = _text(value, MAX_STATEMENT_CHARS)
    if text is None or "://" in text or "www." in text.lower():
        return None
    return text


def check_synthesis(output, inputs):
    """A plain copy of `output` if it has exactly the documented shape and cites only allowed references, else
    None. Nothing here can raise: an object that misbehaves when read is simply refused."""
    try:
        allowed = allowed_refs(inputs)
        if not isinstance(output, dict) or set(output) != {"statements", "uncertainties"}:
            return None
        statements, uncertainties = output["statements"], output["uncertainties"]
        if not isinstance(statements, list) or not 1 <= len(statements) <= MAX_STATEMENTS:
            return None
        if not isinstance(uncertainties, list) or len(uncertainties) > MAX_UNCERTAINTIES:
            return None
        plain_statements = []
        for statement in statements:
            if not isinstance(statement, dict) or set(statement) != {"text", "cites"}:
                return None
            text, cites = _statement_text(statement["text"]), statement["cites"]
            if text is None or not isinstance(cites, list) or not 1 <= len(cites) <= MAX_CITES:
                return None
            if not all(isinstance(c, str) and c in allowed for c in cites) or len(set(cites)) != len(cites):
                return None
            plain_statements.append({"text": text, "cites": list(cites)})
        plain_uncertainties = []
        for entry in uncertainties:
            text = _text(entry, MAX_UNCERTAINTY_CHARS)
            if text is None:
                return None
            plain_uncertainties.append(text)
        return {"statements": plain_statements, "uncertainties": plain_uncertainties}
    except Exception:
        return None


# --------------------------------------------------------------------------- request diagnostics (SY-6)
# A real provider may keep, for its LAST call only, a small record of where a call stopped and the shape of what came
# back. Every value is a closed word, a bounded number or a short list of closed words: never a byte, a word or a slice
# of the prompt, the reply or the model's text. `clean_diagnostic` is the allowlist that anything shown must pass.
DIAG_FAILED_AT = ("configuration_invalid", "credential_missing", "request_not_built", "request_too_large",
                  "transport_failed", "http_status", "status_invalid", "reply_not_bytes", "document_error",
                  "reply_not_json", "reply_shape", "empty_content", "content_too_long", "content_not_json_object",
                  "output_truncated", "output_invalid")
DIAG_FINISH = ("stop", "length", "content_filter", "tool_calls", "error", "other", "absent")
DIAG_SHAPES = ("json_object", "fenced_json", "prose_or_other", "empty")
DIAG_PROBLEMS = (
    "not_an_object", "wrong_top_level_keys", "statements_not_a_list", "statement_count_out_of_range",
    "statement_not_an_object", "statement_wrong_keys", "statement_text_empty_or_not_text", "statement_text_too_long",
    "statement_text_not_printable", "statement_has_web_address", "cites_not_a_list", "cites_count_out_of_range",
    "cite_not_allowed", "cite_repeated", "uncertainties_not_a_list", "too_many_uncertainties",
    "uncertainty_empty_or_not_text", "uncertainty_too_long", "uncertainty_not_printable", "diagnostic_error")
DIAG_KEYS = ("failed_at", "http_status", "finish_reason", "reply_bytes", "content_chars", "completion_tokens",
             "reasoning_tokens", "max_tokens", "content_shape", "output_problems")
DIAG_MAX_PROBLEMS = 12


def diagnose_synthesis(output, inputs):
    """Closed problem tokens (an empty list means `check_synthesis` accepts it) explaining why `output` is refused.
    It mirrors `check_synthesis` rule for rule, reuses its constants and text rules, never raises and never includes
    any of `output`. A test proves it agrees with `check_synthesis` on a battery of documents."""
    problems = []

    def add(token):
        if token not in problems:
            problems.append(token)

    try:
        allowed = allowed_refs(inputs)
        if not isinstance(output, dict):
            return ["not_an_object"]
        if set(output) != {"statements", "uncertainties"}:
            add("wrong_top_level_keys")
        statements, uncertainties = output.get("statements"), output.get("uncertainties")
        if not isinstance(statements, list):
            add("statements_not_a_list")
        else:
            if not 1 <= len(statements) <= MAX_STATEMENTS:
                add("statement_count_out_of_range")
            for statement in statements[:2 * MAX_STATEMENTS]:
                if not isinstance(statement, dict):
                    add("statement_not_an_object")
                    continue
                if set(statement) != {"text", "cites"}:
                    add("statement_wrong_keys")
                text = statement.get("text")
                if not isinstance(text, str) or not text.strip():
                    add("statement_text_empty_or_not_text")
                elif len(text) > MAX_STATEMENT_CHARS:
                    add("statement_text_too_long")
                elif not text.isprintable():
                    add("statement_text_not_printable")
                elif "://" in text or "www." in text.lower():
                    add("statement_has_web_address")
                cites = statement.get("cites")
                if not isinstance(cites, list):
                    add("cites_not_a_list")
                else:
                    if not 1 <= len(cites) <= MAX_CITES:
                        add("cites_count_out_of_range")
                    if not all(isinstance(c, str) and c in allowed for c in cites):
                        add("cite_not_allowed")
                    names = [c for c in cites if isinstance(c, str)]
                    if len(set(names)) != len(names):
                        add("cite_repeated")
        if not isinstance(uncertainties, list):
            add("uncertainties_not_a_list")
        else:
            if len(uncertainties) > MAX_UNCERTAINTIES:
                add("too_many_uncertainties")
            for entry in uncertainties[:2 * MAX_UNCERTAINTIES]:
                if not isinstance(entry, str) or not entry.strip():
                    add("uncertainty_empty_or_not_text")
                elif len(entry) > MAX_UNCERTAINTY_CHARS:
                    add("uncertainty_too_long")
                elif not entry.isprintable():
                    add("uncertainty_not_printable")
    except Exception:
        return ["diagnostic_error"]
    return problems


def _bounded_int(value, low, high):
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and low <= value <= high)


def clean_diagnostic(raw):
    """The allowlisted copy of a provider's last-call diagnostic, or None unless it has exactly the keys of DIAG_KEYS
    and every value is a closed word, a bounded number or a short list of closed words."""
    try:
        if not isinstance(raw, dict) or set(raw) != set(DIAG_KEYS):
            return None
        problems = raw["output_problems"]
        ok = (
            (raw["failed_at"] is None or raw["failed_at"] in DIAG_FAILED_AT)
            and _bounded_int(raw["http_status"], 100, 599)
            and (raw["finish_reason"] is None or raw["finish_reason"] in DIAG_FINISH)
            and _bounded_int(raw["reply_bytes"], 0, 64 * 1024 * 1024)
            and _bounded_int(raw["content_chars"], 0, 64 * 1024 * 1024)
            and _bounded_int(raw["completion_tokens"], 0, 10 ** 7)
            and _bounded_int(raw["reasoning_tokens"], 0, 10 ** 7)
            and _bounded_int(raw["max_tokens"], 1, 4096)
            and (raw["content_shape"] is None or raw["content_shape"] in DIAG_SHAPES)
            and isinstance(problems, list) and len(problems) <= DIAG_MAX_PROBLEMS
            and all(isinstance(p, str) and p in DIAG_PROBLEMS for p in problems))
        if not ok:
            return None
        copy_ = {key: raw[key] for key in DIAG_KEYS}
        copy_["output_problems"] = list(problems)
        return copy_
    except Exception:
        return None


# --------------------------------------------------------------------------- the fake
class FakeSynthesisProvider:
    """A deterministic stand-in for a synthesis service. It makes up nothing random, uses no network and needs no
    credential. `mode`: "ok" returns a short placeholder per available reference; "fail", "unavailable" and
    "rejected" return those closed errors; "malformed", "empty", "unknown_cite" and "not_json" return answers that
    must be refused; "raise" raises an exception whose message contains the inputs (to prove nothing leaks).
    `calls` records a copy of every `inputs` it was given."""

    LABEL = "fake-synthesis (deterministic placeholder; no service was contacted)"
    MODES = ("ok", "fail", "unavailable", "rejected", "malformed", "empty", "unknown_cite", "not_json", "raise")

    def __init__(self, mode="ok"):
        if mode not in self.MODES:
            raise ValueError("mode must be one of the fake provider's modes")
        self._mode = mode
        self.calls = []

    def synthesize(self, inputs):
        self.calls.append(copy.deepcopy(inputs))
        errors = {"fail": ERROR_FAILED, "unavailable": ERROR_UNAVAILABLE, "rejected": ERROR_REJECTED}
        if self._mode in errors:
            return error_result(errors[self._mode], self.LABEL, True)
        if self._mode == "raise":
            raise RuntimeError("the fake provider exploded with %r" % (inputs,))
        refs = sorted(allowed_refs(inputs))
        if self._mode == "malformed":
            output = {"statements": "not a list", "uncertainties": []}
        elif self._mode == "empty":
            output = {"statements": [], "uncertainties": []}
        elif self._mode == "unknown_cite":
            output = {"statements": [{"text": "Cites something that was never provided.", "cites": ["E99"]}],
                      "uncertainties": []}
        elif self._mode == "not_json":
            output = "this is not the documented structure"
        elif not refs:
            return error_result(ERROR_REJECTED, self.LABEL, True)   # nothing to cite: nothing is invented
        else:
            output = {"statements": [{"text": "Placeholder statement about %s." % ref, "cites": [ref]}
                                     for ref in refs[:MAX_STATEMENTS]],
                      "uncertainties": ["This text is fake placeholder output and is not verified evidence."]}
        return SynthesisResult(STATUS_OK, None, output, self.LABEL, True)
