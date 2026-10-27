from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select

from whatsapp_bot.commerce import CommerceService
from whatsapp_bot.errors import CommerceError
from whatsapp_bot.models import Channel, Order, Product


def prepare(tenant, session_factory, units=1):
    with session_factory(tenant) as session:
        service = CommerceService(session, tenant, "test-owner")
        channel = session.scalar(select(Channel))
        product = service.create_product(
            {
                "sku": str(uuid4()),
                "name": "Last coffee",
                "currency": "USD",
                "price_minor": 100,
                "stock_on_hand": units,
            }
        )
        quotes = []
        for _ in range(2):
            conversation = service.create_conversation(channel.id, str(uuid4()))
            service.set_cart_item(conversation["id"], product["id"], 1)
            quotes.append(service.quote_cart(conversation["id"])["id"])
        return product["id"], quotes


@pytest.mark.concurrency
def test_two_shoppers_cannot_buy_last_unit(tenant_a, tenant_session):
    product, quotes = prepare(tenant_a, tenant_session)
    barrier = Barrier(2)

    def confirm(quote):
        barrier.wait(timeout=10)
        try:
            with tenant_session(tenant_a) as session:
                return CommerceService(session, tenant_a, "worker").confirm_quote(quote, str(uuid4()))["id"]
        except CommerceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, quotes))
    assert results.count("insufficient_stock") == 1
    with tenant_session(tenant_a) as session:
        assert session.get(Product, product).stock_reserved == 1
        assert len(session.scalars(select(Order)).all()) == 1


@pytest.mark.concurrency
def test_concurrent_same_quote_different_keys_creates_one_order(tenant_a, tenant_session):
    product, quotes = prepare(tenant_a, tenant_session)
    barrier = Barrier(2)

    def confirm(key):
        barrier.wait(timeout=10)
        with tenant_session(tenant_a) as session:
            return CommerceService(session, tenant_a, "worker").confirm_quote(quotes[0], key)["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, ["first", "second"]))
    assert results[0] == results[1]
    with tenant_session(tenant_a) as session:
        assert session.get(Product, product).stock_reserved == 1
        assert len(session.scalars(select(Order)).all()) == 1


@pytest.mark.concurrency
def test_same_key_for_different_quotes_conflicts_without_stock_leak(tenant_a, tenant_session):
    product, quotes = prepare(tenant_a, tenant_session, units=2)
    barrier = Barrier(2)

    def confirm(quote):
        barrier.wait(timeout=10)
        try:
            with tenant_session(tenant_a) as session:
                return CommerceService(session, tenant_a, "worker").confirm_quote(quote, "same-key")["id"]
        except CommerceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, quotes))
    assert results.count("idempotency_conflict") == 1
    with tenant_session(tenant_a) as session:
        assert session.get(Product, product).stock_reserved == 1
        assert len(session.scalars(select(Order)).all()) == 1
