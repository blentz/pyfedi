"""What this storefront offers. Phase 1's only provider is Stripe, so nothing is offered on the onion (C11)."""
from babel.numbers import format_currency, get_currency_precision
from flask import current_app
from flask_babel import get_locale
from flask_login import current_user

from app import cache, db
from app.models import MemberSubscription, Offer, OfferDomain
from app.onion import is_onion_request
from app.payments.adapters import adapter_for
from app.storefront import current_storefront


def _offers_on(domain_id: int | None) -> list[Offer]:
    """The active offers on a domain whose provider is usable. An offer with no domains is on all of them (M2)."""
    offers = Offer.query.filter_by(active=True).order_by(Offer.offer_group, Offer.position, Offer.id).all()
    limited = {}
    for offer_id, site_domain_id in db.session.execute(db.select(OfferDomain.offer_id, OfferDomain.site_domain_id)):
        limited.setdefault(offer_id, set()).add(site_domain_id)
    usable = {}
    for offer in offers:
        if offer.provider_kind not in usable:
            adapter = adapter_for(offer.provider_kind)
            usable[offer.provider_kind] = adapter is not None and adapter.available()
    return [offer for offer in offers
            if usable[offer.provider_kind] and (offer.id not in limited or domain_id in limited[offer.id])]


def offers_here() -> list[Offer]:
    return [] if is_onion_request() else _offers_on(current_storefront().id)


@cache.memoize(timeout=3600)
def _any_offer_on(domain_id: int | None) -> bool:
    return bool(_offers_on(domain_id))


def offers_changed() -> None:
    cache.delete_memoized(_any_offer_on)


def offers_available() -> bool:
    """Every signed-in page's menu asks this, so it is cached and cleared by whatever changes the answer."""
    return not is_onion_request() and _any_offer_on(current_storefront().id)


def active_subscriptions(user) -> list[MemberSubscription]:
    return MemberSubscription.query.filter_by(user_id=user.id, status='active').order_by(MemberSubscription.id).all()


def donations_menu_shown() -> bool:
    """Whether a signed-in member's menu leads to /donations: somebody holding a subscription must still reach it to
    cancel, with every offer gone."""
    if is_onion_request():
        return False
    return offers_available() or current_user.donor_until is not None


def money(amount_minor: int, currency: str) -> str:
    code = currency.upper()
    return format_currency(amount_minor / 10 ** get_currency_precision(code), code,
                           locale=str(get_locale() or current_app.config.get('BABEL_DEFAULT_LOCALE') or 'en'))
