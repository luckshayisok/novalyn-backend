"""Getting text out of uploaded files, without AI.

PDF text layers are read with pypdf, Word files with python-docx, and scans
or photos with OCR when an OCR engine is available. Every reader is bounded
(pages, characters, decompressed size) because the files come from users.
"""

import io
import logging
import re
import zipfile
from dataclasses import dataclass

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_XML_BYTES = 25 * 1024 * 1024
OCR_PDF_PAGES = 3
# A PDF whose text layer has fewer readable characters than this is treated as a scan.
MIN_TEXT_CHARS = 40


class TextExtractionError(Exception):
    """The file's text could not be read. The message is shown to the user."""


@dataclass(frozen=True)
class ExtractedText:
    text: str
    source: str  # pdf_text, docx, ocr


def meaningful_length(text: str) -> int:
    return len(re.sub(r"\W+", "", text))


def _limit(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[: get_settings().extraction_max_chars]


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise TextExtractionError("This PDF is protected with a password, so it cannot be read.")
        pages = reader.pages[: get_settings().extraction_max_pages]
        return _limit("\n".join(page.extract_text() or "" for page in pages))
    except TextExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as error:
        raise TextExtractionError("This PDF could not be read. It may be damaged.") from error


def docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise TextExtractionError("This Word document is too large to read.")
    except (zipfile.BadZipFile, KeyError) as error:
        raise TextExtractionError("This Word document could not be read. It may be damaged.") from error

    from docx import Document

    try:
        document = Document(io.BytesIO(data))
    except Exception as error:  # python-docx raises several unrelated types for bad files
        raise TextExtractionError("This Word document could not be read. It may be damaged.") from error
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return _limit("\n".join(lines))


def ocr_image(data: bytes, ocr) -> str:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width * image.height > 60_000_000:
                raise TextExtractionError("This image is too large to read.")
            return _limit(ocr.image_to_text(image))
    except TextExtractionError:
        raise
    except (UnidentifiedImageError, OSError) as error:
        raise TextExtractionError("This image could not be read.") from error


def ocr_pdf(data: bytes, ocr) -> str:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise TextExtractionError("This PDF could not be read. It may be damaged.") from error
    try:
        texts = []
        for index in range(min(len(pdf), OCR_PDF_PAGES)):
            image = pdf[index].render(scale=2).to_pil()
            texts.append(ocr.image_to_text(image))
        return _limit("\n".join(texts))
    finally:
        pdf.close()


def extract_text(data: bytes, content_type: str, *, ocr=None) -> ExtractedText | None:
    """The file's text, or None when it is a scan or photo and no OCR engine is available."""
    if content_type == DOCX_TYPE:
        return ExtractedText(docx_text(data), "docx")
    if content_type == "application/pdf":
        text = pdf_text(data)
        if meaningful_length(text) >= MIN_TEXT_CHARS:
            return ExtractedText(text, "pdf_text")
        return ExtractedText(ocr_pdf(data, ocr), "ocr") if ocr else None
    if content_type.startswith("image/"):
        return ExtractedText(ocr_image(data, ocr), "ocr") if ocr else None
    raise TextExtractionError("This file type cannot be read.")
