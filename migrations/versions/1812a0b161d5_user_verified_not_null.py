"""Make user.verified NOT NULL

Revision ID: 1812a0b161d5
Revises: c4f1a9d7e2b8
Create Date: 2026-09-30 15:37:06.000000

`verified` had only a Python-side default, so a NULL was representable, and the
guards that read `user.verified is False` -- the API entry gate among them --
let a NULL user through as if verified (D645). NULL rows are backfilled to
false, the value the model has always written, and the column gets a matching
server default.

The NOT NULL goes through the same CHECK ... NOT VALID / VALIDATE recipe as
c4f1a9d7e2b8, so the scan does not hold an ACCESS EXCLUSIVE lock on "user".
"""
from alembic import op

revision = '1812a0b161d5'
down_revision = 'c4f1a9d7e2b8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('UPDATE "user" SET verified = false WHERE verified IS NULL')
    op.execute('ALTER TABLE "user" ALTER COLUMN verified SET DEFAULT false')
    op.execute('ALTER TABLE "user" ADD CONSTRAINT user_verified_not_null '
               'CHECK (verified IS NOT NULL) NOT VALID')
    op.execute('ALTER TABLE "user" VALIDATE CONSTRAINT user_verified_not_null')
    op.execute('ALTER TABLE "user" ALTER COLUMN verified SET NOT NULL')
    op.execute('ALTER TABLE "user" DROP CONSTRAINT user_verified_not_null')


def downgrade():
    # The backfilled values are left in place: there is no record of which rows were NULL.
    op.execute('ALTER TABLE "user" ALTER COLUMN verified DROP NOT NULL')
    op.execute('ALTER TABLE "user" ALTER COLUMN verified DROP DEFAULT')
