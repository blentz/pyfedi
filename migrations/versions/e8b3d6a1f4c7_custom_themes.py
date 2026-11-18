"""custom themes per domain: a domain's own copies of themes, and their files

Revision ID: e8b3d6a1f4c7
Revises: d7a2c5e9f3b1
Create Date: 2026-11-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e8b3d6a1f4c7'
down_revision = 'd7a2c5e9f3b1'
branch_labels = None
depends_on = None


# A theme belongs to one domain and goes with it. Theme columns name one as `custom:<id>`.
def upgrade():
    op.create_table(
        'custom_theme',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_domain_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('base_theme', sa.String(length=20), nullable=False),
        sa.Column('css', sa.Text(), server_default='', nullable=False),
        sa.Column('retired', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['site_domain_id'], ['site_domain.id'], name='fk_custom_theme_site_domain_id',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['updated_by'], ['user.id'], name='fk_custom_theme_updated_by', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_custom_theme_site_domain_id', 'custom_theme', ['site_domain_id'])
    op.create_index('ix_custom_theme_domain_name', 'custom_theme', ['site_domain_id', sa.text('lower(name)')],
                    unique=True)
    op.create_table(
        'custom_theme_file',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('custom_theme_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=100), nullable=False),
        sa.Column('path', sa.String(length=255), nullable=False),
        sa.Column('mime_type', sa.String(length=64), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['custom_theme_id'], ['custom_theme.id'], name='fk_custom_theme_file_theme_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('custom_theme_id', 'filename', name='uq_custom_theme_file_name'),
    )
    op.create_index('ix_custom_theme_file_custom_theme_id', 'custom_theme_file', ['custom_theme_id'])


def downgrade():
    op.drop_table('custom_theme_file')
    op.drop_table('custom_theme')
