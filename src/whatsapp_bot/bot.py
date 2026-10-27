"""Deterministic bilingual command interpreter; no AI or payment authority."""

from uuid import UUID

ALIASES = {
    "START": "start",
    "INICIAR": "start",
    "STOP": "stop",
    "PARAR": "stop",
    "MENU": "menu",
    "CATALOG": "menu",
    "CATALOGO": "menu",
    "ADD": "add",
    "AGREGAR": "add",
    "REMOVE": "remove",
    "QUITAR": "remove",
    "CART": "cart",
    "CARRITO": "cart",
    "CHECKOUT": "checkout",
    "FINALIZAR": "checkout",
    "CONFIRM": "confirm",
    "CONFIRMAR": "confirm",
    "ORDER": "order",
    "PEDIDO": "order",
    "HUMAN": "human",
    "HUMANO": "human",
}
SPANISH = {
    "INICIAR",
    "PARAR",
    "CATALOGO",
    "AGREGAR",
    "QUITAR",
    "CARRITO",
    "FINALIZAR",
    "CONFIRMAR",
    "PEDIDO",
    "HUMANO",
}


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "help", []
    command, args = ALIASES.get(parts[0].upper(), "help"), parts[1:]
    if command in {"start", "stop", "menu", "cart", "human"} and not args:
        return command, args
    if command == "remove" and len(args) == 1:
        return command, args
    if command == "add" and len(args) == 2 and args[1].isascii() and args[1].isdigit():
        if 1 <= int(args[1]) <= 100:
            return command, args
    if command in {"confirm", "order"} and len(args) == 1:
        try:
            UUID(args[0])
            return command, args
        except ValueError:
            pass
    if command == "checkout" and args:
        mode = {"PICKUP": "pickup", "RECOGIDA": "pickup", "DELIVERY": "delivery", "ENTREGA": "delivery"}.get(
            args[0].upper()
        )
        if mode == "pickup" and len(args) == 1:
            return command, [mode]
        if mode == "delivery" and len(args) >= 3:
            return command, [mode, args[1], " ".join(args[2:])]
    return "help", []


def queue_reply(session, tenant_id, conversation, event_key, text):
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy.dialects.postgresql import insert

    from .models import OutboxEvent

    command_id = uuid5(NAMESPACE_URL, f"commerce:{tenant_id}:{event_key}")
    session.execute(
        insert(OutboxEvent)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            conversation_version=conversation.version,
            event_key=event_key,
            kind="message.reply",
            command_id=command_id,
            payload={
                "text": text[:4096],
                "idempotency_key": str(command_id),
                "correlation_id": str(command_id),
            },
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "event_key"])
    )


