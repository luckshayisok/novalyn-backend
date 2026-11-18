"""Validation of uploaded files.

An upload is accepted only when its extension is allowed AND its content
matches that extension AND, when a scanner is configured, it is free of
malware. The declared Content-Type header is ignored: it is whatever the
client says it is.
"""

import hashlib
import logging
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import BinaryIO

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# extension -> (canonical content type, can be shown inline in a browser)
ALLOWED_TYPES: dict[str, tuple[str, bool]] = {
    ".pdf": ("application/pdf", True),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", False),
    ".jpg": ("image/jpeg", True),
    ".jpeg": ("image/jpeg", True),
    ".png": ("image/png", True),
}
ALLOWED_EXTENSIONS_LABEL = "PDF, DOCX, JPG or PNG"
INLINE_CONTENT_TYPES = frozenset(ctype for ctype, inline in ALLOWED_TYPES.values() if inline)

_READ_CHUNK = 64 * 1024
_SPOOL_LIMIT = 2 * 1024 * 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class InvalidFileError(AppError):
    status_code = 422
    code = "invalid_file"
    message = "This file cannot be uploaded."


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"
    message = "This file is too large."


@dataclass
class ValidatedUpload:
    """An upload that passed validation, spooled to a temporary file ready to store."""

    file: BinaryIO
    filename: str
    extension: str
    content_type: str
    size_bytes: int
    sha256: str

    def close(self) -> None:
        self.file.close()


def clean_filename(raw: str | None) -> str:
    """The base name of a client-supplied filename, safe to store and show."""
    name = raw or ""
    # Browsers send a bare name, but strip any directory part from either OS just in case.
    name = PureWindowsPath(PurePosixPath(name).name).name
    name = unicodedata.normalize("NFC", _CONTROL_CHARS.sub("", name)).strip().strip(".")
    if len(name) > 200:
        stem, dot, suffix = name.rpartition(".")
        name = f"{stem[: 200 - len(suffix) - 1]}{dot}{suffix}" if dot else name[:200]
    return name


def _sniff(head: bytes, file: BinaryIO) -> str | None:
    """The extension family the content really is, or None when it is not an allowed type."""
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"PK\x03\x04"):
        # A .docx is a ZIP with these two members. Only the directory is read; nothing is extracted.
        try:
            file.seek(0)
            with zipfile.ZipFile(file) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return None
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return ".docx"
    return None


class ScannerUnavailableError(AppError):
    status_code = 503
    code = "scanner_unavailable"
    message = (
        "Files cannot be checked for viruses right now, so uploads are paused. Try again in a few minutes."
    )


def _scan_for_malware(file: BinaryIO, filename: str) -> None:
    """Refuse infected files, and refuse anything the scanner could not check (fail closed)."""
    from app.integrations.malware import ScannerUnavailableError as Unavailable
    from app.integrations.malware import get_scanner

    scanner = get_scanner()
    try:
        result = scanner.scan(file)
    except Unavailable as error:
        logger.error("Malware scan failed: %s", error)
        raise ScannerUnavailableError() from error
    if not result.clean:
        logger.warning(
            "Upload refused: malware found", extra={"signature": result.signature, "upload": filename}
        )
        raise InvalidFileError("This file contains malware and was not uploaded.", code="file_infected")


def validate_upload(source: BinaryIO, filename: str | None, *, max_bytes: int) -> ValidatedUpload:
    """Read an upload, enforce the size limit and check that content matches the extension."""
    name = clean_filename(filename)
    extension = PurePosixPath(name).suffix.lower()
    if not name or extension not in ALLOWED_TYPES:
        raise InvalidFileError(f"Upload a {ALLOWED_EXTENSIONS_LABEL} file.", code="unsupported_file_type")

    spooled = tempfile.SpooledTemporaryFile(max_size=_SPOOL_LIMIT)  # noqa: SIM115 - returned to the caller
    try:
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(_READ_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise FileTooLargeError(f"Files can be at most {max_bytes // (1024 * 1024)} MB.")
            digest.update(chunk)
            spooled.write(chunk)
        if size == 0:
            raise InvalidFileError("This file is empty.", code="empty_file")

        spooled.seek(0)
        detected = _sniff(spooled.read(16), spooled)
        expected = ".jpg" if extension == ".jpeg" else extension
        if detected != expected:
            raise InvalidFileError(
                "The content of this file does not match its type. "
                f"Upload a real {ALLOWED_EXTENSIONS_LABEL} file.",
                code="file_content_mismatch",
            )
        spooled.seek(0)
        _scan_for_malware(spooled, name)
        return ValidatedUpload(
            file=spooled,
            filename=name,
            extension=extension,
            content_type=ALLOWED_TYPES[extension][0],
            size_bytes=size,
            sha256=digest.hexdigest(),
        )
    except BaseException:
        spooled.close()
        raise
