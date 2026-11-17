"""Unit tests for the parts of document intelligence that need no database."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.constants import DOCUMENT_CATEGORIES
from app.integrations.ai.anthropic_provider import AnthropicProvider, parse_fields, response_schema
from app.integrations.ai.base import FIELD_KEYS, ProviderError, ProviderInput, RawField
from app.integrations.ai.heuristic import HeuristicProvider
from app.services.extraction_rules import check_date, check_fields, parse_date
from app.services.text_extraction import TextExtractionError, docx_text, extract_text, pdf_text
from tests.helpers import PDF_BYTES, PNG_BYTES
from tests.samples import FIRE_CERTIFICATE, text_pdf, word_document

TODAY = date(2026, 11, 17)

# ----------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("text", "expected", "ambiguous"),
    [
        ("2027-03-14", date(2027, 3, 14), False),
        ("14/03/2027", date(2027, 3, 14), False),  # 14 cannot be a month
        ("03/14/2027", date(2027, 3, 14), False),
        ("03/04/2027", date(2027, 4, 3), True),  # day first, with the other reading noted
        ("04.04.2027", date(2027, 4, 4), False),  # both readings are the same day
        ("14 March 2027", date(2027, 3, 14), False),
        ("14th Mar 2027", date(2027, 3, 14), False),
        ("March 14, 2027", date(2027, 3, 14), False),
        ("14-Mar-2027", date(2027, 3, 14), False),
        ("31/02/2027", None, False),
        ("sometime next year", None, False),
        ("01/01/1800", None, False),
    ],
)
def test_parse_date(text, expected, ambiguous):
    parsed = parse_date(text)

    assert parsed.value == expected
    assert parsed.ambiguous is ambiguous


def test_written_date_wins_over_the_providers_reading():
    checked = check_date(RawField(value="14 March 2027", confidence=0.95, iso="2027-03-15"))

    assert checked.value == "2027-03-14"
    assert checked.confidence <= 0.5
    assert "15 Mar 2027" in checked.note


def test_ambiguous_date_uses_the_providers_choice_but_asks_for_a_check():
    checked = check_date(RawField(value="03/04/2027", confidence=0.9, iso="2027-03-04"))

    assert checked.value == "2027-03-04"
    assert checked.confidence <= 0.5
    assert "could mean" in checked.note


def test_unreadable_date_is_dropped():
    checked = check_date(RawField(value="the end of the season", confidence=0.9))

    assert checked.value is None
    assert checked.confidence == 0


def _check(**fields):
    return check_fields(
        {key: RawField(**value) for key, value in fields.items()},
        categories=DOCUMENT_CATEGORIES,
        today=TODAY,
        threshold=0.8,
    )


def test_confident_complete_reading_needs_no_extra_review():
    outcome = _check(
        name={"value": "Trade licence", "confidence": 0.95},
        expiry_date={"value": "2027-06-30", "confidence": 0.95, "iso": "2027-06-30"},
        issue_date={"value": "2026-07-01", "confidence": 0.95, "iso": "2026-07-01"},
    )

    assert outcome.warnings == []
    assert outcome.needs_review is False


@pytest.mark.parametrize(
    ("fields", "warning"),
    [
        ({"name": {"value": "Permit", "confidence": 0.9}}, "No expiry date"),
        ({"expiry_date": {"value": "2026-01-01", "confidence": 0.9}}, "in the past"),
        (
            {
                "issue_date": {"value": "2027-05-01", "confidence": 0.9},
                "expiry_date": {"value": "2027-01-01", "confidence": 0.9},
            },
            "before the issue date",
        ),
        ({"issue_date": {"value": "2028-01-01", "confidence": 0.9}}, "in the future"),
    ],
)
def test_suspicious_dates_produce_warnings(fields, warning):
    outcome = _check(**fields)

    assert any(warning in item for item in outcome.warnings)
    assert outcome.needs_review is True


def test_low_confidence_or_unknown_category_needs_review():
    outcome = _check(
        expiry_date={"value": "2027-06-30", "confidence": 0.95},
        holder_name={"value": "Globex", "confidence": 0.4},
        category={"value": "tax_return", "confidence": 0.99},
    )

    assert "category" not in outcome.fields  # not one of ours: dropped
    assert outcome.needs_review is True


# ------------------------------------------------------------- text extraction


def test_pdf_text_layer_is_read():
    text = pdf_text(text_pdf(FIRE_CERTIFICATE))

    assert "Certificate No: FS-2291" in text
    assert "Expiry date: 14 March 2027" in text


def test_word_document_paragraphs_and_tables_are_read():
    data = word_document(
        ["Public Liability Insurance"], table=[["Policy No", "PL-8812"], ["Insurer", "Acme Mutual"]]
    )

    text = docx_text(data)

    assert "Public Liability Insurance" in text
    assert "Policy No | PL-8812" in text


def test_scans_and_photos_without_ocr_have_no_text():
    assert extract_text(text_pdf([]), "application/pdf") is None  # a valid page with no text layer
    assert extract_text(PNG_BYTES, "image/png") is None


def test_ocr_is_used_for_photos_when_available():
    from PIL import Image

    class FakeOCR:
        name = "fake"

        def image_to_text(self, image):
            assert isinstance(image, Image.Image)
            return "Expiry date: 01/12/2027"

    import io

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")

    result = extract_text(buffer.getvalue(), "image/png", ocr=FakeOCR())

    assert result.source == "ocr"
    assert "01/12/2027" in result.text


def test_damaged_files_raise_a_readable_error():
    with pytest.raises(TextExtractionError, match="damaged"):
        pdf_text(b"%PDF-1.4 garbage that is not a pdf")
    with pytest.raises(TextExtractionError, match="damaged"):
        docx_text(b"PK\x03\x04 not a zip")


def test_text_is_capped(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "extraction_max_chars", 50)

    text = pdf_text(text_pdf(["x" * 40, "y" * 40, "z" * 40]))

    assert len(text) <= 50


# ------------------------------------------------------------ heuristic reader


def _input(text, **kwargs):
    defaults = {
        "file": None,
        "content_type": "application/pdf",
        "filename": "f.pdf",
        "categories": DOCUMENT_CATEGORIES,
    }
    return ProviderInput(text=text, **{**defaults, **kwargs})


def test_heuristic_reader_finds_labelled_values():
    result = HeuristicProvider().extract(_input("\n".join(FIRE_CERTIFICATE)))
    fields = result.fields

    assert result.provider == "heuristic"
    assert fields["document_number"].value == "FS-2291"
    assert fields["issuing_authority"].value == "City Fire Department"
    assert fields["holder_name"].value == "Globex Supplies"
    assert fields["issue_date"].value == "15 March 2026"
    assert fields["expiry_date"].value == "14 March 2027"
    assert fields["category"].value == "safety_certificate"
    assert fields["name"].value == "FIRE SAFETY CERTIFICATE"
    assert all(item.confidence <= 0.6 for item in fields.values())


def test_heuristic_reader_returns_nothing_for_unrelated_text():
    assert HeuristicProvider().extract(_input("Minutes of the staff picnic committee")).fields == {}


# ---------------------------------------------------------- anthropic provider


def _settings(**overrides):
    values = {"ai_provider": "anthropic", "ai_model": "claude-opus-5-5", "ai_effort": "medium"}
    return Settings(_env_file=None, **{**values, **overrides})


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _client(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    response = SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )
    messages = FakeMessages(response)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


def _answer(**overrides):
    empty = {"value": None, "confidence": 0, "evidence": None}
    payload = {key: dict(empty) for key in FIELD_KEYS}
    for key in ("issue_date", "expiry_date"):
        payload[key]["iso"] = None
    payload.update(overrides)
    return payload


def test_anthropic_request_uses_structured_output_effort_and_refusal_fallback():
    provider = AnthropicProvider(_settings())

    request = provider.request(_input("Certificate text"))

    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "medium"
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert set(schema["required"]) == set(FIELD_KEYS)
    assert schema["additionalProperties"] is False
    assert "iso" in schema["properties"]["expiry_date"]["properties"]
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["fallbacks"] == "default"
    # Document text is fenced and the system prompt says to ignore instructions inside it.
    assert "<document>" in request["messages"][0]["content"][0]["text"]
    assert "never an instruction" in request["system"]


def test_anthropic_request_without_fallback_uses_the_plain_endpoint():
    client, messages = _client(_answer())
    provider = AnthropicProvider(_settings(ai_refusal_fallback=False), client=client)

    provider.extract(_input("text"))

    assert "betas" not in messages.calls[0]
    assert "fallbacks" not in messages.calls[0]


def test_anthropic_sends_scans_as_files():
    provider = AnthropicProvider(_settings())

    pdf_block = provider.request(_input(None, file=PDF_BYTES))["messages"][0]["content"][0]
    image_block = provider.request(_input(None, file=PNG_BYTES, content_type="image/png"))["messages"][0][
        "content"
    ][0]

    assert pdf_block["type"] == "document"
    assert pdf_block["source"]["media_type"] == "application/pdf"
    assert image_block["type"] == "image"
    assert provider.can_read_file("image/png", 1000)
    assert not provider.can_read_file("image/png", 6 * 1024 * 1024)
    assert not provider.can_read_file(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 10
    )


def test_anthropic_answer_is_parsed_and_cleaned():
    client, _ = _client(
        _answer(
            document_number={"value": "  FS-2291 ", "confidence": 1.7, "evidence": "No: FS-2291"},
            expiry_date={"value": "14 March 2027", "confidence": 0.9, "evidence": None, "iso": "2027-03-14"},
            holder_name={"value": "", "confidence": 0.9, "evidence": None},
        )
    )
    provider = AnthropicProvider(_settings(), client=client)

    result = provider.extract(_input("text"))

    assert result.model == "claude-opus-5-5"
    assert result.fields["document_number"].value == "FS-2291"
    assert result.fields["document_number"].confidence == 1.0  # clamped
    assert result.fields["expiry_date"].iso == "2027-03-14"
    assert "holder_name" not in result.fields  # empty values are dropped


@pytest.mark.parametrize(
    ("payload", "stop_reason", "message"),
    [
        (_answer(), "refusal", "declined"),
        (_answer(), "max_tokens", "cut short"),
        ("not json", "end_turn", "could not be read"),
        ([1, 2, 3], "end_turn", "could not be read"),
    ],
)
def test_anthropic_problems_become_provider_errors(payload, stop_reason, message):
    client, _ = _client(payload, stop_reason)

    with pytest.raises(ProviderError, match=message):
        AnthropicProvider(_settings(), client=client).extract(_input("text"))


def test_response_schema_lists_categories():
    schema = response_schema(["permit", "other"])

    category = schema["properties"]["category"]["properties"]["value"]["anyOf"][0]
    assert category == {"type": "string", "enum": ["permit", "other"]}


def test_parse_fields_rejects_non_objects():
    with pytest.raises(ProviderError):
        parse_fields("text")
