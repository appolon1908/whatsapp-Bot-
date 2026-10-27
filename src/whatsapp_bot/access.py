"""Verified staff identity, persisted membership, and conversation authorization."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .auth import AuthError
from .commerce import CommerceService
from .db import assert_runtime_role, tenant_session
from .errors import CommerceError
from .models import AuditEvent, Business, Conversation, Membership, Order, Quote

bearer = HTTPBearer(auto_error=False)


@dataclass
class Actor:
    session: Session
    tenant_id: UUID
    subject: str
    role: str

    @property
    def commerce(self):
        return CommerceService(self.session, self.tenant_id, self.subject)

    def require(self, *roles):
        if self.role not in roles:
            raise CommerceError("forbidden", "This role cannot perform that operation", 403)

    def conversation(self, conversation_id: UUID, *, lock=False):
        self.require("owner", "supervisor", "agent")
        statement = select(Conversation).where(
            Conversation.tenant_id == self.tenant_id, Conversation.id == conversation_id
        )
        if self.role == "agent":
            statement = statement.where(Conversation.assigned_to == self.subject)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalar(statement)
        if row is None:
            raise CommerceError("not_found", "Conversation not found", 404)
        return row

    def order(self, order_id: UUID, *, lock=False):
        row = self.session.scalar(
            select(Order).where(Order.tenant_id == self.tenant_id, Order.id == order_id)
        )
        if row is None:
            raise CommerceError("not_found", "Order not found", 404)
        if self.role == "agent":
            self.conversation(row.conversation_id, lock=lock)
        elif lock:
            # Follow commerce's conversation -> order lock order. Permission checks
            # and the following mutation observe the same protected order state.
            self.session.scalar(
                select(Conversation)
                .where(Conversation.tenant_id == self.tenant_id, Conversation.id == row.conversation_id)
                .with_for_update()
            )
        if lock:
            row = self.session.scalar(
                select(Order)
                .where(Order.tenant_id == self.tenant_id, Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        return row

    def quote(self, quote_id: UUID, *, lock=False):
        row = self.session.scalar(
            select(Quote).where(Quote.tenant_id == self.tenant_id, Quote.id == quote_id)
        )
        if row is None:
            raise CommerceError("not_found", "Quote not found", 404)
        self.conversation(row.conversation_id, lock=lock)
        return row

    def audit(self, action, object_type, object_id=None, **details):
        self.session.add(
            AuditEvent(
                tenant_id=self.tenant_id,
                actor=self.subject,
                action=action,
                object_type=object_type,
                object_id=object_id,
                details=details,
            )
        )


def _get_actor(request, authorization, business_id, *, membership_write=False):
    if authorization is None:
        raise AuthError("missingtoken", "A bearer access token is required", 401)
    principal = request.app.state.verifier.verify(authorization.credentials)
    try:
        tenant_id = UUID(business_id or "")
    except ValueError as exc:
        raise CommerceError("invalid_business", "A valid X-Business-ID is required", 400) from exc
    engine = request.app.state.engine
    if engine is None:
        raise CommerceError("database_unavailable", "Commerce storage is not configured", 503)
    try:
        assert_runtime_role(engine)
    except RuntimeError as exc:
        raise CommerceError("runtime_role_invalid", "Commerce storage is not ready", 503) from exc
    with tenant_session(engine, tenant_id) as session:
        # Acquire before reading permissions or taking row locks. Staff requests
        # share an authorization snapshot; membership mutations wait for all prior
        # requests and then re-read persisted roles under an exclusive lock.
        lock = "pg_advisory_xact_lock" if membership_write else "pg_advisory_xact_lock_shared"
        session.execute(
            text(f"SELECT {lock}(hashtextextended(:key, 0))"), {"key": f"staff-authorization:{tenant_id}"}
        )
        business = session.scalar(select(Business).where(Business.id == tenant_id, Business.active.is_(True)))
        membership = session.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.subject == principal.subject,
                Membership.active.is_(True),
            )
        )
        if business is None or membership is None:
            raise CommerceError("forbidden", "An active business membership is required", 403)
        yield Actor(session, tenant_id, principal.subject, membership.role)


def get_actor(
    request: Request,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    business_id: Annotated[str | None, Header(alias="X-Business-ID")] = None,
):
    yield from _get_actor(request, authorization, business_id)


def get_membership_actor(
    request: Request,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    business_id: Annotated[str | None, Header(alias="X-Business-ID")] = None,
):
    yield from _get_actor(request, authorization, business_id, membership_write=True)


# Finish the transaction before FastAPI sends a successful response.
ActorDep = Annotated[Actor, Depends(get_actor, scope="function")]
MembershipActorDep = Annotated[Actor, Depends(get_membership_actor, scope="function")]
