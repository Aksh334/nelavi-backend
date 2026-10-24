"""Voice & Multilingual Learning Assistant package for CropSync."""

from voice_assistant.boundary import (
    LANG_EN,
    LANG_HI,
    LANG_TE,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    AssistantAnswer,
    AssistantQuestion,
    SpeechRequest,
    SpeechResult,
    TranscriptionRequest,
    TranscriptionResult,
)
from voice_assistant.elevenlabs import ElevenLabsClient
from voice_assistant.assistant import CropSyncAssistant
from voice_assistant.lessons import get_lessons_catalog, get_lesson_detail, LESSONS

__all__ = [
    "LANG_EN",
    "LANG_HI",
    "LANG_TE",
    "LANGUAGE_NAMES",
    "SUPPORTED_LANGUAGES",
    "TranscriptionRequest",
    "TranscriptionResult",
    "SpeechRequest",
    "SpeechResult",
    "AssistantQuestion",
    "AssistantAnswer",
    "ElevenLabsClient",
    "CropSyncAssistant",
    "get_lessons_catalog",
    "get_lesson_detail",
    "LESSONS",
]
