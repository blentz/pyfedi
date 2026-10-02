from __future__ import annotations

import base64
import bisect
import gzip
import hashlib
from html import escape as html_escape
import io
import logging
import mimetypes
import math
import random
import time
import urllib
import warnings
from collections import defaultdict, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from functools import wraps, lru_cache
from json import JSONDecodeError
from time import sleep
from typing import List, Tuple, Optional
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import available_timezones
import socket
import os
import ipaddress

import pendulum
import flask
import httpx
import jwt
from jwt.exceptions import InvalidTokenError
import markdown2
import redis
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, NavigableString
from babel.numbers import format_compact_decimal
import orjson

from app.markdown_extras import apply_enhanced_image_attributes
from app.translation import LibreTranslateAPI

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

from furl import furl
from flask import current_app, json, redirect, url_for, request, make_response, Response, g, flash, abort
from flask_babel import _, lazy_gettext as _l
from flask_login import current_user, logout_user
from flask_wtf.csrf import validate_csrf
from sqlalchemy import text, or_, desc, asc, event, select, func, update
from sqlalchemy.orm import Session
from wtforms.fields import SelectMultipleField, StringField
from wtforms.widgets import ListWidget, CheckboxInput, TextInput
from wtforms.validators import ValidationError
from markupsafe import Markup
import boto3
from app import db, cache, httpx_client, celery, get_ip_address, plugins
from app.pinned_http import is_refused_address
from app.visibility import OPEN_VISIBILITIES, listable_sql, visible_to_sql
from app.constants import *
import re
from PIL import Image, ImageOps, ImageCms
from py_svg_hush import filter_svg

from captcha.audio import AudioCaptcha
from captcha.image import ImageCaptcha

from app.models import CronJobLog, Settings, Domain, Instance, BannedInstances, User, Community, DomainBlock, IpBan, \
    Site, Post, utcnow, Filter, CommunityMember, InstanceBlock, CommunityBan, Topic, UserBlock, Language, \
    File, ModLog, CommunityBlock, Feed, FeedMember, CommunityFlair, CommunityJoinRequest, Notification, UserNote, \
    PostReply, PostReplyBookmark, AllowedInstances, InstanceBan, Tag, Emoji, UserExtraField, ArchivedPostReply, \
    RevokedToken, CommunityFavorite, UserFollower, CommunityFlairBlock, s3_key_from_url, Role, RolePermission
from collections import Counter
from flask import has_request_context
import app as app_pkg

logger = logging.getLogger(__name__)

# Flask's render_template function, with support for themes added
def render_template(template_name: str, skip_protocol_replacement: bool = False, **context) -> Response:
    # R219: on a private instance a page's RSS link carries the member's token, which is what opens the feed
    if isinstance(context.get('rss_feed'), str) and g.site.private_instance and current_user.is_authenticated:
        context['rss_feed'] += f"?token={ensure_rss_token(current_user)}"
    theme = current_theme()
    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        content = flask.render_template(f'themes/{theme}/{template_name}', **context)
    else:
        content = flask.render_template(template_name, **context)

    if not skip_protocol_replacement and current_app.config['HTTP_PROTOCOL'] == 'mixed':  # mixed mode is for instances like retro.piefed.com which has a web ui that uses http while federation happens over https
        server_name = current_app.config['SERVER_NAME']
        content = content.replace(f"https://{server_name}", f"http://{server_name}")

    # Browser caching using ETags and Cache-Control
    resp = make_response(content)
    if current_user.is_anonymous:
        if 'etag' in context:
            resp.headers.add_header('ETag', context['etag'])
        resp.headers.add_header('Cache-Control', 'no-cache, must-revalidate')

    # Early Hints-compatible Link headers (for Cloudflare or supporting proxies)
    resp.headers['Link'] = (
        '</bootstrap/static/css/bootstrap.min.css>; rel=preload; as=style, '
        '</bootstrap/static/umd/popper.min.js>; rel=preload; as=script, '
        '</bootstrap/static/js/bootstrap.min.js>; rel=preload; as=script, '
        '</static/js/htmx.min.js>; rel=preload; as=script, '
        '</static/fonts/feather/feather.woff>; rel=preload; as=font; crossorigin'
    )

    return resp


def request_etag_matches(etag):
    if 'If-None-Match' in request.headers:
        old_etag = request.headers['If-None-Match']
        return old_etag == etag
    return False


def return_304(etag, content_type=None):
    resp = make_response('', 304)
    resp.headers.add_header('Cache-Control', 'no-cache, must-revalidate')
    if current_user.is_anonymous:
        resp.headers.add_header('ETag', request.headers['If-None-Match'])
        resp.headers.add_header('Vary', 'Accept, Accept-Language')
    else:
        resp.headers.add_header('Vary', 'Accept, Cookie, Accept-Language')
    if content_type:
        resp.headers.set('Content-Type', content_type)
    return resp


# Jinja: when a file was modified. Useful for cache-busting
def getmtime(filename):
    if os.path.exists('app/static/' + filename):
        return os.path.getmtime('app/static/' + filename)


# do a GET request to a uri, return the result
def get_request(uri, params=None, headers=None) -> httpx.Response:
    if is_invalid_get_request_uri(uri):
        current_app.logger.info(f"invalid get request {uri}")
        raise httpx.HTTPError("HTTPError: invalid uri") from None
    timeout = 15 if 'washingtonpost.com' in uri else 10  # Washington Post is really slow on og:image for some reason
    if headers is None:
        headers = {'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'}
    else:
        headers.update({'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'})
    if params and '/webfinger' in uri:
        payload_str = urllib.parse.urlencode(params, safe=':@')
    else:
        payload_str = urllib.parse.urlencode(params) if params else None
    try:
        response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout, follow_redirects=False)
    except httpx.InvalidURL as invalid_url:
        # Same normalisation as the ValueError clause below, for the same
        # reason: callers of get_request catch httpx.HTTPError. httpx.InvalidURL
        # is neither an HTTPError nor a ValueError -- it descends straight from
        # Exception -- so without this it escaped past all four handlers here.
        # Reachable whenever is_invalid_get_request_uri lets the uri through:
        # under DEBUG it short-circuits to False, and 'http://[v1.x]/y' passes
        # its checks even with DEBUG off.
        raise httpx.HTTPError(f"HTTPError: {str(invalid_url)}") from None
    except ValueError as ex:
        # Convert to a more generic error we handle
        raise httpx.HTTPError(f"HTTPError: {str(ex)}") from None
    except httpx.ReadError as connection_error:
        try:  # retry, this time with a longer timeout
            sleep(random.randint(3, 10))
            response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout * 2,
                                        follow_redirects=False)
        except Exception as e:
            current_app.logger.info(f"{uri} {connection_error}")
            # httpx.ReadError, not httpx_client.ReadError: httpx_client is an
            # httpx.Client INSTANCE and carries no such attribute, so the old
            # spelling raised AttributeError out of this handler instead of the
            # intended read error. That broke the invariant every caller relies
            # on -- that get_request normalises transport failure to
            # httpx.HTTPError -- and it is load-bearing for fixup_url's narrowed
            # except below, which would otherwise let the AttributeError escape.
            raise httpx.ReadError(f"HTTPReadError: {str(e)}") from connection_error
    except httpx.HTTPError as read_timeout:
        try:  # retry, this time with a longer timeout
            sleep(random.randint(3, 10))
            response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout * 2,
                                        follow_redirects=False)
        except Exception as e:
            current_app.logger.info(f"{uri} {read_timeout}")
            raise httpx.HTTPError(f"HTTPError: {str(e)}") from read_timeout
    except httpx.StreamError as stream_error:
        # Convert to a more generic error we handle
        raise httpx.HTTPError(f"HTTPError: {str(stream_error)}") from None

    return response


# Same as get_request except updates instance on failure and does not raise any exceptions
def get_request_instance(uri, instance: Instance, params=None, headers=None) -> httpx.Response:
    try:
        return get_request(uri, params, headers)
    except:
        instance.failures += 1
        instance.update_dormant_gone()
        db.session.commit()
        return httpx.Response(status_code=500)


# Saves an arbitrary object into a persistent key-value store. cached.
# Similar to g.site.* except g.site.* is populated on every single page load so g.site is best for settings that are
# accessed very often (e.g. every page load)
@cache.memoize(timeout=500)
def get_setting(name: str, default=None):
    setting = db.session.query(Settings).filter_by(name=name).first()
    if setting is None:
        return default
    else:
        try:
            return json.loads(setting.value)
        except JSONDecodeError:
            return default


# retrieves arbitrary object from persistent key-value store
def set_setting(name: str, value):
    setting = Settings.query.filter_by(name=name).first()
    if setting is None:
        db.session.add(Settings(name=name, value=json.dumps(value)))
    else:
        setting.value = json.dumps(value)
    db.session.commit()
    cache.delete_memoized(get_setting)


# Return the contents of a file as a string. Inspired by PHP's function of the same name.
def file_get_contents(filename):
    with open(filename, 'r') as file:
        contents = file.read()
    return contents


random_chars = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'


def gibberish(length: int = 10) -> str:
    return "".join([random.choice(random_chars) for x in range(length)])


# used by @cache.cached() for home page and post caching
def make_cache_key(sort=None, post_id=None, view_filter=None):
    if current_user.is_anonymous:
        return f'{request.url}_{sort}_{post_id}_anon_{request.headers.get("Accept")}_{request.headers.get("Accept-Language")}'  # The Accept header differentiates between activitypub requests and everything else
    else:
        return f'{request.url}_{sort}_{post_id}_user_{current_user.id}'


def is_image_url(url):
    common_image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.avif', '.svg+xml',
                               '.svg+xml; charset=utf-8']
    if not url:
        # THE FALSY GUARD, and the reason every url-shaped helper below carries
        # one. `except ValueError` cannot stand in for it: urlparse(None) does
        # NOT raise. It returns a ParseResultBytes whose members are all b'',
        # and the crash arrives one line later from a str method called on
        # bytes -- b''.endswith('.jpg') is
        # "TypeError: endswith first arg must be bytes or a tuple of bytes".
        # TypeError is not a ValueError, so the guard this branch added for
        # malformed STRINGS is blind to None.
        #
        # A None url is a normal state in this codebase now, not an error: this
        # branch made Post.url NULL-able in practice (Post.new() and
        # update_post_from_activity both store None for a url they will not
        # keep, without resetting post.type). So it belongs in a plain
        # conditional and not in exception handling.
        #
        # False is the same answer this returns for a url with no image
        # extension, and it also skips the mime_type_using_head HEAD request,
        # which has nothing to fetch.
        return False
    # D1408. This predicate decided whether a user's or peer's url was safe to
    # store as `File.source_url` at six sites in app/shared, and it sniffed the
    # extension off `urlparse(url).path` -- which for a `javascript:` url is
    # everything after the colon. Measured:
    #
    #     is_image_url('javascript:alert(1)')            False
    #     is_image_url('javascript:alert(1)/x.png')      True
    #     is_image_url('javascript:x//y.png')            True
    #     is_image_url('data:image/svg+xml,<svg/>.png')  True
    #
    # So `javascript:alert(1)/x.png` was an image url, and in an href the script
    # evaluated is `alert(1)/x.png`: the alert runs and the division is nonsense
    # nobody sees. `admin/edit_community.html:35` puts `header_image()` in a bare
    # href, and `icon_image()`/`header_image()` return `source_url` unchanged.
    #
    # A BLOCKLIST here rather than the http(s) allowlist the ingest paths use,
    # because the callers legitimately pass values that are not http urls at all:
    # `process_upload` returns `app/static/media/...` for a web upload when S3 is
    # off, and an allowlist would refuse every uploaded community icon. A path
    # with no scheme has no scheme to block, so it still passes. The API's own
    # icon and banner fields get the allowlist separately, at their boundary.
    # UNSAFE_URL_SCHEMES, the href set, which is UNSAFE_SRC_SCHEMES plus `data:`.
    # `data:` belongs here even though an <img src="data:image/png;..."> is
    # harmless: what this predicate gates is storing the string as
    # `File.source_url`, and `make_image_sizes` cannot fetch a data: url, so one
    # was never a working image for this codebase.
    if has_unsafe_url_scheme(url):
        return False
    mime_type = mime_type_using_head(url)
    if mime_type:
        mime_type_parts = mime_type.split('/')
        return f'.{mime_type_parts[1]}' in common_image_extensions
    else:
        try:
            parsed_url = urlparse(url)
        except ValueError:
            # Malformed netloc (unbalanced IPv6 bracket, bad IPv6 literal, or a
            # host urllib rejects under NFKC normalization). No path to inspect,
            # so not an image -- the same answer this gives for a path with no
            # image extension.
            return False
        path = parsed_url.path.lower()
        return any(path.endswith(extension) for extension in common_image_extensions)


def is_local_image_url(url):
    if not is_image_url(url):
        return False
    f = furl(url)
    return f.host in ["127.0.0.1", current_app.config["SERVER_NAME"], current_app.config['S3_PUBLIC_URL']]


def is_video_url(url: str) -> bool:
    common_video_extensions = ['.mp4', '.webm', '.mov']
    if not url:
        # See is_image_url above for why this is a conditional and not a wider
        # `except`. This is the site the guard's absence actually reached
        # users: app/post/routes.py's post_edit calls is_video_url(post.url)
        # while building the form, BEFORE its ownership check, so any logged-in
        # reader of a VIDEO post with a NULL url got a 500 where the route owed
        # them a 401. tests/test_post_edit_null_url.py drives that route.
        return False
    try:
        parsed_url = urlparse(url)
    except ValueError:
        # Malformed netloc (unbalanced IPv6 bracket, bad IPv6 literal, or a
        # host urllib rejects under NFKC normalization). No path to inspect, so
        # not a video -- the same answer this gives for a path with no video
        # extension.
        return False
    # D1409, the twin of D1408 one function up: an extension sniffed off
    # `urlparse(url).path` is the attacker's to choose, because for a
    # `javascript:` url the whole string after the colon IS the path.
    # `is_video_url('javascript:x/y.mp4')` was True.
    #
    # Smaller reach than the image case and fixed for the same reason
    # `livescript` and `mocha` are in UNSAFE_URL_SCHEMES: nothing currently
    # stores a url on this answer -- callers use it to set POST_TYPE_VIDEO, and
    # `Post.url` can no longer hold one of these schemes (D1404, D1407) -- so
    # this closes the shape rather than a live hole, and it means the two
    # predicates cannot drift apart again.
    if has_unsafe_url_scheme(url):
        return False
    path = parsed_url.path.lower()
    return any(path.endswith(extension) for extension in common_video_extensions)


def is_video_hosting_site(url: str) -> bool:
    if url is None or url == '':
        return False
    video_hosting_sites = ['https://youtube.com', 'https://www.youtube.com', 'https://youtu.be',
                           'https://www.vimeo.com', 'https://vimeo.com', 'https://streamable.com',
                           'https://www.redgifs.com/watch/']
    for starts_with in video_hosting_sites:
        if url.startswith(starts_with):
            return True

    # D1409. Every other entry above is an anchored `https://` prefix; this one is
    # an unbounded substring test, so ANY url carrying those eleven characters
    # anywhere is "a video hosting site" -- `javascript:videos/watch` included.
    # `_post_full.html:195` and `post_teaser/_macros.html:403` gate a PeerTube
    # iframe on the same substring and feed it `Post.peertube_embed()`
    # (`self.url.replace('watch', 'embed')`), so the template shares the shape.
    #
    # Requiring an http(s) scheme is the narrow fix: it leaves every real
    # PeerTube url matching (they are all https) and stops the class, without
    # guessing at a path-segment anchor that some peer's url shape might not
    # match. Same argument as the D1402 validators: the check is a boundary, so
    # it should be exact about what it admits.
    if url.lower().startswith(('http://', 'https://')) and 'videos/watch' in url:  # PeerTube
        return True

    return False


@cache.memoize(timeout=10)
def mime_type_using_head(url):
    # Find the mime type of a url by doing a HEAD request - this is the same as GET except only the HTTP headers are transferred
    try:
        response = httpx_client.head(url, timeout=5)
        response.raise_for_status()  # Raise an exception for HTTP errors
        content_type = response.headers.get('Content-Type')
        if content_type:
            if content_type == 'application/octet-stream':
                return ''
            return content_type
        else:
            return ''
    except (httpx.HTTPError, httpx.InvalidURL):
        # httpx.InvalidURL is NOT an httpx.HTTPError -- it descends straight
        # from Exception -- and httpx raises it while BUILDING the request, so
        # no transport-level handler ever sees it. A submitted post URL like
        # 'https://[::1/x' or 'http://exa℀mple.com/' lands here, and before
        # this clause it escaped into is_image_url and 500'd post creation.
        # '' is the same "no Content-Type could be determined" answer the
        # HTTPError case returns, which sends the caller to extension sniffing.
        return ''


allowed_tags = ['p', 'strong', 'a', 'ul', 'ol', 'li', 'em', 'blockquote', 'cite', 'br', 'h1', 'h2', 'h3', 'h4', 'h5',
                'h6', 'pre', 'div', 'video', 'source',
                'code', 'img', 'details', 'summary', 'table', 'tr', 'td', 'th', 'tbody', 'thead', 'hr', 'span', 'small',
                'sub', 'sup',
                's', 'tg-spoiler', 'ruby', 'rt', 'rp']


LINK_PATTERN = re.compile(
    r"""
        \b
        (
            (?:https?://|(?<!//)www\.)    # prefix - https:// or www.
            \w[\w_\-]*(?:\.\w[\w_\-]*)*   # host
            [^<>\s\"']*                   # rest of url
            (?<![?!.,:*_~;])(?<!\)\))     # exclude trailing punctuation
            (?=[?!.,:*_~);]?(?:[<\s]|$))  # make sure that we're not followed by " or ', i.e. we're outside of href="...".
        )
    """,
    re.X
)

PERSON_PATTERN = re.compile(r"(?<![\/])@([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b")
COMMUNITY_PATTERN = re.compile(r"(?<![\/])!([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b")
FEED_PATTERN = re.compile(r"(?<![\/])~([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b")

# The WHATWG URL Standard's basic URL parser normalises a URL BEFORE it reads
# the scheme (https://url.spec.whatwg.org/#concept-basic-url-parser):
#
#   step 1 - "remove any leading and trailing C0 control or space from input";
#            a C0 control or space is U+0000 to U+001F, or U+0020.
#   step 2 - "remove all ASCII tab or newline from input", from anywhere in it;
#            an ASCII tab or newline is U+0009, U+000A or U+000D.
#
# So a browser resolves href=" jav<TAB>ascript:alert(1)" to javascript:alert(1)
# and runs it on click. Deciding the scheme from the raw attribute value - which
# is what furl(href).scheme does, since furl performs none of this - misses every
# padded spelling. These two constants are that normalisation, and nothing else
# in this module may decide a scheme without applying them first.
_URL_LEADING_TRAILING_STRIP = ''.join(chr(c) for c in range(0x21))
_URL_TAB_OR_NEWLINE = str.maketrans('', '', '\t\n\r')

# WHATWG URL Standard, scheme start state and scheme state: a scheme is an ASCII
# alpha followed by any number of ASCII alphanumerics, '+', '-' and '.', ending
# at the first ':'. It is ASCII-lowercased before it is compared to anything,
# which is why the comparison below is case-insensitive.
_URL_SCHEME_PATTERN = re.compile(r'\A([A-Za-z][A-Za-z0-9+.\-]*):')

# Schemes an anchor's href may never carry out of sanitisation.
#
# THIS IS A BLOCKLIST, AND A BLOCKLIST IS THE WEAKER DESIGN. Its whole failure
# mode is the entry nobody thought of: 'livescript' and 'mocha' were missing
# until they were noticed in review, and there is no argument that the set is
# now complete - only that nothing else is currently known to be missing. An
# ALLOWLIST of known-safe schemes (http, https, mailto, and whatever else is
# genuinely needed) would be strictly stronger, because an unknown scheme would
# then be refused rather than admitted.
#
# It was not changed to an allowlist here, and the reason is scope rather than
# preference: allowlisting means auditing every scheme that legitimately appears
# in federated content across the fediverse - matrix:, xmpp:, gemini:, magnet:,
# ipfs:, tel:, ftp:, and the long tail of app-specific schemes remote software
# emits - and getting that audit wrong silently destroys legitimate links in
# every remote post, comment and profile that carries one. That audit is a
# separate piece of work with its own evidence and its own review. Until it
# happens this stays a blocklist, documented as one.
#
# What is in it and why:
#
# 'javascript' and 'vbscript' execute script in the current document's origin.
# 'livescript' (Netscape 4) and 'mocha' (Netscape 2/3) are the two legacy
# spellings of the same thing; no current browser executes either, so they are
# not a live hole - they are here because the set is a blocklist and they belong
# in it.
# 'data' lets an attacker supply the whole document (data:text/html,... or
# data:image/svg+xml,...); current browsers block top-level navigation to a
# data: URL, so blocking it here is defence in depth rather than a live hole,
# but PieFed has no reason to carry a data: URL in a link.
UNSAFE_URL_SCHEMES = frozenset({'javascript', 'vbscript', 'livescript', 'mocha', 'data'})

# Schemes a `src` may never carry out of sanitisation.
#
# The same set MINUS 'data', because href and src are not the same control:
#
# * A data: href is a whole document the attacker wrote, reached by top-level
#   navigation. Blocked.
# * A data: src is decoded as an image, or as media for video/source. Browsers
#   render an <img> in a NON-SCRIPTED context, so even data:image/svg+xml cannot
#   run script through it, and data:text/html in an <img> is not a document at
#   all - it is a decode failure. Against that inertness stands a real cost:
#   inline data: images are an ordinary way to embed a small image, and
#   allowlist_html is the sanitisation boundary for every federated post,
#   comment, profile field and community description, so blanking them would
#   silently destroy legitimate remote content. PieFed already takes this
#   position in sanitize_svg_bytes, which passes keep_data_url_mime_types for
#   image/jpeg|png|gif|webp|avif. So: permitted.
#
# Derived from UNSAFE_URL_SCHEMES rather than written out again, so a scheme
# added above is blocked in both places by default and only the deliberate
# exception needs stating.
UNSAFE_SRC_SCHEMES = UNSAFE_URL_SCHEMES - {'data'}


def url_scheme(url: str) -> str:
    """The scheme a browser would read from `url`, lowercased, or '' if it has none.

    Applies the WHATWG normalisation described above before looking, so
    url_scheme(' jav\\tascript:alert(1)') is 'javascript'.
    """
    normalized = url.strip(_URL_LEADING_TRAILING_STRIP).translate(_URL_TAB_OR_NEWLINE)
    match = _URL_SCHEME_PATTERN.match(normalized)
    return match.group(1).lower() if match else ''


def has_unsafe_url_scheme(url: str, unsafe_schemes: frozenset = UNSAFE_URL_SCHEMES) -> bool:
    """True if `url` names a scheme that must not survive sanitisation.

    The comparison is against a whole scheme, never a prefix: 'javascriptic:'
    and a path segment spelt 'javascript:' inside an https URL are both safe and
    must keep working.

    `unsafe_schemes` selects WHICH set to compare against; it does not change
    how the URL is read. There is deliberately one implementation of the
    normalisation and one implementation of the comparison, with href and src
    differing only in the set they pass (see UNSAFE_SRC_SCHEMES). Two
    implementations of one control is exactly what produced the back()/referrer()
    divergence this branch had to unpick.
    """
    return url_scheme(url) in unsafe_schemes


def url_is_storable(url) -> bool:
    """Whether a peer's url may be stored in `Post.url` at all.

    D1404. `Post.url` is rendered as a bare `href` in ten templates --
    `post/_post_full.html:8`, five sites in `post/post_teaser/_macros.html`, four in
    `post/_post_teaser_masonry.html`, two of which also make it an `img src` -- and
    `post_to_page` (app/activitypub/util.py:174) federates it back out again as a Link
    attachment. The only guard between a peer's attachment url and all of that was
    `url_is_parseable`, whose docstring says outright that it checks no scheme, so

        {"attachment": [{"type": "Link", "href": "javascript:alert(document.domain)"}]}

    was stored verbatim and clicked from the post page. Measured for all six attachment
    shapes this codebase reads (Lemmy's `href`, NodeBB's `url`, Mastodon's Document,
    WordPress's Audio, PixelFed's Image, a.gup.pe's dict) and through
    `update_post_from_activity`, which replaced an already-stored `https://` url with it.

    THE SET IS THE HREF BLOCKLIST, NOT AN http(s) ALLOWLIST, and the reason is the one
    recorded at UNSAFE_URL_SCHEMES: this is a url REMOTE software chose, and allowlisting
    means auditing every scheme that legitimately appears in a link post across the
    fediverse. All four local producers of `Post.url` do require `^https?://`
    (`CreateLinkForm.link_url`, `CreateVideoForm.video_url`, and the two Event fields),
    so the allowlist is defensible here and would be strictly stronger -- but taking it
    needs evidence about what peers actually send, and getting it wrong silently drops
    real links from every remote instance. The blocklist closes the hole with no such
    risk. Upgrading it later is a separate piece of work with its own evidence.

    Parseability is tested first and kept: it is what stops `domain_from_url` returning
    None into a `.banned` dereference at three of the four call sites, and the scheme
    test is about a different failure.
    """
    return url_is_parseable(url) and not has_unsafe_url_scheme(url)


def url_host(url: str) -> Optional[str]:
    """The host furl reads from `url`, or None when furl refuses to parse it.

    furl reports every parse refusal by raising ValueError, and it refuses more
    than IPv6 literals: an unterminated bracket ('http://['), a bracketed value
    that is not an address ('http://[zzz]'), a port that is not a number
    ('http://example.com:notaport/') and a host holding characters it will not
    accept ('http://%zz/') are four separate refusals, all ValueError.

    allowlist_html called furl unguarded, purely to read .host for the
    instance_domains comparison, so a single anchor with an unparseable
    authority raised out of the sanitisation boundary and took the whole
    federated document with it. A URL whose host cannot be parsed has no host,
    so it cannot be one of our instances: None is the honest answer and it is
    the same answer furl already gives for a URL with no authority at all
    (mailto:, a bare path), which the caller already handles.

    Only ValueError is caught, deliberately narrowly: anything else furl raises
    is a new defect and should surface with its own traceback rather than be
    swallowed here. That covers every refusal found by probing furl with 200,000
    random short strings plus targeted malformed URLs. The one exotic spelling
    that turned up, UnicodeEncodeError from the idna codec on a lone surrogate
    in the host: a lone-surrogate escape in remote JSON decodes to exactly
    such a string. It is caught too, because UnicodeEncodeError is itself a
    subclass of ValueError.
    """
    try:
        return furl(url).host
    except ValueError:
        return None


