"""The mini CMS: what every page's header and footer link to, and the Donate page a site starts with.

A link is a page's choice (CmsPage.in_header, CmsPage.in_footer). It follows the page's title and URL, and goes when the
page goes, so nothing in a template names /donate.
"""
from flask import current_app

from app import cache
from app.models import CmsPage
from app.onion import is_onion_request

# Both templates' footers always link to these, whether or not a CMS page has replaced what they show.
FIXED_FOOTER_URLS = frozenset({'/about', '/privacy'})

DONATE_URL = '/donate'
DONATE_TITLE = 'Donate'
DONATE_BODY = """PieFed is free and open-source software while operating without any advertising, monetization, or reliance on venture capital. Your donations are vital in supporting the PieFed development effort, allowing us to expand and enhance PieFed with new features.

<div class="btn-group btn-group-lg"><a class="btn btn-primary" href="https://www.patreon.com/PieFed">Donate using Patreon</a><a class="btn btn-primary" href="https://liberapay.com/PieFed/">Donate using Liberapay</a><a class="btn btn-primary" href="https://ko-fi.com/piefed">Donate using Ko-fi</a></div>

To support this site directly, see the [donation plans](/donations).
"""
DONATE_BODY_HTML = """<p>PieFed is free and open-source software while operating without any advertising, monetization, or reliance on venture capital. Your donations are vital in supporting the PieFed development effort, allowing us to expand and enhance PieFed with new features.</p>
<div class="btn-group btn-group-lg"><a class="btn btn-primary" href="https://www.patreon.com/PieFed" rel="nofollow ugc" target="">Donate using Patreon</a><a class="btn btn-primary" href="https://liberapay.com/PieFed/" rel="nofollow ugc" target="">Donate using Liberapay</a><a class="btn btn-primary" href="https://ko-fi.com/piefed" rel="nofollow ugc" target="">Donate using Ko-fi</a></div>
<p>To support this site directly, see the <a href="/donations" rel="nofollow ugc" target="">donation plans</a>.</p>
"""


@cache.memoize(timeout=3600)
def cms_nav_links() -> dict:
    """{'header': [...], 'footer': [...]}, each link {'url', 'title'}. Every page render reads this."""
    pages = CmsPage.query.filter(CmsPage.in_header.is_(True) | CmsPage.in_footer.is_(True)).order_by(CmsPage.id).all()
    return {'header': [{'url': page.url, 'title': page.title} for page in pages if page.in_header],
            'footer': [{'url': page.url, 'title': page.title} for page in pages
                       if page.in_footer and page.url not in FIXED_FOOTER_URLS]}


def cms_nav_links_changed() -> None:
    cache.delete_memoized(cms_nav_links)


def stripe_plans_available() -> bool:
    """Whether /donations has anything to offer here. Never on the onion: Stripe would be told the onion's host."""
    return bool(current_app.config['STRIPE_SECRET_KEY']) and not is_onion_request()
