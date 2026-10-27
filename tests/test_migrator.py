"""Real login and owner-activation regressions for explicit commerce operators."""

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from whatsapp_bot.db import assert_runtime_role, make_engine
from whatsapp_bot.provision import bootstrap_business, migrate_database, provision_database, register_channel


@pytest.fixture
def isolated_database():
    """Allocate a unique test target; never drop the suite's shared database."""
    admin = make_url(os.environ["TEST_DATABASE_ADMIN_URL"])
    database = f"codestra_whatsapp_commerce_test_migrator_{uuid4().hex[:12]}"
    target = admin.set(database=database)
    yield target
    control = make_engine(admin)
    try:
        with control.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
    finally:
        control.dispose()


def _passwords():
    return (
        os.environ.get("TEST_DATABASE_APP_PASSWORD", "local-isolated-test-app-password-only"),
        os.environ.get("TEST_DATABASE_MIGRATOR_PASSWORD", "local-isolated-test-migrator-password-only"),
    )


def _provision(target):
    app_password, migrator_password = _passwords()
    return provision_database(
        os.environ["TEST_DATABASE_ADMIN_URL"],
        target.database,
        app_password,
        migrator_password=migrator_password,
    )


def _migrator(target):
    return target.set(username="codestra_whatsapp_migrator", password=_passwords()[1])


def test_provision_creates_migrator_with_only_explicit_owner_activation(isolated_database):
    _provision(isolated_database)
    admin = make_engine(isolated_database)
    migrator = make_engine(_migrator(isolated_database))
    app = make_engine(isolated_database.set(username="codestra_whatsapp_app", password=_passwords()[0]))
    try:
        with admin.connect() as connection:
            roles = connection.execute(
                text(
                    "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,"
                    "rolinherit,rolreplication,rolbypassrls FROM pg_roles "
                    "WHERE rolname IN ('codestra_whatsapp_owner','codestra_whatsapp_app',"
                    "'codestra_whatsapp_migrator') ORDER BY rolname"
                )
            ).all()
            assert roles == [
                ("codestra_whatsapp_app", True, False, False, False, False, False, False),
                ("codestra_whatsapp_migrator", True, False, False, False, False, False, False),
                ("codestra_whatsapp_owner", False, False, False, False, False, False, False),
            ]
            assert connection.execute(
                text(
                    "SELECT m.inherit_option,m.set_option,m.admin_option FROM pg_auth_members m "
                    "JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member "
                    "WHERE r.rolname='codestra_whatsapp_owner' "
                    "AND u.rolname='codestra_whatsapp_migrator'"
                )
            ).all() == [(False, True, False)]
        with migrator.connect() as connection:
            assert connection.execute(text("SELECT session_user,current_user")).one() == (
                "codestra_whatsapp_migrator",
                "codestra_whatsapp_migrator",
            )
            with pytest.raises(DBAPIError):
                connection.execute(text("CREATE TABLE public.unactivated_migrator (id integer)"))
        for role in ("codestra_whatsapp_owner", "codestra_whatsapp_migrator"):
            with app.begin() as connection, pytest.raises(DBAPIError):
                connection.exec_driver_sql(f"SET ROLE {role}")
    finally:
        app.dispose()
        migrator.dispose()
        admin.dispose()


def test_runtime_rejects_admin_login_even_when_startup_role_is_app(database_urls):
    masked = make_engine(database_urls[0].update_query_dict({"options": "-c role=codestra_whatsapp_app"}))
    try:
        with masked.connect() as connection:
            original, effective = connection.execute(text("SELECT session_user,current_user")).one()
            assert original == database_urls[0].username
            assert effective == "codestra_whatsapp_app"
        with pytest.raises(RuntimeError, match="Runtime role"):
            assert_runtime_role(masked)
    finally:
        masked.dispose()


