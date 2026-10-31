from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ..access import ActorDep, MembershipActorDep
from ..errors import CommerceError
from ..models import AuditEvent, Business, Channel, InboundEvent, Membership, OutboxEvent
from ..schemas import BusinessUpdate, ChannelDisable, MembershipCreate, MembershipUpdate

router = APIRouter(prefix="/commerce/v1", tags=["business administration"])


def member_dict(row):
    return {"id": str(row.id), "subject": row.subject, "role": row.role, "active": row.active}


def business_dict(row):
    return {
        "id": str(row.id),
        "name": row.name,
        "currency": row.currency,
        "timezone": row.timezone,
        "tax_bps": row.tax_bps,
        "settings": row.settings,
        "active": row.active,
    }


@router.get("/business")
def business(actor: ActorDep):
    return business_dict(actor.session.get(Business, actor.tenant_id))


@router.patch("/business")
def update_business(data: BusinessUpdate, actor: ActorDep):
    actor.require("owner")
    row = actor.session.scalar(
        select(Business)
        .where(Business.id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    changes = data.model_dump(exclude_unset=True)
    if "opening_hours" in changes:
        hours = changes.pop("opening_hours")
        row.settings = (
            {**row.settings, "opening_hours": hours}
            if hours is not None
            else {key: value for key, value in row.settings.items() if key != "opening_hours"}
        )
    for key, value in changes.items():
        if value is None:
            raise CommerceError("invalid_settings", "Business settings cannot be null", 422)
        setattr(row, key, value)
    actor.audit("business.updated", "business", actor.tenant_id, fields=sorted(data.model_fields_set))
    actor.session.flush()
    return business_dict(row)


@router.get("/memberships")
def memberships(
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    actor.require("owner")
    rows = actor.session.scalars(
        select(Membership)
        .where(Membership.tenant_id == actor.tenant_id)
        .order_by(Membership.id)
        .limit(limit)
        .offset(offset)
    )
    return [member_dict(row) for row in rows]


@router.post("/memberships", status_code=201)
def create_member(data: MembershipCreate, actor: MembershipActorDep):
    actor.require("owner")
    row = Membership(tenant_id=actor.tenant_id, **data.model_dump())
    actor.session.add(row)
    actor.session.flush()
    actor.audit("membership.created", "membership", row.id, subject=row.subject, role=row.role)
    return member_dict(row)


@router.patch("/memberships/{membership_id}")
def update_member(membership_id: UUID, data: MembershipUpdate, actor: MembershipActorDep):
    actor.require("owner")
    # Serialize concurrent attempts to remove the last active owner.
    actor.session.scalar(select(Business).where(Business.id == actor.tenant_id).with_for_update())
    row = actor.session.scalar(
        select(Membership)
        .where(Membership.tenant_id == actor.tenant_id, Membership.id == membership_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise CommerceError("not_found", "Membership not found", 404)
    changes = data.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise CommerceError("invalid_membership", "Membership fields cannot be null", 422)
    losing_owner = changes.get("active", row.active) is False or changes.get("role", row.role) != "owner"
    if row.active and row.role == "owner" and losing_owner:
        count = actor.session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == actor.tenant_id,
                Membership.role == "owner",
                Membership.active.is_(True),
            )
        )
        if count <= 1:
            raise CommerceError("last_owner", "Keep at least one active business owner")
    for key, value in changes.items():
        setattr(row, key, value)
    actor.audit("membership.updated", "membership", row.id, changes=changes)
    actor.session.flush()
    return member_dict(row)


@router.get("/channels")
def channels(actor: ActorDep):
    actor.require("owner", "supervisor")
    rows = actor.session.scalars(
        select(Channel).where(Channel.tenant_id == actor.tenant_id).order_by(Channel.id)
    )
    return [
        {
            "id": str(row.id),
            "provider": row.provider,
            "account_id": row.account_id,
            "phone_number_id": row.phone_number_id,
            "active": row.active,
            "verified": bool(row.verification_reference),
            "outbound_configured": bool(row.policy_campaign_id),
        }
        for row in rows
    ]


@router.patch("/channels/{channel_id}")
def disable_channel(channel_id: UUID, data: ChannelDisable, actor: ActorDep):
    actor.require("owner")
    row = actor.session.scalar(
        select(Channel)
        .where(Channel.tenant_id == actor.tenant_id, Channel.id == channel_id)
        .with_for_update()
    )
    if row is None:
        raise CommerceError("not_found", "Channel not found", 404)
    row.active = data.active
    actor.audit("channel.disabled", "channel", channel_id)
    actor.session.flush()
    return {"id": str(row.id), "active": row.active}


@router.get("/queue")
def queue(actor: ActorDep):
    actor.require("owner", "supervisor")

    def counts(model):
        return {
            state: count
            for state, count in actor.session.execute(
                select(model.state, func.count())
                .where(model.tenant_id == actor.tenant_id)
                .group_by(model.state)
            )
        }

    return {"inbox": counts(InboundEvent), "outbox": counts(OutboxEvent)}


@router.get("/queue/{kind}/dead")
def dead_letters(
    kind: Literal["inbox", "outbox"], actor: ActorDep, limit: Annotated[int, Query(ge=1, le=100)] = 100
):
    actor.require("owner", "supervisor")
    model = InboundEvent if kind == "inbox" else OutboxEvent
    rows = actor.session.scalars(
        select(model)
        .where(model.tenant_id == actor.tenant_id, model.state == "dead")
        .order_by(model.created_at, model.id)
        .limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "attempts": row.attempts,
            "last_error": row.last_error,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.post("/queue/{kind}/{event_id}/replay")
def replay(kind: Literal["inbox", "outbox"], event_id: UUID, actor: ActorDep):
    actor.require("owner", "supervisor")
    model = InboundEvent if kind == "inbox" else OutboxEvent
    row = actor.session.scalar(
        select(model).where(model.tenant_id == actor.tenant_id, model.id == event_id).with_for_update()
    )
    if row is None:
        raise CommerceError("not_found", "Queue record not found", 404)
    if row.state != "dead":
        raise CommerceError("not_dead_letter", "Only dead-letter records can be replayed")
    row.state, row.attempts, row.last_error = "pending", 0, None
    row.next_attempt_at = datetime.now(UTC)
    actor.audit("queue.replayed", kind, row.id)
    return {"id": str(row.id), "state": row.state}


@router.get("/audit")
def audit(
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    actor.require("owner")
    rows = actor.session.scalars(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == actor.tenant_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return [
        {
            "id": str(row.id),
            "actor": row.actor,
            "action": row.action,
            "object_type": row.object_type,
            "object_id": str(row.object_id) if row.object_id else None,
            "details": row.details,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
