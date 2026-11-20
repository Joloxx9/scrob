"""collection sync from Radarr and Sonarr

Adds the radarr/sonarr collection sources and a per-user pull interval for
each (hours, null = disabled).

Revision ID: arrcol01
Revises: mrgfork1
Create Date: 2026-11-20
"""

from alembic import op
import sqlalchemy as sa


revision = "arrcol01"
down_revision = "mrgfork1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new enum value can't be used in the transaction that adds it; nothing
    # here uses it, and autocommit keeps older PostgreSQL versions happy too.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'radarr'")
        op.execute("ALTER TYPE collectionsource ADD VALUE IF NOT EXISTS 'sonarr'")
    op.add_column("user_settings", sa.Column("radarr_collection_sync_interval", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("sonarr_collection_sync_interval", sa.Float(), nullable=True))


def downgrade() -> None:
    # PostgreSQL can't drop enum values; the two sources stay defined.
    op.drop_column("user_settings", "sonarr_collection_sync_interval")
    op.drop_column("user_settings", "radarr_collection_sync_interval")
