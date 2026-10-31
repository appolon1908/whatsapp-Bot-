"""Bounded, tenant-scoped durable queue worker with a policy-only dispatcher."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, exists, select, tuple_
from sqlalchemy.orm import aliased

from .bot import process_inbound, queue_reply
from .commerce import CommerceService
from .db import assert_runtime_role, tenant_session
from .errors import CommerceError
from .messaging import inbox_identity_lock
from .models import Business, Channel, Conversation, InboundEvent, Message, Order, OutboxEvent
from .policy import PolicyError, validate_acknowledgement


def _stream_key(cls):
    return case(
        (cls.payload["event_type"].astext == "message.received", cls.payload["customer_ref"].astext),
        else_=cls.payload["provider_message_id"].astext,
    )


def _has_predecessor(cls):
    earlier = aliased(InboundEvent)
    return exists(
        select(earlier.id).where(
            earlier.tenant_id == cls.tenant_id,
            earlier.channel_id == cls.channel_id,
            earlier.state == "pending",
            earlier.payload["event_type"].astext == cls.payload["event_type"].astext,
            _stream_key(earlier) == _stream_key(cls),
            tuple_(earlier.created_at, earlier.id) < tuple_(cls.created_at, cls.id),
        )
    )


class Worker:
    BATCH_SIZE = 25

    def __init__(
        self, engine, *, tenant_ids: list[UUID], policy_client=None, outbound_enabled=False, max_attempts=8
    ):
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be 1..100")
        assert_runtime_role(engine)
        self.engine = engine
        self.tenant_ids = list(dict.fromkeys(tenant_ids))
        self.policy_client = policy_client
        self.outbound_enabled = outbound_enabled
        self.max_attempts = max_attempts
        self._cursor = 0

    def tick(self) -> dict:
        counts = {"inbox": 0, "outbox": 0, "expired": 0}
        tenants = self.tenant_ids[self._cursor :] + self.tenant_ids[: self._cursor]
        if tenants:
            self._cursor = (self._cursor + 1) % len(tenants)
        for tenant_id in tenants:
            for cls, kind in [(InboundEvent, "inbox"), (OutboxEvent, "outbox")]:
                with tenant_session(self.engine, tenant_id) as session:
                    ids = session.scalars(
                        select(cls.id)
                        .where(
                            cls.tenant_id == tenant_id,
                            cls.state == "pending",
                            cls.next_attempt_at <= datetime.now(UTC),
                            ~_has_predecessor(cls) if cls is InboundEvent else True,
                        )
                        .order_by(cls.created_at, cls.id)
                        .limit(self.BATCH_SIZE)
                    ).all()
                for event_id in ids:
                    counts[kind] += self._process(tenant_id, cls, event_id)
            with tenant_session(self.engine, tenant_id) as session:
                counts["expired"] += CommerceService(session, tenant_id, "worker").expire_reservations()
        return counts

    def _process(self, tenant_id, cls, event_id):
        with tenant_session(self.engine, tenant_id) as session:
            # Commerce locks conversation before outbox. Follow that order to avoid takeover deadlocks.
            conversation = None
            if cls is InboundEvent:
                candidate = session.scalar(
                    select(InboundEvent).where(
                        InboundEvent.tenant_id == tenant_id, InboundEvent.id == event_id
                    )
                )
                if candidate is None or not inbox_identity_lock(
                    session, tenant_id, candidate.channel_id, candidate.payload
                ):
                    return 0
                if session.scalar(
                    select(_has_predecessor(InboundEvent)).where(
                        InboundEvent.tenant_id == tenant_id, InboundEvent.id == event_id
                    )
                ):
                    return 0
            if cls is OutboxEvent:
                conversation_id = session.scalar(
                    select(OutboxEvent.conversation_id).where(
                        OutboxEvent.tenant_id == tenant_id, OutboxEvent.id == event_id
                    )
                )
                conversation = session.scalar(
                    select(Conversation)
                    .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
                    .with_for_update(skip_locked=True)
                )
                if conversation is None:
                    return 0
            event = session.scalar(
                select(cls)
                .where(
                    cls.tenant_id == tenant_id,
                    cls.id == event_id,
                    cls.state == "pending",
                    cls.next_attempt_at <= datetime.now(UTC),
                )
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return 0
            try:
                with session.begin_nested():
                    if cls is InboundEvent:
                        process_inbound(session, tenant_id, event)
                        event.state = "done"
                    elif event.kind == "message.reply":
                        self._dispatch(session, tenant_id, conversation, event)
                    else:
                        self._business_event(session, tenant_id, conversation, event)
                    session.flush()
            except Exception as exc:
                self._failure(event, exc)
            return 1

    def _failure(self, event, exc):
        event.attempts += 1
        # Only predefined machine codes; never persist exception text, SQL, tokens or customer content.
        known = {
            "policy_unavailable",
            "policy_rejected",
            "policy_ack_invalid",
            "policy_deadline",
            "policy_response_too_large",
            "delivery_correlation_conflict",
            "delivery_message_pending",
            "message_collision",
            "event_scope_invalid",
            "business_event_invalid",
        }
        code = (
            exc.code
            if isinstance(exc, (CommerceError, PolicyError)) and exc.code in known
            else "processing_failed"
        )
        event.last_error = code
        permanent = isinstance(exc, PolicyError) and not exc.transient
        event.state = "dead" if permanent or event.attempts >= self.max_attempts else "pending"
        event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(3600, 2 ** min(event.attempts, 12)))

    def _business_event(self, session, tenant_id, conversation, event):
        if event.kind not in {"order.submitted", "order.updated"}:
            raise CommerceError("business_event_invalid", "Unsupported business event")
        try:
            order_id = UUID(event.payload["order_id"])
        except (KeyError, TypeError, ValueError):
            raise CommerceError("business_event_invalid", "Invalid order event") from None
        order = session.scalar(
            select(Order).where(
                Order.tenant_id == tenant_id, Order.id == order_id, Order.conversation_id == conversation.id
            )
        )
        if order is None:
            raise CommerceError("business_event_invalid", "Order unavailable")
        state = "submitted" if event.kind == "order.submitted" else event.payload.get("status", order.status)
        label = "Pedido" if conversation.language == "es" else "Order"
        queue_reply(
            session,
            tenant_id,
            conversation,
            f"business:{event.id}:reply",
            f"{label} {order.id}: {state}. {order.currency} {order.total_minor // 100}.{order.total_minor % 100:02d}",
        )
        event.state = "done"

    def _dispatch(self, session, tenant_id, conversation, event):
        # Conversation lock is held across the bounded submission: takeover is serialized against send.
        if (
            conversation.mode != "bot"
            or conversation.consent_status != "opted_in"
            or conversation.version != event.conversation_version
        ):
            event.state = "cancelled"
            event.last_error = "conversation_ineligible"
            return
        channel = session.scalar(
            select(Channel)
            .where(Channel.tenant_id == tenant_id, Channel.id == conversation.channel_id)
            .with_for_update()
        )
        business = session.scalar(select(Business).where(Business.id == tenant_id).with_for_update())
        reason = None
        if not self.outbound_enabled:
            reason = "outbound_disabled"
        elif not self.policy_client:
            reason = "policy_unconfigured"
        elif (
            not business
            or not business.active
            or not channel
            or not channel.active
            or not channel.verification_reference
        ):
            reason = "channel_unready"
        elif not channel.policy_campaign_id:
            reason = "campaign_unconfigured"
        if reason:
            event.last_error = reason
            event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=30)
            return
        payload = {
            "tenant_id": str(tenant_id),
            "requested_by": "commerce-worker",
            "command_id": str(event.command_id),
            "correlation_id": event.payload["correlation_id"],
            "idempotency_key": event.payload["idempotency_key"],
            "recipient": conversation.customer_ref,
            "instance_id": channel.account_id,
            "phone_number_id": channel.phone_number_id,
            "provider": channel.provider,
            "message": {"type": "text", "text": event.payload["text"]},
            "consent_verified": True,
            "opted_out": False,
            "campaign_id": channel.policy_campaign_id,
            "business_context": {
                "conversation_id": str(conversation.id),
                "source": "commerce",
                "consent_evidence": conversation.consent_evidence,
            },
        }
        result = validate_acknowledgement(self.policy_client.send(payload), str(event.command_id))
        event.policy_operation_id = result["operation_id"]
        event.state = "sent"
        event.last_error = None
        provider_id = result.get("provider_message_id")
        if provider_id is not None and (not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 200):
            raise PolicyError("policy_ack_invalid", transient=True)
        session.add(
            Message(
                id=event.command_id,
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                direction="outbound",
                text=event.payload["text"],
                status="sent",
                provider_message_id=provider_id,
            )
        )
