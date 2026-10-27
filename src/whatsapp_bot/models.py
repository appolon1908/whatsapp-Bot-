"""Commerce persistence. Tenant foreign keys and PostgreSQL RLS are complementary boundaries."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Tenant(Identity):
    tenant_id: Mapped[UUID] = mapped_column(index=True)


def tenant_args(*constraints):
    return (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["commerce.businesses.id"]),
        *constraints,
        {"schema": "commerce"},
    )


def tenant_fk(column, target):
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"commerce.{target}.tenant_id", f"commerce.{target}.id"]
    )


class Business(Identity, Base):
    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint("currency IN ('USD','DOP')"),
        CheckConstraint("tax_bps BETWEEN 0 AND 10000"),
        {"schema": "commerce"},
    )
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    active: Mapped[bool] = mapped_column(default=True)
    tax_bps: Mapped[int] = mapped_column(Integer)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Tenant, Base):
    __tablename__ = "memberships"
    __table_args__ = tenant_args(
        UniqueConstraint("tenant_id", "subject"),
        CheckConstraint("role IN ('owner','supervisor','agent','fulfillment')"),
    )
    subject: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(default=True)


class Channel(Tenant, Base):
    __tablename__ = "channels"
    __table_args__ = tenant_args(
        UniqueConstraint("provider", "account_id", "phone_number_id"),
        CheckConstraint("provider IN ('meta','evolution')"),
    )
    provider: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[str] = mapped_column(String(200))
    phone_number_id: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=False)
    verification_reference: Mapped[str | None] = mapped_column(String(255))
    policy_campaign_id: Mapped[str | None] = mapped_column(String(200))


class Product(Tenant, Base):
    __tablename__ = "products"
    __table_args__ = tenant_args(
        UniqueConstraint("tenant_id", "sku"),
        CheckConstraint("price_minor >= 0"),
        CheckConstraint("stock_on_hand >= 0 AND stock_reserved >= 0 AND stock_reserved <= stock_on_hand"),
        CheckConstraint("currency IN ('USD','DOP')"),
    )
    sku: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    stock_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    stock_reserved: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(default=True)
    image_url: Mapped[str | None] = mapped_column(Text)


class DeliveryZone(Tenant, Base):
    __tablename__ = "delivery_zones"
    __table_args__ = tenant_args(
        UniqueConstraint("tenant_id", "code"), CheckConstraint("fee_minor >= 0 AND minimum_minor >= 0")
    )
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    minimum_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(default=True)


class Conversation(Tenant, Base):
    __tablename__ = "conversations"
    __table_args__ = tenant_args(
        tenant_fk("channel_id", "channels"),
        UniqueConstraint("tenant_id", "channel_id", "customer_ref"),
        CheckConstraint("language IN ('en','es')"),
        CheckConstraint("mode IN ('bot','human')"),
        CheckConstraint("consent_status IN ('unknown','opted_in','opted_out')"),
    )
    channel_id: Mapped[UUID]
    customer_ref: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(2), default="en")
    mode: Mapped[str] = mapped_column(String(10), default="bot")
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    consent_status: Mapped[str] = mapped_column(String(20), default="unknown")
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_evidence: Mapped[str | None] = mapped_column(Text)
    bot_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Tenant, Base):
    __tablename__ = "messages"
    __table_args__ = tenant_args(
        tenant_fk("conversation_id", "conversations"),
        UniqueConstraint("tenant_id", "conversation_id", "provider_message_id"),
        CheckConstraint("direction IN ('inbound','outbound')"),
    )
    conversation_id: Mapped[UUID]
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="received")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Cart(Tenant, Base):
    __tablename__ = "carts"
    __table_args__ = tenant_args(
        tenant_fk("conversation_id", "conversations"),
        CheckConstraint("status IN ('active','ordered')"),
        Index(
            "uq_active_cart",
            "tenant_id",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    conversation_id: Mapped[UUID]
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")


class CartItem(Tenant, Base):
    __tablename__ = "cart_items"
    __table_args__ = tenant_args(
        tenant_fk("cart_id", "carts"),
        tenant_fk("product_id", "products"),
        UniqueConstraint("tenant_id", "cart_id", "product_id"),
        CheckConstraint("quantity BETWEEN 1 AND 100"),
    )
    cart_id: Mapped[UUID]
    product_id: Mapped[UUID]
    quantity: Mapped[int] = mapped_column(Integer)


class Quote(Tenant, Base):
    __tablename__ = "quotes"
    __table_args__ = tenant_args(
        tenant_fk("cart_id", "carts"),
        tenant_fk("conversation_id", "conversations"),
        CheckConstraint("fulfillment IN ('pickup','delivery')"),
    )
    cart_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    cart_version: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)
    delivery_minor: Mapped[int] = mapped_column(BigInteger)
    tax_minor: Mapped[int] = mapped_column(BigInteger)
    total_minor: Mapped[int] = mapped_column(BigInteger)
    address: Mapped[str] = mapped_column(Text, default="")
    fulfillment: Mapped[str] = mapped_column(String(20))
    zone_code: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuoteItem(Tenant, Base):
    __tablename__ = "quote_items"
    __table_args__ = tenant_args(
        tenant_fk("quote_id", "quotes"),
        tenant_fk("product_id", "products"),
        UniqueConstraint("tenant_id", "quote_id", "product_id"),
        CheckConstraint("quantity BETWEEN 1 AND 100"),
    )
    quote_id: Mapped[UUID]
    product_id: Mapped[UUID]
    product_version: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)


class Order(Tenant, Base):
    __tablename__ = "orders"
    __table_args__ = tenant_args(
        tenant_fk("quote_id", "quotes"),
        tenant_fk("conversation_id", "conversations"),
        UniqueConstraint("tenant_id", "quote_id"),
        UniqueConstraint("tenant_id", "idempotency_key"),
        CheckConstraint(
            "status IN ('submitted','accepted','preparing','out_for_delivery','delivered','ready_for_pickup','collected','rejected','cancelled')"
        ),
        CheckConstraint("payment_status IN ('unpaid','paid_externally','refunded_externally')"),
        CheckConstraint("fulfillment IN ('pickup','delivery')"),
    )
    quote_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="submitted")
    payment_status: Mapped[str] = mapped_column(String(30), default="unpaid")
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)
    delivery_minor: Mapped[int] = mapped_column(BigInteger)
    tax_minor: Mapped[int] = mapped_column(BigInteger)
    total_minor: Mapped[int] = mapped_column(BigInteger)
    address: Mapped[str] = mapped_column(Text, default="")
    fulfillment: Mapped[str] = mapped_column(String(20))
    reservation_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderItem(Tenant, Base):
    __tablename__ = "order_items"
    __table_args__ = tenant_args(
        tenant_fk("order_id", "orders"),
        tenant_fk("product_id", "products"),
        UniqueConstraint("tenant_id", "order_id", "product_id"),
        CheckConstraint("quantity BETWEEN 1 AND 100"),
    )
    order_id: Mapped[UUID]
    product_id: Mapped[UUID]
    sku: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)


class InboundEvent(Tenant, Base):
    __tablename__ = "inbound_events"
    __table_args__ = tenant_args(
        tenant_fk("channel_id", "channels"),
        UniqueConstraint("tenant_id", "channel_id", "event_id"),
        CheckConstraint("state IN ('pending','done','dead')"),
    )
    channel_id: Mapped[UUID]
    event_id: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxEvent(Tenant, Base):
    __tablename__ = "outbox_events"
    __table_args__ = tenant_args(
        tenant_fk("conversation_id", "conversations"),
        UniqueConstraint("tenant_id", "event_key"),
        CheckConstraint("state IN ('pending','done','sent','cancelled','dead')"),
    )
    conversation_id: Mapped[UUID]
    conversation_version: Mapped[int] = mapped_column(Integer)
    event_key: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(100))
    command_id: Mapped[UUID] = mapped_column(default=uuid4)
    policy_operation_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Tenant, Base):
    __tablename__ = "audit_events"
    __table_args__ = tenant_args()
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(100))
    object_id: Mapped[UUID | None]
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