def process_inbound(session, tenant_id, event):
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from .commerce import CommerceService
    from .errors import CommerceError
    from .models import Conversation, Message

    data = event.payload
    if event.tenant_id != tenant_id:
        raise CommerceError("event_scope_invalid", "Event unavailable", 404)
    if data["event_type"] == "message.delivery.updated":
        _delivery_update(session, tenant_id, event)
        return
    session.execute(
        insert(Conversation)
        .values(tenant_id=tenant_id, channel_id=event.channel_id, customer_ref=data["customer_ref"])
        .on_conflict_do_nothing(index_elements=["tenant_id", "channel_id", "customer_ref"])
    )
    conversation = session.scalar(
        select(Conversation)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.channel_id == event.channel_id,
            Conversation.customer_ref == data["customer_ref"],
        )
        .with_for_update()
    )
    previous = session.scalar(
        select(Message).where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation.id,
            Message.provider_message_id == data["provider_message_id"],
        )
    )
    if previous:
        if previous.direction != "inbound" or previous.text != data["text"]:
            raise CommerceError("message_collision", "Message ID reused", 409)
        return
    session.add(
        Message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            provider_message_id=data["provider_message_id"],
            direction="inbound",
            text=data["text"],
            status="received",
            created_at=datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00")),
        )
    )
    command, args = parse_command(data["text"])
    token = data["text"].strip().split()[0].upper() if data["text"].strip() else ""
    if token in SPANISH:
        conversation.language = "es"
    spanish = conversation.language == "es"
    service = CommerceService(session, tenant_id, "worker")
    # Explicit privacy commands remain effective during a human takeover, without bot replies.
    if command in {"start", "stop"}:
        occurred = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
        previous_time = conversation.bot_state.get("consent_occurred_at")
        baselines = [
            stamp
            for stamp in (
                conversation.consent_at,
                datetime.fromisoformat(previous_time) if previous_time else None,
            )
            if stamp is not None
        ]
        previous = max(baselines) if baselines else None
        if previous is None or occurred > previous or (occurred == previous and command == "stop"):
            service.set_consent(
                conversation.id,
                "opted_in" if command == "start" else "opted_out",
                f"inbound:{event.id}:{command}",
            )
            conversation.consent_at = occurred
            conversation.bot_state = {**conversation.bot_state, "consent_occurred_at": occurred.isoformat()}
        else:
            return
    if conversation.mode == "human" or command == "stop":
        return
    if command == "human":
        service.set_mode(conversation.id, "human")
        return
    if conversation.consent_status != "opted_in":
        return
    try:
        with session.begin_nested():
            reply = _command_reply(session, tenant_id, conversation, service, command, args, spanish)
    except CommerceError as exc:
        reply = (
            "No se pudo completar la solicitud" if spanish else "Unable to complete request"
        ) + f" ({exc.code})."
    if reply:
        queue_reply(session, tenant_id, conversation, f"inbound:{event.id}:reply", reply)


def _money(minor, currency):
    return f"{currency} {minor // 100}.{minor % 100:02d}"


def _command_reply(session, tenant_id, conversation, service, command, args, spanish):
    from sqlalchemy import select

    from .errors import CommerceError
    from .models import Cart, CartItem, Order, Product, Quote

    help_text = (
        "CATALOGO · AGREGAR <SKU> <cantidad> · QUITAR <SKU> · CARRITO · "
        "FINALIZAR RECOGIDA o FINALIZAR ENTREGA <zona> <dirección> · "
        "CONFIRMAR <cotización> · PEDIDO <pedido> · HUMANO · PARAR"
        if spanish
        else "MENU · ADD <SKU> <quantity> · REMOVE <SKU> · CART · "
        "CHECKOUT PICKUP or CHECKOUT DELIVERY <zone> <address> · "
        "CONFIRM <quote> · ORDER <order> · HUMAN · STOP"
    )
    if command in {"start", "help"}:
        return ("Bienvenido. " if spanish else "Welcome. ") + help_text
    if command == "menu":
        products = session.scalars(
            select(Product)
            .where(Product.tenant_id == tenant_id, Product.active.is_(True))
            .order_by(Product.sku)
            .limit(20)
        ).all()
        return ("Catálogo:\n" if spanish else "Catalog:\n") + "\n".join(
            f"{p.sku} · {p.name} · {_money(p.price_minor, p.currency)}" for p in products
        )
    if command in {"add", "remove"}:
        product = session.scalar(
            select(Product).where(Product.tenant_id == tenant_id, Product.sku == args[0])
        )
        if product is None:
            raise CommerceError("product_not_found", "Product unavailable", 404)
        service.set_cart_item(conversation.id, product.id, int(args[1]) if command == "add" else 0)
        return ("Carrito actualizado. " if spanish else "Cart updated. ") + ("CARRITO" if spanish else "CART")
    if command == "cart":
        cart = session.scalar(
            select(Cart).where(
                Cart.tenant_id == tenant_id, Cart.conversation_id == conversation.id, Cart.status == "active"
            )
        )
        if cart is None:
            return "Carrito vacío." if spanish else "Cart is empty."
        rows = session.execute(
            select(CartItem, Product)
            .join(Product, (Product.id == CartItem.product_id) & (Product.tenant_id == CartItem.tenant_id))
            .where(CartItem.tenant_id == tenant_id, CartItem.cart_id == cart.id)
            .order_by(Product.sku)
        ).all()
        return ("Carrito:\n" if spanish else "Cart:\n") + "\n".join(
            f"{p.sku} × {item.quantity} · {_money(p.price_minor * item.quantity, p.currency)}"
            for item, p in rows
        )
    if command == "checkout":
        quote = service.quote_cart(
            conversation.id,
            fulfillment=args[0],
            zone_code=args[1] if len(args) > 1 else None,
            address=args[2] if len(args) > 2 else "",
        )
        return (
            ("Total: " + _money(quote["total_minor"], quote["currency"]))
            + "\n"
            + ("CONFIRMAR " if spanish else "CONFIRM ")
            + quote["id"]
            + "\n"
            + ("Vence: " if spanish else "Expires: ")
            + quote["expires_at"]
        )
    if command in {"confirm", "order"}:
        cls = Quote if command == "confirm" else Order
        obj = session.scalar(
            select(cls).where(
                cls.tenant_id == tenant_id, cls.id == UUID(args[0]), cls.conversation_id == conversation.id
            )
        )
        if obj is None:
            raise CommerceError("not_found", "Object unavailable", 404)
        if command == "confirm":
            service.confirm_quote(obj.id, f"bot-quote:{obj.id}")
            return None  # The committed order event owns the acknowledgement.
        return ("Pedido " if spanish else "Order ") + str(obj.id) + ": " + obj.status
    return help_text