@pytest.mark.parametrize("migrator_password", ["", "short", "a" * 32])
def test_migrator_password_is_strong_and_independent_before_connecting(migrator_password):
    with pytest.raises(ValueError, match="password"):
        provision_database(
            "postgresql://unused@127.0.0.1:1/postgres",
            "codestra_whatsapp_commerce_test_password",
            "a" * 32,
            migrator_password=migrator_password,
        )


def test_fresh_migration_and_rerun_use_owner_for_every_object(isolated_database):
    _provision(isolated_database)
    migrate_database(_migrator(isolated_database))
    migrate_database(_migrator(isolated_database))
    admin = make_engine(isolated_database)
    try:
        with admin.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT pg_get_userbyid(relowner) FROM pg_class "
                        "WHERE oid='public.alembic_version'::regclass"
                    )
                ).scalar_one()
                == "codestra_whatsapp_owner"
            )
            assert (
                connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "0001_commerce"
            )
            assert connection.scalar(
                text(
                    "SELECT count(*)=16 AND bool_and(c.relrowsecurity AND c.relforcerowsecurity "
                    "AND r.rolname='codestra_whatsapp_owner') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='commerce' AND c.relkind='r'"
                )
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(proowner) FROM pg_proc "
                        "WHERE oid='commerce.lookup_channel(text,text,text)'::regprocedure"
                    )
                )
                == "codestra_whatsapp_owner"
            )
    finally:
        admin.dispose()


@pytest.mark.parametrize("existing_revision", [False, True])
def test_followup_revision_runs_as_owner_and_updates_version(
    isolated_database, tmp_path, monkeypatch, existing_revision
):
    from whatsapp_bot import provision

    _provision(isolated_database)
    if existing_revision:
        migrate_database(_migrator(isolated_database))
    migration_path = tmp_path / "migrations"
    shutil.copytree(Path(__file__).resolve().parents[1] / "migrations", migration_path)
    (migration_path / "versions/0002_probe.py").write_text(
        "from alembic import op\n"
        "revision = '0002_probe'\n"
        "down_revision = '0001_commerce'\n"
        "def upgrade():\n"
        "    op.execute('CREATE TABLE commerce.migration_probe (actor text NOT NULL)')\n"
        "    op.execute('INSERT INTO commerce.migration_probe VALUES (current_user)')\n"
    )
    monkeypatch.setattr(provision, "files", lambda _: tmp_path)
    migrate_database(_migrator(isolated_database))
    admin = make_engine(isolated_database)
    try:
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "0002_probe"
            assert (
                connection.scalar(text("SELECT actor FROM commerce.migration_probe"))
                == "codestra_whatsapp_owner"
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='commerce.migration_probe'::regclass"
                    )
                )
                == "codestra_whatsapp_owner"
            )
    finally:
        admin.dispose()


def test_failed_followup_migration_rolls_back_schema_and_version(isolated_database, tmp_path, monkeypatch):
    from whatsapp_bot import provision

    _provision(isolated_database)
    migrate_database(_migrator(isolated_database))
    migration_path = tmp_path / "migrations"
    shutil.copytree(Path(__file__).resolve().parents[1] / "migrations", migration_path)
    (migration_path / "versions/0002_failure.py").write_text(
        "from alembic import op\n"
        "revision = '0002_failure'\n"
        "down_revision = '0001_commerce'\n"
        "def upgrade():\n"
        "    op.execute('CREATE TABLE commerce.failed_migration_probe (id integer)')\n"
        "    raise RuntimeError('expected migration failure')\n"
    )
    monkeypatch.setattr(provision, "files", lambda _: tmp_path)
    with pytest.raises(RuntimeError, match="expected migration failure"):
        migrate_database(_migrator(isolated_database))
    admin = make_engine(isolated_database)
    try:
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('commerce.failed_migration_probe')")) is None
            assert (
                connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "0001_commerce"
            )
    finally:
        admin.dispose()


