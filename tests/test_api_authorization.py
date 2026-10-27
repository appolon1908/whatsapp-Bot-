"""Mutation permissions must be checked against the state protected by the transaction."""

from uuid import UUID

import pytest
from sqlalchemy import select

from whatsapp_bot.access import Actor
from whatsapp_bot.commerce import CommerceService
from whatsapp_bot.errors import CommerceError
from whatsapp_bot.models import Channel, Conversation, Order
from whatsapp_bot.routes.orders import transition
from whatsapp_bot.schemas import Transition


def test_agent_cannot_cancel_an_order_accepted_since_it_was_read(tenant_a, tenant_session):
    with tenant_session(tenant_a) as session:
        service = CommerceService(session, tenant_a, "test-owner")
        channel = session.scalar(select(Channel))
        chat = service.create_conversation(channel.id, "concurrent-permissions")
        session.get(Conversation, UUID(chat["id"])).assigned_to = "test-agent"
        item = service.create_product(
            {"sku": "RACE", "name": "Item", "price_minor": 500, "currency": "USD", "stock_on_hand": 1}
        )
        service.set_cart_item(UUID(chat["id"]), UUID(item["id"]), 1)
        quote = service.quote_cart(UUID(chat["id"]), "pickup")
        order = service.confirm_quote(UUID(quote["id"]), "permission-race-order")
        order_id = UUID(order["id"])

    with tenant_session(tenant_a) as agent_session:
        # Hold an ORM reference from the agent's initial read while another staff
        # transaction commits an acceptance. Mutation authorization must refresh it.
        initial = agent_session.get(Order, order_id)
        assert initial.status == "submitted"
        with tenant_session(tenant_a) as owner_session:
            CommerceService(owner_session, tenant_a, "test-owner").transition_order(order_id, "accepted")
        actor = Actor(agent_session, tenant_a, "test-agent", "agent")
        with pytest.raises(CommerceError) as rejected:
            transition(order_id, Transition(status="cancelled"), actor)
        assert rejected.value.code == "forbidden"

    with tenant_session(tenant_a) as session:
        assert session.get(Order, order_id).status == "accepted"
