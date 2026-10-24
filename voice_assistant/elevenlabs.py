"""ElevenLabs client for Speech-to-Text (Scribe) and Text-to-Speech (Multilingual V2).
Uses the standard library urllib transport by default with an injectable transport seam for offline testing.
Never prints, logs, or exposes API keys.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Optional

from voice_assistant.boundary import (
    ALLOWED_AUDIO_TYPES,
    ERROR_AUDIO_EMPTY,
    ERROR_AUDIO_TOO_LARGE,
    ERROR_AUDIO_UNSUPPORTED,
    ERROR_CREDENTIAL_MISSING,
    ERROR_EMPTY_TRANSCRIPT,
    ERROR_PROVIDER_REJECTED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_TIMEOUT,
    ERROR_UNKNOWN,
    LANG_EN,
    LANG_HI,
    LANG_TE,
    MAX_AUDIO_BYTES,
    MAX_TEXT_TO_SPEECH_CHARS,
    SUPPORTED_LANGUAGES,
    SpeechRequest,
    SpeechResult,
    TranscriptionRequest,
    TranscriptionResult,
)

DEFAULT_KEY_ENV = "ELEVENLABS_API_KEY"
FALLBACK_KEY_ENV = "XI_API_KEY"

STT_ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_STT_MODEL = "scribe_v1"

TTS_ENDPOINT_TEMPLATE = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_TTS_MODEL = "eleven_multilingual_v2"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel (multilingual)

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 15 * 1024 * 1024  # 15 MiB


def default_transport(url, headers, body, timeout):
    """Urllib transport with bounded chunked body reading and error handling."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("Response body too large")
            return status, content
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            err_body = exc.read(65536)
        except Exception:
            err_body = b""
        return status, err_body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
            raise TimeoutError("Request timed out") from exc
        raise


def _build_multipart_body(fields, files):
    """Build a multipart/form-data body purely with stdlib without third-party dependencies."""
    boundary = "----ElevenLabsBoundary%s" % uuid.uuid4().hex
    body_parts = []

    for name, value in fields.items():
        if value is None:
            continue
        body_parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode("utf-8")
        )

    for name, (filename, file_bytes, content_type) in files.items():
        header = (
            '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
            % (boundary, name, filename, content_type)
        ).encode("utf-8")
        body_parts.append(header + file_bytes + b"\r\n")

    body_parts.append(('--%s--\r\n' % boundary).encode("utf-8"))
    content = b"".join(body_parts)
    content_type = "multipart/form-data; boundary=%s" % boundary
    return content, content_type


