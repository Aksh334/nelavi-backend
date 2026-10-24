"""Manual smoke test: ONE real NVIDIA image request through the Second Opinion code path.

Run it yourself from the repository root, in a terminal where you have set NVIDIA_API_KEY:

    python -B -m scripts.nvidia_smoke_test            # dry run: sends nothing, needs no key
    python -B -m scripts.nvidia_smoke_test --send     # makes ONE real request (may use credits)

It uses the same adapter, request builder, prompt and answer rules as the demo app, with a
synthetic gradient image and synthetic readings (no photo, no real data). It prints two
results that are judged separately:

  STAGE 1  provider call : did NVIDIA answer with parsable text? (the adapter's own result)
  STAGE 2  answer rules  : did that text pass the app's answer rule? The rule is the one the
                           app enforces: non-empty and FEWER THAN 80 whitespace-separated tokens
                           (second_opinion.TOKEN_LIMIT, counted with str.split()). That is not a
                           word count in any linguistic sense and not a model-token count.

Only safe diagnostics are printed: pass/fail, a closed error code, counts and elapsed time.
It never prints the API key, headers, the value of any variable, the request body or any text the
model wrote. It makes no second attempt (a second provider call is refused), does not fall back to the
simulated provider and changes no limit, timeout or request body. Nothing is written to disk.
Add --show-prompt to a dry run to read the prompt that would be sent (it contains only the
synthetic values above).
Exit codes: 0 both stages passed, 1 stage 1 failed, 2 not run (usage or missing key),
3 stage 1 passed but stage 2 failed.
"""

import argparse
import base64
import struct
import sys
import time
import zlib

from image_analysis.nvidia import ENDPOINT, MODEL, NvidiaImageAnalyzer, api_key_present
from second_opinion.second_opinion import (
    READING_GROUPS,
    TOKEN_LIMIT,
    SecondOpinionService,
    build_prompt,
    parse_request,
)

STAGE_ONE_MEANING = {
    "credential_missing": "no usable API key was found",
    "configuration_invalid": "the adapter settings or prompt were invalid",
    "image_unsupported": "the image was refused locally (PNG within the size limit required)",
    "provider_rejected": "NVIDIA rejected the request (HTTP 4xx, e.g. key, model access or quota)",
    "provider_unavailable": "NVIDIA reported a server-side problem (HTTP 5xx, e.g. 503)",
    "provider_timeout": "no answer arrived before the time limit",
    "provider_connection_failed": "the connection to NVIDIA could not be made",
    "transport_failed": "the request failed for another network reason and no answer arrived",
    "provider_unreachable": "no answer arrived (older, less specific code; the current adapter reports the three above)",
    "response_malformed": "NVIDIA answered but the reply could not be read as expected",
    "response_empty": "NVIDIA answered with empty text",
    "response_too_large": "the reply exceeded the local 4 MiB limit",
}


def synthetic_png(size=64):
    """A small gradient PNG built in memory. It is not a photo of anything."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(
        b"\x00" + b"".join(bytes((x * 4 % 256, y * 4 % 256, 96)) for x in range(size))
        for y in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def synthetic_payload():
    readings = {name: {key: float(index + 1) for index, key in enumerate(keys)}
                for name, keys in READING_GROUPS}
    return {
        "image_png_base64": base64.b64encode(synthetic_png()).decode("ascii"),
        "readings": readings,
        "crop": "Tomato",
        "language": "English",
    }


class OneRequestOnly(Exception):
    """A second provider call was attempted; it is refused."""


class RecordingAnalyzer:
    """Wraps the real adapter and remembers its raw result, so the two stages can be judged
    separately from ONE request. A second call is refused, so nothing can be repeated."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.last = None

    def analyze(self, request):
        if self.calls >= 1:
            raise OneRequestOnly()
        self.calls += 1
        self.last = self._inner.analyze(request)
        return self.last


def run(argv=None, stdout=None, stderr=None, analyzer=None, key_present=None):
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(
        prog="python -m scripts.nvidia_smoke_test",
        description="One real NVIDIA image request through the Second Opinion code path. "
                    "Without --send nothing is sent and no key is needed.")
    parser.add_argument("--send", action="store_true",
                        help="actually send ONE request to NVIDIA (may use credits)")
    parser.add_argument("--show-prompt", action="store_true",
                        help="in a dry run, also print the prompt text (synthetic values only)")
    args = parser.parse_args(argv)

    payload = synthetic_payload()
    request = parse_request(payload)
    prompt = build_prompt(request)
    size = len(base64.b64decode(payload["image_png_base64"]))

    print("NVIDIA smoke test: synthetic %d-byte PNG, synthetic readings, crop 'Tomato', "
          "language 'English'." % size, file=stdout)
    print("Endpoint: %s" % ENDPOINT, file=stdout)
    print("Model: %s" % MODEL, file=stdout)
    if not args.send:
        print("\nDRY RUN: nothing was sent and no key was read. The prompt is %d characters."
              % len(prompt), file=stdout)
        if args.show_prompt:
            print("\n" + prompt, file=stdout)
        print("\nRe-run with --send to make ONE real request.", file=stdout)
        return 0

    present = key_present if key_present is not None else api_key_present
    if not present():
        print("Error: set the NVIDIA_API_KEY variable in this terminal first. Nothing was sent.",
              file=stderr)
        return 2

    print("\nSending ONE request. The synthetic image, readings, crop and language go to NVIDIA "
          "and may use credits. Waiting up to 60 seconds...", file=stdout)
    recorder = RecordingAnalyzer(analyzer if analyzer is not None else NvidiaImageAnalyzer())
    service = SecondOpinionService(recorder, simulated=False)
    started = time.monotonic()
    try:
        result = service.get_opinion(payload)
    except Exception as exc:  # report only the type, never details that could hold a secret
        print("Unexpected failure (%s). No result." % type(exc).__name__, file=stderr)
        return 1
    elapsed = time.monotonic() - started
    raw = recorder.last
    print("Provider requests made: %d. Elapsed: %.1f s." % (recorder.calls, elapsed), file=stdout)

    print("\nSTAGE 1 - provider call", file=stdout)
    if raw is None or raw.status != "ok":
        code = getattr(raw, "error_code", None)
        print("  FAIL: %s (%s)" % (code, STAGE_ONE_MEANING.get(code, "unknown")), file=stdout)
        print("\nRESULT: provider call failed; stage 2 not applicable.", file=stdout)
        return 1
    tokens = len(raw.text.split())
    print("  PASS: NVIDIA answered with HTTP success and parsable text "
          "(%d whitespace-separated tokens, %d characters)." % (tokens, len(raw.text)),
          file=stdout)

    print("\nSTAGE 2 - the app's answer rule (non-empty and fewer than %d whitespace-separated "
          "tokens)" % TOKEN_LIMIT, file=stdout)
    if result.error_code is None:
        print("  PASS: the answer was accepted. Its text is not printed here.", file=stdout)
        print("\nRESULT: both stages passed.", file=stdout)
        return 0
    print("  FAIL: the app would NOT show this answer (code: %s)." % result.error_code,
          file=stdout)
    print("  Measured: %d tokens against a limit of fewer than %d. The answer text is not "
          "printed here." % (tokens, TOKEN_LIMIT), file=stdout)
    print("\nRESULT: the connection works but the answer fails the app's rule.", file=stdout)
    return 3


if __name__ == "__main__":
    raise SystemExit(run())
