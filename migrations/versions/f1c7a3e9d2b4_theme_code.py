"""theme code: a custom theme's template overrides and script

Revision ID: f1c7a3e9d2b4
Revises: e8b3d6a1f4c7
Create Date: 2026-11-18 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f1c7a3e9d2b4'
down_revision = 'e8b3d6a1f4c7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('custom_theme', sa.Column('script', sa.Text(), nullable=True))
    op.add_column('custom_theme', sa.Column('code_failed_template', sa.String(length=255), nullable=True))
    op.add_column('custom_theme', sa.Column('code_failure', sa.Text(), nullable=True))
    op.add_column('custom_theme', sa.Column('code_failed_at', sa.DateTime(), nullable=True))
    op.create_table(
        'custom_theme_template',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('custom_theme_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['custom_theme_id'], ['custom_theme.id'], name='fk_custom_theme_template_theme_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('custom_theme_id', 'name', name='uq_custom_theme_template_name'),
    )
    op.create_index('ix_custom_theme_template_custom_theme_id', 'custom_theme_template', ['custom_theme_id'])


def downgrade():
    op.drop_table('custom_theme_template')
    for column in ('code_failed_at', 'code_failure', 'code_failed_template', 'script'):
        op.drop_column('custom_theme', column)