def _delivery_update(session, tenant_id, event):
    from datetime import datetime

    from sqlalchemy import select

    from .errors import CommerceError
    from .models import Conversation, InboundEvent, Message, OutboxEvent

    data = event.payload
    query = (
        select(Message.id, Message.conversation_id)
        .join(
            Conversation,
            (Conversation.id == Message.conversation_id) & (Conversation.tenant_id == Message.tenant_id),
        )
        .where(
            Message.tenant_id == tenant_id,
            Conversation.channel_id == event.channel_id,
            Message.direction == "outbound",
        )
    )
    correlated = "command_id" in data or "operation_id" in data
    if correlated:
        query = query.join(
            OutboxEvent,
            (OutboxEvent.command_id == Message.id)
            & (OutboxEvent.tenant_id == Message.tenant_id)
            & (OutboxEvent.conversation_id == Message.conversation_id),
        ).where(OutboxEvent.state == "sent", OutboxEvent.kind == "message.reply")
        if "command_id" in data:
            query = query.where(OutboxEvent.command_id == UUID(data["command_id"]))
        if "operation_id" in data:
            query = query.where(OutboxEvent.policy_operation_id == data["operation_id"])
    else:
        query = query.where(Message.provider_message_id == data["provider_message_id"])
    match = session.execute(query).one_or_none()
    if match is None:
        raise CommerceError("delivery_message_pending", "Matching outbound message is not available")
    message_id, conversation_id = match
    session.scalar(
        select(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
        .with_for_update()
    )
    message = session.scalar(
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
            Message.id == message_id,
        )
        .with_for_update()
    )
    if message.provider_message_id not in {None, data["provider_message_id"]}:
        raise CommerceError("delivery_correlation_conflict", "Delivery correlation conflicts")
    # Bind a newly assigned provider ID only after signed correlation resolved one exact command/channel.
    message.provider_message_id = data["provider_message_id"]
    occurred = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
    previous = session.scalars(
        select(InboundEvent).where(
            InboundEvent.tenant_id == tenant_id,
            InboundEvent.channel_id == event.channel_id,
            InboundEvent.state == "done",
            InboundEvent.payload["provider_message_id"].astext == data["provider_message_id"],
            InboundEvent.payload["event_type"].astext == "message.delivery.updated",
        )
    ).all()
    if any(
        datetime.fromisoformat(p.payload["occurred_at"].replace("Z", "+00:00")) > occurred for p in previous
    ):
        return
    ranks = {"sent": 1, "failed": 1, "delivered": 2, "read": 3}
    if ranks.get(data["status"], 0) >= ranks.get(message.status, 0):
        message.status = data["status"]
