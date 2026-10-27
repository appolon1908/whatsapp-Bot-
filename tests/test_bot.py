import importlib


def test_commands_are_bilingual_exact_and_bounded():
    assert importlib.util.find_spec("whatsapp_bot.bot"), "bot implementation missing"
    from whatsapp_bot.bot import parse_command

    assert parse_command("AGREGAR ABC 2") == ("add", ["ABC", "2"])
    assert parse_command("  iniciar  ") == ("start", [])
    assert parse_command("FINALIZAR ENTREGA z Calle principal 5") == (
        "checkout",
        ["delivery", "z", "Calle principal 5"],
    )
    assert parse_command("I want to buy something")[0] == "help"
    assert parse_command("START owner")[0] == "help"
    assert parse_command("ADD ABC -1")[0] == "help"


def inbound(session, tenant_id, channel_id, text, customer="shopper", occurred_at=None):
    from datetime import UTC, datetime
    from uuid import uuid4

    from whatsapp_bot.models import InboundEvent

    event = InboundEvent(
        tenant_id=tenant_id,
        channel_id=channel_id,
        event_id=str(uuid4()),
        payload_hash="a" * 64,
        payload={
            "event_type": "message.received",
            "provider_message_id": str(uuid4()),
            "customer_ref": customer,
            "text": text,
            "occurred_at": occurred_at or datetime.now(UTC).isoformat(),
        },
    )
    session.add(event)
    session.flush()
    from whatsapp_bot.bot import process_inbound

    process_inbound(session, tenant_id, event)
    return event


def setup_shop(tenant_session, tenant_id):
    from sqlalchemy import select

    from whatsapp_bot.commerce import CommerceService
    from whatsapp_bot.models import Channel

    with tenant_session(tenant_id) as session:
        channel = session.scalar(select(Channel))
        product = CommerceService(session, tenant_id, "worker").create_product(
            {"sku": "ABC", "name": "Coffee", "price_minor": 500, "currency": "USD", "stock_on_hand": 10}
        )
        return channel.id, product


def test_bilingual_flow_consent_quote_ownership_and_single_order(tenant_a, tenant_session):
    from sqlalchemy import select

    from whatsapp_bot.models import Conversation, Order, OutboxEvent, Quote

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        for text in ["INICIAR", "CATALOGO", "AGREGAR ABC 2", "CARRITO", "FINALIZAR RECOGIDA"]:
            inbound(session, tenant_a, channel, text)
        conversation = session.scalar(select(Conversation).where(Conversation.customer_ref == "shopper"))
        assert conversation.consent_status == "opted_in"
        assert conversation.language == "es"
        quote = session.scalar(select(Quote))
        inbound(session, tenant_a, channel, "START", "other")
        inbound(session, tenant_a, channel, f"CONFIRM {quote.id}", "other")
        assert session.scalar(select(Order)) is None
        inbound(session, tenant_a, channel, f"CONFIRMAR {quote.id}")
        order = session.scalar(select(Order))
        assert order.conversation_id == conversation.id
        inbound(session, tenant_a, channel, f"CONFIRMAR {quote.id}")
        assert len(session.scalars(select(Order)).all()) == 1
        assert (
            len(session.scalars(select(OutboxEvent).where(OutboxEvent.kind == "order.submitted")).all()) == 1
        )
        inbound(session, tenant_a, channel, f"ORDER {order.id}", "other")
        other = session.scalar(select(Conversation).where(Conversation.customer_ref == "other"))
        replies = session.scalars(select(OutboxEvent).where(OutboxEvent.conversation_id == other.id)).all()
        assert all(str(order.id) not in reply.payload.get("text", "") for reply in replies)


def test_human_mode_records_messages_and_stop_without_bot_replies(tenant_a, tenant_session):
    from sqlalchemy import select

    from whatsapp_bot.models import Conversation, Message, OutboxEvent

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
        inbound(session, tenant_a, channel, "HUMAN")
        inbound(session, tenant_a, channel, "MENU")
        inbound(session, tenant_a, channel, "STOP")
        inbound(session, tenant_a, channel, "START")
        conversation = session.scalar(select(Conversation))
        assert conversation.mode == "human"
        assert len(session.scalars(select(Message)).all()) == 5
        assert not session.scalars(select(OutboxEvent).where(OutboxEvent.state == "pending")).all()


def test_inbound_processor_available():
    import whatsapp_bot.bot as bot

    assert callable(getattr(bot, "process_inbound", None)), "transactional inbound processing missing"


def test_late_start_cannot_revoke_newer_stop(tenant_a, tenant_session):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from whatsapp_bot.models import Conversation

    channel, _ = setup_shop(tenant_session, tenant_a)
    now = datetime.now(UTC)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "STOP", occurred_at=now.isoformat())
        inbound(session, tenant_a, channel, "START", occurred_at=(now - timedelta(minutes=1)).isoformat())
        assert session.scalar(select(Conversation)).consent_status == "opted_out"


def test_delivery_occurrence_and_monotonic_status(tenant_a, tenant_session):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from sqlalchemy import select

    from whatsapp_bot.bot import process_inbound
    from whatsapp_bot.models import Conversation, InboundEvent, Message

    channel, _ = setup_shop(tenant_session, tenant_a)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START")
        conversation = session.scalar(select(Conversation))
        message = Message(
            tenant_id=tenant_a,
            conversation_id=conversation.id,
            provider_message_id="outbound-status-1",
            direction="outbound",
            text="Order",
            status="sent",
        )
        session.add(message)
        now = datetime.now(UTC)
        for status, offset in [
            ("delivered", 0),
            ("failed", -10),
            ("sent", 10),
            ("read", 20),
            ("delivered", 30),
        ]:
            event = InboundEvent(
                tenant_id=tenant_a,
                channel_id=channel,
                event_id=str(uuid4()),
                payload_hash="a" * 64,
                payload={
                    "event_type": "message.delivery.updated",
                    "provider_message_id": "outbound-status-1",
                    "status": status,
                    "occurred_at": (now + timedelta(seconds=offset)).isoformat(),
                },
            )
            session.add(event)
            session.flush()
            process_inbound(session, tenant_a, event)
            event.state = "done"
        session.flush()
        assert message.status == "read"


def test_delayed_start_cannot_override_newer_staff_optout(tenant_a, tenant_session):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from whatsapp_bot.commerce import CommerceService
    from whatsapp_bot.models import Conversation

    channel, _ = setup_shop(tenant_session, tenant_a)
    now = datetime.now(UTC)
    with tenant_session(tenant_a) as session:
        inbound(session, tenant_a, channel, "START", occurred_at=(now - timedelta(minutes=2)).isoformat())
        conversation = session.scalar(select(Conversation))
        CommerceService(session, tenant_a, "test-owner").set_consent(
            conversation.id, "opted_out", "customer requested via staff"
        )
        inbound(session, tenant_a, channel, "START", occurred_at=(now - timedelta(minutes=1)).isoformat())
        assert conversation.consent_status == "opted_out"
