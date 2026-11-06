"""payments: the ledger, offers, subscriptions and encrypted provider credentials

Revision ID: b2e6c4a8d1f3
Revises: a7d3f1c9e5b2
Create Date: 2026-11-05 22:00:00.000000

Spec docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md, C5 and C6. A site with Stripe set up in its
environment keeps working: its two plans become offers in group `legacy`, and its subscribers become subscription rows
with no offer, since which plan each one is on is known only to Stripe. Nobody has to do anything or resubscribe. No
credential is read or stored here. The `User.stripe_*` columns stay: a later release removes them.
"""
import os

from alembic import op
import sqlalchemy as sa

revision = 'b2e6c4a8d1f3'
down_revision = 'a7d3f1c9e5b2'
branch_labels = None
depends_on = None

WHILE_SUBSCRIBED = '9999-12-31 00:00:00'   # app.constants.DONOR_WHILE_SUBSCRIBED


def seed_offers(bind, environ):
    for position, (price, text) in enumerate((('STRIPE_MONTHLY_SMALL', 'STRIPE_MONTHLY_SMALL_TEXT'),
                                              ('STRIPE_MONTHLY_BIG', 'STRIPE_MONTHLY_BIG_TEXT'))):
        if environ.get(price):
            bind.execute(sa.text("""
                INSERT INTO offer (provider_kind, kind, title, offer_group, provider_price_id, interval, grants_donor,
                                   position, active, created_at)
                VALUES ('stripe', 'subscription', :title, 'legacy', :price, 'month', true, :position, true,
                        now() AT TIME ZONE 'utc')
            """), {'title': environ.get(text) or environ[price], 'price': environ[price], 'position': position})


def carry_over_subscribers(bind):
    bind.execute(sa.text("""
        INSERT INTO member_subscription (user_id, provider_kind, provider_reference, status, created_at)
        SELECT DISTINCT ON (stripe_subscription_id) id, 'stripe', stripe_subscription_id, 'active',
               now() AT TIME ZONE 'utc'
        FROM "user" WHERE stripe_subscription_id IS NOT NULL AND stripe_subscription_id <> ''
        ORDER BY stripe_subscription_id, id
    """))
    bind.execute(sa.text("""
        UPDATE "user" SET donor_until = :forever WHERE stripe_subscription_id IS NOT NULL
        AND stripe_subscription_id <> ''
    """), {'forever': WHILE_SUBSCRIBED})


def upgrade():
    op.create_table('payment_provider',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('kind', sa.String(length=20), nullable=False, unique=True),
                    sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
                    sa.Column('credentials', sa.Text()),
                    sa.Column('settings', sa.JSON()))
    op.create_table('offer',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('provider_kind', sa.String(length=20), nullable=False),
                    sa.Column('kind', sa.String(length=20), nullable=False),
                    sa.Column('title', sa.String(length=255), nullable=False),
                    sa.Column('offer_group', sa.String(length=50), nullable=False),
                    sa.Column('provider_price_id', sa.String(length=255)),
                    sa.Column('amount_minor', sa.BigInteger()),
                    sa.Column('currency', sa.String(length=3)),
                    sa.Column('interval', sa.String(length=10)),
                    sa.Column('grants_donor', sa.Boolean(), server_default=sa.false(), nullable=False),
                    sa.Column('donor_days', sa.Integer()),
                    sa.Column('position', sa.Integer(), server_default='0', nullable=False),
                    sa.Column('active', sa.Boolean(), server_default=sa.true(), nullable=False),
                    sa.Column('created_at', sa.DateTime()))
    op.create_table('offer_domain',
                    sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offer.id', ondelete='CASCADE'),
                              primary_key=True),
                    sa.Column('site_domain_id', sa.Integer(), sa.ForeignKey('site_domain.id', ondelete='CASCADE'),
                              primary_key=True))
    op.create_table('payment',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='SET NULL')),
                    sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offer.id')),
                    sa.Column('provider_kind', sa.String(length=20), nullable=False),
                    sa.Column('provider_reference', sa.String(length=255), nullable=False),
                    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
                    sa.Column('currency', sa.String(length=3), nullable=False),
                    sa.Column('status', sa.String(length=20), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.Column('settled_at', sa.DateTime()),
                    sa.UniqueConstraint('provider_kind', 'provider_reference', name='uq_payment_provider_reference'))
    op.create_index(op.f('ix_payment_user_id'), 'payment', ['user_id'])
    op.create_index(op.f('ix_payment_offer_id'), 'payment', ['offer_id'])
    op.create_index(op.f('ix_payment_created_at'), 'payment', ['created_at'])
    op.create_table('member_subscription',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offer.id')),
                    sa.Column('provider_kind', sa.String(length=20), nullable=False),
                    sa.Column('provider_reference', sa.String(length=255), nullable=False, unique=True),
                    sa.Column('status', sa.String(length=20), nullable=False),
                    sa.Column('current_period_end', sa.DateTime()),
                    sa.Column('created_at', sa.DateTime()),
                    sa.Column('ended_at', sa.DateTime()))
    op.create_index(op.f('ix_member_subscription_user_id'), 'member_subscription', ['user_id'])
    op.create_index(op.f('ix_member_subscription_offer_id'), 'member_subscription', ['offer_id'])
    op.create_table('payment_event',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('provider_kind', sa.String(length=20), nullable=False),
                    sa.Column('event_id', sa.String(length=255), nullable=False),
                    sa.Column('received_at', sa.DateTime()),
                    sa.UniqueConstraint('provider_kind', 'event_id', name='uq_payment_event'))
    op.add_column('user', sa.Column('donor_until', sa.DateTime()))

    bind = op.get_bind()
    seed_offers(bind, os.environ)
    carry_over_subscribers(bind)


def downgrade():
    op.drop_column('user', 'donor_until')
    op.drop_table('payment_event')
    op.drop_table('member_subscription')
    op.drop_table('payment')
    op.drop_table('offer_domain')
    op.drop_table('offer')
    op.drop_table('payment_provider')
