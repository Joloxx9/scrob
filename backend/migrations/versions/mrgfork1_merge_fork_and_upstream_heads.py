"""join the fork's legacy revisions with the upstream chain

Fork images ran natwl001 straight after savedflt, then mrgnwsf1, mdblst01 and
mdblst02; upstream runs eodefault, scrobpct and stpull01 before natwl001. In
this fork natwl001 follows savedflt and 9d4e1a7c2b58 follows both stpull01 and
natwl001, so a database from a fork image or an upstream image upgrades
without re-running or skipping anything. This revision joins the two heads.

Revision ID: mrgfork1
Revises: mdblmir1, mdblst02
Create Date: 2026-11-08
"""

revision = "mrgfork1"
down_revision = ("mdblmir1", "mdblst02")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
