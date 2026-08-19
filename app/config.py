# =============================================================================
# Central application settings.
#
# This module defines every configuration value the application needs
# (database connection, secret keys, SMTP credentials, etc.) as a single
# Pydantic "Settings" class. Values are NOT hard-coded here for production use;
# they are meant to be overridden by environment variables or a local ".env"
# file. This is the standard "12-factor app" pattern: configuration lives in
# the environment, not in the source code.
# =============================================================================
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# The absolute path to the "app" package on disk. Other modules use this to
# build reliable paths to the "templates" and "static" folders regardless of
# which directory the process was started from.
BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Typed container for every configurable value in the application.

    Each attribute below has a default that is safe for local development.
    In a real deployment, every value here can be overridden by setting an
    environment variable with the same name in upper case (e.g. SECRET_KEY),
    or by editing the ".env" file referenced in `model_config` below.
    """

    app_name: str = "Courier App Demo"
    # Controls which safety checks are enforced - see validate_production_security below.
    environment: Literal["development", "test", "production"] = "development"
    # Used to sign/verify JWT session tokens. MUST be replaced in production.
    secret_key: str = "local-development-secret-change-before-use"
    # SQLAlchemy connection string. Defaults to a local SQLite file so the
    # project can run with zero external setup; Docker Compose overrides this
    # with a PostgreSQL URL for the "real" deployment.
    database_url: str = "sqlite:///courier.db"
    # How long a login session stays valid before the user must log in again.
    access_token_expire_minutes: int = 480
    # Whether cookies require HTTPS. Must be True in production (enforced below).
    cookie_secure: bool = False
    # Divisor used in the volumetric weight formula (length * width * height / divisor).
    volumetric_divisor: int = 5000
    # SMTP connection details used to email shipment vouchers to customers.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True
    # Base URL inserted into tracking links sent by email.
    public_base_url: str = "http://127.0.0.1:8001"

    # Tells pydantic-settings to also read values from a ".env" file (if
    # present) in addition to real OS environment variables. "extra=ignore"
    # means unrelated keys in the .env file will not raise an error.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        """Fail fast at startup if production is misconfigured.

        This runs automatically after all fields are loaded. Its purpose is
        to prevent a common and dangerous mistake: deploying to production
        with a default/example secret key, or without secure cookies. Rather
        than silently running insecurely, the app refuses to start.
        """

        if self.environment == "production":
            # Reject both "too short" keys and known example values that
            # might have been copy-pasted from the README without changing.
            if len(self.secret_key) < 32 or self.secret_key in {
                "local-development-secret-change-before-use",
                "local-development-secret-change-before-deployment",
                "change-this-secret-key",
                "replace-with-a-random-secret-at-least-32-characters",
            }:
                raise ValueError(
                    "Production requires a unique SECRET_KEY of at least 32 characters."
                )
            if not self.cookie_secure:
                raise ValueError("Production requires COOKIE_SECURE=true.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the single, cached Settings instance for the whole process.

    `lru_cache` with no arguments means this function always returns the
    exact same object after the first call, instead of re-reading and
    re-parsing the .env file on every request. FastAPI dependencies and
    plain module-level code both call this function to access configuration.
    """
    return Settings()
