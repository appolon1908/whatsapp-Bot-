"""Tenant-scoped commerce operations. The caller owns the transaction and commit."""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from whatsapp_bot.errors import CommerceError
from whatsapp_bot.models import (
    AuditEvent,
    Business,
    Cart,
    CartItem,
    Channel,
    Conversation,
    DeliveryZone,
    Order,
    OrderItem,
    OutboxEvent,
    Product,
    Quote,
    QuoteItem,
)

MONEY_MAX = 10**12


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CommerceError("invalid_id", "Invalid object identifier", 422) from exc


def document(row):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return {
        key: (
            str(value)
            if isinstance(value, UUID)
            else value.isoformat()
            if isinstance(value, datetime)
            else value
        )
        for key, value in values.items()
    }


def integer(value, name, minimum=0, maximum=MONEY_MAX):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise CommerceError("invalid_input", f"{name} is outside the allowed integer range", 422)
    return value


def string(value, name, maximum, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not value.strip() and not allow_empty):
        raise CommerceError("invalid_input", f"{name} is invalid", 422)
    return value


class CommerceService:
    def __init__(self, session, tenant_id: UUID, actor: str):
        self.session = session
        self.tenant_id = identifier(tenant_id)
        self.actor = actor

    def _query(self, model):
        return select(model).where(model.tenant_id == self.tenant_id)

    def _one(self, model, object_id, lock=False):
        query = self._query(model).where(model.id == identifier(object_id))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        result = self.session.scalar(query)
        if result is None:
            raise CommerceError("not_found", "Object not found", 404)
        return result

    def _business(self, *, lock=False):
        statement = (
            select(Business).where(Business.id == self.tenant_id).execution_options(populate_existing=True)
        )
        if lock:
            # Checkout observes fresh configuration and protects it until commit.
            # Concurrent checkouts may share the lock; settings updates must wait.
            statement = statement.with_for_update(read=True)
        business = self.session.scalar(statement)
        if business is None or not business.active:
            raise CommerceError("business_unavailable", "Business is unavailable", 409)
        return business

    def _audit(self, action, row, details=None):
        self.session.add(
            AuditEvent(
                tenant_id=self.tenant_id,
                actor=self.actor,
                action=action,
                object_type=row.__tablename__,
                object_id=row.id,
                details=details or {},
            )
        )

    def _new(self, model, values):
        row = model(tenant_id=self.tenant_id, **values)
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            raise CommerceError("conflict", "Object conflicts with existing data") from exc
        return row

    def _list(self, model, limit=100, offset=0):
        integer(limit, "limit", 1, 200)
        integer(offset, "offset", 0, 1000000)
        return [
            document(row)
            for row in self.session.scalars(self._query(model).order_by(model.id).limit(limit).offset(offset))
        ]

    def _product_values(self, data, creating=False):
        allowed = {
            "sku",
            "name",
            "description",
            "price_minor",
            "currency",
            "stock_on_hand",
            "active",
            "image_url",
        }
        if set(data) - allowed or (not creating and "stock_on_hand" in data):
            raise CommerceError("invalid_input", "Unsupported product fields; use stock adjustment", 422)
        if creating and not {"sku", "name", "price_minor", "currency"} <= data.keys():
            raise CommerceError("invalid_input", "Product SKU, name, price and currency are required", 422)
        values = dict(data)
        for name, maximum in [("sku", 100), ("name", 200), ("description", 4096)]:
            if name in values:
                string(values[name], name, maximum, allow_empty=name == "description")
        if "price_minor" in values:
            integer(values["price_minor"], "price_minor")
        if "stock_on_hand" in values:
            integer(values["stock_on_hand"], "stock_on_hand", 0, 1000000000)
        if "currency" in values and values["currency"] != self._business().currency:
            raise CommerceError("currency_mismatch", "Product currency must match the business", 422)
        if "active" in values and not isinstance(values["active"], bool):
            raise CommerceError("invalid_input", "active must be boolean", 422)
        if values.get("image_url") is not None:
            string(values["image_url"], "image_url", 2048)
            if not values["image_url"].startswith("https://"):
                raise CommerceError("invalid_input", "Image URL must use HTTPS", 422)
        return values

    def create_product(self, data):
        self._business()
        row = self._new(Product, self._product_values(data, True))
        self._audit("product.created", row)
        return document(row)

    def update_product(self, product_id, data):
        values = self._product_values(data)
        row = self._one(Product, product_id, True)
        try:
            with self.session.begin_nested():
                for key, value in values.items():
                    setattr(row, key, value)
                row.version += 1
                self.session.flush()
        except IntegrityError as exc:
            raise CommerceError("conflict", "Product conflicts with existing data") from exc
        self._audit("product.updated", row, {"fields": sorted(values)})
        return document(row)

    def list_products(self, limit=100, offset=0):
        return self._list(Product, limit, offset)

    def get_product(self, product_id):
        return document(self._one(Product, product_id))

    def adjust_stock(self, product_id, delta):
        integer(delta, "delta", -1000000000, 1000000000)
        row = self._one(Product, product_id, True)
        if not row.stock_reserved <= row.stock_on_hand + delta <= 1000000000:
            raise CommerceError("stock_conflict", "Stock adjustment would violate reservations or limits")
        row.stock_on_hand += delta
        self._audit("stock.adjusted", row, {"delta": delta})
        self.session.flush()
        return document(row)

    def _zone_values(self, data, creating=False):
        if set(data) - {"code", "name", "fee_minor", "minimum_minor", "active"}:
            raise CommerceError("invalid_input", "Unsupported delivery zone fields", 422)
        if creating and not {"code", "name"} <= data.keys():
            raise CommerceError("invalid_input", "Zone code and name are required", 422)
        for name in ("fee_minor", "minimum_minor"):
            if name in data:
                integer(data[name], name)
        for name, maximum in [("code", 100), ("name", 200)]:
            if name in data:
                string(data[name], name, maximum)
        if "active" in data and not isinstance(data["active"], bool):
            raise CommerceError("invalid_input", "active must be boolean", 422)
        return dict(data)

    def create_zone(self, data):
        row = self._new(DeliveryZone, self._zone_values(data, True))
        self._audit("zone.created", row)
        return document(row)

    def list_zones(self):
        return self._list(DeliveryZone, 200)

    def update_zone(self, zone_id, data):
        values = self._zone_values(data)
        row = self._one(DeliveryZone, zone_id, True)
        try:
            with self.session.begin_nested():
                for key, value in values.items():
                    setattr(row, key, value)
                self.session.flush()
        except IntegrityError as exc:
            raise CommerceError("conflict", "Zone conflicts with existing data") from exc
        self._audit("zone.updated", row, {"fields": sorted(values)})
        return document(row)

    def create_conversation(self, channel_id, customer_ref, language="en"):
        channel = self._one(Channel, channel_id)
        if not channel.active:
            raise CommerceError("channel_unavailable", "Channel is unavailable")
        string(customer_ref, "customer_ref", 255)
        if language not in ("en", "es"):
            raise CommerceError("invalid_input", "Unsupported conversation language", 422)
        existing = self.session.scalar(
            self._query(Conversation).where(
                Conversation.channel_id == channel.id, Conversation.customer_ref == customer_ref
            )
        )
        if existing:
            return document(existing)
        # Serialize first-contact creation without acquiring unrelated tenant locks.
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"conversation:{self.tenant_id}:{channel.id}:{customer_ref}"},
        )
        existing = self.session.scalar(
            self._query(Conversation).where(
                Conversation.channel_id == channel.id, Conversation.customer_ref == customer_ref
            )
        )
        row = existing or self._new(
            Conversation, {"channel_id": channel.id, "customer_ref": customer_ref, "language": language}
        )
        return document(row)

    def get_conversation(self, conversation_id):
        return document(self._one(Conversation, conversation_id))

    def list_conversations(self, limit=100, offset=0):
        return self._list(Conversation, limit, offset)

    def _cancel_replies(self, conversation):
        self.session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == self.tenant_id,
                OutboxEvent.conversation_id == conversation.id,
                OutboxEvent.kind == "message.reply",
                OutboxEvent.state == "pending",
            )
            .values(state="cancelled", last_error="conversation_changed")
        )

    def set_mode(self, conversation_id, mode, assigned_to=None):
        if mode not in ("bot", "human"):
            raise CommerceError("invalid_input", "Unsupported conversation mode", 422)
        if assigned_to is not None:
            string(assigned_to, "assigned_to", 255)
        row = self._one(Conversation, conversation_id, True)
        row.mode = mode
        row.assigned_to = assigned_to
        row.version += 1
        self._cancel_replies(row)
        self._audit("conversation.mode_changed", row, {"mode": mode, "assigned_to": assigned_to})
        self.session.flush()
        return document(row)

    def set_consent(self, conversation_id, status, evidence):
        if status not in ("unknown", "opted_in", "opted_out"):
            raise CommerceError("invalid_input", "Unsupported consent status", 422)
        string(evidence, "evidence", 1000)
        row = self._one(Conversation, conversation_id, True)
        row.consent_status = status
        row.consent_at = datetime.now(UTC)
        row.bot_state = {**row.bot_state, "consent_occurred_at": row.consent_at.isoformat()}
        row.consent_evidence = evidence
        if status != "opted_in":
            row.version += 1
            self._cancel_replies(row)
        self._audit("conversation.consent_changed", row, {"status": status})
        self.session.flush()
        return document(row)

    def _cart(self, conversation):
        cart = self.session.scalar(
            self._query(Cart)
            .where(Cart.conversation_id == conversation.id, Cart.status == "active")
            .with_for_update()
        )
        if cart is None:
            cart = self._new(Cart, {"conversation_id": conversation.id})
        return cart

    def _cart_doc(self, cart):
        result = document(cart)
        result["items"] = []
        for item in self.session.scalars(
            self._query(CartItem).where(CartItem.cart_id == cart.id).order_by(CartItem.product_id)
        ):
            value = document(item)
            product = self._one(Product, item.product_id)
            value.update(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "unit_price_minor": product.price_minor,
                    "subtotal_minor": item.quantity * product.price_minor,
                    "currency": product.currency,
                }
            )
            result["items"].append(value)
        return result

    def get_cart(self, conversation_id):
        conversation = self._one(Conversation, conversation_id, True)
        return self._cart_doc(self._cart(conversation))

    def set_cart_item(self, conversation_id, product_id, quantity):
        integer(quantity, "quantity", 0, 100)
        conversation = self._one(Conversation, conversation_id, True)
        cart = self._cart(conversation)
        product = self._one(Product, product_id)
        if quantity and not product.active:
            raise CommerceError("product_unavailable", "Product is unavailable")
        item = self.session.scalar(
            self._query(CartItem).where(CartItem.cart_id == cart.id, CartItem.product_id == product.id)
        )
        if quantity == 0:
            if item:
                self.session.delete(item)
        elif item:
            item.quantity = quantity
        else:
            self.session.add(
                CartItem(tenant_id=self.tenant_id, cart_id=cart.id, product_id=product.id, quantity=quantity)
            )
        cart.version += 1
        self.session.flush()
        return self._cart_doc(cart)

    def _check_hours(self, business, now):
        # Canonical format: opening_hours={"mon":[["09:00","17:00"]],...}.
        # Missing setting means unrestricted; a configured day with no windows is closed.
        hours = business.settings.get("opening_hours")
        if hours is None:
            return
        local = now.astimezone(ZoneInfo(business.timezone))
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        try:
            if not isinstance(hours, dict) or set(hours) - set(names):
                raise ValueError()
            windows = hours.get(names[local.weekday()], [])
            is_open = False
            for window in windows:
                if not isinstance(window, list) or len(window) != 2:
                    raise ValueError()
                start, end = (time.fromisoformat(value) for value in window)
                if start.tzinfo or end.tzinfo or start >= end:
                    raise ValueError()
                is_open = is_open or start <= local.time().replace(tzinfo=None) < end
        except (ValueError, TypeError, AttributeError) as exc:
            raise CommerceError(
                "invalid_business_hours", "Business opening hours need operator correction"
            ) from exc
        if not is_open:
            raise CommerceError("business_closed", "Business is currently closed")

    def quote_cart(self, conversation_id, fulfillment="pickup", zone_code=None, address=""):
        if fulfillment not in ("pickup", "delivery"):
            raise CommerceError("invalid_input", "Unsupported fulfillment method", 422)
        string(address, "address", 2000, True)
        conversation = self._one(Conversation, conversation_id, True)
        business = self._business(lock=True)
        now = datetime.now(UTC)
        self._check_hours(business, now)
        cart = self._cart(conversation)
        items = list(
            self.session.scalars(
                self._query(CartItem).where(CartItem.cart_id == cart.id).order_by(CartItem.product_id)
            )
        )
        if not items:
            raise CommerceError("empty_cart", "Cart is empty")
        products = {
            product.id: product
            for product in self.session.scalars(
                self._query(Product)
                .where(Product.id.in_([item.product_id for item in items]))
                .order_by(Product.id)
                .with_for_update()
            )
        }
        subtotal = 0
        for item in items:
            product = products[item.product_id]
            if not product.active or product.currency != business.currency:
                raise CommerceError("product_unavailable", "Product or currency is unavailable")
            if product.stock_on_hand - product.stock_reserved < item.quantity:
                raise CommerceError("insufficient_stock", "Insufficient available stock")
            subtotal += product.price_minor * item.quantity
        fee = 0
        if fulfillment == "delivery":
            if not address.strip() or not zone_code:
                raise CommerceError("invalid_input", "Delivery zone and address are required", 422)
            zone = self.session.scalar(
                self._query(DeliveryZone)
                .where(DeliveryZone.code == zone_code, DeliveryZone.active.is_(True))
                .with_for_update()
            )
            if not zone:
                raise CommerceError("zone_unavailable", "Delivery zone is unavailable")
            if subtotal < zone.minimum_minor:
                raise CommerceError("minimum_not_met", "Delivery minimum has not been met")
            fee = zone.fee_minor
        else:
            zone_code = None
            address = ""
        tax = ((subtotal + fee) * business.tax_bps + 5000) // 10000
        quote = self._new(
            Quote,
            {
                "cart_id": cart.id,
                "conversation_id": conversation.id,
                "cart_version": cart.version,
                "currency": business.currency,
                "subtotal_minor": subtotal,
                "delivery_minor": fee,
                "tax_minor": tax,
                "total_minor": subtotal + fee + tax,
                "address": address,
                "fulfillment": fulfillment,
                "zone_code": zone_code,
                "expires_at": now + timedelta(minutes=15),
            },
        )
        for item in items:
            product = products[item.product_id]
            self.session.add(
                QuoteItem(
                    tenant_id=self.tenant_id,
                    quote_id=quote.id,
                    product_id=product.id,
                    product_version=product.version,
                    sku=product.sku,
                    name=product.name,
                    quantity=item.quantity,
                    unit_price_minor=product.price_minor,
                    subtotal_minor=product.price_minor * item.quantity,
                )
            )
        self.session.flush()
        result = document(quote)
        result["items"] = [
            document(item)
            for item in self.session.scalars(
                self._query(QuoteItem).where(QuoteItem.quote_id == quote.id).order_by(QuoteItem.product_id)
            )
        ]
        return result

    def _event(self, conversation, order, kind):
        payload = {"order_id": str(order.id)}
        if kind == "order.updated":
            payload["status"] = order.status
        key = f"{kind}:{order.id}:{order.status}"
        self.session.add(
            OutboxEvent(
                tenant_id=self.tenant_id,
                conversation_id=conversation.id,
                conversation_version=conversation.version,
                event_key=key,
                kind=kind,
                payload=payload,
            )
        )

    def confirm_quote(self, quote_id, idempotency_key):
        string(idempotency_key, "idempotency_key", 200)
        initial = self._one(Quote, quote_id)
        conversation = self._one(Conversation, initial.conversation_id, True)
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"order:{self.tenant_id}:{idempotency_key}"},
        )
        binding_id = uuid5(self.tenant_id, f"order-confirmation:{idempotency_key}")
        binding = self.session.scalar(self._query(AuditEvent).where(AuditEvent.id == binding_id))
        if binding is not None and binding.object_id != initial.id:
            raise CommerceError("idempotency_conflict", "Idempotency key belongs to a different quote")

        def remember_key():
            if binding is None:
                self.session.add(
                    AuditEvent(
                        id=binding_id,
                        tenant_id=self.tenant_id,
                        actor=self.actor,
                        action="order.confirmation_key",
                        object_type="quotes",
                        object_id=initial.id,
                        details={},
                    )
                )
                self.session.flush()

        by_key = self.session.scalar(self._query(Order).where(Order.idempotency_key == idempotency_key))
        if by_key:
            if by_key.quote_id != initial.id:
                raise CommerceError("idempotency_conflict", "Idempotency key belongs to a different quote")
            remember_key()
            return self._order_doc(by_key)
        quote = self._one(Quote, initial.id, True)
        existing = self.session.scalar(self._query(Order).where(Order.quote_id == quote.id))
        if existing:
            remember_key()
            return self._order_doc(existing)
        now = datetime.now(UTC)
        if quote.expires_at <= now:
            raise CommerceError("quote_expired", "Quote expired; request a new quote")
        business = self._business(lock=True)
        self._check_hours(business, now)
        cart = self._one(Cart, quote.cart_id, True)
        if cart.version != quote.cart_version or cart.status != "active":
            raise CommerceError("quote_changed", "Cart changed; request a new quote")
        if business.currency != quote.currency:
            raise CommerceError("quote_changed", "Business currency changed; request a new quote")
        items = list(
            self.session.scalars(
                self._query(QuoteItem).where(QuoteItem.quote_id == quote.id).order_by(QuoteItem.product_id)
            )
        )
        products = {
            product.id: product
            for product in self.session.scalars(
                self._query(Product)
                .where(Product.id.in_([item.product_id for item in items]))
                .order_by(Product.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        for item in items:
            product = products.get(item.product_id)
            if (
                not product
                or not product.active
                or product.version != item.product_version
                or product.price_minor != item.unit_price_minor
                or product.currency != quote.currency
            ):
                raise CommerceError("quote_changed", "Product changed; request a new quote")
            if product.stock_on_hand - product.stock_reserved < item.quantity:
                raise CommerceError("insufficient_stock", "Insufficient available stock")
        if quote.fulfillment == "delivery":
            zone = self.session.scalar(
                self._query(DeliveryZone)
                .where(DeliveryZone.code == quote.zone_code, DeliveryZone.active.is_(True))
                .with_for_update()
            )
            if (
                not zone
                or zone.fee_minor != quote.delivery_minor
                or quote.subtotal_minor < zone.minimum_minor
            ):
                raise CommerceError("quote_changed", "Delivery zone changed; request a new quote")
        if (
            (quote.subtotal_minor + quote.delivery_minor) * business.tax_bps + 5000
        ) // 10000 != quote.tax_minor:
            raise CommerceError("quote_changed", "Tax changed; request a new quote")
        values = {
            name: getattr(quote, name)
            for name in (
                "conversation_id",
                "currency",
                "subtotal_minor",
                "delivery_minor",
                "tax_minor",
                "total_minor",
                "address",
                "fulfillment",
            )
        }
        order = self._new(
            Order,
            {
                **values,
                "quote_id": quote.id,
                "idempotency_key": idempotency_key,
                "reservation_expires_at": now + timedelta(minutes=30),
            },
        )
        for item in items:
            products[item.product_id].stock_reserved += item.quantity
            self.session.add(
                OrderItem(
                    tenant_id=self.tenant_id,
                    order_id=order.id,
                    **{
                        name: getattr(item, name)
                        for name in (
                            "product_id",
                            "sku",
                            "name",
                            "quantity",
                            "unit_price_minor",
                            "subtotal_minor",
                        )
                    },
                )
            )
        cart.status = "ordered"
        self._audit("order.submitted", order, {"quote_id": str(quote.id)})
        self._event(conversation, order, "order.submitted")
        self.session.flush()
        remember_key()
        return self._order_doc(order)

    def _order_doc(self, order):
        result = document(order)
        result["items"] = [
            document(item)
            for item in self.session.scalars(
                self._query(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.product_id)
            )
        ]
        return result

    def get_order(self, order_id):
        return self._order_doc(self._one(Order, order_id))

    def list_orders(self, limit=100, offset=0):
        return self._list(Order, limit, offset)

    def _locked_order(self, order_id):
        initial = self._one(Order, order_id)
        conversation = self._one(Conversation, initial.conversation_id, True)
        return conversation, self._one(Order, order_id, True)

    def transition_order(self, order_id, target):
        conversation, order = self._locked_order(order_id)
        allowed = {
            "submitted": {"accepted", "rejected", "cancelled"},
            "accepted": {"preparing", "cancelled"},
            "preparing": {"out_for_delivery", "ready_for_pickup", "cancelled"},
            "out_for_delivery": {"delivered", "cancelled"},
            "ready_for_pickup": {"collected", "cancelled"},
        }
        if target == order.status:
            return self._order_doc(order)
        if target not in allowed.get(order.status, set()):
            raise CommerceError("invalid_transition", "Order transition is not permitted")
        if (target in ("out_for_delivery", "delivered") and order.fulfillment != "delivery") or (
            target in ("ready_for_pickup", "collected") and order.fulfillment != "pickup"
        ):
            raise CommerceError("invalid_transition", "Transition does not match fulfillment method")
        if target == "accepted" and order.reservation_expires_at <= datetime.now(UTC):
            raise CommerceError("reservation_expired", "Reservation expired before acceptance")
        if target in ("cancelled", "rejected", "delivered", "collected"):
            items = list(
                self.session.scalars(
                    self._query(OrderItem)
                    .where(OrderItem.order_id == order.id)
                    .order_by(OrderItem.product_id)
                )
            )
            products = {
                product.id: product
                for product in self.session.scalars(
                    self._query(Product)
                    .where(Product.id.in_([item.product_id for item in items]))
                    .order_by(Product.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            for item in items:
                products[item.product_id].stock_reserved -= item.quantity
                if target in ("delivered", "collected"):
                    products[item.product_id].stock_on_hand -= item.quantity
        before = order.status
        order.status = target
        self._audit("order.transitioned", order, {"from": before, "to": target})
        self._event(conversation, order, "order.updated")
        self.session.flush()
        return self._order_doc(order)

    def mark_payment(self, order_id, status, reference):
        if status not in ("unpaid", "paid_externally", "refunded_externally"):
            raise CommerceError("invalid_input", "Unsupported payment status", 422)
        string(reference, "reference", 200)
        _, order = self._locked_order(order_id)
        allowed = {
            "unpaid": {"paid_externally"},
            "paid_externally": {"refunded_externally"},
            "refunded_externally": set(),
        }
        if status != order.payment_status and status not in allowed[order.payment_status]:
            raise CommerceError("invalid_payment_transition", "Payment transition is not permitted")
        before = order.payment_status
        order.payment_status = status
        self._audit("payment.recorded", order, {"from": before, "to": status, "reference": reference})
        self.session.flush()
        return self._order_doc(order)

    def expire_reservations(self, now=None):
        now = now or datetime.now(UTC)
        # Read candidate IDs first; acquire conversation before order/product locks.
        candidates = list(
            self.session.scalars(
                self._query(Order)
                .where(Order.status == "submitted", Order.reservation_expires_at <= now)
                .order_by(Order.id)
                .limit(100)
            )
        )
        count = 0
        for candidate in candidates:
            conversation = self.session.scalar(
                self._query(Conversation)
                .where(Conversation.id == candidate.conversation_id)
                .with_for_update(skip_locked=True)
            )
            if conversation is None:
                continue
            order = self._one(Order, candidate.id, True)
            if order.status == "submitted" and order.reservation_expires_at <= now:
                self.transition_order(order.id, "cancelled")
                self._audit("order.reservation_expired", order)
                count += 1
        return count
