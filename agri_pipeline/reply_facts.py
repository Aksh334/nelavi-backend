"""Closed-word facts about the reply to an image-findings request (IA-11). Pure: no network, no clock, no environment.

When a model service answers an image-findings request with an HTTP success, the adapter and the findings validator
refuse some replies with the same public code, `response_malformed`, and nothing kept after the refusal says which rule
refused it. This module looks at the raw reply bytes ONCE, inside the observer that already sees them, and returns only
closed words and bounded numbers:

    envelope          how far the chat-completion envelope could be read (see ENVELOPES)
    finish_reason     the service's own finish reason as a closed word (see FINISH_REASONS)
    completion_tokens, reasoning_tokens   the counts the service reported, if valid whole numbers
    content_chars     the length of the reply text (not the text)
    content_shape     a conservative class of the text: a bare JSON object, a fenced JSON object, or anything else
    findings_check    which step of the findings check the text reached (see FINDINGS_CHECKS)
    findings_rules, findings_rules_total
                      only for "contract_failed" (IA-12): up to MAX_RULES_SHOWN closed (code, structural path) pairs
                      naming the rules the JSON object broke, and how many were found in all

It returns nothing from which a word of the reply could be read, and never an arbitrary string from the service: a
finish reason that is not on the allowlist becomes "other". It does not infer why a reply ended: a finish reason is
shown as the service's report, and neither the shape nor the elapsed time is used to guess truncation. It records the
step the findings check reached and, for a JSON object the rules refused, the closed rule codes and structural paths
(never a word the model wrote).

The envelope rules mirror `_parse_content` of the image adapter and the findings steps mirror `parse_model_findings`; a
test proves the agreement on a battery of replies, so this cannot silently drift from what the real code accepts.
"""

import json
import re

from agri_pipeline.findings import (
    _FENCE,
    MAX_CROP_CANDIDATES,
    MAX_INTERPRETATIONS,
    MAX_NEEDS,
    MAX_REPLY_CHARS,
    MAX_SYMPTOMS,
    validate_findings,
)
from crop_context.schema import MAX_EVIDENCE, MAX_EVIDENCE_CHARS, MAX_NAME, MAX_TEXT, UNCERTAINTY, _name, _text

ENVELOPES = ("not_json", "not_an_object", "no_choices", "no_message", "content_absent", "content_null",
             "content_not_text", "content_blank", "content_text")
FINISH_REASONS = ("stop", "length", "content_filter", "tool_calls", "error", "other", "absent")
SHAPES = ("json_object", "fenced_json", "other")
FINDINGS_CHECKS = ("too_long", "not_json", "not_an_object", "contract_failed", "accepted")
FACT_KEYS = ("envelope", "finish_reason", "completion_tokens", "reasoning_tokens", "content_chars", "content_shape",
             "findings_check", "findings_rules", "findings_rules_total")
MAX_COUNT = 10 ** 7
MAX_CHARS = 64 * 1024 * 1024

# IA-12: WHICH rule of the findings contract refused a JSON object (only when findings_check is "contract_failed").
# Each entry is a closed code plus a structural path made of the contract's own field names and list positions, for
# example `symptoms[0].observation`. A path never contains anything the model wrote: a key the model invented is not
# named (EXTRA_FIELD names only the object it was found in), and no value is kept. The rules mirror `validate_findings`
# and reuse its constants and its own `_name` check; a test proves they agree with it on a large battery. If the
# validator refuses a document these rules cannot explain, the code is UNDIAGNOSED_REJECTION, never a guess.
RULE_CODES = ("WRONG_TYPE", "MISSING_REQUIRED_KEY", "EXTRA_FIELD", "INVALID_ENUM_VALUE", "TOO_MANY_ITEMS", "EMPTY_TEXT",
              "TEXT_TOO_LONG", "CONTROL_CHARACTER", "DISALLOWED_CHARACTER", "EVIDENCE_WITHOUT_CANDIDATE",
              "UNDIAGNOSED_REJECTION", "DIAGNOSTIC_ERROR")
MAX_RULES_SHOWN = 12
ITEM_SCAN_LIMIT = 50
_INDEX = re.compile(r"\[\d{1,2}\]")
_PATH_TEMPLATES = frozenset((
    "", "crop", "growth_stage", "symptoms", "needs_more",
    "crop.candidates", "crop.candidates[]", "crop.candidates[].name", "crop.candidates[].evidence",
    "crop.candidates[].evidence[]", "crop.candidates[].uncertainty",
    "growth_stage.candidate", "growth_stage.evidence", "growth_stage.evidence[]", "growth_stage.uncertainty",
    "symptoms[]", "symptoms[].observation", "symptoms[].possible_interpretations",
    "symptoms[].possible_interpretations[]", "needs_more[]",
))


