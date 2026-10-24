"""Manual smoke test: ONE real NVIDIA image request through the image-FINDINGS contract.

Run it yourself from the repository root, in a terminal where you have set NVIDIA_API_KEY:

    python -B -m scripts.nvidia_findings_smoke_test                       # dry run, no image needed
    python -B -m scripts.nvidia_findings_smoke_test --image my_leaf.png   # dry run that checks the file
    python -B -m scripts.nvidia_findings_smoke_test --image my_leaf.png --send --image-is-non-sensitive

A live request needs ALL of: `--send`, `--image PATH`, `--image-is-non-sensitive`, a usable key, and a
PNG that passes the checks below. It makes at most ONE HTTP request: a counting wrapper around the
adapter's transport refuses any second request and any URL other than the official endpoint. There is
no retry and no fallback to another provider or the simulated one.

What it asks: the image-findings prompt of `agri_pipeline.findings` (no user hints), through the existing
`NvidiaImageAnalyzer` with its defaults (model, `max_tokens` 1024, 60 s timeout). Two results are judged
separately:
  STAGE 1  provider call : did NVIDIA answer with parsable text? (the adapter's own result)
  STAGE 2  findings      : is the text exactly the findings JSON the contract accepts?

Reasoning mode (opt-in, this script only): NVIDIA documents `"chat_template_kwargs":
{"enable_thinking": false}` as a TOP-LEVEL request field, but only in its local-serving (vLLM)
examples. Its hosted-endpoint examples never use it, so whether the hosted endpoint accepts it is NOT
STATED. `--thinking-off` adds exactly that field to the request body, here and nowhere else; without
the flag the body is the adapter's unchanged, previously verified one. NVIDIA's page also says that with
reasoning on, the chain of thought appears in `content`, which the findings contract would reject.

Image rules (checked locally before anything is sent): a PNG signature and well-formed chunks, at most
1 MiB (the adapter's local limit), and no metadata chunks (eXIf, tEXt, iTXt, zTXt) unless you pass
`--allow-metadata`, because metadata can hold location or camera details. Use a non-sensitive picture:
no people, no private information.

Only safe diagnostics are printed: pass/fail, a closed error code, sizes, counts, booleans and elapsed
time. Stage 1 also prints a "Transport observation": the numeric HTTP status code when an HTTP response
existed (with its class only, for example "server error response"), or the kind of failure when none did
(timeout, connection failure). It never says why a status occurred, and never prints a body or header. It never prints the API key, headers, the image, the request body (unless you ask for the
dry-run outline, which replaces the image by a placeholder) or any text the model wrote. Nothing is
written to disk.
Exit codes: 0 both stages passed, 1 stage 1 failed, 2 not run (usage, image or key problem),
3 stage 1 passed but stage 2 failed.
"""

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import unicodedata

from agri_pipeline.findings import (
    _FENCE,
    MAX_CROP_CANDIDATES,
    MAX_INTERPRETATIONS,
    MAX_NEEDS,
    MAX_REPLY_CHARS,
    MAX_SYMPTOMS,
    PROMPT,
    PromptedFindingsProvider,
)
from crop_context.schema import (
    MAX_EVIDENCE,
    MAX_EVIDENCE_CHARS,
    MAX_NAME,
    MAX_TEXT,
    UNCERTAINTY,
    _name,
)
from image_analysis.boundary import ImageInput, is_supported_png
from image_analysis.nvidia import (
    DEFAULT_MAX_IMAGE_BYTES,
    ENDPOINT,
    MODEL,
    NvidiaConfig,
    NvidiaImageAnalyzer,
    ResponseTooLarge,
    TransportError,
    TransportTimeout,
    _build_body,
    api_key_present,
    urllib_transport,
)
from scripts.nvidia_smoke_test import STAGE_ONE_MEANING, OneRequestOnly, RecordingAnalyzer

THINKING_OFF = {"enable_thinking": False}
METADATA_CHUNKS = (b"eXIf", b"tEXt", b"iTXt", b"zTXt")
IMAGE_PLACEHOLDER = "<data URL of your PNG>"


class WrongEndpoint(Exception):
    """A request to a URL other than the official endpoint was refused."""


