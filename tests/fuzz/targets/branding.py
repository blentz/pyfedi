"""Property checks for a domain's branding on the onion (app/branding.py, per-domain branding spec §3.4).

An admin's CSS, sidebar, About and tagline are arbitrary text, and on the onion nothing in them may make a visitor's
browser fetch from another host. Three checks hold the scanner and the scrubber to that:

* branding_offsite_scan: random text as CSS, as HTML and as a URL, through offsite_references with the onion's own
  host. No exception; a list of distinct strings, each naming a host other than the onion's.
* branding_onion_safe: a random Branding through onion_safe. No exception; afterwards no HTML field (the CSS read as
  HTML included) ends inside markup, by the oracle's own parse (an element after the field is read as itself, inside no
  raw-text or RCDATA element), and offsite_references finds nothing off the onion in its CSS (read as CSS and as
  HTML), its tagline, sidebar and About HTML, each as written and followed by each of a set of page finishings of the
  oracle's own, or in its logo and icon URLs,
  and a field onion_safe left alone is the input's own. Fields that are text (the name, the Markdown sidebar, the
  theme) are as given.
* branding_style_kept_open: _style_kept_open never raises, leaves no `</` in the custom CSS, so the CSS can never close
  the `<style>` element pages write it in, and changes no other field.

No database and no network. The domain registry is swapped for one with SERVER_NAME alone (_no_domain_rows), so
nothing reads site_domain.
"""

import app.storefront as storefront
from bs4 import BeautifulSoup

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


# What a page may write after a field, chosen apart from the scanner's own finishings so the oracle does not pass by
# sharing them: nothing, a closed quote of either kind, the end of an unquoted value, an attribute, the page's markup
_ORACLE_FINISHINGS = ('', '"', "'", ' ', '>', '">', "'>", ' >', '"\'>', ' alt="x">', '</strong></p>', '\n<p>x</p>')


# The oracle's own reading of "the page's markup after the field stays markup", apart from ends_inside_markup's: an
# element of its own after the field, which must parse as itself, in no element a browser reads as text
_ORACLE_END = '<b id="oracle-end">end</b>'
_TEXT_CONTENT = ('style', 'script', 'textarea', 'title', 'xmp', 'noembed', 'noframes', 'noscript', 'plaintext',
                 'iframe')


def _ends_in_data_state(html: str) -> bool:
    if html.rfind('<') > html.rfind('>'):
        return False
    soup = BeautifulSoup(html + _ORACLE_END, 'html.parser', on_duplicate_attribute='ignore')
    ends = [tag for tag in soup.find_all('b') if tag.attrs == {'id': 'oracle-end'} and tag.get_text() == 'end']
    return len(ends) == 1 and not any(parent.name in _TEXT_CONTENT for parent in ends[0].parents)


def _offsite_anywhere(branding: Branding) -> list[str]:
    """Each field read as pages write it, one at a time (every page writes each field in a place of its own, so two
    fields never run together), as written and followed by each of the oracle's finishings."""
    found = offsite_references(css=branding.additional_css,
                               urls=[getattr(branding, field) for field in _URL_FIELDS], own_host=ONION_HOST)
    for field in _HTML_FIELDS + ('additional_css',):
        for finishing in _ORACLE_FINISHINGS:
            found += offsite_references(html=getattr(branding, field) + finishing, own_host=ONION_HOST)
    return found


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
    for field in _HTML_FIELDS + ('additional_css',):
        assert _ends_in_data_state(getattr(safe, field)), f'{field} still ends inside markup: {getattr(safe, field)!r}'
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
