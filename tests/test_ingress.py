import hashlib
import hmac
import importlib
import json
import time
from datetime import UTC, datetime

import pytest

SECRET = "s" * 40


def envelope(**changes):
    data = {
        "event_id": "event-1",
        "provider": "meta",
        "provider_account_id": "account",
        "phone_number_id": "phone",
        "provider_message_id": "message-1",
        "event_type": "message.received",
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload_schema_version": 1,
        "trace_id": "trace-1",
        "customer_ref": "+15555550123",
        "text": "START",
    }
    data.update(changes)
    return data


def signed(data, timestamp=None):
    body = json.dumps(data).encode()
    stamp = str(int(time.time())) if timestamp is None else timestamp
    sig = "sha256=" + hmac.new(SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, stamp, sig


def module():
    assert importlib.util.find_spec("whatsapp_bot.messaging"), "ingress implementation missing"
    return importlib.import_module("whatsapp_bot.messaging")


def test_ingress_authentication_fails_before_database_access():
    m = module()
    from whatsapp_bot.errors import CommerceError

    service = m.IngressService(None, SECRET)
    body, stamp, sig = signed(envelope())
    for args in [(body, stamp, "sha256=" + "0" * 64), signed(envelope(), "1"), (body, "nonsense", sig)]:
        with pytest.raises(CommerceError) as caught:
            service.accept(*args)
        assert caught.value.status == 401
    with pytest.raises(CommerceError) as caught:
        m.IngressService(None, "weak").accept(body, stamp, sig)
    assert caught.value.status == 503


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "spoof"},
        {"role": "owner"},
        {"text": "x" * 4097},
        {"occurred_at": "2020-01-01T00:00:00"},
        {"event_type": "unsupported"},
        {"payload_schema_version": True},
    ],
)
def test_ingress_rejects_invalid_signed_envelope(changes):
    m = module()
    from whatsapp_bot.errors import CommerceError

    with pytest.raises(CommerceError) as caught:
        m.IngressService(None, SECRET).accept(*signed(envelope(**changes)))
    assert caught.value.status == 400


def bind(tenant_session, tenant_id):
    from sqlalchemy import select

    from whatsapp_bot.models import Channel

    with tenant_session(tenant_id) as session:
        channel = session.scalar(select(Channel))
        return channel.id, channel.account_id


def test_signed_ingress_routes_and_replays_100_times(engine, tenant_a, tenant_b, tenant_session):
    from sqlalchemy import func, select

    from whatsapp_bot.errors import CommerceError
    from whatsapp_bot.models import InboundEvent

    channel, account = bind(tenant_session, tenant_a)
    data = envelope(provider_account_id=account, phone_number_id="test-phone")
    args = signed(data)
    service = module().IngressService(engine, SECRET)
    first = service.accept(*args)
    for _ in range(100):
        assert service.accept(*args) == {**first, "duplicate": True}
    with tenant_session(tenant_a) as session:
        assert session.scalar(select(func.count()).select_from(InboundEvent)) == 1
    with tenant_session(tenant_b) as session:
        assert session.scalar(select(func.count()).select_from(InboundEvent)) == 0
    with pytest.raises(CommerceError) as caught:
        service.accept(*signed({**data, "text": "STOP"}))
    assert caught.value.code == "event_collision"
    with pytest.raises(CommerceError) as caught:
        service.accept(*signed(envelope(provider_account_id="unknown")))
    assert caught.value.code == "unknown_channel"


@pytest.mark.parametrize("changes", [{"provider": []}, {"event_type": {}}, {"occurred_at": 123}])
def test_malformed_field_types_are_safe_400(changes):
    from whatsapp_bot.errors import CommerceError

    with pytest.raises(CommerceError) as caught:
        module().IngressService(None, SECRET).accept(*signed(envelope(**changes)))
    assert caught.value.status == 400


def test_non_ascii_signature_is_401():
    from whatsapp_bot.errors import CommerceError

    body, stamp, _ = signed(envelope())
    with pytest.raises(CommerceError) as caught:
        module().IngressService(None, SECRET).accept(body, stamp, "sha256=ñ")
    assert caught.value.status == 401


def test_delivery_signed_correlation_fields_are_strict():
    from uuid import uuid4

    from whatsapp_bot.errors import CommerceError
    from whatsapp_bot.messaging import validate_envelope

    data = envelope(
        event_type="message.delivery.updated",
        status="delivered",
        command_id=str(uuid4()),
        operation_id="operation-1",
    )
    data.pop("text")
    data.pop("customer_ref")
    assert validate_envelope(json.dumps(data).encode())["command_id"] == data["command_id"]
    with pytest.raises(CommerceError):
        validate_envelope(json.dumps({**data, "command_id": "bad"}).encode())
