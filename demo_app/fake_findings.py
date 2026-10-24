"""A deterministic FAKE image analyzer for the integrated demo path.

It stands in for a vision model so the integrated workflow can be shown and tested without any service. It makes no
network call, reads no credential, never looks at the image bytes and cannot be configured to reach anything. The
answer it gives is the same made-up findings for every image, in the JSON format `PromptedFindingsProvider` asks a
real model for, and the model label says FAKE. Nothing it returns is an observation of the image.

It is duck-typed on purpose: `analyze(request)` returns the `AnalysisResult` of `image_analysis.boundary`.
"""

import json

from image_analysis.boundary import ok_result

FAKE_MODEL = "FAKE-vision-model (deterministic; no model was run and the image was not looked at)"

_FINDINGS = {
    "crop": {"candidates": [{"name": "fake-crop-a", "evidence": ["fake evidence"], "uncertainty": "high"}]},
    "growth_stage": {"candidate": "fake-stage-x", "evidence": ["fake evidence"], "uncertainty": "high"},
    "symptoms": [{"observation": "FAKE observation made up by the demo", "possible_interpretations": ["fake-cause-a"]}],
    "needs_more": ["FAKE request for a closer photograph"],
}


class FakeFindingsAnalyzer:
    """`analyze(request)` -> an ok AnalysisResult whose text is the fixed fake findings document."""

    def analyze(self, request):
        return ok_result(json.dumps(_FINDINGS), FAKE_MODEL)
