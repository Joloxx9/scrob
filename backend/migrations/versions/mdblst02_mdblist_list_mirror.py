"""mirror MDBList lists instead of syncing them both ways

MDBList becomes the only source of a synced list's items, so the push toggle
goes, and lists remember whether the MDBList account owns them, so a copy
can be deleted when its list is deleted on MDBList.

Revision ID: mdblst02
Revises: mdblst01
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa


revision = "mdblst02"
down_revision = "mdblst01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lists", sa.Column("mdblist_owned", sa.Boolean(), nullable=True))
    # Lists the two-way sync linked were all owned (it only synced the
    # account's own lists).
    op.execute("UPDATE lists SET mdblist_owned = true WHERE mdblist_static IS NOT NULL")
    op.drop_column("user_settings", "mdblist_push_lists")


def downgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("mdblist_push_lists", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.drop_column("lists", "mdblist_owned")