def test_provision_hands_off_only_known_legacy_version_table(isolated_database):
    _provision(isolated_database)
    admin = make_engine(isolated_database)
    tenant = uuid4()
    try:
        with admin.begin() as connection:
            connection.execute(
                text("CREATE TABLE public.alembic_version (version_num varchar(32) PRIMARY KEY)")
            )
            connection.execute(text("INSERT INTO public.alembic_version VALUES ('0001_commerce')"))
            connection.exec_driver_sql(
                (Path(__file__).resolve().parents[1] / "migrations/versions/0001_schema.sql").read_text()
            )
            connection.execute(
                text(
                    "INSERT INTO commerce.businesses (id,name,currency,timezone,active,tax_bps,settings,created_at) "
                    "VALUES (:id,'Legacy business','USD','UTC',true,125,'{}',now())"
                ),
                {"id": tenant},
            )
        _provision(isolated_database)
        with admin.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.alembic_version'::regclass"
                    )
                )
                == "codestra_whatsapp_owner"
            )
        migrate_database(_migrator(isolated_database))
        with admin.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "0001_commerce"
            )
            assert (
                connection.scalar(text("SELECT name FROM commerce.businesses WHERE id=:id"), {"id": tenant})
                == "Legacy business"
            )
    finally:
        admin.dispose()


@pytest.mark.parametrize(
    "definition,version", [("text", "0001_commerce"), ("varchar(32)", "unknown_revision")]
)
def test_provision_refuses_unknown_legacy_version_table(isolated_database, definition, version):
    _provision(isolated_database)
    admin = make_engine(isolated_database)
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE TABLE public.alembic_version (version_num {definition} PRIMARY KEY)"
            )
            connection.execute(
                text("INSERT INTO public.alembic_version VALUES (:version)"), {"version": version}
            )
        with pytest.raises(RuntimeError, match="version table"):
            _provision(isolated_database)
        with admin.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.alembic_version'::regclass"
                    )
                )
                == isolated_database.username
            )
            assert connection.scalar(text("SELECT version_num FROM public.alembic_version")) == version
    finally:
        admin.dispose()


def test_provision_refuses_matching_version_table_owned_by_unrelated_role(isolated_database):
    _provision(isolated_database)
    admin = make_engine(isolated_database)
    unrelated_owner = f"commerce_version_probe_{uuid4().hex[:12]}"
    try:
        with admin.begin() as connection:
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(unrelated_owner)))
                cursor.execute("CREATE TABLE public.alembic_version (version_num varchar(32) PRIMARY KEY)")
                cursor.execute("INSERT INTO public.alembic_version VALUES ('0001_commerce')")
                cursor.execute(
                    sql.SQL("ALTER TABLE public.alembic_version OWNER TO {}").format(
                        sql.Identifier(unrelated_owner)
                    )
                )
        with pytest.raises(RuntimeError, match="version table"):
            _provision(isolated_database)
        with admin.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.alembic_version'::regclass"
                    )
                )
                == unrelated_owner
            )
    finally:
        with admin.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(unrelated_owner)))
        admin.dispose()


