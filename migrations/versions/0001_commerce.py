"""Initial isolated commerce schema and tenant enforcement.

The SQL snapshot is deliberately immutable and independent of current ORM metadata.
"""

from pathlib import Path

from alembic import op

revision = "0001_commerce"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_name("0001_schema.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported; restore an operator-reviewed backup")
