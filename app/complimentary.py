"""Complimentary membership (plan 2026-11-15-complimentary-membership.md, K1-K8): a hidden tier that admins give as a
permanent 100% discount, and that every local account holding no subscription has. It is a subscription like the others,
of its own provider kind and group, with no end date. Nobody can buy it, and somebody holding it is not asked to pay.

Only the models are needed here, so that app.utils can grant it to a new account: anything under app.payments imports its
routes, which import app.utils (K8).
"""
import secrets

from sqlalchemy import exists

from app import db
from app.models import MemberSubscription, Offer, User, utcnow

KIND = 'complimentary'
TITLE = 'Complimentary'


def complimentary_offer() -> Offer:
    """The hidden offer every grant names. The migration makes it; a site made without migrations gets it here."""
    offer = Offer.query.filter_by(provider_kind=KIND).order_by(Offer.id).first()
    if offer is None:
        offer = Offer(provider_kind=KIND, kind='subscription', title=TITLE, offer_group=KIND, amount_minor=0,
                      active=False, grants_donor=False)
        db.session.add(offer)
        db.session.flush()
    return offer


def complimentary_of(user: User) -> MemberSubscription | None:
    """The complimentary membership this account holds now, if any."""
    if user is None or not user.is_authenticated:
        return None
    return MemberSubscription.query.filter_by(user_id=user.id, provider_kind=KIND, status='active').first()


def grant_complimentary(user: User) -> MemberSubscription:
    """Give it, or the one already held. The caller commits."""
    held = complimentary_of(user)
    if held is not None:
        return held
    row = MemberSubscription(user_id=user.id, offer_id=complimentary_offer().id, provider_kind=KIND,
                             provider_reference=f'{KIND}-{user.id}-{secrets.token_hex(8)}', status='active',
                             created_at=utcnow())
    db.session.add(row)
    user.access_lapsed_at = None
    db.session.flush()
    return row


def grant_to_new_account(user: User) -> None:
    """A local account being set up that has held no subscription gets it (K2). One that has, paid or gift, ended or
    not, is left to the membership rules (K3). The caller commits."""
    if not user.is_local():
        return
    if db.session.query(exists().where(MemberSubscription.user_id == user.id)).scalar():
        return
    grant_complimentary(user)


def end_complimentary(user: User) -> bool:
    """An admin taking it away (K6). False if it wasn't held. The caller refreshes access and commits."""
    held = complimentary_of(user)
    if held is None:
        return False
    held.status = 'ended'
    held.ended_at = utcnow()
    db.session.flush()
    return True
