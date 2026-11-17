"""Repositories for users and their credentials. These tables are not tenant-owned."""

import uuid
from datetime import datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.models import RefreshToken, User, UserToken


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.db.scalars(select(User).where(User.email == email)).first()

    def add(self, user: User) -> User:
        self.db.add(user)
        return user


class RefreshTokenRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, token: RefreshToken) -> RefreshToken:
        self.db.add(token)
        return token

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return self.db.scalars(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).first()

    def family_is_active(self, family_id: uuid.UUID, now: datetime) -> bool:
        return bool(
            self.db.scalar(
                select(
                    exists().where(
                        RefreshToken.family_id == family_id,
                        RefreshToken.revoked_at.is_(None),
                        RefreshToken.expires_at > now,
                    )
                )
            )
        )

    def revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def revoke_all_for_user(
        self, user_id: uuid.UUID, now: datetime, *, except_family_id: uuid.UUID | None = None
    ) -> None:
        statement = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        if except_family_id is not None:
            statement = statement.where(RefreshToken.family_id != except_family_id)
        self.db.execute(statement)


class UserTokenRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, token: UserToken) -> UserToken:
        self.db.add(token)
        return token

    def get_by_hash(self, token_hash: str, purpose: str) -> UserToken | None:
        return self.db.scalars(
            select(UserToken).where(UserToken.token_hash == token_hash, UserToken.purpose == purpose)
        ).first()

    def invalidate_unused(self, user_id: uuid.UUID, purpose: str, now: datetime) -> None:
        self.db.execute(
            update(UserToken)
            .where(
                UserToken.user_id == user_id,
                UserToken.purpose == purpose,
                UserToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
