"""A deterministic FAKE research client for the orchestrated demo path (IM-2).

It stands in for a search provider so the orchestration layer can be shown end to end without any service. It
makes no network call, reads no credential and cannot be configured to reach anything. Everything it returns is
made up and labelled FAKE: the titles, the `.invalid` web addresses (a reserved name that can never resolve) and
the caveat. Nothing it returns is a source, a measurement or advice.

It is duck-typed on purpose: `search(query)` returns an object with the same attributes as
`research.tavily.ResearchResult` (status, error_code, query, items, dropped, retrieved_at, kind, caveats), which is
all `orchestration.pipeline_steps` reads. This module therefore imports no provider module. Whether the real
search adapter's output has this shape is shown by the existing adapter tests, not by this module.
"""

from dataclasses import dataclass

# The same limit the real search adapter applies to a question (research.tavily.MAX_QUERY_CHARS). A test keeps the
# two numbers equal; this module does not import the adapter.
QUERY_MAX_CHARS = 200

FAKE_KIND = "research_evidence_only"
FAKE_RETRIEVED_AT = "1970-01-01T00:00:00+00:00"   # a fixed, obviously synthetic time: the output is deterministic
FAKE_CAVEATS = (
    ("fake_provider_output",
     "FAKE: these results were made up by the demo's fake research client. No search was made and no real "
     "source was read."),
    ("not_a_recommendation",
     "These are not agronomic recommendations. Nothing here is a rule, threshold or dose."),
)
# The closed error codes of the real search adapter that the fake can be told to return. `provider_unreachable` is no
# longer produced by the real adapter (it reports the timeout, connection and transport codes instead), so it is not here.
_ERROR_CODES = ("provider_unavailable", "provider_timeout", "provider_connection_failed", "transport_failed",
                "rate_limited", "provider_rejected", "response_malformed")
DEFAULT_ERROR_CODE = "provider_unavailable"

_FAKE_ITEMS = (
    ("FAKE SOURCE 1 - not a real document", "https://fake-source.invalid/item-1",
     "FAKE text made up by the demo. It says nothing about any crop, soil or reading."),
    ("FAKE SOURCE 2 - not a real document", "https://fake-source.invalid/item-2",
     "FAKE text made up by the demo. It is here only to show how evidence entries are listed."),
)


@dataclass(frozen=True)
class FakeItem:
    title: str
    url: str
    snippet: str
    published_date: object
    score: object


@dataclass(frozen=True)
class FakeResult:
    status: str
    error_code: object
    query: object
    items: tuple
    dropped: int
    retrieved_at: object
    kind: str = FAKE_KIND
    caveats: tuple = FAKE_CAVEATS


def query_is_valid(query):
    """True for a non-blank, printable text of at most QUERY_MAX_CHARS characters."""
    return (isinstance(query, str) and query.strip() != "" and len(query) <= QUERY_MAX_CHARS
            and query.isprintable())


class FakeResearchClient:
    """`search(query)` -> a FakeResult. With `error_code` it always fails with that closed code."""

    def __init__(self, error_code=None):
        if error_code is not None and error_code not in _ERROR_CODES:
            raise ValueError("error_code must be None or one of the fake client's closed codes")
        self._error_code = error_code

    def search(self, query):
        if not query_is_valid(query):
            return FakeResult("error", "query_invalid", None, (), 0, None, caveats=())
        if self._error_code is not None:
            return FakeResult("error", self._error_code, query, (), 0, None, caveats=())
        items = tuple(FakeItem(title, url, snippet, None, None) for title, url, snippet in _FAKE_ITEMS)
        return FakeResult("ok", None, query, items, 0, FAKE_RETRIEVED_AT)
