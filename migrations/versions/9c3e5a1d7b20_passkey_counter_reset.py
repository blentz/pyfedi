"""Reset every passkey's signature counter to 0

Revision ID: 9c3e5a1d7b20
Revises: 05ae68b2ebb9
Create Date: 2026-09-30 18:00:00.000000

The login never checked `passkey.counter` and kept it with `counter += 1`, and
registration discarded the authenticator's own count (D886, D887), so every
stored value was invented rather than reported. The login now checks the
stored counter, and a synced passkey reports 0 forever: checked against an
invented 41 it would be refused on every login. 0 is the one value the check
accepts from both kinds of authenticator, and real counts are stored from the
next login on.
"""
from alembic import op

revision = '9c3e5a1d7b20'
down_revision = '05ae68b2ebb9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('UPDATE passkey SET counter = 0')


def downgrade():
    # The invented counts are not restored: they were never the authenticators'.
    pass
