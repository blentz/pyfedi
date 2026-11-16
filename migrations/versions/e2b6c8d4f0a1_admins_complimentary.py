"""Every admin holds complimentary membership; a referral code no longer gives nothing

Revision ID: e2b6c8d4f0a1
Revises: d9f3a7b1e5c6
Create Date: 2026-11-16 09:00:00.000000

Plan 2026-11-16-referral-gifts-admins-checkout.md, X1, X3. A referral code set to give nothing is cleared, so that it
takes the default as its own when next used or shown.
"""
from alembic import op
from sqlalchemy import text

revision = 'e2b6c8d4f0a1'
down_revision = 'd9f3a7b1e5c6'
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
    # User 1, and every local holder of the built-in Admin role whose grant hasn't run out
    bind.execute(text(
        'INSERT INTO member_subscription (user_id, offer_id, provider_kind, provider_reference, status, created_at) '
        "SELECT u.id, :offer_id, 'complimentary', 'complimentary-' || u.id || '-' || md5(random()::text), 'active', "
        "now() AT TIME ZONE 'utc' FROM \"user\" u "
        'WHERE u.ap_id IS NULL AND u.deleted IS NOT TRUE AND (u.id = 1 OR EXISTS ('
        '  SELECT 1 FROM role_grant g JOIN role r ON r.id = g.role_id WHERE g.user_id = u.id AND r.built_in '
        "  AND (g.expires_at IS NULL OR g.expires_at > now() AT TIME ZONE 'utc'))) "
        "AND NOT EXISTS (SELECT 1 FROM member_subscription s WHERE s.user_id = u.id AND s.provider_kind = 'complimentary' "
        "AND s.status = 'active')"), {'offer_id': offer_id})
    bind.execute(text("UPDATE promo_code SET benefit = NULL WHERE owner_id IS NOT NULL AND benefit = 'none'"))


def downgrade():
    pass   # a grant made then is an ordinary one; ending it is the edit page's
