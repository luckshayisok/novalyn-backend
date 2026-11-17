"""Document processing tasks."""

import uuid

from app.core.database import SessionLocal
from app.workers.celery_app import celery_app


@celery_app.task(name="documents.extract", soft_time_limit=240, time_limit=300)
def extract_document_task(extraction_id: str) -> None:
    """Read one uploaded file and store suggestions. Failures are recorded on the extraction, not raised."""
    from app.services.extraction import ExtractionService

    with SessionLocal() as db:
        ExtractionService(db).run(uuid.UUID(extraction_id))
