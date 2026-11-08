"""fork revision: earlier MDBList list mirror

This fork's images recorded this revision id. It stays so that a database
stamped with it still resolves. Any schema change it made now lives in mdblmir1,
which skips columns that already exist. No-op in both directions.

Revision ID: mdblst02
Revises: mdblst01
Create Date: 2026-11-08
"""

revision = "mdblst02"
down_revision = "mdblst01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
