"""Shortcodes in a CMS page, drawn when the page is shown, so they know who is looking and carry their CSRF token:

* `[checkout N]` draws offer N's payment button where it stands, as /donations draws it. A link written in markdown can't
  start a checkout, which is a POST carrying the member's CSRF token. `[checkout N Some words]` puts those words on the
  button. A BTCPay offer's button shows the day's bitcoin price beside it (plan 2026-11-16 DM5).
* `[btcprice N]` writes BTCPay offer N's price in bitcoin today, or nothing (DM5).
* `[membership]` draws the signed-in member's membership: status, cancelling, codes, payments (DM3).

The stored HTML keeps the shortcodes. Code (`...` or an indented block) shows a shortcode as it is written.

Only the page's text is rewritten, never its markup: a shortcode in an attribute stays as written, since a button
drawn there would close the attribute and turn what follows into markup the sanitizer never saw.
"""
import re
import secrets

import flask
from bs4 import BeautifulSoup, NavigableString
from flask_login import current_user
from markupsafe import Markup

from app.complimentary import KIND as COMPLIMENTARY, complimentary_of
from app.models import Offer, Payment
from app.onion import is_onion_request
from app.payments.adapters import adapter_for
from app.payments.btc_price import todays_btc_price
from app.payments.core import active_subscriptions_of
from app.payments.discounts import HELD_CODE, find_discount_code
from app.payments.forms import RedeemCodeForm
from app.payments.offers import LINK, is_tier, membership_groups, offers_here
from app.payments.registration import joinable_tier_ids
from app.utils import get_setting

SHORTCODE = re.compile(r'\[(checkout|btcprice) (\d+)(?: ([^\]]+))?\]|\[membership\]')
MARKS = ('[checkout ', '[btcprice ', '[membership]')
LITERAL = {'pre', 'code', 'script', 'style', 'textarea'}
HISTORY_ROWS = 100
BTCPAY = 'btcpay'


def checkout_shortcodes(html: str) -> Markup:
    """The page's own HTML, from its admin, with each shortcode in its text replaced by what it draws."""
    if not any(mark in html for mark in MARKS):
        return Markup(html)
    soup = BeautifulSoup(html, 'html.parser')
    texts = [text for text in soup.find_all(string=SHORTCODE)
             if type(text) is NavigableString   # not a comment, CDATA or the like
             and not any(parent.name in LITERAL for parent in text.parents)]
    if not texts:
        return Markup(html)
    offers = {offer.id: offer for offer in offers_here()}
    prepaid = {offer.id for offer in offers.values() if offer.provider_kind != LINK and adapter_for(offer.provider_kind).prepaid}
    held = set()
    if current_user.is_authenticated:
        onion = is_onion_request()
        held = {row.offer_id for row in active_subscriptions_of(current_user) if not onion or row.current_period_end}
    held -= prepaid   # one prepaid period is renewed by paying for the next
    joinable = joinable_tier_ids(getattr(flask.g, 'site', None)) if current_user.is_anonymous else frozenset()
    complimentary = complimentary_of(current_user) is not None   # nothing to pay (K5)

    def btc_price(offer) -> str | None:
        return todays_btc_price(offer.amount_minor, offer.currency) \
            if offer is not None and offer.provider_kind == BTCPAY else None

    def button(match: re.Match) -> str:
        if match.group(1) is None:
            return _membership(complimentary)
        offer = offers.get(int(match.group(2)))
        if match.group(1) == 'btcprice':
            return btc_price(offer) or ''   # digits and a point: no markup
        label = match.group(3).strip() if match.group(3) else None   # text: the template escapes it
        return flask.render_template('payments/_checkout_button.html', offer=offer, label=label,
                                     held=offer is not None and offer.id in held, complimentary=complimentary,
                                     joinable=offer is not None and offer.id in joinable,
                                     prepaid=offer is not None and offer.id in prepaid, btc_price=btc_price(offer))

    # Each button goes in after the soup is written out, at a marker no page can contain, so that it is the template's
    # own markup and not the parser's rewriting of it
    marker = secrets.token_hex(16)
    buttons = []
    for text in texts:
        pieces, end = [], 0
        for match in SHORTCODE.finditer(text):
            pieces.append(text[end:match.start()])
            pieces.append(f'{marker}{len(buttons)}{marker}')
            buttons.append(button(match))
            end = match.end()
        pieces.append(text[end:])
        text.replace_with(''.join(pieces))
    return Markup(re.sub(f'{marker}(\\d+){marker}', lambda found: buttons[int(found.group(1))], str(soup)))


def _membership(complimentary: bool) -> str:
    """[membership] (DM3): the member's subscriptions that are not regular donations (those are /donations'), what they
    can do about them, a held code, the code box and their payments. A visitor is asked to log in."""
    here = flask.request.path
    if current_user.is_anonymous:
        return flask.render_template('payments/_membership.html', here=here)
    onion = is_onion_request()
    member = not onion   # cancelling, the card and the history stay off the onion
    groups = membership_groups()
    offers = {offer.id: offer for offer in Offer.query.all()}
    titles = {offer_id: offer.title for offer_id, offer in offers.items()}
    subscriptions = [row for row in active_subscriptions_of(current_user)
                     if (row.provider_kind in ('gift', COMPLIMENTARY) or is_tier(offers.get(row.offer_id), groups))
                     and (not onion or row.current_period_end or row.provider_kind == COMPLIMENTARY)]
    payments = Payment.query.filter_by(user_id=current_user.id).order_by(Payment.created_at.desc()) \
        .limit(HISTORY_ROWS).all() if member else []
    return flask.render_template('payments/_membership.html', here=here, member=member, subscriptions=subscriptions,
                                 titles=titles, payments=payments,
                                 redeem_form=None if complimentary else RedeemCodeForm(),
                                 held_code=None if complimentary else find_discount_code(flask.session.get(HELD_CODE)),
                                 currency=get_setting('payment_currency'))
