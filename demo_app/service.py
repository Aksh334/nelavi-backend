"""Application logic for the Nelavi local demo. No HTTP and no sockets here.

It composes existing modules and adds no schema of its own:
mock rover (mock_rover) -> Record (validated by record.validation) -> SQLite store
(storage.record_store) -> history, plus the demo-only analyzer for a caller-supplied PNG.
The mock rover supplies only a photo *identifier*, never image bytes; the PNG a user
selects in the browser is analysed separately and is never attached to a Record, because
how photos are stored and moved is an open contract item (W-12, A-12, R-12).
"""

import base64
import binascii
import hashlib

from agri_pipeline.findings import PromptedFindingsProvider
from agri_pipeline.pipeline import research_report
from agri_pipeline.reply_facts import clean_reply_facts, reply_facts_of
from agri_pipeline.pipeline import run as run_pipeline
from agri_pipeline.pipeline import validate_user
from image_analysis.boundary import AnalysisRequest, ImageInput
from mock_rover.config import RoverConfig
from mock_rover.scenarios import get_scenario, run_scenario
from mock_rover.simulator import MockRover, MockRoverError
from orchestration import integrated, pipeline_steps, synthesis, task_runner
from record.validation import (
    AIR_KEYS,
    BATTERY_KEYS,
    SENSOR_READINGS,
    SOIL_KEYS,
    validate_record,
)
from second_opinion import second_opinion as second_opinion_core
from second_opinion.second_opinion import InvalidRequest, SecondOpinionService
from second_opinion.simulated_provider import SimulatedSecondOpinionProvider
from storage.record_store import StorageError

from demo_app.demo_analyzer import DEMO_PROMPT, MAX_IMAGE_BYTES, DemoImageAnalyzer
from demo_app.fake_findings import FakeFindingsAnalyzer
from demo_app.fake_research import QUERY_MAX_CHARS, FakeResearchClient, query_is_valid
from voice_assistant.assistant import CropSyncAssistant
from voice_assistant.boundary import (
    LANG_EN,
    LANGUAGE_NAMES,
    MAX_AUDIO_BYTES,
    MAX_QUESTION_CHARS,
    SUPPORTED_LANGUAGES,
    AssistantQuestion,
    SpeechRequest,
    TranscriptionRequest,
)
from voice_assistant.lessons import LESSONS, get_lesson_detail, get_lessons_catalog

# Only these existing scenarios are offered. The descriptions restate what each one
# does in mock_rover/scenarios.py.
SCENARIOS = (
    ("normal_operation",
     "Drive forward, stop, insert the probe, measure, capture a (mock) photo reference "
     "and build a Record."),
    ("probe_measurement",
     "Insert the probe, measure and build a Record with no photo."),
    ("sensor_fault",
     "Inject a moisture-sensor fault, then measure: the Record stays valid and flags "
     "the fault."),
)

# The only reading with a documented unit (ARCHITECTURE_DECISIONS.md, MD-3).
READING_UNITS = {("battery", "voltage_v"): "V"}
UNIT_NOT_DEFINED = "unit not defined (open contract item)"
HISTORY_MAX = 100
SECOND_OPINION_LABEL = "SIMULATED - fixed demonstration text, no AI model was run"
# Shown only when a service declared real answers. It is not used by any default path.
SECOND_OPINION_REAL_LABEL = ("AI-GENERATED text from a model through the configured provider - "
                             "not a diagnosis; confirm with your agriculture officer")


# The orchestrated path (IM-2) is additive and uses fake providers only. Its research stage is skipped by default.
RESEARCH_SKIP, RESEARCH_FAKE, RESEARCH_FAKE_ERROR = "skip", "fake", "fake_error"
RESEARCH_MODES = (RESEARCH_SKIP, RESEARCH_FAKE, RESEARCH_FAKE_ERROR)
ORCHESTRATED_FIELDS = ("scenario", "crop", "region", "growth_stage", "research", "question")
ORCHESTRATED_LABEL = ("FAKE PROVIDERS ONLY - this report was built by the orchestration layer from the local "
                      "rover simulator and a fake research client. Nothing was sent to any service and nothing "
                      "here is a real source, a measurement from a field or advice.")
ORCHESTRATED_DATA_SOURCE = ("The Record comes from the local rover SIMULATOR. The report's origin is deliberately "
                            "'unknown' in this path: it states neither hardware nor simulation, and nothing here "
                            "was measured by hardware.")
IMAGE_STAGE_NOTE = ("Skipped on purpose. Where rover photos are stored and how a photo_id maps to image bytes are "
                    "unresolved contract items (R-12, W-12, A-12), so no image is analysed and no image is "
                    "inferred from the placeholder photo_id.")
SKIP_RESEARCH_NOT_REQUESTED = "research_not_requested"
FAKE_RESEARCH_ERROR_CODE = "provider_unavailable"

# The integrated path runs image findings, research, a compute summary and a synthesis in ONE run
# (`orchestration.integrated`). Each stage is chosen per request: skip, fake (deterministic stand-ins, labelled) or
# real (only if the server was started with that provider; there is never a fallback from one provider to another).
STAGE_SKIP, STAGE_FAKE, STAGE_REAL = "skip", "fake", "real"
STAGE_MODES = (STAGE_SKIP, STAGE_FAKE, STAGE_REAL)
INTEGRATED_STAGES = ("image", "research", "compute", "synthesis")
INTEGRATED_FIELDS = ("scenario", "crop", "region", "growth_stage", "question", "image_png_base64", "providers")
INTEGRATED_FAKE_LABEL = ("FAKE PROVIDERS ONLY - every stage that ran used a deterministic stand-in made up by the demo. "
                         "Nothing was sent to any service and nothing here is a real finding, source, computation "
                         "or advice.")
