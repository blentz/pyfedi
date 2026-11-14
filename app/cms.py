"""The mini CMS: what every page's header and footer link to, and the Donate page a site starts with.

A link is a page's choice (CmsPage.in_header, CmsPage.in_footer). It follows the page's title and URL, and goes when the
page goes, so nothing in a template names /donate.

A page may be for members only (CmsPage.members_only): somebody not logged in is sent to log in instead, and is not
shown links to it. Every place that serves a page asks members_only_refusal first.
"""
from flask import current_app
from flask_login import current_user

from app import cache
from app.models import CmsPage

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
def cms_nav_links(signed_in: bool = False) -> dict:
    """{'header': [...], 'footer': [...]}, each link {'url', 'title'}. Every page render reads this, passing whether
    the visitor is logged in: members-only pages are linked for them alone."""
    pages = CmsPage.query.filter(CmsPage.in_header.is_(True) | CmsPage.in_footer.is_(True)).order_by(CmsPage.id).all()
    if not signed_in:
        pages = [page for page in pages if not page.members_only]
    return {'header': [{'url': page.url, 'title': page.title} for page in pages if page.in_header],
            'footer': [{'url': page.url, 'title': page.title} for page in pages
                       if page.in_footer and page.url not in FIXED_FOOTER_URLS]}


def cms_nav_links_changed() -> None:
    cache.delete_memoized(cms_nav_links)


def members_only_refusal(page: CmsPage):
    """The response for somebody who may not read this page (the login page, then back here), or None if they may."""
    if page.members_only and not current_user.is_authenticated:
        return current_app.login_manager.unauthorized()
    return None
