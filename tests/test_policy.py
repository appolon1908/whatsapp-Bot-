import importlib
import json
from uuid import uuid4

import httpx
import pytest


def module():
    assert importlib.util.find_spec("whatsapp_bot.policy"), "policy implementation missing"
    return importlib.import_module("whatsapp_bot.policy")


def payload():
    return {"command_id": str(uuid4()), "idempotency_key": "stable-key"}


def test_policy_requires_https_and_nonempty_credentials():
    m = module()
    with pytest.raises(ValueError):
        m.PolicyClient("http://policy.example", "secret")
    with pytest.raises(ValueError):
        m.PolicyClient("https://policy.example", "")


def test_policy_posts_only_contract_route_and_validates_acknowledgement():
    m = module()
    p = payload()

    def handler(request):
        assert str(request.url) == "https://policy.example/platform/v1/whatsapp/messages"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            202,
            stream=httpx.ByteStream(
                json.dumps({"operation_id": "op-1", "command_id": p["command_id"]}).encode()
            ),
        )

    client = m.PolicyClient("https://policy.example", "secret")
    client._transport = httpx.MockTransport(handler)
    assert client.send(p)["operation_id"] == "op-1"


@pytest.mark.parametrize(
    "status,body,transient",
    [
        (202, {"operation_id": "x", "command_id": "different"}, True),
        (202, {"command_id": "missing-operation"}, True),
        (302, {}, False),
        (429, {}, True),
        (503, {}, True),
        (401, {}, False),
    ],
)
def test_policy_failure_classification_does_not_expose_response(status, body, transient):
    m = module()
    client = m.PolicyClient("https://policy.example", "secret")
    client._transport = httpx.MockTransport(
        lambda r: httpx.Response(status, stream=httpx.ByteStream(json.dumps(body).encode()))
    )
    with pytest.raises(m.PolicyError) as caught:
        client.send(payload())
    assert caught.value.transient is transient
    assert "secret" not in str(caught.value)


def test_policy_stream_response_bound_and_total_deadline():
    import asyncio
    import time

    m = module()

    class Stream(httpx.AsyncByteStream):
        def __init__(self, slow):
            self.slow = slow
            self.closed = False

        async def __aiter__(self):
            for _ in range(100):
                if self.slow:
                    await asyncio.sleep(0.02)
                yield b"x" * (1 if self.slow else 4096)

        async def aclose(self):
            self.closed = True

    for slow in (False, True):
        stream = Stream(slow)
        client = m.PolicyClient("https://policy.example", "secret", timeout=0.07)
        client._transport = httpx.MockTransport(
            lambda request, stream=stream: httpx.Response(202, stream=stream)
        )
        started = time.monotonic()
        with pytest.raises(m.PolicyError) as caught:
            client.send(payload())
        assert caught.value.code == ("policy_deadline" if slow else "policy_response_too_large")
        assert time.monotonic() - started < 0.3
        assert stream.closed


def test_policy_executor_stall_returns_on_deadline_without_late_submit():
    import asyncio
    import threading
    import time

    m = module()
    resolved = threading.Event()
    submissions = []

    def slow_resolve():
        time.sleep(0.35)
        resolved.set()

    async def handler(request):
        await asyncio.to_thread(slow_resolve)
        submissions.append(request)
        return httpx.Response(202, stream=httpx.ByteStream(b"{}"))

    client = m.PolicyClient("https://policy.example", "secret", timeout=0.03)
    client._transport = httpx.MockTransport(handler)
    started = time.monotonic()
    with pytest.raises(m.PolicyError) as caught:
        client.send(payload())
    elapsed = time.monotonic() - started
    assert caught.value.code == "policy_deadline"
    assert elapsed < 0.15
    assert resolved.wait(1)
    assert submissions == []
