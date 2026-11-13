"""[checkout N] in a CMS page draws offer N's payment button where it stands, as /donations draws it.

A link written in markdown can't start a checkout, which is a POST carrying the member's CSRF token, so the button is
drawn when the page is shown, not when it is saved: the stored HTML keeps the shortcode. `[checkout N Some words]` puts
those words on the button. Code (`...` or an indented block) shows a shortcode as it is written.

Only the page's text is rewritten, never its markup: a shortcode in an attribute stays as written, since a button
drawn there would close the attribute and turn what follows into markup the sanitizer never saw.
"""
import re
import secrets

import flask
from bs4 import BeautifulSoup, NavigableString
from flask_login import current_user
from markupsafe import Markup

from app.onion import is_onion_request
from app.payments.adapters import adapter_for
from app.payments.core import active_subscriptions_of
from app.payments.offers import LINK, offers_here

SHORTCODE = re.compile(r'\[checkout (\d+)(?: ([^\]]+))?\]')
LITERAL = {'pre', 'code', 'script', 'style', 'textarea'}


def checkout_shortcodes(html: str) -> Markup:
    """The page's own HTML, from its admin, with each shortcode in its text replaced by its button."""
    if '[checkout ' not in html:
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

    def button(match: re.Match) -> str:
        offer = offers.get(int(match.group(1)))
        label = match.group(2).strip() if match.group(2) else None   # text: the template escapes it
        return flask.render_template('payments/_checkout_button.html', offer=offer, label=label,
                                     held=offer is not None and offer.id in held,
                                     prepaid=offer is not None and offer.id in prepaid)

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
