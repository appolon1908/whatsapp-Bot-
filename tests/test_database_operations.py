"""Operator-level checks against a disposable PostgreSQL cluster only.

CI must install pg_dump/pg_restore and configure its commerce app HBA restrictions.
TEST_POSTGRES_BIN can select the matching PostgreSQL client binary directory.
"""

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from whatsapp_bot.commerce import CommerceService
from whatsapp_bot.db import assert_runtime_role, make_engine
from whatsapp_bot.db import tenant_session as scoped_session
from whatsapp_bot.models import Business, Channel, InboundEvent, OutboxEvent
from whatsapp_bot.provision import provision_database


def test_app_cannot_connect_to_unrelated_databases_despite_public_connect(database_urls):
    # PUBLIC CONNECT is cluster-default permission, not a database-wide role boundary.
    # The HBA template must reject these connection attempts before SQL authorization.
    for database in ("postgres", "template1"):
        engine = make_engine(database_urls[1].set(database=database))
        try:
            with pytest.raises(DBAPIError, match="rejects connection"):
                with engine.connect():
                    pytest.fail("Runtime role connected to an unrelated database")
        finally:
            engine.dispose()
    admin = make_engine(database_urls[0])
    try:
        with admin.connect() as connection:
            assert connection.scalar(
                text("SELECT has_database_privilege('codestra_whatsapp_app','postgres','CONNECT')")
            )
    finally:
        admin.dispose()


def _postgres_tool(name):
    directory = os.environ.get("TEST_POSTGRES_BIN")
    binary = str(Path(directory) / name) if directory else shutil.which(name)
    if not binary or not Path(binary).is_file():
        pytest.fail(
            f"Restore integration requires {name}; install PostgreSQL client tools or set TEST_POSTGRES_BIN"
        )
    return binary


def _command(name, url, *args):
    env = os.environ.copy()
    env["PGPASSWORD"] = url.password or ""
    env["PGCONNECT_TIMEOUT"] = "5"
    result = subprocess.run(
        [
            _postgres_tool(name),
            "-h",
            url.host or "127.0.0.1",
            "-p",
            str(url.port or 5432),
            "-U",
            url.username,
            "-d",
            url.database,
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        # pg tools can include table names but never credentials supplied via PGPASSWORD.
        pytest.fail(f"{name} failed: {result.stderr[:2000]}")


def test_restore_preserves_pending_work_ownership_and_rls(
    database_urls, tenant_a, tenant_b, tenant_session, tmp_path
):
    with tenant_session(tenant_a) as session:
        channel = session.scalar(select(Channel))
        service = CommerceService(session, tenant_a, "worker")
        conversation = service.create_conversation(channel.id, "restore-customer")
        inbox = InboundEvent(
            tenant_id=tenant_a,
            channel_id=channel.id,
            event_id=f"restore-{uuid4()}",
            payload_hash="0" * 64,
            payload={"fixture": "pending-inbox"},
        )
        outbox = OutboxEvent(
            tenant_id=tenant_a,
            conversation_id=conversation["id"],
            conversation_version=1,
            event_key=f"restore-{uuid4()}",
            kind="message.reply",
            payload={"text": "Restore fixture"},
        )
        session.add_all([inbox, outbox])
        session.flush()
        inbox_id, outbox_id, command_id = inbox.id, outbox.id, outbox.command_id
    dump = tmp_path / "commerce.dump"
    _command("pg_dump", database_urls[0], "-Fc", "-f", str(dump))
    restore_name = f"codestra_whatsapp_commerce_test_restore_{uuid4().hex[:12]}"
    provision_database(
        database_urls[0],
        restore_name,
        os.environ.get("TEST_DATABASE_APP_PASSWORD", "local-isolated-test-app-password-only"),
    )
    restored_admin = database_urls[0].set(database=restore_name)
    restored_app = database_urls[1].set(database=restore_name)
    runtime = make_engine(restored_app)
    admin = make_engine(restored_admin)
    try:
        _command("pg_restore", restored_admin, "--exit-on-error", str(dump))
        assert_runtime_role(runtime)
        with runtime.connect() as connection:
            assert connection.execute(select(Business)).all() == []
        with scoped_session(runtime, tenant_a) as session:
            assert session.get(InboundEvent, inbox_id).state == "pending"
            event = session.get(OutboxEvent, outbox_id)
            assert event.state == "pending"
            assert event.command_id == command_id
            assert session.get(Business, tenant_b) is None
        with admin.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*)=16 AND bool_and(c.relrowsecurity AND c.relforcerowsecurity AND r.rolname='codestra_whatsapp_owner') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='commerce' AND c.relkind='r'"
                )
            )
    finally:
        runtime.dispose()
        admin.dispose()
        # This unique restore DB is created solely by this test; never drop shared fixtures.
        control = make_engine(database_urls[0])
        try:
            with control.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{restore_name}"')
        finally:
            control.dispose()
