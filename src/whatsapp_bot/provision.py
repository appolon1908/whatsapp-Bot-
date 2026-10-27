"""Explicit privileged operator actions. Never imported for automatic startup effects."""

from importlib.resources import as_file, files
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from whatsapp_bot.db import assert_database_target, make_engine, tenant_session, validate_database_name
from whatsapp_bot.models import AuditEvent, Business, Channel, Membership

OWNER = "codestra_whatsapp_owner"
APP = "codestra_whatsapp_app"


def provision_database(admin_url, database_name, app_password):
    """Create only the explicitly named commerce DB and dedicated global roles.

    Existing role passwords are intentionally not rotated. pg_hba restrictions are a
    separate operator action; PostgreSQL PUBLIC CONNECT on other databases is not
    revoked here because that would modify unrelated databases.
    """
    validate_database_name(database_name)
    if not isinstance(app_password, str) or len(app_password) < 24:
        raise ValueError("Application password must contain at least 24 characters")
    engine = make_engine(admin_url)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            assert_database_target(connection)
            dbapi = connection.connection.driver_connection
            with dbapi.cursor() as cursor:
                cursor.execute(
                    "SELECT rolname,rolsuper,rolbypassrls,rolcanlogin,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname IN (%s,%s)",
                    (OWNER, APP),
                )
                existing = {r[0]: r for r in cursor.fetchall()}
                for name in (OWNER, APP):
                    if name in existing:
                        _, superuser, bypass, login, createdb, createrole = existing[name]
                        if superuser or bypass or createdb or createrole or (name == OWNER and login):
                            raise RuntimeError("Existing dedicated role has unsafe attributes")
                    elif name == OWNER:
                        cursor.execute(
                            sql.SQL(
                                "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                            ).format(sql.Identifier(name))
                        )
                    else:
                        cursor.execute(
                            sql.SQL(
                                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                            ).format(sql.Identifier(name), sql.Literal(app_password))
                        )
                cursor.execute("SELECT pg_has_role(%s,%s,'MEMBER')", (APP, OWNER))
                if cursor.fetchone()[0]:
                    raise RuntimeError("Application role must not be an owner-role member")
                cursor.execute(
                    "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (database_name,)
                )
                existing_db = cursor.fetchone()
                if existing_db and existing_db[0] != OWNER:
                    raise RuntimeError("Existing commerce database must be owned by dedicated commerce owner")
                if not existing_db:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            sql.Identifier(database_name), sql.Identifier(OWNER)
                        )
                    )
                cursor.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database_name))
                )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database_name), sql.Identifier(APP)
                    )
                )
        database_url = make_url(admin_url).set(database=database_name)
        target = make_engine(database_url)
        try:
            with target.begin() as connection:
                assert_database_target(connection)
                connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        finally:
            target.dispose()
        return {"database_name": database_name, "runtime_role": APP, "requires_pg_hba_restriction": True}
    finally:
        engine.dispose()


def _assert_operator(connection):
    assert_database_target(connection)
    allowed = connection.scalar(
        text(
            "SELECT rolsuper OR pg_has_role(current_user, 'codestra_whatsapp_owner', 'MEMBER') FROM pg_roles WHERE rolname=current_user"
        )
    )
    if not allowed:
        raise RuntimeError("Explicit privileged operator connection is required")


def migrate_database(owner_url):
    validate_database_name(make_url(owner_url).database or "")
    # Wheel builds map the canonical repository migrations directory into package
    # resources. Source/editable checkouts use that same canonical directory.
    resource = files("whatsapp_bot").joinpath("migrations")
    if not resource.is_dir():
        resource = Path(__file__).resolve().parents[2] / "migrations"
    if not resource.is_dir():
        raise RuntimeError("Packaged commerce migrations are missing")
    engine = make_engine(owner_url)
    try:
        with as_file(resource) as migration_path:
            config = Config()
            config.set_main_option("script_location", str(migration_path))
            with engine.begin() as connection:
                _assert_operator(connection)
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
    finally:
        engine.dispose()


