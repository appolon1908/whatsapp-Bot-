"""Strict deployment configuration, including secret-safe failure messages."""

import pytest
from pydantic import SecretStr, ValidationError


def settings(**kwargs):
    from whatsapp_bot.config import Settings

    return Settings(_env_file=None, **kwargs)


def test_defaults_fail_closed_and_do_not_read_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("COMMERCE_OUTBOUND_ENABLED=true\n")
    from whatsapp_bot.config import Settings

    cfg = Settings()
    assert cfg.outbound_enabled is False
    assert cfg.worker_tenant_ids == []
    assert isinstance(cfg.database_url, SecretStr)
    assert cfg.database_url.get_secret_value() == ""
    assert cfg.allow_insecure_local_http is False


def test_explicit_env_and_prefixed_environment(tmp_path, monkeypatch):
    from whatsapp_bot.config import Settings

    env = tmp_path / "explicit.env"
    env.write_text("COMMERCE_JWT_AUDIENCE=staff\n")
    monkeypatch.setenv("COMMERCE_OUTBOUND_ENABLED", "true")
    cfg = Settings(_env_file=env)
    assert cfg.jwt_audience == "staff"
    assert cfg.outbound_enabled is True


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///anything",
        "postgresql+psycopg://localhost/controller",
        "postgresql+psycopg://localhost/codestra_whatsapp_commerceevil",
        "postgresql+psycopg://localhost/",
    ],
)
def test_database_target_rejects_unrelated_or_non_postgres(url):
    with pytest.raises(ValidationError):
        settings(database_url=url)


def test_database_isolated_names_and_exact_expected_target():
    settings(
        database_url="postgresql+psycopg://localhost/codestra_whatsapp_commerce_test",
        expected_database_name="codestra_whatsapp_commerce_test",
    )
    with pytest.raises(ValidationError):
        settings(database_url="postgresql://localhost/codestra_whatsapp_commerce_test")
    with pytest.raises(ValidationError):
        settings(expected_database_name="controller")


@pytest.mark.parametrize("field", ["jwks_url", "policy_base_url"])
def test_http_requires_explicit_allowance_and_local_host(field):
    with pytest.raises(ValidationError):
        settings(**{field: "http://localhost:8000/api"})
    settings(allow_insecure_local_http=True, **{field: "http://localhost:8000/api"})
    with pytest.raises(ValidationError):
        settings(allow_insecure_local_http=True, **{field: "http://example.com/api"})


def test_production_rejects_http_optin_and_urls_with_credentials():
    with pytest.raises(ValidationError):
        settings(environment="production", allow_insecure_local_http=True)
    with pytest.raises(ValidationError):
        settings(jwks_url="https://user:secret@example.com/jwks")


def test_secrets_are_not_exposed_by_repr_or_validation():
    cfg = settings(ingress_secret="very-private-value", policy_bearer_token="private-token")
    assert "very-private-value" not in repr(cfg)
    assert "private-token" not in repr(cfg)
    with pytest.raises(ValidationError) as exc:
        settings(database_url="postgresql://name:private-password@localhost/controller")
    assert "private-password" not in str(exc.value)
