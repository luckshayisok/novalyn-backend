"""Nightly housekeeping: erase organizations whose deletion date has passed, then apply retention."""

import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="maintenance.nightly", soft_time_limit=1800, time_limit=2100)
def nightly_maintenance_task() -> dict:
    from app.core.database import system_session
    from app.services.data_lifecycle import OrganizationPurger, RetentionService

    with system_session() as db:
        purged = OrganizationPurger(db).run()
        removed = RetentionService(db).run()
    logger.info("Nightly maintenance", extra={"organizations_erased": len(purged.organizations), **removed})
    return {
        "organizations_erased": len(purged.organizations),
        "files_deleted": purged.files_deleted,
        **removed,
    }