OBS_HTTP = "http_response"
OBS_TIMEOUT = "timeout"
OBS_CONNECTION = "connection_failure"
OBS_TOO_LARGE = "response_too_large"
OBS_OTHER = "other_transport_error"
OBS_NO_STATUS = "no_valid_status"
OBS_REFUSED = "refused_by_guard"
OBSERVATIONS = (OBS_HTTP, OBS_TIMEOUT, OBS_CONNECTION, OBS_TOO_LARGE, OBS_OTHER, OBS_NO_STATUS, OBS_REFUSED)


def status_class(status):
    """A neutral description of an HTTP status code. It states the class only; it never says why."""
    if 100 <= status < 200:
        return "informational response"
    if 200 <= status < 300:
        return "success"
    if 300 <= status < 400:
        return "redirect, not followed"
    if 400 <= status < 500:
        return "client error response"
    if 500 <= status < 600:
        return "server error response; the status alone does not say why"
    return "outside the standard status ranges"


def describe_transport(guarded):
    """One line saying what the transport saw: the numeric status when an HTTP response existed, or the
    kind of failure when none did. No response body or header is ever included."""
    if guarded.observation == OBS_HTTP:
        return "HTTP status %d (%s)" % (guarded.status, status_class(guarded.status))
    return {
        OBS_TIMEOUT: "no complete HTTP response: the request timed out (the transport cannot tell a timeout "
                     "before any response from one while the body was being read)",
        OBS_CONNECTION: "no complete HTTP response: connection failure (before or during the response; the "
                        "transport does not distinguish DNS, TLS, refused or reset connections)",
        OBS_TOO_LARGE: "an HTTP response arrived but its body exceeded the local size limit; the status is "
                       "not available",
        OBS_OTHER: "no HTTP response: an unexpected transport error",
        OBS_NO_STATUS: "a response arrived without a valid numeric status",
        OBS_REFUSED: "the request was refused by this script's guard",
        None: "no request reached the transport",
    }.get(guarded.observation, "unknown")


def add_thinking_off(body):
    """Return the request body (bytes) with the documented reasoning-off field added at the top level.

    Nothing else in the body changes. Raises ValueError if the body is not a JSON object or already
    carries the field."""
    document = json.loads(body.decode("utf-8"))
    if not isinstance(document, dict) or "chat_template_kwargs" in document:
        raise ValueError("unexpected request body")
    document["chat_template_kwargs"] = dict(THINKING_OFF)
    return json.dumps(document).encode("utf-8")


class GuardedTransport:
    """Wraps the real transport: refuses a second request and any URL but the official endpoint, and
    optionally adds the reasoning-off field. `calls` counts requests handed to the real transport."""

    def __init__(self, inner, thinking_off):
        self._inner = inner
        self._thinking_off = thinking_off
        self.calls = 0
        self.body_fields = None
        # What the transport observed, for diagnostics only. `status` is the numeric HTTP status code
        # when an HTTP response existed; `observation` is one of the OBSERVATIONS below. The response
        # body and headers are never stored here.
        self.status = None
        self.observation = None

    def __call__(self, url, headers, body, timeout):
        if url != ENDPOINT:
            self.observation = self.observation or OBS_REFUSED
            raise WrongEndpoint()
        if self.calls >= 1:
            self.observation = self.observation or OBS_REFUSED
            raise OneRequestOnly()
        self.calls += 1
        if self._thinking_off:
            body = add_thinking_off(body)
        self.body_fields = sorted(json.loads(body.decode("utf-8")))
        try:
            status, response_body = self._inner(url, headers, body, timeout)
        except ResponseTooLarge:
            self.observation = OBS_TOO_LARGE  # the status is not available: the transport raises instead
            raise
        except TransportTimeout:
            self.observation = OBS_TIMEOUT
            raise
        except TransportError:
            self.observation = OBS_CONNECTION
            raise
        except Exception:
            self.observation = OBS_OTHER
            raise
        if isinstance(status, int) and not isinstance(status, bool):
            self.status, self.observation = status, OBS_HTTP
        else:
            self.observation = OBS_NO_STATUS
        return status, response_body


def inspect_png(data):
    """Return {"width", "height", "chunks"} for a well-formed PNG, or None. Nothing is decoded."""
    if not is_supported_png(ImageInput(data, "image/png")) or len(data) < 33:
        return None
    position, chunks, header = 8, [], None
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data) or not kind.isalpha():
            return None
        if not chunks:
            if kind != b"IHDR" or length != 13:
                return None
            header = struct.unpack(">II", data[position + 8:position + 16])
        chunks.append(kind)
        position = end
        if kind == b"IEND":
            break
    if not chunks or chunks[-1] != b"IEND" or header is None or 0 in header:
        return None
    return {"width": header[0], "height": header[1], "chunks": [c.decode("ascii") for c in chunks]}


