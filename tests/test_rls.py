from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from whatsapp_bot.db import assert_runtime_role
from whatsapp_bot.models import Business, Channel, Conversation, Product


def test_runtime_has_no_context_and_cannot_be_owner(engine, admin_engine, tenant_a, tenant_session):
    assert_runtime_role(engine)
    with pytest.raises(RuntimeError):
        assert_runtime_role(admin_engine)
    with engine.connect() as connection:
        assert connection.execute(select(Business)).all() == []
    with tenant_session(tenant_a) as session:
        assert session.get(Business, tenant_a) is not None
    with engine.connect() as connection:
        assert connection.execute(select(Business)).all() == []
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO commerce.products (id,tenant_id,sku,name,price_minor,currency,stock_on_hand,stock_reserved,version,active) VALUES (:id,:tenant,'x','x',1,'USD',1,0,1,true)"
                ),
                {"id": uuid4(), "tenant": tenant_a},
            )


def test_cross_tenant_read_write_and_fk(engine, tenant_a, tenant_b, tenant_session):
    with tenant_session(tenant_a) as session:
        product = Product(tenant_id=tenant_a, sku="private", name="Private", price_minor=100, currency="USD")
        channel = session.scalars(select(Channel)).first()
        session.add(product)
        session.flush()
        pid, cid = product.id, channel.id
    with tenant_session(tenant_b) as session:
        assert session.get(Product, pid) is None
    with pytest.raises(DBAPIError):
        with tenant_session(tenant_b) as session:
            session.add(Conversation(tenant_id=tenant_b, channel_id=cid, customer_ref="other"))
            session.flush()
    with pytest.raises(DBAPIError):
        with tenant_session(tenant_b) as session:
            session.add(Product(tenant_id=tenant_a, sku="bad", name="bad", price_minor=1, currency="USD"))
            session.flush()


def test_pool_rollback_clears_context(engine, tenant_a, tenant_session):
    with pytest.raises(ValueError):
        with tenant_session(tenant_a):
            raise ValueError("rollback")
    with engine.connect() as connection:
        assert connection.execute(select(Business)).all() == []
