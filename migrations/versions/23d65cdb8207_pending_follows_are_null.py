"""Store a still-pending follow of a remote user as NULL, not False

Revision ID: 23d65cdb8207
Revises: 3f8b2c6d9e41
Create Date: 2026-10-01 18:00:00.000000

`user_follower.is_accepted` means None = requested, True = accepted, False =
refused, but follow_user stored a follow of a remote user as False until the
peer's Accept arrived (R265). A Reject deletes the follow request, so a False
row whose request is still there was never refused: it is pending, and becomes
NULL. False rows with no request left are the real refusals and stay.
"""
from alembic import op

revision = '23d65cdb8207'
down_revision = '3f8b2c6d9e41'
branch_labels = None
depends_on = None

PENDING_FOLLOWS_SQL = (
    'UPDATE user_follower SET is_accepted = NULL '
    'WHERE is_inward IS FALSE AND is_accepted IS FALSE AND EXISTS ('
    'SELECT 1 FROM user_follow_request '
    'WHERE user_follow_request.user_id = user_follower.local_user_id '
    'AND user_follow_request.follow_id = user_follower.remote_user_id)'
)


def upgrade():
    op.execute(PENDING_FOLLOWS_SQL)


def downgrade():
    op.execute(
        'UPDATE user_follower SET is_accepted = FALSE '
        'WHERE is_inward IS FALSE AND is_accepted IS NULL AND EXISTS ('
        'SELECT 1 FROM user_follow_request '
        'WHERE user_follow_request.user_id = user_follower.local_user_id '
        'AND user_follow_request.follow_id = user_follower.remote_user_id)'
    )