INTEGRATED_MIXED_LABEL = ("MIXED - the stages listed in real_stages used the real provider this server was started "
                          "with and sent the data listed in data_sent; every other stage was skipped or is a labelled "
                          "fake. Real output is unreviewed, may be wrong and is not advice. The synthesis, if any, is "
                          "model-generated text and never replaces the findings or the evidence it cites.")
INTEGRATED_DATA_SENT = {
    "image": "the PNG image you supplied",
    "research": "the question text, plus the crop, region and growth stage only if you typed them",
    "compute": "the sensor_status, soil, air and battery sections of the Record",
    "synthesis": "the validated image findings, the retrieved evidence items and the question text",
}
# Transport diagnostics for the real NVIDIA image request (IA-7). Only these values can reach a response: a numeric
# HTTP status, a closed outcome, a closed failure category and numbers. No text from an exception, header or body.
OBS_NOT_SENT = "not_sent"
OBS_HTTP_RESPONSE = "http_response"
OBS_NO_HTTP_RESPONSE = "no_http_response"
OBS_BAD_STATUS = "response_without_valid_status"
OBS_OUTCOMES = (OBS_NOT_SENT, OBS_HTTP_RESPONSE, OBS_NO_HTTP_RESPONSE, OBS_BAD_STATUS)
OBS_FAILURES = ("timeout", "connection_failed", "response_too_large", "other_transport_error")
OBS_MAX_SECONDS = 86400.0
# Classification of the body of an HTTP 5xx reply from the real image request (IA-8). Only one of these closed words and
# the body's length can reach a response: never a byte, slice or word of the body itself.
ERROR_BODY_CLASSES = ("empty", "not_text", "resource_exhausted", "capacity_or_rate_limit", "gateway_or_unavailable",
                      "unclassified")
ERROR_BODY_MAX_BYTES = 4 * 1024 * 1024   # the local response limit of the image adapter
# IA-9: what an HTTP error reply was, by status alone (a standard meaning of the code, not a finding about the account),
# and whether a Retry-After header came with it. `not_captured` means the transport in use does not expose headers.
STATUS_KINDS = ("rate_limited", "authentication_or_permission", "payment_required", "request_timeout", "client_error",
                "server_unavailable", "gateway_error", "server_error")
RETRY_AFTER_KINDS = ("absent", "seconds", "date", "unusable", "not_captured")
RETRY_AFTER_MAX_SECONDS = 86400


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= OBS_MAX_SECONDS


def clean_observation(raw):
    """Return the allowlisted copy of a transport observation, or None if it is not exactly the expected shape."""
    keys = ("request_made", "outcome", "http_status", "failure", "elapsed_seconds", "timeout_seconds",
            "thinking_off", "thinking_off_applied")
    if not isinstance(raw, dict) or set(raw) != set(keys):
        return None
    status, failure, elapsed, limit = raw["http_status"], raw["failure"], raw["elapsed_seconds"], raw["timeout_seconds"]
    if not all(isinstance(raw[k], bool) for k in ("request_made", "thinking_off", "thinking_off_applied")):
        return None
    if raw["outcome"] not in OBS_OUTCOMES:
        return None
    if status is not None and not (isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599):
        return None
    if failure is not None and failure not in OBS_FAILURES:
        return None
    if (elapsed is not None and not _is_number(elapsed)) or (limit is not None and not _is_number(limit)):
        return None
    # The pieces must agree: a status goes with an HTTP response, a failure with no response.
    if (raw["outcome"] == OBS_HTTP_RESPONSE) != (status is not None):
        return None
    if (raw["outcome"] == OBS_NO_HTTP_RESPONSE) != (failure is not None):
        return None
    if raw["outcome"] == OBS_NOT_SENT and (raw["request_made"] or elapsed is not None):
        return None
    if raw["outcome"] != OBS_NOT_SENT and not raw["request_made"]:
        return None
    return {k: raw[k] for k in keys}


FINGERPRINT_HEX_CHARS = 16
FINGERPRINT_MAX_BODY_BYTES = 64 * 1024 * 1024
_FINGERPRINT_HEX = frozenset("0123456789abcdef")


