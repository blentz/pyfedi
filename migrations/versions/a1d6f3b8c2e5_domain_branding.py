"""a domain can store its own branding (per-domain branding, project 4)

Revision ID: a1d6f3b8c2e5
Revises: f7c2a4e9b1d3
Create Date: 2026-10-25 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1d6f3b8c2e5'
down_revision = 'f7c2a4e9b1d3'
branch_labels = None
depends_on = None


# One row per domain that sets anything of its own. Every column is nullable: NULL or '' inherits the first domain's
# value from Site row 1. Deleting the domain deletes its row.
def upgrade():
    op.create_table(
        'domain_branding',
        sa.Column('site_domain_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=True),
        sa.Column('description', sa.String(length=256), nullable=True),
        sa.Column('logo', sa.String(length=40), nullable=True),
        sa.Column('logo_180', sa.String(length=40), nullable=True),
        sa.Column('logo_152', sa.String(length=40), nullable=True),
        sa.Column('logo_32', sa.String(length=40), nullable=True),
        sa.Column('logo_16', sa.String(length=40), nullable=True),
        sa.Column('default_theme', sa.String(length=20), nullable=True),
        sa.Column('additional_css', sa.Text(), nullable=True),
        sa.Column('sidebar', sa.Text(), nullable=True),
        sa.Column('sidebar_html', sa.Text(), nullable=True),
        sa.Column('about', sa.Text(), nullable=True),
        sa.Column('about_html', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['site_domain_id'], ['site_domain.id'], name='fk_domain_branding_site_domain_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('site_domain_id'),
    )


def downgrade():
    op.drop_table('domain_branding')