def _text_problem(value, limit, is_name):
    """The first rule of the validator's `_text` / `_name` that `value` breaks, as a code, or None if it passes."""
    if not isinstance(value, str):
        return "WRONG_TYPE"
    if value.strip() == "":
        return "EMPTY_TEXT"
    if len(value) > limit:
        return "TEXT_TOO_LONG"
    if not _text(value, limit):    # non-blank and short enough are settled above, so only control characters remain
        return "CONTROL_CHARACTER"
    if is_name and not _name(value):
        return "DISALLOWED_CHARACTER"
    return None


class _Found:
    def __init__(self):
        self.items = []

    def add(self, code, path):
        if (code, path) not in self.items:
            self.items.append((code, path))

    def keys(self, obj, expected, path):
        """Check an object's exact key set. True if it is a dict (so its members can be checked)."""
        if not isinstance(obj, dict):
            self.add("WRONG_TYPE", path)
            return False
        for key in expected:
            if key not in obj:
                self.add("MISSING_REQUIRED_KEY", (path + "." if path else "") + key)
        if any(key not in expected for key in obj):
            self.add("EXTRA_FIELD", path)
        return True

    def text(self, value, limit, is_name, path):
        code = _text_problem(value, limit, is_name)
        if code:
            self.add(code, path)

    def short_list(self, value, limit, path):
        """The items to check, or None if `value` is not a list. Too long is reported once."""
        if not isinstance(value, list):
            self.add("WRONG_TYPE", path)
            return None
        if len(value) > limit:
            self.add("TOO_MANY_ITEMS", path)
        return value[:ITEM_SCAN_LIMIT]

    def evidence(self, value, path):
        for index, entry in enumerate(self.short_list(value, MAX_EVIDENCE, path) or []):
            self.text(entry, MAX_EVIDENCE_CHARS, False, "%s[%d]" % (path, index))


def findings_rules(document):
    """A list of (code, path) for every rule of the findings contract that `document` breaks; [] if none is found."""
    found = _Found()
    if not found.keys(document, ("crop", "growth_stage", "symptoms", "needs_more"), ""):
        return found.items
    if "crop" in document and found.keys(document["crop"], ("candidates",), "crop") and "candidates" in document["crop"]:
        for index, item in enumerate(found.short_list(document["crop"]["candidates"], MAX_CROP_CANDIDATES,
                                                      "crop.candidates") or []):
            path = "crop.candidates[%d]" % index
            if found.keys(item, ("name", "evidence", "uncertainty"), path):
                if "name" in item:
                    found.text(item["name"], MAX_NAME, True, path + ".name")
                if "evidence" in item:
                    found.evidence(item["evidence"], path + ".evidence")
                if "uncertainty" in item and item["uncertainty"] not in UNCERTAINTY:
                    found.add("INVALID_ENUM_VALUE", path + ".uncertainty")
    if "growth_stage" in document:
        stage = document["growth_stage"]
        if found.keys(stage, ("candidate", "evidence", "uncertainty"), "growth_stage"):
            if "candidate" in stage and stage["candidate"] is not None:
                found.text(stage["candidate"], MAX_NAME, True, "growth_stage.candidate")
            if "evidence" in stage:
                found.evidence(stage["evidence"], "growth_stage.evidence")
            if "uncertainty" in stage and stage["uncertainty"] not in UNCERTAINTY:
                found.add("INVALID_ENUM_VALUE", "growth_stage.uncertainty")
            if ("candidate" in stage and stage["candidate"] is None and isinstance(stage.get("evidence"), list)
                    and stage["evidence"]):
                found.add("EVIDENCE_WITHOUT_CANDIDATE", "growth_stage")
    if "symptoms" in document:
        for index, item in enumerate(found.short_list(document["symptoms"], MAX_SYMPTOMS, "symptoms") or []):
            path = "symptoms[%d]" % index
            if found.keys(item, ("observation", "possible_interpretations"), path):
                if "observation" in item:
                    found.text(item["observation"], MAX_TEXT, False, path + ".observation")
                if "possible_interpretations" in item:
                    labels = found.short_list(item["possible_interpretations"], MAX_INTERPRETATIONS,
                                              path + ".possible_interpretations")
                    for number, label in enumerate(labels or []):
                        found.text(label, MAX_NAME, True, "%s.possible_interpretations[%d]" % (path, number))
    if "needs_more" in document:
        for index, entry in enumerate(found.short_list(document["needs_more"], MAX_NEEDS, "needs_more") or []):
            found.text(entry, MAX_TEXT, False, "needs_more[%d]" % index)
    return found.items


def _rules_of(document):
    """(list of {"code", "path"} limited to MAX_RULES_SHOWN, total count) for a document the validator refused. Never
    raises; a failure of the diagnostic itself is one DIAGNOSTIC_ERROR entry, and a refusal it cannot explain is one
    UNDIAGNOSED_REJECTION entry."""
    try:
        items = findings_rules(document)
        if not items:
            items = [("UNDIAGNOSED_REJECTION", "")]
    except Exception:
        items = [("DIAGNOSTIC_ERROR", "")]
    return [{"code": code, "path": path} for code, path in items[:MAX_RULES_SHOWN]], min(len(items), MAX_COUNT)


