"""A gift's length is a number of its tier's billing periods, not days

Revision ID: c6f1b4e8a2d5
Revises: b5e9a3d7c1f4
Create Date: 2026-11-17 02:00:00.000000

Owner 2026-11-17: each gift of a free period is a whole number of the tier's periods (1 month, 1 year...), one period
being what one payment buys (30 or 365 days, PERIOD_DAYS). Codes, member's referral codes and the referral default
stored days: each becomes its days divided by its tier's period, rounded UP so nobody gets less than promised. The
ones that were not a whole number of periods are logged.
"""
import json
import logging

from alembic import op
import sqlalchemy as sa

revision = 'c6f1b4e8a2d5'
down_revision = 'b5e9a3d7c1f4'
branch_labels = None
depends_on = None

log = logging.getLogger('alembic.runtime.migration')
PERIOD = "CASE o.interval WHEN 'year' THEN 365 ELSE 30 END"


def _period_days(bind, offer_id) -> int:
    interval = bind.execute(sa.text('SELECT interval FROM offer WHERE id = :id'), {'id': offer_id}).scalar() \
        if offer_id else None
    return 365 if interval == 'year' else 30


def _setting(bind, name):
    value = bind.execute(sa.text('SELECT value FROM settings WHERE name = :n'), {'n': name}).scalar()
    try:
        return json.loads(value) if value is not None else None
    except ValueError:
        return None


def _set(bind, name, value) -> None:
    bind.execute(sa.text('DELETE FROM settings WHERE name = :n'), {'n': name})
    if value is not None:
        bind.execute(sa.text('INSERT INTO settings (name, value) VALUES (:n, :v)'), {'n': name, 'v': json.dumps(value)})


def upgrade():
    bind = op.get_bind()
    op.add_column('promo_code', sa.Column('periods', sa.Integer()))
    uneven = bind.execute(sa.text(
        f'SELECT p.id, p.code, p.days, {PERIOD} FROM promo_code p LEFT JOIN offer o ON o.id = p.offer_id '
        f'WHERE p.days IS NOT NULL AND p.days % ({PERIOD}) <> 0')).all()
    for code_id, code, days, period in uneven:
        log.info('gift periods: code %s (%s) gave %s days; now %s period(s) of %s days', code_id, code, days,
                 -(-days // period), period)
    bind.execute(sa.text(f'UPDATE promo_code p SET periods = CEIL(p.days::numeric / ({PERIOD})) '
                         f'FROM offer o WHERE o.id = p.offer_id AND p.days IS NOT NULL'))
    bind.execute(sa.text('UPDATE promo_code SET periods = CEIL(days::numeric / 30) '
                         'WHERE offer_id IS NULL AND days IS NOT NULL'))
    op.drop_column('promo_code', 'days')
    # The referral default, and its predecessor (an offer id and days in two settings), become {'periods': n}
    chosen = _setting(bind, 'referral_benefit')
    legacy_offer = _setting(bind, 'referral_gift_offer_id')
    if not isinstance(chosen, dict) and legacy_offer:
        chosen = {'kind': 'gift', 'offer_id': legacy_offer, 'days': _setting(bind, 'referral_gift_days')}
    if isinstance(chosen, dict):
        days = chosen.pop('days', None)
        if days:
            chosen['periods'] = -(-int(days) // _period_days(bind, chosen.get('offer_id')))
        _set(bind, 'referral_benefit', chosen)
    _set(bind, 'referral_gift_offer_id', None)
    _set(bind, 'referral_gift_days', None)


def downgrade():
    bind = op.get_bind()
    op.add_column('promo_code', sa.Column('days', sa.Integer()))
    bind.execute(sa.text(f'UPDATE promo_code p SET days = p.periods * ({PERIOD}) '
                         f'FROM offer o WHERE o.id = p.offer_id AND p.periods IS NOT NULL'))
    bind.execute(sa.text('UPDATE promo_code SET days = periods * 30 WHERE offer_id IS NULL AND periods IS NOT NULL'))
    op.drop_column('promo_code', 'periods')
    chosen = _setting(bind, 'referral_benefit')
    if isinstance(chosen, dict):
        periods = chosen.pop('periods', None)
        if periods:
            chosen['days'] = periods * _period_days(bind, chosen.get('offer_id'))
        _set(bind, 'referral_benefit', chosen)
