"""Voice assistant boundary: data contracts, error codes, and bounds for voice & learning features.
No network or provider-specific code here.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

# Supported Languages
LANG_EN = "en"
LANG_HI = "hi"
LANG_TE = "te"
SUPPORTED_LANGUAGES = (LANG_EN, LANG_HI, LANG_TE)

LANGUAGE_NAMES = {
    LANG_EN: "English",
    LANG_HI: "Hindi (हिन्दी)",
    LANG_TE: "Telugu (తెలుగు)",
}

# Bounds
MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_QUESTION_CHARS = 1000
MAX_ANSWER_CHARS = 4000
MAX_TEXT_TO_SPEECH_CHARS = 2000

# Allowed audio formats for upload
ALLOWED_AUDIO_TYPES = (
    "audio/webm",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
)

# Closed Error Codes
ERROR_CREDENTIAL_MISSING = "credential_missing"
ERROR_AUDIO_TOO_LARGE = "audio_too_large"
ERROR_AUDIO_UNSUPPORTED = "audio_unsupported"
ERROR_AUDIO_EMPTY = "audio_empty"
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERROR_PROVIDER_REJECTED = "provider_rejected"
ERROR_TIMEOUT = "timeout"
ERROR_EMPTY_TRANSCRIPT = "empty_transcript"
ERROR_INVALID_LANGUAGE = "invalid_language"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_UNKNOWN = "unknown_error"

CLOSED_ERROR_CODES = (
    ERROR_CREDENTIAL_MISSING,
    ERROR_AUDIO_TOO_LARGE,
    ERROR_AUDIO_UNSUPPORTED,
    ERROR_AUDIO_EMPTY,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_PROVIDER_REJECTED,
    ERROR_TIMEOUT,
    ERROR_EMPTY_TRANSCRIPT,
    ERROR_INVALID_LANGUAGE,
    ERROR_INVALID_REQUEST,
    ERROR_UNKNOWN,
)


@dataclass(frozen=True)
class TranscriptionRequest:
    audio_bytes: bytes
    content_type: str
    language_code: Optional[str] = None


@dataclass(frozen=True)
class TranscriptionResult:
    status: str  # "ok" | "error"
    text: Optional[str] = None
    language_code: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: Optional[str] = None
    language_code: Optional[str] = None


@dataclass(frozen=True)
class SpeechResult:
    status: str  # "ok" | "error"
    audio_bytes: Optional[bytes] = None
    content_type: str = "audio/mpeg"
    error_code: Optional[str] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class AssistantQuestion:
    text: str
    language: str = LANG_EN
    include_rover_data: bool = True


@dataclass(frozen=True)
class AssistantAnswer:
    text: str
    language: str
    rover_context: Optional[dict] = None
    sources: Tuple[str, ...] = ()
    advisory_warning: Optional[str] = None
    status: str = "ok"
    error_code: Optional[str] = None
