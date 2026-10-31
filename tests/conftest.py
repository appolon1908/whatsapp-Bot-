"""Real PostgreSQL integration fixtures, isolated by a fresh business per test.

Set TEST_DATABASE_ADMIN_URL to a privileged throwaway PostgreSQL 16+ cluster.
There is deliberately no SQLite substitute and no silent integration skip.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from whatsapp_bot.db import make_engine, validate_database_name
from whatsapp_bot.db import tenant_session as scoped_session
from whatsapp_bot.models import Business, Channel, Membership
from whatsapp_bot.provision import migrate_database, provision_database


@pytest.fixture(scope="session")
def database_urls():
    admin = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if not admin:
        pytest.fail(
            "Real PostgreSQL integration requires TEST_DATABASE_ADMIN_URL for an isolated test cluster"
        )
    database = os.environ.get("COMMERCE_TEST_DATABASE_NAME", "codestra_whatsapp_commerce_test")
    validate_database_name(database)
    if not database.startswith("codestra_whatsapp_commerce_test"):
        pytest.fail("Tests require a codestra_whatsapp_commerce_test-prefixed database")
    # Explicit test operator action. Password can persist across repeated local runs.
    password = os.environ.get("TEST_DATABASE_APP_PASSWORD", "local-isolated-test-app-password-only")
    provision_database(
        admin,
        database,
        password,
        migrator_password=os.environ.get(
            "TEST_DATABASE_MIGRATOR_PASSWORD", "local-isolated-test-migrator-password-only"
        ),
    )
    owner = make_url(admin).set(database=database)
    migrate_database(
        owner.set(
            username="codestra_whatsapp_migrator",
            password=os.environ.get(
                "TEST_DATABASE_MIGRATOR_PASSWORD", "local-isolated-test-migrator-password-only"
            ),
        )
    )
    app = owner.set(username="codestra_whatsapp_app", password=password)
    return owner, app


@pytest.fixture(scope="session")
def admin_engine(database_urls):
    engine = make_engine(database_urls[0])
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrator_url(database_urls):
    return database_urls[0].set(
        username="codestra_whatsapp_migrator",
        password=os.environ.get(
            "TEST_DATABASE_MIGRATOR_PASSWORD", "local-isolated-test-migrator-password-only"
        ),
    )


@pytest.fixture(scope="session")
def engine(database_urls):
    engine = make_engine(database_urls[1])
    yield engine
    engine.dispose()


@pytest.fixture
def business_factory(admin_engine):
    def create(**kwargs):
        tenant = kwargs.pop("id", uuid4())
        subject = kwargs.pop("owner_subject", "test-owner")
        values = {"name": "Test business", "currency": "USD", "timezone": "UTC", "tax_bps": 0}
        values.update(kwargs)
        with scoped_session(admin_engine, tenant) as session:
            session.add(Business(id=tenant, **values))
            session.flush()
            session.add(Membership(tenant_id=tenant, subject=subject, role="owner"))
            session.add(
                Channel(
                    tenant_id=tenant,
                    provider="meta",
                    account_id=str(tenant),
                    phone_number_id="test-phone",
                    active=True,
                    verification_reference="isolated-test-verified",
                )
            )
        return tenant

    return create


@pytest.fixture
def tenant_a(business_factory):
    return business_factory()


@pytest.fixture
def tenant_b(business_factory):
    return business_factory()


@pytest.fixture
def tenant_session(engine):
    return lambda tenant: scoped_session(engine, tenant)
