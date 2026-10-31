from alembic import context
from sqlalchemy import engine_from_config, pool

from whatsapp_bot.db import activate_operator_role


def run_migrations(connection):
    activate_operator_role(connection)
    context.configure(connection=connection, target_metadata=None, version_table_schema="public")
    with context.begin_transaction():
        context.run_migrations()


provided = context.config.attributes.get("connection")
if provided is not None:
    run_migrations(provided)
else:
    engine = engine_from_config(
        context.config.get_section(context.config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with engine.begin() as connection:
        run_migrations(connection)
