"""merge the v1.8.10 migrations with the blentz branch's

Revision ID: b8d1e6f4a2c7
Revises: a7c2e5f9d3b1, 7c5a293ed40c
Create Date: 2026-10-31 22:00:00.000000

Rebasing blentz onto v1.8.10 left two heads: upstream's chain from v1.7.17 (instance liability and stats,
reposters, community hashtag and posting-warning override, the nodeinfo index) ending at 7c5a293ed40c, and this
branch's ending at a7c2e5f9d3b1. The two touch no column or index in common. A database already at a7c2e5f9d3b1
runs upstream's seven migrations and then this one; a new database runs both chains.
"""

revision = 'b8d1e6f4a2c7'
down_revision = ('a7c2e5f9d3b1', '7c5a293ed40c')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