def fingerprint_of(data):
    """{"bytes": length, "sha256_prefix": the first 16 hex digits of its SHA-256} for bytes, else None. Only a length and
    a short digest: nothing from which the content can be recovered or read."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    return {"bytes": len(data), "sha256_prefix": hashlib.sha256(bytes(data)).hexdigest()[:FINGERPRINT_HEX_CHARS]}


def _fingerprint_part_ok(size, digest, limit):
    if size is None and digest is None:
        return True
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= limit:
        return False
    return (isinstance(digest, str) and len(digest) == FINGERPRINT_HEX_CHARS
            and all(c in _FINGERPRINT_HEX for c in digest))


def clean_request_fingerprint(raw):
    """The allowlisted copy of a request fingerprint (IA-10), or None unless it is exactly {"body_bytes",
    "body_sha256_prefix", "image_bytes", "image_sha256_prefix"}: each pair is either both None or a whole number of
    bytes and a 16-digit lower-case hex digest."""
    keys = ("body_bytes", "body_sha256_prefix", "image_bytes", "image_sha256_prefix")
    if not isinstance(raw, dict) or set(raw) != set(keys):
        return None
    if not _fingerprint_part_ok(raw["body_bytes"], raw["body_sha256_prefix"], FINGERPRINT_MAX_BODY_BYTES):
        return None
    if not _fingerprint_part_ok(raw["image_bytes"], raw["image_sha256_prefix"], MAX_IMAGE_BYTES):
        return None
    return {k: raw[k] for k in keys}


def clean_error_class(raw):
    """Return the allowlisted copy of an HTTP error-reply classification, or None unless it is exactly
    {"status_kind": one of STATUS_KINDS, "body_class": one of ERROR_BODY_CLASSES, "body_bytes": a whole number from 0
    to ERROR_BODY_MAX_BYTES, "retry_after": one of RETRY_AFTER_KINDS, "retry_after_seconds": None, or a whole number
    from 0 to RETRY_AFTER_MAX_SECONDS and only with retry_after "seconds"}."""
    keys = {"status_kind", "body_class", "body_bytes", "retry_after", "retry_after_seconds"}
    if not isinstance(raw, dict) or set(raw) != keys:
        return None
    size, seconds = raw["body_bytes"], raw["retry_after_seconds"]
    if raw["status_kind"] not in STATUS_KINDS or raw["body_class"] not in ERROR_BODY_CLASSES:
        return None
    if raw["retry_after"] not in RETRY_AFTER_KINDS:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= ERROR_BODY_MAX_BYTES:
        return None
    if (raw["retry_after"] == "seconds") != (seconds is not None):
        return None
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)
                                or not 0 <= seconds <= RETRY_AFTER_MAX_SECONDS):
        return None
    return {k: raw[k] for k in ("status_kind", "body_class", "body_bytes", "retry_after", "retry_after_seconds")}


class DemoError(Exception):
    """An error the user should see, with the HTTP status it maps to."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _sensor_for_reading():
    mapping = {}
    for sensor, paths in SENSOR_READINGS.items():
        for path in paths:
            mapping[path] = sensor
    return mapping


