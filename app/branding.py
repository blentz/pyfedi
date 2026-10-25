"""What each domain looks like: its name, description, logos, theme, CSS, sidebar and about text (project 4).

A domain's `DomainBranding` row holds only what it sets. Every field it leaves NULL or blank is the first domain's,
which is `Site` row 1's; the first domain itself is exactly `Site`'s values.
"""
from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
import re
import secrets
from typing import NamedTuple

from bs4 import BeautifulSoup
from bs4.element import Stylesheet
from flask import current_app

from app import cache, db, shaped_memo_name
# The modules, not the names: app.storefront imports this module to clear its memo, so whichever of the two is imported
# first finds the other mid-import (import cycle: storefront)
import app.models as models
import app.storefront as storefront


class Branding(NamedTuple):
    name: str
    description: str
    logo: str
    logo_180: str
    logo_152: str
    logo_32: str
    logo_16: str
    default_theme: str
    additional_css: str
    sidebar: str   # the Markdown sidebar_html is made from: page descriptions are its text
    sidebar_html: str
    about_html: str


def own_branding(domain: storefront.DomainInfo) -> models.DomainBranding | None:
    """The row `domain` stores its own values in, or None. Not memoised: the admin form and federation read it."""
    return db.session.get(models.DomainBranding, domain.id)


def own_value(domain: storefront.DomainInfo, field: str) -> str:
    """`domain`'s own non-blank `field`, else ''. What other servers and clients read (spec §3.3) publishes a sister
    domain's own value where it set one and today's otherwise. The first domain has none: its branding is Site's."""
    row = None if domain.is_default else own_branding(domain)
    return (getattr(row, field) if row is not None else None) or ''


# A logo's sized copies: a domain with a logo of its own shows only its own (spec §5, how a blank logo falls back)
_ICON_FIELDS = frozenset({'logo_180', 'logo_152', 'logo_32', 'logo_16'})


@cache.memoize(timeout=300, make_name=shaped_memo_name)
def _effective(site_domain_id: int | None, is_default: bool) -> Branding:
    """Each field the row sets, else Site's. A row with its own `logo` takes its icon sizes from itself alone: a
    blank size is PieFed's default, as the manifest's icons are, never the first domain's icon beside the domain's
    logo."""
    site = db.session.get(models.Site, 1)
    row = None if is_default else db.session.get(models.DomainBranding, site_domain_id)
    own_logo = bool(getattr(row, 'logo', None))

    def value(field):
        if own_logo and field in _ICON_FIELDS:
            return getattr(row, field) or ''
        return getattr(row, field, None) or getattr(site, field) or ''
    return Branding(*(value(field) for field in Branding._fields))


def branding_for(domain: storefront.DomainInfo) -> Branding:
    """The values `domain` shows: its own where it set one, else Site row 1's. Memoised per domain id."""
    return _effective(domain.id, domain.is_default)


def current_branding() -> Branding:
    """The request's domain's branding, as its pages show it. The one way pages read branding (the `branding` template
    value and the `current_branding` Jinja global both come here), so on the onion nothing off-site gets past it."""
    domain = storefront.current_storefront()
    effective = _style_kept_open(branding_for(domain))
    return _onion_safe(effective, domain.domain) if domain.is_onion else effective


def _style_kept_open(branding: Branding) -> Branding:
    """`branding` with its custom CSS unable to close the `<style>` element pages write it in: each `</` (whatever
    follows, `</STYLE` too) becomes `<\\/`, which CSS reads as `</` and HTML does not read as an end tag. A user's own
    CSS is not branding and is left alone: it reaches only that user (spec §3.2)."""
    if '</' not in branding.additional_css:
        return branding
    return branding._replace(additional_css=branding.additional_css.replace('</', '<\\/'))


# ---------------------------------------------------------------------------------------------------------------------
# Off-site references (spec §3.4). Off-site is any host a browser would load from other than the domain's own, read as
# a browser reads a URL (`storefront.browser_host`); a relative URL, `data:` and any other scheme with no host are on
# the page's own domain. CSS is tokenised as the onion media proxy tokenises it (`_css_url_spans`): every `url(...)`,
# quoted or not, and every string, which covers `@import "x"` and `image-set("x" 1x)`; escapes are undone.

_URL_FIELDS = ('logo', 'logo_180', 'logo_152', 'logo_32', 'logo_16')
# The tagline is HTML too: every page that shows it writes it `|safe`, as Site's always was
_HTML_FIELDS = ('description', 'sidebar_html', 'about_html')
# What a browser loads as soon as it parses the element, without the visitor following anything: never an `a`'s href
_LOADING_ATTRS = frozenset({'src', 'poster', 'data', 'background', 'xlink:href'})
_LOADING_HREF_TAGS = frozenset({'base', 'image', 'use', 'feimage'})   # tag names as html.parser lower-cases them
# A `<link>` loads (or connects to) its href for these: stylesheets, icons and resource hints, never a canonical or
# alternate link, which only names a page
_LOADING_RELS = frozenset({'stylesheet', 'icon', 'apple-touch-icon', 'apple-touch-icon-precomposed', 'mask-icon',
                           'manifest', 'preload', 'modulepreload', 'prefetch', 'prerender', 'preconnect',
                           'dns-prefetch'})


