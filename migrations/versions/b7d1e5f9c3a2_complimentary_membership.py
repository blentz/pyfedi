"""Complimentary membership: a hidden tier, held for good by every local account set up without a subscription

Revision ID: b7d1e5f9c3a2
Revises: a5c9e3d7b1f4
Create Date: 2026-11-15 20:00:00.000000

Plan 2026-11-15-complimentary-membership.md, K1, K2. Data only: the offer and one subscription per account.
"""
from alembic import op
from sqlalchemy import text

revision = 'b7d1e5f9c3a2'
down_revision = 'a5c9e3d7b1f4'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    offer_id = bind.execute(text("SELECT id FROM offer WHERE provider_kind = 'complimentary' ORDER BY id LIMIT 1")).scalar()
    if offer_id is None:
        offer_id = bind.execute(text(
            "INSERT INTO offer (provider_kind, kind, title, offer_group, amount_minor, grants_donor, position, active, "
            "created_at) VALUES ('complimentary', 'subscription', 'Complimentary', 'complimentary', 0, false, 0, false, "
            "now() AT TIME ZONE 'utc') RETURNING id")).scalar()
    # Every local account that is set up (verified, not waiting to pay, not deleted) and holds no subscription now
    bind.execute(text(
        'INSERT INTO member_subscription (user_id, offer_id, provider_kind, provider_reference, status, created_at) '
        "SELECT u.id, :offer_id, 'complimentary', 'complimentary-' || u.id || '-' || md5(random()::text), 'active', "
        "now() AT TIME ZONE 'utc' FROM \"user\" u "
        'WHERE u.ap_id IS NULL AND u.deleted IS NOT TRUE AND u.verified IS TRUE AND u.registration_pending_since IS NULL '
        "AND NOT EXISTS (SELECT 1 FROM member_subscription s WHERE s.user_id = u.id AND s.status = 'active')"),
        {'offer_id': offer_id})
    bind.execute(text(
        'UPDATE "user" SET access_lapsed_at = NULL WHERE access_lapsed_at IS NOT NULL AND id IN '
        "(SELECT user_id FROM member_subscription WHERE provider_kind = 'complimentary' AND status = 'active')"))


def downgrade():
    bind = op.get_bind()
    bind.execute(text("DELETE FROM member_subscription WHERE provider_kind = 'complimentary'"))
    bind.execute(text("DELETE FROM offer WHERE provider_kind = 'complimentary'"))
