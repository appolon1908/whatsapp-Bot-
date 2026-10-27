"""Fail-closed RS256 staff identity verification; app roles come from the DB."""

import asyncio
import json
import math
import re
import threading
import time
from dataclasses import dataclass

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from whatsapp_bot.config import Settings
from whatsapp_bot.http_runtime import run_http

_MAX_TOKEN = 16384
_MAX_HEADER = 2048
_MAX_JWKS = 65536
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str


class AuthError(Exception):
    """Only stable, safe public errors leave the verification boundary."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _invalid() -> AuthError:
    return AuthError("invalidtoken", "Bearer token is invalid.", 401)


def _unavailable() -> AuthError:
    return AuthError("identityunavailable", "Identity verification is temporarily unavailable.", 503)


class TokenVerifier:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._key: RSAPublicKey | None = None
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires_at = 0.0
        self._last_attempt = float("-inf")
        self._failed = False
        self._lock = threading.Lock()
        if settings.jwt_public_key:
            try:
                key = serialization.load_pem_public_key(settings.jwt_public_key.encode("ascii"))
                if isinstance(key, RSAPublicKey) and key.key_size >= 2048:
                    self._key = key
            except (ValueError, TypeError, UnicodeError):
                pass

    def is_configured(self) -> bool:
        return bool(
            self._settings.jwt_issuer.strip()
            and self._settings.jwt_audience.strip()
            and (self._key is not None or self._settings.jwks_url)
        )

    def verify(self, token: str) -> Principal:
        if not self.is_configured():
            raise AuthError("identityunconfigured", "Staff identity is not configured.", 503)
        if (
            not isinstance(token, str)
            or len(token) > _MAX_TOKEN
            or not _TOKEN.fullmatch(token)
            or len(token.split(".")[0]) > _MAX_HEADER
        ):
            raise _invalid()
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or set(header) - {"alg", "typ", "kid"}
                or header.get("typ", "JWT") not in {"JWT", "at+jwt"}
            ):
                raise _invalid()
            kid = header.get("kid")
            if kid is not None and (not isinstance(kid, str) or not 1 <= len(kid) <= 200):
                raise _invalid()
            key = self._key if self._key is not None else self._jwks_key(kid)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._settings.jwt_issuer,
                audience=self._settings.jwt_audience,
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "strict_aud": True},
            )
            # JWT NumericDate values must be numbers, not strings or booleans.
            for name in ("exp", "iat", "nbf"):
                if name in claims:
                    value = claims[name]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise _invalid()
            sub = claims["sub"]
            if (
                not isinstance(sub, str)
                or not 1 <= len(sub) <= 255
                or not sub.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in sub)
            ):
                raise _invalid()
            return Principal(subject=sub)
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError):
            raise _invalid() from None

    def _jwks_key(self, kid: str | None) -> RSAPublicKey:
        if not kid:
            raise _invalid()
        with self._lock:
            now = time.monotonic()
            if not self._failed and now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now - self._last_attempt < self._settings.jwks_min_refresh_seconds:
                if self._failed or now >= self._expires_at:
                    raise _unavailable()
                raise _invalid()
            self._last_attempt = now
            try:
                keys = self._fetch_keys()
            except (
                httpx.HTTPError,
                jwt.PyJWTError,
                ValueError,
                TypeError,
                KeyError,
                UnicodeError,
                RecursionError,
                TimeoutError,
            ):
                self._failed = True
                self._keys.clear()
                self._expires_at = 0
                raise _unavailable() from None
            self._keys = keys
            self._failed = False
            self._expires_at = time.monotonic() + self._settings.jwks_cache_seconds
            if kid not in keys:
                raise _invalid()
            return keys[kid]

    def _fetch_keys(self) -> dict[str, RSAPublicKey]:
        # This synchronous boundary is called from the API's synchronous auth
        # dependency. A total async deadline cancels stalled headers/body reads
        # without waiting for uninterruptible resolver executor shutdown.
        return run_http(self._fetch_keys_bounded, timeout=self._settings.jwks_timeout_seconds)

    async def _fetch_keys_bounded(self) -> dict[str, RSAPublicKey]:
        async with asyncio.timeout(self._settings.jwks_timeout_seconds):
            async with httpx.AsyncClient(
                timeout=self._settings.jwks_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=1),
            ) as client:
                async with client.stream(
                    "GET",
                    self._settings.jwks_url,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("compressed identity responses are prohibited")
                    body = bytearray()
                    # Read raw chunks without buffering or decompression: both
                    # total elapsed time and memory stay bounded on drip input.
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > _MAX_JWKS:
                            raise ValueError("identity response limit exceeded")
                        body.extend(chunk)
                return self._parse_keys(body)

    @staticmethod
    def _parse_keys(body: bytes | bytearray) -> dict[str, RSAPublicKey]:
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise ValueError("invalid key set")
        entries = payload["keys"]
        if not 1 <= len(entries) <= 32:
            raise ValueError("invalid key count")
        keys = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid key")
            # Non-signing and other-algorithm keys may coexist in an issuer JWKS.
            if (
                entry.get("kty") != "RSA"
                or entry.get("alg", "RS256") != "RS256"
                or entry.get("use", "sig") != "sig"
                or "d" in entry
            ):
                continue
            if "key_ops" in entry and entry["key_ops"] != ["verify"]:
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 200 or kid in keys:
                raise ValueError("invalid key identifier")
            key = jwt.algorithms.RSAAlgorithm.from_jwk(entry)
            if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                raise ValueError("invalid signing key")
            keys[kid] = key
        if not keys:
            raise ValueError("no permitted signing keys")
        return keys
