"""Property checks for a domain's branding on the onion (app/branding.py, per-domain branding spec §3.4).

An admin's CSS, sidebar, About and tagline are arbitrary text, and on the onion nothing in them may make a visitor's
browser fetch from another host. Three checks hold the scanner and the scrubber to that:

* branding_offsite_scan: random text as CSS, as HTML and as a URL, through offsite_references with the onion's own
  host. No exception; a list of distinct strings, each naming a host other than the onion's.
* branding_onion_safe: a random Branding through onion_safe. No exception; afterwards offsite_references finds nothing
  off the onion in its CSS (read as CSS and as HTML), its tagline, sidebar and About HTML, or its logo and icon URLs,
  and a field onion_safe left alone is the input's own. Fields that are text (the name, the Markdown sidebar, the
  theme) are as given.
* branding_style_kept_open: _style_kept_open never raises, leaves no `</` in the custom CSS, so the CSS can never close
  the `<style>` element pages write it in, and changes no other field.

No database and no network. The domain registry is swapped for one with SERVER_NAME alone (_no_domain_rows), so
nothing reads site_domain.
"""

import app.storefront as storefront
from app.branding import Branding, _style_kept_open, offsite_references, onion_safe
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _text
from tests.fuzz.targets.urls import _no_domain_rows

MAX_INPUT = 4096
ONION_HOST = 'fuzzfuzzfuzzfuzz.onion'
_URL_FIELDS = ('logo', 'logo_180', 'logo_152', 'logo_32', 'logo_16')
_HTML_FIELDS = ('description', 'sidebar_html', 'about_html')
_TEXT_FIELDS = ('name', 'default_theme', 'sidebar')


def _branding(data: bytes) -> Branding:
    """A Branding of random fields: each of its fields is one length-prefixed slice of `data`."""
    return Branding(*(_text(field) for field in _fields(data[:MAX_INPUT], len(Branding._fields))))


def _offsite_anywhere(branding: Branding) -> list[str]:
    """Each field read as pages write it, one at a time: every page writes each field in a place of its own, so two
    fields never run together."""
    return offsite_references(css=branding.additional_css, html=branding.additional_css,
                              urls=[getattr(branding, field) for field in _URL_FIELDS], own_host=ONION_HOST) + \
        [reference for field in _HTML_FIELDS
         for reference in offsite_references(html=getattr(branding, field), own_host=ONION_HOST)]


def check_branding_offsite_scan(data: bytes) -> None:
    css, html, url = (_text(field) for field in _fields(data[:MAX_INPUT], 3))
    with _no_domain_rows():
        found = offsite_references(css=css, html=html, urls=[url], own_host=ONION_HOST)
        assert isinstance(found, list) and all(isinstance(reference, str) for reference in found)
        assert len(found) == len(set(found)), f'a reference listed twice: {found!r}'
        for reference in found:
            host = storefront.browser_host(reference)
            assert host and host != ONION_HOST, f'{reference!r} is not off the onion'


def check_branding_onion_safe(data: bytes) -> None:
    branding = _branding(data)
    with _no_domain_rows():
        safe = onion_safe(branding, ONION_HOST)
        assert isinstance(safe, Branding)
        left = _offsite_anywhere(safe)
    assert left == [], f'off the onion after onion_safe: {left!r}'
    for field in _TEXT_FIELDS:
        assert getattr(safe, field) == getattr(branding, field), f'{field} changed'
    for field in _URL_FIELDS:
        assert getattr(safe, field) in (getattr(branding, field), ''), f'{field} rewritten, not dropped'


def check_branding_style_kept_open(data: bytes) -> None:
    branding = _branding(data)
    kept = _style_kept_open(branding)
    assert '</' not in kept.additional_css, f'{kept.additional_css!r} can close its style element'
    assert kept._replace(additional_css=branding.additional_css) == branding, 'a field other than the CSS changed'


TARGETS = [
    Target(name='branding_offsite_scan', check=check_branding_offsite_scan, surface='branding', budget_ms=None,
           needs_app=True),
    Target(name='branding_onion_safe', check=check_branding_onion_safe, surface='branding', budget_ms=None,
           needs_app=True),
    Target(name='branding_style_kept_open', check=check_branding_style_kept_open, surface='branding', budget_ms=None,
           needs_app=False),
]
