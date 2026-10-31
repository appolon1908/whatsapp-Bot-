"""Staff API regressions: identity, membership, assigned scope and committed orders."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from whatsapp_bot.auth import AuthError, Principal
from whatsapp_bot.config import Settings
from whatsapp_bot.models import Channel, Membership


class VerifiedTestIdentity:
    def is_configured(self):
        return True

    def verify(self, token):
        subjects = {"owner": "test-owner", "agent": "test-agent", "fulfillment": "test-fulfillment"}
        if token not in subjects:
            raise AuthError("invalidtoken", "Invalid access token", 401)
        return Principal(subjects[token])


def headers(tenant, token="owner"):
    return {"Authorization": f"Bearer {token}", "X-Business-ID": str(tenant)}


@pytest.fixture
def client(engine):
    from whatsapp_bot.api import create_app

    settings = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        expected_database_name=engine.url.database,
        environment="test",
        ingress_secret="isolated-test-ingress-secret-32bytes",
    )
    with TestClient(create_app(settings, engine=engine, token_verifier=VerifiedTestIdentity())) as client:
        yield client


def product(client, tenant, sku="JUICE", **changes):
    payload = {"sku": sku, "name": "Chinola juice", "price_minor": 500, "currency": "USD", "stock_on_hand": 3}
    payload.update(changes)
    response = client.post("/commerce/v1/products", json=payload, headers=headers(tenant))
    assert response.status_code == 201, response.text
    return response.json()


def conversation(client, tenant, tenant_session):
    with tenant_session(tenant) as session:
        channel_id = session.scalar(select(Channel.id))
    response = client.post(
        "/commerce/v1/conversations",
        json={"channel_id": str(channel_id), "customer_ref": "test-customer", "language": "en"},
        headers=headers(tenant),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_missing_or_invalid_identity_never_reaches_business_data(client, tenant_a):
    assert client.get("/commerce/v1/products", headers={"X-Business-ID": str(tenant_a)}).status_code == 401
    assert client.get("/commerce/v1/products", headers=headers(tenant_a, "forged")).status_code == 401


def test_tenant_header_requires_active_persisted_membership(client, business_factory):
    foreign = business_factory(owner_subject="another-owner")
    assert client.get("/commerce/v1/products", headers=headers(foreign)).status_code == 403
    assert client.get("/commerce/v1/products", headers=headers(uuid4())).status_code == 403


def test_object_ids_cannot_cross_business_boundaries(client, tenant_a, tenant_b):
    item = product(client, tenant_b)
    assert client.get(f"/commerce/v1/products/{item['id']}", headers=headers(tenant_a)).status_code == 404
    assert (
        client.patch(
            f"/commerce/v1/products/{item['id']}", json={"name": "stolen"}, headers=headers(tenant_a)
        ).status_code
        == 404
    )


def test_body_cannot_choose_tenant_or_boolean_money(client, tenant_a):
    body = {"sku": "SKU", "name": "Product", "price_minor": 10, "currency": "USD", "stock_on_hand": 1}
    assert (
        client.post(
            "/commerce/v1/products", json={**body, "tenant_id": str(uuid4())}, headers=headers(tenant_a)
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/commerce/v1/products", json={**body, "price_minor": True}, headers=headers(tenant_a)
        ).status_code
        == 422
    )


def test_agent_cannot_manage_catalog_and_only_reads_assigned_conversations(client, tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        session.add(Membership(tenant_id=tenant_a, subject="test-agent", role="agent"))
    chat = conversation(client, tenant_a, tenant_session)
    assert (
        client.get(f"/commerce/v1/conversations/{chat['id']}", headers=headers(tenant_a, "agent")).status_code
        == 404
    )
    response = client.post(
        f"/commerce/v1/conversations/{chat['id']}/assignment",
        json={"subject": "test-agent"},
        headers=headers(tenant_a),
    )
    assert response.status_code == 200, response.text
    assert (
        client.get(f"/commerce/v1/conversations/{chat['id']}", headers=headers(tenant_a, "agent")).status_code
        == 200
    )
    assert (
        client.post(
            "/commerce/v1/products",
            json={"sku": "BAD", "name": "Bad", "price_minor": 5, "currency": "USD"},
            headers=headers(tenant_a, "agent"),
        ).status_code
        == 403
    )


def test_complete_staff_order_and_idempotent_confirmation(client, tenant_a, tenant_session):
    item = product(client, tenant_a)
    chat = conversation(client, tenant_a, tenant_session)
    response = client.put(
        f"/commerce/v1/conversations/{chat['id']}/cart/items",
        json={"product_id": item["id"], "quantity": 2},
        headers=headers(tenant_a),
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/commerce/v1/quotes",
        json={"conversation_id": chat["id"], "fulfillment": "pickup"},
        headers=headers(tenant_a),
    )
    assert response.status_code == 201, response.text
    quote = response.json()
    assert quote["total_minor"] == 1000
    checkout_headers = {**headers(tenant_a), "Idempotency-Key": "one-customer-confirmation"}
    response = client.post("/commerce/v1/orders", json={"quote_id": quote["id"]}, headers=checkout_headers)
    assert response.status_code == 201, response.text
    order = response.json()
    retry = client.post("/commerce/v1/orders", json={"quote_id": quote["id"]}, headers=checkout_headers)
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] == order["id"]
    assert order["payment_status"] == "unpaid"
    for state in ("accepted", "preparing", "ready_for_pickup", "collected"):
        result = client.post(
            f"/commerce/v1/orders/{order['id']}/transitions",
            json={"status": state},
            headers=headers(tenant_a),
        )
        assert result.status_code == 200, result.text
    saved = client.get(f"/commerce/v1/products/{item['id']}", headers=headers(tenant_a)).json()
    assert saved["stock_on_hand"] == 1
    assert saved["stock_reserved"] == 0


def test_fulfillment_cannot_read_conversations_or_record_payments(client, tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        session.add(Membership(tenant_id=tenant_a, subject="test-fulfillment", role="fulfillment"))
    assert (
        client.get("/commerce/v1/conversations", headers=headers(tenant_a, "fulfillment")).status_code == 403
    )
    response = client.post(
        f"/commerce/v1/orders/{uuid4()}/payment",
        json={"status": "paid_externally", "reference": "receipt"},
        headers=headers(tenant_a, "fulfillment"),
    )
    assert response.status_code == 403


def test_last_owner_cannot_be_disabled(client, tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        member_id = session.scalar(select(Membership.id).where(Membership.subject == "test-owner"))
    response = client.patch(
        f"/commerce/v1/memberships/{member_id}", json={"active": False}, headers=headers(tenant_a)
    )
    assert response.status_code == 409, response.text
    assert client.get("/commerce/v1/products", headers=headers(tenant_a)).status_code == 200


def test_channel_activation_cannot_be_claimed_by_business_user(client, tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        channel_id = session.scalar(select(Channel.id))
    assert client.post(
        "/commerce/v1/channels",
        json={"provider": "meta", "phone_number_id": "another-business"},
        headers=headers(tenant_a),
    ).status_code in (403, 405)
    assert (
        client.patch(
            f"/commerce/v1/channels/{channel_id}", json={"active": True}, headers=headers(tenant_a)
        ).status_code
        == 422
    )
    response = client.patch(
        f"/commerce/v1/channels/{channel_id}", json={"active": False}, headers=headers(tenant_a)
    )
    assert response.status_code == 200, response.text
    assert response.json()["active"] is False


def test_privileged_database_connection_cannot_serve_staff_api(admin_engine, tenant_a):
    from whatsapp_bot.api import create_app

    settings = Settings(environment="test", ingress_secret="isolated-test-ingress-secret-32bytes")
    with TestClient(
        create_app(settings, engine=admin_engine, token_verifier=VerifiedTestIdentity())
    ) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/commerce/v1/products", headers=headers(tenant_a)).status_code == 503


def test_health_and_readiness_distinguish_configuration_from_liveness():
    from whatsapp_bot.api import create_app

    with TestClient(create_app(Settings())) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert "password" not in client.get("/readyz").text.lower()


def test_internal_ingress_rejects_unsigned_and_oversized_requests(client):
    assert client.post("/internal/v1/whatsapp/events", json={}).status_code == 401
    response = client.post("/internal/v1/whatsapp/events", content=b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413


def test_business_hours_accepted_by_api_allow_checkout(client, tenant_a, tenant_session, monkeypatch):
    from datetime import UTC, datetime

    class MondayNoon(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 12, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("whatsapp_bot.commerce.datetime", MondayNoon)
    result = client.patch(
        "/commerce/v1/business",
        json={"opening_hours": {"mon": [["09:00", "18:00"]]}},
        headers=headers(tenant_a),
    )
    assert result.status_code == 200, result.text
    item = product(client, tenant_a)
    chat = conversation(client, tenant_a, tenant_session)
    assert (
        client.put(
            f"/commerce/v1/conversations/{chat['id']}/cart/items",
            json={"product_id": item["id"], "quantity": 1},
            headers=headers(tenant_a),
        ).status_code
        == 200
    )
    quote = client.post(
        "/commerce/v1/quotes",
        json={"conversation_id": chat["id"], "fulfillment": "pickup"},
        headers=headers(tenant_a),
    )
    assert quote.status_code == 201, quote.text


@pytest.mark.parametrize("hours", [{"0": [["09:00", "18:00"]]}, {"mon": [["20:00", "05:00"]]}])
def test_invalid_hours_cannot_be_persisted_by_staff_api(client, tenant_a, hours):
    result = client.patch("/commerce/v1/business", json={"opening_hours": hours}, headers=headers(tenant_a))
    assert result.status_code == 422, result.text


@pytest.mark.parametrize("change,expected", [("tax", "quote_changed"), ("hours", "business_closed")])
def test_confirmation_rechecks_settings_changed_after_membership_read(
    client, tenant_a, tenant_session, monkeypatch, change, expected
):
    from whatsapp_bot.access import Actor
    from whatsapp_bot.models import Business

    item = product(client, tenant_a)
    chat = conversation(client, tenant_a, tenant_session)
    assert (
        client.put(
            f"/commerce/v1/conversations/{chat['id']}/cart/items",
            json={"product_id": item["id"], "quantity": 1},
            headers=headers(tenant_a),
        ).status_code
        == 200
    )
    response = client.post(
        "/commerce/v1/quotes", json={"conversation_id": chat["id"]}, headers=headers(tenant_a)
    )
    assert response.status_code == 201, response.text
    quote = response.json()
    original = Actor.quote

    def change_settings_after_dependency_read(actor, quote_id, *, lock=False):
        with tenant_session(tenant_a) as session:
            business = session.scalar(select(Business).where(Business.id == tenant_a).with_for_update())
            if change == "tax":
                business.tax_bps = 1000
            else:
                business.settings = {"opening_hours": {}}
        return original(actor, quote_id, lock=lock)

    monkeypatch.setattr(Actor, "quote", change_settings_after_dependency_read)
    response = client.post(
        "/commerce/v1/orders",
        json={"quote_id": quote["id"]},
        headers={**headers(tenant_a), "Idempotency-Key": "concurrent-business-settings"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == expected


def test_setting_tax_to_previously_cached_value_really_persists(
    client, tenant_a, tenant_session, monkeypatch
):
    from whatsapp_bot.access import Actor
    from whatsapp_bot.models import Business

    original = Actor.require

    def concurrent_update(actor, *roles):
        original(actor, *roles)
        with tenant_session(tenant_a) as session:
            session.get(Business, tenant_a).tax_bps = 1000

    monkeypatch.setattr(Actor, "require", concurrent_update)
    response = client.patch("/commerce/v1/business", json={"tax_bps": 0}, headers=headers(tenant_a))
    assert response.status_code == 200, response.text
    with tenant_session(tenant_a) as session:
        assert session.get(Business, tenant_a).tax_bps == response.json()["tax_bps"] == 0
