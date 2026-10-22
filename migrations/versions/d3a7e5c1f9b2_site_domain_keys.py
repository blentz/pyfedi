"""each federating domain has its own instance actor key (plan C3)

Revision ID: d3a7e5c1f9b2
Revises: c8f4d2a6b9e1
Create Date: 2026-10-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# revision identifiers, used by Alembic.
revision = 'd3a7e5c1f9b2'
down_revision = 'c8f4d2a6b9e1'
branch_labels = None
depends_on = None

# The default domain's instance actor keeps Site row 1's key, so its row stays NULL. Every other row gets a fresh
# pair, as SiteDomain's before_insert hook gives a new one. A row that has a key keeps it, so running this again
# changes nothing. The keys are made here, not by app.rsa_keys: a migration does not import the app, whose code may
# have changed by the time it runs.
NEEDS_KEYS = 'SELECT id FROM site_domain WHERE NOT is_default AND private_key IS NULL ORDER BY id'
SET_KEYS = 'UPDATE site_domain SET private_key = :private_key, public_key = :public_key WHERE id = :id'


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
                                    encryption_algorithm=serialization.NoEncryption()).decode('ascii')
    public_key = key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                               format=serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')
    return private_key, public_key


def backfill_keys(connection) -> None:
    for domain_id in connection.execute(sa.text(NEEDS_KEYS)).scalars().all():
        private_key, public_key = _keypair()
        connection.execute(sa.text(SET_KEYS), {'id': domain_id, 'private_key': private_key, 'public_key': public_key})


def upgrade():
    with op.batch_alter_table('site_domain', schema=None) as batch_op:
        batch_op.add_column(sa.Column('public_key', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('private_key', sa.Text(), nullable=True))
    backfill_keys(op.get_bind())


def downgrade():
    with op.batch_alter_table('site_domain', schema=None) as batch_op:
        batch_op.drop_column('private_key')
        batch_op.drop_column('public_key')
