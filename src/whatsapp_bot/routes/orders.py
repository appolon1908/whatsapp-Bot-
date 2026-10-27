from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query
from sqlalchemy import select

from ..access import ActorDep
from ..errors import CommerceError
from ..models import Conversation, Order
from ..schemas import ConfirmOrder, Payment, QuoteInput, Transition

router = APIRouter(prefix="/commerce/v1", tags=["orders"])


@router.post("/quotes", status_code=201)
def create_quote(data: QuoteInput, actor: ActorDep):
    actor.conversation(data.conversation_id, lock=True)
    return actor.commerce.quote_cart(data.conversation_id, data.fulfillment, data.zone_code, data.address)


@router.post("/orders", status_code=201)
def confirm(
    data: ConfirmOrder,
    actor: ActorDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    actor.quote(data.quote_id, lock=True)
    return actor.commerce.confirm_quote(data.quote_id, idempotency_key)


@router.get("/orders")
def list_orders(
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    query = select(Order.id).where(Order.tenant_id == actor.tenant_id)
    if actor.role == "agent":
        query = query.join(
            Conversation,
            (Conversation.id == Order.conversation_id) & (Conversation.tenant_id == Order.tenant_id),
        ).where(Conversation.assigned_to == actor.subject)
    ids = actor.session.scalars(query.order_by(Order.created_at.desc(), Order.id).limit(limit).offset(offset))
    return [actor.commerce.get_order(value) for value in ids]


@router.get("/orders/{order_id}")
def get_order(order_id: UUID, actor: ActorDep):
    actor.order(order_id)
    return actor.commerce.get_order(order_id)


@router.post("/orders/{order_id}/transitions")
def transition(order_id: UUID, data: Transition, actor: ActorDep):
    row = actor.order(order_id, lock=True)
    if actor.role == "agent" and (data.status != "cancelled" or row.status != "submitted"):
        raise CommerceError("forbidden", "Agents can cancel their submitted orders only", 403)
    if actor.role == "fulfillment" and data.status not in {
        "preparing",
        "out_for_delivery",
        "delivered",
        "ready_for_pickup",
        "collected",
    }:
        raise CommerceError("forbidden", "This role may update fulfillment only", 403)
    return actor.commerce.transition_order(order_id, data.status)


@router.post("/orders/{order_id}/payment")
def payment(order_id: UUID, data: Payment, actor: ActorDep):
    actor.require("owner", "supervisor")
    actor.order(order_id)
    return actor.commerce.mark_payment(order_id, data.status, data.reference)
