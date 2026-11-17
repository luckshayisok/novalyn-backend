"""Rule-based reader: the development adapter.

It finds labelled values ("Expiry date: 31/03/2027", "Policy No. ABC-123")
with regular expressions. It needs no credentials and makes no network calls,
so the whole pipeline works locally. It is far less capable than a language
model, and its confidence scores are capped to say so.
"""

import re

from app.integrations.ai.base import ProviderInput, ProviderResult, RawField

MAX_CONFIDENCE = 0.6

_MONTHS = (
    "jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
    "sep|sept|september|oct|october|nov|november|dec|december"
)
DATE_PATTERN = (
    r"(\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?[ -](?:{_MONTHS})\.?,?[ -]\d{{4}}"
    rf"|(?:{_MONTHS})\.? \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}})"
)

_EXPIRY_LABELS = (
    r"expiry date|date of expiry|expiration date|expires on|expires|expiry|valid until|valid till|"
    r"valid thru|valid through|valid to|renewal date|due for renewal|end date|period to|to date"
)
_ISSUE_LABELS = (
    r"date of issue|issue date|issued on|date issued|issued|valid from|effective date|effective from|"
    r"start date|commencement date|period from|from date"
)
_NUMBER_LABELS = r"certificate|licen[cs]e|policy|permit|registration|reference|document|serial|cert|reg|ref"

_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("insurance_policy", r"\b(insurance|insured|policy holder|policyholder|underwriter|premium)\b"),
    ("equipment_inspection", r"\b(inspection|inspected|thorough examination|loler|pressure vessel)\b"),
    ("safety_certificate", r"\b(fire safety|safety certificate|fire certificate|health and safety|hse)\b"),
    ("maintenance_contract", r"\b(maintenance (contract|agreement)|service agreement|annual maintenance)\b"),
    ("supplier_certification", r"\b(iso \d{4,5}|supplier|vendor approval|accredited)\b"),
    ("permit", r"\bpermit\b"),
    ("business_license", r"\b(trade licen[cs]e|business licen[cs]e|licen[cs]e to (operate|trade))\b"),
    (
        "employee_certification",
        r"\b(has successfully completed|training|certificate of competence|qualified)\b",
    ),
]
_TITLE_WORDS = re.compile(r"\b(certificate|licen[cs]e|policy|permit|agreement|contract|registration)\b", re.I)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;-")


def _found(
    value: str | None, confidence: float, evidence: str | None = None, iso: str | None = None
) -> RawField:
    return RawField(value=value, confidence=min(confidence, MAX_CONFIDENCE), evidence=evidence, iso=iso)


def _labelled_date(text: str, labels: str) -> RawField | None:
    match = re.search(rf"(?:{labels})\s*[:\-]?\s*(?:on\s+)?{DATE_PATTERN}", text, re.I)
    if match:
        return _found(match.group(1), 0.6, evidence=_clean(match.group(0))[:200])
    return None


def _labelled_text(text: str, labels: str) -> RawField | None:
    match = re.search(rf"(?:{labels})\s*[:\-]\s*([^\n]{{2,120}})", text, re.I)
    if match:
        value = _clean(match.group(1))
        if value:
            return _found(value, 0.55, evidence=_clean(match.group(0))[:200])
    return None


class HeuristicProvider:
    name = "heuristic"
    label = "Rule-based reader (development)"

    def can_read_file(self, content_type: str, size_bytes: int) -> bool:
        return False  # text only

    def extract(self, data: ProviderInput) -> ProviderResult:
        text = data.text or ""
        fields: dict[str, RawField] = {}

        if expiry := _labelled_date(text, _EXPIRY_LABELS):
            fields["expiry_date"] = expiry
        if issue := _labelled_date(text, _ISSUE_LABELS):
            fields["issue_date"] = issue

        number = re.search(
            rf"\b(?:{_NUMBER_LABELS})\s*(?:no\.?|number|num|#|id)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-.]{{2,40}})",
            text,
            re.I,
        )
        if number:
            fields["document_number"] = _found(
                number.group(1).strip("."), 0.6, evidence=_clean(number.group(0))
            )
        elif code := re.search(r"\b([A-Z]{1,5}-\d{3,10}(?:-[A-Z0-9]+)?)\b", text):
            fields["document_number"] = _found(code.group(1), 0.35, evidence=code.group(0))

        if issuer := _labelled_text(
            text, r"issued by|issuing authority|issuing body|issuer|insurer|underwriter"
        ):
            fields["issuing_authority"] = issuer
        if holder := _labelled_text(
            text, r"issued to|holder|name of holder|policy ?holder|insured|licensee|permit holder|awarded to"
        ):
            fields["holder_name"] = holder
        elif certify := re.search(r"(?:certify|certifies) that\s+([A-Z][^\n,]{2,80})", text):
            fields["holder_name"] = _found(_clean(certify.group(1)), 0.4, evidence=_clean(certify.group(0)))

        lowered = text.lower()
        for category, pattern in _CATEGORY_KEYWORDS:
            if category in data.categories and re.search(pattern, lowered):
                fields["category"] = _found(category, 0.5)
                break

        for line in text.splitlines()[:15]:
            title = _clean(line)
            if 4 <= len(title) <= 120 and _TITLE_WORDS.search(title):
                # Keep the title part when the line also carries a number or other text.
                fields["name"] = _found(title, 0.45)
                break

        return ProviderResult(fields=fields, provider=self.name)
