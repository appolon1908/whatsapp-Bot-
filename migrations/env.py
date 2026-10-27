from alembic import context
from sqlalchemy import engine_from_config, pool


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=None)
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
    with engine.connect() as connection:
        run_migrations(connection)
