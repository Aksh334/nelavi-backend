"""A deterministic SIMULATED second-opinion provider. No AI model is ever run.

It implements the existing `ImageAnalyzer` interface (image_analysis.boundary)
unchanged, so the second-opinion service can be exercised offline. It contacts no
network, reads no credential, touches no hardware and does not use the real NVIDIA
adapter. Its answer is a fixed text that says plainly that nothing was analysed; it
contains no diagnosis, treatment, dose, confidence or claim about the image.

It is kept in its own module so its role is explicit and it cannot be mistaken for a
real provider.
"""

from image_analysis.boundary import (
    ERROR_CONFIGURATION_INVALID,
    ERROR_IMAGE_UNSUPPORTED,
    STATUS_ERROR,
    STATUS_OK,
    AnalysisRequest,
    AnalysisResult,
    Caveat,
    is_supported_png,
)

MODEL_LABEL = "simulated-second-opinion (not an AI model)"

# The same caveat the existing demo analyzer uses (demo_app/demo_analyzer.py): same code,
# same message. A test checks the two stay identical.
SIMULATED_CAVEAT = Caveat(
    "simulated_demo_output",
    "This output is generated locally by a deterministic demonstration routine. "
    "No AI model, NVIDIA, Nebius, Tavily or other provider was used.",
)

OPINION = (
    "SIMULATED RESPONSE: no AI model was run and no plant assessment was performed.\n"
    "Please confirm with your agriculture officer."
)


def _error(code):
    return AnalysisResult(STATUS_ERROR, None, code, MODEL_LABEL, (SIMULATED_CAVEAT,))


class SimulatedSecondOpinionProvider:
    """Returns the same clearly labelled text for every valid request."""

    SIMULATED = True  # read by SecondOpinionService to catch accidental mislabelling

    def analyze(self, request):
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        if not is_supported_png(request.image):
            return _error(ERROR_IMAGE_UNSUPPORTED)
        if not isinstance(request.prompt, str) or request.prompt.strip() == "":
            return _error(ERROR_CONFIGURATION_INVALID)
        return AnalysisResult(STATUS_OK, OPINION, None, MODEL_LABEL, (SIMULATED_CAVEAT,))
