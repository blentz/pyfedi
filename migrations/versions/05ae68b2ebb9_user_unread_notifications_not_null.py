"""Make user.unread_notifications NOT NULL

Revision ID: 05ae68b2ebb9
Revises: 1812a0b161d5
Create Date: 2026-09-30 15:45:00.000000

cae2e31293e8 added `unread_notifications` as nullable with no server default
and no backfill, so every user row older than it held NULL, and the ~50
`unread_notifications += 1` sites raise TypeError on such a row (D274). NULL
rows are backfilled to 0, the value the model has always written, and the
column gets a matching server default.

The NOT NULL goes through the same CHECK ... NOT VALID / VALIDATE recipe as
c4f1a9d7e2b8, so the scan does not hold an ACCESS EXCLUSIVE lock on "user".
"""
from alembic import op

revision = '05ae68b2ebb9'
down_revision = '1812a0b161d5'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('UPDATE "user" SET unread_notifications = 0 WHERE unread_notifications IS NULL')
    op.execute('ALTER TABLE "user" ALTER COLUMN unread_notifications SET DEFAULT 0')
    op.execute('ALTER TABLE "user" ADD CONSTRAINT user_unread_notifications_not_null '
               'CHECK (unread_notifications IS NOT NULL) NOT VALID')
    op.execute('ALTER TABLE "user" VALIDATE CONSTRAINT user_unread_notifications_not_null')
    op.execute('ALTER TABLE "user" ALTER COLUMN unread_notifications SET NOT NULL')
    op.execute('ALTER TABLE "user" DROP CONSTRAINT user_unread_notifications_not_null')


def downgrade():
    # The backfilled values are left in place: there is no record of which rows were NULL.
    op.execute('ALTER TABLE "user" ALTER COLUMN unread_notifications DROP NOT NULL')
    op.execute('ALTER TABLE "user" ALTER COLUMN unread_notifications DROP DEFAULT')
