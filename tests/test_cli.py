"""Operator commands fail closed and keep secrets out of terminal errors."""

import json
import os

import pytest


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    for name in os.environ:
        if name.startswith("COMMERCE_") and name != "COMMERCE_TEST_DATABASE_NAME":
            monkeypatch.delenv(name)


def test_provision_requires_explicit_admin_credentials(capsys):
    from whatsapp_bot.cli import main

    assert main(["provision", "--database", "codestra_whatsapp_commerce"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "operator_configuration_missing"


def test_provision_requires_separate_migrator_credential_before_database_access(monkeypatch, capsys):
    from whatsapp_bot.cli import main

    monkeypatch.setenv("COMMERCE_ADMIN_DATABASE_URL", "postgresql://operator@127.0.0.1:1/postgres")
    monkeypatch.setenv("COMMERCE_APP_PASSWORD", "test-only-app-password-at-least-24")
    assert main(["provision", "--database", "codestra_whatsapp_commerce"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "operator_configuration_missing"


def test_conflicting_operator_database_variables_are_refused(monkeypatch, capsys):
    from whatsapp_bot.cli import main

    monkeypatch.setenv(
        "COMMERCE_MIGRATOR_DATABASE_URL", "postgresql://migrator@127.0.0.1:1/codestra_whatsapp_commerce"
    )
    monkeypatch.setenv(
        "COMMERCE_OWNER_DATABASE_URL", "postgresql://operator@127.0.0.1:2/codestra_whatsapp_commerce"
    )
    assert main(["migrate"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_configuration"


def test_configuration_errors_do_not_print_secret_dsn(monkeypatch, capsys):
    from whatsapp_bot.cli import main

    monkeypatch.setenv("COMMERCE_DATABASE_URL", "postgresql://app:never-print-this@localhost/controller")
    assert main(["worker", "--once"]) == 2
    output = capsys.readouterr().err
    assert "never-print-this" not in output
    assert "controller" not in output
    assert json.loads(output)["error"] == "invalid_configuration"


def test_worker_requires_explicit_tenant_list(database_urls, monkeypatch, capsys):
    from whatsapp_bot.cli import main

    monkeypatch.setenv("COMMERCE_DATABASE_URL", database_urls[1].render_as_string(hide_password=False))
    monkeypatch.setenv("COMMERCE_EXPECTED_DATABASE_NAME", database_urls[1].database)
    assert main(["worker", "--once"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "worker_configuration_missing"


def test_worker_once_runs_as_restricted_role_with_sends_disabled(
    database_urls, tenant_a, monkeypatch, capsys
):
    from whatsapp_bot.cli import main

    monkeypatch.setenv("COMMERCE_DATABASE_URL", database_urls[1].render_as_string(hide_password=False))
    monkeypatch.setenv("COMMERCE_EXPECTED_DATABASE_NAME", database_urls[1].database)
    monkeypatch.setenv("COMMERCE_WORKER_TENANT_IDS", json.dumps([str(tenant_a)]))
    assert main(["worker", "--once"]) == 0
    assert json.loads(capsys.readouterr().out) == {"inbox": 0, "outbox": 0, "expired": 0}


def test_runtime_role_cannot_execute_migrations(database_urls, monkeypatch, capsys):
    from whatsapp_bot.cli import main

    monkeypatch.setenv(
        "COMMERCE_MIGRATOR_DATABASE_URL", database_urls[1].render_as_string(hide_password=False)
    )
    assert main(["migrate"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "operator_action_refused"


@pytest.mark.parametrize("variable", ["COMMERCE_MIGRATOR_DATABASE_URL", "COMMERCE_OWNER_DATABASE_URL"])
def test_named_migrator_cli_applies_migrations(migrator_url, monkeypatch, capsys, variable):
    from whatsapp_bot.cli import main

    monkeypatch.setenv(variable, migrator_url.render_as_string(hide_password=False))
    assert main(["migrate"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "migrated"}


def test_enabling_delivery_without_policy_credentials_is_refused(
    database_urls, tenant_a, monkeypatch, capsys
):
    from whatsapp_bot.cli import main

    monkeypatch.setenv("COMMERCE_DATABASE_URL", database_urls[1].render_as_string(hide_password=False))
    monkeypatch.setenv("COMMERCE_EXPECTED_DATABASE_NAME", database_urls[1].database)
    monkeypatch.setenv("COMMERCE_WORKER_TENANT_IDS", json.dumps([str(tenant_a)]))
    monkeypatch.setenv("COMMERCE_OUTBOUND_ENABLED", "true")
    assert main(["worker", "--once"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "policy_configuration_missing"
