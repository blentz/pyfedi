"""domain_config: the settings that are each domain's (spec 2026-11-20-multitenant-domain-config, phase 1b)

Each key of the settings table that is now a domain's becomes a column of domain_config, the first domain's row taking
the stored value. A value of the wrong type for its column (a hand edit, a bug long fixed) is dropped, as a missing key
was: every reader has a default. The downgrade writes the first domain's values back as keys; a sister domain's own
values have no key to go back to and are lost.

Revision ID: c3f7a1e9d5b8
Revises: b9e4d2a7c1f6
Create Date: 2026-11-20 15:00:00.000000

"""
import json

from alembic import op
import sqlalchemy as sa


revision = 'c3f7a1e9d5b8'
down_revision = 'b9e4d2a7c1f6'
branch_labels = None
depends_on = None


# Each key, with its column type and the Python type its value must have. logo_192 and logo_512 are domain_config
# columns already (the sisters' PWA icons); the first domain's move into them.
TEXT, BOOL, INT = str, bool, int
SETTINGS = {
    'announcement': (sa.Text, TEXT), 'announcement_html': (sa.Text, TEXT),
    'registration_approved_email': (sa.Text, TEXT), 'email_verification': (sa.Boolean, BOOL),
    'captcha_enabled': (sa.Boolean, BOOL), 'choose_topics': (sa.Boolean, BOOL), 'filter_selection': (sa.Boolean, BOOL),
    'auto_decline_countries': (sa.Text, TEXT), 'nsfw_country_restriction': (sa.Text, TEXT),
    'referral_generators': (lambda: sa.String(20), TEXT), 'referral_max_per_code': (sa.Integer, INT),
    'referral_max_per_user': (sa.Integer, INT), 'meme_comms_low_quality': (sa.Boolean, BOOL),
    'public_modlog': (sa.Boolean, BOOL), 'enable_report_em_dash_replies': (sa.Boolean, BOOL),
    'limit_one_em_report_per_user': (sa.Boolean, BOOL), 'ban_posts': (sa.Boolean, BOOL),
    'allow_default_user_add_remote_community': (sa.Boolean, BOOL),
    'allow_video_file_uploads': (lambda: sa.String(20), TEXT), 'allow_audio_file_uploads': (lambda: sa.String(20), TEXT),
    'max_video_upload_bytes': (sa.BigInteger, INT), 'max_audio_upload_bytes': (sa.BigInteger, INT),
    'max_album_images': (sa.Integer, INT), 'redirect_policy': (lambda: sa.String(20), TEXT),
    'enable_instance_chooser': (sa.Boolean, BOOL), 'elevator_pitch': (sa.Text, TEXT),
    'number_of_admins': (sa.Integer, INT), 'financial_stability': (sa.Boolean, BOOL), 'daily_backups': (sa.Boolean, BOOL),
}
ICONS = ('logo_192', 'logo_512')
TEXT_LENGTHS = {'referral_generators': 20, 'allow_video_file_uploads': 20, 'allow_audio_file_uploads': 20,
                'redirect_policy': 20, 'logo_192': 40, 'logo_512': 40}


def fitting(name: str, value, kind):
    """`value` as column `name` holds it, or None for one that does not fit."""
    if kind is BOOL:
        return value if isinstance(value, bool) else None
    if kind is INT:
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if not isinstance(value, str) or len(value) > TEXT_LENGTHS.get(name, len(value)):
        return None
    return value


def upgrade():
    with op.batch_alter_table('domain_config') as batch_op:
        for name, (column, _kind) in SETTINGS.items():
            batch_op.add_column(sa.Column(name, column()))
    connection = op.get_bind()
    kinds = {name: kind for name, (_column, kind) in SETTINGS.items()} | dict.fromkeys(ICONS, TEXT)
    values = {}
    for name, stored in connection.execute(sa.text('SELECT name, value FROM settings WHERE name = ANY(:names)'),
                                           {'names': list(kinds)}):
        try:
            value = fitting(name, json.loads(stored), kinds[name])
        except (TypeError, ValueError):
            value = None
        if value is not None:
            values[name] = value
    first = connection.execute(sa.text('SELECT id FROM domain_config WHERE site_domain_id IS NULL')).scalar()
    if first is None:
        return   # no site row, so no first domain's row (b9e4d2a7c1f6): the keys stay where they are, unread
    if values:
        connection.execute(sa.text(f'UPDATE domain_config SET {", ".join(f"{n} = :{n}" for n in values)} '
                                   'WHERE id = :id'), {**values, 'id': first})
    connection.execute(sa.text('DELETE FROM settings WHERE name = ANY(:names)'), {'names': list(kinds)})


def downgrade():
    connection = op.get_bind()
    names = [*SETTINGS, *ICONS]
    first = connection.execute(sa.text(f'SELECT {", ".join(names)} FROM domain_config WHERE site_domain_id IS NULL')) \
        .mappings().first()
    if first is not None:
        for name in names:
            if first[name] is not None:
                connection.execute(sa.text('INSERT INTO settings (name, value) VALUES (:name, :value)'),
                                   {'name': name, 'value': json.dumps(first[name])})
        connection.execute(sa.text(f'UPDATE domain_config SET {", ".join(n + " = NULL" for n in ICONS)} '
                                   'WHERE site_domain_id IS NULL'))
    with op.batch_alter_table('domain_config') as batch_op:
        for name in SETTINGS:
            batch_op.drop_column(name)
