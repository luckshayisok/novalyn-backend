"""The interface every document-reading provider implements.

A provider receives the text of a document (and, when it can read files
itself, the file) and returns what it found. It never decides anything:
the results are suggestions that a person reviews before they change a
document.
"""

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """A provider could not read the document. The message is shown to the user."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


# The facts a provider is asked for. Keys match document fields where one exists.
FIELD_KEYS = (
    "name",
    "category",
    "document_number",
    "issuing_authority",
    "holder_name",
    "issue_date",
    "expiry_date",
    "organization_mentioned",
)
DATE_KEYS = ("issue_date", "expiry_date")


@dataclass(frozen=True)
class RawField:
    """One value as the provider reported it, before validation."""

    value: str | None
    confidence: float
    evidence: str | None = None
    # For dates: the provider's reading as YYYY-MM-DD. `value` holds the text as written.
    iso: str | None = None


@dataclass(frozen=True)
class ProviderInput:
    text: str | None
    file: bytes | None
    content_type: str
    filename: str
    categories: dict[str, str]


@dataclass
class ProviderResult:
    fields: dict[str, RawField]
    provider: str
    model: str | None = None
    notes: list[str] = field(default_factory=list)


class AIProvider(Protocol):
    name: str
    label: str

    def can_read_file(self, content_type: str, size_bytes: int) -> bool:
        """Whether the provider can read this file directly (scans, photos)."""

    def extract(self, data: ProviderInput) -> ProviderResult: ...
