"""Storage adapters, storage keys, file validation and expiry rules."""

import io
import uuid
from datetime import date

import boto3
import pytest
from moto import mock_aws

from app.core.config import Settings
from app.core.files import FileTooLargeError, InvalidFileError, clean_filename, validate_upload
from app.core.storage import (
    LocalStorage,
    S3Storage,
    StorageKeyError,
    StoredFileNotFoundError,
    document_key,
    validate_key,
)
from app.services.expiry import DocumentStatus, compute_status, days_until_expiry
from tests.helpers import JPEG_BYTES, PDF_BYTES, PNG_BYTES, docx_bytes

KEY = "organizations/a/documents/b/c.pdf"


@pytest.fixture
def local(tmp_path):
    return LocalStorage(str(tmp_path / "files"))


@pytest.fixture
def s3():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="novalyn-test")
        settings = Settings(
            _env_file=None,
            storage_backend="s3",
            s3_bucket="novalyn-test",
            s3_region="us-east-1",
            s3_access_key_id="test",
            s3_secret_access_key="test",
        )
        yield S3Storage(settings)


@pytest.fixture(params=["local", "s3"])
def storage(request):
    return request.getfixturevalue(request.param)


def test_storage_round_trip(storage):
    assert not storage.exists(KEY)

    storage.save(KEY, io.BytesIO(PDF_BYTES), "application/pdf")

    assert storage.exists(KEY)
    assert b"".join(storage.open(KEY)) == PDF_BYTES

    storage.delete(KEY)
    storage.delete(KEY)  # deleting a missing file is not an error
    assert not storage.exists(KEY)
    with pytest.raises(StoredFileNotFoundError):
        storage.open(KEY)


def test_storage_handles_files_larger_than_one_chunk(storage):
    payload = b"0123456789abcdef" * 20_000  # 320 kB

    storage.save(KEY, io.BytesIO(payload), "application/pdf")

    assert b"".join(storage.open(KEY)) == payload


@pytest.mark.parametrize(
    "key",
    [
        "../secrets.txt",
        "organizations/../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "organizations//double",
        "organizations/a b/c.pdf",
        "",
        "a" * 600,
        ".hidden/file",
    ],
)
def test_unsafe_keys_are_rejected(local, key):
    with pytest.raises(StorageKeyError):
        validate_key(key)
    with pytest.raises(StorageKeyError):
        local.save(key, io.BytesIO(b"x"), "application/pdf")
    with pytest.raises(StorageKeyError):
        local.open(key)


def test_document_keys_are_grouped_by_organization():
    org, doc, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    assert document_key(org, doc, version, ".pdf") == f"organizations/{org}/documents/{doc}/{version}.pdf"


def test_failed_local_write_leaves_no_partial_file(local, tmp_path):
    class Broken(io.BytesIO):
        def read(self, *_):
            raise OSError("disk unplugged")

    with pytest.raises(OSError, match="unplugged"):
        local.save(KEY, Broken(b"data"), "application/pdf")

    assert not local.exists(KEY)
    assert not list((tmp_path / "files").rglob("*.part"))


# -------------------------------------------------------------- file validation


@pytest.mark.parametrize(
    ("filename", "content", "extension", "content_type"),
    [
        ("a.pdf", PDF_BYTES, ".pdf", "application/pdf"),
        ("a.png", PNG_BYTES, ".png", "image/png"),
        ("A.JPEG", JPEG_BYTES, ".jpeg", "image/jpeg"),
        (
            "a.docx",
            docx_bytes(),
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ],
)
def test_validate_upload_accepts_matching_content(filename, content, extension, content_type):
    upload = validate_upload(io.BytesIO(content), filename, max_bytes=1_000_000)
    try:
        assert upload.extension == extension
        assert upload.content_type == content_type
        assert upload.size_bytes == len(content)
        assert upload.file.read() == content
    finally:
        upload.close()


def test_validate_upload_rejects_bad_input():
    with pytest.raises(InvalidFileError) as unsupported:
        validate_upload(io.BytesIO(b"hello"), "notes.txt", max_bytes=100)
    with pytest.raises(InvalidFileError) as mismatch:
        validate_upload(io.BytesIO(JPEG_BYTES), "photo.png", max_bytes=1000)
    with pytest.raises(InvalidFileError) as empty:
        validate_upload(io.BytesIO(b""), "empty.pdf", max_bytes=100)
    with pytest.raises(FileTooLargeError):
        validate_upload(io.BytesIO(PDF_BYTES), "big.pdf", max_bytes=10)

    assert unsupported.value.code == "unsupported_file_type"
    assert mismatch.value.code == "file_content_mismatch"
    assert empty.value.code == "empty_file"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\me\\Desktop\\scan.png", "scan.png"),
        ("  spaced name .pdf ", "spaced name .pdf"),
        ("evil\x00name\n.pdf", "evilname.pdf"),
        ("...pdf", "pdf"),
        (None, ""),
    ],
)
def test_clean_filename(raw, expected):
    assert clean_filename(raw) == expected


def test_clean_filename_keeps_the_extension_when_shortening():
    cleaned = clean_filename("x" * 400 + ".pdf")

    assert len(cleaned) == 200
    assert cleaned.endswith(".pdf")


# ------------------------------------------------------------------ expiry rules


def test_compute_status_boundaries():
    today = date(2026, 6, 15)

    assert compute_status(None, today) is DocumentStatus.VALID
    assert compute_status(date(2026, 6, 14), today) is DocumentStatus.EXPIRED
    assert compute_status(date(2026, 6, 15), today) is DocumentStatus.EXPIRING_SOON
    assert compute_status(date(2026, 7, 15), today) is DocumentStatus.EXPIRING_SOON
    assert compute_status(date(2026, 7, 16), today) is DocumentStatus.VALID
    assert compute_status(date(2026, 6, 20), today, window_days=3) is DocumentStatus.VALID
    assert compute_status(date(2020, 1, 1), today, archived=True) is DocumentStatus.ARCHIVED
    assert days_until_expiry(date(2026, 6, 10), today) == -5
    assert days_until_expiry(None, today) is None
