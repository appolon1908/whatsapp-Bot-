from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from ..access import ActorDep
from ..errors import CommerceError
from ..models import Conversation, Membership, Message
from ..schemas import Assignment, CartItemInput, Consent, ConversationCreate

router = APIRouter(prefix="/commerce/v1/conversations", tags=["conversations"])


@router.get("")
def list_conversations(
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    actor.require("owner", "supervisor", "agent")
    query = select(Conversation.id).where(Conversation.tenant_id == actor.tenant_id)
    if actor.role == "agent":
        query = query.where(Conversation.assigned_to == actor.subject)
    ids = actor.session.scalars(
        query.order_by(Conversation.created_at.desc(), Conversation.id).limit(limit).offset(offset)
    ).all()
    return [actor.commerce.get_conversation(value) for value in ids]


@router.post("", status_code=201)
def create_conversation(data: ConversationCreate, actor: ActorDep):
    actor.require("owner", "supervisor")
    return actor.commerce.create_conversation(data.channel_id, data.customer_ref, data.language)


@router.get("/{conversation_id}")
def get_conversation(conversation_id: UUID, actor: ActorDep):
    actor.conversation(conversation_id)
    return actor.commerce.get_conversation(conversation_id)


@router.get("/{conversation_id}/messages")
def list_messages(
    conversation_id: UUID,
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    actor.conversation(conversation_id)
    rows = actor.session.scalars(
        select(Message)
        .where(Message.tenant_id == actor.tenant_id, Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id)
        .limit(limit)
        .offset(offset)
    )
    return [
        {
            "id": str(row.id),
            "direction": row.direction,
            "text": row.text,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.post("/{conversation_id}/assignment")
def assign(conversation_id: UUID, data: Assignment, actor: ActorDep):
    actor.require("owner", "supervisor")
    row = actor.conversation(conversation_id, lock=True)
    if data.subject is not None:
        member = actor.session.scalar(
            select(Membership.id).where(
                Membership.tenant_id == actor.tenant_id,
                Membership.subject == data.subject,
                Membership.active.is_(True),
                Membership.role.in_(("owner", "supervisor", "agent")),
            )
        )
        if member is None:
            raise CommerceError("invalid_assignee", "Assignee must be active business staff", 422)
    row.assigned_to = data.subject
    actor.audit("conversation.assigned", "conversation", conversation_id, subject=data.subject)
    actor.session.flush()
    return actor.commerce.get_conversation(conversation_id)


@router.post("/{conversation_id}/handover")
def handover(conversation_id: UUID, actor: ActorDep):
    row = actor.conversation(conversation_id, lock=True)
    return actor.commerce.set_mode(conversation_id, "human", assigned_to=row.assigned_to or actor.subject)


@router.post("/{conversation_id}/resume")
def resume(conversation_id: UUID, actor: ActorDep):
    row = actor.conversation(conversation_id, lock=True)
    return actor.commerce.set_mode(conversation_id, "bot", assigned_to=row.assigned_to)


@router.post("/{conversation_id}/consent")
def consent(conversation_id: UUID, data: Consent, actor: ActorDep):
    actor.require("owner", "supervisor")
    actor.conversation(conversation_id)
    return actor.commerce.set_consent(conversation_id, data.status, data.evidence)


@router.get("/{conversation_id}/cart")
def cart(conversation_id: UUID, actor: ActorDep):
    actor.conversation(conversation_id)
    return actor.commerce.get_cart(conversation_id)


@router.put("/{conversation_id}/cart/items")
def cart_item(conversation_id: UUID, data: CartItemInput, actor: ActorDep):
    actor.conversation(conversation_id, lock=True)
    return actor.commerce.set_cart_item(conversation_id, data.product_id, data.quantity)