def _count(value, limit):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit:
        return value
    return None


def _finish(value):
    if value is None:
        return "absent"
    return value if isinstance(value, str) and value in FINISH_REASONS and value != "absent" else "other"


def _check_text(text):
    """(content_shape, findings_check, rules, rules_total) for reply text of at least one non-blank character, following
    the same steps as `parse_model_findings`: too long, optional single code fence, JSON, an object, the findings
    rules. `rules` and `rules_total` are None unless the check is "contract_failed"."""
    if len(text) > MAX_REPLY_CHARS:
        return None, "too_long", None, None
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return "other", "not_json", None, None
    if not isinstance(document, dict):
        return "other", "not_an_object", None, None
    shape = "fenced_json" if fenced else "json_object"
    if validate_findings(document):
        return shape, "accepted", None, None
    rules, total = _rules_of(document)
    return shape, "contract_failed", rules, total


def reply_facts_of(body):
    """The facts (a dict with exactly FACT_KEYS) for the raw bytes of an HTTP success reply, or None if `body` is not
    bytes. Never raises for a reply that is merely odd."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    facts = dict.fromkeys(FACT_KEYS)
    try:
        document = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        facts["envelope"] = "not_json"
        return facts
    if not isinstance(document, dict):
        facts["envelope"] = "not_an_object"
        return facts
    usage = document.get("usage")
    if isinstance(usage, dict):
        facts["completion_tokens"] = _count(usage.get("completion_tokens"), MAX_COUNT)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            facts["reasoning_tokens"] = _count(details.get("reasoning_tokens"), MAX_COUNT)
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        facts["envelope"] = "no_choices"
        return facts
    facts["finish_reason"] = _finish(choices[0].get("finish_reason"))
    message = choices[0].get("message")
    if not isinstance(message, dict):
        facts["envelope"] = "no_message"
        return facts
    if "content" not in message:
        facts["envelope"] = "content_absent"
        return facts
    content = message["content"]
    if content is None:
        facts["envelope"] = "content_null"
        return facts
    if not isinstance(content, str):
        facts["envelope"] = "content_not_text"
        return facts
    facts["content_chars"] = min(len(content), MAX_CHARS)
    if content.strip() == "":
        facts["envelope"] = "content_blank"
        return facts
    facts["envelope"] = "content_text"
    (facts["content_shape"], facts["findings_check"], facts["findings_rules"],
     facts["findings_rules_total"]) = _check_text(content)
    return facts


def clean_reply_facts(raw):
    """The allowlisted copy of `reply_facts_of` output, or None unless it has exactly FACT_KEYS with every value a
    listed closed word or a bounded whole number (or None where a fact was not available)."""
    try:
        if not isinstance(raw, dict) or set(raw) != set(FACT_KEYS):
            return None
        for key, allowed in (("envelope", ENVELOPES), ("finish_reason", FINISH_REASONS), ("content_shape", SHAPES),
                             ("findings_check", FINDINGS_CHECKS)):
            if raw[key] is not None and not (isinstance(raw[key], str) and raw[key] in allowed):
                return None
        if raw["envelope"] is None:
            return None
        for key, limit in (("completion_tokens", MAX_COUNT), ("reasoning_tokens", MAX_COUNT),
                           ("content_chars", MAX_CHARS)):
            if raw[key] is not None and _count(raw[key], limit) is None:
                return None
        if not _rules_ok(raw["findings_rules"], raw["findings_rules_total"], raw["findings_check"]):
            return None
        cleaned = {key: raw[key] for key in FACT_KEYS}
        if cleaned["findings_rules"] is not None:
            cleaned["findings_rules"] = [{"code": r["code"], "path": r["path"]} for r in cleaned["findings_rules"]]
        return cleaned
    except Exception:
        return None


def _rules_ok(rules, total, check):
    """True if the rule list is absent (and so is its total) or, only for a "contract_failed" check, a list of at most
    MAX_RULES_SHOWN entries that each have exactly a listed code and a path of the closed structural grammar."""
    if rules is None or total is None:
        return rules is None and total is None
    if check != "contract_failed" or not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES_SHOWN:
        return False
    if _count(total, MAX_COUNT) is None or total < len(rules):
        return False
    for entry in rules:
        if not isinstance(entry, dict) or set(entry) != {"code", "path"}:
            return False
        if not isinstance(entry["code"], str) or entry["code"] not in RULE_CODES:
            return False
        if not isinstance(entry["path"], str) or len(entry["path"]) > 80:
            return False
        if _INDEX.sub("[]", entry["path"]) not in _PATH_TEMPLATES or "[]" in entry["path"]:
            return False
    return True
