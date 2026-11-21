"""domain_config: one way of saying who may register, and one of who may read (spec
2026-11-20-multitenant-domain-config, MT6 and MT7)

A sister domain had a `registration` column ('anyone', 'closed', 'paid') beside the `registration_mode` it inherited
from the first domain, and a `login_required` column where the first domain had `private_instance`. Now each domain has
`registration_mode` (a sister's NULL is the first domain's) and `private_instance` (the same), and whether its members
pay is `paid_membership`, its own and never inherited. Each domain's behaviour is written out so that nothing changes:

- 'paid' is `paid_membership`;
- a closed sister is 'Closed', or 'ReferralCode' where a referral code opened it (the mode it had was 'ReferralCode'
  and its referral codes were on);
- a sister's `login_required` is its `private_instance`, a sister with no configuration row getting one for it, as the
  first domain's `private_instance` no longer stops at the first domain.

The downgrade writes `registration` and `login_required` back from them and gives each sister's `private_instance`
back to the first domain's.

Revision ID: f5b1d8e3a7c9
Revises: e4a9c7d2b6f1
Create Date: 2026-11-21 04:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f5b1d8e3a7c9'
down_revision = 'e4a9c7d2b6f1'
branch_labels = None
depends_on = None

SISTER = 'site_domain_id IS NOT NULL'
FIRST_MODE = "(SELECT registration_mode FROM domain_config WHERE site_domain_id IS NULL)"


def upgrade():
    connection = op.get_bind()
    op.add_column('domain_config', sa.Column('paid_membership', sa.Boolean(), nullable=False, server_default='false'))
    connection.execute(sa.text("UPDATE domain_config SET paid_membership = true WHERE registration = 'paid'"))
    connection.execute(sa.text(f"""
        UPDATE domain_config SET registration_mode =
            CASE WHEN COALESCE(NULLIF(registration_mode, ''), {FIRST_MODE}) = 'ReferralCode'
                      AND COALESCE(referral_codes, true) THEN 'ReferralCode' ELSE 'Closed' END
        WHERE {SISTER} AND registration = 'closed'"""))
    connection.execute(sa.text(f"""
        INSERT INTO domain_config (site_domain_id) SELECT id FROM site_domain
        WHERE NOT is_default AND id NOT IN (SELECT site_domain_id FROM domain_config WHERE {SISTER})"""))
    connection.execute(sa.text(f'UPDATE domain_config SET private_instance = COALESCE(login_required, false) WHERE {SISTER}'))
    op.drop_constraint('ck_domain_config_registration', 'domain_config', type_='check')
    op.drop_column('domain_config', 'registration')
    op.drop_column('domain_config', 'login_required')


def downgrade():
    connection = op.get_bind()
    op.add_column('domain_config', sa.Column('registration', sa.String(10)))
    op.add_column('domain_config', sa.Column('login_required', sa.Boolean()))
    connection.execute(sa.text(f"""
        UPDATE domain_config SET registration =
            CASE WHEN paid_membership THEN 'paid'
                 WHEN {SISTER} AND COALESCE(NULLIF(registration_mode, ''), {FIRST_MODE}) = 'Closed' THEN 'closed'
                 ELSE 'anyone' END"""))
    connection.execute(sa.text(f"""
        UPDATE domain_config SET login_required = COALESCE(private_instance, false), private_instance = NULL
        WHERE {SISTER}"""))
    op.create_check_constraint('ck_domain_config_registration', 'domain_config',
                               "registration IN ('anyone','closed','paid')")
    op.drop_column('domain_config', 'paid_membership')