# Some of the ways the page's own markup after a field can finish a tag the field leaves open: as it stands, after a
# closed quote of either kind, or after an unquoted value ends. A field is scanned finished each way too, so a refusal
# or warning can name what the finished tag would load. No set of finishings is every page's: on the onion a field
# that ends inside markup goes whole (`ends_inside_markup`), whatever it names.
_PAGE_FINISHINGS = ('>', '">', "'>", ' >')


# Elements whose content a browser reads as text (raw text, RCDATA, or all the rest of the page): a field that leaves
# one open swallows the page's markup after it
_SWALLOWING = frozenset({'style', 'script', 'textarea', 'title', 'xmp', 'noembed', 'noframes', 'noscript', 'plaintext',
                         'iframe'})
_SENTINEL = 'data-end-sentinel'


def ends_inside_markup(text: str) -> bool:
    """Whether `text` ends anywhere but data state, so the page's own markup after it would be read as part of it: a
    tag, a quoted value, a comment, or a raw-text or RCDATA element left open. A page finishes such a field with
    whatever it writes next, which nothing reading the field alone can know.

    First the cheap test, its last '<' after its last '>'. Then the parser's answer: `text` followed by a sentinel
    element of a fresh token must parse with that element real, with that one attribute, and inside no element whose
    content is text."""
    if '<' not in text:
        return False
    if text.rfind('<') > text.rfind('>'):
        return True
    token = secrets.token_hex(8)
    soup = BeautifulSoup(f'{text}<i {_SENTINEL}="{token}"></i>', 'html.parser', on_duplicate_attribute='ignore',
                         multi_valued_attributes=None)
    found = [tag for tag in soup.find_all('i') if tag.attrs == {_SENTINEL: token}]
    return len(found) != 1 or any(parent.name in _SWALLOWING for parent in found[0].parents)


def _scan_finished(html: str, own_host: str) -> list[str]:
    """The off-site loads in `html` followed by each of `_PAGE_FINISHINGS`."""
    return [url for finishing in _PAGE_FINISHINGS for url in _scan_html(html + finishing, own_host)[1]]


def _offsite(url: str, own_host: str) -> bool:
    host = storefront.browser_host(url)
    return bool(host) and host != own_host   # UNMAPPABLE is never a host of ours


def _scan_css(css: str, own_host: str) -> tuple[str, list[str]]:
    """`css` with each off-site URL emptied in place (`url()`, `@import ""`: an empty URL loads nothing), and those
    URLs. `css` itself when it has none."""
    text, spans = storefront._css_url_spans(css)
    found, out, last = [], [], 0
    for start, end, url in spans:
        if _offsite(url, own_host):
            found.append(url)
            out.append(text[last:start])
            last = end
    return (''.join(out) + text[last:] if found else css), found


# SVG presentation attributes that paint, clip or filter with url(...): read as CSS
_CSS_ATTRS = frozenset({'style', 'fill', 'stroke', 'mask', 'filter', 'clip-path', 'marker-start', 'marker-mid',
                        'marker-end', 'cursor'})
_SRCSET_ATTRS = frozenset({'srcset', 'imagesrcset'})
# A refresh's content (HTML "shared declarative refresh steps"): a time, a separator, `url=` optionally, the URL
# optionally quoted. Every part is optional, so it always matches.
_REFRESH = re.compile(r'\s*[\d.]*\s*[;,]?\s*(?:url\s*=\s*)?(["\']?)([^"\']*)', re.IGNORECASE)


def _refresh_url(tag, name: str, value: str) -> list[str]:
    """The URL a `<meta http-equiv=refresh>` sends the browser to, as a one-item list; [] for any other attribute."""
    if tag.name != 'meta' or name != 'content' or tag.get('http-equiv', '').lower() != 'refresh':
        return []
    return [_REFRESH.match(value)[2].strip()]


def _loads_href(tag) -> bool:
    if tag.name == 'link':
        return bool(_LOADING_RELS & set(tag.get('rel', '').lower().split()))
    return tag.name in _LOADING_HREF_TAGS


def _scan_attribute(tag, name: str, value: str, own_host: str) -> tuple[str | None, list[str]]:
    """An attribute's value with its off-site loads gone (None: drop the attribute), and those URLs."""
    if name in _CSS_ATTRS:
        return _scan_css(value, own_host)
    if name in _SRCSET_ATTRS:
        urls = [value[start:end] for start, end in storefront._srcset_urls(value)]
    elif name in _LOADING_ATTRS or (name == 'href' and _loads_href(tag)):
        urls = [value]
    else:
        urls = _refresh_url(tag, name, value)
    return None, [url for url in urls if _offsite(url, own_host)]


