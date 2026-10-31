"""Explicit, secret-safe runtime configuration for the commerce process."""

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_DATABASE_NAME = re.compile(r"^codestra_whatsapp_commerce(?:_[a-z0-9_]+)?$")


class Settings(BaseSettings):
    """Read COMMERCE_ variables; read a dotenv file only via ``_env_file=path``.

    Incomplete configuration is permitted so health checks can report a safe
    unready state. Invalid or dangerous configured values fail at construction.
    """

    model_config = SettingsConfigDict(
        env_prefix="COMMERCE_",
        env_file=None,
        extra="forbid",
        hide_input_in_errors=True,
        validate_default=True,
    )

    database_url: SecretStr = SecretStr("")
    expected_database_name: str = "codestra_whatsapp_commerce"
    jwt_issuer: str = Field(default="", max_length=2048)
    jwt_audience: str = Field(default="", max_length=512)
    jwks_url: str = Field(default="", max_length=2048)
    jwt_public_key: str = Field(default="", max_length=16384)
    ingress_secret: SecretStr = SecretStr("")
    policy_base_url: str = Field(default="", max_length=2048)
    policy_bearer_token: SecretStr = SecretStr("")
    outbound_enabled: bool = False
    worker_tenant_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    environment: Literal["local", "test", "production"] = "local"
    allow_insecure_local_http: bool = False
    jwks_timeout_seconds: float = Field(default=5, ge=0.1, le=10)
    jwks_cache_seconds: int = Field(default=300, ge=1, le=3600)
    jwks_min_refresh_seconds: float = Field(default=5, ge=0, le=60)

    @model_validator(mode="after")
    def validate_targets(self):
        if not _DATABASE_NAME.fullmatch(self.expected_database_name):
            raise ValueError("expected database name must be an isolated commerce database")
        database = self.database_url.get_secret_value()
        if database:
            try:
                parsed = make_url(database)
                allowed_driver = parsed.drivername in {"postgresql", "postgresql+psycopg"}
                safe_query = not (
                    {"dbname", "database", "service", "servicefile", "dsn"}
                    & {key.lower() for key in parsed.query}
                )
                safe = allowed_driver and parsed.database == self.expected_database_name and safe_query
            except (ArgumentError, ValueError):
                safe = False
            if not safe:
                raise ValueError("database URL must target the expected PostgreSQL commerce database")
        if self.environment == "production" and self.allow_insecure_local_http:
            raise ValueError("insecure local HTTP is prohibited in production")
        if self.jwks_url and self.jwt_public_key:
            raise ValueError("configure exactly one identity key source")
        for value in (self.jwks_url, self.policy_base_url):
            if value:
                self._validate_http_url(value)
        if self.environment == "production" and self.jwt_issuer:
            self._validate_http_url(self.jwt_issuer)
        return self

    def _validate_http_url(self, value: str) -> None:
        try:
            url = urlsplit(value)
            host = url.hostname or ""
            # Accessing port validates malformed and out-of-range ports.
            _ = url.port
            if not host or url.username is not None or url.password is not None or url.fragment:
                raise ValueError
            if any(ord(char) <= 32 for char in value) or "\\" in value:
                raise ValueError
            if url.scheme == "https":
                return
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host in {"localhost", "testserver"} or host.endswith((".localhost", ".test"))
            if (
                url.scheme == "http"
                and self.allow_insecure_local_http
                and self.environment != "production"
                and local
            ):
                return
        except ValueError:
            pass
        raise ValueError("service URLs require HTTPS; explicit local HTTP is restricted to local test hosts")
