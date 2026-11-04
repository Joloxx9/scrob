"""merge native watchlist and saved filters heads

Revision ID: mrgnwsf1
Revises: natwl001, savedflt
Create Date: 2026-11-04 11:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'mrgnwsf1'
down_revision: Union[str, Sequence[str], None] = ('natwl001', 'savedflt')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
