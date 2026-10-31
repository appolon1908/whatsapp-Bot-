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


def activate_operator_role(connection):
    """Assume the NOLOGIN owner only inside an explicit migrator transaction."""
    validate_database_name(connection.engine.url.database or "")
    if not connection.in_transaction():
        raise RuntimeError("Explicit operator transaction is required")
    assert_database_target(connection)
    role = connection.execute(
        text("""SELECT session_user AS login_role, current_user AS effective_role,
        r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb
            OR r.rolcreaterole OR r.rolinherit OR r.rolreplication) AS safe_login,
        NOT (o.rolcanlogin OR o.rolsuper OR o.rolbypassrls OR o.rolcreatedb
            OR o.rolcreaterole OR o.rolinherit OR o.rolreplication) AS safe_owner,
        pg_has_role(r.oid,o.oid,'SET') AS can_set_owner,
        pg_has_role(r.oid,o.oid,'USAGE') AS inherits_owner,
        EXISTS (SELECT 1 FROM pg_database d
            WHERE d.datname=current_database() AND d.datdba=o.oid) AS owns_database,
        EXISTS (SELECT 1 FROM pg_auth_members m
            WHERE (m.member IN (r.oid,o.oid) OR m.roleid IN (r.oid,o.oid))
            AND NOT (m.member=r.oid AND m.roleid=o.oid AND m.set_option
                     AND NOT m.inherit_option AND NOT m.admin_option)) AS unsafe_memberships
        FROM pg_roles r JOIN pg_roles o ON o.rolname='codestra_whatsapp_owner'
        WHERE r.rolname=session_user""")
    ).one_or_none()
    if (
        role is None
        or role.login_role != "codestra_whatsapp_migrator"
        or role.effective_role != "codestra_whatsapp_migrator"
        or not role.safe_login
        or not role.safe_owner
        or not role.can_set_owner
        or role.inherits_owner
        or not role.owns_database
        or role.unsafe_memberships
    ):
        raise RuntimeError("Explicit restricted migrator operator connection is required")
    connection.execute(text("SET LOCAL ROLE codestra_whatsapp_owner"))


def assert_runtime_role(engine):
    with engine.connect() as connection:
        role = connection.execute(
            text("""SELECT current_database() AS db, session_user AS login_role,
            r.rolname, r.rolsuper, r.rolbypassrls,
            r.rolcreatedb, r.rolcreaterole, r.rolinherit, r.rolreplication, r.rolcanlogin,
            EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid) AS role_member,
            EXISTS (SELECT 1 FROM pg_shdepend d WHERE d.refclassid='pg_authid'::regclass
                    AND d.refobjid=r.oid AND d.deptype='o'
                    AND d.dbid IN (0,(SELECT oid FROM pg_database WHERE datname=current_database())))
                    AS owns_objects,
            has_database_privilege(r.oid,current_database(),'CREATE')
                OR has_database_privilege(r.oid,current_database(),'TEMPORARY') OR EXISTS (
                SELECT 1 FROM pg_namespace n WHERE has_schema_privilege(r.oid,n.oid,'CREATE'))
                    AS can_create_objects
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
            or role.login_role != "codestra_whatsapp_app"
            or role.rolsuper
            or role.rolbypassrls
            or role.rolcreatedb
            or role.rolcreaterole
            or role.rolinherit
            or role.rolreplication
            or not role.rolcanlogin
            or role.role_member
            or role.owns_objects
            or role.can_create_objects
        ):
            raise RuntimeError("Runtime role must not be privileged or own schema objects")
