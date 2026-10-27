"""Transaction-scoped tenant context; runtime connections never own schema objects."""

import re
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

DATABASE_PATTERN = re.compile(r"codestra_whatsapp_commerce(?:_[a-z0-9_]+)?\Z")


def validate_database_name(name: str):
    if not DATABASE_PATTERN.fullmatch(name) or len(name) > 63:
        raise ValueError("Refusing unrelated or invalid commerce database target")


def make_engine(url: str):
    url = make_url(url)
    if url.drivername not in ("postgresql", "postgresql+psycopg"):
        raise ValueError("Only PostgreSQL with psycopg3 is supported")
    if not url.database or {"dbname", "database", "service", "servicefile", "dsn"} & {
        key.lower() for key in url.query
    }:
        raise ValueError("An explicit database path is required; target override options are forbidden")
    url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, hide_parameters=True, pool_size=5, max_overflow=5)


def assert_database_target(connection):
    if connection.scalar(text("SELECT current_database()")) != connection.engine.url.database:
        raise RuntimeError("Effective database does not match the explicit database target")


@contextmanager
def tenant_session(engine, tenant_id: UUID):
    tenant_id = UUID(str(tenant_id))
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)})
        yield session


def lookup_channel(engine, provider: str, account_id: str, phone_number_id: str):
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT tenant_id, channel_id FROM commerce.lookup_channel(:provider,:account,:phone)"),
            {"provider": provider, "account": account_id, "phone": phone_number_id},
        ).first()
        return (row.tenant_id, row.channel_id) if row else None


def assert_runtime_role(engine):
    with engine.connect() as connection:
        role = connection.execute(
            text("""SELECT current_database() AS db, r.rolname, r.rolsuper, r.rolbypassrls,
            r.rolcreatedb, r.rolcreaterole,
            pg_has_role(current_user, 'codestra_whatsapp_owner', 'MEMBER') AS owner_member,
            EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='commerce' AND c.relowner=r.oid) AS owns_tables
            FROM pg_roles r WHERE r.rolname=current_user""")
        ).one()
        try:
            validate_database_name(role.db)
        except ValueError as exc:
            raise RuntimeError("Runtime database target is not an isolated commerce database") from exc
        if role.db != engine.url.database:
            raise RuntimeError("Runtime connection does not match the explicit database target")
        if (
            role.rolname != "codestra_whatsapp_app"
            or role.rolsuper
            or role.rolbypassrls
            or role.rolcreatedb
            or role.rolcreaterole
            or role.owner_member
            or role.owns_tables
        ):
            raise RuntimeError("Runtime role must not be privileged or own commerce tables")
