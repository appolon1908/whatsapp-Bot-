from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from whatsapp_bot.commerce import CommerceService
from whatsapp_bot.errors import CommerceError
from whatsapp_bot.models import AuditEvent, Channel, Order, OutboxEvent, Product, Quote


def shop(session, tenant, stock=2):
    channel = session.scalars(select(Channel).where(Channel.tenant_id == tenant)).first()
    service = CommerceService(session, tenant, "test-owner")
    conversation = service.create_conversation(channel.id, str(uuid4()))
    product = service.create_product(
        {"sku": str(uuid4()), "name": "Coffee", "price_minor": 105, "currency": "USD", "stock_on_hand": stock}
    )
    service.set_cart_item(conversation["id"], product["id"], 1)
    quote = service.quote_cart(conversation["id"])
    return service, conversation, product, quote


def test_confirm_idempotency_release_audit(tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        service, conversation, product, quote = shop(session, tenant_a)
        order = service.confirm_quote(quote["id"], "checkout-1")
        assert order["total_minor"] == 105
        assert service.confirm_quote(quote["id"], "checkout-1")["id"] == order["id"]
        assert service.confirm_quote(quote["id"], "checkout-2")["id"] == order["id"]
        assert session.get(Product, product["id"]).stock_reserved == 1
        service.mark_payment(order["id"], "paid_externally", "manual-ref")
        service.transition_order(order["id"], "cancelled")
        assert session.get(Product, product["id"]).stock_reserved == 0
        assert session.scalars(select(AuditEvent).where(AuditEvent.action == "payment.recorded")).first()
        assert len(session.scalars(select(Order)).all()) == 1
        assert (
            len(session.scalars(select(OutboxEvent).where(OutboxEvent.kind == "order.submitted")).all()) == 1
        )


def test_stale_expired_quote_and_rollback(tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        service, conversation, product, quote = shop(session, tenant_a)
        service.update_product(product["id"], {"price_minor": 106})
        with pytest.raises(CommerceError, match="changed"):
            service.confirm_quote(quote["id"], "changed")
        fresh = service.quote_cart(conversation["id"])
        session.get(Quote, fresh["id"]).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(CommerceError, match="expired"):
            service.confirm_quote(fresh["id"], "expired")
    with pytest.raises(RuntimeError):
        with tenant_session(tenant_a) as session:
            service, conversation, product, quote = shop(session, tenant_a)
            service.confirm_quote(quote["id"], "rolled-back")
            raise RuntimeError("force rollback")
    with tenant_session(tenant_a) as session:
        assert not session.scalars(select(Order).where(Order.idempotency_key == "rolled-back")).all()


def test_reservation_expiry_and_final_consumption(tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        service, conversation, product, quote = shop(session, tenant_a)
        order = service.confirm_quote(quote["id"], "expiry")
        assert service.expire_reservations(datetime.now(UTC) + timedelta(minutes=31)) == 1
        assert service.expire_reservations(datetime.now(UTC) + timedelta(minutes=32)) == 0
        assert session.get(Product, product["id"]).stock_reserved == 0
        service, conversation, product, quote = shop(session, tenant_a)
        order = service.confirm_quote(quote["id"], "consume")
        service.transition_order(order["id"], "accepted")
        assert service.expire_reservations(datetime.now(UTC) + timedelta(days=1)) == 0
        service.transition_order(order["id"], "preparing")
        service.transition_order(order["id"], "ready_for_pickup")
        service.transition_order(order["id"], "collected")
        assert session.get(Product, product["id"]).stock_on_hand == 1
        assert session.get(Product, product["id"]).stock_reserved == 0


def test_round_half_up_delivery_minimum_hours_and_cart_invalidation(business_factory, tenant_session):
    tenant = business_factory(tax_bps=1000)
    with tenant_session(tenant) as session:
        service, conversation, product, quote = shop(session, tenant)
        assert quote["tax_minor"] == 11  # 10.5 cents rounds up, never banker's rounding.
        service.create_zone({"code": "central", "name": "Central", "fee_minor": 10, "minimum_minor": 200})
        with pytest.raises(CommerceError, match="minimum"):
            service.quote_cart(conversation["id"], "delivery", "central", "Test address")
        service.set_cart_item(conversation["id"], product["id"], 2)
        with pytest.raises(CommerceError, match="changed"):
            service.confirm_quote(quote["id"], "stale-cart")
        delivery = service.quote_cart(conversation["id"], "delivery", "central", "Test address")
        assert delivery["subtotal_minor"] == 210
        assert delivery["delivery_minor"] == 10
        assert delivery["tax_minor"] == 22
        assert delivery["total_minor"] == 242
    closed = business_factory(settings={"opening_hours": {}})
    with tenant_session(closed) as session:
        service = CommerceService(session, closed, "test-owner")
        channel = session.scalar(select(Channel))
        convo = service.create_conversation(channel.id, "closed-shopper")
        p = service.create_product(
            {"sku": "closed", "name": "Closed", "price_minor": 5, "currency": "USD", "stock_on_hand": 1}
        )
        service.set_cart_item(convo["id"], p["id"], 1)
        with pytest.raises(CommerceError, match="closed"):
            service.quote_cart(convo["id"])


def test_snapshot_unchanged_stock_adjustment_and_invalid_transition(tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        service, conversation, product, quote = shop(session, tenant_a)
        order = service.confirm_quote(quote["id"], "snapshot")
        service.update_product(product["id"], {"price_minor": 999, "name": "Renamed"})
        assert service.get_order(order["id"])["items"][0]["unit_price_minor"] == 105
        assert service.get_order(order["id"])["items"][0]["name"] == "Coffee"
        with pytest.raises(CommerceError, match="Stock adjustment"):
            service.adjust_stock(product["id"], -2)
        with pytest.raises(CommerceError, match="transition"):
            service.transition_order(order["id"], "delivered")
        with pytest.raises(CommerceError, match="reference"):
            service.mark_payment(order["id"], "paid_externally", "")


def test_cross_tenant_service_ids_are_not_found(tenant_a, tenant_b, tenant_session):
    with tenant_session(tenant_a) as session:
        _, conversation, product, quote = shop(session, tenant_a)
    with tenant_session(tenant_b) as session:
        service = CommerceService(session, tenant_b, "test-owner")
        for call in (
            lambda: service.get_product(product["id"]),
            lambda: service.get_conversation(conversation["id"]),
            lambda: service.confirm_quote(quote["id"], "foreign"),
        ):
            with pytest.raises(CommerceError) as caught:
                call()
            assert caught.value.status == 404


def test_staff_consent_records_atomic_ordering_baseline(tenant_a, tenant_session):
    from whatsapp_bot.models import Conversation

    with tenant_session(tenant_a) as session:
        service, conversation, _, _ = shop(session, tenant_a)
        row = session.get(Conversation, conversation["id"])
        row.bot_state = {
            "consent_occurred_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "menu": "preserved",
        }
        previous = row.version
        result = service.set_consent(conversation["id"], "opted_out", "staff-request")
        assert result["bot_state"]["consent_occurred_at"] == result["consent_at"]
        assert result["bot_state"]["menu"] == "preserved"
        assert result["version"] == previous + 1


def test_accepted_alternate_confirmation_key_cannot_be_rebound(tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        service, _, _, first = shop(session, tenant_a)
        order = service.confirm_quote(first["id"], "original-key")
        assert service.confirm_quote(first["id"], "alias-key")["id"] == order["id"]
        _, _, _, second = shop(session, tenant_a)
        with pytest.raises(CommerceError) as caught:
            service.confirm_quote(second["id"], "alias-key")
        assert caught.value.code == "idempotency_conflict"
