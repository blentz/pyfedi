"""domain_config: the first domain's row holds a value for each setting (spec 2026-11-20-multitenant-domain-config, MT3)

A setting the first domain never set was NULL on its row, and each reader took its own default for it. The row takes
that default instead, so a NULL on the first domain's row no longer means anything, and the admin pages show what is in
effect. The downgrade leaves the values: each is what its reader took for a NULL.

Revision ID: e4a9c7d2b6f1
Revises: c3f7a1e9d5b8
Create Date: 2026-11-20 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e4a9c7d2b6f1'
down_revision = 'c3f7a1e9d5b8'
branch_labels = None
depends_on = None


# What each reader took for a missing settings key (app.models.SETTING_DEFAULTS when this was written)
DEFAULTS = {
    'logo_192': '', 'logo_512': '', 'announcement': '', 'announcement_html': '', 'registration_approved_email': '',
    'email_verification': True, 'captcha_enabled': True, 'choose_topics': True, 'filter_selection': True,
    'auto_decline_countries': '', 'nsfw_country_restriction': '', 'referral_generators': 'admins',
    'referral_max_per_code': 0, 'referral_max_per_user': 0, 'meme_comms_low_quality': False, 'public_modlog': False,
    'enable_report_em_dash_replies': True, 'limit_one_em_report_per_user': False, 'ban_posts': False,
    'allow_default_user_add_remote_community': True, 'allow_video_file_uploads': 'no', 'allow_audio_file_uploads': 'no',
    'max_video_upload_bytes': 524288000, 'max_audio_upload_bytes': 209715200, 'max_album_images': 4,
    'redirect_policy': 'same_origin', 'enable_instance_chooser': False, 'elevator_pitch': '', 'number_of_admins': 0,
    'financial_stability': False, 'daily_backups': False}


def upgrade():
    assignments = ', '.join(f'{name} = COALESCE({name}, :{name})' for name in DEFAULTS)
    op.get_bind().execute(sa.text(f'UPDATE domain_config SET {assignments} WHERE site_domain_id IS NULL'), DEFAULTS)


def downgrade():
    pass   # each value is what its reader took for the NULL it replaced
