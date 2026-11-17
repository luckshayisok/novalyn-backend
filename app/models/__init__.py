"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from app.models.audit_log import AuditLog
from app.models.document import (
    Document,
    DocumentExtraction,
    DocumentVersion,
    Entity,
    ExtractionStatus,
    VerificationStatus,
    VersionState,
)
from app.models.lifecycle import (
    EmailStatus,
    Notification,
    OutboxEvent,
    OutboxStatus,
    Renewal,
    RenewalEvent,
    RenewalStatus,
)
from app.models.organization import Invitation, Membership, Organization
from app.models.user import RefreshToken, User, UserToken, UserTokenPurpose

__all__ = [
    "AuditLog",
    "Document",
    "DocumentExtraction",
    "DocumentVersion",
    "Entity",
    "ExtractionStatus",
    "Invitation",
    "Membership",
    "Organization",
    "RefreshToken",
    "User",
    "UserToken",
    "UserTokenPurpose",
    "VerificationStatus",
    "VersionState",
    "EmailStatus",
    "Notification",
    "OutboxEvent",
    "OutboxStatus",
    "Renewal",
    "RenewalEvent",
    "RenewalStatus",
]