def load_image(path):
    """Return (data, info, None) or (None, None, reason_code). Reasons are fixed codes."""
    try:
        size = os.stat(path).st_size
    except (OSError, ValueError, TypeError):
        return None, None, "image_not_found"
    if size == 0:
        return None, None, "image_empty"
    if size > DEFAULT_MAX_IMAGE_BYTES:
        return None, None, "image_too_large"
    try:
        with open(path, "rb") as handle:
            data = handle.read(DEFAULT_MAX_IMAGE_BYTES + 1)
    except OSError:
        return None, None, "image_not_readable"
    info = inspect_png(data)
    if info is None:
        return None, None, "image_not_a_valid_png"
    info["bytes"] = len(data)
    info["sha256_prefix"] = hashlib.sha256(data).hexdigest()[:16]
    info["metadata_chunks"] = sorted({c for c in info["chunks"] if c.encode("ascii") in METADATA_CHUNKS})
    return data, info, None


def request_outline(thinking_off):
    """The request body as JSON text, with the image replaced by a placeholder."""
    body = json.loads(_build_body(NvidiaConfig(), PROMPT, b"\x89PNG\r\n\x1a\n").decode("utf-8"))
    body["messages"][0]["content"][1]["image_url"]["url"] = IMAGE_PLACEHOLDER
    if thinking_off:
        body["chat_template_kwargs"] = dict(THINKING_OFF)
    return json.dumps(body, indent=2)


def reply_diagnostics(text):
    """Booleans and counts about a model reply; never any of its text."""
    stripped = text.strip()
    fenced = stripped.startswith("```")
    body = stripped
    if fenced:
        lines = stripped.splitlines()
        body = "\n".join(lines[1:-1]) if len(lines) >= 3 and lines[-1].strip() == "```" else stripped
    parsed = None
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        pass
    return {
        "characters": len(text), "tokens": len(text.split()), "starts_with_brace": stripped.startswith("{"),
        "fenced": fenced, "json_parses": parsed is not None, "json_is_object": isinstance(parsed, dict),
    }


# ---------------------------------------------------------------------------------------------
# Failure diagnostics. They answer "which rule of the findings contract rejected this reply?" with
# closed codes and structural paths only. A path is made of our own contract's field names and list
# positions (for example `symptoms[0].observation`); it never contains anything the model wrote. A key
# name that the model invented is NOT printed (EXTRA_FIELD names only the object it was found in). The
# rules below mirror `agri_pipeline.findings.validate_findings` and reuse its constants and its own text
# checks; a test proves the two agree on a large battery of documents. If the validator ever rejects a
# reply that these rules cannot explain, the code is UNDIAGNOSED_REJECTION, never a guess.
# ---------------------------------------------------------------------------------------------
DIAGNOSTIC_CODES = (
    "REPLY_EMPTY", "REPLY_TOO_LONG", "NOT_JSON", "WRONG_TYPE", "MISSING_REQUIRED_KEY", "EXTRA_FIELD",
    "INVALID_ENUM_VALUE", "TOO_MANY_ITEMS", "EMPTY_TEXT", "TEXT_TOO_LONG", "CONTROL_CHARACTER",
    "DISALLOWED_CHARACTER", "EVIDENCE_WITHOUT_CANDIDATE", "UNDIAGNOSED_REJECTION", "DIAGNOSTIC_ERROR")
MAX_DIAGNOSTICS_SHOWN = 12
ITEM_SCAN_LIMIT = 50


def _text_problem(value, limit, is_name):
    """The first rule of `_text` / `_name` that `value` breaks, as a code, or None if it passes."""
    if not isinstance(value, str):
        return "WRONG_TYPE"
    if value.strip() == "":
        return "EMPTY_TEXT"
    if len(value) > limit:
        return "TEXT_TOO_LONG"
    if any(unicodedata.category(c).startswith("C") for c in value):
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
        """Check an object's exact key set. Returns True if it is a dict (so its members can be checked)."""
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
        """Returns the items to check, or None if `value` is not a list. Too long is reported once."""
        if not isinstance(value, list):
            self.add("WRONG_TYPE", path)
            return None
        if len(value) > limit:
            self.add("TOO_MANY_ITEMS", path)
        return value[:ITEM_SCAN_LIMIT]

    def evidence(self, value, path):
        for index, entry in enumerate(self.short_list(value, MAX_EVIDENCE, path) or []):
            self.text(entry, MAX_EVIDENCE_CHARS, False, "%s[%d]" % (path, index))