class ElevenLabsClient:
    """ElevenLabs voice services client supporting STT and TTS with strict validation."""

    def __init__(self, api_key=None, key_env_var=DEFAULT_KEY_ENV, transport=None,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
        self._explicit_key = api_key
        self._key_env_var = key_env_var
        self._transport = transport if transport is not None else default_transport
        self._timeout_seconds = float(timeout_seconds)

    def _get_api_key(self):
        if self._explicit_key:
            return self._explicit_key.strip()
        env_val = os.environ.get(self._key_env_var)
        if env_val and env_val.strip():
            return env_val.strip()
        fallback_val = os.environ.get(FALLBACK_KEY_ENV)
        if fallback_val and fallback_val.strip():
            return fallback_val.strip()
        return None

    def key_present(self):
        return bool(self._get_api_key())

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """Convert speech audio to text using ElevenLabs Scribe."""
        if not isinstance(request.audio_bytes, (bytes, bytearray)):
            return TranscriptionResult(status="error", error_code=ERROR_AUDIO_EMPTY,
                                       message="Audio data must be bytes.")
        if len(request.audio_bytes) == 0:
            return TranscriptionResult(status="error", error_code=ERROR_AUDIO_EMPTY,
                                       message="Audio data is empty.")
        if len(request.audio_bytes) > MAX_AUDIO_BYTES:
            return TranscriptionResult(status="error", error_code=ERROR_AUDIO_TOO_LARGE,
                                       message="Audio file exceeds %d bytes limit." % MAX_AUDIO_BYTES)

        media_type = (request.content_type or "").split(";")[0].strip().lower()
        if media_type not in ALLOWED_AUDIO_TYPES:
            return TranscriptionResult(status="error", error_code=ERROR_AUDIO_UNSUPPORTED,
                                       message="Unsupported audio format: %s." % media_type)

        api_key = self._get_api_key()
        if not api_key:
            return TranscriptionResult(status="error", error_code=ERROR_CREDENTIAL_MISSING,
                                       message="ElevenLabs API key is not configured.")

        filename = "recording.webm" if "webm" in media_type else "recording.wav" if "wav" in media_type else "recording.mp3"
        fields = {
            "model_id": DEFAULT_STT_MODEL,
        }
        if request.language_code and request.language_code in SUPPORTED_LANGUAGES:
            fields["language_code"] = request.language_code

        files = {
            "file": (filename, request.audio_bytes, media_type)
        }

        body, content_type_header = _build_multipart_body(fields, files)
        headers = {
            "xi-api-key": api_key,
            "Content-Type": content_type_header,
            "Accept": "application/json",
            "User-Agent": "CropSync-Voice/1.0",
        }

        try:
            status, resp_bytes = self._transport(STT_ENDPOINT, headers, body, self._timeout_seconds)
        except TimeoutError:
            return TranscriptionResult(status="error", error_code=ERROR_TIMEOUT,
                                       message="Transcription request timed out.")
        except Exception:
            return TranscriptionResult(status="error", error_code=ERROR_PROVIDER_UNAVAILABLE,
                                       message="Failed to connect to transcription service.")

        if status == 200:
            try:
                data = json.loads(resp_bytes.decode("utf-8"))
                text = (data.get("text") or "").strip()
                if not text:
                    return TranscriptionResult(status="error", error_code=ERROR_EMPTY_TRANSCRIPT,
                                               message="No speech detected in audio.")
                detected_lang = data.get("language_code") or request.language_code or LANG_EN
                return TranscriptionResult(status="ok", text=text, language_code=detected_lang)
            except (ValueError, UnicodeDecodeError):
                return TranscriptionResult(status="error", error_code=ERROR_UNKNOWN,
                                           message="Malformed response from transcription service.")
        elif status in (400, 401, 403):
            return TranscriptionResult(status="error", error_code=ERROR_PROVIDER_REJECTED,
                                       message="Transcription provider rejected request (HTTP %d)." % status)
        elif status in (408, 429, 500, 502, 503, 504):
            return TranscriptionResult(status="error", error_code=ERROR_PROVIDER_UNAVAILABLE,
                                       message="Transcription provider unavailable (HTTP %d)." % status)
        return TranscriptionResult(status="error", error_code=ERROR_UNKNOWN,
                                   message="Transcription failed with status %d." % status)

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        """Convert answer text to speech audio using ElevenLabs Multilingual V2."""
        text = (request.text or "").strip()
        if not text:
            return SpeechResult(status="error", error_code=ERROR_AUDIO_EMPTY,
                                message="Cannot synthesize speech for empty text.")
        if len(text) > MAX_TEXT_TO_SPEECH_CHARS:
            text = text[:MAX_TEXT_TO_SPEECH_CHARS]

        api_key = self._get_api_key()
        if not api_key:
            return SpeechResult(status="error", error_code=ERROR_CREDENTIAL_MISSING,
                                message="ElevenLabs API key is not configured.")

        voice_id = request.voice_id or DEFAULT_VOICE_ID
        url = TTS_ENDPOINT_TEMPLATE.format(voice_id=voice_id)

        payload = {
            "text": text,
            "model_id": DEFAULT_TTS_MODEL,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            }
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "audio/mpeg",
            "User-Agent": "CropSync-Voice/1.0",
        }

        try:
            status, resp_bytes = self._transport(url, headers, body, self._timeout_seconds)
        except TimeoutError:
            return SpeechResult(status="error", error_code=ERROR_TIMEOUT,
                                message="Speech synthesis request timed out.")
        except Exception:
            return SpeechResult(status="error", error_code=ERROR_PROVIDER_UNAVAILABLE,
                                message="Failed to connect to speech synthesis service.")

        if status == 200:
            if not resp_bytes or len(resp_bytes) < 100:
                return SpeechResult(status="error", error_code=ERROR_PROVIDER_UNAVAILABLE,
                                    message="Empty audio returned from speech provider.")
            return SpeechResult(status="ok", audio_bytes=resp_bytes, content_type="audio/mpeg")
        elif status in (400, 401, 403):
            return SpeechResult(status="error", error_code=ERROR_PROVIDER_REJECTED,
                                message="Speech provider rejected request (HTTP %d)." % status)
        elif status in (408, 429, 500, 502, 503, 504):
            return SpeechResult(status="error", error_code=ERROR_PROVIDER_UNAVAILABLE,
                                message="Speech provider unavailable (HTTP %d)." % status)
        return SpeechResult(status="error", error_code=ERROR_UNKNOWN,
                            message="Speech synthesis failed with status %d." % status)
