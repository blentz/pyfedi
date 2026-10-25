"""a domain's logo upload keeps its 192 and 512 pixel icons (per-domain branding, project 4)

Revision ID: c4e8a2f6d1b9
Revises: a1d6f3b8c2e5
Create Date: 2026-10-25 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4e8a2f6d1b9'
down_revision = 'a1d6f3b8c2e5'
branch_labels = None
depends_on = None


# The PWA manifest's icons. Site keeps its own in the logo_192 and logo_512 settings; a domain keeps them on its row.
# NULL or '' means the domain has none of its own.
def upgrade():
    with op.batch_alter_table('domain_branding') as batch_op:
        batch_op.add_column(sa.Column('logo_192', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('logo_512', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('domain_branding') as batch_op:
        batch_op.drop_column('logo_512')
        batch_op.drop_column('logo_192')