def _scan_html(html: str, own_host: str) -> tuple[str, list[str]]:
    """`html` with each off-site load removed: the attribute that names it, or the URL inside a `style` attribute or
    `<style>` element. `html` itself, never re-serialised, when it has none."""
    soup = BeautifulSoup(html, 'html.parser', on_duplicate_attribute='ignore', multi_valued_attributes=None)
    found = []
    for tag in soup.find_all(True):
        for name, value in list(tag.attrs.items()):
            kept, offsite = _scan_attribute(tag, name, value, own_host)
            if offsite:
                found += offsite
                if kept is None:
                    del tag[name]
                else:
                    tag[name] = kept
        if tag.name == 'style':
            kept, offsite = _scan_css(tag.string or '', own_host)
            if offsite:
                found += offsite
                tag.string.replace_with(Stylesheet(kept))
    return (soup.decode(formatter=storefront._FORMATTER) if found else html), found


def onion_url(domain: storefront.DomainInfo, url: str) -> str:
    """`url` as `domain`'s pages may name it: '' when `domain` is the onion and `url` is off it, else `url`. For a
    URL read from somewhere other than `current_branding()` (the manifest's inherited icons)."""
    return '' if domain.is_onion and _offsite(url, storefront.normalise_host(domain.domain)) else url


def offsite_references(css: str = '', html: str = '', urls: Iterable[str] = (), own_host: str = '') -> list[str]:
    """Every reference a browser would fetch from a host other than `own_host`, once each, in order: in `css`, its
    `url(...)` and `@import` targets; in `html`, what loads by itself (`src`, `srcset`, `poster`, `data`, `background`,
    the href of a stylesheet, icon or resource-hint `<link>`, `imagesrcset`, inline `style`, `<style>`, SVG paint
    and clip `url()`s, a `<meta http-equiv=refresh>` target), never a plain link or a canonical or alternate `<link>`;
    and each of `urls`. `html` is read as written and followed by each of `_PAGE_FINISHINGS`, as a page may finish a
    tag it leaves open. With no `own_host`, every reference that names a host."""
    own_host = storefront.normalise_host(own_host)
    found = _scan_css(css, own_host)[1] + _scan_html(html, own_host)[1] + _scan_finished(html, own_host) + \
        [url for url in urls if _offsite(url, own_host)]
    return list(dict.fromkeys(found))


def onion_safe(branding: Branding, own_host: str) -> Branding:
    """`branding` with every off-site reference removed, each removal logged at info; `branding` itself when it has
    none. A logo or icon URL becomes '' (the page shows PieFed's own); CSS and HTML lose only the reference, except a
    field that ends inside markup (`ends_inside_markup`), or whose tag finished by the page loads off the onion: it goes
    whole. The CSS is checked as HTML too. The Markdown `sidebar` stays: pages show it as text, and text fetches
    nothing."""
    host = storefront.normalise_host(own_host)
    changes, dropped = {}, []
    for field in _URL_FIELDS:
        if _offsite(getattr(branding, field), host):
            changes[field] = ''
            dropped.append((field, getattr(branding, field)))
    for field, scan in [('additional_css', _scan_css)] + [(field, _scan_html) for field in _HTML_FIELDS]:
        kept, offsite = scan(getattr(branding, field), host)
        if offsite:
            changes[field] = kept
            dropped += [(field, url) for url in offsite]
    # A field that ends inside a tag is finished by the markup the page writes after it, in ways no scan of the field
    # alone can know: it goes whole, whatever the tag. So does one whose tag, finished any way scanned, loads off the
    # onion (an open quote holding a '>' ends nowhere near a tag's end). The CSS is read as HTML as well, belt and
    # braces with `_style_kept_open`: markup in it would load should its style element ever be closed, and no
    # stylesheet holds such markup.
    unfinished = []
    for field in _HTML_FIELDS + ('additional_css',):
        value = changes.get(field, getattr(branding, field))
        markup = _scan_html(value, host)[1] if field == 'additional_css' else []
        markup += _scan_finished(value, host)
        if ends_inside_markup(value):
            unfinished.append(field)
            changes[field] = ''
        elif markup:
            changes[field] = ''
        dropped += [(field, url) for url in markup]
    for field in unfinished:
        current_app.logger.info('branding: %s dropped on %s: it ends inside an unfinished tag or other markup', field, host)
    for field, url in dict.fromkeys(dropped):
        current_app.logger.info('branding: %s dropped from %s on %s: off-site on the onion', url, field, host)
    return branding._replace(**changes) if changes else branding


# Keyed by the value itself, so a changed branding is a new entry and nothing needs clearing: each worker scans an onion
# branding once, not at every page (macros ask once per post teaser), and logs its removals once
_onion_safe = lru_cache(maxsize=16)(onion_safe)

