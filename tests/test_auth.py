"""Real RSA signatures and a mocked HTTP boundary; no live identity services."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def verifier(keys, **kwargs):
    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    return TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwt_public_key=keys[1],
            **kwargs,
        )
    )


def token(keys, **changes):
    claims = {
        "iss": "https://identity.example",
        "aud": "commerce",
        "sub": "staff-123",
        "iat": int(time.time()) - 1,
        "exp": int(time.time()) + 300,
    }
    claims.update(changes)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, keys[0], algorithm="RS256", headers={"kid": "key-one"})


def assert_rejected(verifier, encoded, status=401, code="invalidtoken"):
    from whatsapp_bot.auth import AuthError

    with pytest.raises(AuthError) as exc:
        verifier.verify(encoded)
    assert (exc.value.status, exc.value.code) == (status, code)
    assert encoded not in str(exc.value)


def test_verified_subject_has_no_token_authorization_claims(keys):
    principal = verifier(keys).verify(token(keys, roles=["owner"], tenant_id="forged"))
    assert principal.subject == "staff-123"
    assert not hasattr(principal, "roles")
    assert not hasattr(principal, "tenant_id")


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": int(time.time()) - 10},
        {"iss": "https://attacker.example"},
        {"aud": "other"},
        {"sub": None},
        {"sub": ""},
        {"iat": None},
        {"exp": None},
        {"iat": int(time.time()) + 300},
        {"nbf": int(time.time()) + 300},
        {"iat": True},
        {"exp": "99999999999"},
        {"sub": 123},
    ],
)
def test_invalid_required_claims_rejected_with_real_signature(keys, changes):
    assert_rejected(verifier(keys), token(keys, **changes))


def test_unsigned_and_symmetric_algorithm_confusion_rejected(keys):
    claims = jwt.decode(token(keys), options={"verify_signature": False})
    assert_rejected(verifier(keys), jwt.encode(claims, "", algorithm="none"))
    assert_rejected(verifier(keys), jwt.encode(claims, "x" * 64, algorithm="HS256"))


def test_other_rsa_key_signature_rejected(keys):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert_rejected(verifier(keys), token((other, keys[1])))


def test_unconfigured_identity_fails_closed():
    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    verify = TokenVerifier(Settings(_env_file=None))
    assert verify.is_configured() is False
    assert_rejected(verify, "anything", status=503, code="identityunconfigured")


def test_weak_rsa_key_is_unconfigured():
    private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    verify = verifier((private, public))
    assert verify.is_configured() is False
    assert_rejected(verify, "anything", status=503, code="identityunconfigured")


def test_token_and_header_size_are_bounded(keys):
    assert_rejected(verifier(keys), "a" * 20000)
    assert_rejected(
        verifier(keys),
        jwt.encode(
            jwt.decode(token(keys), options={"verify_signature": False}),
            keys[0],
            algorithm="RS256",
            headers={"padding": "x" * 4000},
        ),
    )


def test_untrusted_token_urls_never_fetch(keys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("untrusted token triggered networking")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    claims = jwt.decode(token(keys), options={"verify_signature": False})
    encoded = jwt.encode(
        claims,
        keys[0],
        algorithm="RS256",
        headers={"jku": "https://attacker.example/jwks", "x5u": "https://attacker.example/cert"},
    )
    assert_rejected(verifier(keys), encoded)


def test_jwks_fetch_cache_rotation_and_failure(keys, monkeypatch):
    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    first = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(keys[0].public_key()))
    first.update(kid="key-one", alg="RS256", use="sig")
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(second_key.public_key()))
    second.update(kid="key-two", alg="RS256", use="sig")
    calls = []
    responses = [{"keys": [first]}, {"keys": [second]}, None]

    async def send(client, request, **kwargs):
        calls.append(str(request.url))
        value = responses.pop(0)
        if value is None:
            raise httpx.ConnectError("sensitive internal failure", request=request)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
            jwks_min_refresh_seconds=0,
        )
    )
    assert verify.verify(token(keys)).subject == "staff-123"
    assert verify.verify(token(keys)).subject == "staff-123"
    assert len(calls) == 1
    claims = jwt.decode(token(keys), options={"verify_signature": False})
    rotated = jwt.encode(claims, second_key, algorithm="RS256", headers={"kid": "key-two"})
    assert verify.verify(rotated).subject == "staff-123"
    assert len(calls) == 2
    assert_rejected(verify, token(keys), status=503, code="identityunavailable")
    assert calls == ["https://identity.example/jwks"] * 3


@pytest.mark.parametrize(
    "body",
    [
        b'{"keys":[{"kty":"RSA","kid":"key-one","n":"!","e":"AQAB"}]}',
        b"[" * 12000 + b"]" * 12000,
        b'{"keys":[{"kty":"RSA","kid":"key-one","e":"AQAB"}]}',
        b'{"keys":[]}',
        b" " * 65537,
    ],
    ids=["invalid-modulus", "deep-json", "missing-modulus", "empty-keys", "oversized"],
)
def test_malformed_jwks_is_safe_unavailable(keys, monkeypatch, body):
    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    async def send(client, request, **kwargs):
        return httpx.Response(200, stream=httpx.ByteStream(body), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
        )
    )
    assert_rejected(verify, token(keys), status=503, code="identityunavailable")


def test_jwks_outage_clears_cached_keys_and_backs_off(keys, monkeypatch):
    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    clock = [100.0]
    from types import SimpleNamespace

    monkeypatch.setattr("whatsapp_bot.auth.time", SimpleNamespace(monotonic=lambda: clock[0]))
    entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(keys[0].public_key()))
    entry["kid"] = "key-one"
    count = [0]

    async def send(client, request, **kwargs):
        count[0] += 1
        if count[0] == 1:
            return httpx.Response(
                200, stream=httpx.ByteStream(json.dumps({"keys": [entry]}).encode()), request=request
            )
        raise httpx.ConnectError("private upstream detail", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
            jwks_cache_seconds=10,
        )
    )
    encoded = token(keys)
    assert verify.verify(encoded).subject == "staff-123"
    clock[0] = 111.0
    assert_rejected(verify, encoded, status=503, code="identityunavailable")
    assert_rejected(verify, encoded, status=503, code="identityunavailable")
    assert count[0] == 2


def test_unknown_kids_cannot_amplify_network_requests(keys, monkeypatch):
    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(keys[0].public_key()))
    entry["kid"] = "key-one"
    count = [0]

    async def send(client, request, **kwargs):
        count[0] += 1
        return httpx.Response(
            200, stream=httpx.ByteStream(json.dumps({"keys": [entry]}).encode()), request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
        )
    )
    assert verify.verify(token(keys)).subject == "staff-123"
    claims = jwt.decode(token(keys), options={"verify_signature": False})
    for index in range(20):
        unknown = jwt.encode(claims, keys[0], algorithm="RS256", headers={"kid": f"unknown-{index}"})
        assert_rejected(verify, unknown)
    assert count[0] == 1


def test_deeply_nested_signed_claims_are_safe_invalidtoken(keys):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from jwt.utils import base64url_encode

    header = base64url_encode(b'{"alg":"RS256"}')
    claims = base64url_encode(b'{"nested":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}")
    signed = header + b"." + claims
    signature = keys[0].sign(signed, padding.PKCS1v15(), hashes.SHA256())
    encoded = (signed + b"." + base64url_encode(signature)).decode("ascii")
    assert len(encoded) < 16384
    import sys

    original_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        assert_rejected(verifier(keys), encoded)
    finally:
        sys.setrecursionlimit(original_limit)


def test_older_pyjwt_raw_recursion_is_safe_invalidtoken(keys, monkeypatch):
    """The supported PyJWT range includes decoders without recursion wrapping."""
    import sys

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from jwt.utils import base64url_encode

    def raw_decode_payload(self, decoded):
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        return decoder.decode(decoded["payload"].decode("utf-8"))

    monkeypatch.setattr(jwt.api_jwt.PyJWT, "_decode_payload", raw_decode_payload)
    header = base64url_encode(b'{"alg":"RS256"}')
    claims = base64url_encode(b'{"nested":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}")
    signed = header + b"." + claims
    signature = keys[0].sign(signed, padding.PKCS1v15(), hashes.SHA256())
    encoded = (signed + b"." + base64url_encode(signature)).decode("ascii")
    original_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        assert_rejected(verifier(keys), encoded)
    finally:
        sys.setrecursionlimit(original_limit)


def test_jwks_drip_stream_obeys_total_deadline_and_closes(keys, monkeypatch):
    import asyncio

    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    class DripStream(httpx.SyncByteStream, httpx.AsyncByteStream):
        closed = False
        chunks = 0

        def __iter__(self):
            for _ in range(30):
                time.sleep(0.03)
                self.chunks += 1
                yield b" "

        async def __aiter__(self):
            for _ in range(30):
                await asyncio.sleep(0.03)
                self.chunks += 1
                yield b" "

        def close(self):
            self.closed = True

        async def aclose(self):
            self.closed = True

    stream = DripStream()

    async def send(client, request, **kwargs):
        return httpx.Response(200, stream=stream, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
            jwks_timeout_seconds=0.1,
        )
    )
    started = time.perf_counter()
    assert_rejected(verify, token(keys), status=503, code="identityunavailable")
    assert time.perf_counter() - started < 0.4
    assert stream.closed
    assert stream.chunks < 10


def test_jwks_header_wait_obeys_total_deadline(keys, monkeypatch):
    import asyncio

    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    cancelled = []

    async def send(client, request, **kwargs):
        try:
            await asyncio.sleep(0.7)
        finally:
            cancelled.append(True)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
            jwks_timeout_seconds=0.1,
        )
    )
    started = time.perf_counter()
    assert_rejected(verify, token(keys), status=503, code="identityunavailable")
    assert time.perf_counter() - started < 0.4
    assert cancelled == [True]


def test_jwks_compressed_response_is_rejected_without_decompression(keys, monkeypatch):
    import gzip

    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    class CompressedStream(httpx.AsyncByteStream):
        read = False
        closed = False

        async def __aiter__(self):
            self.read = True
            yield gzip.compress(b" " * 1000000)

        async def aclose(self):
            self.closed = True

    stream = CompressedStream()

    async def send(client, request, **kwargs):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
        )
    )
    assert_rejected(verify, token(keys), status=503, code="identityunavailable")
    assert not stream.read
    assert stream.closed


def test_jwks_executor_stall_returns_on_deadline_without_late_fetch(keys, monkeypatch):
    import asyncio
    import threading

    from whatsapp_bot.auth import TokenVerifier
    from whatsapp_bot.config import Settings

    resolved = threading.Event()
    requests = []

    def slow_resolve():
        time.sleep(0.35)
        resolved.set()

    async def send(client, request, **kwargs):
        await asyncio.to_thread(slow_resolve)
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    verify = TokenVerifier(
        Settings(
            _env_file=None,
            jwt_issuer="https://identity.example",
            jwt_audience="commerce",
            jwks_url="https://identity.example/jwks",
            jwks_timeout_seconds=0.1,
        )
    )
    started = time.monotonic()
    assert_rejected(verify, token(keys), status=503, code="identityunavailable")
    assert time.monotonic() - started < 0.25
    assert resolved.wait(1)
    assert requests == []
