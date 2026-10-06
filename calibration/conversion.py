"""Unit conversion for NPK values.

Source: docs/cropsync_coding_overview.docx, section 8: "kg/ha = mg/kg x 2.24".
"""

MG_PER_KG_TO_KG_PER_HA = 2.24


def mg_per_kg_to_kg_per_ha(mg_per_kg):
    """Return kg/ha for a value given in mg/kg (kg/ha = mg/kg x 2.24)."""
    return mg_per_kg * MG_PER_KG_TO_KG_PER_HA
