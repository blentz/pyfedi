"""paid registration and private domains: per-domain login, a domain's first fee, pending and lapsed accounts

Revision ID: d4a8b2f6c1e9
Revises: c3f7a9e1d5b8
Create Date: 2026-11-06 14:00:00.000000

Plan docs/superpowers/plans/2026-11-06-monetization-phase-3.md. Each other domain takes the site's "require login" setting
as its own, so nothing opens up on deploy (S4). The downgrade closes a paid domain, and removes the accounts that never
paid: without their column they'd be ordinary accounts anybody could log in to.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4a8b2f6c1e9'
down_revision = 'c3f7a9e1d5b8'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_site_domain_registration', 'site_domain', type_='check')
    op.create_check_constraint('ck_site_domain_registration', 'site_domain', "registration IN ('anyone','closed','paid')")
    op.add_column('site_domain', sa.Column('registration_offer_id', sa.Integer(),
                                           sa.ForeignKey('offer.id', name='fk_site_domain_registration_offer')))
    op.add_column('site_domain', sa.Column('login_required', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('site_domain', sa.Column('lapse_grace_days', sa.Integer(), nullable=False, server_default='7'))
    op.add_column('user', sa.Column('registration_pending_since', sa.DateTime()))
    op.add_column('user', sa.Column('access_lapsed_at', sa.DateTime()))
    op.add_column('member_subscription', sa.Column('ends_at', sa.DateTime()))
    op.execute("""UPDATE site_domain SET login_required = COALESCE((SELECT private_instance FROM site WHERE id = 1), true)
                  WHERE NOT is_default""")


def downgrade():
    op.execute('DELETE FROM "user" WHERE registration_pending_since IS NOT NULL')
    op.execute("UPDATE site_domain SET registration = 'closed' WHERE registration = 'paid'")
    op.drop_column('member_subscription', 'ends_at')
    op.drop_column('user', 'access_lapsed_at')
    op.drop_column('user', 'registration_pending_since')
    op.drop_column('site_domain', 'lapse_grace_days')
    op.drop_column('site_domain', 'login_required')
    op.drop_column('site_domain', 'registration_offer_id')
    op.drop_constraint('ck_site_domain_registration', 'site_domain', type_='check')
    op.create_check_constraint('ck_site_domain_registration', 'site_domain', "registration IN ('anyone','closed')")
