"""Application configuration, loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Used only when SECRET_KEY is not set. Rejected in production.
DEV_SECRET_KEY = "dev-only-secret-key-not-for-production-use-0000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "testing", "production"] = "development"
    app_name: str = "NOVALYN"
    api_v1_prefix: str = "/api/v1"

    # Database
    database_url: str = "sqlite:///./var/novalyn.db"
    database_pool_size: int = 10
    database_max_overflow: int = 10

    # Security
    secret_key: str = DEV_SECRET_KEY
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    refresh_reuse_grace_seconds: int = 10
    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 60
    invitation_ttl_days: int = 7
    password_min_length: int = 10

    # Web
    frontend_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173"
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None

    # Background processing
    redis_url: str | None = None
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = True

    # Email
    email_backend: Literal["console", "file", "memory", "smtp"] = "file"
    email_from: str = "NOVALYN <no-reply@novalyn.local>"
    email_file_dir: str = "./var/mail"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True

    # File storage
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: str = "./var/storage"
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None  # for S3-compatible services such as MinIO or R2
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    max_upload_size_mb: int = 25
    file_url_ttl_seconds: int = 120

    # Document lifecycle
    expiring_soon_days: int = 30

    # Document intelligence
    # none: no automatic reading. heuristic: rule-based development adapter, no external calls.
    # anthropic: Claude reads the document text or, for scans and photos, the file itself.
    ai_provider: Literal["none", "heuristic", "anthropic"] = "heuristic"
    anthropic_api_key: str | None = None  # falls back to the SDK's own credential lookup
    ai_model: str = "claude-opus-5-5"
    ai_effort: Literal["low", "medium", "high"] = "medium"
    ai_refusal_fallback: bool = True
    ai_timeout_seconds: float = 120.0
    ocr_backend: Literal["auto", "tesseract", "none"] = "auto"
    tesseract_cmd: str | None = None
    extraction_confidence_threshold: float = 0.8
    extraction_max_pages: int = 10
    extraction_max_chars: int = 30_000

    # Rate limiting
    rate_limit_enabled: bool = True

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        # SQLAlchemy defaults "postgresql://" to psycopg2; this project ships psycopg 3.
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def broker_url(self) -> str | None:
        return self.celery_broker_url or self.redis_url

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        if not self.is_production:
            return self
        problems: list[str] = []
        if self.secret_key == DEV_SECRET_KEY or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
        if not self.database_url.startswith("postgresql"):
            problems.append("DATABASE_URL must point to PostgreSQL")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true")
        if self.celery_task_always_eager:
            problems.append("CELERY_TASK_ALWAYS_EAGER must be false")
        if not self.broker_url:
            problems.append("REDIS_URL or CELERY_BROKER_URL must be set")
        if self.email_backend != "smtp":
            problems.append("EMAIL_BACKEND must be smtp")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must list explicit origins")
        if self.storage_backend == "s3" and not self.s3_bucket:
            problems.append("S3_BUCKET must be set when STORAGE_BACKEND is s3")
        if problems:
            raise ValueError("Invalid production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