def diagnose_findings(document):
    """Return a list of (code, path) for every rule of the findings contract that `document` breaks.
    An empty list means the contract's rules are all met."""
    found = _Found()
    if not found.keys(document, ("crop", "growth_stage", "symptoms", "needs_more"), ""):
        return found.items
    if "crop" in document and found.keys(document["crop"], ("candidates",), "crop") and "candidates" in document["crop"]:
        candidates = found.short_list(document["crop"]["candidates"], MAX_CROP_CANDIDATES, "crop.candidates")
        for index, item in enumerate(candidates or []):
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


def diagnose_reply(text):
    """Return a list of (code, path) explaining why `text` is not accepted as findings ([] if it is).
    Never raises; never includes any of `text`."""
    try:
        if not isinstance(text, str) or not text.strip():
            return [("REPLY_EMPTY", "")]
        if len(text) > MAX_REPLY_CHARS:
            return [("REPLY_TOO_LONG", "")]
        body = text.strip()
        fenced = _FENCE.match(body)
        if fenced:
            body = fenced.group(1).strip()
        try:
            document = json.loads(body)
        except (ValueError, RecursionError):
            return [("NOT_JSON", "")]
        return diagnose_findings(document)
    except Exception:  # a diagnostic bug must never hide the result or leak anything
        return [("DIAGNOSTIC_ERROR", "")]


def format_diagnostics(items):
    """Render (code, path) pairs as one line: codes and structural paths only."""
    shown = ["%s at %s" % (code, path or "(top level)") for code, path in items[:MAX_DIAGNOSTICS_SHOWN]]
    more = len(items) - MAX_DIAGNOSTICS_SHOWN
    return "; ".join(shown) + (" (+%d more)" % more if more > 0 else "")


