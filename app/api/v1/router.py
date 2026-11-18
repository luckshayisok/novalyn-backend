"""Version 1 of the HTTP API."""

from fastapi import APIRouter

from app.api.v1.audit.routes import router as audit_router
from app.api.v1.auth.routes import router as auth_router
from app.api.v1.billing.routes import billing_router, public_billing_router
from app.api.v1.documents.routes import documents_router, entities_router, files_router, insights_router
from app.api.v1.insights.routes import calendar_router, reports_router
from app.api.v1.lifecycle.routes import notifications_router, renewals_router
from app.api.v1.organizations.routes import invitation_router, meta_router
from app.api.v1.organizations.routes import router as organizations_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(organizations_router)
api_router.include_router(invitation_router)
api_router.include_router(audit_router)
api_router.include_router(meta_router)
api_router.include_router(documents_router)
api_router.include_router(entities_router)
api_router.include_router(insights_router)
api_router.include_router(files_router)
api_router.include_router(notifications_router)
api_router.include_router(renewals_router)
api_router.include_router(calendar_router)
api_router.include_router(reports_router)
api_router.include_router(billing_router)
api_router.include_router(public_billing_router)
