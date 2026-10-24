"""Nelavi image-analysis boundary (provider-neutral).

The application-facing side of image understanding. Callers hand over image bytes
and a prompt and get back an `AnalysisResult`; they never see a provider's URL,
credentials, request shape or error vocabulary. A provider adapter implements
`ImageAnalyzer`; the first is in `image_analysis.nvidia`, and a later one can
replace it without changing callers.

This is an application boundary, not a hardware or transport contract. How a
rover photo ever becomes image bytes (W-12, A-12, R-12) is unresolved and nothing
here assumes it. Image Reference V1 is unrelated and is not used. The prompt is
owned by the caller and the returned text is free-form model output: nothing
here parses it or derives a diagnosis, label or confidence from it.

V1 accepts PNG only. The image is checked by media type and by the PNG signature
and is never decoded, resized, converted or written anywhere.

Standard library only. No network, file or environment access happens here.
"""

from dataclasses import dataclass
from typing import Protocol

MEDIA_TYPE_PNG = "image/png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUSES = (STATUS_OK, STATUS_ERROR)

ERROR_CREDENTIAL_MISSING = "credential_missing"
ERROR_CONFIGURATION_INVALID = "configuration_invalid"
ERROR_IMAGE_UNSUPPORTED = "image_unsupported"
ERROR_PROVIDER_REJECTED = "provider_rejected"
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERROR_PROVIDER_UNREACHABLE = "provider_unreachable"
ERROR_RESPONSE_MALFORMED = "response_malformed"
ERROR_RESPONSE_EMPTY = "response_empty"
ERROR_RESPONSE_TOO_LARGE = "response_too_large"
# IA-6: transport diagnostics. `provider_unreachable` stays in the set for compatibility; the
# NVIDIA adapter now reports one of these three instead, chosen by exception class only.
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
ERROR_PROVIDER_CONNECTION_FAILED = "provider_connection_failed"
ERROR_TRANSPORT_FAILED = "transport_failed"
ERROR_CODES = (
    ERROR_CREDENTIAL_MISSING,
    ERROR_CONFIGURATION_INVALID,
    ERROR_IMAGE_UNSUPPORTED,
    ERROR_PROVIDER_REJECTED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_PROVIDER_UNREACHABLE,
    ERROR_RESPONSE_MALFORMED,
    ERROR_RESPONSE_EMPTY,
    ERROR_RESPONSE_TOO_LARGE,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_CONNECTION_FAILED,
    ERROR_TRANSPORT_FAILED,
)


@dataclass(frozen=True)
class Caveat:
    code: str
    message: str


CAVEATS = (
    Caveat(
        "descriptive_text_only",
        "The text is free-form model output. It is not parsed, and no diagnosis, label or "
        "confidence is derived from it.",
    ),
    Caveat(
        "image_source_unresolved",
        "The image bytes come from the caller. How rover photos reach a caller is unresolved "
        "in the frozen sources (W-12, A-12, R-12).",
    ),
)


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    media_type: str


@dataclass(frozen=True)
class AnalysisRequest:
    image: ImageInput
    prompt: str


@dataclass(frozen=True)
class AnalysisResult:
    status: str  # STATUS_OK or STATUS_ERROR
    text: object  # the model text when ok, else None
    error_code: object  # one of ERROR_CODES when error, else None
    model: object  # the model the analyzer was configured to use, or None
    caveats: tuple  # tuple of Caveat


class ImageAnalyzer(Protocol):
    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """Analyze one image. Provider and environment failures are returned as an
        error result, not raised."""


def is_supported_png(image):
    """True only for a PNG ImageInput: PNG media type and the PNG signature.

    The bytes are not decoded or inspected beyond the signature.
    """
    return (
        isinstance(image, ImageInput)
        and image.media_type == MEDIA_TYPE_PNG
        and isinstance(image.data, bytes)
        and len(image.data) > 0
        and image.data.startswith(PNG_SIGNATURE)
    )


def ok_result(text, model):
    return AnalysisResult(STATUS_OK, text, None, model, CAVEATS)


def error_result(error_code, model=None):
    return AnalysisResult(STATUS_ERROR, None, error_code, model, CAVEATS)
