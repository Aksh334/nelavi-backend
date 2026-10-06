"""NPK probe correction.

Source: docs/cropsync_coding_overview.docx, section 8:
"Lab = a*Probe + c*Moisture + b".

The coefficients a, b and c are supplied by the caller. No values are defined
here, because they come from fitting to lab samples, which is not part of this
module.
"""


def correct_npk(probe, moisture, a, b, c):
    """Return Lab = a * probe + c * moisture + b."""
    return a * probe + c * moisture + b
