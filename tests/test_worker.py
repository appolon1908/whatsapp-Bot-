from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_bot import inbound, setup_shop


def test_worker_default_disabled_then_handover_cancels(tenant_a, tenant_session, engine):
    from whatsapp_bot.commerce import CommerceService
    from whatsapp_bot.models import Conversation, OutboxEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
    Worker(engine, tenant_ids=[tenant_a]).tick()
    with tenant_session(tenant_a) as session:
        event = session.scalar(select(OutboxEvent))
        assert event.state == "pending"
        assert event.last_error == "outbound_disabled"
        conversation = session.scalar(select(Conversation))
        CommerceService(session, tenant_a, "worker").set_mode(conversation.id, "human")
    Worker(engine, tenant_ids=[tenant_a], outbound_enabled=True, policy_client=FailIfCalled()).tick()
    with tenant_session(tenant_a) as session:
        assert session.scalar(select(OutboxEvent)).state == "cancelled"


class FailIfCalled:
    def send(self, payload):
        raise AssertionError("unexpected outbound effect")


class FakePolicy:
    def __init__(self, failures=0):
        self.payloads = []
        self.failures = failures

    def send(self, payload):
        from whatsapp_bot.policy import PolicyError

        self.payloads.append(payload)
        if len(self.payloads) <= self.failures:
            raise PolicyError("policy_unavailable", transient=True)
        return {
            "operation_id": "operation-1",
            "command_id": payload["command_id"],
            "provider_message_id": "provider-outbound-1",
        }


def force_due(tenant_session, tenant_id):
    from whatsapp_bot.models import OutboxEvent

    with tenant_session(tenant_id) as session:
        for event in session.scalars(select(OutboxEvent).where(OutboxEvent.state == "pending")):
            event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


def test_retry_dead_letter_replay_preserves_ids(tenant_a, tenant_session, engine, admin_engine):
    from whatsapp_bot.models import OutboxEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
    from whatsapp_bot.db import tenant_session as scoped_session
    from whatsapp_bot.models import Channel

    with scoped_session(admin_engine, tenant_a) as session:
        session.scalar(
            select(Channel).where(Channel.tenant_id == tenant_a)
        ).policy_campaign_id = "test-campaign"
    policy = FakePolicy(2)
    worker = Worker(
        engine, tenant_ids=[tenant_a], policy_client=policy, outbound_enabled=True, max_attempts=2
    )
    worker.tick()
    force_due(tenant_session, tenant_a)
    worker.tick()
    with tenant_session(tenant_a) as session:
        event = session.scalar(select(OutboxEvent))
        assert event.state == "dead"
        command_id = event.command_id
        event.state = "pending"
        event.attempts = 0
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    worker.tick()
    with tenant_session(tenant_a) as session:
        event = session.scalar(select(OutboxEvent))
        assert event.state == "sent"
        assert event.command_id == command_id
        assert event.policy_operation_id == "operation-1"
    assert len({p["command_id"] for p in policy.payloads}) == 1
    assert len({p["idempotency_key"] for p in policy.payloads}) == 1


