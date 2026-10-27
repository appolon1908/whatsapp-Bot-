"""A DSN must not override its reviewed database path through driver options."""

import pytest

from whatsapp_bot.db import make_engine


@pytest.mark.parametrize("key", ["dbname", "database", "service", "servicefile", "dsn", "DBNAME"])
def test_connection_target_overrides_are_rejected_before_connecting(key):
    with pytest.raises(ValueError, match="override"):
        make_engine(
            f"postgresql+psycopg://placeholder@127.0.0.1:1/codestra_whatsapp_commerce?{key}=unrelated"
        )
