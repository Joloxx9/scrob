"""fork revision kept for databases that recorded it

This fork's images recorded this revision id. It stays so that a database
stamped with it still resolves. It never changed the schema. No-op in both
directions.

Revision ID: mrgnwsf1
Revises: natwl001
Create Date: 2026-11-08
"""

revision = "mrgnwsf1"
down_revision = "natwl001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
