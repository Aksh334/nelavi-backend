"""Nelavi photo reference (Image Reference V1).

A photo-reference and traceability step. It implements the owner-approved
decisions IP-1 to IP-8 and resolves no frozen contract item. It carries the
reference a Record holds to a photo; it never touches a photo itself. There are
no pixels, bytes, files, formats, transport, storage, retrieval or providers.

The one input is a Record (dict) or a StoredRecord-like item (`id`, `record`).
The Record is checked with the existing Record Core validator and nothing is
re-implemented here. A valid Record with a populated leaf triple is
`photo_referenced`; one with the triple all null is `no_photo_reference`; a Record
that does not pass validation is `invalid_record`. "unclear" is an ordinary leaf label, not
a status.

`photo_id`, `time`, `sequence_number` and `versions` are copied unchanged and
stay opaque: nothing here parses, orders, compares, deduplicates or interprets
them. `leaf_label` and `confidence` are passed through as the Record holds them;
only the Record Core range check applies. Every result carries caveats naming
what is still unresolved.

Pure and deterministic: no wall clock, randomness, network, file or database
access, and no simulator dependency. Inputs are never modified.
"""

from dataclasses import dataclass
from types import MappingProxyType

from record.validation import validate_record

STATUS_PHOTO_REFERENCED = "photo_referenced"
STATUS_NO_PHOTO_REFERENCE = "no_photo_reference"
STATUS_INVALID_RECORD = "invalid_record"
STATUSES = (STATUS_PHOTO_REFERENCED, STATUS_NO_PHOTO_REFERENCE, STATUS_INVALID_RECORD)


@dataclass(frozen=True)
class Caveat:
    code: str
    message: str


CAVEATS = (
    Caveat(
        "no_photo_content",
        "No photo content or pixels are available; only the reference held by the Record is carried.",
    ),
    Caveat(
        "photo_id_opaque",
        "photo_id is an opaque string: it is not parsed, and uniqueness is not assumed.",
    ),
    Caveat(
        "photo_relation_unresolved",
        "Which of the three photos taken at a stop photo_id names is unresolved in the frozen sources (R-12).",
    ),
    Caveat(
        "confidence_semantics_unresolved",
        "Beyond the Record Core range check, the meaning and scale of confidence are unresolved "
        "in the frozen sources (R-9, A-10).",
    ),
    Caveat(
        "mock_records_unmarked",
        "The current Record schema does not mark a Record as coming from the mock rover, so a "
        "mock Record cannot be told from any other (R-18).",
    ),
)

CONTEXT_PHOTO_CONTENT_UNAVAILABLE = (
    "Photo content is not part of the Record and is not available to this step."
)


@dataclass(frozen=True)
class ReferenceResult:
    rover_id: object
    record_id: object  # storage id if the input carried one, else None
    sequence_number: object
    time: object
    photo_id: object
    leaf_label: object
    confidence: object
    versions: object  # read-only mapping, or None
    status: str
    errors: tuple  # Record Core ValidationError entries; empty unless invalid_record
    caveats: tuple  # tuple of Caveat
    context: object  # read-only mapping


def _unpack(item):
    """Return (candidate Record, storage id or None). Never invents an id."""
    if isinstance(item, dict):
        return item, None
    if hasattr(item, "record"):
        item_id = getattr(item, "id", None)
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            item_id = None
        return item.record, item_id
    return item, None


def _context():
    return MappingProxyType(
        {"photo_content": "unavailable", "reason": CONTEXT_PHOTO_CONTENT_UNAVAILABLE}
    )


def reference_photo(item):
    """Build a ReferenceResult for one Record or StoredRecord-like item.

    A Record that fails Record Core validation yields `invalid_record`; the
    Record's own fields are then not trusted and are all None, and only the
    supplied storage id (which the caller, not the Record, provides) is kept.
    """
    candidate, record_id = _unpack(item)
    validation = validate_record(candidate)
    if not validation.valid:
        return ReferenceResult(
            rover_id=None,
            record_id=record_id,
            sequence_number=None,
            time=None,
            photo_id=None,
            leaf_label=None,
            confidence=None,
            versions=None,
            status=STATUS_INVALID_RECORD,
            errors=validation.errors,
            caveats=CAVEATS,
            context=_context(),
        )

    record = validation.record  # an independent copy made by the validator
    referenced = record["photo_id"] is not None
    return ReferenceResult(
        rover_id=record["rover_id"],
        record_id=record_id,
        sequence_number=record["sequence_number"],
        time=record["time"],
        photo_id=record["photo_id"],
        leaf_label=record["leaf_label"],
        confidence=record["confidence"],
        versions=MappingProxyType(dict(record["versions"])),
        status=STATUS_PHOTO_REFERENCED if referenced else STATUS_NO_PHOTO_REFERENCE,
        errors=(),
        caveats=CAVEATS,
        context=_context(),
    )
