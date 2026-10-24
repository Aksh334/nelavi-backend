"""Deterministic, offline, DEMO-ONLY image analyzer.

It implements the existing `ImageAnalyzer` interface (image_analysis.boundary) so the
demo can show the shape of an analysis result, but it is NOT an AI model. It never
contacts any service, uses no credential, and says nothing about the plant. Its text
is built only from facts about the uploaded file itself (size, PNG header
dimensions, a short hash), and every result is labelled as simulated.

Same input always gives the same output. No confidence score and no diagnosis is
ever produced.
"""

import hashlib
import struct

from image_analysis.boundary import (
    CAVEATS,
    ERROR_CONFIGURATION_INVALID,
    ERROR_IMAGE_UNSUPPORTED,
    STATUS_ERROR,
    STATUS_OK,
    AnalysisRequest,
    AnalysisResult,
    Caveat,
    is_supported_png,
)

DEMO_MODEL_LABEL = "demo-analyzer (simulated; not an AI model)"
DEMO_PROMPT = "Demonstration only: no AI model is used and nothing is sent anywhere."

# A local limit for the demo's upload. It matches the NVIDIA adapter's default local
# cap (1 MiB) on purpose, but it is this module's own constant and not an NVIDIA limit.
MAX_IMAGE_BYTES = 1024 * 1024

DEMO_CAVEATS = (
    Caveat(
        "simulated_demo_output",
        "This output is generated locally by a deterministic demonstration routine. "
        "No AI model, NVIDIA, Nebius, Tavily or other provider was used.",
    ),
    Caveat(
        "not_an_assessment",
        "The text describes only the uploaded file's size and header. It says nothing "
        "about the plant and is not a diagnosis, label, confidence or recommendation.",
    ),
) + tuple(c for c in CAVEATS if c.code == "image_source_unresolved")


def _png_dimensions(data):
    """(width, height) from a well-formed IHDR chunk, else None. Nothing is decoded."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    if struct.unpack(">I", data[8:12])[0] != 13:
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width == 0 or height == 0:
        return None
    return width, height


def _error(code):
    return AnalysisResult(STATUS_ERROR, None, code, DEMO_MODEL_LABEL, DEMO_CAVEATS)


class DemoImageAnalyzer:
    """ImageAnalyzer that returns clearly labelled, simulated output."""

    def __init__(self, max_image_bytes=MAX_IMAGE_BYTES):
        if (isinstance(max_image_bytes, bool) or not isinstance(max_image_bytes, int)
                or max_image_bytes <= 0):
            raise ValueError("max_image_bytes must be a positive integer")
        self._max_image_bytes = max_image_bytes

    def analyze(self, request):
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        if not is_supported_png(request.image):
            return _error(ERROR_IMAGE_UNSUPPORTED)
        data = request.image.data
        if len(data) > self._max_image_bytes:
            return _error(ERROR_IMAGE_UNSUPPORTED)
        if not isinstance(request.prompt, str) or request.prompt.strip() == "":
            return _error(ERROR_CONFIGURATION_INVALID)

        dimensions = _png_dimensions(data)
        if dimensions is None:
            size_text = "pixel size not readable from its header"
        else:
            size_text = "%d x %d pixels according to its header" % dimensions
        digest = hashlib.sha256(data).hexdigest()[:16]
        text = (
            "DEMO ONLY - SIMULATED OUTPUT. No AI model was run and nothing was sent to "
            "any service. This text is generated locally from the file's own metadata. "
            "Received a PNG of %d bytes (%s), SHA-256 starting %s. "
            "This is not an analysis of the image content and not a plant-health "
            "assessment." % (len(data), size_text, digest)
        )
        return AnalysisResult(STATUS_OK, text, None, DEMO_MODEL_LABEL, DEMO_CAVEATS)