def test_inbox_failure_rolls_back_message_and_records_dead_letter(
    tenant_a, tenant_session, engine, monkeypatch
):
    import whatsapp_bot.worker as worker_module
    from whatsapp_bot.models import InboundEvent, Message
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        event = InboundEvent(
            tenant_id=tenant_a,
            channel_id=channel,
            event_id=str(uuid4()),
            payload_hash="a" * 64,
            payload={
                "event_type": "message.received",
                "provider_message_id": str(uuid4()),
                "customer_ref": "x",
                "text": "START",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        session.add(event)
    original = worker_module.process_inbound

    def crash(session, tenant_id, event):
        original(session, tenant_id, event)
        raise RuntimeError("sensitive body must never be logged")

    monkeypatch.setattr(worker_module, "process_inbound", crash)
    Worker(engine, tenant_ids=[tenant_a], max_attempts=1).tick()
    with tenant_session(tenant_a) as session:
        assert session.scalar(select(Message)) is None
        event = session.scalar(select(InboundEvent))
        assert event.state == "dead"
        assert event.attempts == 1
        assert event.last_error == "processing_failed"


def test_worker_contract_available():
    import importlib.util

    assert importlib.util.find_spec("whatsapp_bot.worker"), "durable worker implementation missing"


@pytest.mark.parametrize("mutation", ["opted_out", "stale", "campaign"])
def test_dispatch_rechecks_current_eligibility(tenant_a, tenant_session, engine, mutation):
    from whatsapp_bot.models import Conversation, OutboxEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
        conversation = session.scalar(select(Conversation))
        if mutation == "opted_out":
            conversation.consent_status = "opted_out"
        if mutation == "stale":
            conversation.version += 1
    Worker(engine, tenant_ids=[tenant_a], policy_client=FailIfCalled(), outbound_enabled=True).tick()
    with tenant_session(tenant_a) as session:
        event = session.scalar(select(OutboxEvent))
        assert event.state == ("pending" if mutation == "campaign" else "cancelled")
        assert event.last_error == (
            "campaign_unconfigured" if mutation == "campaign" else "conversation_ineligible"
        )


def test_handover_serializes_against_inflight_submission(tenant_a, tenant_session, engine, admin_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from whatsapp_bot.commerce import CommerceService
    from whatsapp_bot.db import tenant_session as scoped_session
    from whatsapp_bot.models import Channel, Conversation, OutboxEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with scoped_session(admin_engine, tenant_a) as session:
        session.scalar(
            select(Channel).where(Channel.tenant_id == tenant_a)
        ).policy_campaign_id = "test-campaign"
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
        conversation_id = session.scalar(select(Conversation.id))
    entered, release, takeover_started = Event(), Event(), Event()

    class BlockingPolicy:
        def send(self, payload):
            entered.set()
            assert release.wait(5)
            return {"operation_id": "op", "command_id": payload["command_id"]}

    def takeover():
        takeover_started.set()
        with tenant_session(tenant_a) as session:
            CommerceService(session, tenant_a, "worker").set_mode(conversation_id, "human")

    worker = Worker(engine, tenant_ids=[tenant_a], policy_client=BlockingPolicy(), outbound_enabled=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(worker.tick)
        assert entered.wait(5)
        handover = pool.submit(takeover)
        assert takeover_started.wait(5)
        # The policy call is in progress under the row lock; takeover cannot finish yet.
        assert not handover.done()
        release.set()
        sending.result(timeout=10)
        handover.result(timeout=10)
    with tenant_session(tenant_a) as session:
        assert session.scalar(select(Conversation)).mode == "human"
        assert session.scalar(select(OutboxEvent)).state == "sent"


def test_order_event_generates_one_acknowledgement(tenant_a, tenant_session, engine):
    from whatsapp_bot.models import OutboxEvent, Quote
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        for command in ["START", "ADD ABC 1", "CHECKOUT PICKUP"]:
            inbound(session, tenant_a, channel, command)
        quote = session.scalar(select(Quote))
        inbound(session, tenant_a, channel, f"CONFIRM {quote.id}")
    worker = Worker(engine, tenant_ids=[tenant_a])
    worker.tick()
    worker.tick()
    with tenant_session(tenant_a) as session:
        replies = session.scalars(select(OutboxEvent).where(OutboxEvent.event_key.like("business:%"))).all()
        assert len(replies) == 1
        assert "submitted" in replies[0].payload["text"]
        business_event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "order.submitted"))
        assert business_event.state == "done"


def test_worker_rejects_privileged_connection(admin_engine, tenant_a):
    from whatsapp_bot.worker import Worker

    with pytest.raises(RuntimeError, match="Runtime role"):
        Worker(admin_engine, tenant_ids=[tenant_a])


def test_two_workers_preserve_accepted_command_order(tenant_a, tenant_session, engine, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import whatsapp_bot.worker as module
    from whatsapp_bot.models import CartItem, InboundEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    now = datetime.now(UTC)
    with tenant_session(tenant_a) as session:
        ids = []
        for index, text in enumerate(["START", "ADD ABC 1"]):
            event = InboundEvent(
                tenant_id=tenant_a,
                channel_id=channel,
                event_id=str(uuid4()),
                payload_hash="a" * 64,
                created_at=now + timedelta(microseconds=index),
                payload={
                    "event_type": "message.received",
                    "provider_message_id": str(uuid4()),
                    "customer_ref": "race-shopper",
                    "text": text,
                    "occurred_at": (now + timedelta(microseconds=index)).isoformat(),
                },
            )
            session.add(event)
            session.flush()
            ids.append(event.id)
    entered, release = Event(), Event()
    original = module.process_inbound

    def delayed(session, tenant_id, event):
        if event.id == ids[0]:
            entered.set()
            assert release.wait(10)
        return original(session, tenant_id, event)

    monkeypatch.setattr(module, "process_inbound", delayed)
    first = Worker(engine, tenant_ids=[tenant_a])
    second = Worker(engine, tenant_ids=[tenant_a])
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(first._process, tenant_a, InboundEvent, ids[0])
        assert entered.wait(5)
        try:
            second.tick()
            with tenant_session(tenant_a) as session:
                later = session.get(InboundEvent, ids[1])
                assert later.state == "pending"
                assert session.scalar(select(CartItem.quantity)) is None
        finally:
            release.set()
        task.result(timeout=10)
    second.tick()
    with tenant_session(tenant_a) as session:
        assert all(event.state == "done" for event in session.scalars(select(InboundEvent)))
        assert session.scalar(select(CartItem.quantity)) == 1


def test_retrying_predecessor_blocks_only_its_customer(tenant_a, tenant_session, engine):
    from whatsapp_bot.models import InboundEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    now = datetime.now(UTC)
    with tenant_session(tenant_a) as session:
        for index in range(32):
            session.add(
                InboundEvent(
                    tenant_id=tenant_a,
                    channel_id=channel,
                    event_id=str(uuid4()),
                    payload_hash="a" * 64,
                    created_at=now + timedelta(microseconds=index),
                    next_attempt_at=now + timedelta(hours=1) if index == 0 else now - timedelta(seconds=1),
                    payload={
                        "event_type": "message.received",
                        "provider_message_id": str(uuid4()),
                        "customer_ref": "blocked" if index < 31 else "other",
                        "text": "START",
                        "occurred_at": now.isoformat(),
                    },
                )
            )
    Worker(engine, tenant_ids=[tenant_a]).tick()
    with tenant_session(tenant_a) as session:
        events = session.scalars(select(InboundEvent).order_by(InboundEvent.created_at)).all()
        assert all(event.state == "pending" for event in events[:31])
        assert events[31].state == "done"


def test_delivery_correlates_async_ack_without_provider_id(
    tenant_a, tenant_b, tenant_session, engine, admin_engine
):
    from whatsapp_bot.db import tenant_session as scoped_session
    from whatsapp_bot.models import Channel, InboundEvent, Message, OutboxEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    with scoped_session(admin_engine, tenant_a) as session:
        session.scalar(select(Channel).where(Channel.tenant_id == tenant_a)).policy_campaign_id = "campaign"
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")

    class AsyncAck:
        def send(self, payload):
            return {"operation_id": "queued-op", "command_id": payload["command_id"]}

    worker = Worker(engine, tenant_ids=[tenant_a], outbound_enabled=True, policy_client=AsyncAck())
    worker.tick()
    with tenant_session(tenant_a) as session:
        reply = session.scalar(select(OutboxEvent))
        delivery = InboundEvent(
            tenant_id=tenant_a,
            channel_id=channel,
            event_id=str(uuid4()),
            payload_hash="b" * 64,
            payload={
                "event_type": "message.delivery.updated",
                "provider_message_id": "later-provider-id",
                "command_id": str(reply.command_id),
                "operation_id": "queued-op",
                "status": "delivered",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        session.add(delivery)
    worker.tick()
    with tenant_session(tenant_a) as session:
        message = session.scalar(select(Message).where(Message.direction == "outbound"))
        assert message.provider_message_id == "later-provider-id"
        assert message.status == "delivered"
        delivery = session.scalar(
            select(InboundEvent).where(
                InboundEvent.payload["event_type"].astext == "message.delivery.updated"
            )
        )
        assert delivery.state == "done"
    with tenant_session(tenant_b) as session:
        assert session.scalar(select(Message)) is None


@pytest.mark.parametrize("conflict", ["operation", "command", "channel", "provider"])
def test_delivery_conflicts_never_match_another_message(
    tenant_a, tenant_session, engine, admin_engine, conflict
):
    from whatsapp_bot.db import tenant_session as scoped_session
    from whatsapp_bot.models import Channel, InboundEvent, Message, OutboxEvent
    from whatsapp_bot.worker import Worker

    channel, _ = setup_shop(tenant_session, tenant_a)
    other_channel = uuid4()
    with scoped_session(admin_engine, tenant_a) as session:
        session.scalar(select(Channel).where(Channel.tenant_id == tenant_a)).policy_campaign_id = "campaign"
        session.add(
            Channel(
                id=other_channel,
                tenant_id=tenant_a,
                provider="meta",
                account_id=str(uuid4()),
                phone_number_id="other",
                active=True,
                verification_reference="test",
            )
        )
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
    worker = Worker(engine, tenant_ids=[tenant_a], outbound_enabled=True, policy_client=FakePolicy())
    worker.tick()
    with tenant_session(tenant_a) as session:
        reply = session.scalar(select(OutboxEvent))
        delivery = InboundEvent(
            tenant_id=tenant_a,
            channel_id=other_channel if conflict == "channel" else channel,
            event_id=str(uuid4()),
            payload_hash="b" * 64,
            payload={
                "event_type": "message.delivery.updated",
                "provider_message_id": "wrong-provider" if conflict == "provider" else "provider-outbound-1",
                "command_id": str(uuid4()) if conflict == "command" else str(reply.command_id),
                "operation_id": "wrong-operation" if conflict == "operation" else "operation-1",
                "status": "read",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        session.add(delivery)
    worker.tick()
    with tenant_session(tenant_a) as session:
        message = session.scalar(select(Message).where(Message.direction == "outbound"))
        assert message.status == "sent"
        delivery = session.scalar(
            select(InboundEvent).where(
                InboundEvent.payload["event_type"].astext == "message.delivery.updated"
            )
        )
        assert delivery.state == "pending"
        assert delivery.last_error in {"delivery_message_pending", "delivery_correlation_conflict"}
