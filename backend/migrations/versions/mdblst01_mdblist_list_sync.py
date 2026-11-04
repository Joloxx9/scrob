"""two-way sync of the user's own MDBList lists

Adds the per-list baseline and static/dynamic flag, and the pull/push
toggles. Both toggles default off, so nothing syncs until a user turns it on.

Revision ID: mdblst01
Revises: mrgnwsf1
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "mdblst01"
down_revision = "mrgnwsf1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lists", sa.Column("mdblist_synced_keys", JSONB(), nullable=True))
    op.add_column("lists", sa.Column("mdblist_static", sa.Boolean(), nullable=True))
    op.add_column("user_settings", sa.Column("mdblist_sync_lists", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("mdblist_push_lists", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("user_settings", "mdblist_push_lists")
    op.drop_column("user_settings", "mdblist_sync_lists")
    op.drop_column("lists", "mdblist_static")
    op.drop_column("lists", "mdblist_synced_keys")
