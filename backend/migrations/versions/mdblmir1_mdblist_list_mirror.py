"""mirror MDBList lists into read-only Scrob copies

Adds the per-list fields the mirror needs (last mirrored keys, whether the
list is static, whether the MDBList account owns it) and the
mdblist_sync_lists toggle. The toggle defaults off, so nothing changes until
a user turns it on.

Fork note: this fork's earlier revisions mdblst01 and mdblst02 already added
these columns on databases from fork images, so each column is added only
when it is missing.

Revision ID: mdblmir1
Revises: uilang
Create Date: 2026-11-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "mdblmir1"
down_revision = "uilang"
branch_labels = None
depends_on = None


def _add_missing(table: str, column: sa.Column) -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    _add_missing("lists", sa.Column("mdblist_synced_keys", JSONB(), nullable=True))
    _add_missing("lists", sa.Column("mdblist_static", sa.Boolean(), nullable=True))
    _add_missing("lists", sa.Column("mdblist_owned", sa.Boolean(), nullable=True))
    _add_missing("user_settings", sa.Column("mdblist_sync_lists", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("user_settings", "mdblist_sync_lists")
    op.drop_column("lists", "mdblist_owned")
    op.drop_column("lists", "mdblist_static")
    op.drop_column("lists", "mdblist_synced_keys")
