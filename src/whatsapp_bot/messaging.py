"""Authenticated, bounded, durable inbox ingestion."""

import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from .db import lookup_channel, tenant_session
from .errors import CommerceError
from .models import InboundEvent


def _invalid():
    raise CommerceError("invalid_event", "Invalid event envelope", 400)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def validate_envelope(body: bytes) -> dict:
    try:
        data = json.loads(body, object_pairs_hook=_pairs, parse_constant=lambda value: _invalid())
    except (ValueError, UnicodeDecodeError):
        _invalid()
    if not isinstance(data, dict):
        _invalid()
    common = {
        "event_id",
        "provider",
        "provider_account_id",
        "phone_number_id",
        "provider_message_id",
        "event_type",
        "occurred_at",
        "payload_schema_version",
        "trace_id",
    }
    optional = {"customer_ref", "text"} if data.get("event_type") == "message.received" else {"status"}
    correlations = (
        {"command_id", "operation_id"} if data.get("event_type") == "message.delivery.updated" else set()
    )
    if not common | optional <= set(data) or set(data) - (common | optional | correlations):
        _invalid()
    for key in correlations & set(data):
        if not isinstance(data[key], str) or not 1 <= len(data[key]) <= 200 or not data[key].strip():
            _invalid()
    if "command_id" in data:
        try:
            UUID(data["command_id"])
        except ValueError:
            _invalid()
    for key in common - {"payload_schema_version", "occurred_at"}:
        if not isinstance(data[key], str) or not 1 <= len(data[key]) <= 200 or not data[key].strip():
            _invalid()
    if data["provider"] not in {"meta", "evolution"} or data["event_type"] not in {
        "message.received",
        "message.delivery.updated",
    }:
        _invalid()
    if type(data["payload_schema_version"]) is not int or data["payload_schema_version"] != 1:
        _invalid()
    try:
        occurred = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            _invalid()
    except (ValueError, TypeError, AttributeError):
        _invalid()
    if data["event_type"] == "message.received":
        if not isinstance(data["customer_ref"], str) or not 1 <= len(data["customer_ref"]) <= 200:
            _invalid()
        if not isinstance(data["text"], str) or not 1 <= len(data["text"]) <= 4096:
            _invalid()
    elif not isinstance(data["status"], str) or data["status"] not in {"sent", "delivered", "read", "failed"}:
        _invalid()
    return data


def inbox_identity_lock(session, tenant_id, channel_id, payload, *, wait=False):
    """Serialize a durable customer stream even before its conversation exists."""
    identity = (
        payload.get("customer_ref")
        if payload.get("event_type") == "message.received"
        else payload.get("provider_message_id")
    )
    material = json.dumps([str(tenant_id), str(channel_id), payload.get("event_type"), identity])
    key = int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big", signed=True)
    function = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    result = session.scalar(text(f"SELECT {function}(:key)"), {"key": key})
    return True if wait else result


class IngressService:
    def __init__(self, engine, secret: str):
        self.engine = engine
        self._secret = secret.encode()

    def accept(self, raw_body: bytes, timestamp: str, signature: str) -> dict:
        if len(self._secret) < 32:
            raise CommerceError("ingress_unconfigured", "Ingress is not configured", 503)
        if len(raw_body) > 1024 * 1024:
            raise CommerceError("event_too_large", "Event exceeds maximum size", 413)
        if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,12}", timestamp):
            raise CommerceError("invalid_signature", "Invalid event authentication", 401)
        expected = (
            "sha256="
            + hmac.new(self._secret, timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
        )
        if (
            abs(time.time() - int(timestamp)) > 300
            or not isinstance(signature, str)
            or not re.fullmatch(r"sha256=[a-f0-9]{64}", signature)
            or not hmac.compare_digest(expected, signature)
        ):
            raise CommerceError("invalid_signature", "Invalid event authentication", 401)
        data = validate_envelope(raw_body)
        channel = lookup_channel(
            self.engine, data["provider"], data["provider_account_id"], data["phone_number_id"]
        )
        if channel is None:
            raise CommerceError("unknown_channel", "No active channel binding", 404)
        tenant_id, channel_id = channel
        digest = hashlib.sha256(raw_body).hexdigest()
        with tenant_session(self.engine, tenant_id) as session:
            inbox_identity_lock(session, tenant_id, channel_id, data, wait=True)
            inserted = session.execute(
                insert(InboundEvent)
                .values(
                    tenant_id=tenant_id,
                    channel_id=channel_id,
                    event_id=data["event_id"],
                    payload_hash=digest,
                    payload=data,
                    created_at=func.clock_timestamp(),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "channel_id", "event_id"])
                .returning(InboundEvent.id)
            ).scalar_one_or_none()
            event = session.scalar(
                select(InboundEvent).where(
                    InboundEvent.tenant_id == tenant_id,
                    InboundEvent.channel_id == channel_id,
                    InboundEvent.event_id == data["event_id"],
                )
            )
            if event.payload_hash != digest:
                raise CommerceError("event_collision", "Event ID was reused with different content", 409)
            result = {"accepted": True, "duplicate": inserted is None, "event_id": str(event.id)}
        return result