def run(argv=None, stdout=None, stderr=None, transport=None, key_present=None, environ=None):
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(
        prog="python -m scripts.nvidia_findings_smoke_test",
        description="One real NVIDIA image request through the findings contract. Without --send "
                    "nothing is sent and no key is needed.")
    parser.add_argument("--image", help="path of a non-sensitive PNG (at most 1 MiB)")
    parser.add_argument("--send", action="store_true", help="actually send ONE request (may use credits)")
    parser.add_argument("--image-is-non-sensitive", action="store_true",
                        help="confirm the PNG has no people and no private information (needed with --send)")
    parser.add_argument("--thinking-off", action="store_true",
                        help="add the documented chat_template_kwargs enable_thinking=false field "
                             "(documented for local serving only; hosted support is not stated)")
    parser.add_argument("--allow-metadata", action="store_true",
                        help="allow a PNG that carries metadata chunks (eXIf, tEXt, iTXt, zTXt)")
    parser.add_argument("--show-request", action="store_true",
                        help="in a dry run, print the request body outline (image replaced by a placeholder)")
    args = parser.parse_args(argv)

    data = info = None
    if args.image is not None:
        data, info, reason = load_image(args.image)
        if reason is not None:
            print("Error: %s. Nothing was sent." % reason, file=stderr)
            return 2

    print("NVIDIA findings smoke test", file=stdout)
    print("Endpoint: %s" % ENDPOINT, file=stdout)
    print("Model: %s" % MODEL, file=stdout)
    print("Request settings: max_tokens %d, timeout %s s, reasoning %s" % (
        NvidiaConfig().max_tokens, NvidiaConfig().timeout_seconds,
        "OFF (enable_thinking=false added)" if args.thinking_off else "left at the adapter's unchanged default"),
        file=stdout)
    print("Prompt: the findings prompt, %d characters, no user hints" % len(PROMPT), file=stdout)
    if info is None:
        print("Image: none given", file=stdout)
    else:
        print("Image: %s, %d bytes, %dx%d, sha256 %s..., metadata chunks: %s" % (
            os.path.basename(args.image), info["bytes"], info["width"], info["height"], info["sha256_prefix"],
            ", ".join(info["metadata_chunks"]) or "none"), file=stdout)

    if not args.send:
        print("\nDRY RUN: nothing was sent and no key was read.", file=stdout)
        if args.show_request:
            print("\nRequest body outline:\n" + request_outline(args.thinking_off), file=stdout)
        print("\nA live run needs --send, --image PATH and --image-is-non-sensitive.", file=stdout)
        return 0

    refusal = None
    if data is None:
        refusal = "--send needs --image PATH"
    elif not args.image_is_non_sensitive:
        refusal = "--send needs --image-is-non-sensitive (confirm the PNG has no people or private information)"
    elif info["metadata_chunks"] and not args.allow_metadata:
        refusal = "the PNG carries metadata chunks (%s); re-save it without metadata or pass --allow-metadata" % (
            ", ".join(info["metadata_chunks"]))
    elif not (key_present if key_present is not None else api_key_present)():
        refusal = "set the NVIDIA_API_KEY variable in this terminal first"
    if refusal is not None:
        print("Error: %s. Nothing was sent." % refusal, file=stderr)
        return 2

    guarded = GuardedTransport(transport if transport is not None else urllib_transport, args.thinking_off)
    recorder = RecordingAnalyzer(NvidiaImageAnalyzer(transport=guarded, environ=environ))
    provider = PromptedFindingsProvider(recorder)
    print("\nSending ONE request. Your PNG and the prompt go to NVIDIA and may use trial credits. "
          "Waiting up to 60 seconds...", file=stdout)
    started = time.monotonic()
    try:
        result = provider.analyze_image(data)
    except Exception as exc:  # report only the type, never details that could hold a secret
        print("Unexpected failure (%s). No result." % type(exc).__name__, file=stderr)
        return 1
    elapsed = time.monotonic() - started
    raw = recorder.last
    print("HTTP requests handed to the transport: %d. Request body fields: %s. Elapsed: %.1f s." % (
        guarded.calls, ", ".join(guarded.body_fields or []) or "none", elapsed), file=stdout)

    print("\nSTAGE 1 - provider call", file=stdout)
    print("  Transport observation: %s." % describe_transport(guarded), file=stdout)
    if raw is None or raw.status != "ok":
        code = getattr(raw, "error_code", None)
        print("  FAIL: %s (%s)" % (code, STAGE_ONE_MEANING.get(code, "unknown")), file=stdout)
        print("\nRESULT: provider call failed; stage 2 not applicable.", file=stdout)
        return 1
    stats = reply_diagnostics(raw.text)
    print("  PASS: NVIDIA answered with HTTP success and parsable text (%d whitespace-separated tokens, %d "
          "characters)." % (stats["tokens"], stats["characters"]), file=stdout)

    print("\nSTAGE 2 - the findings contract (one JSON object in the exact bounded shape)", file=stdout)
    if result.status == "ok":
        findings = result.findings
        print("  PASS: the reply is valid findings. Crop candidates: %d; growth stage given: %s; symptoms: %d; "
              "more-information requests: %d. The text is not printed here." % (
                  len(findings["crop"]["candidates"]), "yes" if findings["growth_stage"]["candidate"] else "no",
                  len(findings["symptoms"]), len(findings["needs_more"])), file=stdout)
        print("\nRESULT: both stages passed. This shows one reply fit the contract; it does not show the "
              "model's observations are correct.", file=stdout)
        return 0
    print("  FAIL: the reply was not accepted (code: %s)." % result.error_code, file=stdout)
    print("  Reply shape: starts with '{': %s; code-fenced: %s; parses as JSON: %s; JSON is an object: %s. "
          "The text is not printed here." % (stats["starts_with_brace"], stats["fenced"], stats["json_parses"],
                                              stats["json_is_object"]), file=stdout)
    diagnostics = diagnose_reply(raw.text)
    if not diagnostics:
        # The validator rejected the reply but no rule here explains it: say so rather than guess.
        diagnostics = [("UNDIAGNOSED_REJECTION", "")]
    print("  Why (closed codes and structural paths only; nothing the model wrote): %s" %
          format_diagnostics(diagnostics), file=stdout)
    print("\nRESULT: the connection works but the reply does not fit the findings contract.", file=stdout)
    return 3


if __name__ == "__main__":
    raise SystemExit(run())
