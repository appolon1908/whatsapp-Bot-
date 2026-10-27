"""Bounded staff inputs; identity and tenant never come from request bodies."""

from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

Money = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Role = Literal["owner", "supervisor", "agent", "fulfillment"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductCreate(Input):
    sku: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    price_minor: Money
    currency: Literal["USD", "DOP"]
    stock_on_hand: Annotated[int, Field(strict=True, ge=0, le=1_000_000)] = 0
    image_url: str | None = Field(default=None, max_length=2048)

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value):
        return value.upper()

    @field_validator("image_url")
    @classmethod
    def validate_image(cls, value):
        if value is not None:
            from urllib.parse import urlsplit

            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError("product image requires HTTPS without credentials")
        return value


class ProductUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    price_minor: Money | None = None
    active: bool | None = None
    image_url: str | None = Field(default=None, max_length=2048)
    expected_version: Annotated[int, Field(strict=True, ge=1)] | None = None

    @field_validator("image_url")
    @classmethod
    def validate_image(cls, value):
        return ProductCreate.validate_image(value)


class StockAdjustment(Input):
    delta: Annotated[int, Field(strict=True, ge=-1_000_000, le=1_000_000)]


class ZoneCreate(Input):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    fee_minor: Money = 0
    minimum_minor: Money = 0


class ZoneUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    fee_minor: Money | None = None
    minimum_minor: Money | None = None
    active: bool | None = None


class ConversationCreate(Input):
    channel_id: UUID
    customer_ref: str = Field(min_length=1, max_length=200)
    language: Literal["en", "es"] = "en"


class Assignment(Input):
    subject: str | None = Field(default=None, min_length=1, max_length=255)


class Consent(Input):
    status: Literal["opted_in", "opted_out"]
    evidence: str = Field(min_length=1, max_length=1000)


class CartItemInput(Input):
    product_id: UUID
    quantity: Annotated[int, Field(strict=True, ge=0, le=100)]


class QuoteInput(Input):
    conversation_id: UUID
    fulfillment: Literal["pickup", "delivery"] = "pickup"
    zone_code: str | None = Field(default=None, min_length=1, max_length=64)
    address: str = Field(default="", max_length=1000)


class ConfirmOrder(Input):
    quote_id: UUID


class Transition(Input):
    status: Literal[
        "accepted",
        "preparing",
        "out_for_delivery",
        "delivered",
        "ready_for_pickup",
        "collected",
        "rejected",
        "cancelled",
    ]


class Payment(Input):
    status: Literal["paid_externally", "refunded_externally"]
    reference: str = Field(min_length=1, max_length=200)


class MembershipCreate(Input):
    subject: str = Field(min_length=1, max_length=255)
    role: Role


class MembershipUpdate(Input):
    role: Role | None = None
    active: bool | None = None


class ChannelDisable(Input):
    active: Literal[False]


class BusinessUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=100)
    tax_bps: Annotated[int, Field(strict=True, ge=0, le=10000)] | None = None
    opening_hours: dict[str, list[list[str]]] | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @field_validator("opening_hours")
    @classmethod
    def valid_hours(cls, value):
        if value is None:
            return value
        from datetime import time

        if len(value) > 7:
            raise ValueError("at most seven weekdays")
        for day, ranges in value.items():
            if day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} or len(ranges) > 4:
                raise ValueError("weekdays are mon..sun with at most four intervals")
            for interval in ranges:
                if len(interval) != 2 or any(len(part) != 5 for part in interval):
                    raise ValueError("hours use HH:MM pairs")
                start, end = (time.fromisoformat(part) for part in interval)
                if start.tzinfo or end.tzinfo or start >= end:
                    raise ValueError("same-day opening time must precede closing time")
        return value