# sanitise HTML using an allow list
def allowlist_html(html: str, a_target='_blank', test_env=False) -> str:
    # RUN THE TESTS in tests/test_allowlist_html.py whenever you alter this function, it's fragile and bugs are hard to spot.
    if html is None or html == '':
        return ''

    # Produce a short, random string that is used for footnotes
    if test_env:
        fn_string = test_env.get('fn_string', 'fn-test')
    else:
        fn_string = gibberish(6)

    code_placeholder = gibberish(10)
    link_placeholder = gibberish(10)

    # substitute out the <code> snippets so that they don't inadvertently get formatted
    code_snippets, clean_html = stash_code_html(html, code_placeholder)

    # avoid returning empty anchors
    re_empty_anchor = re.compile(r'<a href="(.*?)" rel="nofollow ugc" target="_blank"><\/a>')
    clean_html = re_empty_anchor.sub(r'<a href="\1" rel="nofollow ugc" target="_blank">\1</a>', clean_html)

    # replace lemmy's spoiler markdown left in HTML
    clean_html = clean_html.replace('<h2>:::</h2>',
                                    '<p>:::</p>')  # this is needed for lemmy.world/c/hardware's sidebar, for some reason.
    re_spoiler = re.compile(r':{3}\s*?spoiler\s+?(\S.+?)(?:\n|</p>)(.+?)(?:\n|<p>):{3}', re.S)
    clean_html = re_spoiler.sub(r'<details><summary>\1</summary><div class="spoiler_block"><p>\2</p></div></details>', clean_html)

    # replace strikethough markdown left in HTML
    re_strikethough = re.compile(r'~~(.*)~~')
    clean_html = re_strikethough.sub(r'<s>\1</s>', clean_html)

    # replace subscript markdown left in HTML, don't break links that have a ~ in them
    re_subscript = re.compile(r'~([^~\r\n\t\f\v ]+)~')
    link_snippets, clean_html = stash_link_html(clean_html, link_placeholder)
    clean_html = re_subscript.sub(r'<sub>\1</sub>', clean_html)
    clean_html = pop_link(link_snippets, clean_html, link_placeholder)

    # replace superscript markdown left in HTML
    re_superscript = re.compile(r'\^([^\^\r\n\t\f\v ]+)\^')
    clean_html = re_superscript.sub(r'<sup>\1</sup>', clean_html)

    # replace <img src> for mp4 with <video> - treat them like a GIF (autoplay, but initially muted)
    re_embedded_mp4 = re.compile(r'<img .*?src="(https://.*?\.mp4)".*?/>')
    clean_html = re_embedded_mp4.sub(
        r'<video class="responsive-video" controls preload="auto" autoplay muted loop playsinline disablepictureinpicture><source src="\1" type="video/mp4"></video>',
        clean_html)

    # replace <img src> for webm with <video> - treat them like a GIF (autoplay, but initially muted)
    re_embedded_webm = re.compile(r'<img .*?src="(https://.*?\.webm)".*?/>')
    clean_html = re_embedded_webm.sub(
        r'<video class="responsive-video" controls preload="auto" autoplay muted loop playsinline disablepictureinpicture><source src="\1" type="video/webm"></video>',
        clean_html)

    # replace <img src> for mp3 with <audio>
    re_embedded_mp3 = re.compile(r'<img .*?src="(https://.*?\.mp3)".*?/>')
    clean_html = re_embedded_mp3.sub(r'<audio controls><source src="\1" type="audio/mp3"></audio>', clean_html)

    # replace the 'static' for images hotlinked to fandom sites with 'vignette'
    re_fandom_hotlink = re.compile(r'<img alt="(.*?)" loading="lazy" src="https://static.wikia.nocookie.net')
    clean_html = re_fandom_hotlink.sub(r'<img alt="\1" loading="lazy" src="https://vignette.wikia.nocookie.net',
                                       clean_html)

    # replace ruby markdown like {漢字|かんじ}
    re_ruby = re.compile(r'\{(.+?)\|(.+?)\}')
    clean_html = re_ruby.sub(r'<ruby>\1<rp>(</rp><rt>\2</rt><rp>)</rp></ruby>', clean_html)

    # replace :emoji: with images
    emoji_replacements = get_emoji_replacements() if test_env is False else None
    if emoji_replacements:
        pattern = re.compile(
            "|".join(re.escape(k) for k in emoji_replacements),
            re.IGNORECASE
        )

        clean_html = pattern.sub(
            lambda m: "<img referrerpolicy='no-referrer' width=30 height=30 src='" + emoji_replacements[m.group(0).lower()] + "'>",
            clean_html
        )

    # bring back the <code> snippets
    clean_html = pop_code(code_snippets, clean_html, code_placeholder)

    # Pre-escape angle brackets that aren't valid HTML tags before BeautifulSoup parsing
    # We need to distinguish between:
    # 1. Valid HTML tags (allowed or disallowed) - let BeautifulSoup handle them
    # 2. Invalid/non-HTML content in angle brackets - escape them
    def escape_non_html_brackets(match):
        tag_content = match.group(1).strip().lower()
        if tag_content == '':
            return f"&lt;{match.group(1)}&gt;"
        # Handle closing tags by removing the leading slash before extracting tag name
        if tag_content.startswith('/'):
            words = tag_content[1:].split()
        elif tag_content.endswith('/'):
            words = tag_content[:-1].split()
        else:
            words = tag_content.split()
        # words is empty when the brackets name no tag at all: '</>' strips to
        # '', and ''.split() is []. Taking words[0] there raised IndexError, so
        # a three-character string in any federated post, comment or profile was
        # an unhandled 500 on the ingest path. An empty tag name matches nothing
        # in html_tags below, so the content is escaped as text - the same
        # treatment the tag_content == '' guard above gives '<>'.
        tag_name = words[0] if words else ''

        # Check if this looks like a valid HTML tag (allowed or not)
        # Valid HTML tags have specific patterns
        html_tags = ['a', 'abbr', 'acronym', 'address', 'area', 'article', 'aside', 'audio', 'b', 'bdi', 'bdo', 'big',
                     'blockquote', 'body', 'br', 'button', 'canvas', 'caption', 'center', 'cite', 'code', 'col',
                     'colgroup', 'data', 'datalist', 'dd', 'del', 'details', 'dfn', 'dialog', 'dir', 'div', 'dl', 'dt',
                     'em', 'embed', 'fieldset', 'figcaption', 'figure', 'font', 'footer', 'form', 'frame', 'frameset',
                     'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'head', 'header', 'hr', 'html', 'i', 'iframe', 'img', 'input',
                     'ins', 'kbd', 'label', 'legend', 'li', 'link', 'main', 'map', 'mark', 'meta', 'meter', 'nav',
                     'noframes', 'noscript', 'object', 'ol', 'optgroup', 'option', 'output', 'p', 'param', 'picture',
                     'pre', 'progress', 'q', 'rp', 'rt', 'ruby', 's', 'samp', 'script', 'section', 'select', 'small',
                     'source', 'span', 'strike', 'strong', 'style', 'sub', 'summary', 'sup', 'svg', 'table', 'tbody',
                     'tg-spoiler', 'td', 'template', 'textarea', 'tfoot', 'th', 'thead', 'time', 'title', 'tr', 'track',
                     'tt', 'u', 'ul', 'var', 'video', 'wbr']

        if tag_name in html_tags:
            # This is a valid HTML tag - let BeautifulSoup handle it (it will remove if not allowed)
            return match.group(0)
        else:
            # This doesn't look like a valid HTML tag - escape it
            return f"&lt;{match.group(1)}&gt;"

    html = re.sub(r'<([^<>]+?)>', escape_non_html_brackets, clean_html)

    # Parse the HTML using BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    if not test_env:
        instance_domains = fediverse_domains()
    else:
        instance_domains = []

    # Filter tags, leaving only safe ones
    for tag in soup.find_all():
        # If the tag is not in the allowed_tags list, remove it and its contents
        if tag.name not in allowed_tags:
            tag.extract()
        else:
            # Filter and sanitize attributes
            allowed_attrs = ['href', 'src', 'alt', 'class', 'id']
            # Add image-specific attributes for enhanced-images markdown extra
            if tag.name == 'img':
                allowed_attrs.extend(['width', 'height', 'align', 'title', 'data-enhanced-img'])

            if tag.name == 'video':
                allowed_attrs.extend(['controls', 'loop', 'preload', 'autoplay', 'muted', 'playsinline',
                                      'disablepictureinpicture'])

            if tag.name == 'source':
                allowed_attrs.extend(['type'])

            for attr in list(tag.attrs):
                if attr not in allowed_attrs:
                    del tag[attr]
            # Scheme-check `src`, the same way `href` is checked below.
            #
            # `src` is on allowed_attrs for EVERY element and used to be
            # scheme-checked nowhere, so <img src="javascript:alert(1)">
            # survived sanitisation verbatim - safe only because no current
            # browser executes it, which is an argument about browsers rather
            # than about filtering. allowed_tags carries no iframe/object/embed/
            # svg, so src is loadable on img, video and source and nowhere else,
            # but the check is applied wherever the attribute survives rather
            # than to a hardcoded list of three: the attribute filter above is
            # what decides where src can appear, and this should not have to be
            # kept in step with it by hand.
            #
            # Same helper and same normalisation as the href path, differing
            # only in the scheme set: UNSAFE_SRC_SCHEMES permits `data:`, which
            # href blocks. The reasoning for that difference is at
            # UNSAFE_SRC_SCHEMES.
            #
            # A rejected src is blanked, matching what the href path does with a
            # rejected href: the element stays, its URL goes nowhere. An
            # accepted src is kept exactly as it arrived - the normalisation
            # decides, it does not rewrite.
            if tag.attrs.get('src') is not None and has_unsafe_url_scheme(tag['src'], UNSAFE_SRC_SCHEMES):
                tag['src'] = ''
            # Remove some mastodon guff - spans with class "invisible"
            if tag.name == 'span' and 'class' in tag.attrs and 'invisible' in tag.attrs['class']:
                tag.extract()
            # Add nofollow and target=_blank to anchors
            if tag.name == 'a' and tag.attrs.get('href') is not None:
                if not tag.attrs.get('href', "").startswith("#"):
                    tag.attrs['rel'] = 'nofollow ugc'
                    tag.attrs['target'] = a_target
                    # The scheme test comes FIRST, and is has_unsafe_url_scheme
                    # rather than furl(...).scheme: furl does not perform the
                    # WHATWG normalisation a browser performs before reading a
                    # scheme, so it saw no javascript scheme in
                    # href=" javascript:alert(1)" or href="java<LF>script:..."
                    # and left the attribute intact - stored XSS on every piece
                    # of federated content. Testing the scheme before the host
                    # also means a hostile href can never reach rewrite_href.
                    #
                    # A rejected href is blanked, which is what this function
                    # already did for an unpadded javascript: URL. An ACCEPTED
                    # href is kept exactly as it arrived: the normalisation
                    # above decides, it does not rewrite. Every browser applies
                    # the same normalisation itself when it follows the link, so
                    # rewriting would change stored content for no gain.
                    #
                    # `src` is checked too, a few lines above, against
                    # UNSAFE_SRC_SCHEMES rather than this set - same helper,
                    # same normalisation, one scheme's difference.
                    if has_unsafe_url_scheme(tag['href']):
                        tag['href'] = ''
                    elif url_host(tag['href']) in instance_domains:
                        # url_host, not furl(...).host: furl raises ValueError on
                        # a host it cannot parse, and an unguarded call here made
                        # one malformed anchor fail the whole document to render.
                        tag['href'] = rewrite_href(tag['href'])
                else:
                    # This is a same-page anchor - a footnote, give unique suffix for href
                    tag.attrs['href'] = tag.attrs.get('href', '') + '-' + fn_string
            # Add unique suffix for footnote id's
            if 'class' in tag.attrs and 'footnote-ref' in tag.attrs.get('class'):
                tag.attrs['id'] = tag.attrs.get('id', '') + '-' + fn_string
            if tag.name == 'li' and tag.attrs.get('id', '').startswith('fn-'):
                tag.attrs['id'] = tag.attrs.get('id', '') + '-' + fn_string
            # Add loading=lazy to images
            if tag.name == 'img':
                tag.attrs['loading'] = 'lazy'
            if tag.name == 'table':
                tag.attrs['class'] = 'table'

    return str(soup)


def escape_non_html_angle_brackets(text: str) -> str:
    placeholder = gibberish(10)
    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_md(text, placeholder)

    # Step 2: Escape <...> unless they look like valid HTML tags
    def escape_tag(match):
        tag_content = match.group(1).strip().lower()
        # Handle closing tags by removing the leading slash before extracting tag name
        words = tag_content[1:].split() if tag_content.startswith('/') else tag_content.split()
        # Same defect as escape_non_html_brackets, plus one more case: this
        # function has no empty-content guard, so '< >' raised IndexError here
        # as well as '</>'. An empty tag name is in neither allowed_tags nor
        # emoticons and matches no LINK_PATTERN, so the content is escaped as
        # text, which is what it is.
        tag_name = words[0] if words else ''
        emoticons = ['3', # heart
                     '\\3', # broken heart
                     '|:‑)', # santa claus *<|:‑)
                     ':‑|' # dumb, dunce-like
                     ]
        if tag_name in allowed_tags or re.match(LINK_PATTERN, tag_content) or tag_content in emoticons:
            return match.group(0)
        else:
            return f"&lt;{match.group(1)}&gt;"

    bracket_regex = re.compile(r'<([^<>\n]+?)>', re.M)
    text = re.sub(bracket_regex, escape_tag, text)

    # Step 3: Restore code blocks
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text