@pytest.mark.parametrize("baggage", ["trigger", "inheritance", "partition", "default", "extra_constraint"])
def test_provision_refuses_legacy_version_table_with_unexpected_behavior(isolated_database, baggage):
    _provision(isolated_database)
    admin = make_engine(isolated_database)
    try:
        with admin.begin() as connection:
            if baggage == "partition":
                connection.execute(
                    text(
                        "CREATE TABLE public.version_parent (version_num varchar(32) PRIMARY KEY) PARTITION BY RANGE (version_num)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TABLE public.alembic_version PARTITION OF public.version_parent FOR VALUES FROM ('0000') TO ('0009')"
                    )
                )
            else:
                connection.execute(
                    text("CREATE TABLE public.alembic_version (version_num varchar(32) PRIMARY KEY)")
                )
            connection.execute(text("INSERT INTO public.alembic_version VALUES ('0001_commerce')"))
            if baggage == "trigger":
                connection.execute(
                    text(
                        "CREATE FUNCTION public.version_probe_trigger() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TRIGGER version_probe BEFORE INSERT ON public.alembic_version FOR EACH ROW EXECUTE FUNCTION public.version_probe_trigger()"
                    )
                )
            elif baggage == "inheritance":
                connection.execute(
                    text("CREATE TABLE public.version_child () INHERITS (public.alembic_version)")
                )
            elif baggage == "default":
                connection.execute(
                    text(
                        "ALTER TABLE public.alembic_version ALTER COLUMN version_num SET DEFAULT '0001_commerce'"
                    )
                )
            elif baggage == "extra_constraint":
                connection.execute(
                    text("ALTER TABLE public.alembic_version ADD CHECK (length(version_num)>0)")
                )
        with pytest.raises(RuntimeError, match="version table"):
            _provision(isolated_database)
        with admin.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.alembic_version'::regclass"
                    )
                )
                == isolated_database.username
            )
    finally:
        admin.dispose()


def test_standalone_alembic_commits_with_pinned_public_version_table(isolated_database):
    from alembic import command
    from alembic.config import Config

    _provision(isolated_database)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        _migrator(isolated_database)
        .update_query_dict({"options": "-c search_path=commerce"})
        .render_as_string(hide_password=False)
        .replace("%", "%%"),
    )
    command.upgrade(config, "head")
    admin = make_engine(isolated_database)
    try:
        with admin.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "0001_commerce"
            )
            assert connection.scalar(text("SELECT to_regclass('commerce.alembic_version')")) is None
    finally:
        admin.dispose()


@pytest.mark.parametrize(
    "role,attribute,restore",
    [
        ("codestra_whatsapp_app", "REPLICATION", "NOREPLICATION"),
        ("codestra_whatsapp_migrator", "INHERIT", "NOINHERIT"),
        ("codestra_whatsapp_owner", "LOGIN", "NOLOGIN"),
        ("codestra_whatsapp_app", "NOLOGIN", "LOGIN"),
    ],
)
def test_provision_refuses_unsafe_roles_before_creating_database(
    isolated_database, admin_engine, role, attribute, restore
):
    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER ROLE {role} {attribute}")
        with pytest.raises(RuntimeError, match="unsafe attributes"):
            _provision(isolated_database)
        with admin_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT oid FROM pg_database WHERE datname=:name"),
                    {"name": isolated_database.database},
                )
                is None
            )
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER ROLE {role} {restore}")


@pytest.mark.parametrize(
    "grant,restore",
    [
        (
            "GRANT codestra_whatsapp_owner TO codestra_whatsapp_migrator WITH INHERIT TRUE",
            "GRANT codestra_whatsapp_owner TO codestra_whatsapp_migrator WITH INHERIT FALSE",
        ),
        (
            "GRANT codestra_whatsapp_owner TO codestra_whatsapp_migrator WITH SET FALSE",
            "GRANT codestra_whatsapp_owner TO codestra_whatsapp_migrator WITH SET TRUE",
        ),
        (
            "GRANT codestra_whatsapp_owner TO codestra_whatsapp_migrator WITH ADMIN TRUE",
            "GRANT codestra_whatsapp_owner TO codestra_whatsapp_migrator WITH ADMIN FALSE",
        ),
        (
            "GRANT codestra_whatsapp_migrator TO codestra_whatsapp_app WITH INHERIT FALSE, SET TRUE",
            "REVOKE codestra_whatsapp_migrator FROM codestra_whatsapp_app",
        ),
    ],
)
def test_provision_refuses_unsafe_memberships_without_repair(isolated_database, admin_engine, grant, restore):
    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(grant)
            memberships = connection.execute(
                text(
                    "SELECT roleid,member,grantor,inherit_option,set_option,admin_option FROM pg_auth_members ORDER BY oid"
                )
            ).all()
        with pytest.raises(RuntimeError, match="unsafe memberships"):
            _provision(isolated_database)
        with admin_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT oid FROM pg_database WHERE datname=:name"),
                    {"name": isolated_database.database},
                )
                is None
            )
            assert (
                connection.execute(
                    text(
                        "SELECT roleid,member,grantor,inherit_option,set_option,admin_option FROM pg_auth_members ORDER BY oid"
                    )
                ).all()
                == memberships
            )
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(restore)


