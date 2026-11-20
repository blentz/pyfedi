"""domain_config: each domain's configuration, the first domain's included (spec 2026-11-20-multitenant-domain-config)

The first domain's values move from `site` into the row with no `site_domain_id`, with the default `site_domain` row's
storefront columns. Each sister domain's `domain_branding` row and storefront columns become its row. `domain_branding`
and the moved columns of `site` and `site_domain` are dropped.

Revision ID: b9e4d2a7c1f6
Revises: a7d3c9e1f5b2
Create Date: 2026-11-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b9e4d2a7c1f6'
down_revision = 'a7d3c9e1f5b2'
branch_labels = None
depends_on = None


def _string(length):
    return lambda: sa.String(length=length)


# Each column `site` had that is now the first domain's, with its type
SITE_COLUMNS = {
    'name': _string(256), 'description': _string(256), 'sidebar': sa.Text, 'sidebar_html': sa.Text,
    'legal_information': sa.Text, 'legal_information_html': sa.Text, 'tos_url': _string(256),
    'source_url': _string(256), 'bug_report_url': _string(256), 'get_involved_url': _string(256),
    'enable_downvotes': sa.Boolean, 'enable_gif_reply_rep_decrease': sa.Boolean, 'enable_chan_image_filter': sa.Boolean,
    'enable_this_comment_filter': sa.Boolean, 'allow_local_image_posts': sa.Boolean, 'enable_nsfw': sa.Boolean,
    'enable_nsfl': sa.Boolean, 'community_creation_admin_only': sa.Boolean, 'reports_email_admins': sa.Boolean,
    'registration_mode': _string(20), 'application_question': sa.Text, 'auto_decline_referrers': sa.Text,
    'default_theme': _string(20), 'default_filter': _string(20), 'contact_email': _string(255), 'about': sa.Text,
    'about_html': sa.Text, 'logo': _string(40), 'logo_180': _string(40), 'logo_152': _string(40),
    'logo_32': _string(40), 'logo_16': _string(40), 'logo_dark': _string(40), 'logo_dark_152': _string(40),
    'home_sidebar': sa.Text, 'community_sidebar': sa.Text, 'show_inoculation_block': sa.Boolean,
    'additional_css': sa.Text, 'additional_js': sa.Text, 'private_instance': sa.Boolean, 'language_id': sa.Integer,
}
# Each column `domain_branding` had besides its key
BRANDING_COLUMNS = {
    'name': _string(256), 'description': _string(256), 'logo': _string(40), 'logo_180': _string(40),
    'logo_152': _string(40), 'logo_32': _string(40), 'logo_16': _string(40), 'logo_192': _string(40),
    'logo_512': _string(40), 'logo_dark': _string(40), 'logo_dark_152': _string(40), 'default_theme': _string(20),
    'additional_css': sa.Text, 'sidebar': sa.Text, 'sidebar_html': sa.Text, 'about': sa.Text, 'about_html': sa.Text,
    'home_sidebar': sa.Text, 'community_sidebar': sa.Text,
}
# Each storefront column `site_domain` had, with its server default ('' for none)
STOREFRONT_COLUMNS = {
    'registration': (_string(10), "'anyone'"), 'registration_offer_id': (sa.Integer, ''),
    'login_required': (sa.Boolean, 'false'), 'referral_codes': (sa.Boolean, 'true'),
    'lapse_grace_days': (sa.Integer, '7'), 'paid_since': (sa.DateTime, ''),
}
CONFIG_COLUMNS = {**SITE_COLUMNS, **BRANDING_COLUMNS, **{name: kind for name, (kind, _) in STOREFRONT_COLUMNS.items()}}


def _insert(connection, values: dict):
    names = ', '.join(values)
    connection.execute(sa.text(f'INSERT INTO domain_config ({names}) VALUES ({", ".join(":" + n for n in values)})'),
                       values)


def upgrade():
    op.create_table(
        'domain_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('site_domain_id', sa.Integer(),
                  sa.ForeignKey('site_domain.id', name='fk_domain_config_site_domain_id', ondelete='CASCADE'),
                  unique=True),
        *(sa.Column(name, kind()) for name, kind in CONFIG_COLUMNS.items() if name != 'registration_offer_id'),
        sa.Column('registration_offer_id', sa.Integer(),
                  sa.ForeignKey('offer.id', name='fk_domain_config_registration_offer')),
        sa.CheckConstraint("registration IN ('anyone','closed','paid')", name='ck_domain_config_registration'),
    )
    # One row with no domain: the first domain's
    op.create_index('ix_domain_config_first', 'domain_config', [sa.text('(site_domain_id IS NULL)')], unique=True,
                    postgresql_where=sa.text('site_domain_id IS NULL'))
    connection = op.get_bind()
    storefront = ', '.join(STOREFRONT_COLUMNS)
    site = connection.execute(sa.text(f'SELECT {", ".join(SITE_COLUMNS)} FROM site WHERE id = 1')).mappings().first()
    default = connection.execute(sa.text(f'SELECT {storefront} FROM site_domain WHERE is_default')).mappings().first()
    if site is not None or default is not None:
        # The first domain: every value it had, NULLs included (a NULL of the first domain's is no value, not "inherit")
        _insert(connection, {**(dict(site) if site is not None else {}),
                             **(dict(default) if default is not None else {})})
    branding = ', '.join(f'b.{name}' for name in BRANDING_COLUMNS)
    for row in connection.execute(sa.text(
            f'SELECT d.id AS site_domain_id, {", ".join("d." + n for n in STOREFRONT_COLUMNS)}, {branding} '
            'FROM site_domain d LEFT JOIN domain_branding b ON b.site_domain_id = d.id '
            'WHERE NOT d.is_default ORDER BY d.id')).mappings():
        _insert(connection, dict(row))
    # A default domain's domain_branding row was never read (its branding was Site's): it goes with the table
    op.drop_table('domain_branding')
    with op.batch_alter_table('site') as batch_op:
        for name in SITE_COLUMNS:
            batch_op.drop_column(name)
    op.drop_constraint('ck_site_domain_registration', 'site_domain', type_='check')
    op.drop_constraint('fk_site_domain_registration_offer', 'site_domain', type_='foreignkey')
    with op.batch_alter_table('site_domain') as batch_op:
        for name in STOREFRONT_COLUMNS:
            batch_op.drop_column(name)


def downgrade():
    with op.batch_alter_table('site_domain') as batch_op:
        for name, (kind, default) in STOREFRONT_COLUMNS.items():
            if default:
                batch_op.add_column(sa.Column(name, kind(), nullable=False, server_default=sa.text(default)))
            elif name == 'registration_offer_id':
                batch_op.add_column(sa.Column(name, kind(), sa.ForeignKey('offer.id',
                                                                         name='fk_site_domain_registration_offer')))
            else:
                batch_op.add_column(sa.Column(name, kind()))
    op.create_check_constraint('ck_site_domain_registration', 'site_domain', "registration IN ('anyone','closed','paid')")
    with op.batch_alter_table('site') as batch_op:
        for name, kind in SITE_COLUMNS.items():
            batch_op.add_column(sa.Column(name, kind()))
    op.create_table(
        'domain_branding',
        sa.Column('site_domain_id', sa.Integer(), nullable=False),
        *(sa.Column(name, kind()) for name, kind in BRANDING_COLUMNS.items()),
        sa.ForeignKeyConstraint(['site_domain_id'], ['site_domain.id'], name='fk_domain_branding_site_domain_id',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('site_domain_id'),
    )
    connection = op.get_bind()
    # A NULL storefront column is its old server default
    restored = ', '.join(f'{name} = COALESCE(c.{name}, {default})' if default else f'{name} = c.{name}'
                         for name, (_, default) in STOREFRONT_COLUMNS.items())
    connection.execute(sa.text(f'UPDATE site_domain d SET {restored} FROM domain_config c '
                               'WHERE c.site_domain_id = d.id'))
    connection.execute(sa.text(f'UPDATE site_domain d SET {restored} FROM domain_config c '
                               'WHERE c.site_domain_id IS NULL AND d.is_default'))
    site = ', '.join(f'{name} = c.{name}' for name in SITE_COLUMNS)
    connection.execute(sa.text(f'UPDATE site s SET {site} FROM domain_config c WHERE c.site_domain_id IS NULL AND s.id = 1'))
    names = ', '.join(BRANDING_COLUMNS)
    connection.execute(sa.text(
        f'INSERT INTO domain_branding (site_domain_id, {names}) '
        f'SELECT site_domain_id, {names} FROM domain_config WHERE site_domain_id IS NOT NULL '
        f'AND ({" OR ".join(name + " IS NOT NULL" for name in BRANDING_COLUMNS)})'))
    op.drop_table('domain_config')