def handle_bold_em(text: str) -> str:
    """
    Handles properly assigning <strong> tags to **bolded** words in markdown even if there are **two** of them in the
    same sentence.
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_md(text, placeholder)

    # Step 2: Wrap **bold** sections with <strong></strong>
    # First, sub any that are both italics and bold
    re_em_bold = re.compile(r"(\*\*\*|___)(?=\S)(.+?)(?<=\S)\1", re.S | re.X)
    text = re_em_bold.sub(r"<em><strong>\2</strong></em>", text)

    # Second, sub any that are just bold
    re_bold = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S | re.X)
    text = re_bold.sub(r"<strong>\2</strong>", text)

    # Step 3: Restore code blocks
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def escape_img(raw_html: str) -> str:
    """Prevents embedding images for places where an image would break formatting."""

    re_img = re.compile(r"<img.+?>")
    raw_html = re_img.sub(r"<code><image placeholder></code>", raw_html)

    return raw_html


def handle_lemmy_autocomplete(text: str) -> str:
    """
    Handles markdown formatted links that are in the format that lemmy autocompletes users/communities to and replaces
    them with instance-agnostic links.

    Lemmy autocomplete format:
        [!news@lemmy.world](https://lemmy.world/c/news)
    Convert this to:
        !news@lemmy.world

    ...which will be later converted to an instance-local link
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_md(text, placeholder)

    # Step 2: ID all the markdown-formatted links and check the part in [] if it matches comm/person/feed formats
    def sub_non_formatted_actor(match):
        bracket_part = match.group(1)
        if re.match(COMMUNITY_PATTERN, bracket_part):
            return bracket_part
        elif re.match(PERSON_PATTERN, bracket_part):
            return bracket_part
        elif re.match(FEED_PATTERN, bracket_part):
            return bracket_part
        return match.group(0)

    re_link = re.compile(r"\[((!|@|~).*?)\]\(.*?\)")

    text = re.sub(re_link, sub_non_formatted_actor, text)

    # Step 3: Restore code blocks
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def handle_lemmy_spoilers(text: str) -> str:
    """
    Handles the block spoiler syntax inherited from lemmy
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_html(text, placeholder)

    # Step 2: Regex stuff
    spoiler_opening = re.compile(r'^<p>:{3}\sspoiler\s+?(\S.+?)?</p>$', re.M)
    spoiler_closing = re.compile(r'^<p>:{3}\s*?</p>$', re.M)

    # Step 3: Count the number of openings and closings so that we know we are closing each html tag we open
    num_openings = re.findall(spoiler_opening, text)
    num_closings = re.findall(spoiler_closing, text)

    # Step 4: If the number of openings and closings match, make the html substitutions
    # If they don't match, then process the block spoilers in allowlist_html instead (can't nest spoilers, more quirks)
    if len(num_openings) == len(num_closings):
        text = spoiler_opening.sub(r'<details><summary>\1</summary><div class="spoiler_block symmetric">', text)
        text = spoiler_closing.sub(r'</div></details>', text)

    # Step 5: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def handle_naked_spoilers(text: str) -> str:
    """
    Makes lemmy spoiler blocks without a summary block actually work
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_md(text, placeholder)

    # Step 2: Regex stuff
    empty_spoilers = re.compile(r'^:{3}\sspoiler[ ]*?$', re.M)
    text = empty_spoilers.sub('::: spoiler Spoiler', text)

    # Step 3: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def handle_spoiler_spacing(text: str) -> str:
    """
    Inserts a blank line in the markdown after a spoiler block opening (see #1612)
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_md(text, placeholder)

    # Step 2: Regex stuff
    spoiler_opening = re.compile(r'^:{3}\sspoiler\s+?(\S.+?)?$', re.M)
    spoiler_closing = re.compile(r'^:{3}\s*?$', re.M)
    text = spoiler_opening.sub(r'::: spoiler \1\n', text)
    text = spoiler_closing.sub(r'\n:::\n', text)

    # Step 3: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def handle_video_embeds(text: str) -> str:
    """
    Takes markdown video embedding format and turns it into html.
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_md(text, placeholder)

    # Step 2: Function to handle matched regex groups
    def sub_video_markdown(match):
        alt_text = match.group(1)
        link = match.group(2)

        if is_video_url(link):
            output = ('<video class="responsive-video" muted controls loop ' +
                      'playsinline disablepictureinpicture" preload="metadata">')
            download_text = _('You can download a copy of the file instead.')
            if link.endswith('.webm'):
                output += f'<source type="video/webm" src="{link}"> '
            elif link.endswith('.mp4') or link.endswith('.mov'):
                output += f'<source type="video/mp4" src="{link}"> '

            output += _('Your browser does not support playing HTML5 video.')
            output += " " + f'<a href="{link}" download>' + download_text + '</a>'
            output += " " + _("Here is a description of the content: %s" % alt_text)
            output += '</video>'

            return output
        else:
            # return things unchanged (probably an image link)
            return match.group(0)

    # Step 3: Do the regex matching and substitutions
    img_md = re.compile(r'!\[(.*?)\]\((\S*?)\)', re.M)
    text = re.sub(img_md, sub_video_markdown, text)

    # Step 4: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def make_quotes_straight(text: str) -> str:
    """
    Don't do the stylized opening and ending quotation marks as part of the smartypants extra inside angle brackets.
    It breaks too many things.
    """

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_html(text, placeholder)

    # Step 2: function to do replacements
    def replace_quotes_in_brackets(match):
        left_dquot = match.group(0).replace('&#8220;', '"')
        right_dquot = left_dquot.replace('&#8221;', '"')
        left_squot = right_dquot.replace("&#8216;", "'")
        right_squot = left_squot.replace("&#8217;", "'")

        return right_squot

    # Step 3: regex stuff
    potential_html = re.compile(r'<([^<>\n]+?)>', re.M)
    text = re.sub(potential_html, replace_quotes_in_brackets, text)

    # Step 4: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def handle_reddit_spoilers(text: str) -> str:
    """Handle reddit-style in-line spoilers >! like this !<"""

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_html(text, placeholder)

    # Step 2: Do the regex matching and substitutions
    img_md = re.compile(r'>!\s?(.+?)\s?!<', re.M)
    text = img_md.sub(r'<tg-spoiler>\1</tg-spoiler>', text)

    # Step 3: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def handle_blockquotes(text: str) -> str:
    """Handle blockquotes so that special syntax is handled correctly (spoiler blocks, etc.)"""

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_html(text, placeholder)

    # Step 2: Regex to capture all groups of lines preceded by > (roughly based on markdown2 regex)
    md_quotes = re.compile(r'((^[ \t]*>[ \t]?.*(\n|$))+)', re.M)

    # Step 3: Function to do replacements
    def wrap_blockquotes(match):
        contents = match.group(1)

        # Add attribute so that markdown2 later knows to format the contents of this html tag
        output = '<blockquote markdown="1">'

        # Remove one layer of > from the contents since we are directly appending html around it
        strip_leading_angle = re.compile(r'(^[ \t]*>[ \t]?)', re.M)
        contents = strip_leading_angle.sub(r'', contents)
        output += contents + "</blockquote>\n"

        # Recursively handle blockquotes to deal with more layers of quoting
        if re.search(strip_leading_angle, contents):
            output = handle_blockquotes(output)

        return output

    # Step 4: Do the regex substitution work
    text = re.sub(md_quotes, wrap_blockquotes, text)

    # Step 5: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


def links_with_parens(text: str) -> str:
    """Try to fix links that have trailing parentheses"""

    soup = BeautifulSoup(text, 'html.parser')

    for link in soup.find_all("a"):
        target_url = link.get("href")
        contents = link.text
        following_text = link.next_sibling

        num_left_paren = target_url.count("(")
        num_right_paren = target_url.count(")")

        if following_text and isinstance(following_text, NavigableString) and following_text.startswith(")"):
            if num_left_paren - num_right_paren == 1:
                # Link dropped the trailing paren, add it back and remove it from trailing text
                link['href'] = link['href'] + ")"
                following_text.replace_with(following_text[1:])
                link.string = contents + ")"
        elif target_url.endswith(")"):
            if num_right_paren - num_left_paren == 1:
                # Trailing paren added to link, strip it and add it to trailing text
                link['href'] = link['href'][:-1]
                link.string = contents[:-1]
                if not following_text or not isinstance(following_text, NavigableString):
                    link.insert_after(")")
                else:
                    following_text.replace_with(")" + following_text)

    better_html = str(soup)

    return better_html


def fix_www_links(text: str) -> str:
    """Add https:// to links that start with www."""

    soup = BeautifulSoup(text, 'html.parser')

    for link in soup.find_all("a"):
        target_url = link.get("href")
        if target_url and target_url.startswith("www."):
            # Add https:// to links that start with www. but don't have a protocol
            link['href'] = "https://" + target_url

    return str(soup)


def handle_better_lists(text: str) -> str:
    """Handles lists that don't have a blank line preceding them."""

    placeholder = gibberish(10)

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets, text = stash_code_html(text, placeholder)

    # Step 2: Split the whole entry into each newline
    text_list = text.splitlines()

    # Step 3: Define regex for beginning of line of each list type
    re_numbered_list = re.compile(r'^\d+\.\s+')
    re_bulleted_list = re.compile(r'^-\s+')

    # Step 4: Loop through the lines, inserting an extra entry where needed to help markdown processing
    new_text_list = []  # list that will store processed text, each item is a line of text
    prev_line = ""
    for line in text_list:
        if not prev_line:
            # First line in string
            prev_line = line
            new_text_list.append(line)
            continue

        # Check for bulleted lists preceded by hyphen and space
        if re.search(re_bulleted_list, line) and not re.search(re_bulleted_list, prev_line):
            # First line in a bulleted list, insert a blank line first to make it render correctly
            new_text_list.append("")

        # Check for numbered lists preceded by number(s), period, and then space
        if re.search(re_numbered_list, line) and not re.search(re_numbered_list, prev_line):
            # First line in a numbered list, insert a blank line first to make it render correctly
            new_text_list.append("")

        # End of iteration, add line to processed output and set new prev_line
        new_text_list.append(line)
        prev_line = line

    # Step 5: Join the lines into one string
    text = "\n".join(new_text_list)

    # Step 6: Restore code snippets
    text = pop_code(code_snippets=code_snippets, text=text, placeholder=placeholder)

    return text


# use this for Markdown irrespective of origin, as it can deal with both soft break newlines ('\n' used by PieFed) and hard break newlines ('  \n' or ' \\n')
# ' \\n' will create <br /><br /> instead of just <br />, but hopefully that's acceptable.
def markdown_to_html(markdown_text, anchors_new_tab=True, allow_img=True, a_target="_blank", test_env=False) -> str:
    if markdown_text:

        markdown_text = handle_reddit_spoilers(markdown_text)

        # Escape <...> if it’s not a real HTML tag
        markdown_text = escape_non_html_angle_brackets(
            markdown_text)  # To handle situations like https://ani.social/comment/9666667

        markdown_text = handle_blockquotes(markdown_text) # handle blockquotes ourselves to do it better in some cases
        markdown_text = handle_bold_em(markdown_text)  # Some preprocessing to better handle bold and italics
        markdown_text = handle_better_lists(markdown_text)  # preprocessing to handle lists not preceded by blank line
        markdown_text = handle_lemmy_autocomplete(markdown_text)
        markdown_text = handle_naked_spoilers(markdown_text)
        markdown_text = handle_spoiler_spacing(markdown_text)
        markdown_text = handle_video_embeds(markdown_text)

        try:
            md = markdown2.Markdown(extras={'middle-word-em': False,
                                            'tables': True,
                                            'fenced-code-blocks': None,
                                            'strike': True,
                                            'tg-spoiler': True,
                                            'link-patterns': [(LINK_PATTERN, r'\1')],
                                            'breaks': {'on_backslash': True},
                                            'tag-friendly': True,
                                            'smarty-pants': True,
                                            'enhanced-images': True,
                                            'footnotes': True,
                                            'markdown-in-html': True})
            raw_html = md.convert(markdown_text)
            # Apply enhanced image attributes after markdown processing
            raw_html = apply_enhanced_image_attributes(raw_html, md)
        except TypeError:
            # weird markdown, like https://mander.xyz/u/tty1 and https://feddit.uk/comment/16076443,
            # causes "markdown2.Markdown._color_with_pygments() argument after ** must be a mapping, not bool" error, so try again without fenced-code-blocks extra
            try:
                md = markdown2.Markdown(extras={'middle-word-em': False,
                                                'tables': True,
                                                'strike': True,
                                                'tg-spoiler': True,
                                                'link-patterns': [(LINK_PATTERN, r'\1')],
                                                'breaks': {'on_backslash': True},
                                                'tag-friendly': True,
                                                'smarty-pants': True,
                                                'enhanced-images': True,
                                                'footnotes': True})
                raw_html = md.convert(markdown_text)
                # Apply enhanced image attributes after markdown processing
                raw_html = apply_enhanced_image_attributes(raw_html, md)
            except:
                raw_html = ''

        if not allow_img:
            raw_html = escape_img(raw_html)

        raw_html = handle_lemmy_spoilers(raw_html)
        raw_html = make_quotes_straight(raw_html)
        raw_html = links_with_parens(raw_html)
        raw_html = fix_www_links(raw_html)

        return allowlist_html(raw_html, a_target=a_target if anchors_new_tab else '', test_env=test_env)
    else:
        return ''


# this function lets local users use the more intuitive soft-breaks for newlines, but actually stores the Markdown in Lemmy-compatible format
# Reasons for this:
# 1. it's what any adapted Lemmy apps using an API would expect
# 2. we've reverted to sending out Markdown in 'source' because:
#    a. Lemmy doesn't convert '<details><summary>' back into its '::: spoiler' format
#    b. anything coming from another PieFed instance would get reduced with html_to_text()
#    c. raw 'https' strings in code blocks are being converted into <a> links for HTML that Lemmy then converts back into []()
def piefed_markdown_to_lemmy_markdown(piefed_markdown: str):
    # only difference is newlines for soft breaks.
    re_breaks = re.compile(r'(\S)(\r\n)')
    lemmy_markdown = re_breaks.sub(r'\1  \2', piefed_markdown)
    return lemmy_markdown


def markdown_to_text(markdown_text) -> str:
    if not markdown_text or markdown_text == '':
        return ''
    return markdown_text.replace("# ", '')


def html_to_text(html) -> str:
    if html is None or html == '':
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    return soup.get_text()


@cache.memoize(timeout=5000)
def get_emoji_replacements():
    return {e.token: e.url for e in db.session.query(Emoji)}


def mastodon_extra_field_link(extra_field: str) -> str:
    """The href of the first anchor in a Mastodon PropertyValue field.

    Returns `extra_field` unchanged when the value carries no anchor with an
    href. A str in every path, so a caller may .strip() the result without
    checking it -- which is what app/activitypub/util.py:648 and :1157 do.

    Both of those call sites reach this function whenever the literal '<a '
    appears in a remote actor's attachment value, which is a substring test and
    not HTML validation. Two remote-controlled inputs used to get past it and
    crash:

      '<a class="x">link</a>'  -> KeyError('href') on tag['href'] here
      'text <a '               -> None here, then AttributeError on the
                                  caller's .strip()

    An anchor without an href does not hide a later one that has it: the first
    href found wins, which is what the callers want from a profile field.
    """
    soup = BeautifulSoup(extra_field, 'html.parser')
    for tag in soup.find_all('a'):
        href = tag.get('href')
        if href is not None:
            return href
    return extra_field


def boost_cache_entries(rows) -> list:
    """Shape (user_id, ap_id, display_name, created_at) rows into the post_boosts cache.

    Kept separate from the query so the stored JSON shape can be tested without
    a database. Input order is preserved; the caller chooses the ordering.
    """
    return [
        {
            'user_id': user_id,
            'ap_id': ap_id if ap_id else '',
            'display_name': display_name,
            'created_at': created_at.isoformat() if created_at else '',
        }
        for user_id, ap_id, display_name, created_at in rows
    ]


def microblog_content_to_title(html: str) -> Tuple[str, str]:
    title = ''
    link = ''
    if '<h1>' in html.lower():
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup.find_all('h1'):
            text = tag.get_text(separator=" ")
            if len(text) >= 5:
                title = text
                a = tag.find('a', href=True)
                if a:
                    link = a['href']
                break
    elif '<p>' in html:
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup.find_all('p'):
            title = tag.get_text(separator=" ")
            if title and title.strip() != '' and len(title.strip()) >= 5:
                break
    else:
        title = html_to_text(html)

    period_index = title.find('.')
    question_index = title.find('?')
    exclamation_index = title.find('!')

    # Find the earliest occurrence of either '.' or '?' or '!'
    end_index = min(period_index if period_index != -1 else float('inf'),
                    question_index if question_index != -1 else float('inf'),
                    exclamation_index if exclamation_index != -1 else float('inf'))

    # there's no recognised punctuation
    if end_index == float('inf'):
        if len(title) >= 10:
            title = title.replace(' @ ', '').replace(' # ', '')
            title = shorten_string(title, 197)
        else:
            title = '(content in post body)'
        return title.strip(), link

    if end_index != -1:
        if question_index != -1 and question_index == end_index:
            end_index += 1  # Add the ? back on
        if exclamation_index != -1 and exclamation_index == end_index:
            end_index += 1  # Add the ! back on
        title = title[:end_index]

    if len(title) > 150:
        for i in range(149, -1, -1):
            if title[i] == ' ':
                break
        title = title[:i] + ' ...' if i > 0 else ''

    return title.strip(), link


def microblog_content_to_link(html: str, exclude: str):
    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a"):
        if furl(link.get("href")).host != exclude:
            return link.get("href")
    return None


def first_paragraph(html):
    soup = BeautifulSoup(html, 'html.parser')
    first_para = soup.find('p')
    if first_para:
        if first_para.text.strip() == 'Summary' or \
                first_para.text.strip() == '*Summary*' or \
                first_para.text.strip() == 'Comments' or \
                first_para.text.lower().startswith('cross-posted from:'):
            second_paragraph = first_para.find_next('p')
            if second_paragraph:
                # D1381. This branch returned the same construction as the line
                # below WITHOUT allowlist_html, and the result is rendered
                # `{{ first_paragraph(post.body_html) | safe }}` in four post
                # teaser macros -- the feed listing.
                #
                # `.text` DECODES entities, so a body carrying
                # `&lt;img src=x onerror=alert(1)&gt;` -- which is exactly what
                # allowlist_html produces from an author who typed a literal
                # `<img ...>` -- came back out as live markup:
                #
                #     second.text = '<img src=x onerror=alert(1)>'
                #     returned    = '<p><img src=x onerror=alert(1)></p>'
                #
                # Reached by any author: the branch needs a first paragraph of
                # `Summary`, `*Summary*`, `Comments`, or one starting
                # `cross-posted from:`, all of which they write.
                return allowlist_html(f'<p>{second_paragraph.text}</p>')
        return allowlist_html(f'<p>{first_para.text}</p>')
    else:
        return ''


def community_link_to_href(link: str, server_name_override: str | None = None) -> str:
    if server_name_override:
        server_name = server_name_override
    else:
        server_name = current_app.config['SERVER_NAME']

    code_placeholder = gibberish(10)
    link_placeholder = gibberish(10)

    # Stash the <code> portions so they are not formatted
    code_snippets, link = stash_code_html(link, code_placeholder)

    # Stash the existing links so they are not formatted
    link_snippets, link = stash_link_html(link, link_placeholder)

    pattern = COMMUNITY_PATTERN
    server = r'<a href="https://' + server_name + r'/community/lookup/'
    link = re.sub(pattern, server + r'\g<1>/\g<2>">' + r'!\g<1>@\g<2></a>', link)

    # Bring back the links
    link = pop_link(link_snippets=link_snippets, text=link, placeholder=link_placeholder)

    # Bring back the <code> portions
    link = pop_code(code_snippets=code_snippets, text=link, placeholder=code_placeholder)

    return link


def feed_link_to_href(link: str, server_name_override: str | None = None) -> str:
    if server_name_override:
        server_name = server_name_override
    else:
        server_name = current_app.config['SERVER_NAME']

    code_placeholder = gibberish(10)
    link_placeholder = gibberish(10)

    # Stash the <code> portions so they are not formatted
    code_snippets, link = stash_code_html(link, code_placeholder)

    # Stash the existing links so they are not formatted
    link_snippets, link = stash_link_html(link, link_placeholder)

    pattern = FEED_PATTERN
    server = r'<a href="https://' + server_name + r'/feed/lookup/'
    link = re.sub(pattern, server + r'\g<1>/\g<2>">' + r'~\g<1>@\g<2></a>', link)

    # Bring back the links
    link = pop_link(link_snippets=link_snippets, text=link, placeholder=link_placeholder)

    # Bring back the <code> portions
    link = pop_code(code_snippets=code_snippets, text=link, placeholder=code_placeholder)

    return link


def person_link_to_href(link: str, server_name_override: str | None = None) -> str:
    if server_name_override:
        server_name = server_name_override
    else:
        server_name = current_app.config['SERVER_NAME']

    code_placeholder = gibberish(10)
    link_placeholder = gibberish(10)

    # Stash the <code> portions so they are not formatted
    code_snippets, link = stash_code_html(link, code_placeholder)

    # Stash the existing links so they are not formatted
    link_snippets, link = stash_link_html(link, link_placeholder)

    # Substitute @user@instance.tld with <a> tags, but ignore if it has a preceding / or [ character
    pattern = PERSON_PATTERN
    server = f'https://{server_name}/user/lookup/'
    replacement = (r'<a href="' + server + r'\g<1>/\g<2>" rel="nofollow noindex">@\g<1>@\g<2></a>')
    link = re.sub(pattern, replacement, link)

    # Bring back the links
    link = pop_link(link_snippets=link_snippets, text=link, placeholder=link_placeholder)

    # Bring back the <code> portions
    link = pop_code(code_snippets=code_snippets, text=link, placeholder=code_placeholder)

    return link


def stash_code_html(text: str, placeholder: str) -> tuple[list, str]:
    code_snippets = []

    def store_code(match):
        code_snippets.append(match.group(0))
        return f"{placeholder}{len(code_snippets) - 1}$"

    text = re.sub(r'<code>[\s\S]*?<\/code>', store_code, text)

    return (code_snippets, text)


def stash_code_md(text: str, placeholder: str) -> tuple[list, str]:
    code_snippets = []

    def store_code(match):
        code_snippets.append(match.group(0))
        return f"{placeholder}{len(code_snippets) - 1}$"

    # Fenced code blocks (```...```)
    text = re.sub(r'```[\s\S]*?```', store_code, text)
    # Inline code (`...`)
    text = re.sub(r'`[^`\n]+`', store_code, text)

    return (code_snippets, text)


def pop_code(code_snippets: list, text: str, placeholder: str) -> str:
    for i, code in enumerate(code_snippets):
        text = text.replace(f"{placeholder}{i}$", code)

    return text


def stash_link_html(text: str, placeholder: str) -> tuple[list, str]:
    link_snippets = []

    def store_link(match):
        link_snippets.append(match.group(0))
        return f"{placeholder}{len(link_snippets) - 1}$"

    text = re.sub(r'<a href=[\s\S]*?<\/a>', store_link, text)

    return (link_snippets, text)


def pop_link(link_snippets: list, text: str, placeholder: str) -> str:
    for i, link in enumerate(link_snippets):
        text = text.replace(f"{placeholder}{i}$", link)

    return text


def url_is_parseable(url) -> bool:
    """Whether urlparse() can read `url` at all, without raising.

    This is an INGRESS check, and it exists because the guards that stop
    urlparse's ValueError escaping (domain_from_url, remove_tracking_from_link,
    fixup_url, ...) all return a safe value instead. That is right for those
    functions, but it means "no domain" and "not a URL at all" arrive at their
    callers as the same answer -- so a caller that has to REFUSE an unparseable
    URL cannot tell the two apart from the return value and has to ask here
    first.

    Deliberately narrow: it answers only "does urlparse accept this", not "is
    this a good URL". No scheme check, no host check, no length check -- those
    belong to the callers that want them, and widening this predicate would
    reject legitimate URLs (ports, userinfo, IDN and percent-encoded hosts all
    parse fine and must keep passing). A non-str is not parseable; the empty
    string is, and callers that care about emptiness already say so.
    """
    if not isinstance(url, str):
        return False
    try:
        urlparse(url)
        # domain_from_url parses url.lower(), not the raw string, and every
        # caller of this predicate hands the same string to it. Checking only
        # one of the two would let a string that diverged between them be
        # stored AND skip the banned-domain check, since domain_from_url would
        # return None for it. No divergent shape is known -- raw and lowered
        # agree for every case-changing codepoint in Python's Unicode tables,
        # which tests/test_unparseable_url_ingress.py sweeps -- so this costs
        # nothing today and removes the need to keep re-deriving that.
        urlparse(url.lower())
    except ValueError:
        # The shapes urlparse itself refuses: an unbalanced IPv6 bracket
        # ('https://youtube.com[abc'), two '::' runs in an address, a netloc
        # that fails urllib's NFKC confusability check.
        return False
    return True


def domain_from_url(url: str, create=True) -> Domain:
    if not url:
        # See is_image_url for why this is a conditional. This one does not even
        # reach urlparse: url.lower() on None is AttributeError, which no
        # ValueError handler sees either. None is the answer this already gives
        # for a url whose host it cannot determine, and every caller in app/
        # writes `if domain:` before dereferencing it.
        return None
    try:
        parsed_url = urlparse(url.lower())
    except ValueError:
        # urlparse itself refuses some netlocs a person can put in the URL box:
        # an unbalanced IPv6 bracket ('https://[::1/x'), an address with two
        # '::' runs, or a host that fails its NFKC confusability check. No host
        # could be determined, which is the same answer the else: arm below
        # gives for a hostless URL.
        #
        # Scoped to the parse alone on purpose. The Domain query below raises
        # its own ValueError from the Postgres driver when the host contains a
        # NUL byte; widening this guard to cover the query would silently
        # swallow that (see tests/test_urlparse_valueerror_guards.py).
        return None
    if parsed_url and parsed_url.hostname:
        find_this = parsed_url.hostname.lower()
        if find_this.startswith('www.'):
            find_this = find_this[4:]
        if find_this == 'youtu.be':
            find_this = 'youtube.com'
        domain = db.session.query(Domain).filter_by(name=find_this).first()
        if create and domain is None:
            domain = Domain(name=find_this)
            db.session.add(domain)
            db.session.commit()
        return domain
    else:
        return None


def domain_from_email(email: str) -> str:
    if email is None or email.strip() == '':
        return ''
    else:
        if '@' in email:
            parts = email.split('@')
            return parts[-1]
        else:
            return ''


def sanitise_posting_warning(html: str) -> str:
    """A posting warning that is safe to render, cut to its column.

    D1377. `Community.posting_warning` and `Instance.posting_warning` are rendered
    `{{ ...|safe }}` (app/templates/post/post.html:91, :94). That is deliberate --
    a moderator may format the warning -- so the value has to be sanitised where it
    is WRITTEN, which is what every other `|safe` field in this codebase relies on
    (`description_html`, `about_html`, `rules_html` all come out of
    `allowlist_html`). Four writers had no sanitiser at all: the community create
    and edit forms, the admin community form, and the two ActivityPub paths, where
    the warning is whatever a peer published.

    Cut after sanitising because escaping can lengthen the string, and the column
    is `String(512)`. A tag the cut splits is dropped by the browser, and nothing
    that survives `allowlist_html` can execute.
    """
    return allowlist_html(html)[:512]


def community_link_markup(community) -> Markup:
    """A safe `<a>` for a community, for the flash messages that need markup.

    D1376. Four places built this by hand as

        community_name=f'<a href="/c/{community.link()}">{community.display_name()}</a>'

    and handed the result to `Markup(_(...))`. `Markup` marks a string safe, and
    Jinja renders a safe string verbatim -- so `{{ message }}` in base.html put
    both interpolated values straight into the DOM. Neither is safe to put there:
    `display_name()` is `title` (or `title@ap_domain`) and `link()` is `name` or
    `ap_id`, and for a REMOTE community all of those come from the peer's actor
    document, which restricts no characters (fact 797). Measured, the whole
    rendered message for a community whose title a peer set:

        You joined <a href="/c/memes@peer.example"></a><img src=x
        onerror=alert(1)>@peer.example</a>

    `Markup.format` escapes its arguments, which is the construction that cannot
    be got wrong; the four call sites could each be fixed and one of them missed.
    """
    return Markup('<a href="/c/{link}">{name}</a>').format(
        link=community.link(), name=community.display_name())


def shorten_string(input_str, max_length=50):
    if input_str:
        if len(input_str) <= max_length:
            return input_str
        else:
            return input_str[:max_length - 3] + '…'
    else:
        return ''


def shorten_url(input: str, max_length=20):
    if input:
        return shorten_string(input.replace('https://', '').replace('http://', ''))
    else:
        return ''


def remove_images(html) -> str:
    # Parse the HTML content
    soup = BeautifulSoup(html, 'html.parser')

    # Remove all <img> tags
    for img in soup.find_all('img'):
        img.decompose()

    # Remove all <video> tags
    for video in soup.find_all('video'):
        video.decompose()

    # Return the modified HTML
    return str(soup)


# the number of digits in a number. e.g. 1000 would be 4
def digits(input: int) -> int:
    return len(shorten_number(input))


def decrement_unread_counts(user_ids) -> None:
    """Take one off `User.unread_notifications` for each id in `user_ids`.

    D1360. Three places did this as

        update(User).where(User.id.in_(user_ids))
                    .values({User.unread_notifications: User.unread_notifications - 1})

    and an `IN` list is a SET: the arithmetic runs ONCE per matching user however
    many times their id appears, so an admin with two unread notifications from one
    registration lost one of them and kept a badge for a notification that had been
    marked read. There was no floor either, so a list naming somebody with nothing
    unread drove the count NEGATIVE. Measured through `finalize_user_setup` with an
    admin holding notifications from one registration:

        2 unread  -> 1   (expected 0)
        1 unread  -> 0   (expected 0)
        0 unread  -> -1  (expected 0)

    `user_ids` may hold duplicates and is counted, not de-duplicated. `GREATEST(...,
    0)` is the floor, which is what `app/api/alpha/utils/reply.py`'s single-row
    decrement already had as `AND unread_notifications > 0`.
    """

    for user_id, how_many in Counter(user_ids).items():
        db.session.execute(
            text('UPDATE "user" SET unread_notifications = '
                 'GREATEST(unread_notifications - :how_many, 0) WHERE id = :user_id'),
            {'how_many': how_many, 'user_id': user_id})


@cache.memoize(timeout=50)
def user_access(permission: str, user_id: int) -> bool:
    if user_id == 0:
        return False
    if user_id == 1:
        return True
    has_access = db.session.execute(text('SELECT * FROM "role_permission" as rp ' +
                                         'INNER JOIN user_role ur on rp.role_id = ur.role_id ' +
                                         'WHERE ur.user_id = :user_id AND rp.permission = :permission'),
                                    {'user_id': user_id, 'permission': permission}).first()
    return has_access is not None


def can_moderate(community, user) -> bool:
    # D422/D521/D642/D963: the one moderation gate -- a moderator (owners included), an admin, staff, or a holder of
    # 'administer all communities'. Deleting or restoring a whole community is narrower: owner and admins (D615).
    return community.is_moderator(user) or user.is_admin_or_staff() or \
        user_access('administer all communities', user.id)


def role_access(permission: str, role_id: int) -> bool:
    has_access = db.session.execute(text('SELECT * FROM "role_permission" as rp ' +
                                         'WHERE rp.role_id = :role_id AND rp.permission = :permission'),
                                    {'role_id': role_id, 'permission': permission}).first()
    return has_access is not None


@cache.memoize(timeout=10)
def community_membership(user: User, community: Community) -> int:
    if community is None:
        return False
    return user.subscribed(community.id)


@cache.memoize(timeout=10)
def feed_membership(user: User, feed: Feed) -> int:
    if feed is None:
        return False
    return feed.subscribed(user.id)


@cache.memoize(timeout=86400)
def communities_banned_from(user_id: int) -> List[int]:
    if user_id == 0:
        return []
    community_bans = db.session.query(CommunityBan).filter(CommunityBan.user_id == user_id).all()
    instance_bans = db.session.query(Community).join(InstanceBan, Community.instance_id == InstanceBan.instance_id).\
        filter(InstanceBan.user_id == user_id).all()
    return [cb.community_id for cb in community_bans] + [cb.id for cb in instance_bans]


def user_banned_from_community(user_id: int, community_id: int) -> bool:
    """D995: the one answer to "is this user banned from this community".

    A community ban or a ban from the community's whole instance, through the
    memoized `communities_banned_from` -- and, because that list is cached for a
    day and a ban arriving by a path that does not invalidate it would leave it
    stale, a fresh read of the CommunityBan row as well (D991).
    """
    if community_id in communities_banned_from(user_id):
        return True
    return db.session.query(CommunityBan).filter_by(user_id=user_id, community_id=community_id).first() is not None


@cache.memoize(timeout=86400)
def communities_banned_from_all_users() -> dict[int, List[int]]:
    """Returns dict mapping user_id to list of community_ids they are banned from."""
    rows = db.session.execute(text("""
        SELECT user_id, ARRAY_AGG(DISTINCT community_id) as community_ids
        FROM (
            SELECT user_id, community_id FROM community_ban
            UNION
            SELECT ib.user_id, c.id as community_id
            FROM instance_ban ib
            JOIN community c ON c.instance_id = ib.instance_id
        ) all_bans
        GROUP BY user_id
    """)).fetchall()

    return {user_id: list(community_ids) for user_id, community_ids in rows}


@cache.memoize(timeout=86400)
def blocked_domains(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = db.session.query(DomainBlock).filter_by(user_id=user_id)
    return [block.domain_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_communities(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = db.session.query(CommunityBlock).filter_by(user_id=user_id)
    return [block.community_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_or_banned_instances(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = db.session.query(InstanceBlock).filter_by(user_id=user_id)
    return [block.instance_id for block in blocks] + banned_instances(user_id)


@cache.memoize(timeout=86400)
def blocked_instances(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = db.session.query(InstanceBlock).filter_by(user_id=user_id)
    return [block.instance_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_users(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = db.session.query(UserBlock).filter_by(blocker_id=user_id)
    return [block.blocked_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_phrases() -> List[str]:
    site = db.session.get(Site, 1)
    # D1429. `site.blocked_phrases` was read outright, so with no Site row this
    # raised `AttributeError: 'NoneType' object has no attribute
    # 'blocked_phrases'` -- and `PostReply.new` two lines above its own
    # `if site is None: site = Site()` calls this, so that default could only
    # ever be reached by a reply with an EMPTY BODY. The row is written by
    # `flask init-db`, and an instance whose migrations have run but whose setup
    # has not can already be delivered to: every incoming post and reply with a
    # body was lost, `Post.new` and `PostReply.new` alike. No row means no
    # blocked phrases, which is the same answer as an empty setting.
    if site and site.blocked_phrases:
        blocked_phrases = []
        for phrase in site.blocked_phrases.split('\n'):
            if phrase != '':
                if phrase.endswith('\r'):
                    blocked_phrases.append(phrase[:-1])
                else:
                    blocked_phrases.append(phrase)
        return blocked_phrases
    else:
        return []


@cache.memoize(timeout=86400)
def blocked_referrers() -> List[str]:
    site = db.session.get(Site, 1)
    if site.auto_decline_referrers:
        return [referrer for referrer in site.auto_decline_referrers.split('\n') if referrer != '']
    else:
        return []


def block_honey_pot():
    # Return 403 for any IP address that has visited /honey/* too many times. See honey_pot()
    if current_user.is_anonymous and g.site.honeypot:
        if app_pkg.redis_client.exists(f"ban:{ip_address()}"):
            abort(403)


def instance_community_ids(instance_id) -> List[int]:
    rows = db.session.execute(select(Community.id).where(Community.instance_id == instance_id)).scalars()
    return list(rows)


@cache.memoize(timeout=86400)
def banned_instances(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = db.session.query(InstanceBan).filter_by(user_id=user_id)
    return [block.instance_id for block in blocks]


@cache.memoize(timeout=86400)
def silenced_instances() -> List[int]:
    instances = db.session.query(Instance).filter(Instance.silenced == True)
    return [block.id for block in instances]


@cache.memoize(timeout=86400)
def allowed_instance_domains() -> List[str]:
    allowed = db.session.query(AllowedInstances).all()
    return [instance.domain for instance in allowed]


def retrieve_block_list():
    try:
        response = httpx_client.get('https://raw.githubusercontent.com/rimu/no-qanon/master/domains.txt', timeout=1)
    except:
        return None
    if response and response.status_code == 200:
        return response.text


def retrieve_peertube_block_list():
    try:
        response = httpx_client.get('https://peertube_isolation.frama.io/list/peertube_isolation.json', timeout=1)
    except:
        return None
    list = ''
    if response and response.status_code == 200:
        response_data = response.json()
        for row in response_data['data']:
            list += row['value'] + "\n"
    response.close()
    return list.strip()


def ensure_directory_exists(directory):
    """Ensure a directory exists and is writable, creating it if necessary.

    Handles absolute and relative paths alike. It previously handled only
    relative ones: it split the argument on '/' and rebuilt the path one
    component at a time starting from '', and an absolute path's split always
    begins with an empty component, so the very first os.mkdir('') raised
    FileNotFoundError before any real directory was created or touched. That
    was unconditional for every absolute path, not an edge case. It stayed
    latent because every caller in app/ happens to pass a relative path
    ('app/static/...'), but this function is used for upload directories and a
    future absolute path would have failed confusingly.

    os.makedirs does the component-at-a-time creation the loop was hand-rolling,
    and exist_ok=True gives the same tolerance of an already-existing directory
    the `if not os.path.isdir(...)` test gave. Relative paths still resolve
    against the working directory - nothing is absolutised here.
    """
    os.makedirs(directory, exist_ok=True)

    # Check if the final directory is writable
    if not os.access(directory, os.W_OK):
        current_app.logger.warning(f"Directory '{directory}' is not writable")


def feed_entry_body(post) -> str | None:
    """What a feed entry says for a post: a content warning stands in for the body, which a reader cannot collapse."""
    if post.content_warning:
        return f'<p>{html_escape(post.content_warning)}</p>'
    return post.body_html


def mimetype_from_url(url):
    if not url:
        # See is_image_url for why this is a conditional. Without it,
        # urlparse(None).path is b'' and b''.split('?') raises
        # "TypeError: a bytes-like object is required, not 'str'". None is the
        # same "no mimetype could be determined" answer mimetypes.guess_type
        # gives for an unrecognised path, and every one of the seven RSS
        # callers already writes `if type and not type.startswith('text/')`.
        return None
    try:
        parsed_url = urlparse(url)
    except ValueError:
        # Malformed netloc (unbalanced IPv6 bracket, bad IPv6 literal, or a host
        # urllib rejects under NFKC normalization). No path to guess from, which
        # is the same None mimetypes.guess_type returns for an unrecognised one.
        return None
    path = parsed_url.path.split('?')[0]  # Strip off anything after '?'
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type


def is_activitypub_request():
    return 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', '')


def validation_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if current_user.verified or not get_setting('email_verification', True):
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.validation_required'))

    return decorated_view


def approval_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if not (current_user.private_key is None and (g.site.registration_mode == 'RequireApplication' or g.site.registration_mode == 'Closed')):
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.please_wait'))

    return decorated_view


def trustworthy_account_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if current_user.trustworthy() or current_user.get_id() in g.admin_ids:
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.not_trustworthy'))

    return decorated_view


def aged_account_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if current_user.get_id() in g.admin_ids or not current_user.created_very_recently():
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.not_trustworthy'))

    return decorated_view


def login_required_if_private_instance(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if is_activitypub_request():
            return func(*args, **kwargs)
        if current_app.config['CONTENT_WARNING'] and request.cookies.get('warned') is None:
            return redirect(url_for('main.content_warning', next=request.path))
        if (g.site.private_instance and current_user.is_authenticated) or is_activitypub_request() or g.site.private_instance is False:
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.login', next=referrer()))

    return decorated_view


def refuse_if_private_instance(func):
    """404 every request to this view while the instance is private.

    D1410 put this rule inline at the top of six RSS views. D1411's commit review found
    the hole that leaves: `show_community_rss`, `show_profile_rss` and `show_topic_rss`
    carry `@cache.cached`, which is ABOVE the function body, so a response cached while
    the instance was public is replayed for up to 600 seconds after an admin makes it
    private -- and an inline check inside the function never runs at all. The same
    arithmetic in reverse leaves a cached 404 answering for ten minutes after an instance
    is made public again.

    As a decorator listed directly under `@bp.route` it is the OUTERMOST wrapper, so it
    runs before `cache.cached` consults its store and the cached body is never reached.
    That is the same "refuse first, then answer conditionally" ordering `index_rss`'s
    comment argues for against its own 304 -- whose `@cache.cached` line is commented out,
    which is why its inline check was enough.

    Unconditional, as `index_rss` writes it: an RSS reader presents no session and follows
    no redirect to a login form, so a private instance has no feeds rather than
    members-only feeds. `login_required_if_private_instance` is the right decorator for a
    route a person opens in a browser (D1411 uses it on the embeds); this one is for the
    machine-readable views.
    """

    @wraps(func)
    def decorated_view(*args, **kwargs):
        if g.site.private_instance and rss_token_user() is None:  # R219: a member's RSS token opens the feed
            abort(404)
        return func(*args, **kwargs)

    return decorated_view


def ensure_rss_token(user) -> str:
    """The user's RSS token, created on first use so a private feed can be generated for them."""
    if user.rss_token is None:
        user.rss_token = gibberish(20)
        db.session.commit()
    return user.rss_token


def rss_token_user():
    """The local account whose RSS token this request's `?token=` carries, or None.

    R219 (owner ruling): an RSS reader presents no session, so on a private instance the
    token in the url is how a member's reader gets in; anonymous readers still get 404.
    D1356's rules for the token: a banned or deleted account's token no longer works.
    """
    rss_token = request.args.get('token')
    if not rss_token:
        return None
    return User.query.filter(User.rss_token == rss_token.strip(),
                             User.ap_id == None,  # noqa: E711 -- a local account
                             User.banned == False,  # noqa: E712
                             User.deleted == False).first()  # noqa: E712


def check_anoobis(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        whitelist = ['Mastodon', 'Friendica', 'Synapse', 'PieFed', 'Bridgy', 'Lemmy', 'FlipboardProxy', 'Googlebot', 'GoogleOther', 'Kagibot', 'bingbot', 'Discordbot']
        if current_user.is_anonymous and current_app.config['ANOOBIS'] and \
                request.cookies.get('anoobis') is None and \
                not any(item in request.user_agent.string for item in whitelist):
            return redirect(url_for('main.anoobis', next=request.path))
        return func(*args, **kwargs)

    return decorated_view


def permission_required(permission):
    def decorator(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            if user_access(permission, current_user.get_id()):
                return func(*args, **kwargs)
            else:
                # Handle the case where the user doesn't have the required permission
                return redirect(url_for('auth.permission_denied'))

        return decorated_view

    return decorator


def login_required(csrf=True):
    def decorator(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            if request.method in {"OPTIONS"} or current_app.config.get("LOGIN_DISABLED"):
                pass
            elif not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()

            # Validate CSRF token for POST requests
            if request.method == 'POST' and csrf:
                try:
                    validate_csrf(request.form.get('csrf_token',
                                                   request.headers.get('x-csrftoken')))
                except ValidationError:
                    # abort(400), not the bare ValidationError. There is no
                    # CSRFProtect registered on this app, so nothing turned
                    # that into a response and a POST with a missing or stale
                    # token answered 500 with a traceback -- on every route
                    # using this decorator. A stale token is what an ordinary
                    # user gets from a page left open too long, so it is a
                    # request error, not a server error.
                    abort(400, description='The CSRF token is missing or invalid.')

            # flask 1.x compatibility
            # current_app.ensure_sync is only available in Flask >= 2.0
            if callable(getattr(current_app, "ensure_sync", None)):
                return current_app.ensure_sync(func)(*args, **kwargs)
            return func(*args, **kwargs)

        return decorated_view

    # Handle both @login_required and @login_required()
    if callable(csrf):
        # Called as @login_required (csrf is actually the function)
        func = csrf
        csrf = True
        return decorator(func)
    else:
        # Called as @login_required(csrf=False)
        return decorator


def debug_mode_only(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        if current_app.debug:
            return func(*args, **kwargs)
        else:
            return abort(403,
                         description="Not available in production mode. Set the FLASK_DEBUG environment variable to 1.")

    return decorated_function


def block_bots(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        if not is_bot(request.user_agent.string):
            return func(*args, **kwargs)
        else:
            return abort(403, description="Do not index this.")

    return decorated_function


def is_bot(user_agent) -> bool:
    user_agent = user_agent.lower()
    if 'bot' in user_agent:
        return True
    if 'meta-externalagent' in user_agent:
        return True
    return False


# Characters a browser strips from a URL before parsing it (tab, LF, CR). We strip
# them too, so this function and the browser cannot disagree about the host.
_URL_STRIPPED_BY_BROWSERS = str.maketrans('', '', '\t\n\r')

# The admin-configurable redirect policy: which HOSTS is_safe_redirect_target will
# accept, over and above this server itself. Stored in the Setting table (no
# migration), read through the memoized get_setting.
#
# The default is SAME_ORIGIN, and is the value get_setting is asked for whenever
# no row exists -- so an install that upgrades into this feature keeps exactly the
# behaviour it had before, without an admin touching anything. Any value that is
# not one of the four below is also treated as SAME_ORIGIN: this fails closed, so
# a typo, a hand-edited row or a value written by a future version can only ever
# narrow the check, never widen it.
REDIRECT_POLICY_SETTING = 'redirect_policy'
REDIRECT_POLICY_SAME_ORIGIN = 'same_origin'
REDIRECT_POLICY_TRUSTED_SERVERS = 'trusted_servers'
REDIRECT_POLICY_FEDERATED_SERVERS = 'federated_servers'
REDIRECT_POLICY_ALL_REFERRERS = 'all_referrers'


@cache.memoize(timeout=150)
def instance_redirect_allowed(host: str, trusted_only: bool) -> bool:
    """True when `host` is a federated instance a redirect may point at.

    `trusted_only` narrows the answer to instances an admin has marked
    `trusted`; otherwise any instance we know about qualifies.

    Memoized like its siblings instance_banned / instance_online /
    instance_gone_forever, because is_safe_redirect_target runs on every "go
    back" and every `?next=`. None of those siblings answers this question:
    instance_allowed reads the AllowedInstances federation allowlist rather than
    Instance; instance_online adds `dormant` to the test, which is not wanted
    here (see below); and instance_gone_forever would answer it only by relying
    on its "unknown domain -> True" convention, which is a fragile thing for a
    security decision to lean on -- flipping that default, a defensible change
    on its own terms, would silently turn this mode into "any host".
    trusted_instance_ids() returns ids, not domains, so it cannot match a host.

    BANNED and GONE_FOREVER instances are excluded, in both modes:

    * Banned: an admin has declared the instance unwanted and we send it no
      activities. Sending it a *person* -- which is what a redirect does -- is
      strictly worse than sending it an activity, and a redirect to a hostile
      host is exactly the phishing shape this whole check exists to prevent. A
      ban is also a later and more deliberate act than the `trusted` flag, so
      where a row is both banned and trusted, the ban wins.
    * Gone forever: set after ~12 days of unreachability. The row then records
      that nobody has answered on that domain for nearly a fortnight, so it is no
      longer evidence about who *does* answer -- a lapsed domain can be
      re-registered by anyone, and the stale row would hand them our users.

    `dormant` is NOT excluded. It is a transient five-day send-failure state
    that clears itself once the instance responds again; it says nothing about
    who owns the domain, so it is not a reason to refuse a person's own "go
    back".

    Host matching is case-insensitive, done the way every other instance lookup
    in this module does it: the input is lower-cased with inbox_domain() and
    compared to the stored `Instance.domain`, which is written lower-case by the
    same function. That keeps the unique index on `domain` in play on a hot
    path. A row somehow stored with upper-case would not match and the redirect
    would be refused -- failing closed, which is the right direction here.
    """
    if not host:
        return False
    domain = inbox_domain(host.strip())
    if not domain:
        return False
    instance = db.session.query(Instance).filter_by(domain=domain).first()
    if instance is None:
        return False
    if instance.gone_forever:
        return False
    if trusted_only and not instance.trusted:
        return False
    return not instance_banned(domain)


def is_safe_redirect_target(url) -> bool:
    """True when `url` can be handed to `redirect()` under the site's policy.

    This is THE origin check. Every place a user-influenced URL becomes a
    redirect target -- `back()`, and all three of `referrer()`'s sources -- goes
    through it, so there is one implementation and one behaviour.

    Accepted:
      - relative URLs (`/foo`, `foo`, `?x=1`, `#frag`): same-origin by definition.
      - absolute `http://` or `https://` URLs whose HOST equals `SERVER_NAME`.
      - absolute `http://` or `https://` URLs whose HOST the admin's
        `redirect_policy` setting additionally allows (see below).

    Rejected: any host the policy does not allow, protocol-relative `//host/x`, a
    backslash standing in for the authority slashes, any non-http(s) scheme
    (`javascript:`, `data:`, ...), control characters, and anything `urlparse`
    will not parse.

    THE POLICY WIDENS WHICH HOSTS ARE ACCEPTABLE. IT NEVER CHANGES HOW A URL IS
    PARSED, NOR HOW ITS HOST IS DETERMINED.

    Everything above the `POLICY` marker in the body below is parsing, and runs
    identically in all four modes. Only the host decision underneath it branches.
    That split is what keeps `javascript:`, a control character, an unparseable
    URL and a bare authority rejected even under `all_referrers`, and it is what
    makes userinfo smuggling impossible to use as a disguise: the host every mode
    matches on is `urlparse(...).hostname`, the part after any `@`, which is the
    host the browser will actually navigate to. Under `all_referrers`,
    `https://our.host@evil.example/` IS accepted -- because its real host is
    `evil.example` and that mode accepts every host, not because the userinfo
    fooled anything. Under `trusted_servers` the identical URL is rejected unless
    `evil.example` itself is a trusted instance.

    The four modes, from `redirect_policy` (default `same_origin`):

      same_origin        this server only. What every install had before this
                         setting existed, and what one that has never been
                         configured still gets.
      trusted_servers    plus instances marked `trusted`.
      federated_servers  plus every instance we federate with.
      all_referrers      any host. DANGEROUS -- this is an open redirect by
                         choice, and the admin form says so.

    Consequence worth knowing: this function is no longer pure. Under the default
    it touches the database only for an off-origin absolute URL -- a relative URL
    and this server's own host are both decided before the setting is read -- but
    it can query, so it needs an app context with a working database, not just a
    request context.

    Two decisions worth stating, because they are the ones that make this
    different from what it replaces:

    * It compares the parsed HOST, not `SERVER_NAME in url`. The substring form
      that used to guard `referrer()` and two community routes accepts
      `https://evil.example/?x=<SERVER_NAME>` -- the server name is in the query
      string -- and also `https://<SERVER_NAME>@evil.example/`, where everything
      before the `@` is userinfo the browser ignores.

    * `SERVER_NAME` may carry a port (`env.sample` ships `127.0.0.1:5000`), so the
      port is stripped from it before comparing, and the URL's port is IGNORED.
      Ports are a deployment detail -- a dev server on :5000, a public origin on
      :443 behind a proxy -- and a same-host different-port URL requires an
      attacker to already control a service on this very host, at which point a
      redirect is not the weak link. Host identity is the control that matters.
      Requiring an exact port match would instead break "go back" on real
      deployments whose `SERVER_NAME` and public port disagree.

    Scheme mismatch (`http://` to an https site, or the reverse) is ACCEPTED.
    Rejecting it buys nothing: an attacker cannot make `http://<our host>` serve
    their content, so both schemes land on this site either way. PieFed also
    supports `HTTP_PROTOCOL = 'mixed'`, and a Referer legitimately arrives with
    either scheme from behind a TLS-terminating proxy. Every scheme that is
    actually dangerous is not http(s) at all, and those are rejected outright.
    """
    if not isinstance(url, str):
        return False
    candidate = url.strip().translate(_URL_STRIPPED_BY_BROWSERS)
    if not candidate:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in candidate):
        return False
    # WHATWG URL treats a backslash in the authority position as a forward slash,
    # so `/\evil.example` navigates to another origin even though urlparse reports
    # it as an ordinary relative path. Normalise before deciding.
    candidate = candidate.replace('\\', '/')
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        parsed.port  # raises ValueError on a port that is not a number in range
    except ValueError:
        # Malformed IPv6 literals ('http://[::1') and out-of-range ports land
        # here. Unparseable is not same-origin.
        return False

    if not parsed.scheme and not parsed.netloc:
        # Relative -- unless it opens with the authority marker. urlparse reports
        # '///evil.example' as the relative path '/evil.example' with no netloc,
        # but a browser reads three-or-more leading slashes as an authority and
        # navigates off-site. Anything starting '//' is not relative here.
        return not candidate.startswith('//')

    if parsed.scheme.lower() not in ('http', 'https'):
        return False

    if not host:
        return False

    # ---- POLICY ------------------------------------------------------------
    # Everything above this line is parsing, and is identical in all four modes.
    # Below it, only WHICH HOSTS are acceptable changes. `host` is
    # urlparse's .hostname -- the authority with any userinfo already discarded,
    # which is the host the browser will navigate to -- and it is the only value
    # any mode below is allowed to match on.

    # `.lower()` is belt-and-braces: urlparse's .hostname normalises the case of
    # an ASCII host, and config.py lower-cases SERVER_NAME at import, so neither
    # call is discriminated by a test. They are here so the comparison stays
    # correct if either of those normalisations goes away.
    host = host.lower()

    server_name = (current_app.config.get('SERVER_NAME') or '').strip().lower()
    expected_host = urlparse(f'//{server_name}').hostname if server_name else None
    if expected_host and host == expected_host.lower():
        # Our own host is acceptable under every policy, and deciding it here
        # means the common case never reads the setting or touches the database.
        return True

    policy = get_setting(REDIRECT_POLICY_SETTING, REDIRECT_POLICY_SAME_ORIGIN)
    if policy == REDIRECT_POLICY_ALL_REFERRERS:
        return True
    if policy == REDIRECT_POLICY_TRUSTED_SERVERS:
        return instance_redirect_allowed(host, True)
    if policy == REDIRECT_POLICY_FEDERATED_SERVERS:
        return instance_redirect_allowed(host, False)
    # REDIRECT_POLICY_SAME_ORIGIN, and anything unrecognised: fail closed.
    return False


def safe_redirect_target(candidate, default: str) -> str:
    """`candidate` if it is safe to redirect to, otherwise `default`.

    The single-source form of the origin check, for the ~25 route sites that
    take ONE user-supplied redirect target out of the request -- `?next=`,
    `?redirect=`, `?return_to=`, a posted `referrer` field -- and have their own
    per-site fallback. `referrer()` is the multi-source form of the same
    decision; both defer to `is_safe_redirect_target`, so there is still exactly
    one implementation of the control.

    A rejected candidate is REPLACED, not raised on: these are ordinary
    navigations, and a user who arrives with a mangled `?redirect=` should land
    on the page the route would have chosen anyway.

    `candidate` is deliberately untyped. `request.args.get` returns None when the
    parameter is absent, and a form field can hand back a list; both are handled
    here rather than at 25 call sites, because `is_safe_redirect_target` returns
    False for any non-`str`.

    Callers whose default is expensive or has a side effect (the login flow's
    `determine_next_page` commits `finished_onboarding`) call
    `is_safe_redirect_target` directly instead, so the default stays lazy.
    """
    if candidate and is_safe_redirect_target(candidate):
        return candidate
    return default


# sends the user back to where they came from
def back(default_url):
    # Get the referrer from the request headers
    referrer = request.referrer

    # Redirect to the referrer when there is one, it is not the URL being requested
    # (which would be a loop), and it points at this site (an unchecked Referer is an
    # open redirect -- see is_safe_redirect_target).
    if referrer and referrer != request.url and is_safe_redirect_target(referrer):
        return redirect(referrer)

    # Otherwise the default URL.
    return redirect(default_url)


# format a datetime in a way that is used in ActivityPub
def ap_datetime(date_time: datetime) -> str:
    return date_time.isoformat() + '+00:00'


class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


# The client IP address. One implementation, shared with Flask-Limiter's key function:
# this used to be a copy of app.get_ip_address and the two drifted apart, so rate
# limiting and IP bans could bucket the same request differently. It lives in
# app/__init__.py because that module builds the limiter at import time and cannot
# import from app.utils (app.utils imports from app). See app.get_ip_address for what
# is trusted and why.
ip_address = get_ip_address


def user_ip_banned() -> bool:
    current_ip_address = ip_address()
    if current_ip_address:
        return current_ip_address in banned_ip_addresses()


@cache.memoize(150)
def instance_allowed(host: str) -> bool:
    # An absent host is NOT on the allowlist. This returned True until
    # 2026-08-29 -- "allowed by default" -- which made the function fail OPEN on
    # the one input its callers cannot vouch for. Two producers hand it an empty
    # host and both are fed by remote input: extract_domain_and_actor returns
    # ('', '') when urlparse refuses an actor id the peer chose, and
    # inbox_domain returns '' for the same reason on a peer-chosen inbox URL.
    # See instance_banned below, which was inverted in the same change; read
    # together the pair used to admit an empty host in both federation modes.
    if host is None or host == '':
        return False
    host = inbox_domain(host)
    instance = db.session.query(AllowedInstances).filter_by(domain=host.strip()).first()
    return instance is not None


@cache.memoize(timeout=150)
def instance_banned(domain: str) -> bool:
    session = get_task_session()  # noqa: F811
    try:
        # An absent domain is refused rather than waved through. This returned
        # False -- "not banned" -- until 2026-08-29; with instance_allowed's
        # empty case returning True, the pair failed open in both federation
        # modes, and a banned instance evaded its ban either by malforming its
        # actor id or simply by being referenced as a webfinger handle.
        #
        # This direction has a cost and it is deliberate: many callers read
        # `not instance_banned(instance.domain)` as an outbound delivery gate,
        # and Instance.domain is nullable, so a row with no domain now stops
        # receiving deliveries. Not federating with a row whose identity is
        # unknown is the safe direction, and such a row is already broken.
        if domain is None or domain == '':
            return True
        domain = inbox_domain(domain.strip())
        banned = session.query(BannedInstances).filter_by(domain=domain).first()
        if banned is not None:
            return True

        # Mastodon sometimes bans with a * in the domain name, meaning "any letter", e.g. "cum.**mp"
        #
        # re.escape first, then reinstate the wildcard. Interpolating the domain
        # raw made every '.' in a wildcard ban a metacharacter, so 'ev*l.com'
        # banned 'evilXcom' as well as 'evXl.com' -- and an entry that also held
        # a regex metacharacter did not compile at all: 'ev*l.co(m' raised
        # re.PatternError out of this function, which re-raises. This gates
        # inbound activity processing and outbound delivery, so one malformed
        # entry in the admin blocklist box took federation down instance-wide.
        regex_patterns = [re.compile('^' + re.escape(cond.domain).replace(r'\*', '[a-zA-Z0-9]') + '$')
                          for cond in
                          session.query(BannedInstances).filter(BannedInstances.domain.like('%*%')).all()]
        return any(pattern.match(domain) for pattern in regex_patterns)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@cache.memoize(timeout=150)
def instance_online(domain: str) -> bool:
    if domain is None or domain == '':
        return False
    domain = inbox_domain(domain.strip())
    session = get_task_session()  # noqa: F811
    try:
        instance = session.query(Instance).filter_by(domain=domain).first()
        if instance is not None:
            return instance.online()
        else:
            return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@cache.memoize(timeout=150)
def instance_gone_forever(domain: str) -> bool:
    if domain is None or domain == '':
        return False
    domain = inbox_domain(domain.strip())
    session = get_task_session()  # noqa: F811
    try:
        instance = session.query(Instance).filter_by(domain=domain).first()
        if instance is not None:
            return instance.gone_forever
        else:
            return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def user_cookie_banned() -> bool:
    cookie = request.cookies.get('sesion', None)
    return cookie is not None


@cache.memoize(timeout=30)
def banned_ip_addresses() -> List[str]:
    session = get_task_session()  # noqa: F811
    try:
        ips = session.query(IpBan).all()
        return [ip.ip_address for ip in ips]
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def guess_mime_type(file_path: str) -> str:
    # D1435. An `if content_type is None:` arm stood above this, duplicating the
    # fallback below it, and it was unreachable: `mimetypes.guess_type` returns a
    # (type, encoding) TUPLE and never None, so the tuple's own `[0] is None` was
    # always the live test. The duplicate is gone rather than both being kept in
    # step by hand.
    guessed_type = mimetypes.guess_type(file_path)[0]
    if guessed_type is None:
        ext = os.path.splitext(file_path)[1].lower().lstrip('.')  # get extension without dot
        return f'image/{ext}' if ext else 'application/octet-stream'
    return guessed_type


def can_downvote(user, community: Community, communities_banned_from_list=None) -> bool:
    if user is None or community is None or user.banned or user.bot:
        return False

    try:
        site = g.site
    except:
        site = db.session.get(Site, 1)

    if not site.enable_downvotes:
        return False

    if community.local_only and not user.is_local():
        return False

    if (user.attitude is not None and user.attitude < 0.0) or user.reputation < -10:
        return False

    if community.downvote_accept_mode != DOWNVOTE_ACCEPT_ALL:
        if community.downvote_accept_mode == DOWNVOTE_ACCEPT_NONE:
            return False
        elif community.downvote_accept_mode == DOWNVOTE_ACCEPT_MEMBERS:
            if not community.is_member(user):
                return False
        elif community.downvote_accept_mode == DOWNVOTE_ACCEPT_INSTANCE:
            if user.instance_id != community.instance_id:
                return False
        elif community.downvote_accept_mode == DOWNVOTE_ACCEPT_TRUSTED:
            if community.instance_id == user.instance_id:
                pass
            else:
                if user.instance_id not in trusted_instance_ids():
                    return False

    if communities_banned_from_list is not None:
        if community.id in communities_banned_from_list:
            return False
    else:
        if community.id in communities_banned_from(user.id):
            return False

    return True


def can_upvote(user, community: Community, communities_banned_from_list=None) -> bool:
    if user is None or community is None or user.banned or user.bot:
        return False

    if communities_banned_from_list is not None:
        if community.id in communities_banned_from_list:
            return False
    else:
        if community.id in communities_banned_from(user.id):
            return False

    return True


def can_create_post(user, content: Community) -> bool:
    if content is None:
        return False

    if user is None or content is None or user.banned:
        return False

    if user.ban_posts:
        return False

    if user.is_local():
        if not user.verified or user.private_key is None:
            return False
    else:
        if not hasattr(g, 'site'):
            g.site = db.session.get(Site, 1)
        if get_setting('use_allowlist') and g.site.allowlist_mode == ALLOWLIST_INTENSE:
            if not instance_allowed(user.ap_domain):
                return False
        else:
            if instance_banned(user.ap_domain):   # don't allow posts from defederated instances
                return False
        if user.created_very_recently() and user.post_count > 3:    # new users can only do 3 posts in their first 24h
            return False

    if content.banned:
        return False

    if user.is_rss_bot() or content.is_moderator(user) or user.is_admin():
        return True

    if content.restricted_to_mods:
        return False

    if content.local_only and not user.is_local():
        return False

    # Private communities are invite-only (Community.private, app/models.py:594):
    # only members may post. Placed after the moderator/admin early return above,
    # alongside the ban checks, so the existing mod/admin model is preserved.
    if content.private and content.id not in community_membership_private(user.id):
        return False

    if content.id in communities_banned_from(user.id):
        return False

    if content.instance_id in banned_instances(user.id):
        return False

    return True


def can_create_post_reply(user, content: Community) -> bool:
    if user is None or content is None or user.banned:
        return False

    if user.ban_comments:
        return False

    if user.is_local():
        if not user.verified or user.private_key is None:
            return False
    else:
        if not hasattr(g, 'site'):
            g.site = db.session.get(Site, 1)
        if get_setting('use_allowlist') and g.site.allowlist_mode == ALLOWLIST_INTENSE:
            if not instance_allowed(user.ap_domain):
                return False
        else:
            if instance_banned(user.ap_domain):
                return False

    if content.banned:
        return False

    if content.is_moderator(user) or user.is_admin():
        return True

    if content.local_only and not user.is_local():
        return False

    if content.id in communities_banned_from(user.id):
        return False

    return True


def can_upload_video(user: User | None = None):
    """Checks if the user can upload a video.

    :param user: The user to check, e.g. for API contexts. If not provided, uses the current_user from flask_login.
    """
    upload_access = get_setting('allow_video_file_uploads', 'no')
    upload_user = user or current_user
    if upload_access == 'no':
        return False
    elif upload_access == 'user 1' and upload_user.get_id() != 1:
        return False
    elif upload_access == 'admins' and not upload_user.is_admin_or_staff():
        return False
    elif upload_access == 'users' and not upload_user.is_authenticated:
        return False
    return True


def reply_already_exists(user_id, post_id, parent_id, body) -> bool:
    if parent_id is None:
        num_matching_replies = db.session.execute(text(
            'SELECT COUNT(*) as c FROM "post_reply" WHERE deleted is false and user_id = :user_id AND post_id = :post_id AND parent_id is null AND body = :body'),
            {'user_id': user_id, 'post_id': post_id, 'body': body}).scalar()
    else:
        num_matching_replies = db.session.execute(text(
            'SELECT COUNT(*) as c FROM "post_reply" WHERE deleted is false and user_id = :user_id AND post_id = :post_id AND parent_id = :parent_id AND body = :body'),
            {'user_id': user_id, 'post_id': post_id, 'parent_id': parent_id, 'body': body}).scalar()
    return num_matching_replies != 0


def reply_is_just_link_to_gif_reaction(body) -> bool:
    # D1333. `body` reaches here from a peer's Note, where
    # `request_json['object']['source']['content']` is whatever the peer put
    # there -- including null, which made this `AttributeError: 'NoneType' object
    # has no attribute 'strip'` and lost the comment. A comment with no body is
    # not a gif link.
    if not isinstance(body, str):
        return False
    tmp_body = body.strip()
    if tmp_body.startswith('https://media.tenor.com/') or \
            tmp_body.startswith('https://media1.tenor.com/') or \
            tmp_body.startswith('https://media2.tenor.com/') or \
            tmp_body.startswith('https://media3.tenor.com/') or \
            tmp_body.startswith('https://i.giphy.com/') or \
            tmp_body.startswith('https://i.imgflip.com') or \
            tmp_body.startswith('https://media1.giphy.com/') or \
            tmp_body.startswith('https://media2.giphy.com/') or \
            tmp_body.startswith('https://media3.giphy.com/') or \
            tmp_body.startswith('https://media4.giphy.com/'):
        return True
    else:
        return False


def reply_is_low_effort(body) -> bool:
    # D1333's sibling, reached from the same call site with the same value.
    if not isinstance(body, str):
        return False
    lower_body = body.lower().strip()
    if lower_body == 'this' or lower_body == 'this.' or lower_body == 'this!':
        return True
    return False


@cache.memoize(timeout=3000)
def trusted_instance_ids() -> List[int]:
    return [instance.id for instance in Instance.query.filter(Instance.trusted == True)]


def inbox_domain(inbox: str) -> str:
    """Reduce an ActivityPub URL, or a bare domain, to a lower-case hostname.

    Accepts both forms because callers hold values from either source: an inbox
    or actor URL from a remote payload, or a domain already stored on a row. A
    value with no scheme is only lower-cased. `.hostname` rather than `.netloc`,
    so any port is dropped.

    This is the single implementation of a normalisation that used to be copied
    inline into instance_allowed, instance_banned, instance_online and
    instance_gone_forever. It does not strip surrounding whitespace: those
    callers strip before or after to preserve their own long-standing behaviour.
    """
    inbox = inbox.lower()
    if 'https://' in inbox or 'http://' in inbox:
        try:
            # `or ''` for the adjacent empty-host case: 'https:///x' PARSES,
            # but .hostname is None, and returning that None broke the same two
            # callers named below -- no exception involved, so the guard alone
            # did not cover it.
            inbox = urlparse(inbox).hostname or ''
        except ValueError:
            # A remote instance chooses its own `inbox` URL -- refresh_instance
            # copies it verbatim out of the JSON that instance served -- and
            # instance_banned() is later handed it whole, so a peer can send a
            # netloc urlparse refuses. '' is the no-domain answer this already
            # returns for input with nothing URL-shaped in it, and is the only
            # one that leaves every caller on a defined path: instance_allowed
            # calls .strip() on the result and instance_banned matches it
            # against a regex, neither of which accepts None.
            return ''
    # The root label's trailing dot is stripped LAST, so it is removed whether
    # the value arrived as a URL or as a bare domain. 'evil.com.' is the
    # fully-qualified form of 'evil.com' -- DNS resolves the two identically and
    # TLS works either way -- but it is a different string, so a banned instance
    # that presented its actor ids as 'https://evil.com./users/x' missed its own
    # row in banned_instances and federated normally. The allowlist direction
    # failed safe (an unrecognised string is simply not on the list), which is
    # why this only ever showed up as a ban bypass.
    return inbox.rstrip('.')


def awaken_dormant_instance(instance):
    """Decide whether a dormant instance is due another try, called from the
    inbox when something arrives from it.

    `start_trying_again` is meaningful only while the instance is dormant: it is
    the time the wait ends. D1380 -- waking the instance left the timestamp
    behind, so the value outlived the dormancy that produced it, and two
    different readers then believed it:

    * this function. `Instance.update_dormant_gone` sets `dormant = True` without
      setting the timestamp, so on the SECOND dormancy the `else` arm ran against
      a value already in the past and woke the instance immediately. The backoff
      applied once per instance, ever. Measured -- `dormant=False` straight out of
      a fresh dormancy, with `sta` still holding the previous one's time;
    * `app/shared/tasks/maintenance.py:435`, which gives up on
      `dormant == True, start_trying_again < five_days_ago`. A stale value is
      older than five days by definition, so an instance that was dormant once
      months ago and has a brief failure spell now is marked `gone_forever` on the
      next maintenance pass -- never delivered to again -- without having waited
      five days for anything. Measured: the query matches.

    Clearing it on wake restores the invariant, and both readers then behave: the
    next dormancy recomputes the backoff from `failures`, and `NULL < x` is NULL,
    so the give-up query cannot match an instance that is not actually waiting.
    """
    if instance and not instance.gone_forever:
        if instance.dormant:
            if instance.start_trying_again is None:
                instance.start_trying_again = utcnow() + timedelta(seconds=instance.failures ** 4)
                db.session.commit()
            else:
                if instance.start_trying_again < utcnow():
                    instance.dormant = False
                    instance.start_trying_again = None
                    db.session.commit()
        # give up after ~5 days of trying
        if instance.start_trying_again and utcnow() + timedelta(days=5) < instance.start_trying_again:
            instance.gone_forever = True
            instance.dormant = True
            db.session.commit()


def shorten_number(number) -> str:
    if number < 1000:
        return str(number)
    elif number < 1000000:
        return f'{number / 1000:.1f}k'
    else:
        return f'{number / 1000000:.1f}M'


@cache.memoize(timeout=300)
def user_filters_home(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_home=True).filter(
        or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:  # type == 1 means hide completely. These posts are excluded from output by the jinja template
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def user_filters_posts(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_posts=True).filter(
        or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def user_filters_replies(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_replies=True).filter(
        or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:
            result['-1'].update(keywords)
    return result


def reply_filter_keyword(post_reply) -> str | None:
    """The keyword of the viewer's reply filters that `post_reply` matches, or None.

    R168 (owner ruling): the reader views of post/_post_reply_teaser.html render a match as a collapsed
    'Filtered: <keyword>' stub, 'hide completely' filters included; admin and moderation views pass
    no_content_filter. `PostReply.blocked_by_content_filter` decides the match, as Post's does for posts.
    """
    if not current_user.is_authenticated:
        return None
    for name, keywords in user_filters_replies(current_user.id).items():
        for keyword in sorted(keywords):
            if post_reply.blocked_by_content_filter({name: [keyword]}, current_user.id):
                return keyword
    return None


@cache.memoize(timeout=300)
def user_filters_languages(user_id):
    user = db.session.get(User, user_id)
    if user.read_language_ids and len(user.read_language_ids) > 0:
        return user.read_language_ids
    else:
        return None


@cache.memoize(timeout=300)
def moderating_communities(user_id) -> List[Community]:
    if user_id is None or user_id == 0:
        return []
    communities = Community.query.join(CommunityMember, Community.id == CommunityMember.community_id). \
        filter(Community.banned == False). \
        filter(or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True)). \
        filter(CommunityMember.is_banned == False). \
        filter(CommunityMember.user_id == user_id).order_by(Community.title).all()

    # Track display names to identify duplicates
    display_name_counts = {}
    for community in communities:
        display_name = community.title
        display_name_counts[display_name] = display_name_counts.get(display_name, 0) + 1

    # Flag communities as duplicates if their display name appears more than once
    for community in communities:
        community.is_duplicate = display_name_counts[community.title] > 1

    return communities


@cache.memoize(timeout=300)
def moderating_communities_ids(user_id) -> List[int]:
    """
    Raw SQL version of moderating_communities() that returns community IDs instead of full objects.
    """
    if user_id is None or user_id == 0:
        return []

    sql = text("""
        SELECT c.id
        FROM community c
        JOIN community_member cm ON c.id = cm.community_id
        WHERE c.banned = false
          AND (cm.is_moderator = true OR cm.is_owner = true)
          AND cm.is_banned = false
          AND cm.user_id = :user_id
        ORDER BY c.title
    """)

    return db.session.execute(sql, {'user_id': user_id}).scalars().all()


@cache.memoize(timeout=86400)
def moderating_communities_ids_all_users() -> dict[int, List[int]]:
    """Returns dict mapping user_id to list of community_ids they moderate."""
    rows = db.session.execute(text("""
        SELECT cm.user_id, ARRAY_AGG(c.id ORDER BY c.title) as community_ids
        FROM community c
        JOIN community_member cm ON c.id = cm.community_id
        WHERE c.banned = false
          AND (cm.is_moderator = true OR cm.is_owner = true)
          AND cm.is_banned = false
        GROUP BY cm.user_id
    """)).fetchall()

    return {user_id: list(community_ids) for user_id, community_ids in rows}


@cache.memoize(timeout=300)
def joined_communities(user_id) -> List[Community]:
    if user_id is None or user_id == 0:
        return []
    banned_instance_ids = banned_instances(user_id)
    communities = Community.query.join(CommunityMember, Community.id == CommunityMember.community_id). \
        filter(Community.banned == False). \
        filter(CommunityMember.is_moderator == False, CommunityMember.is_owner == False). \
        filter(CommunityMember.is_banned == False). \
        filter(CommunityMember.user_id == user_id)

    if banned_instance_ids:
        communities = communities.filter(Community.instance_id.notin_(banned_instances(user_id)))

    communities = communities.order_by(Community.title).all()

    # track display names to identify duplicates
    display_name_counts = {}
    for community in communities:
        display_name = community.title
        display_name_counts[display_name] = display_name_counts.get(display_name, 0) + 1

    # flag communities as duplicates if their display name appears more than once
    for community in communities:
        community.is_duplicate = display_name_counts[community.title] > 1

    return communities


def joined_or_modding_communities(user_id):
    if user_id is None or user_id == 0:
        return []
    return db.session.execute(text(
        'SELECT c.id FROM "community" as c INNER JOIN "community_member" as cm on c.id = cm.community_id WHERE c.banned = false AND cm.user_id = :user_id'),
                              {'user_id': user_id}).scalars().all()


def pending_communities(user_id):
    if user_id is None or user_id == 0:
        return []
    result = []
    for join_request in CommunityJoinRequest.query.filter_by(user_id=user_id).all():
        result.append(join_request.community_id)
    return result


@cache.memoize(timeout=3000)
def menu_topics():
    return Topic.query.filter(Topic.parent_id == None).order_by(Topic.name).all()


@cache.memoize(timeout=3000)
def menu_instance_feeds():
    return Feed.query.filter(Feed.parent_feed_id == None).filter(Feed.is_instance_feed == True).order_by(
        Feed.name).all()


# @cache.memoize(timeout=3000)
def menu_my_feeds(user_id):
    return Feed.query.filter(Feed.parent_feed_id == None).filter(Feed.user_id == user_id).order_by(Feed.name).all()


@cache.memoize(timeout=3000)
def menu_subscribed_feeds(user_id):
    return Feed.query.join(FeedMember, Feed.id == FeedMember.feed_id).filter(FeedMember.user_id == user_id).filter_by(
        is_owner=False).order_by(Feed.name).all()


# @cache.memoize(timeout=3000)
def subscribed_feeds(user_id: int) -> List[int]:
    if user_id is None or user_id == 0:
        return []
    return [feed.id for feed in
            Feed.query.join(FeedMember, Feed.id == FeedMember.feed_id).filter(FeedMember.user_id == user_id)]


@cache.memoize(timeout=300)
def community_moderators(community_id):
    mods = CommunityMember.query.filter((CommunityMember.community_id == community_id) &
                                        (or_(
                                            CommunityMember.is_owner,
                                            CommunityMember.is_moderator
                                        ))
                                        ).all()
    # D174: only real moderator rows; community.user_id without one is not synthesised in
    return mods


def finalize_user_setup(user):
    from app.activitypub.signature import RsaKeys  # cycle: app.activitypub.signature imports from this module
    user.verified = True
    user.last_seen = utcnow()
    if user.private_key is None and user.public_key is None:
        private_key, public_key = RsaKeys.generate_keypair()
        user.private_key = private_key
        user.public_key = public_key

    # Only set AP profile IDs if they haven't been set already
    if user.ap_profile_id is None:
        user.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/{user.user_name}".lower()
        user.ap_public_url = f"{current_app.config['SERVER_URL']}/u/{user.user_name}"
        user.ap_inbox_url = f"{current_app.config['SERVER_URL']}/u/{user.user_name.lower()}/inbox"

    # find all notifications from this registration and mark them as read
    #
    # D1360. `Notification.read == False` is part of the WHERE now: without it the
    # update returned every notification of this type from this author, READ ONES
    # INCLUDED, and each of those took another one off the admin's count -- so
    # approving a registration whose notification had already been read left the
    # count at -1. And the decrement itself is `decrement_unread_counts`, which
    # counts the ids rather than treating them as a set.
    unread_notification_users = db.session.scalars(
        update(Notification)
            .where(Notification.notif_type == NOTIF_REGISTRATION, Notification.author_id == user.id,
                   Notification.read == False)  # noqa: E712
            .values({Notification.read: True})
            .returning(Notification.user_id)
    ).all()
    decrement_unread_counts(unread_notification_users)

    db.session.commit()

    # fire hook for plugins to use upon a new user
    plugins.fire_hook("new_user", user)


def notification_subscribers(entity_id: int, entity_type: int) -> List[int]:
    return list(db.session.execute(
        text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :entity_id AND type = :type '),
        {'entity_id': entity_id, 'type': entity_type}).scalars())


def num_topics() -> int:
    return db.session.execute(text('SELECT COUNT(*) as c FROM "topic"')).scalar_one()


def num_feeds() -> int:
    return db.session.execute(text('SELECT COUNT(*) as c FROM "feed"')).scalar_one()


# topics, in a tree
def topic_tree() -> List:
    topics = Topic.query.order_by(Topic.name)

    topics_dict = {topic.id: {'topic': topic, 'children': []} for topic in topics.all()}

    for topic in topics:
        if topic.parent_id is not None:
            parent_topic = topics_dict.get(topic.parent_id)
            if parent_topic:
                parent_topic['children'].append(topics_dict[topic.id])

    # A topic whose parent row is gone is shown at the top rather than not at
    # all (D1310): the tree used to root on `parent_id is None` alone, so a
    # dangling parent_id -- which nothing in the schema forbids, the column
    # being a plain Integer -- hid the topic and everything under it.
    return [topic for topic in topics_dict.values()
            if topic['topic'].parent_id is None or topic['topic'].parent_id not in topics_dict]


# feeds, in a tree
def feed_readable_by(feed: Feed, user_id) -> bool:
    """Whether the holder of `user_id` may see what is inside `feed`.

    D1394. One rule for every caller that takes a feed id from the request.
    `show_feed` serves a private feed to its owner and its members only, and
    `/f/<name>/following`, `/f/<name>/outbox` and the API's `get_feed` refuse one
    outright -- but nine other readers of a caller-supplied feed id asked
    nothing, and each of them reports the feed's community membership in one
    form or another.

    `user_id` is the id of the account making the request, or None for an
    anonymous one; callers in the web app pass `current_user.id` and callers in
    the API pass the id they authorised, because `user` is not bound there
    unless a token was presented.

    `>= SUBSCRIPTION_MEMBER`, not a bare truth test: `subscribed()` answers -1
    for an unapproved join request and -2 for a member the owner banned, and
    both of those are truthy. `show_feed` tested truth, so asking to join a
    private feed was enough to read it and being thrown out of one did not stop
    you -- which is the same reading the rest of the codebase avoids by
    comparing (app/feed/routes.py:134, :781, app/community/routes.py:229).
    """
    if feed is None:
        return False
    if feed.public:
        return True
    if not user_id:
        return False
    if feed.user_id == user_id:
        return True
    return feed.subscribed(user_id) >= SUBSCRIPTION_MEMBER


def feed_tree(user_id) -> List[dict]:
    feeds = Feed.query.filter(Feed.user_id == user_id).order_by(Feed.name)

    feeds_dict = {feed.id: {'feed': feed, 'children': []} for feed in feeds.all()}

    for feed in feeds:
        if feed.parent_feed_id is not None:
            parent_feed = feeds_dict.get(feed.parent_feed_id)
            if parent_feed:
                parent_feed['children'].append(feeds_dict[feed.id])

    return [feed for feed in feeds_dict.values() if feed['feed'].parent_feed_id is None]


def feed_tree_public(search_param=None) -> List[dict]:
    if search_param:
        feeds = Feed.query.filter(Feed.public == True).filter(Feed.title.ilike(f"%{search_param}%")).order_by(Feed.title)
    else:
        feeds = Feed.query.filter(Feed.public == True).order_by(Feed.title)

    feeds_dict = {feed.id: {'feed': feed, 'children': []} for feed in feeds.all()}

    for feed in feeds:
        if feed.parent_feed_id is not None:
            parent_feed = feeds_dict.get(feed.parent_feed_id)
            if parent_feed:
                parent_feed['children'].append(feeds_dict[feed.id])

    return [feed for feed in feeds_dict.values() if feed['feed'].parent_feed_id is None]


@cache.memoize(timeout=600)
def opengraph_parse(url):
    if url == '':
        return None
    if '?' in url:
        url = url.split('?')
        url = url[0]
    try:
        return parse_page(url)
    except Exception:
        return None


# The extensions a remote thumbnail may be written under. Taken from
# `app/community/util.py:allowed_extensions`, which is what PieFed accepts from
# its own users, plus the two Pillow reads that are not in that list. Anything
# else becomes '.img': see D1327 in `url_to_thumbnail_file`.
allowed_thumbnail_extensions = ('.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic',
                                '.mpo', '.avif', '.svg', '.bmp', '.tiff')


def url_to_thumbnail_file(filename) -> File:
    if is_invalid_get_request_uri(filename):
        return None
    try:
        timeout = 15 if 'washingtonpost.com' in filename else 5  # Washington Post is really slow for some reason
        response = httpx_client.get(filename, timeout=timeout)
    except:
        return None

    if response.status_code == 200:
        # D1427. Case-folded once, because RFC 9110 section 8.3.1 makes a media
        # type and its subtype case-insensitive and both readings below used to
        # be case-sensitive. `Content-Type: IMAGE/PNG` failed `startswith` and
        # the whole thumbnail was silently discarded, and `image/SVG+XML` --
        # which is what a peer serving an SVG may legitimately send -- failed the
        # `"svg" in content_type` test, so the sanitiser was not reached and the
        # extension fell through to `.img`, where Pillow refused it. Both
        # spellings are a peer's, so neither should decide whether the fetch
        # works.
        content_type = response.headers.get('content-type')
        if content_type:
            content_type = content_type.lower()
        if content_type and content_type.startswith('image'):
            # Sanitize SVG files to remove potentially dangerous elements.
            #
            # sanitize_svg_bytes raises ValueError on input it will not sanitize
            # -- an entity declaration, a UTF-16 encoding, or over MAX_SVG_SIZE.
            # A remote thumbnail we cannot sanitize is DROPPED, exactly as a
            # thumbnail we could not fetch is dropped a few lines above: this
            # function reports failure by returning None, and letting the
            # ValueError out instead would crash edit_post, which calls this and
            # does not catch it. The two sanitize_svg_bytes calls are the only
            # statements below that raise ValueError.
            response_content = response.content
            try:
                if "svg" in content_type:
                    response_content = sanitize_svg_bytes(response_content)
                    file_extension = final_ext = ".svg"
                else:
                    # Generate file extension from mime type
                    if ';' in content_type:
                        content_type_parts = content_type.split(';')
                        content_type = content_type_parts[0]
                    # D1327. This used to be `'.' + content_type.split('/')[-1]`,
                    # so the REMOTE SERVER chose the extension of a file written
                    # into `app/static/media/posts`, which this instance serves.
                    # `Content-Type: image/html` passed the `startswith('image')`
                    # gate above and wrote the peer's body to `<name>.html` --
                    # measured, `<html><script>alert(document.domain)</script>`
                    # served from our own origin, which is stored XSS. Other
                    # measured spellings: `.php`, an extension with a space in
                    # it, a bare `.`, and a 200-character one.
                    #
                    # An extension PieFed itself accepts is kept, because Pillow
                    # and the code below read it; anything else becomes `.img`,
                    # which no web server serves as script. Pillow sniffs the
                    # CONTENT, so a format it supports under a content type not
                    # listed here still works -- and one it does not support is
                    # dropped below either way.
                    #
                    # The `else` this replaces was dead: `str.split` never
                    # returns an empty list, so `if content_type_parts:` was
                    # always true and the url-derived fallback never ran.
                    subtype = content_type.split('/')[-1].strip().lower()
                    file_extension = '.jpg' if subtype == 'jpeg' else '.' + subtype
                    if file_extension not in allowed_thumbnail_extensions:
                        file_extension = '.img'

                # D1428. A second `if file_extension == '.svg' and "svg" not in
                # content_type: sanitize` stood here, and with D1427's case-fold
                # above it is unreachable: `file_extension` is `'.svg'` only when
                # `subtype` is `svg`, `subtype` is a substring of `content_type`,
                # and a `content_type` containing `svg` takes the arm above
                # instead. It was reachable before the case-fold, for the single
                # spelling `image/SVG`, which now takes that arm too. Every SVG
                # still passes through `sanitize_svg_bytes` exactly once.
            except ValueError as e:
                current_app.logger.info(f"Discarding unsanitizable remote SVG {filename}: {e}")
                response.close()
                return None

            new_filename = gibberish(15)
            if store_files_in_s3():
                directory = 'app/static/tmp'
            else:
                directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
            ensure_directory_exists(directory)
            temp_file_path = os.path.join(directory, new_filename + file_extension)
            # D1345. Keep the name the peer's bytes were written under. When the
            # configured medium format differs from the fetched one, the resize
            # below moves `temp_file_path` to a new extension, and the original
            # was then orphaned AT WRITE TIME: no column names it, so nothing
            # later can find it. Measured, for one remote PNG with
            # MEDIA_IMAGE_MEDIUM_FORMAT=WEBP (the default):
            #
            #   posts/Rq/Mk/RqMkzFBw22MVyW1.png       <- left for ever
            #   posts/Rq/Mk/RqMkzFBw22MVyW1.webp      <- thumbnail_path
            #   posts/Rq/Mk/RqMkzFBw22MVyW1_512.webp  <- file_path
            #
            # On the S3 arm the leftover lands in `app/static/tmp`, where
            # `clean_up_tmp` sweeps only eight extensions after a day, so `.img`
            # -- D1327's fallback for a content type PieFed does not accept --
            # and `.avif`, `.bmp`, `.tiff`, `.mpo` were never swept at all. Both
            # directories are under `app/static`, which this instance serves.
            original_file_path = temp_file_path

            with open(temp_file_path, 'wb') as f:
                f.write(response_content)
            response.close()

            if file_extension != ".svg":
                # Use environment variables to determine URL thumbnail

                medium_image_format = current_app.config['MEDIA_IMAGE_MEDIUM_FORMAT']
                medium_image_quality = current_app.config['MEDIA_IMAGE_MEDIUM_QUALITY']

                final_ext = file_extension.lower()

                if medium_image_format == 'AVIF':
                    import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

                # D1328. This used to run unguarded, so a body Pillow refuses --
                # anything that is not an image, and a peer chooses what it serves --
                # raised `UnidentifiedImageError` out of this function AND LEFT THE
                # DOWNLOAD ON DISK, under `app/static/media/posts`, which this
                # instance serves. That is how the html measured in D1327 stayed
                # reachable. The exception also reached `edit_post`, which does not
                # catch it.
                #
                # A thumbnail that cannot be processed is dropped, exactly as one
                # that cannot be fetched or sanitized is, and its bytes go with it.
                try:
                    with Image.open(temp_file_path) as img:
                        img = ImageOps.exif_transpose(img)
                        img = img.convert('RGB' if (medium_image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']) else 'RGBA')

                        # Create 170px thumbnail
                        img_170 = img.copy()
                        img_170.thumbnail((170, 170), resample=Image.LANCZOS)

                        kwargs = {}
                        if medium_image_format:
                            kwargs['format'] = medium_image_format.upper()
                            final_ext = '.' + medium_image_format.lower()
                            temp_file_path = os.path.splitext(temp_file_path)[0] + final_ext
                        if medium_image_quality:
                            kwargs['quality'] = int(medium_image_quality)

                        img_170.save(temp_file_path, optimize=True, **kwargs)
                        thumbnail_width = img_170.width
                        thumbnail_height = img_170.height

                        # Create 512px thumbnail
                        img_512 = img.copy()
                        img_512.thumbnail((512, 512), resample=Image.LANCZOS)

                        # Create filename for 512px thumbnail
                        temp_file_path_512 = os.path.splitext(temp_file_path)[0] + '_512' + final_ext
                        img_512.save(temp_file_path_512, optimize=True, **kwargs)
                        thumbnail_512_width = img_512.width
                        thumbnail_512_height = img_512.height
                except Exception as e:
                    current_app.logger.info(
                        f'Discarding remote thumbnail {filename}: {e}')
                    for path in (original_file_path, temp_file_path,
                                 os.path.splitext(temp_file_path)[0] + '_512' + final_ext):
                        if os.path.isfile(path):
                            os.unlink(path)
                    return None

                # The resize wrote its own files, so the peer's original bytes are
                # no longer wanted (D1345). `os.path.isfile` rather than a plain
                # unlink because a format that matched the fetched extension leaves
                # the two paths equal, and that one file IS the thumbnail.
                if original_file_path != temp_file_path and \
                        os.path.isfile(original_file_path):
                    os.unlink(original_file_path)
            else:
                thumbnail_width = thumbnail_height = None
                thumbnail_512_width = thumbnail_512_height = None

            if store_files_in_s3():
                content_type = guess_mime_type(temp_file_path)
                extra_args = {'ContentType': content_type}
                if current_app.config.get('S3_STORAGE_CLASS'):
                    extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                if current_app.config.get('S3_PUBLIC_ACL'):
                    extra_args['ACL'] = 'public-read'
                boto3_session = boto3.session.Session()
                s3 = boto3_session.client(
                    service_name='s3',
                    region_name=current_app.config['S3_REGION'],
                    endpoint_url=current_app.config['S3_ENDPOINT'],
                    aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                    aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                )
                # Upload 170px thumbnail
                s3.upload_file(temp_file_path, current_app.config['S3_BUCKET'], 'posts/' +
                               new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                               ExtraArgs=extra_args)
                os.unlink(temp_file_path)
                thumbnail_170_url = f"https://{current_app.config['S3_PUBLIC_URL']}/posts/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                    '/' + new_filename + final_ext

                if final_ext != ".svg":
                    # Upload 512px thumbnail
                    s3.upload_file(temp_file_path_512, current_app.config['S3_BUCKET'], 'posts/' +
                                new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + '_512' + final_ext,
                                ExtraArgs=extra_args)
                    os.unlink(temp_file_path_512)
                    thumbnail_512_url = f"https://{current_app.config['S3_PUBLIC_URL']}/posts/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                        '/' + new_filename + '_512' + final_ext
                else:
                    thumbnail_512_url = thumbnail_170_url
            else:
                # For local storage, use the temp file paths as final URLs
                thumbnail_170_url = temp_file_path
                thumbnail_512_url = temp_file_path_512 if not file_extension == ".svg" else temp_file_path
            return File(file_path=thumbnail_512_url, thumbnail_width=thumbnail_width, width=thumbnail_512_width,
                        height=thumbnail_512_height,
                        thumbnail_height=thumbnail_height, thumbnail_path=thumbnail_170_url,
                        source_url=filename)


# By no means is this a complete list, but it is very easy to search for the ones you need later.



def parse_page(page_url):
    '''
    Parses a page, returns a JSON style dictionary of all OG tags found on that page.

    Passing in tags_to_search is optional. By default it will search through KNOWN_OPENGRAPH_TAGS constant, but for the sake of efficiency, you may want to only search for 1 or 2 tags

    Returns False if page is unreadable
    '''

    tags_to_search = [
        "og:site_name",
        "og:title",
        "og:locale",
        "og:type",
        "og:image",
        "og:url",
        "og:image:url",
        "og:image:secure_url",
        "og:image:type",
        "og:image:width",
        "og:image:height",
        "og:image:alt",
        "og:description"
    ]

    #todo: do a head request head_request() and check content-type and size before proceeding

    # read the html from the page
    response = get_request(page_url)

    if response.status_code != 200:
        return False

    if 'text/html' not in response.headers.get('Content-Type', ''):
        return False

    # set up beautiful soup
    soup = BeautifulSoup(response.content, 'html.parser')

    # loop through the known list of opengraph tags, searching for each and appending a dictionary as we go.
    found_tags = {}

    for og_tag in tags_to_search:
        new_found_tag = soup.find("meta", property=og_tag)
        if new_found_tag is not None:
            found_tags[new_found_tag["property"]] = new_found_tag["content"]

    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        found_tags["description"] = desc["content"]
        if "og:description" not in found_tags:
            found_tags["og_description"] = desc["content"]

    if "og:description" in found_tags and "description" not in found_tags:
        found_tags["description"] = found_tags["og:description"]

    if len(found_tags) == 0 or 'og:title' not in found_tags:
        title = soup.find("title")
        if title:
            found_tags['og:title'] = title.get_text()
    return found_tags


def current_theme():
    """ The theme the current user has set, falling back to the site default if none specified or user is not logged in """
    if hasattr(g, 'site'):
        site = g.site
    else:
        site = db.session.get(Site, 1)
    if current_user.is_authenticated:
        if current_user.theme is not None and current_user.theme != '':
            return current_user.theme
        else:
            return site.default_theme if site.default_theme is not None else 'piefed'
    else:
        return site.default_theme if site.default_theme is not None else 'piefed'


def theme_list():
    """ All the themes available, by looking in the templates/themes directory """
    result = []
    for root, dirs, files in os.walk('app/templates/themes'):
        for dir in dirs:
            if os.path.exists(f'app/templates/themes/{dir}/{dir}.json'):
                theme_settings = json.loads(file_get_contents(f'app/templates/themes/{dir}/{dir}.json'))
                result.append((dir, theme_settings['name']))
    return [('piefed', 'PieFed')] + sorted(result)


def sha256_digest(input_string):
    """
    Compute the SHA-256 hash digest of a given string.

    Args:
    - input_string: The string to compute the hash digest for.

    Returns:
    - A hexadecimal string representing the SHA-256 hash digest.
    """
    sha256_hash = hashlib.sha256()
    sha256_hash.update(input_string.encode('utf-8'))
    return sha256_hash.hexdigest()


# still used to hint to a local user that a post to a URL has already been submitted
def remove_tracking_from_link(url):
    if not url:
        # See is_image_url for why this is a conditional. Alone among these
        # helpers this one SURVIVES None today, and only by accident: the
        # bytes netloc b'' can never equal the str 'youtu.be', so the rewrite
        # branch (which does call str methods on the parse result) is never
        # entered. That is one added line away from being a crash, so the
        # passthrough is made explicit rather than left to the accident.
        return url
    try:
        parsed_url = urlparse(url)
    except ValueError:
        # Malformed netloc (unbalanced IPv6 bracket, bad IPv6 literal, or a
        # host urllib rejects under NFKC normalization). Nothing to rewrite --
        # the same answer the else: arm gives for any non-youtu.be link.
        return url

    if parsed_url.netloc == 'youtu.be':
        # Extract video ID
        video_id = parsed_url.path[1:]  # Remove leading slash

        # Preserve 't' parameter if it exists
        query_params = parse_qs(parsed_url.query)
        if 't' in query_params:
            new_query_params = {'t': query_params['t']}
            new_query_string = urlencode(new_query_params, doseq=True)
        else:
            new_query_string = ''

        cleaned_url = f"https://youtube.com/watch?v={video_id}"
        if new_query_string:
            new_query_string = new_query_string.replace('t=', 'start=')
            cleaned_url += f"&{new_query_string}"

        return cleaned_url
    else:
        return url


# Fixes URLs so we're more likely to get a thumbnail from youtube, and more posts from streaming sites are embedded
# Also duplicates link tracking removal from the function above.
def fixup_url(url):
    thumbnail_url = embed_url = url
    if not url:
        # See is_image_url for why this is a conditional. This one crashes
        # before the parse result is ever touched -- len(None) at the peertube
        # length test below is TypeError -- so guarding the urlparse alone
        # could never have covered it. (url, url) is this function's own
        # passthrough, and it is what an empty string already returns.
        return url, url
    try:
        parsed_url = urlparse(url)
    except ValueError:
        # Malformed netloc (unbalanced IPv6 bracket, bad IPv6 literal, or a
        # host urllib rejects under NFKC normalization). Nothing to fix up --
        # the same passthrough this returns for any host outside
        # youtube_domains.
        return url, url

    # fixup embed_url for peertube videos shared outside of the channel
    if len(url) > 25 and url[-25:][:3] == '/w/':
        peertube_domains = db.session.execute(text("SELECT domain FROM instance WHERE software = 'peertube'")).scalars()
        if parsed_url.netloc in peertube_domains:
            try:
                response = get_request(url, headers={'Accept': 'application/activity+json'})
                if response.status_code == 200:
                    try:
                        video_json = response.json()
                        if 'id' in video_json:
                            embed_url = video_json['id']
                        response.close()
                    except (ValueError, TypeError):
                        # ValueError: the body is not JSON at all. httpx's
                        # .json() is json.loads(self.content) over BYTES, so a
                        # malformed body surfaces as json.JSONDecodeError for
                        # some inputs (b'not json') and UnicodeDecodeError for
                        # others (b'\x80\x81\x82'). Both descend from
                        # ValueError; neither descends from the other.
                        # TypeError: the body is valid JSON but not a mapping.
                        # `'id' in None` and `'id' in 5` raise, and a bare
                        # string body that happens to contain "id" gets past
                        # the membership test and raises on the subscript
                        # ("string indices must be integers").
                        response.close()
            except httpx.HTTPError:
                # get_request normalises every transport failure it can raise
                # to httpx.HTTPError: the is_invalid_get_request_uri refusal,
                # httpx.InvalidURL, ValueError, httpx.StreamError, and both
                # retry paths (one re-raises httpx.ReadError, an HTTPError
                # subclass). urlparse's own ValueError cannot reach here -- it
                # is caught at the top of this function.
                pass

    youtube_domains = ['www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtube.com', 'youtu.be']

    if not parsed_url.netloc in youtube_domains:
        return thumbnail_url, embed_url
    else:
        video_id = timestamp = None
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        # Handle YouTube playlists and posts - let them through unmolested
        if path == '/playlist' and 'list' in query_params or path.startswith("/post/"):
            thumbnail_url = ''
            embed_url = url
            return thumbnail_url, embed_url

        if path:
            if path.startswith('/shorts/') and len(path) > 8:
                video_id = path[8:]
            elif path == '/watch' and 'v' in query_params:
                video_id = query_params['v'][0]
            else:
                video_id = path[1:]
        if not video_id:
            return thumbnail_url, embed_url
        if 'start' in query_params:
            timestamp = query_params['start'][0]
        elif 't' in query_params:
            timestamp = query_params['t'][0]

        thumbnail_url = 'https://youtu.be/' + video_id
        embed_url = 'https://www.youtube.com/watch?v=' + video_id
        if timestamp:
            timestamp_param = {'start': timestamp}
            timestamp_query = urlencode(timestamp_param, doseq=True)
            embed_url += f"&{timestamp_query}"

        return thumbnail_url, embed_url


def show_ban_message():
    flash(_('You have been banned.'), 'error')
    logout_user()
    resp = make_response(redirect(url_for('main.index')))
    resp.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
    return resp


# search a sorted list using a binary search. Faster than using 'in' with a unsorted list.
def in_sorted_list(arr, target):
    index = bisect.bisect_left(arr, target)
    return index < len(arr) and arr[index] == target


@cache.memoize(timeout=600)
def recently_upvoted_posts(user_id) -> List[int]:
    post_ids = db.session.execute(
        text('SELECT post_id FROM "post_vote" WHERE user_id = :user_id AND effect > 0 ORDER BY id DESC LIMIT 3000'),
        {'user_id': user_id}).scalars()
    return sorted(post_ids)  # sorted so that in_sorted_list can be used


@cache.memoize(timeout=600)
def recently_downvoted_posts(user_id) -> List[int]:
    post_ids = db.session.execute(
        text('SELECT post_id FROM "post_vote" WHERE user_id = :user_id AND effect < 0 ORDER BY id DESC LIMIT 3000'),
        {'user_id': user_id}).scalars()
    return sorted(post_ids)


@cache.memoize(timeout=600)
def recently_upvoted_post_replies(user_id) -> List[int]:
    reply_ids = db.session.execute(text(
        'SELECT post_reply_id FROM "post_reply_vote" WHERE user_id = :user_id AND effect > 0 ORDER BY id DESC LIMIT 3000'),
                                   {'user_id': user_id}).scalars()
    return sorted(reply_ids)  # sorted so that in_sorted_list can be used


@cache.memoize(timeout=600)
def recently_downvoted_post_replies(user_id) -> List[int]:
    reply_ids = db.session.execute(text(
        'SELECT post_reply_id FROM "post_reply_vote" WHERE user_id = :user_id AND effect < 0 ORDER BY id DESC LIMIT 3000'),
                                   {'user_id': user_id}).scalars()
    return sorted(reply_ids)


def languages_for_form(all_languages=False):
    used_languages = []
    if current_user.is_authenticated:
        if current_user.read_language_ids is None or len(current_user.read_language_ids) == 0:
            all_languages=True
        # if they've defined which languages they read, only present those as options for writing.
        # otherwise, present their most recently used languages and then all other languages
        if current_user.read_language_ids is None or len(current_user.read_language_ids) == 0:
            recently_used_language_ids = db.session.execute(text("""SELECT language_id
                                                                    FROM (
                                                                        SELECT language_id, posted_at
                                                                        FROM "post"
                                                                        WHERE user_id = :user_id
                                                                        UNION ALL
                                                                        SELECT language_id, posted_at
                                                                        FROM "post_reply"
                                                                        WHERE user_id = :user_id
                                                                    ) AS subquery
                                                                    GROUP BY language_id
                                                                    ORDER BY MAX(posted_at) DESC
                                                                    LIMIT 10"""),
                                                            {'user_id': current_user.id}).scalars().all()

            # note: recently_used_language_ids is now a List, ordered with the most recently used at the top
            # but Language.query.filter(Language.id.in_(recently_used_language_ids)) isn't guaranteed to return
            # language results in the same order as that List :(
            for language_id in recently_used_language_ids:
                if language_id is not None:
                    used_languages.append((language_id, ""))
        else:
            for language in Language.query.filter(Language.id.in_(tuple(current_user.read_language_ids))).order_by(Language.name).all():
                used_languages.append((language.id, language.name))

        if not used_languages:
            id = site_language_id()
            if id:
                used_languages.append((id, ""))

    other_languages = []
    for language in Language.query.order_by(Language.name).all():
        try:
            i = used_languages.index((language.id, ""))
            used_languages[i] = (language.id, language.name)
        except:
            if all_languages and language.code != "und":
                other_languages.append((language.id, language.name))

    return used_languages + other_languages


def flair_for_form(community_id):
    result = []
    for flair in CommunityFlair.query.filter(CommunityFlair.community_id == community_id).order_by(CommunityFlair.flair):
        result.append((flair.id, flair.flair))
    return result


def find_flair_id(flair: str, community_id: int) -> int | None:
    flair = CommunityFlair.query.filter(CommunityFlair.community_id == community_id,
                                        CommunityFlair.flair == flair.strip()).first()
    if flair:
        return flair.id
    else:
        return None


def site_language_id(site=None):
    if site is not None and site.language_id:
        return site.language_id
    if g and hasattr(g, 'site') and g.site.language_id:
        return g.site.language_id
    else:
        english = db.session.query(Language).filter(Language.code == 'en').first()
        return english.id if english else None


def site_language_code(site=None):
    if site is not None and site.language_id:
        return db.session.get(Language, site.language_id).code
    if g and hasattr(g, 'site') and g.site.language_id:
        return db.session.get(Language, g.site.language_id).code
    else:
        english = db.session.query(Language).filter(Language.code == 'en').first()
        return english.code if english else ''


def read_language_choices() -> List[tuple]:
    result = []
    for language in Language.query.order_by(Language.name).all():
        result.append((language.id, language.name))
    return result


def actor_contains_blocked_words(actor: str):
    """Whether an actor name is one the admin has blocked.

    D1388. `'' in anything` is True, and nothing skipped an empty entry -- so one
    blank line in the setting blocked EVERY actor. A textarea submits `'spam\r\n'`
    for one word and an Enter, and `'\r'.strip()` is `''`, so the most ordinary
    way to fill this setting was enough. Measured against an innocent actor:

        'spam'          not blocked   (correct)
        'spam\n'        BLOCKED
        'spam\r\n'      BLOCKED
        '\nspam'        BLOCKED
        'spam\n\nscam'  BLOCKED

    Three callers made that a silent outage: `find_actor_or_create`
    (app/activitypub/actor.py:72) resolves no remote actor at all,
    `app/auth/util.py:219` refuses every registration, and
    `app/auth/oauth_util.py:277` every OAuth signup. `blocked_phrases()` in this
    same module already skips empty entries with `if phrase != ''`.
    """
    actor = actor.lower().strip()
    blocked_words = get_setting('actor_blocked_words')
    if blocked_words and blocked_words.strip() != '':
        for blocked_word in blocked_words.split('\n'):
            blocked_word = blocked_word.lower().strip()
            if not blocked_word:
                continue
            if blocked_word in actor:
                return True
    return False


def actor_profile_contains_blocked_words(user: User) -> bool:
    if user is None or not isinstance(user, User):
        return False
    blocked_words = get_setting('actor_bio_blocked_words')
    if blocked_words and blocked_words.strip() != '':
        for blocked_word in blocked_words.split('\n'):
            blocked_word = blocked_word.lower().strip()
            # D1388, as in actor_contains_blocked_words above: an empty entry
            # matched every bio, so `find_actor_or_create` refused every user who
            # had one.
            if not blocked_word:
                continue
            if user.about_html and blocked_word in user.about_html.lower():
                return True
    return False


def add_to_modlog(action: str, actor: User, target_user: User = None, reason: str = '',
                  community: Community = None, post: Post = None, reply: PostReply = None,
                  link: str = '', link_text: str = ''):
    """ Adds a new entry to the Moderation Log """
    if action not in ModLog.action_map.keys():
        raise Exception('Invalid action: ' + action)
    if actor.is_instance_admin() or actor.is_admin() or actor.is_staff():
        action_type = 'admin'
    else:
        action_type = 'mod'
    community_id = community.id if community else None
    target_user_id = target_user.id if target_user else None
    # The modlog can be public. Content only some viewers may see (D19) is named neither by its title or body nor by its author.
    if any(obj is not None and (obj.visibility or 'public') not in OPEN_VISIBILITIES
           for obj in (post, reply, reply.post if reply else None)):
        link_text = 'followers-only content'  # the target user is kept; readers who are not admins never see it (R3)
    post_id = post.id if post else None
    reply_id = reply.id if reply else None
    reason = shorten_string(reason, 512)
    public = get_setting('public_modlog', False) and action not in ModLog.admin_only_actions
    db.session.add(ModLog(user_id=actor.id, type=action_type, action=action, target_user_id=target_user_id,
                          community_id=community_id, post_id=post_id, reply_id=reply_id,
                          reason=reason, link=link, link_text=link_text, public=public))
    db.session.commit()


def authorise_api_user(auth, return_type=None, id_match=None) -> User | dict | int:
    if not auth:
        raise Exception('incorrect_login')
    if not auth.startswith('Bearer '):
        raise Exception('incorrect_login')

    token = auth[7:]  # remove 'Bearer '

    try:
        decoded = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
    except InvalidTokenError as e:
        # InvalidTokenError, not DecodeError: PyJWT raises ExpiredSignatureError
        # for an expired token and ImmatureSignatureError for one dated in the
        # future, and both are SIBLINGS of DecodeError under InvalidTokenError
        # rather than subclasses of it. Catching only DecodeError let them
        # escape as themselves, and app/api/alpha/__init__.py's error handler
        # treats any exception whose message is not exactly 'incorrect_login' as
        # an application error -- logging it and capturing it to Sentry, and
        # echoing its text back to the caller. Expiry is the commonest
        # legitimate rejection there is; it is not an application error.
        #
        # InvalidTokenError covers every way a token itself can be bad
        # (DecodeError, InvalidSignatureError, ExpiredSignatureError,
        # ImmatureSignatureError, InvalidAlgorithmError, MissingRequiredClaimError,
        # and any future sibling) without swallowing InvalidKeyError, which sits
        # outside it and means this server's SECRET_KEY is unusable -- that one
        # IS an application error and must keep escaping.
        #
        # The reason goes to the log, where it is useful; the caller gets the
        # same bare 'incorrect_login' as every other rejection, because the
        # handler compares on that exact string and because telling a caller
        # WHICH check its token failed narrows an attacker's search.
        current_app.logger.info('authorise_api_user: bearer token rejected by jwt.decode: %s: %s',
                                type(e).__name__, e)
        raise Exception('incorrect_login')

    if decoded:
        if RevokedToken.query.filter_by(jti=decoded.get('jti')).first():
            raise Exception('incorrect_login')
        user_id = decoded['sub']
        user = db.session.get(User, user_id)
        if user is None:
            raise Exception('incorrect_login')
        if user.ap_id is not None or not user.verified or user.banned is True or user.deleted is True:
            raise Exception('incorrect_login')
        if user.password_updated_at:
            issued_at_time = decoded['iat']
            password_updated_time = int(user.password_updated_at.timestamp())
            if issued_at_time < password_updated_time:
                raise Exception('incorrect_login')
        if id_match and user.id != id_match:
            raise Exception('incorrect_login')
        if return_type and return_type == 'model':
            return user
        elif return_type and return_type == 'dict':
            user_ban_community_ids = communities_banned_from(user_id)
            followed_community_ids = list(db.session.execute(text(
                'SELECT community_id FROM "community_member" WHERE user_id = :user_id'),
                {'user_id': user_id}).scalars())
            bookmarked_reply_ids = list(db.session.execute(text(
                'SELECT post_reply_id FROM "post_reply_bookmark" WHERE user_id = :user_id'),
                {'user_id': user_id}).scalars())
            blocked_creator_ids = blocked_users(user_id)
            upvoted_reply_ids = recently_upvoted_post_replies(user_id)
            downvoted_reply_ids = recently_downvoted_post_replies(user_id)
            subscribed_reply_ids = list(db.session.execute(text(
                'SELECT entity_id FROM "notification_subscription" WHERE type = :type and user_id = :user_id'),
                {'type': NOTIF_REPLY, 'user_id': user_id}).scalars())
            moderated_community_ids = list(db.session.execute(text(
                'SELECT community_id FROM "community_member" WHERE user_id = :user_id AND is_moderator = true'),
                {'user_id': user_id}).scalars())
            user_dict = {
                'id': user.id,
                'user_ban_community_ids': user_ban_community_ids,
                'followed_community_ids': followed_community_ids,
                'bookmarked_reply_ids': bookmarked_reply_ids,
                'blocked_creator_ids': blocked_creator_ids,
                'upvoted_reply_ids': upvoted_reply_ids,
                'downvoted_reply_ids': downvoted_reply_ids,
                'subscribed_reply_ids': subscribed_reply_ids,
                'moderated_community_ids': moderated_community_ids
            }
            return user_dict
        else:
            return user.id


# Set up a new SQLAlchemy session specifically for Celery tasks
def get_task_session() -> Session:
    # Use the same engine as the main app, but create an independent session
    return Session(bind=db.engine)


@contextmanager
def patch_db_session(task_session):
    """Temporarily replace db.session with task_session for functions that use it internally"""

    # Only patch if we're not in a Flask request context (i.e., in a Celery worker)
    if has_request_context():
        # In Flask request context, don't patch - just use the existing session
        yield
        return

    original_session = db.session

    # Create a wrapper that makes the task session work with Flask-SQLAlchemy's Model.query
    class SessionWrapper:
        def __init__(self, session):  # noqa: F811
            self._session = session

        def __call__(self):
            return self._session

        def __getattr__(self, name):
            # Handle scoped session methods that don't exist on regular Session
            if name == 'remove':
                # For task sessions, we don't want to remove since we manage the lifecycle
                return lambda: None
            return getattr(self._session, name)

    db.session = SessionWrapper(task_session)
    try:
        yield
    finally:
        db.session = original_session


def get_redis_connection(connection_string=None) -> redis.Redis:
    if connection_string is None:
        connection_string = current_app.config['CACHE_REDIS_URL']
    if connection_string.startswith('unix://'):
        unix_socket_path, db, password = parse_redis_pipe_string(connection_string)
        return redis.Redis(unix_socket_path=unix_socket_path, db=db, password=password, decode_responses=True)
    else:
        host, port, db, password = parse_redis_socket_string(connection_string)
        return redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)


def parse_redis_pipe_string(connection_string: str):
    if connection_string.startswith('unix://'):
        # Parse the connection string
        parsed_url = urlparse(connection_string)

        # Extract the path (Unix socket path)
        unix_socket_path = parsed_url.path

        # Extract query parameters (if any)
        query_params = parse_qs(parsed_url.query)

        # Extract database number (default to 0 if not provided)
        db = int(query_params.get('db', [0])[0])

        # Extract password (if provided)
        password = query_params.get('password', [None])[0]

        return unix_socket_path, db, password


def parse_redis_socket_string(connection_string: str):
    # Parse the connection string
    parsed_url = urlparse(connection_string)

    # Extract password
    password = parsed_url.password

    # Extract host and port
    host = parsed_url.hostname
    port = parsed_url.port

    # Extract database number (default to 0 if not provided)
    db_num = int(parsed_url.path.lstrip('/') or 0)

    return host, port, db_num, password


def download_defeds(defederation_subscription_id: int, domain: str, replace: bool = False):
    if current_app.debug:
        download_defeds_worker(defederation_subscription_id, domain, replace)
    else:
        download_defeds_worker.delay(defederation_subscription_id, domain, replace)


@celery.task
def download_defeds_worker(defederation_subscription_id: int, domain: str, replace: bool = False):
    session = get_task_session()  # noqa: F811
    try:
        allowed_instances = [instance.domain for instance in session.query(AllowedInstances).all()]
        defederation_list = retrieve_defederation_list(domain)
        if defederation_list is None:
            # Could not fetch the list: that is not an empty list, so the
            # subscription's existing bans stay as they are.
            current_app.logger.warning(f'Could not download the defederation list from {domain}')
            return
        # The periodic sync replaces the subscription's bans. The old rows go in
        # the same transaction the new ones arrive in, and only once the new
        # list is in hand, so a failed download never leaves instances unbanned.
        if replace:
            session.query(BannedInstances).filter(
                BannedInstances.subscription_id == defederation_subscription_id).delete()
        # A domain the list names twice, or a subscription downloaded twice
        # from the admin screen, used to insert a second row: nothing here
        # looked for one, and `BannedInstances.domain` is not unique.
        already = {row.domain for row in session.query(BannedInstances).filter(
            BannedInstances.subscription_id == defederation_subscription_id)}
        for defederation_url in defederation_list:
            if defederation_url not in allowed_instances and defederation_url not in already:
                already.add(defederation_url)
                session.add(BannedInstances(domain=defederation_url, reason='auto', subscription_id=defederation_subscription_id))
        session.commit()
    finally:
        session.close()


def retrieve_defederation_list(domain: str) -> Optional[List[str]]:
    """The domains `domain` blocks, or None when the list could not be fetched
    (unreachable, not 200, not JSON, or not the expected shape)."""
    result = []
    software = instance_software(domain)
    if software == 'lemmy' or software == 'piefed' or software == 'pylova':
        try:
            response = get_request(f'https://{domain}/api/v3/federated_instances')
        except:
            response = None
        if not response or response.status_code != 200:
            return None
        # Everything below is another instance's answer, and this runs in a
        # Celery worker: a missing key was a KeyError that stopped the
        # subscription updating and left the task session open.
        try:
            instance_data = response.json()
        except ValueError:
            return None
        if not isinstance(instance_data, dict):
            return None
        blocked = instance_data.get('federated_instances') or {}
        if not isinstance(blocked, dict) or not isinstance(blocked.get('blocked') or [], list):
            return None
        for row in blocked.get('blocked') or []:
            if isinstance(row, dict) and row.get('domain'):
                result.append(row['domain'])
    else:  # Assume mastodon-compatible API
        try:
            response = get_request(f'https://{domain}/api/v1/instance/domain_blocks')
        except:
            response = None
        if not response or response.status_code != 200:
            return None
        try:
            instance_data = response.json()
        except ValueError:
            return None
        if not isinstance(instance_data, list):
            return None
        for row in instance_data:
            if isinstance(row, dict) and row.get('domain'):
                result.append(row['domain'])

    return result


def instance_software(domain: str):
    instance = Instance.query.filter(Instance.domain == domain).first()
    return instance.software.lower() if instance else ''


# ----------------------------------------------------------------------
# Return contents of referrer with a fallback
def referrer(default: str = None) -> str:
    """Where to send the user next, from the first source that is safe to use.

    All three sources are user-controlled and ~29 call sites hand the result
    straight to `redirect()`, so each one is checked with is_safe_redirect_target
    -- the same function `back()` uses. `?next=` and the posted `referrer` field
    used to be returned verbatim (`?next=https://evil.example` was an open
    redirect) and the header was guarded only by a bypassable substring test.

    A source that fails the check is skipped, not raised on: the next source is
    tried, and finally the default.
    """
    for candidate in (request.args.get('next'), request.form.get('referrer'), request.referrer):
        if candidate and is_safe_redirect_target(candidate):
            return candidate
    if default:
        return default
    return url_for('main.index')


def create_captcha(length=4):
    code = ""
    for i in range(length):
        code += str(random.choice(['2', '3', '4', '5', '6', '8', '9']))

    imagedata = ImageCaptcha().generate(code)
    image = "data:image/jpeg;base64," + base64.encodebytes(imagedata.read()).decode()

    audiodata = AudioCaptcha().generate(code)
    audio = "data:audio/wav;base64," + base64.encodebytes(audiodata).decode()

    uuid = os.urandom(12).hex()

    redis_client = get_redis_connection()
    redis_client.set("captcha_" + uuid, code, ex=30 * 60)

    return {"uuid": uuid, "audio": audio, "image": image}


def decode_captcha(uuid: str, code: str):
    re_uuid = re.compile(r'^([a-fA-F0-9]{24})$')
    try:
        if not re.fullmatch(re_uuid, uuid):
            return False
    except TypeError:
        return False

    redis_client = get_redis_connection()
    # GETDEL reads and deletes in a single atomic command (requires Redis >= 6.2,
    # already the version pinned by compose.yaml/compose.dev.yaml/compose.test.yaml).
    # A separate get() then delete() left a TOCTOU window: two concurrent callers
    # could both read the code before either deleted it, so one solved captcha
    # could validate more than once.
    saved_code = redis_client.getdel("captcha_" + uuid)
    if saved_code is not None and code is not None:
        if code.lower() == saved_code.lower():
            return True
    return False


class CaptchaField(StringField):
    widget = TextInput()

    def __call__(self, *args, **kwargs):
        self.data = ''
        captcha = create_captcha()
        input_field_html = super(CaptchaField, self).__call__(*args, **kwargs)
        return Markup("""<input type="hidden" name="captcha_uuid" value="{uuid}" id="captcha-uuid">
                         <img src="{image}" class="border mb-2" id="captcha-image">
                         <audio src="{audio}" type="audio/wav" controls></audio>
                         <!--<button type="button" id="captcha-refresh-button">Refresh</button>-->
                         <br />
                      """).format(uuid=captcha["uuid"], image=captcha["image"],
                                  audio=captcha["audio"]) + input_field_html

    def post_validate(self, form, validation_stopped):
        if decode_captcha(request.form.get('captcha_uuid', None), self.data):
            pass
        else:
            raise ValidationError(_l('Wrong Captcha text.'))


user2_cache = {}


def wilson_confidence_lower_bound(ups, downs, z = 1.281551565545) -> float:
    if ups is None or ups < 0:
        ups = 0
    if downs is None or downs < 0:
        downs = 0
    n = ups + downs
    if n == 0:
        return 0.0
    p = float(ups) / n
    left = p + 1 / (2 * n) * z * z
    right = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    under = 1 + 1 / n * z * z
    return (left - right) / under


def jaccard_similarity(user1_upvoted: set, user2_id: int):
    if user2_id not in user2_cache:
        user2_upvoted_posts = ['post/' + str(id) for id in recently_upvoted_posts(user2_id)]
        user2_upvoted_replies = ['reply/' + str(id) for id in recently_upvoted_post_replies(user2_id)]
        user2_cache[user2_id] = set(user2_upvoted_posts + user2_upvoted_replies)

    user2_upvoted = user2_cache[user2_id]

    if len(user2_upvoted) > 12:
        intersection = len(user1_upvoted.intersection(user2_upvoted))
        union = len(user1_upvoted.union(user2_upvoted))

        return (intersection / union) * 100
    else:
        return 0


def dedupe_post_ids(post_ids: List[Tuple[int, Optional[List[int]], int, int]], limit_to_visible: bool = True) -> List[int]:
    # Remove duplicate posts based on cross-posting rules
    # post_ids is a list of tuples: (post_id, cross_post_ids, user_id, reply_count)
    result = []
    if post_ids is None or len(post_ids) == 0:
        return result

    seen_before = set()  # Track which post IDs we've already processed to avoid duplicates
    priority = set()     # Track post IDs that should be prioritized (kept over their cross-posts)
    lvp = low_value_reposters()  # Get set of bot user IDs
    visible_post_ids = {p[0] for p in post_ids}  # Set of all post IDs that are visible to the user

    for post_id in post_ids:
        # If this post has cross-posts AND it's not already prioritized or seen
        if post_id[1] and post_id[0] not in priority and post_id[0] not in seen_before:
            # Get all cross-posts including the current post
            all_related_posts = [post_id[0]] + list(post_id[1])

            # If limit_to_visible=True, only consider cross-posts that are actually visible
            if limit_to_visible:
                all_related_posts = [cp for cp in all_related_posts if cp in visible_post_ids]

            # Only proceed if we have posts to consider
            if all_related_posts:
                # Find reply counts and author info for each related post
                # Only include posts that actually exist in post_ids
                related_post_info = {}  # Maps post_id -> (reply_count, is_bot)
                for other_post in post_ids:
                    if other_post[0] in all_related_posts:
                        related_post_info[other_post[0]] = (other_post[3], other_post[2] in lvp)

                # Filter all_related_posts to only include posts we found in post_ids
                all_related_posts = [pid for pid in all_related_posts if pid in related_post_info]

                # Only perform deduplication if we have at least 2 related posts
                # (if we only have 1, it's just the current post with no actual alternatives)
                if len(all_related_posts) >= 2:
                    # If current post is from a bot, try to find a non-bot alternative
                    current_is_bot = post_id[2] in lvp
                    if current_is_bot:
                        # Separate into bot and non-bot posts
                        non_bot_posts = [pid for pid in all_related_posts if not related_post_info[pid][1]]

                        if non_bot_posts:
                            # Choose the non-bot post with the most replies
                            best_post = max(non_bot_posts, key=lambda x: related_post_info[x][0])
                        else:
                            # No non-bot alternatives, choose the bot post with the most replies
                            best_post = max(all_related_posts, key=lambda x: related_post_info[x][0])
                    else:
                        # Current post is not from a bot, choose post with most replies (regardless of bot status)
                        best_post = max(all_related_posts, key=lambda x: related_post_info[x][0])

                    priority.add(best_post)

                    # Mark all other related posts as seen to avoid duplicates
                    for related_post in all_related_posts:
                        if related_post != best_post:
                            seen_before.add(related_post)

        # Only add the post to results if we haven't seen it before
        if post_id[0] not in seen_before:
            result.append(post_id[0])
    return result


def paginate_post_ids(post_ids, page: int, page_length: int):
    start = page * page_length
    end = start + page_length
    return post_ids[start:end]


# The two follow-based sources a post can reach an aggregate feed through, as SQL
# fragments OR'd into get_deduped_post_ids' WHERE clause. Module level rather than
# inline so tests exercise the production string itself: they used to hold a
# hand-copied duplicate, which cannot notice a restructure and had already drifted
# from the code it claimed to mirror. Both bind :local_user_id and
# :visibility_viewer_id, so get_deduped_post_ids must add both whenever it appends
# either of them. Each is ANDed with the viewer predicate on the POST's author, so
# following a booster never grants the author's followers-only audience.
#
# NEITHER carries a `p.private is false` gate, deliberately. p.private is the
# microblog marker (Post.new(), app/models.py ~1796), and gating these excluded
# every ingested Mastodon post from the feed -- a boosted microblog SHOULD appear
# when you follow the booster. The gate belongs on the community source instead;
# see MICROBLOG_GATE below.
FOLLOWED_AUTHOR_SQL = """(EXISTS (SELECT 1 FROM user_follower uf
                                  WHERE uf.local_user_id = :local_user_id
                                  AND uf.remote_user_id = p.user_id AND uf.is_inward is false
                                  AND uf.is_accepted is true)
                                  AND """ + visible_to_sql('p') + ')'

FOLLOWED_BOOSTER_SQL = """(EXISTS (SELECT 1 FROM post_boost pb
                                  INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                                  WHERE pb.post_id = p.id
                                  AND uf2.local_user_id = :local_user_id
                                  AND uf2.is_inward is false
                                  AND uf2.is_accepted is true)
                                  AND """ + visible_to_sql('p') + ')'

# Applied to the COMMUNITY source only, never to the whole query and never to the
# two disjuncts above. A microblog reaches an aggregate feed because you follow its
# author or its booster, never merely because you subscribed to a community that
# carries it.
MICROBLOG_GATE = 'p.private is false'


def get_deduped_post_ids(result_id: str, community_ids: List[int], sort: str, hashtag: str = '', include_following=False, community_sql: str = None) -> List[int]:
    if not community_sql and (community_ids is None or len(community_ids) == 0):
        return []
    # result_id is client-controlled (every web caller reads it from ?result_id= and
    # echoes it back into its pagination links), while the cached value is a list of
    # post ids filtered for ONE viewer's authorisation that nothing downstream
    # re-checks. So the key is namespaced per user, and the SAME key governs both the
    # read below and the write at the end of this function -- two separate conditions
    # could disagree, and did: the read used to require a non-empty result_id while
    # the write required only an authenticated user.
    cache_key = f'feed:{current_user.id}:{result_id}' if result_id and current_user.is_authenticated else None
    if cache_key and app_pkg.redis_client.exists(cache_key):
        return json.loads(app_pkg.redis_client.get(cache_key))

    params = {}                 # parameters provided to the SQL query
    post_id_sql = 'SELECT p.id, p.cross_posts, p.user_id, p.reply_count FROM "post" as p\nINNER JOIN "community" as c on p.community_id = c.id\n'
    if community_sql:
        community_disjunct = community_sql
    elif community_ids[0] == -1:  # A special value meaning to get posts from all communities
        community_disjunct = 'c.show_all is true'
    else:
        community_disjunct = 'c.id IN :community_ids'
        params['community_ids'] = tuple(community_ids)

    # The microblog gate rides with the community source, not with the whole query.
    # Gating the whole query instead meant the gate had to be dropped wholesale for
    # the subscribed feed (include_following=True), which put every ingested Mastodon
    # post in front of users who follow nobody. The extra parentheses are
    # load-bearing: community_sql is caller-supplied (app/main/routes.py) and must
    # not be able to bind looser than the AND.
    sources = [f'(({community_disjunct}) AND {MICROBLOG_GATE} AND {listable_sql("p")})']
    if current_user.is_authenticated and current_user.num_following and include_following:
        sources.append(FOLLOWED_AUTHOR_SQL)
        sources.append(FOLLOWED_BOOSTER_SQL)
        params['local_user_id'] = current_user.id
        params['visibility_viewer_id'] = current_user.id

    post_id_where = ["(" + " OR ".join(sources) + ")", 'c.banned is false']
    if current_user.is_authenticated and current_user.hide_low_quality and community_ids[0] == -1:
        post_id_where.append('c.low_quality is false')

    # Filter by post tag
    if hashtag:
        tag_record = Tag.query.filter(Tag.name == hashtag.strip()).first()
        if tag_record:
            post_id_sql += 'INNER JOIN "post_tag" as pt ON p.id = pt.post_id\n'
            post_id_where.append('pt.tag_id = :tag_record_id')
            params['tag_record_id'] = tag_record.id

    # filter out posts in communities where the community name is objectionable to them or they blocked the instance
    if current_user.is_authenticated:
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            post_id_where.append('c.id NOT IN :filtered_out_community_ids ')
            params['filtered_out_community_ids'] = tuple(filtered_out_community_ids)

        if bi := blocked_or_banned_instances(current_user.id):
            post_id_where.append('c.instance_id NOT IN :filtered_out_instance_ids ')
            params['filtered_out_instance_ids'] = tuple(bi)
            post_id_where.append('p.instance_id NOT IN :filtered_out_instance_ids2 ')
            params['filtered_out_instance_ids2'] = tuple(bi)

    # Private communities are invite-only real access control (Community.private,
    # not the Post.private microblog marker), so the base restriction applies to
    # EVERY viewer -- anonymous included -- and membership only WIDENS it. Unlike
    # the blocklist filters below, an empty list here does not mean "filter
    # nothing": it means this viewer has no exceptions, so the restriction must
    # still be appended. Kept as one unconditional site above the
    # anonymous/authenticated split so no branch can be added that lacks it.
    if current_user.is_authenticated and (private_community_ids := community_membership_private(current_user.id)):
        post_id_where.append('(c.private is false OR c.id IN :private_community_ids) ')
        params['private_community_ids'] = tuple(private_community_ids)
    else:
        post_id_where.append('c.private is false ')

    # filter out nsfw and nsfl if desired
    if current_user.is_anonymous:
        if current_app.config['CONTENT_WARNING']:
            post_id_where.append('p.from_bot is false AND p.nsfl is false AND p.deleted is false AND p.status > 0 ')
        else:
            post_id_where.append('p.from_bot is false AND p.nsfw is false AND p.nsfl is false AND p.deleted is false AND p.status > 0 ')
    else:
        if current_user.ignore_bots == 1:
            post_id_where.append('p.from_bot is false ')
        if current_user.hide_nsfl == 1:
            post_id_where.append('p.nsfl is false ')
        if current_user.hide_nsfw == 1:
            post_id_where.append('p.nsfw is false ')
        if current_user.hide_read_posts:
            post_id_where.append('p.id NOT IN (SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id) ')
        if current_user.hide_gen_ai == 1:
            post_id_where.append('p.ai_generated is false ')
        post_id_where.append('p.id NOT IN (SELECT hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id) ')
        params['user_id'] = current_user.id

        # Language filter
        if current_user.read_language_ids and len(current_user.read_language_ids) > 0:
            post_id_where.append('(p.language_id IN :read_language_ids OR p.language_id is null) ')
            params['read_language_ids'] = tuple(current_user.read_language_ids)

        post_id_where.append('p.deleted is false AND p.status > 0 ')

        # filter blocked domains and instances
        if domains_ids := blocked_domains(current_user.id):
            post_id_where.append('(p.domain_id NOT IN :domain_ids OR p.domain_id is null) ')
            params['domain_ids'] = tuple(domains_ids)
        if instance_ids := blocked_or_banned_instances(current_user.id):
            post_id_where.append('(p.instance_id NOT IN :instance_ids OR p.instance_id is null) ')
            params['instance_ids'] = tuple(instance_ids)
        if blocked_community_ids := blocked_communities(current_user.id):
            post_id_where.append('p.community_id NOT IN :blocked_community_ids ')
            params['blocked_community_ids'] = tuple(blocked_community_ids)
        # filter blocked users
        if blocked_accounts := blocked_users(current_user.id):
            post_id_where.append('p.user_id NOT IN :blocked_accounts ')
            params['blocked_accounts'] = tuple(blocked_accounts)
        # filter communities banned from
        if banned_from := communities_banned_from(current_user.id):
            post_id_where.append('p.community_id NOT IN :banned_from ')
            params['banned_from'] = tuple(banned_from)
        if community_ids[0] != -1:
            blocked_flair = CommunityFlairBlock.query.filter(CommunityFlairBlock.user_id == current_user.id,
                                                             CommunityFlairBlock.community_id.in_(community_ids)).all()
            if blocked_flair:
                blocked_flair_ids = [bf.community_flair_id for bf in blocked_flair]
                post_id_where.append('p.id NOT IN (SELECT post_id FROM "post_flair" WHERE flair_id IN :blocked_flair_ids) ')
                params['blocked_flair_ids'] = tuple(blocked_flair_ids)

    # sorting
    post_id_sort = ''
    if sort == '' or sort == 'hot':
        post_id_sort = 'ORDER BY p.ranking DESC, p.posted_at DESC'
    elif sort == 'scaled':
        post_id_sort = 'ORDER BY p.ranking_scaled DESC, p.ranking DESC, p.posted_at DESC'
        post_id_where.append('p.ranking_scaled is not null AND p.from_bot is false ')
    elif sort.startswith('top'):
        if sort != 'top_all':
            post_id_where.append('p.posted_at > :top_cutoff ')
        post_id_sort = 'ORDER BY p.score DESC'
        if sort == 'top_1h':
            params['top_cutoff'] = utcnow() - timedelta(hours=1)
        elif sort == 'top_6h':
            params['top_cutoff'] = utcnow() - timedelta(hours=6)
        elif sort == 'top_12h':
            params['top_cutoff'] = utcnow() - timedelta(hours=12)
        elif sort == 'top':
            params['top_cutoff'] = utcnow() - timedelta(hours=24)
        elif sort == 'top_1w':
            params['top_cutoff'] = utcnow() - timedelta(days=7)
        elif sort == 'top_1m':
            params['top_cutoff'] = utcnow() - timedelta(days=28)
        elif sort == 'top_1y':
            params['top_cutoff'] = utcnow() - timedelta(days=365)
        elif sort != 'top_all':
            params['top_cutoff'] = utcnow() - timedelta(days=1)
    elif sort == 'new':
        post_id_sort = 'ORDER BY p.posted_at DESC'
    elif sort == 'old':
        post_id_sort = 'ORDER BY p.posted_at ASC'
    elif sort == 'active':
        post_id_where.append('p.reply_count > 0 ')
        post_id_sort = 'ORDER BY p.last_active DESC'
    # Filter out posts stickied to the instance, they are handled separately
    post_id_where.append('p.instance_sticky is false ')
    final_post_id_sql = f"{post_id_sql} WHERE {' AND '.join(post_id_where)}\n{post_id_sort}\nLIMIT 1000"
    post_ids = db.session.execute(text(final_post_id_sql), params).all()
    post_ids = dedupe_post_ids(post_ids, limit_to_visible=(community_ids[0] != -1))

    if cache_key:  # same key the read above used; see where it is derived
        app_pkg.redis_client.set(cache_key, json.dumps(post_ids), ex=86400)  # 86400 is 1 day
    return post_ids


def post_ids_to_models(post_ids: List[int], sort: str):
    posts = Post.query.filter(Post.id.in_([p for p in post_ids]))
    # Final sorting
    if sort == '' or sort == 'hot':
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == 'scaled':
        posts = posts.order_by(desc(Post.ranking_scaled)).order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort.startswith('top'):
        posts = posts.order_by(desc(Post.score))
    elif sort == 'new':
        posts = posts.order_by(desc(Post.posted_at))
    elif sort == 'old':
        posts = posts.order_by(asc(Post.posted_at))
    elif sort == 'active':
        posts = posts.order_by(desc(Post.last_active))
    return posts


@cache.memoize(timeout=3600)
def instance_sticky_post_ids():
    post_ids = db.session.execute(text('SELECT id FROM "post" WHERE instance_sticky = :sticky AND deleted = :deleted AND status > 0'),
                                  {"sticky": True, "deleted": False}).all()
    post_ids = [post_id[0] for post_id in post_ids]
    return post_ids


@cache.memoize(timeout=3600)
def instance_sticky_posts(sort: str):
    posts = Post.query.filter(Post.instance_sticky == True, Post.deleted == False, Post.status > 0)
    if sort == '' or sort == 'hot':
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == 'scaled':
        posts = posts.order_by(desc(Post.ranking_scaled)).order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort.startswith('top'):
        posts = posts.order_by(desc(Post.up_votes - Post.down_votes))
    elif sort == 'new':
        posts = posts.order_by(desc(Post.posted_at))
    elif sort == 'old':
        posts = posts.order_by(asc(Post.posted_at))
    elif sort == 'active':
        posts = posts.order_by(desc(Post.last_active))
    return posts.all()


def get_instance_stickies(community_ids: List[int], sort: str):
    posts = instance_sticky_posts(sort=sort)
    visible_posts = []
    if len(community_ids) == 1 and community_ids[0] < 0:
        all_communities = True
    else:
        all_communities = False

    # Check each post in turn and only return list of posts that should be displayed
    if current_user.is_anonymous:
        for post in posts:
            # Only show NSFW/NSFL to anon users if content warning enabled
            if not current_app.config['CONTENT_WARNING']:
                if post.nsfw or post.nsfl:
                    continue
            # Community not in main feed view filter
            if post.community_id not in community_ids and not all_communities:
                continue

            # Post should be visible
            visible_posts.append(post)
    else:
        hidden_post_ids = db.session.execute(text('SELECT hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id'),
                                             {"user_id": current_user.id}).all()
        hidden_post_ids = [post_id[0] for post_id in hidden_post_ids]
        if current_user.hide_read_posts:
            read_post_ids = db.session.execute(text('SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id'),
                                               {"user_id": current_user.id}).all()
            read_post_ids = [read_post_id[0] for read_post_id in read_post_ids]
        else:
            read_post_ids = []

        for post in posts:
            # All the different reasons a post might be filtered out
            # Community not in main feed view filter
            if post.community_id not in community_ids and not all_communities:
                continue
            # User hides NSFL posts
            if current_user.hide_nsfl == 1 and post.nsfl:
                continue
            # User hides NSFW posts
            if current_user.hide_nsfw == 1 and post.nsfw:
                continue
            # User has marked post as read and hides read posts
            if post.id in read_post_ids:
                continue
            # User has hidden the post
            if post.id in hidden_post_ids:
                continue

            # We made it past all the filters, this post should be displayed
            visible_posts.append(post)

    return visible_posts


def total_comments_on_post_and_cross_posts(post_id):
    sql = """SELECT
            p.reply_count + (
                SELECT COALESCE(SUM(cp.reply_count), 0)
                FROM post cp
                WHERE cp.id = ANY(p.cross_posts)
            ) AS total_reply_count
        FROM post p
        WHERE p.id = :post_id;
    """
    result = db.session.execute(text(sql), {'post_id': post_id}).scalar_one_or_none()
    return result if result is not None else 0


def store_files_in_s3():
    return current_app.config['S3_ACCESS_KEY'] != '' and current_app.config['S3_ACCESS_SECRET'] != '' and \
        current_app.config['S3_ENDPOINT'] != ''


def move_file_to_s3(file_id, s3):
    if store_files_in_s3():
        file: File = db.session.get(File, file_id)
        if file:
            if file.thumbnail_path and not file.thumbnail_path.startswith('http') and file.thumbnail_path.startswith(
                    'app/static/media'):
                if os.path.isfile(file.thumbnail_path):
                    content_type = guess_mime_type(file.thumbnail_path)
                    extra_args = {'ContentType': content_type}
                    if current_app.config.get('S3_STORAGE_CLASS'):
                        extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                    if current_app.config.get('S3_PUBLIC_ACL'):
                        extra_args['ACL'] = 'public-read'
                    new_path = file.thumbnail_path.replace('app/static/media/', "")
                    s3.upload_file(file.thumbnail_path, current_app.config['S3_BUCKET'], new_path,
                                   ExtraArgs=extra_args)
                    os.unlink(file.thumbnail_path)
                    file.thumbnail_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                    db.session.commit()

            if file.file_path and not file.file_path.startswith('http') and file.file_path.startswith(
                    'app/static/media'):
                if os.path.isfile(file.file_path):
                    content_type = guess_mime_type(file.file_path)
                    extra_args = {'ContentType': content_type}
                    if current_app.config.get('S3_STORAGE_CLASS'):
                        extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                    if current_app.config.get('S3_PUBLIC_ACL'):
                        extra_args['ACL'] = 'public-read'
                    new_path = file.file_path.replace('app/static/media/', "")
                    s3.upload_file(file.file_path, current_app.config['S3_BUCKET'], new_path,
                                   ExtraArgs=extra_args)
                    os.unlink(file.file_path)
                    file.file_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                    db.session.commit()

            if file.source_url and not file.source_url.startswith('http') and file.source_url.startswith(
                    'app/static/media'):
                if os.path.isfile(file.source_url):
                    content_type = guess_mime_type(file.source_url)
                    extra_args = {'ContentType': content_type}
                    if current_app.config.get('S3_STORAGE_CLASS'):
                        extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                    if current_app.config.get('S3_PUBLIC_ACL'):
                        extra_args['ACL'] = 'public-read'
                    new_path = file.source_url.replace('app/static/media/', "")
                    s3.upload_file(file.source_url, current_app.config['S3_BUCKET'], new_path,
                                   ExtraArgs=extra_args)
                    os.unlink(file.source_url)
                    file.source_url = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                    db.session.commit()


def days_to_add_for_next_month(start_date):
    """
    Calculate days to add to get to the same day next month.
    Uses the "try and backtrack" approach:
    1. Try to use the same day as start_date
    2. If that's invalid (e.g., Feb 31), subtract a day and try again
    3. Repeat until we find a valid date

    This ensures that posts scheduled for the 31st will:
    - Use the 31st in months that have 31 days
    - Use the last day (28-30) in months that don't have 31 days
    - Return to the 31st in subsequent months that have 31 days
    """
    # Calculate the new month and year
    new_month = start_date.month + 1
    new_year = start_date.year

    if new_month > 12:
        new_month = 1
        new_year += 1

    # Try to use the same day as start_date, backing off if needed
    new_day = start_date.day

    # Try to create the date, backing off one day at a time if invalid
    while True:
        try:
            target_date = datetime(new_year, new_month, new_day)
            break  # Success!
        except ValueError:
            # Invalid date (e.g., Feb 31), try the previous day
            new_day -= 1
            if new_day < 1:
                # Should never happen, but just in case
                new_day = 1

    # Calculate the number of days to add. On the DATES, not the datetimes:
    # `target_date` is midnight, so subtracting a `start_date` with any time of
    # day on it lost the remainder and answered one day short -- a post
    # scheduled for 12:00 on the 15th moved to the 14th, and again the month
    # after that.
    days_to_add = (target_date.date() - start_date.date()).days

    return days_to_add


def find_next_occurrence(post: Post) -> timedelta:
    if post.repeat is not None and post.repeat != 'none':
        if post.repeat == 'daily':
            return timedelta(days=1)
        elif post.repeat == 'weekly':
            return timedelta(days=7)
        elif post.repeat == 'monthly':
            days_to_add = days_to_add_for_next_month(post.scheduled_for)
            return timedelta(days=days_to_add)

    return timedelta(seconds=0)


def notif_id_to_string(notif_id) -> str:
    # -- user level ---
    if notif_id == NOTIF_USER:
        return _('User')
    if notif_id == NOTIF_COMMUNITY:
        return _('Community')
    if notif_id == NOTIF_TOPIC:
        return _('Topic/feed')
    if notif_id == NOTIF_POST:
        return _('Comment')
    if notif_id == NOTIF_REPLY:
        return _('Comment')
    if notif_id == NOTIF_FEED:
        return _('Topic/feed')
    if notif_id == NOTIF_MENTION:
        return _('Comment')
    if notif_id == NOTIF_MESSAGE:
        return _('Chat')
    if notif_id == NOTIF_BAN:
        return _('Admin')
    if notif_id == NOTIF_UNBAN:
        return _('Admin')
    if notif_id == NOTIF_NEW_MOD:
        return _('Admin')
    if notif_id == NOTIF_ANSWER:
        return _('Answer')

    # --- mod/admin level ---
    if notif_id == NOTIF_REPORT:
        return _('Admin')

    # --- admin level ---
    if notif_id == NOTIF_REPORT_ESCALATION:
        return _('Admin')
    if notif_id == NOTIF_REGISTRATION:
        return _('Admin')

    # --model/db default--
    if notif_id == NOTIF_DEFAULT:
        return _('All')


@cache.memoize(timeout=6000)
def filtered_out_communities(user: User) -> List[int]:
    if user.community_keyword_filter:
        keyword_filters = []
        for community_filter in user.community_keyword_filter.split(','):
            keyword = community_filter.strip()
            if keyword:
                keyword_filters.append(or_(Community.name.ilike(f"%{keyword}%"),
                                           Community.title.ilike(f"%{keyword}%")))

        if keyword_filters:
            communities = Community.query.filter(or_(*keyword_filters))
            return [community.id for community in communities.all()]

    return []


@cache.memoize(timeout=300)
def retrieve_image_hash(image_url):
    def fetch_hash(retries_left):
        # D1336. Everything this reads comes out of the hashing endpoint's answer,
        # and none of it was checked. Measured against the four shapes an endpoint
        # can return with a 200:
        #
        #   quality as a string -> TypeError: '>=' not supported between
        #                          instances of 'str' and 'int'
        #   quality as null     -> the same (the key EXISTS, so `.get('quality', 0)`
        #                          answers None rather than the default)
        #   a body that is not json -> JSONDecodeError
        #   a json list         -> AttributeError: 'list' object has no attribute 'get'
        #
        # None of those is an `httpx.HTTPError`, so all four escaped the clauses
        # below -- and this function is called from `Post.new`, so a federated post
        # was lost, and from `app/post/routes.py` and `app/admin/routes.py`, where
        # it is a 500. A hash this instance cannot obtain is no hash, which is what
        # every caller already handles.
        try:
            response = get_request(current_app.config['IMAGE_HASHING_ENDPOINT'], {'image_url': image_url})
            if response.status_code == 200:
                try:
                    result = response.json()
                except ValueError:
                    return None
                if not isinstance(result, dict):
                    return None
                quality = result.get('quality', 0)
                # No `isinstance(quality, bool)` clause: `True >= 70` and
                # `False >= 70` are both False, so a boolean is refused by the
                # comparison itself and excluding it explicitly changes nothing.
                # A mutant removing such a clause survives, which is the test
                # telling the truth about it.
                if not isinstance(quality, (int, float)):
                    return None
                if quality >= 70:
                    pdq_hash = result.get('pdq_hash_binary', '')
                    # D1402. Stripped, so surrounding whitespace from the endpoint does
                    # not turn an otherwise good hash into no hash at all once
                    # `hash_matches_blocked_image` compares it exactly. Without this
                    # the blocklist would silently stop checking such an image rather
                    # than raising, which is the quieter of the two failures.
                    return pdq_hash.strip() if isinstance(pdq_hash, str) else None
            elif response.status_code == 429 and retries_left > 0:
                sleep(random.uniform(1, 3))
                return fetch_hash(retries_left - 1)
        # `httpx.ReadError` had a clause of its own below this one, and it is a
        # subclass of `HTTPError` (ReadError -> NetworkError -> TransportError ->
        # RequestError -> HTTPError), so it never ran.
        except httpx.HTTPError as e:
            current_app.logger.warning(f"Error retrieving image hash: {e}")
        finally:
            try:
                response.close()
            except:
                pass
        return None

    return fetch_hash(retries_left=2)


BINARY_RE = re.compile(r'^[01]+$')  # used in hash_matches_blocked_image()


def hash_matches_blocked_image(hash: str) -> bool:
    # calculate hamming distance between the provided hash and the hashes of all the blocked images.
    # the hamming distance is a value between 0 and 256 indicating how many bits are different.
    # 15 is the number of different bits we will accept. Anything less than that and we consider the images to be the same.

    # only accept a string with 0 and 1 in it. This makes it safe to use sql injection-prone code below, which greatly simplifies the conversion of binary strings
    #
    # D1402. `fullmatch`, not `match`. `$` also matches immediately BEFORE a trailing
    # newline, so `'0' * 256 + '\n'` passed this guard and reached Postgres as
    # `B'000...0\n'`:
    #
    #     BINARY_RE.match('0'*256 + '\n')   ->  True
    #     hash_matches_blocked_image(...)   ->  DataError:
    #         (psycopg2.errors.InvalidTextRepresentation)
    #
    # The newline stays inside the quoted literal, so it is not an injection -- it is a
    # DataError that poisons the transaction. `Post.new` (app/models.py:2685) calls
    # this, so a federated post was lost and the aborted transaction took the rest of
    # the inbox request; `app/shared/post.py:570` calls it on a local upload, where it
    # is a 500. Reachable because `retrieve_image_hash` returns the hashing endpoint's
    # `pdq_hash_binary` verbatim, and a service emitting a trailing newline is
    # ordinary.
    if not BINARY_RE.fullmatch(hash):
        current_app.logger.warning(f"Invalid binary hash: {hash}")
        return False

    sql = f"""SELECT id FROM blocked_image WHERE length(replace((hash # B'{hash}')::text, '0', '')) < 15;"""
    blocked_images = db.session.execute(text(sql)).scalars().first()
    return blocked_images is not None


def posts_with_blocked_images() -> List[int]:
    # A post's own image and the images of its gallery (post_file) are both held to the blocked hashes
    sql = """
    SELECT DISTINCT post.id
    FROM post
    JOIN (
        SELECT post.id AS post_id, post.image_id AS file_id FROM post
        UNION ALL
        SELECT post_id, file_id FROM post_file
    ) AS post_image ON post_image.post_id = post.id
    JOIN file ON post_image.file_id = file.id
    JOIN blocked_image ON (
        length(replace((file.hash # blocked_image.hash)::text, '0', ''))
    ) < 15
    WHERE post.deleted = false AND file.hash is not null
    """

    return list(db.session.execute(text(sql)).scalars())


def notify_admin(title, url, author_id, notif_type, subtype, targets):
    for admin in Site.admins():
        notify = Notification(title=title, url=url,
                              user_id=admin.id,
                              author_id=author_id, notif_type=notif_type,
                              subtype=subtype,
                              targets=targets)
        admin.unread_notifications += 1
        db.session.add(notify)
    db.session.commit()


@cache.memoize(timeout=60)
def reported_posts(user_id: int, is_admin: bool) -> List[int]:
    if user_id is None:
        return []
    if is_admin:
        post_ids = list(db.session.execute(text('SELECT id FROM "post" WHERE reports > 0')).scalars())
    else:
        community_ids = moderating_communities_ids(user_id)
        if len(community_ids) > 0:
            post_ids = list(db.session.execute(text('SELECT id FROM "post" WHERE reports > 0 AND community_id IN :community_ids'),
                                               {'community_ids': tuple(community_ids)}).scalars())
        else:
            return []
    return post_ids


def reported_post_replies(user_id: int, is_admin: bool) -> List[int]:
    """The reply twin of `reported_posts`, which is called from nine templates.

    D1366. This one had no callers and could not have run either way. It passed a
    LIST to an `IN :community_ids` parameter, which `text()` renders as a Postgres
    array literal rather than expanding, and it had no guard for the empty case.
    Measured for a moderator of no communities:

        reported_posts         -> []
        reported_post_replies  -> ProgrammingError: (psycopg2.errors.SyntaxError)
                                  syntax error at or near "'{}'"

    It also took `admin_ids` and tested membership itself while its twin takes
    `is_admin`, so the same question had two shapes. Both are the live one's now:
    nothing called this, so there was no signature to keep compatible.
    """
    if user_id is None:
        return []
    if is_admin:
        return list(db.session.execute(
            text('SELECT id FROM "post_reply" WHERE reports > 0')).scalars())
    community_ids = moderating_communities_ids(user_id)
    if len(community_ids) == 0:
        return []
    return list(db.session.execute(
        text('SELECT id FROM "post_reply" WHERE reports > 0 AND community_id IN :community_ids'),
        {'community_ids': tuple(community_ids)}).scalars())


def possible_communities():
    which_community = {}
    joined = joined_communities(current_user.get_id())
    moderating = moderating_communities(current_user.get_id())
    comms = []
    already_added = set()
    for c in moderating:
        if c.id not in already_added:
            comms.append((c.id, c.display_name()))
            already_added.add(c.id)
    if len(comms) > 0:
        which_community['Moderating'] = comms
    comms = []
    for c in joined:
        if c.id not in already_added:
            comms.append((c.id, c.display_name()))
            already_added.add(c.id)
    if len(comms) > 0:
        which_community['Joined communities'] = comms
    comms = []
    # Private communities are invite-only real access control (Community.private,
    # app/models.py:594), so they must not be disclosed -- nor offered as a post
    # destination -- to anyone who is not a member. One unconditional filter,
    # base restriction widened by membership, rather than an if/else that a later
    # branch could drop the restriction out of. can_create_post carries the
    # matching authorisation check; the form field is client-supplied, so hiding
    # them here is not on its own enough.
    for c in db.session.query(Community.id, Community.ap_id, Community.title, Community.ap_domain).\
            filter(Community.banned == False).join(Instance, Instance.id == Community.instance_id).\
            filter(Instance.gone_forever == False, Community.name != 'microblogs').\
            filter(or_(Community.private == False,
                       Community.id.in_(community_membership_private(current_user.get_id())))).\
            order_by(Community.title).all():
        if c.id not in already_added:
            if c.ap_id is None:
                display_name = c.title
            else:
                display_name = f"{c.title}@{c.ap_domain}"
            comms.append((c.id, display_name))
            already_added.add(c.id)
    if len(comms) > 0:
        which_community['Others'] = comms
    return which_community


@cache.memoize(timeout=300)
def user_notes(user_id):
    if user_id is None:
        return {}
    result = {}
    for note in db.session.query(UserNote).filter(UserNote.user_id == user_id).all():
        result[note.target_id] = note.body
    return result


@cache.memoize(timeout=300)
def favorite_communities(user_id):
    if user_id is None:
        return []
    return (
        db.session.execute(
            select(CommunityFavorite.community_id)
            .where(CommunityFavorite.user_id == user_id)
        )
        .scalars()
        .all()
    )


def communities_run_by_inactive_mods():
    cutoff = utcnow() - timedelta(days=90)

    sql = """
        SELECT c.id
        FROM community c
        JOIN community_member cm
          ON cm.community_id = c.id
         AND cm.is_banned = false
         AND (cm.is_moderator OR cm.is_owner)
        JOIN "user" u
          ON u.id = cm.user_id
        GROUP BY c.id
        HAVING COUNT(*) > 0
           AND SUM(
                CASE
                    WHEN COALESCE(u.bot, false) = false
                     AND COALESCE(u.bot_override, false) = false
                     AND u.last_seen >= :cutoff
                    THEN 1 ELSE 0
                END
           ) = 0
    """

    return db.session.execute(text(sql), {"cutoff": cutoff}).scalars().all()


class SqlKeysetPagination:
    """Wrapper to make sqlakeyset pages more similar to existing Flask pagination interface"""

    def __init__(self, page_obj):
        self.items = page_obj
        self._page_obj = page_obj

    @property
    def has_next(self):
        return self._page_obj.paging.has_next

    @property
    def has_prev(self):
        return self._page_obj.paging.has_previous

    @property
    def next_bookmark(self):
        return self._page_obj.paging.bookmark_next if self.has_next else None

    @property
    def prev_bookmark(self):
        return self._page_obj.paging.bookmark_previous if self.has_prev else None


@event.listens_for(User.unread_notifications, 'set')
def on_unread_notifications_set(target, value, oldvalue, initiator):
    if value != oldvalue and current_app.config['NOTIF_SERVER']:
        publish_sse_event(f"notifications:{target.id}", json.dumps({'num_notifs': value}))


def publish_sse_event(key, value):
    r = get_redis_connection()
    r.publish(key, value)


# The one username charset rule, shared by both user-creation paths:
# RegistrationForm.validate_user_name (app/auth/forms.py, self-registration) and
# AddUserForm.validate_user_name (app/admin/forms.py, admin-created users).
# They used to differ -- the admin path checked only for '@' -- and
# apply_feed_url_rules below interpolates current_user.user_name into a regex,
# so an admin-created name carrying a metacharacter changed what that pattern
# matched. Both paths produce local User rows that are indistinguishable
# downstream (actor url, webfinger, feed namespace), so there is no consumer
# that could safely accept a wider charset from one of them.
USER_NAME_CHARSET_RE = re.compile(r'^[a-zA-Z0-9_]+$')


def validate_user_name_charset(user_name):
    """WTForms inline-validator body: reject a user name outside
    USER_NAME_CHARSET_RE. Called by both forms' validate_user_name hooks rather
    than copied into each, so the two cannot drift apart again.
    """
    # D1402, the same one character. `$` matches before a trailing newline, so
    # `'alice\n'` satisfied a guard whose message says letters, numbers and
    # underscores -- and `app/admin/routes.py:2125` stores `form.user_name.data`
    # unstripped, so that newline reached the `user_name` column and every actor url
    # built from it. Self-registration strips before calling this; the admin path did
    # not, which is why the boundary itself has to be exact.
    if not USER_NAME_CHARSET_RE.fullmatch(user_name.data):
        raise ValidationError(_l('User names can only contain letters, numbers, and underscores.'))


# Names nobody may register. `process_registration_form` held this as a local
# list and the OAuth signup path held nothing at all, which is how D1139
# happened: an account called `admin` was one Google sign-in away.
RESERVED_USER_NAMES = ('admin',)


def user_name_is_taken(user_name: str) -> bool:
    """True when a LOCAL user, community or feed already answers to this name.

    `RegistrationForm.validate_user_name` asks this question one query at a
    time. The OAuth signup path asked a narrower, CASE-SENSITIVE version of
    it (`User.user_name == local_part`), so `Person` and `person` ended up
    side by side while `find_user` lowers both sides and takes `.first()`
    (D1140). One function, so the two ends cannot drift apart again.
    """
    name = user_name.strip()
    if User.query.filter(func.lower(User.user_name) == func.lower(name),
                         User.ap_id == None).first() is not None:
        return True
    if Community.query.filter(func.lower(Community.name) == func.lower(name),
                              Community.ap_id == None).first() is not None:
        return True
    return Feed.query.filter(func.lower(Feed.name) == func.lower(name),
                             Feed.ap_id == None).first() is not None


def apply_feed_url_rules(self):
    # D1436: a feed's address pasted from the url bar starts with /f/, which is not part of its name
    if self.url.data.strip().lower().startswith('/f/'):
        self.url.data = self.url.data.strip()[3:]
    if '-' in self.url.data.strip():
        self.url.errors.append(_l('- cannot be in Url. Use _ instead?'))
        return False

    # D697: an admin editing someone else's feed sets owner_user_name; a private feed's suffix is its owner's
    owner_user_name = getattr(self, 'owner_user_name', current_user.user_name).lower()
    if not self.public.data and not '/' in self.url.data.strip():
        self.url.data = self.url.data.strip().lower() + '/' + owner_user_name
    elif self.public.data and '/' in self.url.data.strip():
        self.url.data = self.url.data.strip().split('/', 1)[0]
    else:
        self.url.data = self.url.data.strip().lower()

    # Allow alphanumeric characters and underscores (a-z, A-Z, 0-9, _)
    if self.public.data:
        regex = r'^[a-zA-Z0-9_]+$'
    else:
        # re.escape: user_name is DATA here, not pattern. Without it a name
        # carrying a regex metacharacter changes what the optional suffix
        # group matches -- 'a.b' made '/aXb' validate, letting that user
        # claim a private feed url in another user's namespace, and 'a(b'
        # raised re.error out of this function. This is the defence that
        # holds for names ALREADY in the database; USER_NAME_CHARSET_RE
        # above only governs names created from now on.
        regex = r'^[a-zA-Z0-9_]+(?:/' + re.escape(owner_user_name) + ')?$'
    if not re.match(regex, self.url.data):
        self.url.errors.append(_l('Feed urls can only contain letters, numbers, and underscores.'))
        return False

    try:
        self.feed_id
    except AttributeError:
        feed = Feed.query.filter(Feed.name == self.url.data).first()
    else:
        feed = Feed.query.filter(Feed.name == self.url.data).filter(Feed.id != self.feed_id).first()
    if feed is not None:
        self.url.errors.append(_l('A Feed with this url already exists.'))
        return False
    return True


# notification destination user helper function to make sure the
# notification text is stored in the database using the language of the
# recipient, rather than the language of the originator
def get_recipient_language(user_id: int) -> str:
    lang_to_use = ''

    # look up the user in the db based on the id
    recipient = db.session.get(User, user_id)

    # if the user has language_id set, use that
    if recipient.language_id:
        lang = db.session.get(Language, recipient.language_id)
        lang_to_use = lang.code

    # else if the user has interface_language use that
    elif recipient.interface_language:
        lang_to_use = recipient.interface_language

    # else default to english
    else:
        lang_to_use = 'en'

    return lang_to_use


def safe_order_by(sort_param: str, model, allowed_fields: set):
    """
    Returns a SQLAlchemy order_by clause for a given model and sort parameter. Guards against SQL injection.

    Parameters:
        sort_param (str): The user-supplied sort string (e.g., 'name desc').
        model (db.Model): The SQLAlchemy model class to sort on.
        allowed_fields (set): A set of allowed field names (str) from the model.

    Returns:
        A SQLAlchemy order_by clause (asc/desc column expression).

    Example usage:
        sort_param = request.args.get('sort_by', 'post_reply_count desc')
        allowed_fields = {'name', 'created_at', 'post_reply_count'}

        communities = communities.order_by(
            safe_order_by(sort_param, Community, allowed_fields)
        )
    """
    parts = (sort_param or '').strip().split()
    field_name = parts[0] if parts else ''
    direction = parts[1].lower() if len(parts) > 1 else 'asc'

    if field_name in allowed_fields and hasattr(model, field_name):
        column = getattr(model, field_name)
        if direction == 'desc':
            return desc(column)
        else:
            return asc(column)
    else:
        # Return a default safe order if invalid input. This must not be
        # `next(iter(allowed_fields))`: a set has no order, and Python
        # randomizes string hashing per process, so that picked a different
        # column in every worker -- and raised AttributeError in the workers
        # where it picked a name the model does not have.
        for default_field in sorted(allowed_fields):
            if hasattr(model, default_field):
                return desc(getattr(model, default_field))
        return desc(model.id)



def render_from_tpl(tpl: str) -> str:
    """
    Replace tags in `template` like {% week %}, {%day%}, {% month %}, {%year%}
    with the corresponding values.
    """
    date = utcnow()

    # Words to replace
    replacements = {
        "week": f"{date.isocalendar()[1]:02d}",
        "day": f"{date.day:02d}",
        "month": f"{date.month:02d}",
        "year": str(date.year)
    }

    # Regex to find {%   word   %}, spaces will be ignored
    pattern = re.compile(r"\{\%\s*(week|day|month|year)\s*\%\}")

    # Substitute each match with its replacement
    def _sub(match):
        key = match.group(1)
        return replacements.get(key, match.group(0))

    return pattern.sub(_sub, tpl)


@lru_cache(maxsize=None)
def get_timezones():
    """
    returns an OrderedDict of timezones:
    {
       'Africa': [('Africa/Abidjan','Africa/Abidjan'), ...],
       'America': [('America/New_York','America/New_York'), ...],
       ...
    }
    """
    by_region = OrderedDict()
    for tz in sorted(available_timezones()):
        if '/' in tz:
            region, _ = tz.split('/', 1)
            if region in ['Arctic', 'Atlantic', 'Etc', 'Other']:
                continue
            by_region.setdefault(region, []).append((tz, tz))
    return by_region


@cache.memoize(timeout=6000)
def low_value_reposters() -> List[int]:
    result = db.session.execute(text('SELECT id FROM "user" WHERE bot = true or bot_override = true or suppress_crossposts = true')).scalars()
    return list(result)


def orjson_response(obj, status=200, headers=None):
    return Response(
        response=orjson.dumps(obj),
        status=status,
        headers=headers,
        mimetype="application/json"
    )


# The complement of the XML 1.0 (Fifth Edition) section 2.2 Char production:
#
#   Char ::= #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
#
# A single search therefore finds any character XML cannot represent. Matching on
# code points rather than on byte patterns is what makes the check unbypassable:
# a forbidden code point has exactly one code point value however it was spelled.
# Lone surrogates are included because a Python str can hold them and XML cannot.
_XML_CHAR_RANGES = (
    (0x9, 0x9), (0xA, 0xA), (0xD, 0xD),
    (0x20, 0xD7FF), (0xE000, 0xFFFD), (0x10000, 0x10FFFF),
)
_XML_FORBIDDEN_CHAR_RE = re.compile(
    '[^' + ''.join(f'{chr(lo)}-{chr(hi)}' for lo, hi in _XML_CHAR_RANGES) + ']'
)


def is_valid_xml_utf8(pystring):
    """Check if a string is valid UTF-8 XML character data.

    Accepts str or bytes.

    bytes must decode as *strict* UTF-8. RFC 3629 section 3 forbids overlong
    forms, stray continuation bytes, truncated sequences, surrogate encodings and
    anything above U+10FFFF; none of them denotes a code point, so a decode
    failure is an outright rejection. Decoding strictly is also what closes the
    respelling bypass RFC 3629 section 10 describes: a forbidden code point
    re-encoded as a longer sequence decodes to the same code point and is caught
    by the same test as its shortest form.

    Every resulting code point must then satisfy the XML 1.0 5e section 2.2 Char
    production. A str is checked directly, without an encode/decode round trip,
    so a lone surrogate is reported invalid instead of being silently discarded.
    """
    if isinstance(pystring, bytes):
        try:
            pystring = pystring.decode('utf-8')
        except UnicodeDecodeError:
            return False

    return _XML_FORBIDDEN_CHAR_RE.search(pystring) is None


def archive_post(post_id: int, s3_connection):
    session = get_task_session()  # noqa: F811
    try:
        with patch_db_session(session):
            if current_app.debug:
                filename = f'post_{post_id}{gibberish(5)}.json'
            else:
                filename = f'post_{post_id}.json'
            post = session.get(Post, post_id)

            if post is None:
                return

            # D1335. Archiving moves the post's body and every reply INTO the
            # archive and removes them from the database, so a second run starts
            # from a post that has nothing left: it writes `body: null` and
            # `replies: []` over the archive the first run made, at the same
            # path, and the text is then gone from both places. Measured:
            #
            #   PROBE pi after one archive:    body='wwwwwwwwwwww' replies=1
            #   PROBE pj after a second archive: path_same=True body=None replies=0
            #
            # `archive_old_posts` selects `WHERE p.archived IS NULL`, so nothing
            # reaches here twice today. That is the caller's care, not this
            # function's, and the cost of being wrong about it is unrecoverable.
            if post.archived:
                return

            # Delete thumbnail and medium sized versions if post has an image
            if post.image_id is not None:

                image_file = session.get(File, post.image_id)
                if image_file:

                    # Delete thumbnail
                    if image_file.thumbnail_path:
                        if image_file.thumbnail_path.startswith('app/'):
                            # Local file deletion
                            try:
                                os.unlink(image_file.thumbnail_path)
                            except (OSError, FileNotFoundError):
                                pass
                        elif store_files_in_s3() and s3_key_from_url(image_file.thumbnail_path):
                            # S3 file deletion. `s3_key_from_url` is the one
                            # reading of what an S3 URL of ours is and which key
                            # it names; this used to be
                            # `.split(S3_PUBLIC_URL)[-1].lstrip('/')` behind a
                            # prefix test with no boundary (D1343).
                            try:
                                s3_connection.delete_object(
                                    Bucket=current_app.config['S3_BUCKET'],
                                    Key=s3_key_from_url(image_file.thumbnail_path))
                            except Exception:
                                pass
                        image_file.thumbnail_path = None

                    # Delete medium sized version (file_path)
                    if image_file.file_path:
                        if image_file.file_path.startswith('app/'):
                            # Local file deletion
                            try:
                                os.unlink(image_file.file_path)
                            except (OSError, FileNotFoundError):
                                pass
                        elif store_files_in_s3() and s3_key_from_url(image_file.file_path):
                            # S3 file deletion. `s3_key_from_url` is the one
                            # reading of what an S3 URL of ours is and which key
                            # it names; this used to be
                            # `.split(S3_PUBLIC_URL)[-1].lstrip('/')` behind a
                            # prefix test with no boundary (D1343).
                            try:
                                s3_connection.delete_object(
                                    Bucket=current_app.config['S3_BUCKET'],
                                    Key=s3_key_from_url(image_file.file_path))
                            except Exception:
                                pass
                        image_file.file_path = None

                session.commit()

            if post.reply_count == 0 and (post.body is None or len(post.body) < 200):  # don't save to json when the url of the json will be longer than the savings from removing the body
                return

            save_this = {}

            save_this['id'] = post.id
            save_this['version'] = 1
            save_this['body'] = post.body
            save_this['body_html'] = post.body_html
            save_this['replies'] = []
            post.body = None
            post.body_html = None
            if post.reply_count:
                from app.post.util import post_replies  # cycle: app.post.util imports from this module
                # Get replies sorted by 'hot' with scores preserved - keep hierarchical structure
                hot_replies = post_replies(post, 'hot', None, db_only=True)  # No viewer to get all replies

                # Serialization of hierarchical tree
                def serialize_tree(reply_tree):
                    result = []
                    for reply_dict in reply_tree:
                        comment = reply_dict['comment']
                        if comment.visibility not in (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED):
                            # D18: an archive keeps the position of a non-public reply, never its content
                            result.append({
                                'id': int(comment.id),
                                'parent_id': int(comment.parent_id) if comment.parent_id else None,
                                'depth': int(comment.depth) if comment.depth else 0,
                                'post_id': int(comment.post_id),
                                'visibility': comment.visibility,
                                'path': list(comment.path) if comment.path else [],
                                'replies': serialize_tree(reply_dict['replies'])
                            })
                            continue
                        serialized = {
                            'id': int(comment.id) if comment.id else None,
                            'visibility': comment.visibility,
                            'body': str(comment.body) if comment.body else '',
                            'body_html': str(comment.body_html) if comment.body_html else '',
                            'posted_at': comment.posted_at.isoformat() if comment.posted_at else None,
                            'edited_at': comment.edited_at.isoformat() if comment.edited_at else None,
                            'score': int(comment.score) if comment.score else 0,
                            'ranking': float(comment.ranking) if comment.ranking else 0.0,
                            'parent_id': int(comment.parent_id) if comment.parent_id else None,
                            'distinguished': bool(comment.distinguished),
                            'deleted': bool(comment.deleted),
                            'deleted_by': int(comment.deleted_by) if comment.deleted_by else None,
                            'user_id': int(comment.user_id) if comment.user_id else None,
                            'depth': int(comment.depth) if comment.depth else 0,
                            'language_id': int(comment.language_id) if comment.language_id else None,
                            'replies_enabled': bool(comment.replies_enabled),
                            'community_id': int(comment.community_id) if comment.community_id else None,
                            'up_votes': int(comment.up_votes) if comment.up_votes else 0,
                            'down_votes': int(comment.down_votes) if comment.down_votes else 0,
                            'child_count': int(comment.child_count) if comment.child_count else 0,
                            'path': list(comment.path) if comment.path else [],
                            'answer': bool(comment.answer),
                            'author_name': str(comment.author.display_name()) if comment.author and comment.author.display_name() else 'Unknown',
                            'author_id': int(comment.author.id) if comment.author and comment.author.id else None,
                            'author_indexable': bool(comment.author.indexable) if comment.author else True,
                            'author_deleted': bool(comment.author.deleted) if comment.author else False,
                            'author_user_name': comment.author.user_name if comment.author else False,
                            'author_ap_id': comment.author.ap_id if comment.author else False,
                            'author_ap_profile_id': comment.author.ap_profile_id if comment.author else False,
                            'author_reputation': comment.author.reputation if comment.author else 0,
                            'author_created': comment.author.created.isoformat() if comment.author else None,
                            'author_ap_domain': comment.author.ap_domain if comment.author else '',
                            'author_bot': comment.author.bot if comment.author else False,
                            'author_banned': comment.author.banned if comment.author else False,
                            'replies': serialize_tree(reply_dict['replies'])
                        }
                        result.append(serialized)
                    return result

                save_this['replies'] = serialize_tree(hot_replies)

            if store_files_in_s3():
                # upload orjson(save_this) to a file in S3 named f'archived/{filename}'
                # save url to  new file into s3_url variable
                s3_key = f'archived/{filename}.gz'
                json_data = orjson.dumps(save_this)
                compressed_data = gzip.compress(json_data)

                s3_connection.put_object(
                    Bucket=current_app.config['S3_BUCKET'],
                    Key=s3_key,
                    Body=compressed_data,
                    ContentType='application/gzip',
                    ContentEncoding='gzip'
                )

                s3_url = f"https://{current_app.config['S3_PUBLIC_URL']}/{s3_key}"

                post.archived = s3_url
            else:
                ensure_directory_exists('app/static/media/archived')
                file_path = f'app/static/media/archived/{filename}'
                with gzip.open(file_path + '.gz', 'wb') as f:
                    f.write(orjson.dumps(save_this))
                post.archived = file_path + '.gz'

            session.commit()

            # Delete all post_replies associated with the post
            # First, get all reply IDs that have bookmarks by users other than the reply author
            bookmarked_reply_ids = set(
                session.execute(text('''
                    SELECT DISTINCT prb.post_reply_id
                    FROM post_reply_bookmark prb
                    JOIN post_reply pr ON prb.post_reply_id = pr.id
                    WHERE pr.post_id = :post_id
                '''), {'post_id': post.id}).scalars()
            )

            for reply in session.query(PostReply).filter(PostReply.post_id == post.id).order_by(desc(PostReply.created_at)):
                session.add(ArchivedPostReply(user_id=reply.user_id, post_id=post.id, post_reply_id=reply.id,
                                              created_at=reply.created_at))
                if reply.id not in bookmarked_reply_ids:
                    reply.delete_dependencies()
                    session.delete(reply)
                session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def user_in_restricted_country(user: User) -> bool:
    restricted_countries = get_setting('nsfw_country_restriction', '').split('\n')
    return user.ip_address_country and user.ip_address_country in [country_code.strip() for country_code in restricted_countries]


@cache.memoize(timeout=80600)
def libretranslate_string(text: str, source: str, target: str):
    try:
        lt = LibreTranslateAPI(current_app.config['TRANSLATE_ENDPOINT'], api_key=current_app.config['TRANSLATE_KEY'])
        return lt.translate(text, source=source, target=target)
    except Exception as e:
        current_app.logger.exception(str(e))
        return ''


def to_srgb(im: Image.Image, assume="sRGB"):
    """ Convert a jpeg to sRGB, from other color profiles like CMYK. Test with testing_data/sample-wonky.profile.jpg.
     See https://civitai.com/articles/18193 for background and the source of this code. """
    srgb_cms = ImageCms.createProfile("sRGB")
    srgb_wrap = ImageCms.ImageCmsProfile(srgb_cms)

    # 1) source profile
    #
    # The ICC bytes come out of an uploaded or federated image, so they are
    # attacker-controlled and may be corrupt. littlecms refuses bytes it cannot
    # parse by raising OSError("cannot open profile from string") -- NOT
    # PyCMSError, and not AttributeError. This construction used to sit outside
    # the try below, so neither `except` arm could see that OSError and it
    # escaped to_srgb entirely, defeating the graceful fallback this function
    # exists to provide. Both routes into app/shared/post.py's upload path turn
    # an escaped exception into a RAW MESSAGE SHOWN TO THE USER: the web routes
    # flash `_('Your post was not accepted because %(reason)s', reason=str(ex))`
    # (app/community/routes.py:1083, app/post/routes.py:155/840/1077), and the
    # API's shared_error_handler falls through to
    # `{"code": 400, "message": str(e), "status": "Bad Request"}`
    # (app/api/alpha/__init__.py:113).
    #
    # A profile we cannot parse is treated exactly as no profile at all: the
    # `assume` profile is substituted and the conversion below proceeds
    # normally. PyCMSError is caught alongside OSError because ImageCms wraps
    # some failures in its own exception type, which is not an OSError
    # subclass; between them they cover every refusal this constructor makes.
    icc_bytes = im.info.get("icc_profile")
    src = None
    if icc_bytes:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        except (OSError, ImageCms.PyCMSError):
            src = None
    if src is None:
        src = ImageCms.createProfile(assume)

    # 2) CMYK → RGB first
    if im.mode == "CMYK":
        im = im.convert("RGB")

    try:
        im = ImageCms.profileToProfile(
            im, src, srgb_cms,
            outputMode="RGB",
            renderingIntent=0,
            flags=ImageCms.Flags["BLACKPOINTCOMPENSATION"],
        )
        # keep an sRGB tag just in case
        im.info["icc_profile"] = srgb_wrap.tobytes()
    except ImageCms.PyCMSError:
        # Fallback: just convert without ICC
        im = im.convert("RGB")
    except AttributeError:  # pragma: no cover -- unreachable on the pinned Pillow.
        # Fallback, older versions of PIL have a different attribute name.
        # This arm exists for Pillow releases whose ImageCms module exposed
        # the BLACKPOINTCOMPENSATION flag as `ImageCms.FLAGS` rather than
        # `ImageCms.Flags`. The pinned version (Pillow 12.3.0, confirmed via
        # `hasattr`) has `Flags` and not `FLAGS`, so the `ImageCms.Flags[...]`
        # lookup in the try block above always succeeds, and every failure
        # profileToProfile can raise on this version surfaces as
        # ImageCms.PyCMSError (proven interactively: mismatched mode/profile
        # combinations, e.g. a LAB profile against an RGB image, raise
        # PyCMSError -- "cannot build transform" -- never AttributeError).
        # Reaching this branch would require downgrading Pillow, which is out
        # of scope for a test-only change. Note also that if this arm were
        # ever reached, its own fallback below still references the
        # nonexistent `ImageCms.FLAGS` and would itself raise AttributeError,
        # uncaught by the `except ImageCms.PyCMSError` two lines down -- a
        # latent bug in this dead code, reported rather than fixed here.
        try:
            im = ImageCms.profileToProfile(
                im, src, srgb_cms,
                outputMode="RGB",
                renderingIntent=0,
                flags=ImageCms.FLAGS["BLACKPOINTCOMPENSATION"],
            )
            # keep an sRGB tag just in case
            im.info["icc_profile"] = srgb_wrap.tobytes()
        except ImageCms.PyCMSError:
            pass

    return im


@cache.memoize(timeout=30)
def show_explore():
    return num_topics() > 0 or num_feeds() > 0


@cache.memoize(timeout=30)
def fediverse_domains():
    return [instance.domain for instance in db.session.query(Instance).filter(Instance.id != 1).all() if instance.online()]


def rewrite_href(url: str) -> str:
    if '/post/' in url or ('/c/' in url and '/p/' in url) or ('/m/' in url and '/t/' in url and '/comment/' not in url):
        post = Post.get_by_ap_id(url)
        if post:
            if post.slug:
                return post.slug
            else:
                return f'/post/{post.id}'
    elif '/comment/' in url:
        post_reply = PostReply.get_by_ap_id(url)
        if post_reply:
            return f'/comment/{post_reply.id}'
    elif ('/c/' in url and '/p/' not in url) or ('/m/' in url and '/t/' not in url):
        community = db.session.query(Community).filter(Community.ap_profile_id == url, Community.banned == False).first()
        if community and not community.is_local():
            url = f'/c/{community.link()}'
    else:
        # U-rewrite-href-post: a matched Post rewrites to its local url, like the post, reply and community rules
        post = Post.get_by_ap_id(url)
        if post:
            return post.slug if post.slug else f'/post/{post.id}'
        post_reply = PostReply.get_by_ap_id(url)
        if post_reply:
            return f'/comment/{post_reply.id}'

    return url


@cache.memoize(timeout=600)
def user_pronouns() -> defaultdict:
    result = defaultdict(str)
    pronouns = db.session.query(UserExtraField).filter(or_(func.lower(UserExtraField.label) == 'pronouns',
                                                           func.lower(UserExtraField.label) == 'species'))
    for pronoun in pronouns:
        if len(pronoun.text) <= 22:
            if '<' in pronoun.text and '>' in pronoun.text:
                result[pronoun.user_id] = html_to_text(pronoun.text)
            else:
                result[pronoun.user_id] = pronoun.text
    return result


def following_user_ids(user_id):
    if user_id == 0:
        return []
    stmt = (
        select(User.id)
        .join(UserFollower, UserFollower.remote_user_id == User.id)
        .where(
            User.banned == False,
            UserFollower.local_user_id == user_id,
            UserFollower.is_inward == False
        )
    )

    return db.session.execute(stmt).scalars().all()


SHORTHAND_HEX_COLOR_PATTERN = re.compile(r'#[0-9A-Fa-f]{3}')


def expand_hex_color(text: str) -> str:
    """Expand CSS three-digit shorthand: '#abc' -> '#aabbcc'. Case is preserved.

    Total on str input: anything that is not '#' followed by exactly three hex
    digits is returned UNCHANGED, and nothing is raised. The values reaching
    this function are CSS colours supplied by API clients
    (app/api/alpha/utils/community.py, lines 547, 553, 600 and 606), so raising
    would turn bad input into a 500. Two things that guard motivates:

    * it used to index text[1]..text[3] with no guard at all, so
      expand_hex_color('#ab') raised IndexError. Every call site happens to
      test `len(...) == 4` first, so that was unreachable -- but only by
      accident of the caller.
    * a length test is not a format test. 'abcd' is four characters, passed
      every one of those guards, and was silently expanded to '#bbccdd'. It is
      now stored as the client sent it rather than turned into a different
      colour.
    """
    if not SHORTHAND_HEX_COLOR_PATTERN.fullmatch(text):
        return text
    return "#" + text[1] * 2 + text[2] * 2 + text[3] * 2


def scale_gif(path, scale, new_path=None):
    # from https://stackoverflow.com/a/69850807
    gif = Image.open(path)
    if not new_path:
        new_path = path
    old_gif_information = {
        'loop': bool(gif.info.get('loop', 1)),
        'duration': gif.info.get('duration', 40),
        'background': gif.info.get('background', 223),
        'extension': gif.info.get('extension', (b'NETSCAPE2.0')),
        'transparency': gif.info.get('transparency', 223)
    }
    new_frames = get_new_frames(gif, scale)
    save_new_gif(new_frames, old_gif_information, new_path)


def get_new_frames(gif, scale):
    new_frames = []
    actual_frames = gif.n_frames
    for frame in range(actual_frames):
        gif.seek(frame)
        new_frame = Image.new('RGBA', gif.size)
        new_frame.paste(gif)
        new_frame.thumbnail(scale)
        new_frames.append(new_frame)
    return new_frames


def save_new_gif(new_frames, old_gif_information, new_path):
    new_frames[0].save(new_path,
                       save_all = True,
                       append_images = new_frames[1:],
                       duration = old_gif_information['duration'],
                       loop = old_gif_information['loop'],
                       background = old_gif_information['background'],
                       extension = old_gif_information['extension'] ,
                       transparency = old_gif_information['transparency'])


@cache.memoize(timeout=100)
def community_membership_private(user_id: int) -> List[int]:
    community_ids = db.session.execute(text("""SELECT c.id FROM "community" as c
                                                INNER JOIN community_member cm on c.id = cm.community_id
                                                WHERE c.private is true AND cm.user_id = :user_id AND cm.is_banned is false"""),
                                       {'user_id': user_id}).scalars()
    return list(community_ids)


def intlist_to_strlist(input: List[int]) -> List[str]:
    return [str(x) for x in input]


def human_filesize(size_bytes):
    """Convert bytes to human-readable string (e.g. 1.2 MB)."""
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.1f} {units[i]}"


def compaction_level():
    compact_level = request.cookies.get('compact_level', None)
    if current_app.config['HTTP_PROTOCOL'] == 'mixed' and compact_level is None:
        compact_level = 'compact-min compact-max'
    return compact_level


def humanize_number(value):
    """Return an abbreviated, human-readable number (e.g. 1.2k instead of 1215)"""

    if not value:
        return "0"

    return format_compact_decimal(value, locale=g.locale)


def round_invisible_digits(value):
    """
    Ensure 1.0k Users always uses the 'many' plural form, but for smaller numbers
    like 123 Users, respect the usual language rules.
    """
    if value is None:
        return 0
    if format_compact_decimal(value, locale=g.locale) == str(value):
        return value
    return int(value / 1000) * 1000


def debug_checkpoint(name: str):
    """
    record a named debug checkpoint.
    returns (timestamp, delta_since_last_checkpoint)
    use in jinja like this: {{ dbg_checkpoint('some label') }}
    """
    now = time.time()
    if not hasattr(g, "_debug_checkpoints"):
        g._debug_checkpoints = []

    last_time = g._debug_checkpoints[-1][1] if g._debug_checkpoints else None
    delta = now - last_time if last_time else 0

    g._debug_checkpoints.append((name, now))
    return now, delta


@cache.memoize(timeout=60)
def get_site_as_dict() -> dict:
    # return the Site as a dict so that it can be serialized by flask-caching
    site = db.session.get(Site, 1)
    if site is None:
        # D911: no request can be served without a Site row, so make the default one rather than fail every page
        current_app.logger.warning('No Site row found; creating Site(id=1) with defaults')
        site = Site(id=1)
        db.session.add(site)
        db.session.commit()
    exclude = ['private_key']
    return { c.name: getattr(site, c.name) for c in site.__table__.columns if c.name not in exclude}


def localize_datetime(inp, locale='en'):
    try:
        return pendulum.instance(inp).diff_for_humans(locale=locale)
    except ValueError:
        return pendulum.instance(inp).diff_for_humans(locale='en')


def show_reason_why_no_federation(instance_id):
    if instance_id in blocked_instances(current_user.get_id()):
        instance = db.session.get(Instance, instance_id)
        flash(_('You have blocked %(instance_name)s which hosts this community so none of your posts or comments will be sent there.',
                instance_name=instance.domain), 'warning')

    if instance_id in banned_instances(current_user.get_id()):
        instance = db.session.get(Instance, instance_id)
        flash(_('You have been banned from %(instance_name)s which hosts this community.',
                instance_name=instance.domain), 'warning')


def log_cron_task_to_db(task_name: str):
    """Log a cron task run to the cron_job_log table.

    Operates as an "upsert" to replace existing 'last_run' timestamp.
    Always opens a new db session.

    Args:
        task_name: The 'name' column in the database.
    """

    session = get_task_session()
    try:
        # Query for existing record
        cron_log = session.query(CronJobLog).filter_by(name=task_name).first()
        if cron_log:
            cron_log.last_run = utcnow()
        else:
            cron_log = CronJobLog(name=task_name, last_run=utcnow())
            session.add(cron_log)
        session.commit()
    except Exception as e:
        logger.error(f"error while saving cron logs to db: {e}")
        session.rollback()
    finally:
        session.close()


def display_back_button():
    ua = request.user_agent.string or ""
    if "iPhone" in ua or "iPad" in ua or "iPod" in ua:
        if request.referrer and request.referrer.startswith(current_app.config['SERVER_URL']):
            return 'display_back_button'
        else:
            return ''
    else:
        return ''


@cache.memoize(timeout=300)
def is_invalid_get_request_uri(uri):
    if current_app.debug:
        return False

    try:
        f = furl(uri)
        if not f.host:
            return True

        if f.host.endswith(".local"):
            return True

        if f.scheme not in ("http", "https"):
            return True

        # D1358, and the half that mattered most. `furl(...).host` keeps the
        # BRACKETS of an IPv6 literal: for `https://[::1]/x` it is `'[::1]'`, so
        # `ipaddress.ip_address` raised ValueError, `getaddrinfo('[::1]', None)`
        # raised `gaierror [Errno -2] Name or service not known`, and the DNS
        # handler below FAILS OPEN -- so every IPv6 literal was allowed, `[::1]`
        # and `[fd00::1]` included, with no DNS control needed to get there.
        # Measured for all three of `[::1]`, `[fd00::1]` and
        # `[::ffff:127.0.0.1]`.
        host = f.host
        if host.startswith('[') and host.endswith(']'):
            host = host[1:-1]

        # check if host is an IP literal
        try:
            ip = ipaddress.ip_address(host)
            ips = [ip]
        except ValueError:
            # otherwise, resolve hostname and check the IP(s) associated with that.
            # Resolution can fail transiently (a single flaky/overloaded nameserver,
            # packet loss, UDP rate-limiting). Don't let a momentary DNS blip mark a
            # valid peer invalid: on a resolution failure, fail open (return False)
            # rather than treating the URI as invalid.
            try:
                infos = socket.getaddrinfo(host, None)
            except (socket.gaierror, socket.timeout):
                return False

            ips = []
            for info in infos:
                sockaddr = info[4]
                ip_str = sockaddr[0]
                ips.append(ipaddress.ip_address(ip_str))

        # D1358. `not ip.is_global` alone let four kinds of address through, all of
        # which can reach this host or its neighbours. Measured with Python
        # 3.13.15's own `ipaddress`:
        #
        #   ::7f00:1             is_global=True   IPv4-compatible IPv6 (RFC 4291),
        #                                         127.0.0.1 in the low 32 bits
        #   ::ffff:0:127.0.0.1   is_global=True   the same, in the other spelling
        #   64:ff9b::7f00:1      is_global=True   the NAT64 well-known prefix
        #                                         (RFC 6052) embedding 127.0.0.1 --
        #                                         on a host behind a NAT64 gateway
        #                                         that is a route to loopback
        #   64:ff9b::a00:1       is_global=True   the same, embedding 10.0.0.1
        #   ff02::1              is_global=True   IPv6 all-nodes multicast
        #   224.0.0.1            is_global=True   IPv4 all-hosts multicast
        #
        # `is_reserved` covers the first four and `is_multicast` the last two, so
        # the three tests together need no prefix list to maintain. Checked against
        # 8.8.8.8, 1.1.1.1, 93.184.216.34, 2606:4700:4700::1111,
        # 2001:4860:4860::8888 and 2a00:1450:4001:827::200e, all still accepted.
        #
        # The request resolves the name again to connect, so a name that answers
        # differently the second time (DNS rebinding) would get past this check
        # alone; R162 pins the connection itself, in app/pinned_http.py, which
        # refuses the same addresses with this same predicate.
        if any(is_refused_address(ip) for ip in ips):
            return True

        return False

    except Exception:
        return True


MAX_SVG_SIZE = 10 * 1024 * 1024  # 10 MB


def refuse_svg_entity_declarations(svg_bytes: bytes) -> None:
    """Raise ValueError if an uploaded SVG declares an XML entity.

    Entity declarations are the actual threat in an SVG's DTD, and they are what
    the `<!DOCTYPE svg [<!ENTITY greater "x>y">]>` bypass exploited. A DOCTYPE
    that declares none -- the `<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" ...>`
    that Illustrator and Inkscape emit -- is accepted, because refusing it buys
    nothing: filter_svg never fetches an external DTD, never dereferences a SYSTEM
    or parameter entity, and an ELEMENT/ATTLIST/NOTATION declaration is inert
    (attribute defaults declared in an internal subset are not applied). All of
    that was established by execution against the installed py-svg-hush and is
    pinned by TestFilterSvgIsSafeWithoutPreStripping.

    ENCODING ASSUMPTION, and why UTF-16 is refused outright. The scan below reads
    ASCII bytes, so it is sound only over an ASCII-compatible encoding. Every
    encoding XML permits is ASCII-compatible except UTF-16 and UTF-32 -- the
    others have to be, because the XML declaration naming them must itself be
    readable -- and py-svg-hush accepts UTF-16 but not UTF-32. A UTF-16 document
    therefore carried `<!ENTITY` straight past this scan: the literal bytes
    simply are not present, and filter_svg went on to transcode the document and
    expand the entity. XML 1.0 section 4.3.3 requires a UTF-16 entity to begin
    with a byte order mark, and py-svg-hush refuses UTF-16 without one, so
    refusing FF FE / FE FF closes the whole of that surface.

    Refusing rather than decoding first is deliberate. Nothing in PieFed produces
    or expects a UTF-16 SVG; filter_svg's output is UTF-8 whatever it is given,
    so a UTF-16 upload was never stored as UTF-16 even when it was accepted; and
    decoding attacker-chosen bytes before scanning them would make this function
    correct only as long as its decode agreed with py-svg-hush's on every input,
    which is a much harder property to hold than "we do not accept UTF-16".

    The refusal is on the encoding, not on the content, so a harmless UTF-16 SVG
    is refused too. That is the cost, and it is why the guard is kept as narrow
    as it can be: a UTF-8 BOM is not a UTF-16 BOM, and a legacy 8-bit encoding
    such as ISO-8859-1 -- which filter_svg accepts and which spells `<!ENTITY`
    with exactly those bytes -- is not affected. Refusing everything that fails a
    UTF-8 decode would have caught ISO-8859-1 as well, for no gain.

    This refuses rather than strips. A regex hunting for a declaration's closing
    '>' is defeated by quoting -- XML 1.0 section 2.8 permits '>' inside a quoted
    EntityValue, so `<!ENTITY g "x>y">` ends a non-greedy `<\\!.*?>` match early
    and hands the parser a broken document -- and stripping also destroys valid
    comments and CDATA sections that contain '>'. Refusal can do neither.

    The scan is exact rather than a substring search. XML 1.0 section 2.4 forbids
    a literal '<' in character data and in attribute values, so in a well-formed
    document a raw '<!' can only begin a markup declaration, a comment or a CDATA
    section. Comments and CDATA sections are skipped by their terminators, '-->'
    and ']]>', neither of which can be quoted away the way a declaration's '>'
    can, so `<!-- <!ENTITY x "y"> -->` is correctly accepted. Any other '<!'
    advances by two rather than to its own '>', which is what lets a DOCTYPE's
    internal subset be scanned for the entity declarations it may contain.

    The ENTITY match is case-insensitive. XML is case-sensitive and only
    `<!ENTITY` is a real declaration, so this is deliberate slack in the
    refusing direction; the comment and CDATA terminators are matched exactly,
    because being lenient about those would mean skipping over bytes unexamined.

    An unterminated comment or CDATA section is refused: their contents cannot be
    skipped safely if the terminator is missing, and the document is not
    well-formed XML anyway.

    Nothing is modified -- this only inspects -- so no valid SVG can be damaged by
    it. Its false positives are all in the safe direction: the literal text
    '<!ENTITY' inside a processing instruction or inside a DOCTYPE's system
    literal is refused rather than accepted, and no real SVG contains one.
    """
    if svg_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        raise ValueError('SVG must not be UTF-16: this scan reads ASCII bytes')

    i = 0
    while True:
        i = svg_bytes.find(b'<!', i)
        if i == -1:
            return
        if svg_bytes.startswith(b'<!--', i):
            end = svg_bytes.find(b'-->', i + 4)
            if end == -1:
                raise ValueError('SVG contains an unterminated comment')
            i = end + 3
        elif svg_bytes.startswith(b'<![CDATA[', i):
            end = svg_bytes.find(b']]>', i + 9)
            if end == -1:
                raise ValueError('SVG contains an unterminated CDATA section')
            i = end + 3
        elif svg_bytes[i:i + 8].upper() == b'<!ENTITY':
            raise ValueError('SVG entity declarations are not allowed')
        else:
            i += 2


def sanitize_svg_bytes(svg_bytes: bytes) -> bytes:
    """Sanitize an SVG, or raise ValueError if it cannot be sanitized.

    Two refusals of our own -- oversize input and entity declarations -- and then
    py-svg-hush's filter_svg, which does the actual sanitizing. Nothing here
    rewrites the bytes before filter_svg sees them: byte-level pre-processing was
    what previously both corrupted valid documents and let hostile ones through.

    filter_svg is safe on its own against XXE: verified by execution against this
    exact version, a SYSTEM entity naming file:/// or http:// is never
    dereferenced (it expands to nothing) and an external DTD is never fetched.

    It is NOT safe on its own against entity expansion, and an earlier revision
    of this docstring wrongly said it refused nested entity references outright.
    It does not. What it has is an expansion BUDGET, measured against the
    installed version:

    * expanding one entity's value may trigger at most two further expansions,
      counted transitively. `<!ENTITY b "&a;&a;">` expands and
      `<!ENTITY b "&a;&a;&a;">` does not, which is what refuses classic billion
      laughs -- a fanout of ten blows the budget on the first nested entity, at
      depth 2 as much as at depth 9. Nesting itself is permitted.
    * references in element content are not subject to that budget at all. A
      single non-nested entity referenced many times amplifies freely until a
      TOTAL expansion budget of 256 MiB is reached: 255.990 MiB of expansion
      succeeds, 256.010 MiB is refused, and a 787 KB input is enough to get
      there. MAX_SVG_SIZE bounds the input, not the output.

    So refuse_svg_entity_declarations is NOT merely defence in depth against
    expansion -- it is the only thing standing between a 10 MB upload and a
    quarter-gigabyte allocation, which is why its encoding blind spot (UTF-16)
    was worth closing. It remains defence in depth against XXE, where filter_svg
    is safe by itself. All of the above is pinned by
    TestFilterSvgIsSafeWithoutPreStripping.
    """
    if len(svg_bytes) > MAX_SVG_SIZE:
        raise ValueError(f"SVG file too large: {len(svg_bytes)} bytes (max {MAX_SVG_SIZE})")

    refuse_svg_entity_declarations(svg_bytes)

    # Allow common image MIME types in data URLs
    keep_data_url_mime_types = {
        "image": ["jpeg", "png", "gif", "webp", "avif"],
    }

    return filter_svg(svg_bytes, keep_data_url_mime_types)


def discard_unsanitized_svg(filepath: str) -> None:
    """Destroy an SVG that could not be sanitized.

    Truncate first, then unlink: truncation is what actually destroys the
    payload, and it has already happened if the unlink then fails for a reason
    of its own (a read-only directory, say).

    Only a regular file is touched. A missing path or a directory is left alone
    rather than being created or removed, so sanitize_svg's failure cases stay
    side-effect-free apart from the one file it was asked to clean.
    """
    if not os.path.isfile(filepath):
        return
    try:
        with open(filepath, 'wb'):
            pass
        os.remove(filepath)
    except OSError as e:
        current_app.logger.error(f"Could not discard unsanitized SVG {filepath}: {e}")


def sanitize_svg(filepath: str) -> bool:
    """
    Sanitize an SVG file using py-svg-hush to remove potentially dangerous elements.
    Returns True if sanitization was successful, False otherwise.

    On failure the file is destroyed before returning. Sanitized bytes are only
    written when sanitizing succeeded, so without this a failure would leave the
    attacker's original bytes on disk -- and a caller that ignored the False
    return would go on to publish them. Failing this way makes the function safe
    regardless of what the caller does with the return value.

    The invariant, stated without a count so it cannot go stale: EVERY caller
    checks the return and rejects the upload on False, and the file is destroyed
    before False is returned, so a caller that forgets is still safe. Do not
    weaken either half -- the destruction is what makes a forgetful caller safe,
    and the check is what tells the user their upload was refused.
    """
    try:

        with open(filepath, 'rb') as f:
            svg_bytes = f.read()

        sanitized_svg = sanitize_svg_bytes(svg_bytes)

        if sanitized_svg != svg_bytes:
            with open(filepath, 'wb') as f:
                f.write(sanitized_svg)
        return True
    except Exception as e:
        current_app.logger.error(f"Error sanitizing SVG: {e}")
        discard_unsanitized_svg(filepath)
        return False


def requestor_domain():
    requesting_domain = ''
    if user_agent := str(request.user_agent):
        if '+' in user_agent:
            parts = user_agent.split('+')
            requesting_domain = parts[-1].replace(')', '')
            requesting_domain = furl(requesting_domain).host
    return requesting_domain


EMAIL_RE = re.compile(
    r'^[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@'
    r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$'
)


def validate_email(value):
    return bool(EMAIL_RE.fullmatch(value.strip()))


def inspect_image_c2pa(data: bytes, mimetype: str) -> dict:
    import c2pa  # lazy: native c2pa library, loaded only when an upload is inspected
    result = {
        "c2pa": {
            "present": False,
            "ai_generated": False,
            "creator": None,
            "software": None,
        },
    }

    try:
        with c2pa.Context() as context:
            with c2pa.Reader(mimetype, io.BytesIO(data), context=context) as reader:

                result["c2pa"]["present"] = True

                manifest = reader.get_active_manifest()

                if manifest:
                    result["c2pa"]["creator"] = (
                        manifest.get("claim_generator")
                    )

                    # Inspect assertions
                    for assertion in manifest.get("assertions", []):
                        label = assertion.get("label", "")
                        value = assertion.get("data", {})

                        if label.startswith("c2pa.actions"):
                            for action in value.get("actions", []):
                                action_name = action.get("action")

                                if action_name in ["c2pa.created", "c2pa.placed"]:
                                    source = action.get("digitalSourceType", "")

                                    if "trainedAlgorithmicMedia" in source:
                                        result["c2pa"]["ai_generated"] = True

    except Exception:
        # No C2PA manifest, unsupported format, etc.
        pass
    return result

def get_event_start(post_id: int):
    post = db.session.get(Post, post_id)

    if post and post.is_event():
        if getattr(post.event, "start", False):
            return post.event.start

    return None


def roles_with(permission):
    roles = Role.query.join(RolePermission, Role.id == RolePermission.role_id).\
        filter(RolePermission.permission == permission).\
        order_by(Role.weight)
    return ', '.join([role.name for role in roles.all()])


class TaskError(Exception):
    """Raised by app/shared/tasks/ when a task is given arguments it cannot act on.

    DEFINED AT THE END OF THIS FILE DELIBERATELY, not beside get_task_session
    and patch_db_session where it thematically belongs. The reason is
    STRUCTURAL and needs no count, and it is ANCHORED TO A COMMIT because the
    un-anchored form of it was falsified within one round -- see below.

    AS OF 2a63f063^, THE COMMIT THIS CLASS WAS APPENDED IN: this file's last
    line was :5798, and the highest app/utils.py line cited anywhere in the
    tracked tree was :5743. Appending below :5798 therefore could not move a
    single cited line, however many there were. Citations written AFTER the
    append -- including this class's own register entry, D315, which cites
    :5801 -- point INTO the appended region by construction and cannot be
    shifted by it either. So the placement is safe in both directions and the
    argument does not depend on a count. Inserting beside the task-session
    helpers at :3673 WOULD move every cited line above it, which is the
    placement this argument rules out. Do not "tidy" this upward without
    re-deriving those citations first.

    DO NOT RE-DERIVE THE MAXIMUM AT HEAD AND EXPECT :5743. At dce6eee2 the
    same sweep gives 323 occurrences, 136 at or after :3673, and a maximum of
    :5801 -- because the register entry for this very class cites it. That is
    not drift; it is the claim being changed by the commit that made it, and
    anchoring, not a better number, is the fix. Later commits will move it
    again for the same reason.

    THIS DOCSTRING FIRST GAVE THE REASON AS A BARE COUNT AND THE COUNT DID NOT
    REPRODUCE, which is why it is now stated structurally. It read "358
    `utils.py:NNN` citations in tracked files, 135 of them at or after :3673".
    Sweep definition, since it is what the derivations disagree about, and
    stated precisely because "count the endpoint" alone is AMBIGUOUS and the
    two readings give different answers. Occurrences of `app/utils.py:NNN` in
    tracked files, excluding the auto-generated `#: app/utils.py:NNN` source
    references in app/translations/**/*.po AND in
    app/translations/lt/LC_MESSAGES/messages.po.original, which that glob
    misses and which carries the same references. Then:

      - for the MAXIMUM, take the ENDPOINT of a range citation. A naive `:NNN`
        regex stops at the start and reports `app/utils.py:5736-5743`'s
        maximum as 5736, which is wrong: the citation reaches 5743.
      - for the AT-OR-AFTER :3673 test, take the START. Exactly one citation
        straddles that boundary -- `app/utils.py:3663-3673`, in D60's cell at
        findings.md:2799 -- so endpoint-counting there gives 136, not 135.

    NO SINGLE RULE APPLIED UNIFORMLY YIELDS BOTH 135 AND :5743: endpoints
    everywhere gives 136 and :5743, starts everywhere gives 135 and :5736. The
    split above is what reproduces the figures below, and it is not arbitrary
    -- a range is at-or-after a line when it BEGINS there, and it reaches a
    line when it ENDS there.

    Under that definition, AS OF 2a63f063^: 322 occurrences and 135 at or
    after :3673, so the 135 reproduces exactly -- 324 if only the *.po glob is
    excluded, 844 if the whole .po family is counted. The 358 is none of
    those, and a third reviewer's sweep gave 239 unique / ~330-346 occurrences
    / 105 at-or-after. Three derivations, three answers, and the .po family is
    the main reason they diverge. The counts are withdrawn rather than
    arbitrated, because the placement never depended on one; the structural
    argument is also the only form stable under the .po question, since that
    family's own highest cited line is :4484 and including it does not change
    the maximum.

    Introduced by sub-project 22 for app/shared/tasks/pages.py's
    `move_object`, which previously raised a bare `Exception`. `raise
    Exception(...)` is the established idiom across app/shared/ -- by `ast`,
    69 bare `raise Exception(...)` statements in 9 files, so this is the FIRST
    and ONLY narrow raise in the package, not "the first among eleven sites"
    as this docstring first said (that was the design's ten-site sample plus
    this one, and the design itself wrote "and more"). It is intended as the
    migration target for the rest; see the findings register for D315.
    """