def test_provision_rerun_keeps_existing_login_passwords_and_data(isolated_database):
    _provision(isolated_database)
    admin = make_engine(isolated_database)
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE TABLE public.provision_probe (value integer)"))
            connection.execute(text("INSERT INTO public.provision_probe VALUES (7)"))
        provision_database(
            os.environ["TEST_DATABASE_ADMIN_URL"],
            isolated_database.database,
            "replacement-application-password-only",
            migrator_password="replacement-migrator-password-only",
        )
        for role, password in zip(
            ("codestra_whatsapp_app", "codestra_whatsapp_migrator"), _passwords(), strict=True
        ):
            login = make_engine(isolated_database.set(username=role, password=password))
            try:
                with login.connect() as connection:
                    assert connection.scalar(text("SELECT session_user")) == role
            finally:
                login.dispose()
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT value FROM public.provision_probe")) == 7
    finally:
        # Keep test credentials recoverable even if a regression rotates them.
        with admin.begin() as connection:
            with connection.connection.driver_connection.cursor() as cursor:
                for role, password in zip(
                    ("codestra_whatsapp_app", "codestra_whatsapp_migrator"), _passwords(), strict=True
                ):
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
        admin.dispose()


def test_migrator_cannot_grant_owner_membership(migrator_url):
    operator = make_engine(migrator_url)
    try:
        with operator.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(text("GRANT codestra_whatsapp_owner TO codestra_whatsapp_app"))
    finally:
        operator.dispose()


def test_administrator_is_refused_by_normal_migration_path(database_urls):
    with pytest.raises(RuntimeError, match="migrator operator"):
        migrate_database(database_urls[0])


def test_named_migrator_preserves_onboarding_scope_and_audit_identity(migrator_url, admin_engine):
    name = f"Scoped onboarding {uuid4()}"
    first = bootstrap_business(migrator_url, name, "USD", "UTC", "first-owner", 125)
    second = bootstrap_business(migrator_url, name, "DOP", "UTC", "second-owner", 250)
    assert first["id"] != second["id"]
    assert bootstrap_business(migrator_url, name, "USD", "UTC", "first-owner", 125)["id"] == first["id"]
    channel = register_channel(
        migrator_url, first["id"], "meta", str(uuid4()), "phone", "evidence", "campaign"
    )
    with admin_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT actor FROM commerce.audit_events WHERE object_id=:id"), {"id": channel["id"]}
            )
            == "operator:codestra_whatsapp_migrator"
        )


def test_operator_activation_and_tenant_context_are_transaction_local(migrator_url, tenant_a, tenant_b):
    from whatsapp_bot.db import activate_operator_role, tenant_session

    operator = make_engine(migrator_url)
    try:
        for fail in (False, True):
            try:
                with tenant_session(operator, tenant_a) as session:
                    activate_operator_role(session.connection())
                    assert session.scalar(text("SELECT current_user")) == "codestra_whatsapp_owner"
                    assert session.scalar(text("SELECT current_setting('app.tenant_id')")) == str(tenant_a)
                    assert (
                        session.scalar(
                            text("SELECT count(*) FROM commerce.memberships WHERE tenant_id=:tenant"),
                            {"tenant": tenant_b},
                        )
                        == 0
                    )
                    with pytest.raises(DBAPIError), session.begin_nested():
                        session.execute(
                            text(
                                "INSERT INTO commerce.memberships (id,tenant_id,subject,role,active) VALUES (:id,:tenant,'cross-tenant','owner',true)"
                            ),
                            {"id": uuid4(), "tenant": tenant_b},
                        )
                    if fail:
                        raise ValueError("expected operator rollback")
            except ValueError:
                assert fail
            with operator.connect() as connection:
                assert connection.scalar(text("SELECT current_user")) == "codestra_whatsapp_migrator"
                assert (
                    connection.scalar(text("SELECT nullif(current_setting('app.tenant_id',true),'')")) is None
                )
    finally:
        operator.dispose()


