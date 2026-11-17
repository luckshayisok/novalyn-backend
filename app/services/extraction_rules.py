"""Deterministic checks applied to whatever a provider reports.

Date parsing, ambiguity detection, date-order checks and the decision whether
a person must look closely are all done here in plain code, not by the model.
"""

import re
from dataclasses import dataclass
from datetime import date

from app.integrations.ai.base import DATE_KEYS, RawField

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip

AMBIGUOUS_CONFIDENCE = 0.5
MIN_YEAR, MAX_YEAR = 1950, 2100


@dataclass(frozen=True)
class ParsedDate:
    value: date | None
    ambiguous: bool = False
    alternative: date | None = None


def _make(year: int, month: int, day: int) -> date | None:
    if year < 100:
        year += 2000
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str | None) -> ParsedDate:
    """Read a date as written on a document.

    Numeric dates where both of the first two parts could be the month
    (03/04/2026) are ambiguous: the day-first reading is returned with the
    month-first reading as the alternative.
    """
    if not text:
        return ParsedDate(None)
    raw = text.strip().lower().replace(",", " ")
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw)
    raw = re.sub(r"\s+", " ", raw)

    if match := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw):
        return ParsedDate(_make(int(match[1]), int(match[2]), int(match[3])))

    if match := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", raw):
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        day_first = _make(year, second, first)
        month_first = _make(year, first, second)
        if day_first and month_first and first != second:
            return ParsedDate(day_first, ambiguous=True, alternative=month_first)
        return ParsedDate(day_first or month_first)

    if match := re.fullmatch(r"(\d{1,2})[ -]([a-z]+)\.?[ -](\d{4})", raw):
        month = _MONTHS.get(match[2])
        return ParsedDate(_make(int(match[3]), month, int(match[1])) if month else None)

    if match := re.fullmatch(r"([a-z]+)\.? (\d{1,2}) (\d{4})", raw):
        month = _MONTHS.get(match[1])
        return ParsedDate(_make(int(match[3]), month, int(match[2])) if month else None)

    return ParsedDate(None)


def parse_iso(text: str | None) -> date | None:
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text.strip())
    return _make(int(match[1]), int(match[2]), int(match[3])) if match else None


def _readable(day: date) -> str:
    return day.strftime("%d %b %Y").lstrip("0")


@dataclass
class CheckedField:
    value: str | None
    confidence: float
    evidence: str | None = None
    note: str | None = None
    raw: str | None = None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 2),
            "evidence": self.evidence,
            "note": self.note,
            "raw": self.raw,
        }


def check_date(raw: RawField) -> CheckedField:
    """Settle a date: the document's own wording wins over the provider's reading."""
    written = parse_date(raw.value)
    reading = parse_iso(raw.iso)
    confidence = raw.confidence

    if written.value and not written.ambiguous:
        note = None
        if reading and reading != written.value:
            note = f"The reader suggested {_readable(reading)}; the text says {_readable(written.value)}."
            confidence = min(confidence, AMBIGUOUS_CONFIDENCE)
        return CheckedField(written.value.isoformat(), confidence, raw.evidence, note, raw.value)

    if written.ambiguous and written.value and written.alternative:
        chosen = reading if reading in (written.value, written.alternative) else written.value
        other = written.alternative if chosen == written.value else written.value
        return CheckedField(
            chosen.isoformat(),
            min(confidence, AMBIGUOUS_CONFIDENCE),
            raw.evidence,
            f'"{raw.value}" could mean {_readable(chosen)} or {_readable(other)}. Check which one is right.',
            raw.value,
        )

    if reading:
        return CheckedField(
            reading.isoformat(),
            min(confidence, AMBIGUOUS_CONFIDENCE),
            raw.evidence,
            f'The date "{raw.value}" was not in a familiar format. Check it against the file.',
            raw.value,
        )
    return CheckedField(None, 0.0, raw.evidence, f'"{raw.value}" could not be read as a date.', raw.value)


@dataclass
class CheckOutcome:
    fields: dict[str, CheckedField]
    warnings: list[str]
    needs_review: bool


def check_fields(
    raw_fields: dict[str, RawField],
    *,
    categories: dict[str, str],
    today: date,
    threshold: float,
) -> CheckOutcome:
    fields: dict[str, CheckedField] = {}
    warnings: list[str] = []

    for key, raw in raw_fields.items():
        if key in DATE_KEYS:
            fields[key] = check_date(raw)
        elif key == "category":
            if raw.value in categories:
                fields[key] = CheckedField(raw.value, raw.confidence, raw.evidence)
        else:
            fields[key] = CheckedField(raw.value, raw.confidence, raw.evidence)

    issue = parse_iso(fields["issue_date"].value) if "issue_date" in fields else None
    expiry = parse_iso(fields["expiry_date"].value) if "expiry_date" in fields else None

    if expiry is None:
        warnings.append("No expiry date was found. Check whether this document expires.")
    elif expiry < today:
        warnings.append(f"The expiry date found ({_readable(expiry)}) is in the past.")
    if issue and expiry and expiry < issue:
        warnings.append("The expiry date found is before the issue date.")
    if issue and issue > today:
        warnings.append(f"The issue date found ({_readable(issue)}) is in the future.")
    for key in DATE_KEYS:
        if key in fields and fields[key].note:
            warnings.append(fields[key].note)

    low_confidence = any(item.value is not None and item.confidence < threshold for item in fields.values())
    return CheckOutcome(fields, warnings, needs_review=bool(warnings) or low_confidence or not fields)
