import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from whatsapp_bot.db import lookup_channel, make_engine
from whatsapp_bot.provision import migrate_database, provision_database


def test_migration_rerun_keeps_data_and_all_tenant_tables_forced(
    migrator_url, admin_engine, engine, tenant_a
):
    migrate_database(migrator_url)
    migrate_database(migrator_url)
    with admin_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_commerce"
        tables = connection.execute(
            text(
                "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,r.rolname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='commerce' AND c.relkind='r'"
            )
        ).all()
        assert len(tables) == 16
        assert all(row[1] and row[2] and row[3] == "codestra_whatsapp_owner" for row in tables)
    assert lookup_channel(engine, "meta", str(tenant_a), "test-phone")[0] == tenant_a


def test_runtime_cannot_truncate_create_or_activate_channel(engine, tenant_a, tenant_session):
    for sql in ("TRUNCATE commerce.products", "CREATE TABLE commerce.bad (x integer)"):
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(sql))
    with pytest.raises(DBAPIError), tenant_session(tenant_a) as session:
        session.execute(text("UPDATE commerce.channels SET active=true"))
    with pytest.raises(DBAPIError), tenant_session(tenant_a) as session:
        session.execute(text("UPDATE commerce.channels SET verification_reference='forged'"))
    with tenant_session(tenant_a) as session:
        session.execute(text("UPDATE commerce.channels SET active=false"))
    assert lookup_channel(engine, "meta", str(tenant_a), "test-phone") is None


def test_route_function_not_public_and_inactive_business_not_routed(admin_engine, engine, tenant_a):
    with admin_engine.begin() as connection:
        assert connection.scalar(
            text(
                "SELECT NOT has_function_privilege('public','commerce.lookup_channel(text,text,text)','EXECUTE')"
            )
        )
        connection.execute(text("UPDATE commerce.businesses SET active=false WHERE id=:id"), {"id": tenant_a})
    assert lookup_channel(engine, "meta", str(tenant_a), "test-phone") is None


def test_database_guard_precedes_network_and_plain_url_uses_psycopg():
    with pytest.raises(ValueError):
        provision_database(
            "postgresql://nobody:unavailable@127.0.0.1:1/postgres",
            "unrelated",
            "a" * 32,
            migrator_password="m" * 32,
        )
    with pytest.raises(ValueError):
        make_engine("sqlite://")
    engine = make_engine("postgresql://nobody@127.0.0.1:1/codestra_whatsapp_commerce_test")
    assert engine.dialect.driver == "psycopg"
    engine.dispose()


def test_bootstrap_repeated_request_is_non_destructive_and_requires_operator(database_urls, migrator_url):
    from uuid import uuid4

    from whatsapp_bot.provision import bootstrap_business

    name = f"Onboard-{uuid4()}"
    first = bootstrap_business(migrator_url, name, "USD", "UTC", "idempotent-owner", 125)
    second = bootstrap_business(migrator_url, name, "USD", "UTC", "idempotent-owner", 125)
    assert first["id"] == second["id"]
    with pytest.raises(ValueError, match="existing"):
        bootstrap_business(migrator_url, name, "USD", "UTC", "idempotent-owner", 126)
    with pytest.raises(RuntimeError, match="operator"):
        bootstrap_business(database_urls[1], f"Invalid-{uuid4()}", "USD", "UTC", "attacker", 0)


def test_register_channel_requires_operator_and_never_reassigns_tenant(
    database_urls, migrator_url, tenant_a, tenant_b, engine
):
    from uuid import UUID, uuid4

    from whatsapp_bot.provision import register_channel

    account = f"operator-{uuid4()}"
    kwargs = {
        "tenant_id": tenant_a,
        "provider": "meta",
        "account_id": account,
        "phone_number_id": "verified-phone",
        "verification_reference": "operator-evidence-1",
        "policy_campaign_id": "approved-campaign",
    }
    with pytest.raises(RuntimeError, match="operator"):
        register_channel(database_urls[1], **kwargs)
    result = register_channel(migrator_url, **kwargs)
    assert result["active"] is True
    assert register_channel(migrator_url, **kwargs)["id"] == result["id"]
    assert lookup_channel(engine, "meta", account, "verified-phone") == (tenant_a, UUID(result["id"]))
    with pytest.raises(ValueError, match="bound"):
        register_channel(migrator_url, **{**kwargs, "tenant_id": tenant_b})
    with pytest.raises(ValueError, match="verification"):
        register_channel(migrator_url, **{**kwargs, "verification_reference": ""})
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_a)}
        )
        connection.execute(
            text("UPDATE commerce.channels SET active=false WHERE id=:id"), {"id": result["id"]}
        )
    with pytest.raises(ValueError, match="fresh verification"):
        register_channel(migrator_url, **kwargs)
    reactivated = register_channel(
        migrator_url, **{**kwargs, "verification_reference": "operator-evidence-2"}
    )
    assert reactivated["active"] is True
    assert reactivated["id"] == result["id"]
