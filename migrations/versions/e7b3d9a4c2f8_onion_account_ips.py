"""every onion-registered account stores the sentinel IP and no country (onion unlinkability, project 5)

Revision ID: e7b3d9a4c2f8
Revises: c4e8a2f6d1b9
Create Date: 2026-10-26 09:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'e7b3d9a4c2f8'
down_revision = 'c4e8a2f6d1b9'
branch_labels = None
depends_on = None

ONION_ACCOUNT_IP = '169.254.88.88'


def upgrade():
    op.execute(f"""
        UPDATE "user" SET ip_address = '{ONION_ACCOUNT_IP}', ip_address_country = NULL
        WHERE site_domain_id IN (SELECT id FROM site_domain WHERE is_onion)
    """)


def downgrade():
    # The addresses this replaced are gone, which is the point; there is nothing to restore.
    pass