def bootstrap_business(owner_url, name, currency, timezone, owner_subject, tax_bps):
    validate_database_name(make_url(owner_url).database or "")
    if (
        currency not in ("USD", "DOP")
        or not isinstance(tax_bps, int)
        or isinstance(tax_bps, bool)
        or not 0 <= tax_bps <= 10000
    ):
        raise ValueError("Currency and explicit tax basis points must be valid")
    if not name or len(name) > 200 or not owner_subject or len(owner_subject) > 255:
        raise ValueError("Business name and owner subject are required")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Business timezone must be an IANA timezone") from exc
    engine = make_engine(owner_url)
    tenant = uuid4()
    try:
        with tenant_session(engine, tenant) as session:
            _assert_operator(session.connection())
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"onboard:{owner_subject}:{name}"},
            )
            candidates = list(session.scalars(select(Business).where(Business.name == name)))
            for candidate in candidates:
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(candidate.id)}
                )
                membership = session.scalar(
                    select(Membership).where(
                        Membership.tenant_id == candidate.id,
                        Membership.subject == owner_subject,
                        Membership.role == "owner",
                    )
                )
                if membership is not None:
                    if (candidate.currency, candidate.timezone, candidate.tax_bps) != (
                        currency,
                        timezone,
                        tax_bps,
                    ):
                        raise ValueError("Onboarding request conflicts with existing business configuration")
                    return {
                        "id": str(candidate.id),
                        "name": candidate.name,
                        "currency": candidate.currency,
                        "timezone": candidate.timezone,
                        "tax_bps": candidate.tax_bps,
                        "active": candidate.active,
                    }
            session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant)}
            )
            business = Business(id=tenant, name=name, currency=currency, timezone=timezone, tax_bps=tax_bps)
            session.add(business)
            session.flush()
            session.add(Membership(tenant_id=tenant, subject=owner_subject, role="owner"))
            session.flush()
            return {
                "id": str(tenant),
                "name": name,
                "currency": currency,
                "timezone": timezone,
                "tax_bps": tax_bps,
                "active": True,
            }
    finally:
        engine.dispose()


def register_channel(
    owner_url, tenant_id, provider, account_id, phone_number_id, verification_reference, policy_campaign_id
):
    """Record an operator attestation; this performs no remote provider verification.

    Binding ownership is permanent here. Reactivation requires a fresh evidence
    reference; a repeated request with unchanged active binding is idempotent.
    """
    validate_database_name(make_url(owner_url).database or "")
    tenant_id = UUID(str(tenant_id))
    if provider not in ("meta", "evolution"):
        raise ValueError("Provider must be meta or evolution")
    for label, value, maximum in (
        ("account_id", account_id, 200),
        ("phone_number_id", phone_number_id, 200),
        ("verification_reference", verification_reference, 255),
        ("policy_campaign_id", policy_campaign_id, 200),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"Valid {label} is required")
    engine = make_engine(owner_url)
    try:
        with tenant_session(engine, tenant_id) as session:
            _assert_operator(session.connection())
            business = session.scalar(
                select(Business).where(Business.id == tenant_id, Business.active.is_(True))
            )
            if business is None:
                raise ValueError("Active business is required")
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"channel:{provider}:{account_id}:{phone_number_id}"},
            )
            # Explicitly privileged ownership check across bindings; never exposed
            # through runtime service/API queries or returning another tenant's data.
            channel = session.scalar(
                select(Channel)
                .where(
                    Channel.provider == provider,
                    Channel.account_id == account_id,
                    Channel.phone_number_id == phone_number_id,
                )
                .with_for_update()
            )
            if channel is not None and channel.tenant_id != tenant_id:
                raise ValueError("Provider binding is already bound to a different business")
            if channel is None:
                channel = Channel(
                    tenant_id=tenant_id,
                    provider=provider,
                    account_id=account_id,
                    phone_number_id=phone_number_id,
                    active=True,
                    verification_reference=verification_reference,
                    policy_campaign_id=policy_campaign_id,
                )
                session.add(channel)
                session.flush()
                action = "channel.registered"
            else:
                if not channel.active and channel.verification_reference == verification_reference:
                    raise ValueError("Reactivation requires a fresh verification reference")
                unchanged = (
                    channel.active
                    and channel.verification_reference == verification_reference
                    and channel.policy_campaign_id == policy_campaign_id
                )
                channel.active = True
                channel.verification_reference = verification_reference
                channel.policy_campaign_id = policy_campaign_id
                action = None if unchanged else "channel.verified"
            if action:
                actor = session.scalar(text("SELECT current_user"))
                session.add(
                    AuditEvent(
                        tenant_id=tenant_id,
                        actor=f"operator:{actor}",
                        action=action,
                        object_type="channels",
                        object_id=channel.id,
                        details={"verification_reference": verification_reference},
                    )
                )
            session.flush()
            return {
                "id": str(channel.id),
                "tenant_id": str(channel.tenant_id),
                "provider": channel.provider,
                "account_id": channel.account_id,
                "phone_number_id": channel.phone_number_id,
                "active": channel.active,
                "verification_reference": channel.verification_reference,
                "policy_campaign_id": channel.policy_campaign_id,
            }
    finally:
        engine.dispose()
