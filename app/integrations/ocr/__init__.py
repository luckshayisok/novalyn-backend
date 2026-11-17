"""Optical character recognition for scans and photos.

Tesseract is used when it is installed (or TESSERACT_CMD points at it). Without
it, OCR is reported as unavailable and the pipeline either lets the AI provider
read the file itself or asks the user to enter the details.
"""

import logging
import shutil
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class OCREngine(Protocol):
    name: str

    def image_to_text(self, image) -> str:
        """Read the text in a PIL image."""


class TesseractOCR:
    name = "tesseract"

    def __init__(self, command: str) -> None:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = command
        self._pytesseract = pytesseract

    def image_to_text(self, image) -> str:
        return self._pytesseract.image_to_string(image, timeout=60)


@lru_cache
def get_ocr() -> OCREngine | None:
    settings = get_settings()
    if settings.ocr_backend == "none":
        return None
    command = settings.tesseract_cmd or shutil.which("tesseract")
    if not command:
        if settings.ocr_backend == "tesseract":
            logger.warning("OCR_BACKEND=tesseract but the tesseract program was not found")
        return None
    return TesseractOCR(command)
