"""[checkout N] in a CMS page draws offer N's payment button where it stands, as /donations draws it.

A link written in markdown can't start a checkout, which is a POST carrying the member's CSRF token, so the button is
drawn when the page is shown, not when it is saved: the stored HTML keeps the shortcode. `[checkout N Some words]` puts
those words on the button. Code (`...` or an indented block) shows a shortcode as it is written.
"""
import re

import flask
from flask_login import current_user
from markupsafe import Markup

from app.onion import is_onion_request
from app.payments.adapters import adapter_for
from app.payments.core import active_subscriptions_of
from app.payments.offers import LINK, offers_here

# Code is matched first and kept whole. A label runs to the bracket and can't cross a tag.
SHORTCODE = re.compile(r'(<(pre|code)\b.*?</\2>)|\[checkout (\d+)(?: ([^\]<]+))?\]', re.DOTALL)


def checkout_shortcodes(html: str) -> Markup:
    """The page's own HTML, from its admin, with each shortcode replaced by its button."""
    if '[checkout ' not in html:
        return Markup(html)
    offers = {offer.id: offer for offer in offers_here()}
    prepaid = {offer.id for offer in offers.values() if offer.provider_kind != LINK and adapter_for(offer.provider_kind).prepaid}
    held = set()
    if current_user.is_authenticated:
        onion = is_onion_request()
        held = {row.offer_id for row in active_subscriptions_of(current_user) if not onion or row.current_period_end}
    held -= prepaid   # one prepaid period is renewed by paying for the next

    def button(match: re.Match) -> str:
        if match.group(1):
            return match.group(1)
        offer = offers.get(int(match.group(3)))
        label = Markup(match.group(4).strip()) if match.group(4) else None   # already escaped by the markdown
        return flask.render_template('payments/_checkout_button.html', offer=offer, label=label,
                                     held=offer is not None and offer.id in held,
                                     prepaid=offer is not None and offer.id in prepaid)

    return Markup(SHORTCODE.sub(button, html))