@pytest.mark.parametrize("attribute,restore", [("INHERIT", "NOINHERIT"), ("REPLICATION", "NOREPLICATION")])
def test_runtime_rejects_additional_role_attributes(admin_engine, engine, attribute, restore):
    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER ROLE codestra_whatsapp_app {attribute}")
        with pytest.raises(RuntimeError, match="Runtime role"):
            assert_runtime_role(engine)
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER ROLE codestra_whatsapp_app {restore}")


def test_runtime_rejects_unrelated_role_elevation_membership(admin_engine, engine):
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                text("GRANT pg_read_all_data TO codestra_whatsapp_app WITH INHERIT FALSE, SET TRUE")
            )
        with pytest.raises(RuntimeError, match="Runtime role"):
            assert_runtime_role(engine)
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text("REVOKE pg_read_all_data FROM codestra_whatsapp_app"))


def test_runtime_rejects_temporary_table_creation_authority(admin_engine, engine):
    assert_runtime_role(engine)
    try:
        with admin_engine.begin() as connection:
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("GRANT TEMPORARY ON DATABASE {} TO codestra_whatsapp_app").format(
                        sql.Identifier(admin_engine.url.database)
                    )
                )
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT has_database_privilege(current_user,current_database(),'CREATE'),"
                    "has_database_privilege(current_user,current_database(),'TEMPORARY')"
                )
            ).one() == (False, True)
        with pytest.raises(RuntimeError, match="Runtime role"):
            assert_runtime_role(engine)
    finally:
        with admin_engine.begin() as connection:
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM codestra_whatsapp_app").format(
                        sql.Identifier(admin_engine.url.database)
                    )
                )


@pytest.mark.parametrize("grant", ["schema_ownership", "schema_create", "function_ownership"])
def test_runtime_rejects_schema_authority(admin_engine, engine, grant):
    try:
        with admin_engine.begin() as connection:
            if grant == "schema_ownership":
                connection.execute(
                    text("CREATE SCHEMA runtime_owned_probe AUTHORIZATION codestra_whatsapp_app")
                )
            elif grant == "schema_create":
                connection.execute(text("GRANT CREATE ON SCHEMA public TO codestra_whatsapp_app"))
            else:
                connection.execute(
                    text(
                        "CREATE FUNCTION public.runtime_owned_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
                    )
                )
                connection.execute(
                    text("ALTER FUNCTION public.runtime_owned_probe() OWNER TO codestra_whatsapp_app")
                )
        with pytest.raises(RuntimeError, match="Runtime role"):
            assert_runtime_role(engine)
    finally:
        with admin_engine.begin() as connection:
            if grant == "schema_ownership":
                connection.execute(text("DROP SCHEMA runtime_owned_probe"))
            elif grant == "schema_create":
                connection.execute(text("REVOKE CREATE ON SCHEMA public FROM codestra_whatsapp_app"))
            else:
                connection.execute(text("DROP FUNCTION public.runtime_owned_probe()"))


@pytest.mark.parametrize("database", ["postgres", "template1"])
def test_migrator_cannot_connect_to_unrelated_databases(migrator_url, database):
    connection = make_engine(migrator_url.set(database=database))
    try:
        with pytest.raises(DBAPIError, match="rejects connection"), connection.connect():
            pytest.fail("Migrator connected to an unrelated database")
    finally:
        connection.dispose()