class DemoService:
    def __init__(self, store, rover_config=None, analyzer=None, second_opinion_provider=None,
                 second_opinion_simulated=None, research_client=None, research_domains=(),
                 research_pro_client=None, fake_research_client=None, findings_analyzer=None,
                 compute_runner=None, synthesis_provider=None, findings_observer=None,
                 elevenlabs_client=None, assistant=None):
        self._store = store
        self._elevenlabs_client = elevenlabs_client
        self._assistant = assistant if assistant is not None else CropSyncAssistant(record_store=self._store)
        self._completed_lessons = set()
        # Optional diagnostics source for the real image request: reset() before a run, snapshot() after it. The
        # service only copies the allowlisted fields of what snapshot() returns (see clean_observation).
        if findings_observer is not None and not (callable(getattr(findings_observer, "reset", None))
                                                  and callable(getattr(findings_observer, "snapshot", None))):
            raise TypeError("findings_observer must have reset() and snapshot() methods")
        self._findings_observer = findings_observer
        # IA-11: the observer sees the raw reply bytes; this service owns the closed-word inspection of them (it is the
        # only demo module that may import the findings code). Only what `reply_facts_of` returns is ever kept.
        if findings_observer is not None and callable(getattr(findings_observer, "set_reply_inspector", None)):
            findings_observer.set_reply_inspector(reply_facts_of)
        # The integrated path's REAL providers are injected here by the server entry point; the service never builds
        # or imports a provider. `findings_analyzer` is an image analyzer with analyze(); the real research client is
        # the existing `research_client` above.
        for label, obj, method in (("findings_analyzer", findings_analyzer, "analyze"),
                                   ("compute_runner", compute_runner, "run"),
                                   ("synthesis_provider", synthesis_provider, "synthesize")):
            if obj is not None and not callable(getattr(obj, method, None)):
                raise TypeError("%s must have a %s() method" % (label, method))
        self._real_findings = PromptedFindingsProvider(findings_analyzer) if findings_analyzer is not None else None
        self._compute_runner = compute_runner
        self._synthesis_provider = synthesis_provider
        # The orchestrated path (IM-2) uses this FAKE client for its "fake" research mode. It exists only so tests
        # can substitute a misbehaving fake; there is no real-provider option here.
        if fake_research_client is not None and not callable(getattr(fake_research_client, "search", None)):
            raise TypeError("fake_research_client must have a search() method")
        self._fake_research = fake_research_client
        # Research Pro (start and poll) is also off unless a client is injected. The service only needs an
        # object with start(question) and poll(task_id); it never imports a provider.
        if research_pro_client is not None:
            if research_client is not None:
                raise ValueError("choose one research provider, not both")
            if not callable(getattr(research_pro_client, "start", None)) \
                    or not callable(getattr(research_pro_client, "poll", None)):
                raise TypeError("research_pro_client must have start() and poll() methods")
        self._research_pro = research_pro_client
        # Research is off unless a client is injected together with the host names it is limited to.
        # The service never imports a provider: the client only needs a `search(question)` method.
        if research_client is not None:
            domains = list(research_domains)
            if not callable(getattr(research_client, "search", None)):
                raise TypeError("research_client must have a search() method")
            if not domains or not all(isinstance(d, str) and d for d in domains):
                raise ValueError("research needs the non-empty list of host names it is limited to")
            self._research_domains = domains
        else:
            self._research_domains = []
        self._research = research_client
        self._rover = MockRover(rover_config if rover_config is not None else RoverConfig())
        self._analyzer = analyzer if analyzer is not None else DemoImageAnalyzer()
        if second_opinion_provider is None:
            # The default is the simulated provider, and the mode can only be simulated.
            if second_opinion_simulated is not None and second_opinion_simulated is not True:
                raise ValueError("the default Second Opinion provider is simulated")
            second_opinion_provider = SimulatedSecondOpinionProvider()
            second_opinion_simulated = True
        elif not isinstance(second_opinion_simulated, bool):
            # An injected provider must come with its stated mode; there is no default.
            raise TypeError("second_opinion_simulated must be True or False")
        # The mode is declared once, here. The core service checks it against the provider's
        # own SIMULATED marker and raises if they disagree, so a mismatch fails at construction.
        self._second_opinion = SecondOpinionService(
            second_opinion_provider, simulated=second_opinion_simulated)
        self._second_opinion_simulated = second_opinion_simulated
        self._descriptions = dict(SCENARIOS)
        self._sensor_of = _sensor_for_reading()

    # ------------------------------------------------------------ state
    def state(self):
        return {
            "scenarios": [{"name": n, "description": d} for n, d in SCENARIOS],
            "max_image_bytes": MAX_IMAGE_BYTES,
            "rover_id": self._rover.config.rover_id,
            "history_max": HISTORY_MAX,
            "second_opinion": self._second_opinion_state(),
            "research": {
                "configured": self._research is not None,
                "domains": list(self._research_domains),
                "query_max_chars": getattr(self._research, "max_query_chars", None),
            },
            "research_pro": {
                "configured": self._research_pro is not None,
                "domain": getattr(self._research_pro, "domain", None),
                "model": getattr(self._research_pro, "model", None),
                "query_max_chars": getattr(self._research_pro, "max_query_chars", None),
                "deadline_seconds": getattr(self._research_pro, "deadline_seconds", None),
            },
            "orchestrated": {
                "available": True,
                "fake_providers": True,
                "research_modes": list(RESEARCH_MODES),
                "query_max_chars": QUERY_MAX_CHARS,
            },
            "integrated": {
                "available": True,
                "stages": list(INTEGRATED_STAGES),
                "modes": list(STAGE_MODES),
                "real_available": {"image": self._real_findings is not None,
                                   "research": self._research is not None,
                                   "compute": self._compute_runner is not None,
                                   "synthesis": self._synthesis_provider is not None},
                "query_max_chars": QUERY_MAX_CHARS,
                "max_image_bytes": MAX_IMAGE_BYTES,
            },
            "voice_assistant": {
                "available": True,
                "speech_to_text_configured": self._elevenlabs_client is not None and getattr(self._elevenlabs_client, "key_present", lambda: False)(),
                "text_to_speech_configured": self._elevenlabs_client is not None and getattr(self._elevenlabs_client, "key_present", lambda: False)(),
                "supported_languages": list(SUPPORTED_LANGUAGES),
                "language_names": dict(LANGUAGE_NAMES),
                "lessons_count": len(LESSONS),
            },
        }

    def _second_opinion_state(self):
        """Limits and reading keys come from the Second Opinion core and the Record Core;
        `simulated` is the configured mode that every response must match."""
        return {
            "simulated": self._second_opinion_simulated,
            "max_image_bytes": second_opinion_core.MAX_IMAGE_BYTES,
            "crop_max_chars": second_opinion_core.CROP_MAX_CHARS,
            "language_max_chars": second_opinion_core.LANGUAGE_MAX_CHARS,
            "reading_groups": [{"name": name, "keys": list(keys)}
                               for name, keys in second_opinion_core.READING_GROUPS],
        }

    # -------------------------------------------------------- scenarios
    def run_scenario(self, name, user=None):
        """Run a scenario, store the Record if valid, and build the farmer report for it.

        `user` is None or a dict of the user's own optional statements (crop, region, growth_stage).
        It is validated BEFORE anything runs, so a bad value stores nothing."""
        results, record = self._simulate_record(name, user)
        outcome = self.process_record(record)
        # Computed on the fly from the Record and never stored or added to it. The pipeline has no
        # rules, sources, image model or calibration here, so it reports what is unavailable.
        report = run_pipeline(record, origin="simulated", user=user)
        return {
            "scenario": name,
            "description": self._descriptions[name],
            "steps": [step for step, _ in results],
            "record": record,
            "readings": self._reading_rows(record),
            "photo": self._photo_block(record),
            "validation": {"valid": outcome["valid"], "errors": outcome["errors"]},
            "stored_id": outcome["stored_id"],
            "advice": report["advice"],
            "report": report,
        }

    def _simulate_record(self, name, user):
        """Validate `name` and `user`, run the simulated scenario and return (steps, Record). Shared by the
        legacy route and the orchestrated route; nothing is stored here."""
        if name not in self._descriptions:
            raise DemoError("Unknown scenario.", 400)
        if validate_user(user):
            raise DemoError("The crop, region or growth stage is not valid: use letters, digits, spaces and "
                            "hyphens, up to 64 characters.", 400)
        try:
            results = run_scenario(self._rover, get_scenario(name))
        except MockRoverError as exc:
            raise DemoError("The simulated scenario failed: %s" % (exc,), 500) from None
        finally:
            self._reset_rover()
        record = next((value for step, value in results if step == "build_record"), None)
        if record is None:
            raise DemoError("The scenario did not build a Record.", 500)
        return results, record

    # --------------------------------------------- orchestrated (fake) path
    def run_orchestrated(self, payload):
        """Run a scenario and build its report through `orchestration.pipeline_steps.run_report` with FAKE
        providers only (IM-2). Additive: the legacy `run_scenario` is unchanged and does not call this.

        `payload` is {"scenario": name, optional "crop", "region", "growth_stage", "research": one of
        RESEARCH_MODES (default "skip"), "question": text, needed for the fake research modes}. Everything is
        validated BEFORE the simulator runs, so a refused request stores nothing. The Record is validated and
        stored once (`process_record`); the stored id is passed on as the run's `record_id`. The image stage is
        always skipped (no photo-to-bytes mapping exists) and the origin is "unknown". A failed or skipped stage is
        part of the answer, not an HTTP error."""
        if (not isinstance(payload, dict) or not set(payload) <= set(ORCHESTRATED_FIELDS)
                or not isinstance(payload.get("scenario"), str)):
            raise DemoError('Send JSON like {"scenario": "<name>"} with optional crop, region, growth_stage, '
                            'research and question.', 400)
        mode = payload.get("research", RESEARCH_SKIP)
        if not isinstance(mode, str) or mode not in RESEARCH_MODES:
            raise DemoError("research must be one of: %s." % ", ".join(RESEARCH_MODES), 400)
        question = payload.get("question")
        if mode != RESEARCH_SKIP and not query_is_valid(question):
            raise DemoError("The question is empty, too long or contains characters that are not allowed "
                            "(at most %d characters)." % QUERY_MAX_CHARS, 400)
        user = {}
        for key in ("crop", "region", "growth_stage"):
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise DemoError("%s must be text." % key.replace("_", " ").capitalize(), 400)
            if value.strip():
                user[key] = value
        user = user or None
        results, record = self._simulate_record(payload["scenario"], user)
        outcome = self.process_record(record)   # the only place the Record is stored
        if mode == RESEARCH_FAKE:
            client = self._fake_research if self._fake_research is not None else FakeResearchClient()
        elif mode == RESEARCH_FAKE_ERROR:
            client = FakeResearchClient(error_code=FAKE_RESEARCH_ERROR_CODE)
        else:
            client = None
        run = pipeline_steps.run_report(
            record, findings_provider=None, png_bytes=None, research_client=client,
            query=question if client is not None else None,
            research_skip_reason=SKIP_RESEARCH_NOT_REQUESTED if client is None else None,
            origin="unknown", user=user, record_id=outcome["stored_id"])
        return {
            "workflow": "orchestrated_fake",
            "fake_providers": True,
            "label": ORCHESTRATED_LABEL,
            "data_source": ORCHESTRATED_DATA_SOURCE,
            "scenario": payload["scenario"],
            "description": self._descriptions[payload["scenario"]],
            "steps": [step for step, _ in results],
            "record": record,
            "readings": self._reading_rows(record),
            "photo": self._photo_block(record),
            "validation": {"valid": outcome["valid"], "errors": outcome["errors"]},
            "stored_id": outcome["stored_id"],
            "image_stage": {"status": "skipped", "note": IMAGE_STAGE_NOTE},
            "research_stage": {"mode": mode, "fake": client is not None,
                               "note": "FAKE research client: deterministic made-up results." if client is not None
                               else "Skipped: research was not requested."},
            "orchestration": run,
        }

    # ------------------------------------------------ integrated path
    def _stage_modes(self, requested):
        """The mode of each stage: skip (default), fake, or real. A real stage is refused unless the server was
        started with that provider; nothing falls back to anything else."""
        modes = {stage: STAGE_SKIP for stage in INTEGRATED_STAGES}
        if requested is None:
            return modes
        if not isinstance(requested, dict) or not set(requested) <= set(INTEGRATED_STAGES):
            raise DemoError("providers must be an object with any of: %s." % ", ".join(INTEGRATED_STAGES), 400)
        configured = {"image": self._real_findings is not None, "research": self._research is not None,
                      "compute": self._compute_runner is not None, "synthesis": self._synthesis_provider is not None}
        for stage, mode in requested.items():
            if not isinstance(mode, str) or mode not in STAGE_MODES:
                raise DemoError("The mode of each stage must be one of: %s." % ", ".join(STAGE_MODES), 400)
            if mode == STAGE_REAL and not configured[stage]:
                raise DemoError("The %s stage has no real provider configured on this server; nothing was run and "
                                "there is no fallback." % stage, 400)
            modes[stage] = mode
        return modes

    @staticmethod
    def _png_from_base64(value):
        """Strict base64 decoding with the same size limit as the image analyzer. The PNG signature is checked by
        the image step itself, which does not call the provider for anything else."""
        if not isinstance(value, str) or value == "":
            raise DemoError("image_png_base64 must be a non-empty base64 string.", 400)
        if len(value) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise DemoError("The image is larger than the %d-byte limit." % MAX_IMAGE_BYTES, 400)
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise DemoError("image_png_base64 is not valid base64.", 400) from None
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise DemoError("The image is empty or larger than the %d-byte limit." % MAX_IMAGE_BYTES, 400)
        return data

    def run_integrated(self, payload):
        """Run a scenario and then the integrated workflow (`orchestration.integrated.run_integrated`): image
        findings, research, a compute summary and a synthesis in one run. Additive: no other route calls this.

        `payload` is {"scenario": name, optional "crop", "region", "growth_stage", "question", "image_png_base64",
        "providers": {"image"|"research"|"compute"|"synthesis": "skip"|"fake"|"real"}}. Everything is validated
        BEFORE the simulator runs, so a refused request stores nothing and calls no provider. The Record is stored
        once. The synthesis stage depends on the image and research stages and is skipped if either did not
        complete. A failed or skipped stage is part of the answer, not an HTTP error."""
        if (not isinstance(payload, dict) or not set(payload) <= set(INTEGRATED_FIELDS)
                or not isinstance(payload.get("scenario"), str)):
            raise DemoError('Send JSON like {"scenario": "<name>"} with optional crop, region, growth_stage, question, '
                            'image_png_base64 and providers.', 400)
        modes = self._stage_modes(payload.get("providers"))
        question = payload.get("question")
        if question is not None and not query_is_valid(question):
            raise DemoError("The question is empty, too long or contains characters that are not allowed "
                            "(at most %d characters)." % QUERY_MAX_CHARS, 400)
        if modes["research"] != STAGE_SKIP and question is None:
            raise DemoError("The research stage needs a question.", 400)
        image_value = payload.get("image_png_base64")
        png = None
        if modes["image"] == STAGE_SKIP:
            if image_value is not None:
                raise DemoError("An image was supplied but the image stage is set to skip.", 400)
        else:
            if image_value is None:
                raise DemoError("The image stage needs image_png_base64.", 400)
            png = self._png_from_base64(image_value)
        user = {}
        for key in ("crop", "region", "growth_stage"):
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise DemoError("%s must be text." % key.replace("_", " ").capitalize(), 400)
            if value.strip():
                user[key] = value
        user = user or None
        results, record = self._simulate_record(payload["scenario"], user)
        outcome = self.process_record(record)   # the only place the Record is stored

        findings = {STAGE_FAKE: PromptedFindingsProvider(FakeFindingsAnalyzer()),
                    STAGE_REAL: self._real_findings}.get(modes["image"])
        if modes["research"] == STAGE_FAKE:
            research = self._fake_research if self._fake_research is not None else FakeResearchClient()
        else:
            research = self._research if modes["research"] == STAGE_REAL else None
        compute = {STAGE_FAKE: task_runner.FakeTaskRunner(), STAGE_REAL: self._compute_runner}.get(modes["compute"])
        writer = {STAGE_FAKE: synthesis.FakeSynthesisProvider(), STAGE_REAL: self._synthesis_provider}.get(
            modes["synthesis"])
        observe = self._findings_observer is not None and modes["image"] == STAGE_REAL
        if observe:
            self._findings_observer.reset()
        # The real synthesis provider keeps a closed-word record of its last call (SY-6); clear it so a run that never
        # reaches the provider cannot show an earlier run's record.
        synthesis_source = self._synthesis_provider if modes["synthesis"] == STAGE_REAL else None
        if callable(getattr(synthesis_source, "reset_diagnostic", None)):
            synthesis_source.reset_diagnostic()
        run = integrated.run_integrated(
            record, findings_provider=findings, png_bytes=png, research_client=research,
            query=self._research_query(question, user),
            research_skip_reason=SKIP_RESEARCH_NOT_REQUESTED if research is None else None,
            task_runner=compute, synthesis_provider=writer, origin="unknown", user=user,
            record_id=outcome["stored_id"])
        real_stages = [stage for stage in INTEGRATED_STAGES if modes[stage] == STAGE_REAL]
        response = self._integrated_response(payload, modes, real_stages, record, outcome, results, run)
        if synthesis_source is not None:
            try:
                diagnostic = synthesis.clean_diagnostic(getattr(synthesis_source, "last_diagnostic", None))
            except Exception:
                diagnostic = None
            if diagnostic is not None:   # absent (not invented) when the provider was never called or gave nothing usable
                response["synthesis_diagnostic"] = diagnostic
        if observe:
            try:
                observation = clean_observation(self._findings_observer.snapshot())
            except Exception:
                observation = None
            # Absent (not invented) when the observer gave nothing usable.
            if observation is not None:
                response["findings_observation"] = observation
            # Only for an HTTP 5xx reply: a closed class of the body and its length, never any of its text (IA-8).
            error_class = getattr(self._findings_observer, "error_class", None)
            try:
                classified = clean_error_class(error_class()) if callable(error_class) else None
            except Exception:
                classified = None
            if classified is not None:
                response["findings_error_class"] = classified
            # IA-10: a length and a short SHA-256 prefix of the request body the transport was actually handed (None if
            # no request reached it) and of the decoded image this run used. Computed from this run's own bytes.
            image_print = fingerprint_of(png)
            request_print = getattr(self._findings_observer, "request_fingerprint", None)
            try:
                body_print = request_print() if callable(request_print) else None
            except Exception:
                body_print = None
            body_print = body_print if isinstance(body_print, dict) else {}
            fingerprint = clean_request_fingerprint({
                "body_bytes": body_print.get("bytes"), "body_sha256_prefix": body_print.get("sha256_prefix"),
                "image_bytes": image_print["bytes"] if image_print else None,
                "image_sha256_prefix": image_print["sha256_prefix"] if image_print else None})
            if fingerprint is not None:
                response["findings_request_fingerprint"] = fingerprint
            # IA-11: closed facts about an HTTP success reply (see agri_pipeline/reply_facts.py), only if the observer
            # produced them and they pass the allowlist. Never any text of the reply.
            reply_getter = getattr(self._findings_observer, "reply_facts", None)
            try:
                reply = clean_reply_facts(reply_getter()) if callable(reply_getter) else None
            except Exception:
                reply = None
            if reply is not None:
                response["findings_reply_facts"] = reply
        return response

    @staticmethod
    def _research_query(question, user):
        """The text sent to the research stage and to the synthesis as "the question". It is the question alone unless
        the user typed a crop, region or growth stage; those user-stated values (already validated: letters, digits,
        spaces and hyphens, at most 64 characters each) are then appended as plain labels. Nothing is inferred from the
        image or guessed. If the result would exceed the question limit or fail the question check, the question is
        used unchanged."""
        if question is None or not user:
            return question
        labels = (("crop", "crop"), ("region", "region"), ("growth_stage", "growth stage"))
        parts = ["%s: %s" % (label, user[key].strip()) for key, label in labels if user.get(key)]
        if not parts:
            return question
        text = "%s (%s)" % (question, "; ".join(parts))
        return text if query_is_valid(text) else question

    def _integrated_response(self, payload, modes, real_stages, record, outcome, results, run):
        return {
            "workflow": "integrated",
            "label": INTEGRATED_MIXED_LABEL if real_stages else INTEGRATED_FAKE_LABEL,
            "providers": modes,
            "real_stages": real_stages,
            "data_sent": {stage: INTEGRATED_DATA_SENT[stage] for stage in real_stages},
            "data_source": ORCHESTRATED_DATA_SOURCE,
            "scenario": payload["scenario"],
            "description": self._descriptions[payload["scenario"]],
            "steps": [step for step, _ in results],
            "record": record,
            "readings": self._reading_rows(record),
            "photo": self._photo_block(record),
            "validation": {"valid": outcome["valid"], "errors": outcome["errors"]},
            "stored_id": outcome["stored_id"],
            "integrated": run,
        }

    def _reset_rover(self):
        """Return the simulated rover to a neutral state so scenarios stay independent."""
        rover = self._rover
        for sensor in list(rover.faults):
            rover.clear_fault(sensor)
        rover.stop()
        rover.retract_probe()
        rover.clear_capture()
        rover.set_led(False)

    # ---------------------------------------------------------- records
    def process_record(self, record):
        """Validate a Record with the Record Core and store it only if valid."""
        result = validate_record(record)
        errors = [{"path": e.path, "code": e.code, "message": e.message}
                  for e in result.errors]
        stored_id = None
        if result.valid:
            try:
                stored_id = self._store.add(result.record)
            except StorageError as exc:
                raise DemoError("The Record could not be stored: %s" % (exc,), 500) from None
        return {"valid": result.valid, "errors": errors, "stored_id": stored_id}

    def _reading_rows(self, record):
        rows = []
        for section, keys in (("soil", SOIL_KEYS), ("air", AIR_KEYS),
                              ("battery", BATTERY_KEYS)):
            values = record.get(section) if isinstance(record, dict) else None
            if not isinstance(values, dict):
                continue
            status_map = record.get("sensor_status", {})
            for key in keys:
                sensor = self._sensor_of.get((section, key))
                rows.append({
                    "name": "%s.%s" % (section, key),
                    "value": values.get(key),
                    "unit": READING_UNITS.get((section, key), UNIT_NOT_DEFINED),
                    "sensor": sensor,
                    "status": status_map.get(sensor) if isinstance(status_map, dict) else None,
                })
        return rows

    @staticmethod
    def _photo_block(record):
        if record.get("photo_id") is None:
            return None
        return {
            "photo_id": record["photo_id"],
            "leaf_label": record["leaf_label"],
            "confidence": record["confidence"],
            "note": "These three fields are simulator placeholders (a fixture name and "
                    "preset label). No image exists and nothing was analysed.",
        }

    def history(self, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= HISTORY_MAX:
            raise DemoError("limit must be a whole number from 1 to %d." % HISTORY_MAX, 400)
        stored = self._store.list_records()
        newest_first = list(reversed(stored))[:limit]
        return {
            "total": len(stored),
            "records": [{"id": item.id, "record": item.record} for item in newest_first],
        }

    # --------------------------------------------------- second opinion
    def get_second_opinion(self, payload):
        """Validate a Second Opinion request and return the opinion for the configured mode.

        A request that fails validation is a DemoError (400) and reaches no provider.
        The opinion is not attached to a Record and is not stored. The result must match
        the announced mode; otherwise nothing is returned (the caller gets a generic 500).
        This is a second line of defence: construction already rejects a disagreeing provider.
        """
        try:
            result = self._second_opinion.get_opinion(payload)
        except InvalidRequest as exc:  # its message never contains submitted values
            raise DemoError(str(exc), 400) from None
        if result.simulated is not self._second_opinion_simulated:
            raise RuntimeError("the Second Opinion result does not match the configured mode")
        return {
            "opinion": result.opinion,
            "error_code": result.error_code,
            "simulated": result.simulated,
            "model": result.model,
            "label": SECOND_OPINION_LABEL if result.simulated else SECOND_OPINION_REAL_LABEL,
        }

    # --------------------------------------------------------- research
    ADVISORY_FIELDS = ("question", "crop", "region", "growth_stage")

    def get_advisory(self, payload):
        """Run one bounded, domain-limited research search and return the evidence report.

        `payload` is {"question": text, optional "crop", "region", "growth_stage"}. The result is a list of
        retrieved sources assessed for applicability to what the user stated. It is evidence to read, never
        a recommendation, and nothing is stored. A failed search returns a report whose status is "error"
        with a closed code; there is no fallback to any other provider."""
        if self._research is None:
            raise DemoError("Research is not configured on this server.", 400)
        if not isinstance(payload, dict) or not set(payload) <= set(self.ADVISORY_FIELDS) \
                or not isinstance(payload.get("question"), str):
            raise DemoError('Send JSON like {"question": "<text>"} with optional crop, region and growth_stage.', 400)
        user = {}
        for key in ("crop", "region", "growth_stage"):
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise DemoError("%s must be text." % key.replace("_", " ").capitalize(), 400)
            if value.strip():
                user[key] = value
        if validate_user(user or None):
            raise DemoError("The crop, region or growth stage is not valid: use letters, digits, spaces and "
                            "hyphens, up to 64 characters.", 400)
        result = self._research.search(payload["question"])
        return research_report(result, user or None)

    # ------------------------------------------------------ research pro
    def start_research_pro(self, payload):
        """Start one Research Pro task for the typed question and return at once with a local task id.

        Only the question text is accepted and forwarded: no crop, region, readings, image or file. The result
        is unreviewed AI-generated research, never a recommendation, and nothing is stored."""
        if self._research_pro is None:
            raise DemoError("Research Pro is not configured on this server.", 400)
        if not isinstance(payload, dict) or set(payload) != {"question"} \
                or not isinstance(payload["question"], str):
            raise DemoError('Send JSON like {"question": "<text>"}.', 400)
        return self._research_pro.start(payload["question"])

    def research_pro_status(self, task_id):
        """Return the current state of a local task (one bounded provider check at most)."""
        if self._research_pro is None:
            raise DemoError("Research Pro is not configured on this server.", 400)
        view = self._research_pro.poll(task_id)
        if view is None:
            raise DemoError("That research task is unknown or has expired.", 404)
        return view

    # ----------------------------------------------------------- images
    def analyze_png(self, data, media_type="image/png"):
        request = AnalysisRequest(ImageInput(data, media_type), DEMO_PROMPT)
        result = self._analyzer.analyze(request)
        return {
            "status": result.status,
            "error_code": result.error_code,
            "text": result.text,
            "model": result.model,
            "caveats": [{"code": c.code, "message": c.message} for c in result.caveats],
            "label": "DEMO ONLY - simulated output, not produced by any AI model",
        }

    # ------------------------------------- voice & learning assistant
    def transcribe_speech(self, audio_bytes, content_type, language_code=None):
        """Transcribe speech audio using ElevenLabs or return structured error."""
        if self._elevenlabs_client is None:
            return {
                "status": "error",
                "error_code": "credential_missing",
                "message": "ElevenLabs is not configured on this server. Please type your question.",
            }
        req = TranscriptionRequest(audio_bytes=audio_bytes, content_type=content_type, language_code=language_code)
        res = self._elevenlabs_client.transcribe(req)
        return {
            "status": res.status,
            "text": res.text,
            "language_code": res.language_code,
            "error_code": res.error_code,
            "message": res.message,
        }

    def synthesize_speech(self, text, voice_id=None, language_code=None):
        """Synthesize answer text to speech audio using ElevenLabs."""
        if self._elevenlabs_client is None:
            return {
                "status": "error",
                "error_code": "credential_missing",
                "message": "ElevenLabs text-to-speech is not configured on this server.",
            }
        req = SpeechRequest(text=text, voice_id=voice_id, language_code=language_code)
        res = self._elevenlabs_client.synthesize(req)
        if res.status == "ok" and res.audio_bytes:
            return {
                "status": "ok",
                "audio_base64": base64.b64encode(res.audio_bytes).decode("ascii"),
                "content_type": res.content_type,
            }
        return {
            "status": "error",
            "error_code": res.error_code,
            "message": res.message,
        }

    def ask_assistant(self, question, language="en", include_rover_data=True):
        """Answer an agricultural query using telemetry, knowledge base, and agronomic reasoning."""
        q = AssistantQuestion(text=question, language=language, include_rover_data=include_rover_data)
        ans = self._assistant.ask(q)
        return {
            "status": ans.status,
            "text": ans.text,
            "language": ans.language,
            "rover_context": ans.rover_context,
            "sources": list(ans.sources),
            "advisory_warning": ans.advisory_warning,
            "error_code": ans.error_code,
        }

    def get_learning_lessons(self, language="en"):
        """Get the agricultural lessons catalog with user's completion status."""
        catalog = get_lessons_catalog(language)
        decorated = []
        for item in catalog:
            decorated.append(dict(item, completed=item["id"] in self._completed_lessons))
        return {
            "lessons": decorated,
            "total": len(decorated),
            "completed_count": len(self._completed_lessons),
        }

    def get_learning_lesson(self, lesson_id, language="en"):
        """Get complete details of a specific agricultural lesson."""
        detail = get_lesson_detail(lesson_id, language)
        if detail is None:
            raise DemoError("Lesson '%s' was not found." % lesson_id, 404)
        return dict(detail, completed=lesson_id in self._completed_lessons)

    def update_lesson_progress(self, lesson_id, completed=True):
        """Update completion status of a lesson."""
        valid_ids = {l["id"] for l in LESSONS}
        if lesson_id not in valid_ids:
            raise DemoError("Unknown lesson id '%s'." % lesson_id, 400)
        if completed:
            self._completed_lessons.add(lesson_id)
        else:
            self._completed_lessons.discard(lesson_id)
        return {
            "status": "ok",
            "lesson_id": lesson_id,
            "completed": lesson_id in self._completed_lessons,
            "completed_count": len(self._completed_lessons),
            "total": len(LESSONS),
        }
