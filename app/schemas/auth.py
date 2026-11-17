"""Authentication request and response schemas."""

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.schemas.common import Schema

# Length rules beyond these bounds are applied in the service, where the
# configured minimum is known.
_Password = Field(min_length=1, max_length=256)
_Token = Field(min_length=16, max_length=200)


class UserOut(Schema):
    id: uuid.UUID
    email: str
    full_name: str
    is_email_verified: bool
    created_at: datetime


class RegisterRequest(Schema):
    email: EmailStr
    password: str = _Password
    full_name: str = Field(min_length=1, max_length=200)

    @field_validator("full_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter your name")
        return value.strip()


class LoginRequest(Schema):
    email: EmailStr
    password: str = _Password


class TokenResponse(Schema):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class VerifyEmailRequest(Schema):
    token: str = _Token


class ForgotPasswordRequest(Schema):
    email: EmailStr


class ResetPasswordRequest(Schema):
    token: str = _Token
    password: str = _Password


class ChangePasswordRequest(Schema):
    current_password: str = _Password
    new_password: str = _Password


class UpdateProfileRequest(Schema):
    full_name: str = Field(min_length=1, max_length=200)

    @field_validator("full_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter your name")
        return value.strip()
