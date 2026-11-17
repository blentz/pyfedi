"""What this storefront offers. On the onion that is link-outs, and a provider that has an onion address of its own
(C11): a link tells a provider nothing about the onion, where a checkout's return address would."""
from babel.numbers import format_currency, get_currency_precision
from flask import current_app
from flask_babel import get_locale, ngettext
from flask_login import current_user

from app import cache, db
from app.models import MemberSubscription, Offer, OfferDomain, Payment, SiteDomain
from app.onion import is_onion_request
from app.payments.adapters import adapter_for
from app.storefront import current_storefront


LINK = 'link'


def offers_on(domain_id: int | None, onion: bool) -> list[Offer]:
    """The active offers on a domain whose provider is usable. An offer with no domains is on all of them (M2)."""
    offers = Offer.query.filter_by(active=True).order_by(Offer.offer_group, Offer.position, Offer.id).all()
    limited = {}
    for offer_id, site_domain_id in db.session.execute(db.select(OfferDomain.offer_id, OfferDomain.site_domain_id)):
        limited.setdefault(offer_id, set()).add(site_domain_id)
    usable = {LINK: True}
    for offer in offers:
        if offer.provider_kind not in usable:
            adapter = adapter_for(offer.provider_kind)
            usable[offer.provider_kind] = adapter is not None and adapter.available() and (not onion or adapter.on_onion())
    return [offer for offer in offers
            if usable[offer.provider_kind] and (offer.id not in limited or domain_id in limited[offer.id])]


def membership_groups() -> frozenset[str]:
    """The offer groups some domain's membership is paid for with (plan 2026-11-16 DM1)."""
    return frozenset(db.session.execute(db.select(Offer.offer_group).join(
        SiteDomain, SiteDomain.registration_offer_id == Offer.id)).scalars())


def is_tier(offer: Offer | None, groups: frozenset[str] | None = None) -> bool:
    """A membership tier: a subscription in a membership group. Anything else on offer is a donation (DM1)."""
    return offer is not None and offer.kind == 'subscription' and \
        offer.offer_group in (membership_groups() if groups is None else groups)


def offers_here() -> list[Offer]:
    return offers_on(current_storefront().id, is_onion_request())


@cache.memoize(timeout=3600)
def _any_offer_on(domain_id: int | None, onion: bool) -> bool:
    return bool(offers_on(domain_id, onion))


def offers_changed() -> None:
    cache.delete_memoized(_any_offer_on)


def offers_available() -> bool:
    """Every signed-in page's menu asks this, so it is cached and cleared by whatever changes the answer."""
    return _any_offer_on(current_storefront().id, is_onion_request())


def tiers_on(domain) -> list[Offer]:
    """What a gift on `domain` can be a free period of (plan 2026-11-16 R2): the subscriptions on offer there in its
    membership group; on a domain without membership, every subscription on offer there. Cheapest first."""
    subscriptions = [offer for offer in offers_on(domain.id, domain.is_onion) if offer.kind == 'subscription']
    own = next((offer for offer in subscriptions if offer.id == domain.registration_offer_id), None)
    if own is not None:
        subscriptions = [offer for offer in subscriptions if offer.offer_group == own.offer_group]
    return sorted(subscriptions, key=lambda offer: (offer.amount_minor is None, offer.amount_minor or 0, offer.id))


def subscription_settings_shown() -> bool:
    """Whether Settings has a Subscription tab for the signed-in member (plan 2026-11-16 DM3): somewhere has membership
    tiers, or they have held a subscription or paid for something."""
    if not current_user.is_authenticated:
        return False
    if membership_groups():
        return True
    return db.session.query(db.exists().where(MemberSubscription.user_id == current_user.id)).scalar() or \
        db.session.query(db.exists().where(Payment.user_id == current_user.id)).scalar()


def donations_menu_shown() -> bool:
    """Whether a signed-in member's menu leads to /donations: somebody holding a subscription must still reach it to
    cancel, with every offer gone."""
    return offers_available() or (not is_onion_request() and current_user.donor_until is not None)


def money(amount_minor: int, currency: str) -> str:
    code = currency.upper()
    return format_currency(amount_minor / 10 ** get_currency_precision(code), code,
                           locale=str(get_locale() or current_app.config.get('BABEL_DEFAULT_LOCALE') or 'en'))



def money_major(amount, currency: str) -> str:
    """An amount in whole units of `currency` (a rate per bitcoin, say), as money() shows one in its minor unit."""
    return format_currency(amount, currency.upper(),
                           locale=str(get_locale() or current_app.config.get('BABEL_DEFAULT_LOCALE') or 'en'))

def period_text(count: int, interval: str | None) -> str:
    """`count` of a tier's billing periods, in words: '1 month', '3 months', '1 year' (a gift's length)."""
    if interval == 'year':
        return ngettext('%(num)d year', '%(num)d years', count)
    return ngettext('%(num)d month', '%(num)d months', count)
