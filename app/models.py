from __future__ import annotations

import html
import math
import os
import uuid
import re
import unicodedata
from datetime import datetime, timedelta, date, timezone
from hashlib import sha256
from time import time
from typing import List, Union
from urllib.parse import urlparse, parse_qs, urlencode, unquote
from zoneinfo import ZoneInfo

import pendulum
import jwt
from flask import current_app, g, json
from flask_babel import _, lazy_gettext as _l
from flask_babel import force_locale, gettext
from flask_login import UserMixin, current_user
from flask_sqlalchemy.query import Query
from furl import furl
from slugify import slugify
from sqlalchemy import or_, text, desc, Index, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.dialects.postgresql import BIT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy_searchable import SearchQueryMixin
from sqlalchemy_utils.types import \
    TSVectorType  # https://sqlalchemy-searchable.readthedocs.io/en/latest/installation.html
from werkzeug.security import generate_password_hash, check_password_hash

from app import db, login, cache, celery, httpx_client, constants, app_bcrypt
from app.constants import SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_MEMBER, SUBSCRIPTION_MODERATOR, SUBSCRIPTION_OWNER, \
    SUBSCRIPTION_BANNED, SUBSCRIPTION_PENDING, NOTIF_USER, NOTIF_COMMUNITY, NOTIF_TOPIC, NOTIF_POST, NOTIF_REPLY, \
    NOTIF_FEED, NOTIF_DEFAULT, NOTIF_REPORT, NOTIF_MENTION, POST_STATUS_REVIEWING, \
    POST_STATUS_PUBLISHED, POST_TYPE_VIDEO, INVITE_MEMBERS_ONLY, INVITE_MODS_ONLY, INVITE_OWNER_ONLY, ROLE_ADMIN_NAME, \
    ROLE_STAFF_NAME
import app as app_pkg


def utcnow(naive=True):
    if naive:
        return datetime.now(ZoneInfo('UTC')).replace(tzinfo=None)
    return datetime.now(ZoneInfo('UTC'))


def votes_cast_today(user_id: int) -> int:
    num = app_pkg.redis_client.get(f'votes_cast_{date.today()}_{user_id}')
    if num is None:
        return 0
    return int(num)


def ai_verdict(response):
    """The AI-detection endpoint's answer as `(detection_result, confidence)`.

    None when the endpoint did not answer usably. D1331: `Post.new` and
    `PostReply.new` both read `is_ai.json()['confidence']` and
    `['detection_result']` outright, so an endpoint answering a 200 with an error
    body, a different version of its API, or a proxy's own HTML page raised a
    KeyError (or a JSON decode error before that) and lost the post or the
    comment. Neither one is worth a post.

    `bool` is excluded explicitly because `True` is an `int` in Python and
    `True > 0.8`, so a `confidence: true` would otherwise count as certainty.
    """
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    confidence = payload.get('confidence')
    detection = payload.get('detection_result')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not isinstance(detection, str):
        return None
    return detection, confidence


def image_url_from(value, prefer_last: bool = False):
    """The url out of an actor's or object's `icon`/`image`, whatever shape a peer
    sent.

    D1325 gave the three profile-refresh tasks and `actor_json_to_model` one
    reading of these two keys. D1341 is the copy this file kept: `Post.new`'s Video
    branch read `request_json['object']['icon'][-1]['url']` behind an
    `isinstance(..., list)` test that says nothing about the list being non-empty
    or its entries being objects, so a peer's Video with `icon: []` was an
    IndexError and `icon: [5]` a TypeError -- and the post was lost either way.

    Round 143's property test looked for the shape in `app/activitypub/util.py`
    only, which is why this one survived it (fact 687, one file over). It now scans
    every file under app/.

    Which end of a list is used is kept as it was: the LAST entry for an icon, where
    the largest is conventionally offered, and the FIRST for an image. An entry that
    is unusable gives None rather than a look at the other end.

    D1405. Every shape is read through `_as_url`, so the scheme is checked here and
    nowhere else. What this function returns becomes `File.source_url`, and
    `File.view_url()`, `User.avatar_image()`, `User.cover_image()` and the Community
    and Feed equivalents all return that string unchanged when there is no local copy
    (`served_path` rewrites only our own `app/` paths). Twelve templates put
    `view_url()` in a bare `href` and eight put `avatar_image()`/`cover_image()` there,
    including `user/show_profile.html:40` -- so an actor with
    `icon: {"url": "javascript:alert(document.domain)"}` was a clickable javascript:
    link on its own profile page for every visitor. Measured:

        PROBE actor icon      source_url='javascript:alert(document.domain)'
              avatar_image()  'javascript:alert(document.domain)'
              cover_image()   'javascript:alert(2)'

    An ALLOWLIST of http(s) rather than the href blocklist `Post.url` gets
    (`url_is_storable`), and the reason is what the value is FOR: this instance fetches
    it, with `httpx`, in `make_image_sizes`. A scheme httpx cannot fetch is not a
    picture this instance could ever display, so there is no legitimate `magnet:` or
    `matrix:` image to protect and nothing to audit -- the argument recorded at
    UNSAFE_URL_SCHEMES for keeping link schemes open does not apply to an image.

    1024 is `File.source_url`'s own width, so a peer cannot make a DataError at commit
    out of a very long url either.
    """
    if isinstance(value, str):
        return _as_url(value, 1024)
    if isinstance(value, dict):
        return _as_url(value.get('url'), 1024)
    if isinstance(value, list) and value:
        entry = value[-1] if prefer_last else value[0]
        if isinstance(entry, dict):
            return _as_url(entry.get('url'), 1024)
        if isinstance(entry, str):
            return _as_url(entry, 1024)
    return None


def _as_int(value, default):
    """An integer out of a peer's document, or `default`.

    D1339's companions. These columns are Integer, Float and String, so a peer
    sending a list where a number belongs is a DataError at commit -- which
    poisons the transaction and loses the post, not just the field.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _as_float(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_text(value, limit=None):
    """A string out of a peer's document, or None, trimmed to what fits.

    `limit` is the column's own width. A peer choosing a 200-character timezone
    for a `String(30)` is a DataError at commit, which loses the post and not just
    the field -- the same arithmetic as every other value here.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:limit] if limit else value


def _as_url(value, limit=None):
    """An http(s) URL out of a peer's document, or None.

    D1403. Three of an Event's fields are URLs a peer supplies, and all three were read
    with `_as_text` -- which checks that the value is a string and trims it to the
    column's width, and says nothing about its scheme. `post/_post_full.html:219`
    renders one of them as a link:

        <a href="{{ event.online_link }}" target="_blank" rel="nofollow ugc">

    so a peer sending `onlineLink: "javascript:alert(document.domain)"` got a clickable
    `javascript:` href on the post page. Measured, straight out of `Post.new`:

        stored online_link:                'javascript:alert(document.domain)'
        stored external_participation_url: 'javascript:alert(2)'
        stored buy_tickets_link:           'javascript:alert(3)'

    `rel="nofollow ugc"` does not stop a scheme from executing, and the other two are
    stored unrendered today -- a template linking them later would inherit the hole.

    `http://` and `https://` are exactly what the local form requires of the same
    fields (`CreateEventForm.online_link`, `Regexp(r'^https?://')`), so the two
    producers of an Event now agree on what a link is. A value that is not one is
    dropped rather than refused: the rest of the event is still worth ingesting, and
    None is what these columns hold for an event that named no link at all.
    """
    text = _as_text(value, limit)
    if text is None:
        return None
    return text if text.lower().startswith(('http://', 'https://')) else None


def _as_dict(value):
    """A mapping out of a peer's document, or an empty one.

    D1397. `'type' in x and x['type'] == 'Mention'` is how this codebase reads an
    element of a peer's array, and over a STRING element `in` is a substring test
    while the subscript is `TypeError: string indices must be integers`. Every
    actor URL that happens to contain the key reaches it -- `prototype`,
    `typewriter`, `stereotype`, `.../type/1` -- and the exception leaves the inbox
    request through the handler, so one crafted `tag` entry stops the activity
    being processed at all. The arrays themselves are `isinstance(..., list)`
    checked at every site; their ELEMENTS were not.

    An empty dict rather than None, so the reads that follow need no second guard:
    `'type' in {}` is False and `{}.get('type')` is None, which is what a peer
    sending something that is not an object should amount to.

    Where a bare string IS meaningful -- an `attributedTo` entry naming an actor by
    url -- the caller keeps its own `isinstance(..., str)` arm, as
    app/activitypub/util.py:4472 and :4556 already do. This helper is for the
    elements that only ever make sense as objects.
    """
    return value if isinstance(value, dict) else {}


# R223: an event's 'More info' link federates as an extra Link attachment carrying this name, which is how
# ingest tells it apart from a Link holding the post's own url
MORE_INFO_LINK_NAME = 'More info'


def more_info_link(url: str) -> dict:
    return {'type': 'Link', 'href': url, 'name': MORE_INFO_LINK_NAME}


def is_more_info_link(attachment) -> bool:
    attachment = _as_dict(attachment)
    return attachment.get('type') == 'Link' and attachment.get('name') == MORE_INFO_LINK_NAME


def more_info_url_from(attachments):
    """The 'More info' link among a peer's attachments, scheme-checked as an Event's other links are, or None."""
    for attachment in attachments if isinstance(attachments, list) else []:
        if is_more_info_link(attachment):
            return _as_url(_as_dict(attachment).get('href'), 1024)
    return None


def actor_name_from_ap(activity_json, key='preferredUsername', limit=255):
    """The name a peer publishes for an actor under `key`, or None if it published
    nothing a name column can hold.

    D1372. Four places read `preferredUsername` by hand as
    `activity_json['preferredUsername'].strip()`, and one untrusted value failed
    three different ways: the key absent was `KeyError`, a number or a list was
    `AttributeError: 'int' object has no attribute 'strip'`, and a value wider than
    the column was a `DataError` at commit. In `refresh_user_profile_task` any of
    those aborts the task, so an actor whose document carries
    `preferredUsername: null` can never be refreshed again -- and the guard that
    was there, `except KeyError`, catches exactly one of the three.

    Stripped before the width test, because the width that matters is the width of
    what is stored. The mapping test is `public_key_pem`'s: what reaches these
    readers is whatever a peer's `.json()` returned, which may be a list or a bare
    string.
    """
    if not isinstance(activity_json, dict):
        return None
    value = _as_text(activity_json.get(key))
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


def parse_ap_timestamp(value):
    """A timestamp out of a peer's document, or None if it does not read as one.

    Named for polls when it was written (`parse_poll_end_time`) and renamed when
    the Event branches turned out to need exactly the same thing: a string from a
    peer going into a `db.DateTime` column.

    D1330. `Post.new` assigned `request_json['object']['endTime']` straight into
    `Poll.end_poll`, a DateTime column, so a peer sending `endTime: "not a
    date"` was

        DataError: (psycopg2.errors.InvalidDatetimeFormat) invalid input syntax
        for type timestamp: "not a date"

    -- which also poisons the transaction, so the post is lost as well as the
    poll. The UPDATE path in `app/activitypub/util.py` already refused a missing
    endTime and assigned the string the same way; both go through this now.

    `parse_ban_expiry` beside it is the same two-step parse and is NOT reused: it
    answers None for a date in the past, which is right for a ban and wrong for a
    poll, since a poll that has already closed still has an end time.

    An offset is converted to UTC and dropped, because `Poll.end_poll` is
    `timestamp without time zone` and everything else in this file stores naive
    UTC (`utcnow`). Storing an AWARE datetime there would leave the conversion to
    the database session's own TimeZone, so the same document would mean different
    instants on differently configured servers. What the peer's string used to do
    was worse still: assigned as a string, PostgreSQL discarded the offset
    entirely, so `12:00+05:00` stored as 12:00 rather than 07:00 and the poll
    closed five hours late (recorded by round 129's
    `test_the_end_time_string_is_cast_by_postgres_to_a_naive_datetime`, which now
    asserts the conversion instead).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def property_value_fields(attachment, limit=1024):
    """The (label, text) pairs out of an actor's `attachment`, ready to store.

    D1354. Two copies of this loop read `field_data['type']`, `['value']` and
    `['name']` outright and then called `.strip()` on both, so every one of these
    raised out of `refresh_user_profile_task` -- and a task that raises leaves the
    actor UNREFRESHABLE, which is the consequence D1325 was about. Measured, each
    as the only entry of `attachment`:

        {'type': 'PropertyValue'}                    KeyError: 'value'
        {'type': 'PropertyValue', 'value': 'x'}      KeyError: 'name'
        {'type': 'PropertyValue', 'name': 'n'}       KeyError: 'value'
        {'type': ..., 'name': 'n', 'value': 5}       TypeError: argument of type
                                                     'int' is not iterable
        {'type': ..., 'name': 5, 'value': 'x'}       AttributeError: 'int' object
                                                     has no attribute 'strip'
        {'type': ..., 'name': 'n', 'value': None}    TypeError: argument of type
                                                     'NoneType' is not iterable
        {'value': 'x', 'name': 'n'}                  KeyError: 'type'
        'a string'                                   TypeError: string indices
                                                     must be integers
        5                                            TypeError: 'int' object is
                                                     not subscriptable
        None                                         TypeError: 'NoneType' object
                                                     is not subscriptable
        name and value of 3000 characters            DataError: value too long
                                                     for type character varying

    An entry this instance cannot read is SKIPPED rather than losing the refresh:
    the rest of the profile is still worth applying. `limit` is
    `UserExtraField.label`/`text`'s own String(1024), because a column width is not
    a validation rule the peer knows about.

    The Mastodon anchor conversion stays here so both callers share it -- the
    substring test `'<a ' in value` is only safe once `value` is known to be a str.
    """
    if not isinstance(attachment, list):
        return []
    from app.utils import mastodon_extra_field_link  # cycle: app.utils imports from this module

    fields = []
    for entry in attachment:
        if not isinstance(entry, dict) or entry.get('type') != 'PropertyValue':
            continue
        label, text = entry.get('name'), entry.get('value')
        if not isinstance(label, str) or not isinstance(text, str):
            continue
        if '<a ' in text:
            text = mastodon_extra_field_link(text)
        fields.append((label.strip()[:limit], text.strip()[:limit]))
    return fields


def public_key_pem(document):
    """The PEM a peer's actor document offers, or None.

    D1354. `activity_json['publicKey']['publicKeyPem']` in
    `refresh_user_profile_task` was a KeyError for a document with no `publicKey`,
    a TypeError for one where it is a string or a number, and -- worse than either
    -- `{'publicKeyPem': None}` stored the STRING 'None' as the key, which no
    signature can ever verify against.
    """
    if not isinstance(document, dict):
        return None
    key = document.get('publicKey')
    if not isinstance(key, dict):
        return None
    pem = key.get('publicKeyPem')
    return pem if isinstance(pem, str) and pem else None


def language_from_ap(value):
    """The (code, name) of a Language out of a peer's `language` entry, or None.

    D1355. Seven sites read `['identifier']` and `['name']` off whatever the peer
    sent. Measured through `refresh_community_profile_task`, each as the only entry
    of a community's `language` list:

        'en'                                 TypeError: string indices must be
                                             integers -- and a list of plain
                                             language codes is a shape peers send
        {'identifier': 'en'}                 KeyError: 'name'
        {'name': 'English'}                  KeyError: 'identifier'
        {'identifier': 5, 'name': '...'}     ProgrammingError: operator does not
                                             exist: character varying = integer
        None / 5                             TypeError: not subscriptable
        a 50-character identifier            DataError: value too long for type
                                             character varying

    `Language.code` is String(5) and `Language.name` is String(50), so both are
    capped: a column width is not a validation rule the peer knows about. A missing
    name falls back to the code, because a language this instance can identify is
    worth keeping even unnamed -- but without an identifier there is nothing to
    key on, so that gives None and the caller skips the entry.
    """
    if not isinstance(value, dict):
        return None
    code = _as_text(value.get('identifier'), 5)
    if not code:
        return None
    return code, _as_text(value.get('name'), 50) or code


def adjust_domain_post_count(post, delta):
    """Move `Domain.post_count` by `delta` for the domain this post's url belongs to.

    D1362. `Domain.post_count` was incremented when a post was created and
    decremented in exactly one place -- an EDIT that moved a post from one domain to
    another. Deleting a post left it alone, and unlike `Community.post_count` and
    `Tag.post_count` there is no nightly recount for domains
    (`update_community_stats` and `update_hashtag_counts` cover those two), so the
    number only ever grew.

    Two things read it, and both are wrong once it has drifted:

      * `app/domain/routes.py` offers the domain's RSS feed only `if
        domain.post_count > 0`, so a domain whose only post was deleted keeps
        advertising a feed with nothing in it;
      * that feed's ETag is `f"{domain.id}_{hash(domain.post_count)}"`, and a
        conditional request matching it gets a 304 -- so a reader holding the ETag
        KEEPS THE DELETED POST until some other post arrives on the same domain.

    A post with no url has no domain and is nothing to do with this. The floor at 0
    is here because rows written before this existed are already too high, and a
    later delete must not push them negative.
    """
    # `db.session.get(Domain, None)` would answer None and the next guard would
    # catch it, so this early return is unobservable -- it is here to keep a SELECT
    # out of every delete of a post that has no url, which is most of them.
    if not post.domain_id:
        return
    domain = db.session.get(Domain, post.domain_id)
    if domain is None:
        return
    domain.post_count = max((domain.post_count or 0) + delta, 0)


def served_path(value):
    """The path `value` is served at, or `value` unchanged when it is not ours.

    D1363. Two spellings of one normalisation lived in this file. `File.view_url`,
    `medium_url` and `thumbnail_url` anchor it, taking `value[4:]` when the string
    starts with the media root; seventeen places in the icon, header, avatar and
    cover methods instead rewrote every occurrence of that prefix anywhere in the
    string. Both are reached with `source_url`, which is a peer's string, so one
    File could render two different URLs depending on which method a template
    called.

    Our own generated paths cannot contain a second copy of the prefix -- the shards
    are two characters each and the filename is last -- so this is about what a peer
    can put in `source_url`, and about there being one answer rather than two.

    The seventeen sites were each an `if startswith(...) / else` pair returning the
    rewritten value or the value itself, which is exactly this function; collapsing
    them removed thirty-four lines that no test could reach separately.
    """
    if not isinstance(value, str):
        return value
    return f'/{value[4:]}' if value.startswith('app/') else value


def markdown_source(document, require_media_type=True):
    """The markdown a peer offered in an object's `source`, or None if it offered
    none usable.

    D1346. Eleven sites read this by hand, in three spellings, and every one of
    them subscripted `content` outright:

        if 'source' in x and x['source'].get('mediaType') == 'text/markdown':
            body = x['source']['content']

    `source` is optional in ActivityPub and its shape is entirely the peer's
    choice, so each of those reads is a way to lose the whole object. Measured
    against `Post.new`, with `content` present on the object as normal:

        source={}                                       KeyError: 'mediaType'
        source={'content': 'x'}                         KeyError: 'mediaType'
        source={'mediaType': 'text/markdown'}           KeyError: 'content'
        source={'mediaType': 'text/markdown',
                'content': 5}                           TypeError: expected
                                                        string or bytes-like
                                                        object, got 'int'

    -- the last from `markdown_to_html`, which hands the value to a regex. A
    `source` that is a string rather than an object was `AttributeError: 'str'
    object has no attribute 'get'` at the six sites spelled with `.get`.

    Returning None means "the peer offered no markdown", which every caller
    already has an arm for: they fall back to the HTML in `content`, which is
    what an object without `source` has always done. An empty string is markdown
    the peer really sent, so it is returned rather than treated as absent.

    `require_media_type=False` for the one caller that has no HTML to fall back
    to -- `Feed(description=...)`, whose html comes from `summary` instead, and
    which accepted a `source` with no `mediaType` at all before this helper
    existed. Everywhere else a missing `mediaType` means the HTML in `content` is
    used, which is the older and safer reading of an ambiguous document.
    """
    if not isinstance(document, dict):
        return None
    source = document.get('source')
    if not isinstance(source, dict):
        return None
    media_type = source.get('mediaType')
    if media_type != 'text/markdown' and (require_media_type or media_type is not None):
        return None
    content = source.get('content')
    return content if isinstance(content, str) else None


def s3_key_from_url(url):
    """The object key this URL names in OUR bucket, or None if it names none.

    D1343, and the S3 half of D1324. Six places turned a URL into a key by
    `url.replace(f'https://{S3_PUBLIC_URL}/', '')` behind
    `url.startswith(f'https://{S3_PUBLIC_URL}')`, and both halves are wrong for a
    value that ARRIVES FROM A PEER. `File.source_url` does arrive from a peer --
    it is set from `request_json['object']['image']['url']` and
    `['icon'][-1]['url']`, which is what D1324 repaired for the on-disk branch of
    the same method while leaving this one alone. Measured, with
    `S3_PUBLIC_URL = cdn.example.com`:

        source_url                                     key sent to delete_objects
        https://cdn.example.com/users/victim/avatar.webp  users/victim/avatar.webp
        https://cdn.example.com.evil.test/x/y.png         the whole URL
        https://cdn.example.com/../../secret.png          ../../secret.png
        https://cdn.example.com/%2e%2e/secret.png         %2e%2e/secret.png
        https://cdn.example.com/                          '' (the empty key)

    The first line is a remote instance deleting any object in this instance's
    bucket -- another user's avatar, another community's icon -- by naming it as
    its post's image and waiting for the post to be deleted. The second is the
    prefix test having no boundary, so a host that merely STARTS with ours passed
    it.

    So: the host must EQUAL the host of `S3_PUBLIC_URL`, any path prefix in
    `S3_PUBLIC_URL` must be matched as a whole segment, the path is unquoted
    before it is read (`%2e%2e` is `..`), and a key with an empty, `.` or `..`
    segment is refused. Refusing is safe: nothing this instance wrote has such a
    key, so no legitimate delete is lost.

    Naming an object is not the same as owning it, which no URL can settle;
    `s3_object_is_referenced_elsewhere` is what answers that.
    """
    if not isinstance(url, str) or not url:
        return None
    public = current_app.config.get('S3_PUBLIC_URL') or ''
    if not public:
        return None
    base = urlparse(f'https://{public}')
    parsed = urlparse(url)
    # Hosts are case-insensitive, so a peer echoing the same object back in a
    # different case names the same object. Keys are NOT, so the path is left
    # exactly as it reads.
    if parsed.scheme not in ('http', 'https') or \
            parsed.netloc.lower() != base.netloc.lower():
        return None
    key = unquote(parsed.path).lstrip('/')
    prefix = unquote(base.path).strip('/')
    if prefix:
        if not key.startswith(prefix + '/'):
            return None
        key = key[len(prefix) + 1:]
    # An empty key needs no test of its own: `''.split('/')` is `['']`, and an
    # empty SEGMENT is already refused below. A separate `if not key` was here
    # and no mutant could kill it (fact 708).
    if any(segment in ('', '.', '..') for segment in key.split('/')):
        return None
    return key


def s3_object_is_referenced_elsewhere(url, file_id=None, post_id=None):
    """Whether any row other than this one still points at `url`.

    D1343. Two separate reasons the caller needs this, both measured:

    * a peer can put this instance's own S3 URL in a post's image, so the row
      being deleted may never have owned the object it names; and
    * `Post.url` is shared BY DESIGN. Cross-posts are found by url equality
      (`Post.cross_posts`), so a federated video mirrored into the bucket is
      named by up to ten Post rows, and deleting one of them must not take the
      file the other nine still play.

    Matching is by the URL as stored. Two spellings of one key (a different
    escaping, say) read as two objects here, which errs towards keeping a file
    that could have been removed rather than removing one that is still in use.
    """
    files = db.session.query(File.id).filter(or_(File.file_path == url,
                                                 File.thumbnail_path == url,
                                                 File.source_url == url))
    if file_id is not None:
        files = files.filter(File.id != file_id)
    if db.session.query(files.exists()).scalar():
        return True
    posts = db.session.query(Post.id).filter(or_(Post.url == url,
                                                 Post.archived == url))
    if post_id is not None:
        posts = posts.filter(Post.id != post_id)
    return db.session.query(posts.exists()).scalar()


def reputation_delta(old_effect: float, new_effect: float, low_quality: bool) -> float:
    """What a change of vote does to the author's reputation.

    The policy, which the community flag states in its own label ("Low quality /
    toxic - upvotes in here don't add to reputation"): an upvote in a low-quality
    community earns nothing, a downvote always costs, and no vote is worth
    nothing. The delta is the difference between the two votes, so withdrawing a
    vote gives back exactly what it gave, and reversing one applies the new vote
    as well as removing the old.

    `old_effect` and `new_effect` are 0 for 'no vote', +1 for an upvote and -1
    for a downvote.
    """

    def earned(effect: float) -> float:
        return 0.0 if low_quality and effect > 0 else float(effect)

    return earned(new_effect) - earned(old_effect)


class PostReplyValidationError(Exception):
    """Custom exception for PostReply validation errors"""
    pass


class FullTextSearchQuery(Query, SearchQueryMixin):
    pass


class BannedInstances(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)
    reason = db.Column(db.String(256))
    initiator = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)
    subscription_id = db.Column(db.Integer, db.ForeignKey('defederation_subscription.id'), index=True)  # is None when the ban was done by a local admin


class AllowedInstances(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class DefederationSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)


class Instance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True, unique=True)
    inbox = db.Column(db.String(256))
    shared_inbox = db.Column(db.String(256))
    outbox = db.Column(db.String(256))
    software = db.Column(db.String(50))
    version = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow)  # When an Activity was received from them
    last_successful_send = db.Column(db.DateTime)  # When we successfully sent them an Activity
    failures = db.Column(db.Integer, default=0)  # How many days that we have been unable to send (reset to 0 after every successful send)
    most_recent_attempt = db.Column(db.DateTime)  # When the most recent failure was
    dormant = db.Column(db.Boolean, default=False)  # True once this instance is considered offline and not worth sending to any more (5 days offline)
    start_trying_again = db.Column(db.DateTime)  # When to start trying again.
    gone_forever = db.Column(db.Boolean, default=False)  # True once this instance is considered offline forever - never start trying again (12 days offline)
    ip_address = db.Column(db.String(50))
    trusted = db.Column(db.Boolean, default=False, index=True)
    silenced = db.Column(db.Boolean, default=False, index=True)
    posting_warning = db.Column(db.String(512))
    nodeinfo_href = db.Column(db.String(100))
    admin_note = db.Column(db.Text)
    popular = db.Column(db.Boolean, default=True)  # New communities from here have their popular flag set

    __table_args__ = (
        Index('ix_instance_created_at_active', created_at.desc(),
              postgresql_where=text('gone_forever = false AND dormant = false')),
    )

    posts = db.relationship('Post', backref='instance', lazy='dynamic')
    post_replies = db.relationship('PostReply', backref='instance', lazy='dynamic')
    communities = db.relationship('Community', backref='instance', lazy='dynamic')

    def online(self):
        return not (self.dormant or self.gone_forever)

    def user_is_admin(self, user_id):
        role = InstanceRole.query.filter_by(instance_id=self.id, user_id=user_id).first()
        return role and role.role == 'admin'

    def votes_are_public(self):
        if self.trusted is True:  # only vote privately with untrusted instances
            return False
        return self.software.lower() == 'lemmy' or self.software.lower() == 'mbin' or self.software.lower() == 'kbin' or self.software.lower() == 'guppe groups'

    def post_count(self):
        return db.session.execute(text('SELECT count(*) as c FROM "post" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def post_replies_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "post_reply" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def known_communities_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "community" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def known_users_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "user" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def update_dormant_gone(self):
        if self.failures > 7 and self.dormant == True:
            self.gone_forever = True
        elif self.failures > 2 and self.dormant == False:
            self.dormant = True

    def can_poll(self):
        return self.software != 'lemmy'

    def can_event(self):
        return self.software != 'lemmy'

    def __repr__(self):
        return '<Instance {}>'.format(self.domain)

    @classmethod
    def unique_software_names(cls):
        return list(db.session.execute(text('SELECT DISTINCT software FROM instance ORDER BY software')).scalars())


class InstanceRole(db.Model):
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    role = db.Column(db.String(50), default='admin')

    user = db.relationship('User', lazy='joined')


# Instances that this user has blocked
class InstanceBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


# Instances that have banned this user
class InstanceBan(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    banned_until = db.Column(db.DateTime)


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    reported = db.Column(db.Boolean, default=False)
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)

    initiator = db.relationship('User', backref=db.backref('conversations_initiated', lazy='dynamic'),
                                foreign_keys=[user_id])
    messages = db.relationship('ChatMessage', backref=db.backref('conversation'), cascade='all,delete',
                               lazy='dynamic')

    def member_names(self, user_id):
        retval = []
        for member in self.members:
            if member.id != user_id:
                retval.append(member.display_name())
        return ', '.join(retval)

    def is_member(self, user):
        for member in self.members:
            if member.id == user.id:
                return True
        return False

    def instances(self):
        retval = []
        for member in self.members:
            if member.instance.id != 1 and member.instance not in retval:
                retval.append(member.instance)
        return retval

    def delete_if_abandoned(self):
        # Delete the conversation if all the participants are either remote or have left the conversation
        keep_convo = False
        for member in self.members:
            if member.is_local():
                joined = db.session.execute(text("SELECT joined FROM conversation_member WHERE user_id = :person_id AND conversation_id = :conversation_id"),
                                            {"person_id": member.id, "conversation_id": self.id}).first()

                # Returns None or a tuple, need to make it into a bool
                if joined and any(joined):
                    # There is still a local user joined to this conversation, just break and don't delete the convo
                    keep_convo = True
                    break

        if not keep_convo:
            # Delete the conversation
            Report.query.filter(Report.suspect_conversation_id == self.id).delete()
            db.session.delete(self)
            db.session.commit()

    def last_ap_id(self, sender_id):
        for message in self.messages.filter(ChatMessage.sender_id == sender_id).order_by(
                desc(ChatMessage.created_at)).limit(50):
            if message.ap_id:
                return message.ap_id
        return ''
        # most_recent_message = self.messages.order_by(desc(ChatMessage.created_at)).first()
        # if most_recent_message and most_recent_message.ap_id:
        #    return f"{current_app.config.server_name()}/private_message/{most_recent_message.id}"
        # else:
        #    return ''

    @staticmethod
    def find_existing_conversation(recipient, sender):
        sql = """SELECT
                    c.id AS conversation_id,
                    c.created_at AS conversation_created_at,
                    c.updated_at AS conversation_updated_at,
                    cm1.user_id AS user1_id,
                    cm2.user_id AS user2_id
                FROM
                    public.conversation AS c
                JOIN
                    public.conversation_member AS cm1 ON c.id = cm1.conversation_id
                JOIN
                    public.conversation_member AS cm2 ON c.id = cm2.conversation_id
                WHERE
                    cm1.user_id = :user_id_1 AND
                    cm2.user_id = :user_id_2 AND
                    cm1.user_id <> cm2.user_id;"""
        ec = db.session.execute(text(sql), {'user_id_1': recipient.id, 'user_id_2': sender.id}).fetchone()
        return db.session.get(Conversation, ec[0]) if ec else None


conversation_member = db.Table('conversation_member',
                               db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                               db.Column('conversation_id', db.Integer, db.ForeignKey('conversation.id')),
                               db.Column('joined', db.Boolean, default=True),
                               db.PrimaryKeyConstraint('user_id', 'conversation_id')
                               )


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'), index=True)
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    reported = db.Column(db.Boolean, default=False)
    read = db.Column(db.Boolean, default=False)
    encrypted = db.Column(db.String(15))
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    deleted = db.Column(db.Boolean, default=False)

    ap_id = db.Column(db.String(255), index=True, unique=True)

    sender = db.relationship('User', foreign_keys=[sender_id])


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), index=True)  # lowercase version of tag, e.g. solarstorm
    display_as = db.Column(db.String(256))  # Version of tag with uppercase letters, e.g. SolarStorm
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True)


class Licence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))


class Language(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(5), index=True)
    name = db.Column(db.String(50))


class CommunityInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(25), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    inviter_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=utcnow)

class CommunityThemeAllowed(db.Model):
    community_id = db.Column(db.Integer,db.ForeignKey('community.id'),primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'),primary_key=True)
    allowed = db.Column(db.Boolean, default=False)

community_language = db.Table('community_language',
                              db.Column('community_id', db.Integer, db.ForeignKey('community.id')),
                              db.Column('language_id', db.Integer, db.ForeignKey('language.id')),
                              db.PrimaryKeyConstraint('community_id', 'language_id')
                              )

post_tag = db.Table('post_tag', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id')),
                    db.PrimaryKeyConstraint('post_id', 'tag_id')
                    )

post_flair = db.Table('post_flair', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                      db.Column('flair_id', db.Integer, db.ForeignKey('community_flair.id')),
                      db.PrimaryKeyConstraint('post_id', 'flair_id')
                      )


post_file = db.Table('post_file', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                      db.Column('file_id', db.Integer, db.ForeignKey('file.id')),
                      db.Column('weight', db.Integer),
                      db.PrimaryKeyConstraint('post_id', 'file_id')
                      )


user_file = db.Table('user_file',
                     db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                     db.Column('file_id', db.Integer, db.ForeignKey('file.id')),
                     db.Column('size', db.Integer),
                     db.PrimaryKeyConstraint('user_id', 'file_id')
                     )


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_path = db.Column(db.String(255))
    file_name = db.Column(db.String(255))
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    alt_text = db.Column(db.String(1500))
    source_url = db.Column(db.String(1024))
    thumbnail_path = db.Column(db.String(255))
    thumbnail_width = db.Column(db.Integer)
    thumbnail_height = db.Column(db.Integer)
    hash = db.Column(BIT(256), index=True)

    user = db.relationship('User', lazy='dynamic', secondary=user_file)

    def view_url(self, resize=False):
        if self.source_url:
            if resize and '/pictrs/' in self.source_url and '?' not in self.source_url:
                return f'{self.source_url}?thumbnail=1024'
            else:
                return self.source_url
        elif self.file_path:
            if self.file_path.startswith('http'):
                return self.file_path
            return f"{current_app.config['SERVER_URL']}{served_path(self.file_path)}"
        else:
            return ''

    def medium_url(self):
        if self.file_path is None:
            return self.thumbnail_url()
        if self.file_path.startswith('http'):
            return self.file_path
        return f"{current_app.config['SERVER_URL']}{served_path(self.file_path)}"

    def thumbnail_url(self):
        if self.thumbnail_path is None:
            if self.source_url:
                return self.source_url
            else:
                return ''
        if self.thumbnail_path.startswith('http'):
            return self.thumbnail_path
        # image paths must include fqdn (not just starting with /) because apps need
        # to make a request from outside
        return f"{current_app.config['SERVER_URL']}{served_path(self.thumbnail_path)}"

    def is_image(self):
        # D1409, as `is_image_url` (app/utils.py) one round earlier: the value read here is
        # `thumbnail_url()`, which falls back to `source_url` -- a string a peer or an API
        # client supplied -- and an extension sniffed off `urlparse(url).path` is the
        # sender's to choose, because for a `javascript:` url the whole string after the
        # colon IS the path. `admin/media.html:34` gates a link on this method.
        from app.utils import has_unsafe_url_scheme  # cycle: app.utils imports from this module
        common_image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.avif', '.svg+xml',
                                   '.svg+xml; charset=utf-8']
        url = self.thumbnail_url()
        if has_unsafe_url_scheme(url):
            return False
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()
        return any(path.endswith(extension) for extension in common_image_extensions)

    def local_path_for_url(self, url: str):
        """The file this URL names on THIS server's disk, or None if it names none.

        D1324. `delete_from_disk` used to turn `source_url` into a path by string
        replacement -- `url.replace(f"{SERVER_URL}/", 'app/')` -- behind the test
        `url.startswith('http') and SERVER_NAME in url`. Both halves are wrong for
        a value that ARRIVES FROM A PEER, and `source_url` does: it is set from
        `request_json['object']['image']['url']` and `['icon'][-1]['url']` when a
        Create is processed (:2186, :2204, :2350) and from a remote actor's icon
        and image in `app/activitypub/util.py` (:741, :750).

        `SERVER_NAME in url` is a substring test, so any URL mentioning this
        instance anywhere passed it; and `replace` does not anchor, so a path
        could climb out of `app/`. Measured: a File whose source_url was
        `https://<this host>/../../tmp/probe_delete_target` deleted
        `/tmp/probe_delete_target` -- a remote instance could delete any file the
        application user can, by naming it in a post's image url and waiting for
        the post to be deleted.

        So: the host must EQUAL this server's, the path is unquoted before it is
        resolved (`%2e%2e` is `..`), and the resolved path must still be inside
        `app/`.
        """
        parsed = urlparse(url)
        if parsed.hostname != current_app.config['SERVER_NAME'] and \
                parsed.netloc != current_app.config['SERVER_NAME']:
            return None
        relative = unquote(parsed.path).lstrip('/')
        if not relative:
            return None
        root = os.path.realpath('app')
        candidate = os.path.realpath(os.path.join(root, relative))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def delete_from_disk(self, purge_cdn=True, cache_urls=None):
        """`cache_urls`, when given, receives the URLs this delete invalidates
        instead of them being flushed here.

        D1350. Deleting a post's image with `purge_cdn=False` was how every caller
        avoided one Cloudflare request per file, and the cost was that no post
        image was ever purged at all. A list lets the caller deleting a hundred
        posts flush once for all of them, so there is no reason left to turn the
        purge off.
        """
        purge_from_cache = []
        s3_files_to_delete = []
        if self.file_path:
            s3_key = s3_key_from_url(self.file_path) if _store_files_in_s3() else None
            if s3_key:
                s3_files_to_delete.append(s3_key)
                purge_from_cache.append(self.file_path)
            else:
                # D1349. The purge used to live inside `if os.path.isfile(...)`, so
                # a file already gone from disk was never purged from the CDN --
                # and the CDN is what the public reads. Measured: deleting a user
                # whose files had already been removed by any other means purged
                # nothing at all. Whether the local copy is still there says
                # nothing about whether an edge still holds it.
                if os.path.isfile(self.file_path):
                    try:
                        os.unlink(self.file_path)
                    except FileNotFoundError:
                        ...
                purge_from_cache.append(self.file_path.replace('app/', f"{current_app.config['SERVER_URL']}/"))

        if self.thumbnail_path:
            s3_key = s3_key_from_url(self.thumbnail_path) if _store_files_in_s3() else None
            if s3_key:
                s3_files_to_delete.append(s3_key)
                purge_from_cache.append(self.thumbnail_path)
            else:
                if os.path.isfile(self.thumbnail_path):  # D1349, as above
                    try:
                        os.unlink(self.thumbnail_path)
                    except FileNotFoundError:
                        ...
                purge_from_cache.append(
                    self.thumbnail_path.replace('app/', f"{current_app.config['SERVER_URL']}/"))
        if self.source_url:
            # `source_url` is the one of these three that a PEER writes, so naming
            # an object in this instance's bucket is not enough to have it deleted:
            # something else still pointing at it means this row did not own it
            # (D1343). The on-disk branch below is D1324's repair of the same
            # field.
            s3_key = s3_key_from_url(self.source_url) if _store_files_in_s3() else None
            if s3_key:
                if not s3_object_is_referenced_elsewhere(self.source_url,
                                                         file_id=self.id):
                    s3_files_to_delete.append(s3_key)
                    purge_from_cache.append(self.source_url)
            elif self.source_url.startswith('http'):
                # `local_path_for_url` answers None for anything that is not a
                # file of ours, which is what stops a peer naming someone else's
                # (D1324).
                local_path = self.local_path_for_url(self.source_url)
                if local_path:
                    try:
                        os.unlink(local_path)
                    except FileNotFoundError:
                        ...
                    purge_from_cache.append(self.source_url)

        if len(s3_files_to_delete) > 0:
            from app.shared.tasks.maintenance import delete_from_s3  # cycle: app.shared.tasks.maintenance imports from this module
            if current_app.debug:
                delete_from_s3(s3_files_to_delete)
            else:
                delete_from_s3.delay(s3_files_to_delete)

        if not purge_cdn:
            return
        if cache_urls is not None:
            cache_urls.extend(purge_from_cache)
        elif purge_from_cache:
            flush_cdn_cache(purge_from_cache)

    def filesize(self):
        size = 0
        if self.file_path and os.path.exists(self.file_path):
            size += os.path.getsize(self.file_path)
        if self.thumbnail_path and os.path.exists(self.thumbnail_path):
            size += os.path.getsize(self.thumbnail_path)
        return size


def flush_cdn_cache(url: Union[str, List[str]]):
    zone_id = current_app.config['CLOUDFLARE_ZONE_ID']
    token = current_app.config['CLOUDFLARE_API_TOKEN']
    if zone_id and token:
        if current_app.debug:
            flush_cdn_cache_task(url)
        else:
            flush_cdn_cache_task.delay(url)


@celery.task
def flush_cdn_cache_task(to_purge: Union[str, List[str]]):
    with current_app.app_context():
        zone_id = current_app.config['CLOUDFLARE_ZONE_ID']
        token = current_app.config['CLOUDFLARE_API_TOKEN']
        if zone_id and token:
            headers = {
                'Authorization': f"Bearer {token}",
                'Content-Type': 'application/json'
            }
            # url can be a string or a list of strings
            body = ''
            if isinstance(to_purge, str) and to_purge == 'all':
                body = {
                    'purge_everything': True
                }
            else:
                if isinstance(to_purge, str):
                    body = {
                        'files': [to_purge]
                    }
                elif isinstance(to_purge, list):
                    body = {
                        'files': to_purge
                    }

            if body:
                # D1351. Cloudflare's purge_cache endpoint takes at most 30 files
                # per request and answers 400 for a longer list. This sent whatever
                # it was given in one request and never read the response, so a
                # purge of 31 files failed silently -- and the callers that batch
                # (a community, a domain, a user's whole history) are exactly the
                # ones that exceed it.
                batches = [{'purge_everything': True}] if 'purge_everything' in body \
                    else [{'files': body['files'][index:index + 30]}
                          for index in range(0, len(body['files']), 30)]
                for batch in batches:
                    response = httpx_client.request(
                        'POST',
                        f'https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache',
                        headers=headers,
                        json=batch,
                        timeout=5,
                    )
                    if response.status_code != 200:
                        current_app.logger.warning(
                            'CDN purge refused: %s %s', response.status_code,
                            response.text[:200])


class Topic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_name = db.Column(db.String(50), index=True)
    name = db.Column(db.String(50))
    num_communities = db.Column(db.Integer, default=0)
    parent_id = db.Column(db.Integer)
    show_posts_in_children = db.Column(db.Boolean, default=False)
    countries = db.Column(MutableList.as_mutable(ARRAY(db.String(15))))
    communities = db.relationship('Community', lazy='dynamic', backref='topic', cascade="all, delete-orphan")

    def path(self):
        return_value = [self.machine_name]
        # D1308. The walk ended at a topic with no parent, and a cycle has none:
        # two topics each naming the other went round for ever, with the request
        # that asked for the path never answered. `topics_for_form` no longer
        # offers a topic its own descendant (D1307), so no new cycle can be
        # written here -- but a database that already holds one is a hung worker
        # per request, and this ends the walk instead.
        seen = {self.id}
        parent_id = self.parent_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent_topic = db.session.get(Topic, parent_id)
            if parent_topic is None:
                break
            return_value.append(parent_topic.machine_name)
            parent_id = parent_topic.parent_id
        return_value = list(reversed(return_value))
        return '/'.join(return_value)

    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_TOPIC).first()
        return existing_notification is not None


class Community(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    icon_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    name = db.Column(db.String(256), index=True)
    title = db.Column(db.String(256))
    description = db.Column(db.Text)  # markdown
    description_html = db.Column(db.Text)  # html equivalent of above markdown
    theme = db.Column(db.String(20), default='')
    rules = db.Column(db.Text)  # this is unused but do not remove, it breaks everything
    content_warning = db.Column(db.Text)  # "Are you sure you want to view this community?"
    subscriptions_count = db.Column(db.Integer, default=0)  # Local subscribers
    total_subscriptions_count = db.Column(db.Integer, default=0)  # Local AND remote
    post_count = db.Column(db.Integer, default=0)
    post_reply_count = db.Column(db.Integer, default=0)
    nsfw = db.Column(db.Boolean, default=False)
    nsfl = db.Column(db.Boolean, default=False)
    ai_generated = db.Column(db.Boolean, default=False, index=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    low_quality = db.Column(db.Boolean, default=False)  # upvotes earned in low quality communities don't improve reputation
    created_at = db.Column(db.DateTime, default=utcnow)
    last_active = db.Column(db.DateTime, default=utcnow)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    content_retention = db.Column(db.Integer, default=-1)
    topic_id = db.Column(db.Integer, db.ForeignKey('topic.id'), index=True)
    default_layout = db.Column(db.String(15))
    default_post_type = db.Column(db.String(15))
    posting_warning = db.Column(db.String(512))
    downvote_accept_mode = db.Column(db.Integer, default=0)  # -1 = None, 0 = All, 2 = Community members, 4 = This instance, 6 = Trusted instances
    rss_url = db.Column(db.String(2048))
    can_be_archived = db.Column(db.Boolean, default=True, index=True)
    always_translate = db.Column(db.Boolean)
    post_url_type = db.Column(db.String(15))
    question_answer = db.Column(db.Boolean, default=False)     # if this is a stackoverflow-style question and answer community
    first_federated_at = db.Column(db.DateTime, index=True, default=utcnow)

    ap_id = db.Column(db.String(255), index=True)
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)
    ap_followers_url = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_discoverable = db.Column(db.Boolean, default=False)
    ap_public_url = db.Column(db.String(255))
    ap_fetched_at = db.Column(db.DateTime)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_outbox_url = db.Column(db.String(255))
    ap_featured_url = db.Column(db.String(255))
    ap_moderators_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))

    banned = db.Column(db.Boolean, default=False)
    restricted_to_mods = db.Column(db.Boolean, default=False)
    local_only = db.Column(db.Boolean, default=False)  # only users on this instance can post. no federation.
    private = db.Column(db.Boolean, default=False)     # only members can view. no federation.
    encrypted = db.Column(db.Boolean, default=False)
    invitations = db.Column(db.Integer, default=0)     # 0 = anyone can join, 1 = apply to join, 2 = must be invited by a member, 3 = must be invited by a mod, 4 = must be invited by owner
    new_mods_wanted = db.Column(db.Boolean, default=False)
    searchable = db.Column(db.Boolean, default=True)
    private_mods = db.Column(db.Boolean, default=False)
    un_moderated = db.Column(db.Boolean, default=False)

    active_daily = db.Column(db.Integer, default=0)
    active_weekly = db.Column(db.Integer, default=0)
    active_monthly = db.Column(db.Integer, default=0)
    active_6monthly = db.Column(db.Integer, default=0)

    # Which feeds posts from this community show up in
    show_popular = db.Column(db.Boolean, default=True)
    show_all = db.Column(db.Boolean, default=True)

    ignore_remote_language = db.Column(db.Boolean, default=False)
    ignore_remote_gen_ai = db.Column(db.Boolean, default=False)

    search_vector = db.Column(TSVectorType('name', 'title', 'description', 'rules', auto_index=False))

    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")
    wiki_pages = db.relationship('CommunityWikiPage', lazy='dynamic', backref='community', cascade="all, delete-orphan")
    icon = db.relationship('File', lazy='joined', foreign_keys=[icon_id], single_parent=True, backref='community',
                           cascade="all, delete-orphan")
    image = db.relationship('File', foreign_keys=[image_id], single_parent=True, cascade="all, delete-orphan")
    languages = db.relationship('Language', lazy='dynamic', secondary=community_language,
                                backref=db.backref('communities', lazy='dynamic'))
    flair = db.relationship('CommunityFlair', backref=db.backref('community'), cascade="all, delete-orphan")
    rss_feeds = db.relationship('RssFeed', backref=db.backref('community'), cascade="all, delete-orphan")
    modlog = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.community_id", back_populates='community')

    __table_args__ = (
        db.Index(
            'idx_community_fts',
            'search_vector',
            postgresql_using='gin'
        ),
    )

    def language_ids(self):
        return [language.id for language in self.languages.all()]

    def language_names(self):
        return [language.name for language in self.languages.filter(Language.code != 'und').all()]

    def icon_image(self, size='default') -> str:
        if self.icon_id is not None:
            if size == 'default':
                if self.icon.file_path is not None:
                    return served_path(self.icon.file_path)
                if self.icon.source_url is not None:
                    return served_path(self.icon.source_url)
            elif size == 'tiny':
                if self.icon.thumbnail_path is not None:
                    return served_path(self.icon.thumbnail_path)
                if self.icon.source_url is not None:
                    return served_path(self.icon.source_url)
        return '/static/images/1px.gif'

    @cache.memoize(timeout=500)
    def header_image(self) -> str:
        if self.image_id is not None:
            if self.image.file_path is not None:
                return served_path(self.image.file_path)
            if self.image.source_url is not None:
                return served_path(self.image.source_url)
        return ''

    def display_name(self) -> str:
        if self.ap_id is None:
            return self.title
        else:
            return f"{self.title}@{self.ap_domain}"

    def link(self) -> str:
        return self.name if self.ap_id is None else self.ap_id.lower()

    def lemmy_link(self) -> str:
        if self.ap_id is None:
            return f"!{self.name}@{current_app.config['SERVER_NAME']}"
        else:
            return f"!{self.ap_id.lower()}"

    @cache.memoize(timeout=300)
    def moderators(self):
        return CommunityMember.query.filter((CommunityMember.community_id == self.id) &
                                            (or_(
                                                CommunityMember.is_owner,
                                                CommunityMember.is_moderator
                                            ))
                                            ).filter(CommunityMember.is_banned == False).all()

    def is_member(self, user):
        if user.is_anonymous:
            return False
        if user is None:
            return CommunityMember.query.filter(CommunityMember.user_id == current_user.get_id(),
                                                CommunityMember.community_id == self.id,
                                                CommunityMember.is_banned == False).all()
        else:
            return CommunityMember.query.filter(CommunityMember.user_id == user.id,
                                                CommunityMember.community_id == self.id,
                                                CommunityMember.is_banned == False).all()

    def is_moderator(self, user=None):
        if user is None:
            return any(moderator.user_id == current_user.get_id() for moderator in self.moderators())
        else:
            return any(moderator.user_id == user.id for moderator in self.moderators())

    def is_owner(self, user=None):
        if user is None:
            return any(moderator.user_id == current_user.get_id() and moderator.is_owner for moderator in
                       self.moderators())
        else:
            return any(moderator.user_id == user.id and moderator.is_owner for moderator in self.moderators())

    def can_invite(self, user=None):
        if user is None and current_user.is_anonymous:
            return False
        else:
            u = current_user if user is None else user
            if self.invitations == INVITE_MEMBERS_ONLY and not self.is_member(u):
                return False
            elif self.invitations == INVITE_MODS_ONLY and not self.is_moderator(u):
                return False
            elif self.invitations == INVITE_OWNER_ONLY and not self.is_owner(u):
                return False
            return True

    def num_owners(self):
        result = 0
        for moderator in self.moderators():
            if moderator.is_owner:
                result += 1
        return result

    def is_instance_admin(self, user):
        if self.instance_id:
            instance_role = InstanceRole.query.filter(InstanceRole.instance_id == self.instance_id,
                                                      InstanceRole.user_id == user.id,
                                                      InstanceRole.role == 'admin').first()
            return instance_role is not None
        else:
            return False

    def is_admin_or_staff(self, user):
        return user.is_admin_or_staff()

    def profile_id(self):
        retval = self.ap_profile_id if self.ap_profile_id else f"{current_app.config['SERVER_URL']}/c/{self.name}"
        return retval.lower()

    def public_url(self):
        result = self.ap_public_url if self.ap_public_url else f"{current_app.config['SERVER_URL']}/c/{self.name}"
        return result

    def is_local(self):
        return self.ap_id is None or self.profile_id().startswith(f"{current_app.config['SERVER_URL']}")

    def local_url(self):
        if self.is_local():
            return self.ap_profile_id
        else:
            return f"{current_app.config['SERVER_URL']}/c/{self.ap_id}"

    def humanize_subscribers(self, total=True, **kwargs):
        """Return an abbreviated, human readable number of followers (e.g. 1.2k instead of 1215)"""
        from app.utils import humanize_number  # cycle: app.utils imports from this module

        if "value" in kwargs:
            subscribers = kwargs.get("value")
        else:
            if total:
                subscribers = self.total_subscriptions_count if self.total_subscriptions_count else self.subscriptions_count
            else:
                subscribers = self.subscriptions_count

        return humanize_number(subscribers)

    def has_poster(self, user) -> bool:
        post_reply_count = 0
        post_count = db.session.execute(text('SELECT count(*) as c FROM "post" WHERE community_id = :community_id AND user_id = :user_id'),
                                        {'community_id': self.id, 'user_id': user.id}).scalar_one_or_none()
        if not post_count:
            post_reply_count = db.session.execute(text('SELECT count(*) as c FROM "post_reply" WHERE community_id = :community_id AND user_id = :user_id'),
                                                  {'community_id': self.id, 'user_id': user.id}).scalar_one_or_none()
        return post_count or post_reply_count

    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = db.session.query(NotificationSubscription).\
            filter(NotificationSubscription.entity_id == self.id,
                   NotificationSubscription.user_id == user_id,
                   NotificationSubscription.type == NOTIF_COMMUNITY).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when there is a post in this community
    def notification_subscribers(self):
        return list(db.session.execute(
            text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :community_id AND type = :type '),
            {'community_id': self.id, 'type': NOTIF_COMMUNITY}).scalars())


    # instances that have users which are members of this community. (excluding the current instance)
    def following_instances(self, include_dormant=False, mod_hosts_only=False) -> List[Instance]:
        instances = db.session.query(Instance).join(User, User.instance_id == Instance.id).join(CommunityMember,
                                                                                                CommunityMember.user_id == User.id)
        instances = instances.filter(CommunityMember.community_id == self.id, CommunityMember.is_banned == False)
        if mod_hosts_only:
            instances = instances.filter(CommunityMember.is_moderator == True)
        if not include_dormant:
            instances = instances.filter(Instance.dormant == False)
        instances = instances.filter(Instance.id != 1, Instance.gone_forever == False)
        return instances.distinct().all()


    def has_followers_from_domain(self, domain: str) -> bool:
        instances = db.session.query(Instance).join(User, User.instance_id == Instance.id).join(CommunityMember,
                                                                                    CommunityMember.user_id == User.id)
        instances = instances.filter(CommunityMember.community_id == self.id, CommunityMember.is_banned == False)
        for instance in instances:
            if instance.domain == domain:
                return True
        return False

    def loop_videos(self) -> bool:
        return 'gifs' in self.name

    def scale_by(self) -> int:
        """The boost this community's posts get in `ranking_scaled`: bigger for a
        smaller community, so a large one does not drown the feed.

        D1378. The fast path returned 3 where the computation below returns 4 for
        the same community. The two agreed by construction until 0aa6993d7
        ("smarter large community calculation #495") added the `influence < 0.05`
        band returning 4 -- before it, the top band and this guard were both 3 --
        and left this line at the old maximum. Measured, with the top-15% average
        at 100 subscribers:

            subscribers  1 -> 3     2 -> 4     5 -> 3

        so a brand-new community, which has exactly one subscriber because its
        creator joined it, got LESS of the small-community boost than one with two.
        The guard is a fast path around a cached query, not a policy: it returns
        what the computation would, which is the largest boost.

        Invisible on a small instance -- at a top-15% average of 20 or below,
        `1/largest` is not under 0.05 and the old value was right by coincidence.
        """
        if self.subscriptions_count <= 1:
            return 4
        largest_community = _large_community_subscribers()
        if largest_community is None or largest_community == 0:
            return 0
        influence = self.subscriptions_count / int(largest_community)
        if influence < 0.05:
            return 4
        if influence < 0.25:
            return 3
        elif influence < 0.60:
            return 2
        elif influence < 1.0:
            return 1
        else:
            return 0

    def flair_for_ap(self, version=1):
        result = []

        if version == 1:
            for flair in self.flair:
                result.append({'type': 'lemmy:CommunityTag',
                            'id': f'{current_app.config["SERVER_URL"]}/c/{self.link()}/tag/{flair.id}',
                            'display_name': flair.flair,
                            'text_color': flair.text_color,
                            'background_color': flair.background_color,
                            'blur_images': flair.blur_images
                            })
        elif version == 2:
            for flair in self.flair:
                result.append({
                    "type": "CommunityPostTag",
                    "id": flair.get_ap_id(),
                    "preferredUsername": flair.flair,
                    "textColor": flair.text_color,
                    "backgroundColor": flair.background_color,
                    "blurImages": flair.blur_images,
                })

        return result

    def delete_dependencies(self):
        # One flush for every post in the community rather than one per file, which
        # is what `purge_cdn=False` was standing in for (D1350).
        cache_urls = []
        for rss_feed in self.rss_feeds:
            rss_feed.delete_dependencies()
            db.session.delete(rss_feed)
            db.session.commit()
        for post in db.session.query(Post).filter_by(community_id=self.id):
            with app_pkg.redis_client.lock(f"lock:post:{post.id}", timeout=30, blocking_timeout=30):
                post.delete_dependencies(cache_urls=cache_urls)
                db.session.delete(post)
                db.session.commit()
        if cache_urls:
            flush_cdn_cache(cache_urls)
        db.session.query(FeedItem).filter(FeedItem.community_id == self.id).delete()
        db.session.query(CommunityBan).filter(CommunityBan.community_id == self.id).delete()
        db.session.query(CommunityBlock).filter(CommunityBlock.community_id == self.id).delete()
        db.session.query(CommunityFlairBlock).filter(CommunityFlairBlock.community_id == self.id).delete()
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == self.id).delete()
        db.session.query(CommunityMember).filter(CommunityMember.community_id == self.id).delete()
        db.session.query(CommunityFavorite).filter(CommunityFavorite.community_id == self.id).delete()
        db.session.query(Report).filter(Report.suspect_community_id == self.id).delete()
        db.session.query(UserFlair).filter(UserFlair.community_id == self.id).delete()
        db.session.query(ModLog).filter(ModLog.community_id == self.id).update({ModLog.community_id: None})
        db.session.query(ActivityBatch).filter(ActivityBatch.community_id == self.id).delete()
        db.session.query(CommunityThemeAllowed).filter(CommunityThemeAllowed.community_id == self.id).delete()
        db.session.commit()


user_role = db.Table('user_role',
                     db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                     db.Column('role_id', db.Integer, db.ForeignKey('role.id')),
                     db.PrimaryKeyConstraint('user_id', 'role_id')
                     )

# table to hold users' read post ids
read_posts = db.Table('read_posts',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True, nullable=False),
                      db.Column('read_post_id', db.Integer, db.ForeignKey('post.id'), primary_key=True, nullable=False),
                      db.Column('interacted_at', db.DateTime, index=True, default=utcnow),
                      db.Index('ix_read_posts_user_post', 'user_id', 'read_post_id')
                      )


# table to hold users' hidden post ids
hidden_posts = db.Table('hidden_posts',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True, nullable=False),
                      db.Column('hidden_post_id', db.Integer, db.ForeignKey('post.id'), primary_key=True, nullable=False),
                      db.Column('interacted_at', db.DateTime, index=True, default=utcnow),
                      db.Index('ix_hidden_posts_user_post', 'user_id', 'hidden_post_id')
                      )


class Passkey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    passkey_id = db.Column(db.String(256))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    public_key = db.Column(db.LargeBinary)  # Store the raw binary public key
    created = db.Column(db.DateTime, default=utcnow)
    used = db.Column(db.DateTime, default=utcnow)
    device = db.Column(db.String(50))
    counter = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f"<Passkey {self.id} {self.device}>"


class User(UserMixin, db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(255), index=True)
    alt_user_name = db.Column(db.String(255), index=True)
    title = db.Column(db.String(256))
    email = db.Column(db.String(255), index=True)
    password_hash = db.Column(db.String(165))
    verified = db.Column(db.Boolean, default=False, server_default='false', nullable=False)  # NOT NULL: guards read `not user.verified` (D645)
    verification_token = db.Column(db.String(16), index=True)
    banned = db.Column(db.Boolean, default=False, index=True)
    banned_until = db.Column(db.DateTime)  # null == permanent ban
    ban_posts = db.Column(db.Boolean, default=False)
    ban_comments = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False)
    deleted_by = db.Column(db.Integer, index=True)
    about = db.Column(db.Text)  # markdown
    about_html = db.Column(db.Text)  # html
    keywords = db.Column(db.String(256))
    matrix_user_id = db.Column(db.String(256))
    hide_nsfw = db.Column(db.Integer, default=1)
    hide_nsfl = db.Column(db.Integer, default=1)
    hide_gen_ai = db.Column(db.Integer, default=2)      # 0 = show, 1 = hide, 2 = label, 3 = semi-transparent
    created = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow, index=True)
    avatar_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    cover_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    newsletter = db.Column(db.Boolean, default=True)
    email_unread = db.Column(db.Boolean, default=True)  # True if they want to receive 'unread notifications' emails
    email_unread_sent = db.Column(db.Boolean)  # True after a 'unread notifications' email has been sent. None for remote users
    receive_message_mode = db.Column(db.String(20), default='Closed')  # possible values: Open, TrustedOnly, Closed
    bounces = db.Column(db.SmallInteger, default=0)
    timezone = db.Column(db.String(30))
    reputation = db.Column(db.Float, default=0.0)
    attitude = db.Column(db.Float, default=None)  # (upvotes cast - downvotes cast) / (upvotes + downvotes). A number between 1 and -1 is the ratio between up and down votes they cast
    post_count = db.Column(db.Integer, default=0)
    post_reply_count = db.Column(db.Integer, default=0)
    stripe_customer_id = db.Column(db.String(50))
    stripe_subscription_id = db.Column(db.String(50))
    searchable = db.Column(db.Boolean, default=True)        # whether their profile is visible in profile list
    indexable = db.Column(db.Boolean, default=True)         # whether posts appear in search results
    bot = db.Column(db.Boolean, default=False, index=True)
    bot_override = db.Column(db.Boolean, default=False, index=True)
    suppress_crossposts = db.Column(db.Boolean, default=False, index=True)
    vote_privately = db.Column(db.Boolean, default=False)
    can_send_pm = db.Column(db.Boolean, default=True)
    finished_onboarding = db.Column(db.Boolean, default=False)
    ignore_bots = db.Column(db.Integer, default=0)
    unread_notifications = db.Column(db.Integer, default=0, server_default='0', nullable=False)  # NOT NULL: read as `+= 1` everywhere (D274)
    ip_address = db.Column(db.String(50))
    ip_address_country = db.Column(db.String(50))
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    reports = db.Column(db.Integer, default=0)  # how many times this user has been reported.
    default_sort = db.Column(db.String(25), default='hot')
    default_comment_sort = db.Column(db.String(10), default='hot')
    default_filter = db.Column(db.String(25), default='subscribed')
    theme = db.Column(db.String(20), default='')
    font = db.Column(db.String(25), default='')
    allow_community_themes = db.Column(db.Boolean, default=True, server_default="1")
    community_keyword_filter = db.Column(db.String(150))
    referrer = db.Column(db.String(256))
    markdown_editor = db.Column(db.Boolean, default=True)
    interface_language = db.Column(db.String(10))  # a locale that the translation system understands e.g. 'en' or 'en-us'. If empty, use browser default
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'))  # the default choice in the language dropdown when composing posts & comments. NOT UI language
    read_language_ids = db.Column(MutableList.as_mutable(ARRAY(db.Integer)))
    reply_collapse_threshold = db.Column(db.Integer, default=-10)
    reply_hide_threshold = db.Column(db.Integer, default=-20)
    feed_auto_follow = db.Column(db.Boolean, default=True)  # does the user want to auto-follow feed communities
    feed_auto_leave = db.Column(db.Boolean, default=False)  # does the user want to auto-leave feed communities (D663: off, as the form says)
    accept_private_messages = db.Column(db.Integer, default=3)  # None or 0 = do not accept, 1 = This instance, 2 = Trusted instances, 3 = All instances
    google_oauth_id = db.Column(db.String(64), unique=True, index=True)
    hide_low_quality = db.Column(db.Boolean, default=False)
    show_subscribed_communities = db.Column(db.Boolean, default=False)
    additional_css = db.Column(db.Text)
    mastodon_oauth_id = db.Column(db.String(64), unique=True, index=True)
    discord_oauth_id = db.Column(db.String(64), unique=True, index=True)
    password_updated_at = db.Column(db.DateTime, default=utcnow)
    code_style = db.Column(db.String(25), default='fruity')
    admin_note = db.Column(db.Text)
    page_length = db.Column(db.Integer)
    num_following = db.Column(db.Integer, default=0)    # number of users being followed, not number of communities
    num_followers = db.Column(db.Integer, default=0)    # number of users that follow this user
    rss_token = db.Column(db.String(20), index=True)

    avatar = db.relationship('File', lazy='joined', foreign_keys=[avatar_id], single_parent=True, cascade="all, delete-orphan")
    cover = db.relationship('File', lazy='joined', foreign_keys=[cover_id], single_parent=True, cascade="all, delete-orphan")
    instance = db.relationship('Instance', lazy='joined', foreign_keys=[instance_id])
    conversations = db.relationship('Conversation', lazy='dynamic', secondary=conversation_member,
                                    backref=db.backref('members', lazy='joined'))
    user_notes = db.relationship('UserNote', lazy='dynamic', foreign_keys="UserNote.target_id")

    ap_id = db.Column(db.String(255), index=True)  # e.g. username@server
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)  # e.g. https://server/u/username
    ap_public_url = db.Column(db.String(255))  # e.g. https://server/u/UserName
    ap_fetched_at = db.Column(db.DateTime)
    ap_followers_url = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_manually_approves_followers = db.Column(db.Boolean, default=False)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))

    search_vector = db.Column(TSVectorType('user_name', 'about', 'keywords', auto_index=False))
    activity = db.relationship('ActivityLog', backref='account', lazy='dynamic', cascade="all, delete-orphan")
    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    post_replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")
    extra_fields = db.relationship('UserExtraField', lazy='dynamic', cascade="all, delete-orphan")
    roles = db.relationship('Role', secondary=user_role, lazy='dynamic')
    passkeys = db.relationship('Passkey', lazy='dynamic', cascade="all, delete-orphan")
    modlog_target = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.target_user_id", back_populates='target_user', cascade="all, delete-orphan")
    modlog_actor = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.user_id", back_populates='author', cascade="all, delete-orphan")

    hide_read_posts = db.Column(db.Boolean, default=False)
    # db relationship tracked by the "read_posts" table
    # this is the User side, so its referencing the Post side
    # read_by is the corresponding Post object variable
    read_post = db.relationship('Post', secondary=read_posts, back_populates='read_by', lazy='dynamic')
    hidden_post = db.relationship('Post', secondary=hidden_posts, back_populates='hidden_by', lazy='dynamic')

    __table_args__ = (
        db.Index(
            'idx_user_user_name_lower',
            db.text('lower(user_name)')
        ),
    )

    def __repr__(self):
        return '<User {}_{}>'.format(self.user_name, self.id)

    def set_password(self, password, revoke_sessions=True):
        """Change the password, and by default stamp password_updated_at.

        `password_updated_at` is what authorise_api_user() compares a bearer
        token's `iat` against: a token issued before the stamp is refused. That
        is the ONLY mechanism by which changing a password invalidates existing
        API sessions, so the stamp belongs here rather than at each call site --
        it used to be written only by the settings form, which left the
        forgot-password reset (the path a compromised user actually takes), the
        admin reset and the CLI resets unable to sign an attacker out.

        revoke_sessions=False is for one caller only: check_password()'s
        legacy-bcrypt fallback just below, which re-saves the hash of a password
        the user has *just proved they know*. Nothing about the password changed
        there, only its encoding, so it is not a reason to sign that user's API
        clients out. Any genuine password change must leave the default alone.

        D862: edge whitespace is stripped here and in check_password, so a
        password means the same thing whichever form or API set or submits it.
        """
        self.password_hash = generate_password_hash(password.strip())
        if revoke_sessions:
            self.password_updated_at = utcnow()

    def check_password(self, password):
        """Total: any stored hash, however malformed, yields True or False.

        The bcrypt fallback deliberately runs OUTSIDE the first try, not inside
        its `except ValueError:` block. Handlers on the same `try` guard the try
        body, not one another, so a fallback that raised from inside the
        ValueError handler escaped check_password entirely -- past the sibling
        `except Exception: return False` that was written to make this method
        total -- and turned a login against a malformed password_hash into a 500
        rather than a failed login. Every hash shape that makes werkzeug raise
        ValueError can also make bcrypt raise: a truncated column, a partial
        migration from another system, a hand-edited row.
        """
        if isinstance(password, str):
            password = password.strip()  # D862, as set_password stores it
        try:
            return check_password_hash(self.password_hash, password)
        except ValueError:
            # Caused when invalid hash method used, check bcrypt as a fallback
            # (below, so that its own failures are caught too).
            pass
        except Exception:
            return False

        try:
            result = app_bcrypt.check_password_hash(self.password_hash, password)
        except Exception:
            # The hash is not usable by werkzeug OR by bcrypt: nothing can
            # authenticate against it, which is a failed login, not an error.
            return False

        # If pw validates, resave the hash using a more secure hashing algorithm.
        # revoke_sessions=False: this is a hash migration on a successful
        # login, not a password change -- see set_password's docstring.
        if result:
            self.set_password(password, revoke_sessions=False)
            db.session.commit()

        return result

    def get_id(self):
        if self.is_authenticated:
            return self.id
        else:
            return 0

    @classmethod
    def get_by_email(cls, email):
        return User.query.filter(User.email == email.strip()).first()

    def display_name(self):
        if self.deleted is False:
            if self.title:
                # Sanitize some special unicode formatting characters
                # ref: https://krvtz.net/posts/input-validation-of-free-form-unicode-text-in-python.html
                title = self.title.strip()
                clean_title = "".join(c for c in title if unicodedata.category(c) != "Cf")
                return clean_title
            else:
                return self.user_name.strip()
        else:
            return '[deleted]'

    def avatar_thumbnail(self) -> str:
        if self.avatar_id is not None:
            if self.avatar.thumbnail_path is not None:
                return served_path(self.avatar.thumbnail_path)
            else:
                return self.avatar_image()
        return ''

    def avatar_image(self) -> str:
        if self.avatar_id is not None:
            if self.avatar.file_path is not None:
                return served_path(self.avatar.file_path)
            if self.avatar.source_url is not None:
                return served_path(self.avatar.source_url)
        return ''

    @cache.memoize(timeout=500)
    def cover_image(self) -> str:
        if self.cover_id is not None:
            if self.cover.thumbnail_path is not None:
                return served_path(self.cover.thumbnail_path)
            if self.cover.source_url is not None:
                return served_path(self.cover.source_url)
        return ''

    def community_theme_allowed(self,community_id:int) ->bool:
        from app.community.util import get_community_theme_allowed  # cycle: app.community.util imports from this module
        return get_community_theme_allowed(community_id,self.id)

    def filesize(self):
        size = 0
        if self.avatar_id:
            size += self.avatar.filesize()
        if self.cover_id:
            size += self.cover.filesize()
        return size

    def community_flair(self, community_id: int):
        user_flair = UserFlair.query.filter(UserFlair.community_id == community_id,
                                            UserFlair.user_id == self.id).first()
        return user_flair.flair.strip() if user_flair else ''

    def num_content(self):
        content = 0
        content += db.session.execute(text('SELECT COUNT(*) as c FROM "post" WHERE user_id = :user_id'),
                                      {'user_id': self.id}).scalar()
        content += db.session.execute(text('SELECT COUNT(*) as c FROM "post_reply" WHERE user_id = :user_id'),
                                      {'user_id': self.id}).scalar()
        return content

    def is_local(self):
        return self.ap_id is None or self.ap_profile_id.startswith(current_app.config['SERVER_URL'])

    def waiting_for_approval(self):
        application = UserRegistration.query.filter_by(user_id=self.id, status=0).first()
        return application is not None

    @cache.memoize(timeout=30)
    def is_admin(self):
        if self.id == 1:
            return True
        for role in self.roles:
            if role.name == ROLE_ADMIN_NAME:
                return True
        return False

    @cache.memoize(timeout=30)
    def is_staff(self):
        for role in self.roles:
            if role.name == ROLE_STAFF_NAME:
                return True
        return False

    def is_admin_or_staff(self):
        return self.is_admin() or self.is_staff()

    def is_ban_exempt(self):
        # D583: user 1 is the account that set the instance up. No ban -- account, IP or cookie -- stops it logging
        # in, and nobody may ban it, so an instance can never lock out its founder. Every ban check asks this.
        return self.id == 1

    def is_instance_admin(self):
        if self.instance_id:
            instance_role = InstanceRole.query.filter(InstanceRole.instance_id == self.instance_id,
                                                      InstanceRole.user_id == self.id,
                                                      InstanceRole.role == 'admin').first()
            return instance_role is not None
        else:
            return False

    def is_rss_bot(self):
        return self.bot and self.user_name == 'feed_bot'

    def trustworthy(self):
        if self.is_admin():
            return True
        if self.created_recently() and self.reputation < 100:
            return False
        return True

    def link(self) -> str:
        if self.is_local():
            return self.user_name
        else:
            return self.ap_id

    def lemmy_link(self) -> str:
        if self.ap_id is None:
            return f"{self.user_name}@{current_app.config['SERVER_NAME']}"
        else:
            return self.ap_id.lower()

    def followers_url(self):
        if self.ap_followers_url:
            return self.ap_followers_url
        else:
            return self.public_url() + '/followers'

    def instance_domain(self):
        if self.ap_domain:
            return self.ap_domain
        if self.is_local():
            return current_app.config['SERVER_NAME']
        else:
            return self.instance.domain

    def email_domain(self):
        email_parts = self.email.split('@')
        return email_parts[1]

    def reset_password_fingerprint(self):
        """A short digest of the credential a reset token is issued against.

        D1130. The token is a JWT and therefore stateless, so nothing about
        using one changed anything: the same link reset the password again,
        and again, until it expired. Measured: `PROBE ar3 first reset worked:
        True | same token reused: True`. Anyone who came by the link
        afterwards -- browser history, a forwarded message, a shared device --
        could take the account from the person who had just secured it.

        Carrying a digest of the current `password_hash` makes the token
        single-use without any storage: the reset changes the hash, so the
        digest in an already-used token no longer matches. A digest rather
        than the hash itself, because a JWT is signed and NOT encrypted --
        whoever holds the token can read its payload.
        """
        return sha256((self.password_hash or '').encode()).hexdigest()[:16]

    def get_reset_password_token(self, expires_in=600):
        return jwt.encode(
            {'reset_password': self.id, 'pw': self.reset_password_fingerprint(),
             'exp': time() + expires_in},
            current_app.config['SECRET_KEY'],
            algorithm='HS256')

    def another_account_using_email(self, email):
        another_account = User.query.filter(User.email == email, User.id != self.id).first()
        return another_account is not None

    # D1365. `expires_soon`, `is_expired` and `expired_ages_ago` were here, each
    # reading `self.expires`. `User` has no such column -- the only `expires` in this
    # file is in the commented-out `IngressQueue` model -- so all three raised
    # `AttributeError` on any call, and all three date from the initial commit.
    # Nothing called them, so there was no behaviour to keep and nothing to point
    # them at: three methods that could not be called, referencing a column that does
    # not exist.

    def recalculate_attitude(self):
        # Use direct SQL queries to avoid potential ORM-related deadlocks
        # Count post upvotes and downvotes
        post_votes_result = db.session.execute(text("""
            SELECT
                COUNT(CASE WHEN effect > 0 THEN 1 END) AS upvotes,
                COUNT(CASE WHEN effect < 0 THEN 1 END) AS downvotes
            FROM (
                SELECT effect
                FROM post_vote
                WHERE user_id = :user_id
                ORDER BY id DESC
                LIMIT 50
            ) AS recent_votes
        """), {"user_id": self.id}).fetchone()

        upvotes = post_votes_result[0] or 0
        downvotes = post_votes_result[1] or 0

        # Count comment upvotes and downvotes
        comment_votes_result = db.session.execute(text("""
            SELECT
                COUNT(CASE WHEN effect > 0 THEN 1 END) AS upvotes,
                COUNT(CASE WHEN effect < 0 THEN 1 END) AS downvotes
            FROM (
                SELECT effect
                FROM post_reply_vote
                WHERE user_id = :user_id
                ORDER BY id DESC
                LIMIT 50
            ) AS recent_votes
        """), {"user_id": self.id}).fetchone()

        comment_upvotes = comment_votes_result[0] or 0
        comment_downvotes = comment_votes_result[1] or 0

        total_upvotes = upvotes + comment_upvotes
        total_downvotes = downvotes + comment_downvotes

        # Calculate the new attitude value
        if total_upvotes + total_downvotes > 9:  # Only calculate attitude if they've done 10 or more votes
            new_attitude = (total_upvotes - total_downvotes) / (total_upvotes + total_downvotes)
        else:
            new_attitude = None

        # Update attitude
        db.session.execute(text("""
            UPDATE "user"
            SET attitude = :attitude
            WHERE id = :user_id
        """), {"attitude": new_attitude, "user_id": self.id})
        # Note: Caller is responsible for committing

    def get_num_upvotes(self):
        post_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_vote" WHERE user_id = :user_id AND effect > 0'),
            {'user_id': self.id}).scalar()
        post_reply_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_reply_vote" WHERE user_id = :user_id AND effect > 0'),
            {'user_id': self.id}).scalar()
        return post_votes + post_reply_votes

    def get_num_downvotes(self):
        post_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_vote" WHERE user_id = :user_id AND effect < 0'),
            {'user_id': self.id}).scalar()
        post_reply_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_reply_vote" WHERE user_id = :user_id AND effect < 0'),
            {'user_id': self.id}).scalar()
        return post_votes + post_reply_votes

    def recalculate_post_stats(self, posts=True, replies=True):
        if posts:
            self.post_count = db.session.execute(
                text('SELECT COUNT(*) as c FROM "post" WHERE user_id = :user_id AND deleted = false'),
                {'user_id': self.id}).scalar()
        if replies:
            self.post_reply_count = db.session.execute(
                text('SELECT COUNT(*) as c FROM "post_reply" WHERE user_id = :user_id AND deleted = false'),
                {'user_id': self.id}).scalar()

    def subscribed(self, community_id: int) -> int:
        if community_id is None:
            return False
        subscription: CommunityMember = CommunityMember.query.filter_by(user_id=self.id,
                                                                        community_id=community_id).first()
        if subscription:
            if subscription.is_banned:
                return SUBSCRIPTION_BANNED
            elif subscription.is_owner:
                return SUBSCRIPTION_OWNER
            elif subscription.is_moderator:
                return SUBSCRIPTION_MODERATOR
            else:
                return SUBSCRIPTION_MEMBER
        else:
            join_request = CommunityJoinRequest.query.filter_by(user_id=self.id, community_id=community_id).first()
            if join_request:
                return SUBSCRIPTION_PENDING
            else:
                return SUBSCRIPTION_NONMEMBER

    def communities(self) -> List[Community]:
        return Community.query.filter(Community.banned == False). \
            join(CommunityMember).filter(CommunityMember.is_banned == False, CommunityMember.user_id == self.id).all()

    def profile_id(self):
        result = self.ap_profile_id if self.ap_profile_id else f"{current_app.config['SERVER_URL']}/u/{self.user_name.lower()}"
        return result

    def public_url(self):
        return self.ap_public_url if self.ap_public_url else f"{current_app.config['SERVER_URL']}/u/{self.user_name}"

    def created_recently(self):
        return self.created and self.created > utcnow() - timedelta(days=7)

    def created_very_recently(self):
        return self.created and self.created > utcnow() - timedelta(days=1)

    def has_blocked_instance(self, instance_id: int):
        if instance_id is None:
            return False
        instance_block = db.session.query(InstanceBlock).filter_by(user_id=self.id, instance_id=instance_id).first()
        return instance_block is not None

    def has_blocked_instances(self):
        instance_block = db.session.query(InstanceBlock).filter_by(user_id=self.id).first()
        return instance_block is not None

    def has_blocked_user(self, user_id: int):
        existing_block = db.session.query(UserBlock).filter_by(blocker_id=self.id, blocked_id=user_id).first()
        return existing_block is not None

    @staticmethod
    def verify_reset_password_token(token):
        try:
            payload = jwt.decode(token, current_app.config['SECRET_KEY'],
                                 algorithms=['HS256'])
            id = payload['reset_password']
        except:
            return
        user = db.session.get(User, id)
        if user is None:
            return
        # D1130. A token issued against one password is spent once that
        # password changes -- by this reset or by any other route to it.
        # Tokens minted before this field existed carry no 'pw' and are
        # refused rather than honoured, which costs their holders one more
        # click on "forgot password" and closes the window for everyone else.
        if payload.get('pw') != user.reset_password_fingerprint():
            return
        return user

    def delete_dependencies(self, purge_cdn=True):
        """`purge_cdn` reaches the `user_file` uploads, which is what D1348 was
        about: `purge_content(flush=...)` could not control them, because by the
        time it ran they were already gone."""
        # Get cover and avatar file IDs before clearing references
        cover_file_id = self.cover_id
        avatar_file_id = self.avatar_id
        
        # Clear references first
        self.cover_id = None
        self.avatar_id = None
        db.session.flush()
        
        if self.waiting_for_approval():
            db.session.query(UserRegistration).filter(UserRegistration.user_id == self.id).delete()
        
        # Handle user_file associations -- the images this user uploaded.
        #
        # D1348. These were deleted with `purge_cdn=False` and their File rows
        # left behind, and `purge_content`'s own `user_file` block -- the one that
        # would have purged them with `purge_cdn=flush` and deleted the rows --
        # could never run, because this method had already removed every
        # association it looked for. So a banned user's uploads went from disk but
        # stayed in the CDN, which is what the public reads.
        #
        # The other-user check is the same policy as round 152's S3 delete:
        # `DELETE FROM user_file WHERE file_id = :file_id` removes EVERY user's
        # association with that file, not only this user's, so a file somebody
        # else also uploaded must lose this association and nothing more.
        user_files = db.session.query(File).join(user_file).filter(user_file.c.user_id == self.id).all()
        for file in user_files:
            shared = db.session.query(user_file).filter(
                user_file.c.file_id == file.id, user_file.c.user_id != self.id).count()
            db.session.execute(
                text('DELETE FROM "user_file" WHERE file_id = :file_id AND user_id = :user_id'),
                {'file_id': file.id, 'user_id': self.id})
            if shared:
                continue
            # D1426. The cover/avatar loop below checks `User.cover_id` / `User.avatar_id`
            # before deleting a File; this loop checked only OTHER USERS' `user_file`
            # associations. So a file this account uploaded that another account references as
            # its avatar or cover was deleted here, and the DELETE hit
            # `user_avatar_id_fkey` -- a ForeignKeyViolation that aborts the whole
            # `delete_dependencies` call, leaving the account half-deleted. Same check, same
            # policy as the loop below.
            if db.session.query(User).filter(
                    or_(User.cover_id == file.id, User.avatar_id == file.id)).count() > 0:
                continue
            file.delete_from_disk(purge_cdn=purge_cdn)
            db.session.delete(file)
        
        # Now handle cover and avatar files - delete them one at a time
        # after checking they're no longer referenced
        for file_id in [cover_file_id, avatar_file_id]:
            if file_id is None:
                continue
            file = db.session.get(File, file_id)
            if file is None:
                continue
            
            # Check if any user still references this file
            if db.session.query(User).filter(
                or_(User.cover_id == file_id, User.avatar_id == file_id)
            ).count() > 0:
                continue
            # Check user_file table
            if db.session.query(user_file).filter(user_file.c.file_id == file_id).count() > 0:
                continue
            
            # Safe to delete
            file.delete_from_disk()
            db.session.delete(file)
        db.session.execute(text('DELETE FROM "post_vote" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.execute(text('DELETE FROM "post_reply_vote" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.execute(text('DELETE FROM "user_role" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.execute(text('DELETE FROM "hidden_posts" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.execute(text('DELETE FROM "read_posts" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.query(NotificationSubscription).filter(NotificationSubscription.user_id == self.id).delete()
        db.session.query(ArchivedPostReply).filter(ArchivedPostReply.user_id == self.id).delete()
        db.session.query(Filter).filter(Filter.user_id == self.id).delete()
        db.session.query(UserFlair).filter(UserFlair.user_id == self.id).delete()
        db.session.query(UserFollower).filter(or_(UserFollower.local_user_id == self.id, UserFollower.remote_user_id == self.id)).delete()
        db.session.query(UserFollowRequest).filter(UserFollowRequest.user_id == self.id).delete()
        db.session.query(UserFollowRequest).filter(UserFollowRequest.follow_id == self.id).delete()
        db.session.query(CommunityFavorite).filter(CommunityFavorite.user_id == self.id).delete()
        db.session.query(CommunityMember).filter(CommunityMember.user_id == self.id).delete()
        db.session.query(CommunityBlock).filter(CommunityBlock.user_id == self.id).delete()
        db.session.query(CommunityFlairBlock).filter(CommunityFlairBlock.user_id == self.id).delete()
        db.session.query(CommunityBan).filter(CommunityBan.user_id == self.id).delete()
        db.session.query(BotChallenge).filter(BotChallenge.user_id == self.id).delete()
        db.session.query(BotChallenge).filter(BotChallenge.sent_by == self.id).delete()
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.user_id == self.id).delete()
        db.session.query(ChatMessage).filter(or_(ChatMessage.sender_id == self.id, ChatMessage.recipient_id == self.id)).delete()
        db.session.query(UserBlock).filter(or_(UserBlock.blocker_id == self.id, UserBlock.blocked_id == self.id)).delete()
        db.session.query(Notification).filter(Notification.user_id == self.id).delete()
        db.session.query(PollChoiceVote).filter(PollChoiceVote.user_id == self.id).delete()
        db.session.query(PostBookmark).filter(PostBookmark.user_id == self.id).delete()
        db.session.query(PostReplyBookmark).filter(PostReplyBookmark.user_id == self.id).delete()
        db.session.query(Reminder).filter(Reminder.user_id == self.id).delete()
        db.session.query(CommunityWikiPageRevision).filter(CommunityWikiPageRevision.user_id == self.id).update({CommunityWikiPageRevision.user_id: None})
        db.session.query(UserNote).filter(or_(UserNote.user_id == self.id, UserNote.target_id == self.id)).delete()
        db.session.query(CommunityThemeAllowed).filter(CommunityThemeAllowed.user_id == self.id).delete()

    def purge_content(self, soft=True, flush=True):
        files = File.query.join(Post).filter(Post.user_id == self.id).all()
        for file in files:
            file.delete_from_disk(purge_cdn=flush)
        db.session.commit()
        self.delete_dependencies(purge_cdn=flush)
        db.session.commit()
        cache_urls = [] if flush else None
        posts = Post.query.filter_by(user_id=self.id).all()
        for post in posts:
            post.delete_dependencies(cache_urls=cache_urls)
            if soft:
                post.deleted = True
            else:
                db.session.delete(post)
            db.session.commit()

        post_replies = PostReply.query.filter_by(user_id=self.id).all()
        for reply in post_replies:
            reply.delete_dependencies(cache_urls=cache_urls)
            if soft:
                reply.deleted = True
            else:
                db.session.delete(reply)
            db.session.commit()

        # The `user_file` block that used to be here is gone: `delete_dependencies`
        # above has already removed every association this query looked for, so it
        # could never run. Its intent -- purge the CDN and delete the row -- moved
        # into that method instead, where it does happen (D1348).
        db.session.commit()
        if cache_urls:
            flush_cdn_cache(cache_urls)

    def mention_tag(self):
        if self.ap_domain is None:
            return '@' + self.user_name + '@' + current_app.config['SERVER_NAME']
        else:
            return '@' + self.user_name + '@' + self.ap_domain

    # True if user_id wants to be notified about posts by self
    def notify_new_posts(self, user_id):
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_USER).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when self makes a post
    def notification_subscribers(self):
        return list(db.session.execute(
            text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :user_id AND type = :type '),
            {'user_id': self.id, 'type': NOTIF_USER}).scalars())

    def encode_jwt_token(self):
        if not current_app.config['SECRET_KEY']:
            raise Exception('SECRET_KEY environment variable is not set')
        expiry_seconds = current_app.config['JWT_EXPIRY_DAYS'] * 86400
        payload = {'sub': str(self.id),
                   'iss': current_app.config['SERVER_NAME'],
                   'iat': int(time()),
                   'exp': int(time()) + expiry_seconds,
                   'jti': str(uuid.uuid4())  # Unique token ID, for revocation
                   }
        return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')

    # mark a post as 'read' for this user
    def mark_post_as_read(self, post):
        # check if its already marked as read, if not, mark it as read
        if not self.has_read_post(post):
            self.read_post.append(post)

    # check if post has been read by this user
    # returns true if the post has been read, false if not
    def has_read_post(self, post):
        return self.read_post.filter(read_posts.c.read_post_id == post.id).count() > 0


    # mark a post as 'hidden' for this user
    def mark_post_as_hidden(self, post):
        # check if its already marked as hidden, if not, mark it as hidden
        if not self.has_hidden_post(post):
            self.hidden_post.append(post)

    # check if post has been hidden by this user
    # returns true if the post has been hidden, false if not
    def has_hidden_post(self, post):
        return self.hidden_post.filter(hidden_posts.c.hidden_post_id == post.id).count() > 0

    def get_note(self, by_user):
        user_note = self.user_notes.filter(UserNote.target_id == self.id, UserNote.user_id == by_user.id).first()
        if user_note:
            return user_note.body
        else:
            return ''

    def can_send_pm_to(self, recipient):
        if (
            self.created_very_recently()
            or self.reputation <= -10
            or self.banned
            or not self.verified
            or not self.can_send_pm
        ) and not (self.is_admin_or_staff() or recipient.is_admin_or_staff()):
            return False

        return True

    # instances that have users which follow this user. (excluding the current instance). Optionally limit to instances of the specificed software type
    def following_instances(self, include_dormant=False, software: List[str] | None = None) -> List[Instance]:
        instances = db.session.query(Instance).join(User, User.instance_id == Instance.id).\
            join(UserFollower, UserFollower.remote_user_id == User.id).filter(UserFollower.local_user_id == self.id,
                                                                              UserFollower.is_inward == True)
        if not include_dormant:
            instances = instances.filter(Instance.dormant == False)
        instances = instances.filter(Instance.id != 1, Instance.gone_forever == False)
        if software:
            if len(software) == 1:
                instances = instances.filter(Instance.software == software[0])
            else:
                instances = instances.filter(Instance.software.in_(software))
        return instances.distinct().all()

    def is_following(self, other_user) -> str:
        user_follow = db.session.query(UserFollower).filter(UserFollower.local_user_id == self.id,
                                                            UserFollower.remote_user_id == other_user.id,
                                                            UserFollower.is_inward == False).first()
        if user_follow:
            if user_follow.is_accepted is True:
                return 'following'
            # D1431. An `else: return 'no'` stood below the next arm and was
            # unreachable: `is_accepted` is a nullable Boolean, so True, None and
            # False exhaust its values and the arms here cover all three. The
            # value it would have returned is the one the fall-through already
            # gives.
            elif user_follow.is_accepted is None:
                return 'pending'
            # R265: a pending follow is stored as None, so False is the other side's refusal
            elif user_follow.is_accepted is False:
                return 'refused'
        return 'no'


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    activity_type = db.Column(db.String(64))
    activity = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, index=True, default=utcnow)


class Post(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    licence_id = db.Column(db.Integer, db.ForeignKey('licence.id'), index=True)
    status = db.Column(db.Integer, index=True, default=1)  # see POST_STATUS_* in constants.py
    slug = db.Column(db.String(255))
    title = db.Column(db.String(255))
    url = db.Column(db.String(2048))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    type = db.Column(db.Integer, default=constants.POST_TYPE_ARTICLE)
    microblog = db.Column(db.Boolean, default=False)
    comments_enabled = db.Column(db.Boolean, default=True)
    deleted = db.Column(db.Boolean, default=False, index=True)
    deleted_by = db.Column(db.Integer, index=True)
    mea_culpa = db.Column(db.Boolean, default=False)
    has_embed = db.Column(db.Boolean, default=False)
    reply_count = db.Column(db.Integer, default=0, index=True)
    reply_count_cross_posted = db.Column(db.Integer, default=0)
    # The seven columns the API's post sorts page by are NOT NULL, and that is
    # load-bearing rather than tidiness: sqlakeyset pages with
    # `WHERE (sort columns) < (the last row's values)`, and a NULL anywhere in
    # that comparison makes the predicate NULL instead of true, so the row comes
    # back on NO page of that sort while still being counted in the total. It
    # warns about exactly this, and migration c4f1a9d7e2b8 backfilled the rows
    # written before these defaults existed.
    score = db.Column(db.Integer, default=0, server_default='0', index=True, nullable=False)  # used for 'top' ranking
    nsfw = db.Column(db.Boolean, default=False, index=True)
    nsfl = db.Column(db.Boolean, default=False, index=True)
    content_warning = db.Column(db.Text)  # a peer's `summary`, shown collapsed above the body
    sticky = db.Column(db.Boolean, default=False, server_default='false', index=True, nullable=False)
    instance_sticky = db.Column(db.Boolean, default=False, server_default='false', index=True, nullable=False)
    ai_generated = db.Column(db.Boolean, default=False, index=True)
    notify_author = db.Column(db.Boolean, default=True)
    indexable = db.Column(db.Boolean, default=True, index=True)
    from_bot = db.Column(db.Boolean, default=False, index=True)
    private = db.Column(db.Boolean, default=False, index=True)
    visibility = db.Column(db.String(10), default='public', server_default='public', nullable=False, index=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)  # this is when the content arrived here
    posted_at = db.Column(db.DateTime, index=True, default=utcnow, server_default=db.func.now(), nullable=False)  # this is when the original server created it
    # `default=utcnow` is new with the NOT NULL: this column had no default of
    # any kind, so a post whose last_active was never written was omitted from
    # every page of the Active sort. A new post's last activity is its own
    # arrival, which is what Community.last_active and Feed.last_active use.
    last_active = db.Column(db.DateTime, index=True, default=utcnow, server_default=db.func.now(), nullable=False)
    ip = db.Column(db.String(50))
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Float, default=0.0, server_default='0', index=True, nullable=False)  # used for 'hot' ranking
    ranking_scaled = db.Column(db.Float, default=0.0, server_default='0', index=True, nullable=False)  # used for 'scaled' ranking
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)  # how many times this post has been reported. Set to -1 to ignore reports
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    cross_posts = db.Column(MutableList.as_mutable(ARRAY(db.Integer)))
    scheduled_for = db.Column(db.DateTime, index=True)  # The first (or only) occurrence of this post
    repeat = db.Column(db.String(20), default='')  # 'daily', 'weekly', 'monthly'. Empty string = no repeat, just post once.
    stop_repeating = db.Column(db.DateTime, index=True)  # No more repeats after this datetime
    emoji_reactions = db.Column(db.JSON)            # a cache of the emoji reactions a post has received, to avoid joins
    post_boosts = db.Column(db.JSON)                # a cache of the boosts(retweets) a microblog post has received, to avoid joins
    tags = db.relationship('Tag', lazy='joined', secondary=post_tag, backref=db.backref('posts', lazy='dynamic'))
    timezone = db.Column(db.String(30))
    archived = db.Column(db.String(100))
    flair = db.relationship('CommunityFlair', lazy='joined', secondary=post_flair,
                            backref=db.backref('posts', lazy='dynamic'))

    ap_id = db.Column(db.String(255), index=True, unique=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))
    ap_updated = db.Column(db.DateTime)  # When the remote instance edited the Post. Useful when local instance has been offline and a flurry of potentially out of order updates are coming in.

    search_vector = db.Column(TSVectorType('title', 'body', weights={"title": "A", "body": "B"}, auto_index=False))

    image = db.relationship(File, lazy='joined', foreign_keys=[image_id], cascade='all, delete')
    domain = db.relationship('Domain', lazy='joined', foreign_keys=[domain_id])
    author = db.relationship('User', lazy='joined', overlaps='posts', foreign_keys=[user_id])
    community = db.relationship('Community', lazy='joined', overlaps='posts', foreign_keys=[community_id])
    replies = db.relationship('PostReply', lazy='dynamic', backref='post', cascade='all, delete-orphan')
    language = db.relationship('Language', foreign_keys=[language_id], lazy='joined')
    licence = db.relationship('Licence', foreign_keys=[licence_id])
    modlog = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.post_id", back_populates='post')
    event = db.relationship('Event', uselist=False, backref='post', lazy='select', cascade='all, delete-orphan')
    boosts = db.relationship('PostBoost', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    gallery = db.relationship('File', secondary=post_file, lazy='dynamic', order_by=post_file.c.weight)
    votes = db.relationship('PostVote', lazy='dynamic', backref='post', cascade='all, delete-orphan', passive_deletes=True)
    bookmarks = db.relationship('PostBookmark', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    poll = db.relationship('Poll', uselist=False, backref='post', lazy='select', cascade='all, delete-orphan')

    # db relationship tracked by the "read_posts" table
    # this is the Post side, so its referencing the User side
    # read_post is the corresponding User object variable
    read_by = db.relationship('User', secondary=read_posts, back_populates='read_post', lazy='dynamic')
    hidden_by = db.relationship('User', secondary=hidden_posts, back_populates='hidden_post', lazy='dynamic')

    __table_args__ = (
        db.Index(
            'ix_post_user_id_not_deleted',
            'user_id',
            postgresql_where=db.text('deleted = false')
        ),
        db.Index(
            'ix_post_community_id_not_deleted',
            'community_id',
            postgresql_where=db.text('deleted = false')
        ),
        db.Index(
            'idx_post_fts',
            'search_vector',
            postgresql_using='gin'
        ),
        db.Index(
            'ix_post_community_posted_bot',
            'community_id', 'posted_at',
            postgresql_where=db.text('from_bot = false')
        ),
        db.Index(
            'ix_post_community_created_bot',
            'community_id', 'created_at',
            postgresql_where=db.text('from_bot = false')
        ),
        db.Index(
            'idx_post_reports_gt_0',
            'id',
            postgresql_where=db.text('reports > 0')
        ),
        Index(
            'ix_post_feed_new',
            desc('instance_sticky'), desc('posted_at'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
        Index(
            'ix_post_feed_hot',
            desc('instance_sticky'), desc('ranking'), desc('posted_at'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
        Index(
            'ix_post_feed_scaled',
            desc('instance_sticky'), desc('ranking_scaled'), desc('ranking'), desc('posted_at'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
        Index(
            'ix_post_feed_top',
            desc('posted_at'), desc('score'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
        Index(
            'ix_post_feed_active',
            desc('last_active'),
            postgresql_where=db.text('deleted = false AND status > 0 AND last_active IS NOT NULL')
        ),
        Index(
            'ix_post_feed_community',
            'community_id', desc('sticky'), desc('posted_at'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
    )

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith(current_app.config['SERVER_URL'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return db.session.query(cls).filter_by(ap_id=ap_id).first()

    @classmethod
    def get_by_slug(cls, slug):
        return db.session.query(cls).filter_by(slug=slug).first()

    @classmethod
    def new(cls, user: User, community: Community, request_json: dict, announce_id=None):
        # cycle: app.activitypub.util imports from this module
        from app.activitypub.util import find_language_or_create, find_language, \
            find_hashtag_or_create, \
            find_licence_or_create, make_image_sizes, notify_about_post, find_flair_or_create, host_of, \
            activitypub_visibility, set_post_gallery, content_warning_from
        # cycle: app.utils imports from this module
        from app.utils import allowlist_html, markdown_to_html, html_to_text, microblog_content_to_title, \
            microblog_content_to_link, blocked_phrases, get_setting, \
            is_image_url, is_video_url, domain_from_url, opengraph_parse, shorten_string, fixup_url, \
            is_video_hosting_site, communities_banned_from, recently_upvoted_posts, blocked_users, \
            url_is_storable

        microblog = False
        private = False
        if request_json['object'].get('name') is None:  # Microblog posts; a null name is no name (D258)
            private = True
            if 'content' in request_json['object'] and request_json['object']['content'] is not None:
                title = ""
                microblog = True
            else:
                return None
            if 'to' in request_json and len(request_json['to']) >= 1:
                if 'https://www.w3.org/ns/activitystreams#Public' in request_json['to']:
                    private = False
            if 'cc' in request_json and len(request_json['cc']) >= 1:
                if 'https://www.w3.org/ns/activitystreams#Public' in request_json['cc']:
                    private = False
        else:
            title = request_json['object']['name'].strip()
        visibility = activitypub_visibility(request_json['object'])
        nsfl_in_title = '[NSFL]' in title.upper() or '(NSFL)' in title.upper() or '[COMBAT]' in title.upper()
        post = Post(user_id=user.id, community_id=community.id,
                    title=html.unescape(title),
                    comments_enabled=request_json['object']['commentsEnabled'] if 'commentsEnabled' in request_json['object'] else True,
                    sticky=request_json['object']['stickied'] if 'stickied' in request_json['object'] else False,
                    nsfw=request_json['object']['sensitive'] if 'sensitive' in request_json['object'] else False,
                    nsfl=request_json['object']['nsfl'] if 'nsfl' in request_json['object'] else nsfl_in_title,
                    content_warning=content_warning_from(request_json['object']),
                    ai_generated=request_json['object']['genAI'] if 'genAI' in request_json['object'] else False,
                    private=private,
                    visibility=visibility,
                    ap_id=request_json['object']['id'],
                    ap_create_id=request_json['id'],
                    ap_announce_id=announce_id,
                    up_votes=1,
                    from_bot=user.bot or user.bot_override,
                    score=1.0,
                    instance_id=user.instance_id,
                    indexable=user.indexable,
                    microblog=microblog,
                    posted_at=utcnow()
                    )
        if 'type' in request_json and request_json['type'] == 'Update':
            post.edited_at = utcnow()
        if community.nsfw:
            post.nsfw = True  # old Lemmy instances ( < 0.19.8 ) allow nsfw content in nsfw communities to be flagged as sfw which makes no sense
        if community.nsfl:
            post.nsfl = True
        if community.ai_generated:
            post.ai_generated = True
        if community.private:
            post.indexable = False
        if 'content' in request_json['object'] and request_json['object']['content'] is not None:
            # prefer Markdown in 'source' if provided (D1346)
            source_markdown = markdown_source(request_json['object'])
            if source_markdown is not None:
                post.body = source_markdown
                post.body_html = markdown_to_html(post.body)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
                post.body_html = allowlist_html(request_json['object']['content'])
                post.body = html_to_text(post.body_html)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/markdown':
                post.body = request_json['object']['content']
                post.body_html = markdown_to_html(post.body)
            else:
                if not (request_json['object']['content'].startswith('<p>') or request_json['object'][
                    'content'].startswith('<blockquote>')):
                    request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
                post.body_html = allowlist_html(request_json['object']['content'])
                post.body = html_to_text(post.body_html)
            if microblog:
                autogenerated_title, link = microblog_content_to_title(post.body_html)
                title = autogenerated_title.strip()
                if '[NSFL]' in title.upper() or '(NSFL)' in title.upper() or '[COMBAT]' in title.upper():
                    post.nsfl = True
                if '[NSFW]' in title.upper() or '(NSFW)' in title.upper():
                    post.nsfw = True
                post.title = shorten_string(post.content_warning, 255) if post.content_warning else title  # a warning hides the body, so the body must not become the title
                if link != '':
                    post.url = link
                else:
                    if link_from_body := microblog_content_to_link(post.body_html, exclude=furl(request_json['object']['id']).host):
                        post.url = link_from_body
        # Discard post if it contains certain phrases. Good for stopping spam floods.
        blocked_phrases_list = blocked_phrases()
        for blocked_phrase in blocked_phrases_list:
            if blocked_phrase in post.title:
                return None
        if post.body:
            for blocked_phrase in blocked_phrases_list:
                if blocked_phrase in post.body:
                    return None

        file_path = None
        alt_text = None
        # D1397 on the last operand. The loop below already refuses an element
        # that is not a dict; this pre-check, which decides whether the loop runs
        # at all, was a membership test over whatever element 0 happens to be --
        # `TypeError: argument of type 'int' is not iterable` for a number, and
        # true for any string containing 'type'. It now scans every element, so a
        # junk first entry cannot hide the attachments behind it (R202).
        if ('attachment' in request_json['object'] and
                isinstance(request_json['object']['attachment'], list) and
                len(request_json['object']['attachment']) > 0 and
                any('type' in _as_dict(attachment) for attachment in request_json['object']['attachment'])):
            for attachment in request_json['object']['attachment']:
                alt_text = None
                # Every entry is read here, whatever its shape, so one that is not
                # a typed object is skipped -- it was a KeyError, and the post
                # never arrived.
                if not isinstance(attachment, dict) or 'type' not in attachment:
                    continue
                if is_more_info_link(attachment):  # R223: an event's 'More info' link is not the post's url
                    continue
                if attachment['type'] == 'Link':
                    if 'href' in attachment:
                        post.url = attachment['href']  # Lemmy < 0.19.4
                    elif 'url' in attachment:
                        post.url = attachment['url']  # NodeBB
                    if post.url:
                        break
                elif attachment['type'] == 'Document':
                    if 'url' not in attachment:     # as the Link branch above
                        continue                    # already tests for
                    post.url = attachment['url']  # Mastodon
                    if 'name' in attachment:
                        alt_text = attachment['name']
                    if post.url:
                        break
                elif attachment['type'] == 'Audio':  # WordPress podcast
                    if 'url' not in attachment:
                        continue
                    post.url = attachment['url']
                    if 'name' in attachment:
                        post.title = attachment['name']
                    if post.url:
                        break
            # Lastly, check for image posts. Mbin sends link posts with both image and link and we want to ignore the image in that case.
            if not post.url:
                for attachment in request_json['object']['attachment']:
                    if not isinstance(attachment, dict) or 'type' not in attachment:
                        continue
                    if attachment['type'] == 'Image' and 'url' in attachment:
                        post.url = attachment['url']  # PixelFed, PieFed, Lemmy >= 0.19.4
                        alt_text = attachment.get("name")
                        file_path = attachment.get("file_path")

        if 'attachment' in request_json['object'] and isinstance(request_json['object']['attachment'],
                                                                 dict):  # a.gup.pe (Mastodon)
            alt_text = None
            if 'url' in request_json['object']['attachment']:
                post.url = request_json['object']['attachment']['url']

        # Every write above this line takes a url straight from a REMOTE peer.
        # One site for all of them, the Create twin of the guard in
        # update_post_from_activity (app/activitypub/util.py): refuse to store a
        # url urlparse cannot read OR one naming a scheme an href may not carry
        # (D1404 -- six attachment shapes all stored javascript:), rather than
        # rejecting the peer's whole post,
        # which would hand peers a way to make us drop content. None, not '',
        # because None is what Post.url holds for every post with no url and
        # what post_to_page tests for before federating an attachment out again.
        if post.url and not url_is_storable(post.url):
            post.url = None

        if post.url:
            thumbnail_url, embed_url = fixup_url(post.url)
            post.url = embed_url
            if is_image_url(post.url):
                post.type = constants.POST_TYPE_IMAGE

                # PDQ hash of image
                image_hash = None
                if current_app.config['IMAGE_HASHING_ENDPOINT']:
                    from app.utils import retrieve_image_hash, hash_matches_blocked_image  # cycle: app.utils imports from this module
                    image_hash = retrieve_image_hash(post.url)
                    if image_hash and hash_matches_blocked_image(image_hash):
                        return None

                image = File(source_url=post.url, hash=image_hash)
                if alt_text:
                    image.alt_text = alt_text
                if file_path:
                    image.file_path = file_path
                db.session.add(image)
                post.image = image
            elif is_video_url(post.url) or is_video_hosting_site(post.url):
                post.type = constants.POST_TYPE_VIDEO
            elif host_of(post.url) in {'pixelfed.social', 'pixelfed.uno'}:  # as edit_post's two (D478)
                post.type = constants.POST_TYPE_IMAGE
                opengraph = opengraph_parse(thumbnail_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    # D1405. `og:image` is a string from a page this instance
                    # fetched, and it becomes `File.source_url`, which twelve
                    # templates render as a bare `href` through `view_url()`. The
                    # old guard, `not filename.startswith('/')`, skipped a relative
                    # path and admitted every scheme; `_as_url` covers the relative
                    # path too, since it has no http scheme, and applies the
                    # column's 1024 width.
                    filename = _as_url(opengraph.get('og:image') or opengraph.get('og:image:url'), 1024)
                    if filename:
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)
            elif post.url.startswith('https://loops.video'):
                post.type = constants.POST_TYPE_VIDEO
                opengraph = opengraph_parse(thumbnail_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    # D1405. `og:image` is a string from a page this instance
                    # fetched, and it becomes `File.source_url`, which twelve
                    # templates render as a bare `href` through `view_url()`. The
                    # old guard, `not filename.startswith('/')`, skipped a relative
                    # path and admitted every scheme; `_as_url` covers the relative
                    # path too, since it has no http scheme, and applies the
                    # column's 1024 width.
                    filename = _as_url(opengraph.get('og:image') or opengraph.get('og:image:url'), 1024)
                    if filename:
                        filename = filename.replace('.jpg', '.720p.mp4')
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)
            else:
                post.type = constants.POST_TYPE_LINK
                # remove unnecessary "cross-posted from..." message that Lemmy inserts (only on link posts where we have a UI showing cross-posts)
                if post.body and ('cross-posted from: https://' in post.body or 'cross-posted from:  https://' in post.body):
                    lines = []
                    for line in post.body.split('\n'):
                        if not 'cross-posted from:  https://' in line.strip() and not 'cross-posted from: https://' in line.strip():
                            lines.append(line)
                    post.body = '\n'.join(lines)
                    post.body_html = markdown_to_html(post.body)
            domain = domain_from_url(post.url)
            # `if domain:` because domain_from_url returns None for a url whose host
            # it cannot determine -- unparseable, or parseable but hostless
            # ('https:///x' parses, .hostname is None). post.url is peer-supplied, so
            # without this the first dereference below is AttributeError: 'NoneType'
            # object has no attribute 'notify_mods', and create_post's
            # `except Exception` silently drops the peer's whole post. Same shape the
            # three sites in app/shared/post.py (:192, :443, :566) already use.
            if domain:
                # notify about links to banned websites.
                already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
                # D1391. Three things this dict has to get right, because
                # app/templates/user/notifs/20.html's
                # `post_from_suspicious_domain` block reads `orig_post_title`
                # (:92), `orig_post_body` (:106) and `suspect_user_user_name`
                # (:110), and a key no producer writes renders as empty rather
                # than raising.
                #
                # `domain.name`, not `post.domain`: `post.domain` is assigned
                # BELOW (:2737), so it was None here for every new post -- and in
                # `update_post_from_activity`, which builds the same dict, it is
                # the OLD Domain object, which a db.JSON column cannot take.
                # Measured there: `StatementError (builtins.TypeError) Object of
                # type Domain is not JSON serializable`.
                #
                # `suspect_user_user_name` was written by none of the three
                # producers, so the Author line rendered `/u/` with no text on
                # every path. The name is the one the report templates use.
                targets_data = {'gen': '0',
                                'post_id': post.id,
                                'orig_post_title': post.title,
                                'orig_post_body': post.body,
                                'orig_post_domain': domain.name,
                                'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                                }
                if domain.notify_mods:
                    # D1423. `post.community`, not `community`. The Post is built a few
                    # lines above and has not been added to the session yet, so the
                    # RELATIONSHIP is None however good `community_id` is -- and
                    # `None.moderators()` is an AttributeError that `create_post` catches,
                    # logs as a failure and turns into a dropped post. So flagging a domain
                    # `notify_mods` did not notify the moderators of anything: it silently
                    # discarded every incoming post linking to that domain. The community
                    # is already an argument to this function; it is the same object.
                    for community_member in community.moderators():
                        # local moderators only, as edit_post and update_post_from_activity do (D288)
                        if community_member.user.is_local():
                            notify = Notification(title='Suspicious content', url=post.ap_id,
                                                  user_id=community_member.user_id,
                                                  author_id=user.id, notif_type=NOTIF_REPORT,
                                                  subtype='post_from_suspicious_domain',
                                                  targets=targets_data)
                            db.session.add(notify)
                            already_notified.add(community_member.user_id)
                if domain.notify_admins:
                    # D1391. This reassigned `targets_data` to two keys, so an
                    # admin's notification lost the title, the body and the
                    # author that a moderator's for the SAME post carried -- and
                    # this is the federated path, where the post came from a peer
                    # and the context matters most. The other two producers
                    # (app/shared/post.py, app/activitypub/util.py) both give
                    # admins the same dict as moderators.
                    for admin in Site.admins():
                        if admin.id not in already_notified:
                            notify = Notification(title='Suspicious content',
                                                  url=post.ap_id, user_id=admin.id,
                                                  author_id=user.id, notif_type=NOTIF_REPORT,
                                                  subtype='post_from_suspicious_domain',
                                                  targets=targets_data)
                            db.session.add(notify)
                if domain.banned or domain.name.endswith('.pages.dev'):
                    raise Exception(domain.name + ' is blocked by admin')
                else:
                    domain.post_count += 1
                    post.domain = domain

            # D1352. `request_json['object']['image']['url']` with no guard at all.
            # A bare-string `image` -- valid ActivityPub, and the commonest
            # spelling outside Lemmy -- was `TypeError: string indices must be
            # integers`, `image: {}` was a KeyError, and a list or a number was a
            # TypeError apiece. `create_post`'s `except Exception` then dropped the
            # peer's whole post. `image_url_from` is the one reading of this key.
            image_url = image_url_from(request_json['object'].get('image'))
            if image_url and post.image is None:
                image = File(source_url=image_url)
                db.session.add(image)
                post.image = image
            if post.image is None:  # This is a link post but the source instance has not provided a thumbnail image
                # Let's see if we can do better than the source instance did!
                opengraph = opengraph_parse(thumbnail_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    # D1405. `og:image` is a string from a page this instance
                    # fetched, and it becomes `File.source_url`, which twelve
                    # templates render as a bare `href` through `view_url()`. The
                    # old guard, `not filename.startswith('/')`, skipped a relative
                    # path and admitted every scheme; `_as_url` covers the relative
                    # path too, since it has no http scheme, and applies the
                    # column's 1024 width.
                    filename = _as_url(opengraph.get('og:image') or opengraph.get('og:image:url'), 1024)
                    if filename:
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)

        if post is not None:
            if request_json['object']['type'] == 'Video':
                post.type = constants.POST_TYPE_VIDEO
                post.url = request_json['object']['id']
                # D1341. `['icon'][-1]['url']` behind a bare `isinstance(...,
                # list)`: `icon: []` was an IndexError and `icon: [5]` a TypeError,
                # and a peer's Video post was lost to either.
                icon_url = image_url_from(request_json['object'].get('icon'),
                                          prefer_last=True)
                if icon_url:
                    icon = File(source_url=icon_url)
                    db.session.add(icon)
                    post.image = icon

            # Language. Lemmy uses 'language' while Mastodon has 'contentMap'
            # D1355. The membership tests below were right about the keys and said
            # nothing about their TYPES: `identifier: 5` reached a String(5) column
            # as `ProgrammingError: operator does not exist: character varying =
            # integer`, and a 50-character one was a DataError.
            ap_language = language_from_ap(request_json['object'].get('language'))
            if ap_language is not None:
                post.language = find_language_or_create(*ap_language)
            # A non-empty dict: an empty map names no language, like an absent one, and next(iter({})) raises
            elif isinstance(request_json['object'].get('contentMap'), dict) and request_json['object']['contentMap']:
                language = find_language(next(iter(request_json['object']['contentMap'])))
                post.language_id = language.id if language else None
            else:
                from app.utils import site_language_id  # cycle: app.utils imports from this module
                post.language_id = site_language_id()
            if 'licence' in request_json['object'] and isinstance(request_json['object']['licence'], dict) \
                    and 'name' in request_json['object']['licence']:
                licence = find_licence_or_create(request_json['object']['licence']['name'])
                post.licence = licence
            if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
                for json_tag in request_json['object']['tag']:
                    # A tag that is not an object was `TypeError: string
                    # indices must be integers`, and one with no `type` a
                    # KeyError -- either killed the whole post.
                    if not isinstance(json_tag, dict) or 'type' not in json_tag:
                        continue
                    if json_tag['type'] == 'Hashtag' and 'name' in json_tag:
                        if json_tag['name'][1:].lower() != community.name.lower():  # Lemmy adds the community slug as a hashtag on every post in the community, which we want to ignore
                            hashtag = find_hashtag_or_create(json_tag['name'])
                            if hashtag:
                                post.tags.append(hashtag)
                    if json_tag['type'] == 'lemmy:CommunityTag':
                        flair = find_flair_or_create(json_tag, post.community_id)
                        if flair:
                            post.flair.append(flair)
            if 'searchableBy' in request_json['object'] and request_json['object']['searchableBy'] != 'https://www.w3.org/ns/activitystreams#Public':
                post.indexable = False

            db.session.add(post)
            post.ranking = post.post_ranking(post.score, post.posted_at)
            post.ranking_scaled = int(post.ranking + community.scale_by())
            community.post_count += 1
            community.last_active = utcnow()
            db.session.execute(text('UPDATE "user" SET post_count = post_count + 1, last_seen = now() WHERE id = :user_id'),
                               {'user_id': user.id})
            db.session.execute(text('UPDATE "site" SET last_active = NOW()'))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return Post.query.filter_by(ap_id=request_json['object']['id']).one()

            # Mentions also need a post_id
            if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
                for json_tag in request_json['object']['tag']:
                    # D1397. A string element makes `'type' in json_tag` a
                    # substring test and `json_tag['type']` a TypeError, and this
                    # one runs while the Post is already in the session.
                    json_tag = _as_dict(json_tag)
                    if 'type' in json_tag and json_tag['type'] == 'Mention':
                        profile_id = json_tag['href'] if 'href' in json_tag else None
                        if profile_id and isinstance(profile_id, str) and profile_id.startswith(current_app.config['SERVER_URL']):
                            profile_id = profile_id.lower()
                            recipient = User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
                            # A mention must not hand a followers-only body to a recipient who may not view it
                            if recipient and visibility_mod.can_view(post, recipient.id):
                                blocked_senders = blocked_users(recipient.id)
                                if post.user_id not in blocked_senders:
                                    # D1329. `db.session.get` answers a model
                                    # or None and has no `.first()`, so this was
                                    # `AttributeError: 'User' object has no
                                    # attribute 'first'` for EVERY federated post
                                    # that mentions a local user who has not
                                    # blocked the sender -- the ordinary case.
                                    author = db.session.get(User, post.user_id)
                                    if author is None:
                                        continue

                                    targets_data = {'gen': '0',
                                                    'post_id': post.id,
                                                    'post_body': post.body,
                                                    'post_title': post.title,
                                                    'author_user_name': author.ap_id if author.ap_id else author.user_name,
                                                    }
                                    # import here to avoid circular import errors
                                    from app.utils import get_recipient_language  # cycle: app.utils imports from this module
                                    with force_locale(get_recipient_language(recipient.id)):
                                        notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in post {post.id}"),
                                                                    url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                                                    author_id=post.user_id, notif_type=NOTIF_MENTION,
                                                                    subtype='post_mention',
                                                                    targets=targets_data)
                                        recipient.unread_notifications += 1
                                        db.session.add(notification)
                                        db.session.commit()

            # Polls need to be processed quite late because they need a post_id to refer to
            if request_json['object']['type'] == 'Question':
                # D1330. Four reads out of the peer's document, none of them
                # asked first: `endTime` was a KeyError when absent and a
                # DataError when not a date, `oneOf` a KeyError for a Question
                # carrying neither collection, and `choice_ap['name']` a KeyError
                # for a choice without one and a TypeError for a choice that is a
                # string. Any of them lost the whole post, not just the poll.
                #
                # A poll is built only when the peer gave both an end time and at
                # least one usable choice; otherwise the post stays an ordinary
                # post rather than becoming a poll with nothing in it. An end time
                # is required because `ap_datetime(poll.end_poll)` serialises this
                # poll back out again and a None there would break that.
                mode = 'multiple' if 'anyOf' in request_json['object'] else 'single'
                choices = request_json['object'].get('anyOf' if mode == 'multiple' else 'oneOf')
                end_poll = parse_ap_timestamp(request_json['object'].get('endTime'))
                names = []
                if end_poll and isinstance(choices, list):
                    for choice_ap in choices:
                        if isinstance(choice_ap, dict):
                            name = choice_ap.get('name')
                        else:
                            name = choice_ap if isinstance(choice_ap, str) else None
                        if isinstance(name, str) and name.strip():
                            names.append(name)
                if names:
                    post.type = constants.POST_TYPE_POLL
                    db.session.add(Poll(post_id=post.id, end_poll=end_poll, mode=mode,
                                        local_only=False))
                    for sort_order, name in enumerate(names, start=1):
                        db.session.add(PollChoice(post_id=post.id, choice_text=name,
                                                  sort_order=sort_order))
                    db.session.commit()

            if request_json['object']['type'] == 'Event':
                # D1339. Fourteen keys read out of the peer's document with `[...]`,
                # so an Event missing ANY of them was a KeyError that lost the whole
                # post -- and `maximumAttendeeCapacity`, `onlineLink`, `joinMode`,
                # `externalParticipationUrl`, `anonymousParticipation`,
                # `buyTicketsLink`, `feeCurrency`, `feeAmount` and `location` are
                # all optional in the vocabulary, so this was not an edge case: it
                # was most federated events.
                #
                # Only `startTime` is required to call it an event at all; without
                # one the post stays an ordinary post, exactly as a Question with
                # no usable choices does (D1330). The two timestamps go through
                # `parse_ap_timestamp` because a string straight into a DateTime
                # column is a DataError for anything that is not a date, which
                # poisons the transaction and takes the post with it.
                event_json = request_json['object']
                start = parse_ap_timestamp(event_json.get('startTime'))
                if start is not None:
                    post.type = constants.POST_TYPE_EVENT
                    event = Event(post_id=post.id,
                                  start=start,
                                  end=parse_ap_timestamp(event_json.get('endTime')),
                                  timezone=_as_text(event_json.get('timezone'), 30),
                                  max_attendees=_as_int(event_json.get('maximumAttendeeCapacity'), 0),
                                  participant_count=_as_int(event_json.get('participantCount'), 0),
                                  online_link=_as_url(event_json.get('onlineLink'), 1024),
                                  join_mode=_as_text(event_json.get('joinMode'), 10) or 'free',
                                  external_participation_url=_as_url(event_json.get('externalParticipationUrl'), 1024),
                                  anonymous_participation=bool(event_json.get('anonymousParticipation')),
                                  online=bool(event_json.get('isOnline')),
                                  buy_tickets_link=_as_url(event_json.get('buyTicketsLink'), 1024),
                                  event_fee_currency=_as_text(event_json.get('feeCurrency'), 4),
                                  event_fee_amount=_as_float(event_json.get('feeAmount'), 0),
                                  location=event_json.get('location') if isinstance(
                                      event_json.get('location'), (dict, list)) else None,
                                  more_info_url=more_info_url_from(event_json.get('attachment')))
                    db.session.add(event)
                # Mobilizon puts the AP ID in request_json['object']['url'] and any attached website links in a request_json['object']['attachment'] list.
                # None, not '': Post.url is nullable with no default, so None is
                # what the column holds for a post that has no url, and it is
                # what app/shared/post.py:613 already stores for a locally
                # created event with a banner image. It matters beyond tidiness
                # because post_to_page (app/activitypub/util.py:170) gates the
                # outbound attachment on `post.url is not None` -- with '' every
                # ingested Mobilizon event federated `{"href": ""}` to its
                # peers, an attachment claiming a link that is not there.
                # Rows written before this change still hold '';
                # update_post_from_activity's Links section compares an event's
                # url with itself rather than with a literal, so it tolerates
                # both and no migration is needed.
                post.url = None
                if ('attachment' in request_json['object'] and
                        isinstance(request_json['object']['attachment'], list) and
                        len(request_json['object']['attachment']) > 0):
                    for attachment_item in request_json['object']['attachment']:
                        # D1397. `attachment_item['type']` with no membership test
                        # at all: a dict without `type` was a KeyError and a string
                        # element a TypeError. `.get` answers None for both, which
                        # is not 'Link'.
                        attachment_item = _as_dict(attachment_item)
                        if attachment_item.get('type') == 'Link' and not is_more_info_link(attachment_item):
                            if 'href' in attachment_item:
                                post.url = attachment_item['href']
                                break
                # This write is BELOW the domain block above, so nothing there
                # saw it: a Mobilizon event's link had no ban check and no parse
                # check at all. None is this branch's own "no url" value, set a
                # few lines up, and the two must not diverge. Same predicate as
                # the Page path above, so an Event's link cannot carry a scheme
                # a Page's link may not (D1404).
                if post.url and not url_is_storable(post.url):
                    post.url = None
                image_url = image_url_from(request_json['object'].get('image'))  # D1352
                if image_url and post.image is None:
                    image = File(source_url=image_url)
                    db.session.add(image)
                    post.image = image

                db.session.commit()

            if post.image_id and not post.type == constants.POST_TYPE_VIDEO and get_setting('cache_remote_images_locally', True):
                if post.type == constants.POST_TYPE_IMAGE:
                    make_image_sizes(post.image_id, 512, 1200, 'posts',
                                     community.low_quality)  # the 512 sized image is for masonry view
                else:
                    make_image_sizes(post.image_id, 170, 512, 'posts',
                                     community.low_quality)  # the 512 sized image is for masonry view and API responses

            # The rest of an album: Pixelfed and Mastodon send one attachment per image
            if post.type == constants.POST_TYPE_IMAGE:
                set_post_gallery(post, request_json, community.low_quality)

            # Update list of cross posts
            if post.url:
                post.calculate_cross_posts()

            if post.community_id not in communities_banned_from(user.id) and post.status == POST_STATUS_PUBLISHED:
                notify_about_post(post)

            # attach initial upvote to author
            vote = PostVote(user_id=user.id, post_id=post.id, author_id=user.id, effect=1)
            db.session.add(vote)
            if user.is_local():
                cache.delete_memoized(recently_upvoted_posts, user.id)

            post.generate_slug(community)
            db.session.commit()

            # check new accounts to see if their comments are AI generated
            # D1332. `len(post.body)` was `TypeError: object of type 'NoneType'
            # has no len()` for a post with no body at all -- a link post, an
            # image post with no text -- so on an instance with AI detection
            # configured, a new account's first link post was lost. Only such an
            # instance reaches this line, which is why it survived.
            if current_app.config['DETECT_AI_ENDPOINT'] and user.created_very_recently() \
                    and len(post.body or '') > 250:
                from app.utils import get_request, notify_admin  # cycle: app.utils imports from this module
                try:
                    is_ai = get_request(f"{current_app.config['DETECT_AI_ENDPOINT']}?url={post.ap_id}")
                except Exception:
                    is_ai = None
                # D1331. Both keys were read outright, here and in
                # `PostReply.new`; `ai_verdict` answers None for anything the
                # endpoint says that cannot be read, and neither is worth a post.
                verdict = ai_verdict(is_ai) if is_ai and is_ai.status_code == 200 else None
                if verdict:
                    detection_result, confidence = verdict
                    if confidence > 0.8:
                        if detection_result == 'ai':
                            post.ai_generated = True
                            db.session.commit()
                        # use redis to keep track of the posts this person has done in the last day and whether each is AI-generated

                        redis_key = f"ai_detection:user:{user.id}"
                        now = time()

                        # Store each detection as a JSON entry with timestamp
                        detection_data = {
                            'detection': detection_result,
                            'confidence': confidence,
                            'timestamp': utcnow().isoformat()
                        }

                        # Add with timestamp as score
                        app_pkg.redis_client.zadd(redis_key, {json.dumps(detection_data): now})

                        # Remove entries older than 24h
                        app_pkg.redis_client.zremrangebyscore(redis_key, 0, now - 86400)

                        # Get all recent detections
                        detections = app_pkg.redis_client.zrange(redis_key, 0, -1)
                        if len(detections) >= 3:
                            ai_count = sum(1 for d in detections if json.loads(d)['detection'] != 'human')
                            ai_percentage = ai_count / len(detections)

                            # if there are 3 or more posts and > 66% of them are ai generated
                            if ai_percentage > 0.66:
                                user.banned = True  # ban
                                db.session.commit()

                                # notify admin
                                targets_data = {'gen': '0',
                                                'suspect_user_id': user.id,
                                                'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                                                'source_instance_id': 1,
                                                'source_instance_domain': '',
                                                'reporter_id': 1,
                                                'reporter_user_name': 'automated'
                                                }
                                notify_admin('User auto-banned for AI-generated content', f'/u/{user.link()}', 1,
                                             NOTIF_REPORT, 'user_reported', targets_data)

        return post

    def calculate_cross_posts(self, delete_only=False, url_changed=False):
        if not self.url and not delete_only:
            return

        if self.cross_posts and (url_changed or delete_only):
            old_cross_posts = db.session.query(Post).filter(Post.id.in_(self.cross_posts),
                                                            Post.status == POST_STATUS_PUBLISHED).all()
            self.cross_posts.clear()
            for ocp in old_cross_posts:
                if ocp.cross_posts and self.id in ocp.cross_posts:
                    ocp.cross_posts.remove(self.id)

            db.session.commit()
        if delete_only:
            return

        if self.url.count('/') < 3 or (self.url.count('/') == 3 and self.url.endswith('/')):
            # reject if url is just a domain without a path
            return

        if self.community.ap_profile_id == 'https://lemmy.zip/c/dailygames':
            # daily posts to this community (e.g. to https://travle.earth/usa or https://www.nytimes.com/games/wordle/index.html) shouldn't be treated as cross-posts
            return

        limit = 9
        new_cross_posts = db.session.query(Post).filter(Post.id != self.id, Post.url == self.url, Post.deleted == False,
                                                        Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.id)).limit(limit).all()

        # grab these rows in id order first, otherwise the same url hitting a few communities at
        # once ends up with two of these running at the same time and deadlocking on each other
        if new_cross_posts:
            db.session.query(Post.id).filter(Post.id.in_([ncp.id for ncp in new_cross_posts])).order_by(Post.id).with_for_update().all()

        # other posts: update their cross_posts field with this post.id if they have less than the limit
        for ncp in new_cross_posts:
            if ncp.cross_posts is None:
                ncp.cross_posts = [self.id]
            elif len(ncp.cross_posts) < limit:
                ncp.cross_posts.append(self.id)

        # this post: set the cross_posts field to the limited list of ids from the most recent other posts
        if new_cross_posts:
            self.cross_posts = [ncp.id for ncp in new_cross_posts]
        db.session.commit()

    def delete_dependencies(self, cache_urls=None):
        # Handle non-cascading deletes and special cleanup
        #
        # D1350. The two image deletes below passed `purge_cdn=False`, so a post
        # deleted by a moderator kept its image at the edge -- the CDN is what the
        # public reads. `cache_urls` collects instead, so a caller deleting many
        # posts still makes one request; when nobody passes one, this method flushes
        # its own at the end.
        owns_cache_urls = cache_urls is None
        if owns_cache_urls:
            cache_urls = []

        # ModLog entries should be preserved with NULL post_id
        db.session.query(ModLog).filter(ModLog.post_id == self.id).update({ModLog.post_id: None})

        # Reports should be deleted
        db.session.query(Report).filter(Report.suspect_post_id == self.id).delete()

        # Reminders should be deleted
        db.session.query(Reminder).filter(Reminder.reminder_destination == self.id, Reminder.reminder_type == 1).delete()

        # Delete event_user entries (association table, no cascade relationship)
        db.session.execute(text('DELETE FROM "event_user" WHERE post_id = :post_id'), {'post_id': self.id})

        db.session.execute(text('DELETE FROM "hidden_posts" WHERE hidden_post_id = :post_id'), {'post_id': self.id})
        db.session.execute(text('DELETE FROM "read_posts" WHERE read_post_id = :post_id'), {'post_id': self.id})
        db.session.execute(text('UPDATE "rss_feed_item" SET post_id = null WHERE post_id = :post_id'), {'post_id': self.id})

        # Handle file deletions from disk before cascade deletes the File records
        if self.image_id and self.image:
            self.image.delete_from_disk(cache_urls=cache_urls)
        if self.type == POST_TYPE_VIDEO and _store_files_in_s3() and self.url:
            # D1343. This passed `self.url` -- the whole `https://...` -- as an S3
            # KEY, so `delete_objects` was asked for an object that cannot exist
            # and every mirrored video stayed in the bucket for good. The key it
            # meant is what the sibling branches strip out.
            #
            # And `url` is both peer-written AND shared: cross-posts are found by
            # url equality, so up to ten Post rows name one video and deleting one
            # of them must not take the file the others play.
            s3_key = s3_key_from_url(self.url)
            if s3_key and not s3_object_is_referenced_elsewhere(self.url,
                                                                post_id=self.id):
                from app.shared.tasks.maintenance import delete_from_s3  # cycle: app.shared.tasks.maintenance imports from this module
                if current_app.debug:
                    delete_from_s3([s3_key])
                else:
                    delete_from_s3.delay([s3_key])

        for reply in self.replies:
            if reply.image_id and reply.image:
                reply.image.delete_from_disk(cache_urls=cache_urls)
            # Update ModLog entries to remove references to deleted replies
            db.session.query(ModLog).filter(ModLog.reply_id == reply.id).update({ModLog.reply_id: None})
            # Delete reports for this reply
            db.session.query(Report).filter(Report.suspect_post_reply_id == reply.id).delete()

        if self.archived:
            db.session.query(ArchivedPostReply).filter(ArchivedPostReply.post_id == self.id).delete()
            s3_key = s3_key_from_url(self.archived) if _store_files_in_s3() else None
            if s3_key:
                from app.shared.tasks.maintenance import delete_from_s3  # cycle: app.shared.tasks.maintenance imports from this module
                s3_files_to_delete = [s3_key]
                if current_app.debug:
                    delete_from_s3(s3_files_to_delete)
                else:
                    delete_from_s3.delay(s3_files_to_delete)
            elif os.path.isfile(self.archived):
                try:
                    os.unlink(self.archived)
                except FileNotFoundError:
                    ...

        if owns_cache_urls and cache_urls:
            flush_cdn_cache(cache_urls)

    def has_been_reported(self):
        return self.reports > 0 and current_user.is_authenticated and self.community.is_moderator()

    def youtube_can_embed(self) -> bool:
        if not self.url:
            # `"youtube.com" not in None` is TypeError, and this method was the
            # only one of the three youtube_* siblings without the `if
            # self.url:` its neighbours already carry -- youtube_embed and
            # youtube_video_id both have one.
            #
            # NOT a live crash today, and that is worth stating rather than
            # implying: all four call sites are inside a template block that
            # already tested post.url --
            # app/templates/post/post_teaser/_macros.html:397 under `{% if
            # post.url -%}` at :367, app/templates/themes/dillo/post/
            # post_teaser/_macros.html:369 under the same at :339, and
            # app/templates/post/_post_full.html:142 and :190 under
            # `{% elif post.type == POST_TYPE_LINK and post.url ... %}` at :109
            # and its VIDEO twin at :150. So this is defence in depth for a
            # public method whose safe value the templates already agree on:
            # every call site is `{% if post.youtube_can_embed() %}`, and False
            # means "render no embed".
            return False
        if "youtube.com" not in self.url:
            return False

        try:
            parsed_url = urlparse(self.url)
        except ValueError:
            # The gate above is a substring test over the whole url, so it says
            # nothing about the authority: 'https://youtube.com[abc' passes it
            # and then makes urlparse raise. Reachable from a stored post.url,
            # and this method is called from four templates -- including the
            # post-teaser macro every listing renders -- so an unguarded raise
            # here 500s every listing page containing that post, for every
            # reader, not just the post's own page.
            #
            # False is the correct degradation: all four call sites are
            # `{% if post.youtube_can_embed() %}`, and False means "render no
            # embed".
            return False
        query_params = parse_qs(parsed_url.query)

        # Only create embed for videos, playlists and shorts (not e.g. posts)
        return (
            'list' in query_params or
            'v' in query_params or
            "/shorts/" in parsed_url.path
        )

    def youtube_embed(self, rel=True) -> str:
        if self.url:
            try:
                parsed_url = urlparse(self.url)
            except ValueError:
                # '' is this method's own existing fallback for a url it cannot
                # build an embed from (the bare `return ''` below). Every
                # template interpolates the result straight into a url
                # attribute, so a str is required. Unreachable in practice --
                # all four call sites sit inside
                # `{% if post.youtube_can_embed() %}`, which now returns False
                # first -- but guarded because this is a public method and the
                # two gates are independent.
                return ''
            query_params = parse_qs(parsed_url.query)

            # Handle playlists
            if 'list' in query_params:
                playlist_id = query_params['list'][0]
                return f'videoseries?list={playlist_id}'

            if 'v' in query_params:
                video_id = query_params.pop('v')[0]
                # D1424. The `/shorts/` branch below renamed `t` to `start` and this one did
                # not, so an ordinary `watch?v=...&t=90` link embedded from the beginning: the
                # iframe player ignores `t` and honours `start`. Same rename, same two lines.
                if 't' in query_params:
                    query_params['start'] = query_params.pop('t')[0]
                if rel:
                    query_params['rel'] = '0'
                new_query = urlencode(query_params, doseq=True)
                return f'{video_id}?{new_query}'

            if '/shorts/' in parsed_url.path:
                video_id = parsed_url.path.split('/shorts/')[1].split('/')[0]
                if 't' in query_params:
                    query_params['start'] = query_params.pop('t')[0]
                if rel:
                    query_params['rel'] = '0'
                new_query = urlencode(query_params, doseq=True)
                return f'{video_id}?{new_query}'

        return ''

    def youtube_video_id(self) -> str:
        if self.url:
            try:
                parsed_url = urlparse(self.url)
            except ValueError:
                # Same as youtube_embed above: '' is this method's own existing
                # fallback, and the templates interpolate it into an
                # img.youtube.com thumbnail url.
                return ''
            query_params = parse_qs(parsed_url.query)

            if 'v' in query_params:
                return query_params['v'][0]
            if '/shorts/' in parsed_url.path:
                video_id = parsed_url.path.split('/shorts/')[1].split('/')[0]
                return f'{video_id}'

        return ''

    def url_domain(self):
        return 'https://' + furl(self.url).host + '/'

    def generate_ap_id(self, community: Community):
        if not community.post_url_type or community.post_url_type == 'friendly':
            # Make the ActivityPub ID of a post in the format of instance.tld/c/community@instance/p/post_id/post-title-as-slug
            # Use this for posts this instance is creating only - remote posts will already have an AP ID.
            if self.ap_id is None or self.ap_id == '' or len(self.ap_id) == 10:
                slug = slugify(self.title, max_length=100 - len(current_app.config["SERVER_NAME"]))
                if slug:
                    self.ap_id = f'{current_app.config["SERVER_URL"]}/c/{community.name}@{community.ap_domain}/p/{self.id}/{slug}'
                    self.slug = f'/c/{community.name}@{community.ap_domain}/p/{self.id}/{slug}'
                else:
                    # Post title can't be slugified, fall back to old url structure
                    self.ap_id = f'{current_app.config["SERVER_URL"]}/post/{self.id}'
                    self.slug = f'/post/{self.id}'
        else:
            # Make the ActivityPub ID of a post in the format of instance.tld/post/post_id
            if self.ap_id is None or self.ap_id == '' or len(self.ap_id) == 10:
                self.ap_id = f'{current_app.config["SERVER_URL"]}/post/{self.id}'
                self.slug = f'/post/{self.id}'

    def generate_slug(self, community: Community):
        if not community.post_url_type or community.post_url_type == 'friendly':
            # Make the slug of a post in the format of /c/community@instance/p/post_id/post-title-as-slug
            # This should only be used for incoming remote posts. Locally-made posts will have a slug from generate_ap_id()
            if self.slug is None or self.slug == '':
                slug = slugify(self.title, max_length=100 - len(current_app.config["SERVER_NAME"]))
                if slug:
                    self.slug = f'/c/{community.link()}/p/{self.id}/{slug}'
                else:
                    self.slug = f'/post/{self.id}'
        else:
            # Make the slug use the old format of /post/post_id
            if self.slug is None or self.slug == '':
                self.slug = f'/post/{self.id}'

    def peertube_embed(self):
        if self.url:
            return self.url.replace('/videos/watch/', '/videos/embed/', 1)

    def is_microblog(self):
        return self.microblog and self.community.name == 'microblogs'

    def is_event(self):
        return self.type == constants.POST_TYPE_EVENT

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"{current_app.config['SERVER_URL']}/post/{self.id}"

    def public_url(self):
        return self.profile_id()

    def blocked_by_content_filter(self, content_filters, user_id):
        if self.user_id == user_id:
            return False

        # tokenize title into words (lowercase)
        tokens = re.findall(r"\w+", self.title.lower())

        for name, keywords in (content_filters or {}).items():
            for keyword in keywords:
                if keyword.lower() in tokens:
                    return name
        return False

    def blurred(self, user):
        if user is None:
            return self.nsfw or self.nsfl or self.spoiler_flair()
        else:
            return (user.hide_nsfw == 2 and self.nsfw) or \
                (user.hide_nsfl == 2 and self.nsfl) or \
                (user.ignore_bots == 2 and self.from_bot) or \
                self.spoiler_flair()

    def posted_at_localized(self, sort, locale):
        # some locales do not have a definition for 'weeks' so are unable to display some dates in some languages. Fall back to english for those languages.
        try:
            return pendulum.instance(self.last_active if sort == 'active' and self.last_active else self.posted_at).diff_for_humans(locale=locale)
        except ValueError:
            return pendulum.instance(self.last_active if sort == 'active' and self.last_active else self.posted_at).diff_for_humans(locale='en')

    def posted_at_formatted(self, sort):
        return pendulum.instance(self.last_active if sort == 'active' and self.last_active else self.posted_at).format('YYYY-MM-DD HH:mm:ss ZZ')

    def notify_new_replies(self, user_id: int) -> bool:
        existing_notification = db.session.query(NotificationSubscription).\
            filter(NotificationSubscription.entity_id == self.id,
                   NotificationSubscription.user_id == user_id,
                   NotificationSubscription.type == NOTIF_POST).first()
        return existing_notification is not None

    def language_code(self):
        if self.language_id:
            return self.language.code
        else:
            return 'en'

    def language_name(self):
        if self.language_id:
            return self.language.name
        else:
            return 'English'

    def tags_for_activitypub(self):
        return_value = []
        for flair in self.flair:
            return_value.append({'type': 'lemmy:CommunityTag',
                                 'id': f'{current_app.config["SERVER_URL"]}/c/{self.community.link()}/tag/{flair.id}',
                                 'display_name': flair.flair,
                                 'text_color': flair.text_color,
                                 'background_color': flair.background_color,
                                 'blur_images': flair.blur_images})
        for tag in self.tags:
            return_value.append({'type': 'Hashtag',
                                 'href': f'{current_app.config["SERVER_URL"]}/tag/{tag.name}',
                                 'name': f'#{tag.name}'})

        # include emojis used in body text
        if self.body and ':' in self.body:
            from app.utils import guess_mime_type  # cycle: app.utils imports from this module
            EMOJI_RE = re.compile(r':([a-z0-9_+-]{1,20}):', re.IGNORECASE)
            tokens = {
                f':{m.group(1).lower()}:'
                for m in EMOJI_RE.finditer(self.body)
            }

            if tokens:
                emojis = db.session.query(Emoji).filter(Emoji.token.in_(tokens)).order_by(Emoji.instance_id).all()
                for emoji in emojis:
                    return_value.append({
                        'type': 'Emoji',
                        'name': emoji.token,
                        'icon': {
                            'type': 'Image',
                            'mediaType': guess_mime_type(emoji.url),  # or store this in DB
                            'url': emoji.url,
                        },
                    })

        return return_value

    def spoiler_flair(self):
        for flair in self.flair:
            if flair.blur_images:
                return True

        return False

    def post_reply_count_recalculate(self):
        """Recount this post's live replies.

        D1365. This assigned to `self.post_reply_count`, which is a column on
        Community and on User and NOT on Post -- Post's is `reply_count`. Assigning
        an attribute a model does not have raises nothing: it set a stray Python
        attribute and the recount went nowhere, which is worse than the
        AttributeErrors found beside it because a caller would have seen a plausible
        number on the object and no change in the database.
        """
        self.reply_count = db.session.execute(
            text('SELECT COUNT(*) as c FROM "post_reply" WHERE post_id = :post_id AND deleted is false'),
            {'post_id': self.id}).scalar()

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    epoch = datetime(1970, 1, 1)

    def epoch_seconds(self, post_date):
        td = post_date - self.epoch
        return td.days * 86400 + td.seconds + (float(td.microseconds) / 1000000)

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    def post_ranking(self, score, post_date: datetime):
        if post_date is None:
            post_date = utcnow()
        if score is None:
            score = 1
        order = math.log(max(abs(score), 1), 10)
        sign = 1 if score > 0 else -1 if score < 0 else 0
        seconds = self.epoch_seconds(post_date) - 1685766018
        return round(sign * order + seconds / 45000, 7)

    def vote(self, user: User, vote_direction: str, emoji: str | None):
        if vote_direction == 'downvote':
            if self.author.has_blocked_user(user.id) or self.author.has_blocked_instance(user.instance_id):
                return None
        with app_pkg.redis_client.lock(f"lock:post:{self.id}", timeout=10, blocking_timeout=6):
            existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=self.id).first()
            if vote_direction == 'reversal':
                if existing_vote:  # api receives '1' for upvote, '-1' for downvote, and '0' for reversal
                    if existing_vote.effect == 1:
                        vote_direction = 'upvote'
                    elif existing_vote.effect == -1:
                        vote_direction = 'downvote'
                    else:
                        return None  # no point reversing a vote with no effect. There shouldn't be any more of these anyway, now that the vote manipulation bot detection code is removed.
                else:
                    return None      # cannot reverse non-existent vote
            if vote_direction != 'upvote' and vote_direction != 'downvote':
                # Was an assert, and asserts vanish under `python -O`. Here every
                # 'reversal' has already returned above, so this catches a direction
                # that is none of the three -- but it is still a real check rather
                # than one the interpreter can delete.
                raise ValueError(f'unresolvable vote direction: {vote_direction!r}')
            undo = None
            if existing_vote:
                # If emoji is provided and vote direction matches existing vote, just update the emoji
                if emoji and ((existing_vote.effect > 0 and vote_direction == 'upvote') or
                             (existing_vote.effect < 0 and vote_direction == 'downvote')):
                    existing_vote.emoji = emoji
                    db.session.commit()
                    self.update_reaction_cache()
                    db.session.commit()
                    return None  # No undo, vote stays as-is with new emoji

                with app_pkg.redis_client.lock(f"lock:vote:{existing_vote.id}", timeout=10, blocking_timeout=6):
                    # D1302. This subtracted the old vote's effect and stopped there,
                    # so a reversal moved the score by 2 and the reputation by 1: an
                    # author's reputation depended on the order a voter clicked in
                    # rather than on the votes standing against them. And the whole
                    # update was skipped in a low-quality community, where only
                    # UPVOTES are meant to be worth nothing, so a downvote taken back
                    # there kept costing the author forever (D1303).
                    same_direction = (existing_vote.effect > 0) == (vote_direction == 'upvote')
                    new_effect = 0.0 if same_direction else -existing_vote.effect
                    with app_pkg.redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                        db.session.execute(
                            text('UPDATE "user" SET reputation = reputation + :effect WHERE id = :user_id'),
                            {'effect': reputation_delta(existing_vote.effect, new_effect,
                                                        self.community.low_quality),
                             'user_id': self.user_id})
                        db.session.commit()
                    if existing_vote.effect > 0:  # previous vote was up
                        if vote_direction == 'upvote':  # new vote is also up, so remove it
                            db.session.delete(existing_vote)
                            db.session.commit()
                            self.up_votes -= 1
                            self.score -= existing_vote.effect  # score - (+1) = score-1
                            undo = 'Like'
                        else:  # new vote is down while previous vote was up, so reverse their previous vote
                            existing_vote.effect = -1
                            existing_vote.emoji = emoji
                            db.session.commit()
                            self.up_votes -= 1
                            self.down_votes += 1
                            self.score += existing_vote.effect * 2  # score + (-2) = score-2
                    else:  # previous vote was down
                        if vote_direction == 'downvote':  # new vote is also down, so remove it
                            db.session.delete(existing_vote)
                            db.session.commit()
                            self.down_votes -= 1
                            self.score -= existing_vote.effect  # score - (-1) = score+1
                            undo = 'Dislike'
                        else:  # new vote is up while previous vote was down, so reverse their previous vote
                            existing_vote.effect = 1
                            existing_vote.emoji = emoji
                            db.session.commit()
                            self.up_votes += 1
                            self.down_votes -= 1
                            self.score += existing_vote.effect * 2  # score + (+2) = score+2
                    db.session.commit()
            else:
                if vote_direction == 'upvote':
                    effect = 1.0
                    spicy_effect = effect
                    # Make 'hot' sort more spicy by amplifying the effect of early upvotes
                    if self.up_votes + self.down_votes <= 10:
                        spicy_effect = effect * current_app.config['SPICY_UNDER_10']
                    elif self.up_votes + self.down_votes <= 30:
                        spicy_effect = effect * current_app.config['SPICY_UNDER_30']
                    elif self.up_votes + self.down_votes <= 60:
                        spicy_effect = effect * current_app.config['SPICY_UNDER_60']
                    self.up_votes += 1
                    self.score += spicy_effect  # score + (+1) = score+1
                else:
                    effect = -1.0
                    spicy_effect = effect
                    self.down_votes += 1
                    # Make 'hot' sort more spicy by amplifying the effect of early downvotes
                    if self.up_votes + self.down_votes <= 30:
                        spicy_effect *= current_app.config['SPICY_UNDER_30']
                    elif self.up_votes + self.down_votes <= 60:
                        spicy_effect *= current_app.config['SPICY_UNDER_60']
                    self.score += spicy_effect  # score + (-1) = score-1
                vote = PostVote(user_id=user.id, post_id=self.id, author_id=self.author.id,
                                effect=effect, emoji=emoji)
                # upvotes do not increase reputation in low quality communities
                with app_pkg.redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                    db.session.execute(text('UPDATE "user" SET reputation = reputation + :effect WHERE id = :user_id'),
                                       {'effect': reputation_delta(0.0, effect,
                                                                   self.community.low_quality),
                                        'user_id': self.user_id})
                    db.session.commit()
                db.session.add(vote)

                # keep track of how many votes this user has cast today
                votes_cast = votes_cast_today(user.id)
                if votes_cast == 0:
                    app_pkg.redis_client.set(f'votes_cast_{date.today()}_{user.id}', 1, ex=86400)
                else:
                    app_pkg.redis_client.incr(f'votes_cast_{date.today()}_{user.id}')

            if emoji or emoji == '-1':
                db.session.commit()
                self.update_reaction_cache()

            # Calculate new ranking values
            self.ranking = self.post_ranking(self.score + self.reply_count, self.created_at)
            self.ranking_scaled = self.ranking + self.community.scale_by()

            db.session.commit()

            if user.is_local():
                with app_pkg.redis_client.lock(f"lock:user:{user.id}", timeout=10, blocking_timeout=6):
                    user.last_seen = utcnow()
                    db.session.commit()
                from app.utils import recently_upvoted_posts, recently_downvoted_posts  # cycle: app.utils imports from this module
                cache.delete_memoized(recently_upvoted_posts, user.id)
                cache.delete_memoized(recently_downvoted_posts, user.id)
        return undo

    def move_to(self, community: Community):
        self.community_id = community.id
        self.instance_id = community.instance_id
        self.flair.clear()
        db.session.execute(text('UPDATE post_reply SET community_id = :community_id, instance_id = :instance_id WHERE post_id = :post_id'),
                           {'community_id': community.id, 'instance_id': community.instance_id, 'post_id': self.id})

    def update_reaction_cache(self):
        count = func.count(PostVote.id).label("count")
        # Use LEFT JOIN so unicode emojis (not in Emoji table) are included
        rows = db.session.query(PostVote.emoji, Emoji.url, count,
                                func.array_agg(User.user_name).label("authors")).\
            outerjoin(Emoji, PostVote.emoji == Emoji.token).\
            join(User, User.id == PostVote.user_id).\
            filter(PostVote.post_id == self.id, PostVote.emoji.isnot(None)).\
            group_by(PostVote.emoji, Emoji.url).\
            order_by(count.desc()).all()

        self.emoji_reactions = [
            {
                "url": url if url else '',  # None for unicode emoji, URL for custom emoji
                "token": emoji,  # The actual emoji value (unicode or :token:)
                "authors": authors,
                "count": count,
            }
            for emoji, url, count, authors in rows
        ]

    def update_boost_cache(self):
        from app.utils import boost_cache_entries  # cycle: app.utils imports from this module
        rows = db.session.query(PostBoost.user_id, User.ap_id, User.user_name, PostBoost.created_at). \
            join(User, User.id == PostBoost.user_id). \
            filter(PostBoost.post_id == self.id). \
            order_by(PostBoost.created_at.desc()).all()
        self.post_boosts = boost_cache_entries(rows)


class PostReply(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    parent_id = db.Column(db.Integer, index=True)
    root_id = db.Column(db.Integer, index=True)
    depth = db.Column(db.Integer, default=0)
    path = db.Column(MutableList.as_mutable(ARRAY(db.Integer)), index=True)
    child_count = db.Column(db.Integer, default=0)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    body_html_safe = db.Column(db.Boolean, default=False)
    score = db.Column(db.Integer, default=0, index=True)  # used for 'top' sorting
    indexable = db.Column(db.Boolean, default=True, index=True)
    nsfw = db.Column(db.Boolean, default=False, index=True)
    content_warning = db.Column(db.Text)  # a peer's `summary`, shown collapsed above the body
    private = db.Column(db.Boolean, default=False, index=True)
    visibility = db.Column(db.String(10), default='public', server_default='public', nullable=False, index=True)
    distinguished = db.Column(db.Boolean, default=False)
    notify_author = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)
    posted_at = db.Column(db.DateTime, index=True, default=utcnow)
    deleted = db.Column(db.Boolean, default=False, index=True)
    deleted_by = db.Column(db.Integer, index=True)
    replies_enabled = db.Column(db.Boolean, default=True)
    sticky = db.Column(db.Boolean, default=False, index=True)
    ip = db.Column(db.String(50))
    from_bot = db.Column(db.Boolean, default=False, index=True)
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Float, default=0.0, index=True)  # used for 'hot' sorting
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)  # how many times this post has been reported. Set to -1 to ignore reports
    answer = db.Column(db.Boolean, default=False)   # this comment was designated as the best answer to a question
    emoji_reactions = db.Column(db.JSON)            # a cache of the emoji reactions a post has received, to avoid joins
    collapsible = db.Column(db.Boolean, default=True)

    ap_id = db.Column(db.String(255), index=True, unique=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))
    ap_updated = db.Column(db.DateTime)  # When the remote instance edited the PostReply. Useful when local instance has been offline and a flurry of potentially out of order updates are coming in.

    search_vector = db.Column(TSVectorType('body', auto_index=False))

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id], single_parent=True, overlaps="post_replies")
    community = db.relationship('Community', lazy='joined', overlaps='replies', foreign_keys=[community_id])
    language = db.relationship('Language', foreign_keys=[language_id], lazy='joined')
    image = db.relationship('File', foreign_keys=[image_id], cascade='all, delete')
    modlog = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.reply_id", back_populates='reply')
    votes = db.relationship('PostReplyVote', lazy='dynamic', backref='reply', cascade='all, delete-orphan', passive_deletes=True)
    bookmarks = db.relationship('PostReplyBookmark', backref='reply', cascade='all, delete-orphan')

    __table_args__ = (
        db.Index(
            'ix_post_reply_community_id_not_deleted',
            'community_id',
            postgresql_where=db.text('deleted = false')
        ),
        db.Index(
            'idx_post_reply_fts',
            'search_vector',
            postgresql_using='gin'
        ),
        db.Index(
            'idx_post_reply_path',
            'path',
            postgresql_using='gin'
        ),
        db.Index(
            'ix_post_reply_community_posted_bot',
            'community_id', 'posted_at',
            postgresql_where=db.text('from_bot = false')
        )
    )

    @classmethod
    def new(cls, user: User, post: Post, in_reply_to, body, body_html, notify_author, language_id, distinguished, answer,
            request_json: dict = None, announce_id=None, session=None):
        # cycle: app.utils imports from this module
        from app.utils import shorten_string, blocked_phrases, recently_upvoted_post_replies, reply_already_exists, \
            reply_is_just_link_to_gif_reaction, reply_is_low_effort, wilson_confidence_lower_bound, get_setting
        # cycle: app.activitypub.util imports from this module
        from app.activitypub.util import notify_about_post_reply, activitypub_visibility, content_warning_from

        if session is None:
            session = db.session

        if not post.comments_enabled:
            raise PostReplyValidationError(_('Comments are disabled on this post'))

        if user.ban_comments:
            raise PostReplyValidationError(_('Banned from commenting'))

        if in_reply_to is not None:
            parent_id = in_reply_to.id
            depth = in_reply_to.depth + 1
        else:
            parent_id = None
            depth = 0

        visibility = activitypub_visibility(request_json['object']) \
            if request_json and isinstance(request_json.get('object'), dict) else 'public'

        reply = PostReply(user_id=user.id, post_id=post.id, parent_id=parent_id,
                          depth=depth,
                          community_id=post.community.id, body=body,
                          body_html=body_html, body_html_safe=True,
                          from_bot=user.bot or user.bot_override, nsfw=post.nsfw,
                          notify_author=notify_author, instance_id=user.instance_id,
                          language_id=language_id, collapsible=user.id != post.user_id,
                          distinguished=distinguished, answer=answer, visibility=visibility,
                          indexable=user.indexable,
                          content_warning=content_warning_from(request_json['object']) if request_json else None,
                          ap_id=request_json['object']['id'] if request_json else None,
                          ap_create_id=request_json['id'] if request_json else None,
                          ap_announce_id=announce_id)
        # 'type' in request_json, not just request_json -- Post.new's equivalent
        # read one class up has always been guarded this way, and this one was
        # not. The resolvers in app/activitypub/util.py synthesise their
        # activity as {'id': ..., 'object': post_data} with no 'type' key, so
        # this raised KeyError('type') for every remote reply; create_post_reply
        # swallowed it in its `except Exception` and the caller saw None. Three
        # call paths silently created no reply at all: an Announce naming a
        # reply URI, the microblog boost path, and the alpha API's resolve.
        # Registered as D35.
        if request_json and 'type' in request_json and request_json['type'] == 'Update':
            reply.edited_at = utcnow()
        if reply.body:
            for blocked_phrase in blocked_phrases():
                if blocked_phrase in reply.body:
                    raise PostReplyValidationError(_('Blocked phrase in comment'))
        if request_json and 'searchableBy' in request_json['object'] and request_json['object']['searchableBy'] != 'https://www.w3.org/ns/activitystreams#Public':
            reply.indexable = False
        if post.community.private:
            reply.indexable = False
        if in_reply_to is None or in_reply_to.parent_id is None:
            notification_target = post
        else:
            notification_target = db.session.get(PostReply, in_reply_to.parent_id)

        if notification_target.author.has_blocked_user(reply.user_id):
            raise PostReplyValidationError(_('Replier blocked'))

        if reply_already_exists(user_id=user.id, post_id=post.id, parent_id=reply.parent_id, body=reply.body):
            raise PostReplyValidationError(_('Duplicate reply'))

        site = db.session.get(Site, 1)
        if site is None:
            site = Site()

        if reply_is_just_link_to_gif_reaction(reply.body) and site.enable_gif_reply_rep_decrease:
            raise PostReplyValidationError(_('Gif comment ignored'))

        if reply_is_low_effort(reply.body) and site.enable_this_comment_filter:
            raise PostReplyValidationError(_('Low quality reply'))

        try:
            session.add(reply)
            session.commit()
        except IntegrityError:
            session.rollback()
            return PostReply.query.filter_by(ap_id=request_json['object']['id']).one()

        if in_reply_to and in_reply_to.path:
            reply.path = in_reply_to.path[:]
            reply.path.append(reply.id)
            session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                               {'parents': tuple(in_reply_to.path)})
        else:
            reply.path = [0, reply.id]
        reply.root_id = reply.path[1]

        # Notify subscribers
        notify_about_post_reply(in_reply_to, reply)

        # Subscribe to own comment
        if notify_author:
            new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=post.title), 50),
                                                        user_id=user.id, entity_id=reply.id,
                                                        type=NOTIF_REPLY)
            session.add(new_notification)

        # upvote own reply
        reply.score = 1
        reply.up_votes = 1
        reply.ranking = wilson_confidence_lower_bound(1, 0)
        vote = PostReplyVote(user_id=user.id, post_reply_id=reply.id, author_id=user.id, effect=1)
        session.add(vote)
        if user.is_local():
            cache.delete_memoized(recently_upvoted_post_replies, user.id)

        reply.ap_id = reply.profile_id()

        with app_pkg.redis_client.lock(f"lock:post:{post.id}", timeout=10, blocking_timeout=6):
            if not user.bot:
                post.reply_count += 1
                post.community.post_reply_count += 1
                post.community.last_active = post.last_active = utcnow()
            session.execute(text('UPDATE "user" SET post_reply_count = post_reply_count + 1, last_seen = now() WHERE id = :user_id'),
                               {'user_id': user.id})
            session.execute(text('UPDATE "site" SET last_active = NOW()'))
            session.commit()

            # update reply_count_cross_posted
            if post.cross_posts and len(post.cross_posts) > 0:
                ids = [post.id, *post.cross_posts]

                total = (session.query(db.func.sum(Post.reply_count)).filter(Post.id.in_(ids)).scalar()) or 0
                session.query(Post).filter(Post.id.in_(ids)).update({"reply_count_cross_posted": total}, synchronize_session=False)

                session.commit()
            else:
                post.reply_count_cross_posted = post.reply_count
                session.commit()

        # LLM Detection
        if reply.body and '—' in reply.body and user.created_very_recently() and get_setting('enable_report_em_dash_replies', True):
            # Check if this user has already been reported
            if get_setting('limit_one_em_report_per_user', False):
                cache_report = True
                previous_report = cache.get(f'em-dash_used_by_{repr(reply.author)}')
            else:
                cache_report = False
                previous_report = None

            if not previous_report:
                # usage of em-dash is highly suspect.
                from app.utils import notify_admin  # cycle: app.utils imports from this module
                # notify admin
                targets_data = {'gen': '0',
                                'suspect_user_id': user.id,
                                'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                                'source_instance_id': 1,
                                'source_instance_domain': '',
                                'reporter_id': 1,
                                'reporter_user_name': 'automated'
                                }
                notify_admin('Used em-dash in comment - likely AI', f'/u/{user.link()}', 1,
                            NOTIF_REPORT, 'user_reported', targets_data)

                # Store this in redis for a day so that duplicate reports aren't created if that setting is enabled
                if cache_report:
                    cache.set(f'em-dash_used_by_{repr(reply.author)}', True, timeout=86400)
        elif current_app.config['DETECT_AI_ENDPOINT'] and user.created_very_recently() \
                and len(reply.body or '') >= 250:
            # Use API to check new accounts to see if their comments are AI generated
            from app.utils import get_request, notify_admin  # cycle: app.utils imports from this module
            try:
                is_ai = get_request(f"{current_app.config['DETECT_AI_ENDPOINT']}?url={reply.ap_id}")
            except Exception:
                is_ai = None
            # D1331's other half, which had no guard of any kind.
            verdict = ai_verdict(is_ai) if is_ai and is_ai.status_code == 200 else None
            if verdict:
                detection_result, confidence = verdict
                if confidence > 0.8:
                    # use redis to keep track of the posts this person has done in the last day and whether each is AI-generated

                    redis_key = f"ai_detection:user:{user.id}"
                    now = time()

                    # Store each detection as a JSON entry with timestamp
                    detection_data = {
                        'reply_id': reply.id,
                        'detection': detection_result,
                        'confidence': confidence,
                        'timestamp': utcnow().isoformat()
                    }

                    # Add with timestamp as score
                    app_pkg.redis_client.zadd(redis_key, {json.dumps(detection_data): now})

                    # Remove entries older than 24h
                    app_pkg.redis_client.zremrangebyscore(redis_key, 0, now - 86400)

                    # Get all recent detections
                    detections = app_pkg.redis_client.zrange(redis_key, 0, -1)
                    if len(detections) >= 3:
                        ai_count = sum(1 for d in detections if json.loads(d)['detection'] != 'human')
                        ai_percentage = ai_count / len(detections)

                        # if there are 3 or more posts and > 66% of them are ai generated
                        if ai_percentage > 0.66:
                            user.banned = True  # ban
                            session.commit()

                            # notify admin
                            targets_data = {'gen': '0',
                                            'suspect_user_id': user.id,
                                            'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                                            'source_instance_id': 1,
                                            'source_instance_domain': '',
                                            'reporter_id': 1,
                                            'reporter_user_name': 'automated'
                                            }
                            notify_admin('User auto-banned for AI-generated content', f'/u/{user.link()}', 1,
                                         NOTIF_REPORT, 'user_reported', targets_data)

        return reply

    def language_code(self):
        if self.language_id:
            return self.language.code
        else:
            return 'en'

    def language_name(self):
        if self.language_id:
            return self.language.name
        else:
            return 'English'

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith(current_app.config['SERVER_URL'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return db.session.query(cls).filter_by(ap_id=ap_id).first()

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"{current_app.config['SERVER_URL']}/comment/{self.id}"

    def public_url(self):
        return self.profile_id()

    def posted_at_localized(self, locale):
        try:
            return pendulum.instance(self.posted_at).diff_for_humans(locale=locale)
        except ValueError:
            return pendulum.instance(self.posted_at).diff_for_humans(locale='en')

    # the ap_id of the parent object, whether it's another PostReply or a Post
    def in_reply_to(self):
        if self.parent_id is None:
            return self.post.ap_id
        else:
            parent = db.session.get(PostReply, self.parent_id)
            return parent.ap_id

    # the AP profile of the person who wrote the parent object, which could be another PostReply or a Post
    def to(self):
        if self.parent_id is None:
            return self.post.author.public_url()
        else:
            parent = db.session.get(PostReply, self.parent_id)
            return parent.author.public_url()

    def tags_for_activitypub(self):
        return_value = []
        # include emojis used in body text
        if self.body and ':' in self.body:
            from app.utils import guess_mime_type  # cycle: app.utils imports from this module
            EMOJI_RE = re.compile(r':([a-z0-9_+-]{1,20}):', re.IGNORECASE)
            tokens = {
                f':{m.group(1).lower()}:'
                for m in EMOJI_RE.finditer(self.body)
            }

            if tokens:
                emojis = db.session.query(Emoji).filter(Emoji.token.in_(tokens)).order_by(Emoji.instance_id).all()
                for emoji in emojis:
                    return_value.append({
                        'type': 'Emoji',
                        'name': emoji.token,
                        'icon': {
                            'type': 'Image',
                            'mediaType': guess_mime_type(emoji.url),  # or store this in DB
                            'url': emoji.url,
                        },
                    })

        return return_value

    def delete_dependencies(self, cache_urls=None):
        """
        Handle non-cascading deletes and special cleanup.
        Note: PostReplyBookmark and PostReplyVote are now handled by cascade='all, delete-orphan'

        `cache_urls` as in `Post.delete_dependencies`: collect the URLs this
        invalidates rather than flushing them here, so a caller deleting many
        replies makes one request (D1350).
        """
        owns_cache_urls = cache_urls is None
        if owns_cache_urls:
            cache_urls = []

        # Reminders should be deleted (no relationship defined, small table)
        db.session.query(Reminder).filter(Reminder.reminder_destination == self.id, Reminder.reminder_type == 2).delete()

        # ModLog entries should be preserved with NULL reply_id
        db.session.query(ModLog).filter(ModLog.reply_id == self.id).update({ModLog.reply_id: None})

        # Reports should be deleted (small table, not worth adding cascade)
        db.session.query(Report).filter(Report.suspect_post_reply_id == self.id).delete()

        # Handle file deletion from disk before cascade deletes the File record
        if self.image_id and self.image:
            self.image.delete_from_disk(cache_urls=cache_urls)  # D1350
        if owns_cache_urls and cache_urls:
            flush_cdn_cache(cache_urls)

    def child_replies(self):
        """The replies directly under this one.

        D1365. This read `db.session(PostReply)` -- calling the scoped session
        rather than `db.session.query(...)`, which is what `has_replies` two lines
        below does with the same filter. Nothing called it, so the TypeError had
        never been seen; the next caller would have been the first.
        """
        return db.session.query(PostReply).filter_by(parent_id=self.id).all()

    def has_replies(self, include_deleted=False):
        if include_deleted:
            reply = db.session.query(PostReply).filter_by(parent_id=self.id).first()
        else:
            reply = db.session.query(PostReply).filter_by(parent_id=self.id).filter(PostReply.deleted == False).first()
        return reply is not None

    def has_been_reported(self):
        return self.reports > 0 and current_user.is_authenticated and self.community.is_moderator()

    def blocked_by_content_filter(self, content_filters, user_id):
        r"""The reply half of the user's keyword filters, matching `Post`'s exactly.

        D1364. This had NO CALLERS -- `Post.blocked_by_content_filter` is the one the
        API and the three post-teaser templates use -- and it disagreed with that one
        four ways. Measured, with the filter `{'spoilers': ['ass']}`:

            'a classic passage'   post: False      reply: 'spoilers'
                                  Post tokenizes on `\w+` and matches whole words;
                                  this matched any substring, so one filter hid
                                  every reply containing "class" or "passage".
            keyword 'Ass'         post: 'spoilers' reply: False
                                  Post lowercases each keyword; this did not, so a
                                  filter typed with a capital silently did nothing.
            viewer is the author  post: False      reply: n/a
                                  Post exempts your own content; this took no
                                  user_id at all, so your own reply could be hidden
                                  from you.
            body is NULL          -- reply: AttributeError: 'NoneType' object has no
                                  attribute 'lower'. `PostReply.body` is nullable and
                                  a peer's `source.content` of null lands there
                                  (D1333), so wiring this up would have crashed the
                                  page for every filtering user who met one.

        Because nothing calls it, aligning it with `Post`'s had no user-visible
        consequence -- which is the only reason the substring-versus-word difference
        could be settled here rather than left as a product question. If reply
        filtering is ever wired up, a filter now means the same thing in both places.
        """
        if self.user_id == user_id:
            return False

        # tokenize body into words (lowercase), as Post does with its title
        tokens = re.findall(r"\w+", (self.body or '').lower())

        for name, keywords in (content_filters or {}).items():
            for keyword in keywords:
                if keyword.lower() in tokens:
                    return name
        return False

    def notify_new_replies(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_REPLY).first()
        return existing_notification is not None

    def vote(self, user: User, vote_direction: str, emoji: str):
        from app.utils import wilson_confidence_lower_bound  # cycle: app.utils imports from this module
        # D1304. `Post.vote` has had this refusal all along and this method had
        # none, so blocking someone stopped them downvoting your POSTS and left
        # them free to downvote every COMMENT you wrote -- which is where a
        # follow-around does its work.
        if vote_direction == 'downvote':
            if self.author.has_blocked_user(user.id) or self.author.has_blocked_instance(user.instance_id):
                return None
        with app_pkg.redis_client.lock(f"lock:post_reply:{self.id}", timeout=10, blocking_timeout=6):
            existing_vote = db.session.query(PostReplyVote).filter_by(user_id=user.id, post_reply_id=self.id).first()
            # D1196. This used to read `if existing_vote and vote_direction ==
            # 'reversal':`, so a reversal with NOTHING TO REVERSE fell through
            # to the ValueError below -- while `Post.vote` answers None for
            # exactly the same request. The API sends '0' for a reversal, so
            # the same call was a 500 for a comment and a no-op for a post:
            #
            #     PROBE bn1 comment: ValueError: unresolvable vote direction: 'reversal'
            #     PROBE bn2 post: accepted
            #
            # The early return is what `Post.vote` has, and it keeps the
            # ValueError below load-bearing for a direction that is none of
            # the three -- which is what :3357's comment is about.
            if vote_direction == 'reversal':  # api sends '1' for upvote, '-1' for downvote, and '0' for reversal
                if existing_vote:
                    if existing_vote.effect == 1:
                        vote_direction = 'upvote'
                    elif existing_vote.effect == -1:
                        vote_direction = 'downvote'
                    else:
                        return None  # no point reversing a vote with no effect
                else:
                    return None      # cannot reverse non-existent vote
            if vote_direction != 'upvote' and vote_direction != 'downvote':
                # Was an assert, and asserts vanish under `python -O`. This one is
                # load-bearing: unlike Post.vote above, `:3321` remaps 'reversal' only
                # when an existing vote is found, so a reversal with no existing vote
                # arrives here still spelled 'reversal'. Under -O the assert vanished,
                # it fell through to the else-branch below, and `effect` became -1 --
                # a NEW downvote cast past the caller's permission gates.
                raise ValueError(f'unresolvable vote direction: {vote_direction!r}')
            undo = None
            if existing_vote:
                # If emoji is provided and vote direction matches existing vote, just update the emoji
                if emoji and ((existing_vote.effect > 0 and vote_direction == 'upvote') or
                             (existing_vote.effect < 0 and vote_direction == 'downvote')):
                    existing_vote.emoji = emoji
                    db.session.commit()
                    self.update_reaction_cache()
                    db.session.commit()
                    return None  # No undo, vote stays as-is with new emoji

                # D1302, the comment half: the same half-applied reversal as
                # `Post.vote` had, on a method that never exempted a low-quality
                # community from earning reputation on an upvote at all (D1305).
                same_direction = (existing_vote.effect > 0) == (vote_direction == 'upvote')
                new_effect = 0.0 if same_direction else -existing_vote.effect
                with app_pkg.redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                    db.session.execute(text('UPDATE "user" SET reputation = reputation + :effect WHERE id = :user_id'),
                                       {'effect': reputation_delta(existing_vote.effect, new_effect,
                                                                   self.community.low_quality),
                                        'user_id': self.user_id})
                    db.session.commit()
                if existing_vote.effect > 0:  # previous vote was up
                    if vote_direction == 'upvote':  # new vote is also up, so remove it
                        db.session.delete(existing_vote)
                        db.session.commit()
                        self.up_votes -= 1
                        self.score -= 1
                        undo = 'Like'
                    else:  # new vote is down while previous vote was up, so reverse their previous vote
                        existing_vote.effect = -1
                        existing_vote.emoji = emoji
                        db.session.commit()
                        self.up_votes -= 1
                        self.down_votes += 1
                        self.score -= 2
                else:  # previous vote was down
                    if vote_direction == 'downvote':  # new vote is also down, so remove it
                        db.session.delete(existing_vote)
                        db.session.commit()
                        self.down_votes -= 1
                        self.score += 1
                        undo = 'Dislike'
                    else:  # new vote is up while previous vote was down, so reverse their previous vote
                        existing_vote.effect = 1
                        existing_vote.emoji = emoji
                        db.session.commit()
                        self.up_votes += 1
                        self.down_votes -= 1
                        self.score += 2
            else:
                effect = 1
                if vote_direction == 'upvote':
                    self.up_votes += 1
                else:
                    effect = effect * -1
                    self.down_votes += 1
                self.score += effect
                vote = PostReplyVote(user_id=user.id, post_reply_id=self.id, author_id=self.author.id,
                                     effect=effect, emoji=emoji)
                # upvotes do not increase reputation in low quality communities
                with app_pkg.redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                    db.session.execute(text('UPDATE "user" SET reputation = reputation + :effect WHERE id = :user_id'),
                                       {'effect': reputation_delta(0.0, effect,
                                                                   self.community.low_quality),
                                        'user_id': self.user_id})
                    db.session.commit()
                db.session.add(vote)

                # keep track of how many votes this user has cast today
                votes_cast = votes_cast_today(user.id)
                if votes_cast == 0:
                    app_pkg.redis_client.set(f'votes_cast_{date.today()}_{user.id}', 1, ex=86400)
                else:
                    app_pkg.redis_client.incr(f'votes_cast_{date.today()}_{user.id}')

            if emoji or emoji == '-1':
                db.session.commit()
                self.update_reaction_cache()

            # Calculate the new ranking value
            self.ranking = wilson_confidence_lower_bound(self.up_votes, self.down_votes)
            db.session.commit()
            if user.is_local():
                with app_pkg.redis_client.lock(f"lock:user:{user.id}", timeout=10, blocking_timeout=6):
                    user.last_seen = utcnow()
                    db.session.commit()
                from app.utils import recently_upvoted_post_replies, recently_downvoted_post_replies  # cycle: app.utils imports from this module
                cache.delete_memoized(recently_upvoted_post_replies, user.id)
                cache.delete_memoized(recently_downvoted_post_replies, user.id)
        return undo

    def update_reaction_cache(self):
        count = func.count(PostReplyVote.id).label("count")
        # Use LEFT JOIN so unicode emojis (not in Emoji table) are included
        rows = db.session.query(PostReplyVote.emoji, Emoji.url, count,
                                func.array_agg(User.user_name).label("authors")).\
            outerjoin(Emoji, PostReplyVote.emoji == Emoji.token).\
            join(User, User.id == PostReplyVote.user_id).\
            filter(PostReplyVote.post_reply_id == self.id, PostReplyVote.emoji.isnot(None)).\
            group_by(PostReplyVote.emoji, Emoji.url).\
            order_by(count.desc()).all()

        self.emoji_reactions = [
            {
                "url": url if url else '',  # None for unicode emoji, URL for custom emoji
                "token": emoji,  # The actual emoji value (unicode or :token:)
                "authors": authors,
                "count": count,
            }
            for emoji, url, count, authors in rows
        ]


class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    licence_id = db.Column(db.Integer, db.ForeignKey('licence.id'), index=True)
    title = db.Column(db.String(255))
    url = db.Column(db.String(2048))
    body = db.Column(db.Text)
    microblog = db.Column(db.Boolean, default=False)
    nsfw = db.Column(db.Boolean, default=False, index=True)
    nsfl = db.Column(db.Boolean, default=False, index=True)
    sticky = db.Column(db.Boolean, default=False, index=True)
    indexable = db.Column(db.Boolean, default=True)
    from_bot = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    scheduled_for = db.Column(db.DateTime, index=True)  # The first (or only) occurrence of this post
    repeat = db.Column(db.String(20), default='')  # 'daily', 'weekly', 'monthly'. Empty string = no repeat, just post once.

    @classmethod
    def new(cls, user, community: Community, request_json: dict):
        ...
        # use Post.new() for inspiration


class Domain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), index=True)
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True)  # Domains can be banned site-wide (by admin) or DomainBlock'ed by users
    notify_mods = db.Column(db.Boolean, default=False, index=True)
    notify_admins = db.Column(db.Boolean, default=False, index=True)
    post_warning = db.Column(db.String(512))
    warning_type = db.Column(db.Integer, default=0)             # 0 = warning, 1 = helpful context, 2 = recommendation

    def blocked_by(self, user):
        block = DomainBlock.query.filter_by(domain_id=self.id, user_id=user.id).first()
        return block is not None

    def purge_content(self):
        # D1350. Banning a domain removed its posts' images from disk and left the
        # CDN serving them, because both deletes passed `purge_cdn=False`. One
        # flush for the whole domain now.
        #
        # The `File.query.join(Post).filter(Post.domain_id == self.id)` loop that
        # used to run first is gone: that join is on `post.image_id == file.id`, so
        # it selected exactly the images `post.delete_dependencies` deletes a few
        # lines below, and deleting each of them twice was the only thing it added.
        # No mutant of it could die, which is what said it was redundant rather
        # than untested (fact 708).
        cache_urls = []
        posts = Post.query.filter_by(domain_id=self.id).all()
        for post in posts:
            post.delete_dependencies(cache_urls=cache_urls)
            db.session.delete(post)
        db.session.commit()
        if cache_urls:
            flush_cdn_cache(cache_urls)

    def type_to_class(self):
        if self.warning_type is None or self.warning_type == 0:
            return 'fe-warning red'
        elif self.warning_type == 1:
            return 'fe-context green'
        elif self.warning_type == 2:
            return 'fe-recommended red'
        return ''


class DomainBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class CommunityBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class CommunityMember(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    is_moderator = db.Column(db.Boolean, default=False)
    is_owner = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False, index=True)
    notify_new_posts = db.Column(db.Boolean, default=False)
    joined_via_feed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')

    __table_args__ = (
        db.Index('ix_community_member_community_banned', 'community_id', 'is_banned'),
    )


class CommunityFavorite(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)


class CommunityWikiPage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    slug = db.Column(db.String(100), index=True)
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow)
    who_can_edit = db.Column(db.Integer, default=0)  # 0 = mods & admins, 1 = trusted, 2 = community members, 3 = anyone
    revisions = db.relationship('CommunityWikiPageRevision', backref=db.backref('page'), cascade='all,delete',
                                lazy='dynamic')

    def can_edit(self, user: User, community: Community):
        # The page's OWN community, first. `community` is an argument and
        # self.community_id used to be ignored entirely, so this answered "may
        # this user edit some page of that community" -- and every caller takes
        # the community from the URL and the page from an id. A moderator of
        # any community could therefore rewrite any other community's wiki
        # pages by passing their page_id. Measured: a moderator of 'mine'
        # POSTed to /community/mine/wiki/<page in 'theirs'>/edit and the body
        # became HIJACKED.
        #
        # Fixed here rather than in the four routes, because the routes are not
        # the only callers -- three templates ask the same question to decide
        # whether to show an edit link.
        if community is None or self.community_id != community.id:
            return False
        if user.is_anonymous:
            return False
        if self.who_can_edit == 0:
            if user.is_admin() or user.is_staff() or community.is_moderator(user):
                return True
        elif self.who_can_edit == 1:
            if user.is_admin() or user.is_staff() or community.is_moderator(user) or user.trustworthy():
                return True
        elif self.who_can_edit == 2:
            if user.is_admin() or user.is_staff() or community.is_moderator(
                    user) or user.trustworthy() or community.is_member(user):
                return True
        elif self.who_can_edit == 3:
            return True
        return False


class CommunityWikiPageRevision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wiki_page_id = db.Column(db.Integer, db.ForeignKey('community_wiki_page.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    edited_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id])


class UserFollower(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    local_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    remote_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    is_accepted = db.Column(db.Boolean)              # None = request sent. True = accepted. False = Rejected
    is_inward = db.Column(db.Boolean, default=True, index=True)  # true = remote user is following a local one
    created_at = db.Column(db.DateTime, default=utcnow)


# people banned from communities
class CommunityBan(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)  # person who is banned, not the banner
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    banned_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    reason = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)
    ban_until = db.Column(db.DateTime)


class UserNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    target_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    body = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class UserExtraField(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    label = db.Column(db.String(1024))
    text = db.Column(db.String(1024))


class UserBlock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Settings(db.Model):
    name = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(1024))


class Interest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    communities = db.Column(db.Text)


class CommunityJoinRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(UUID(as_uuid=True), index=True, default=uuid.uuid4)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    joined_via_feed = db.Column(db.Boolean, default=False)


class UserFollowRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(UUID(as_uuid=True), index=True, default=uuid.uuid4)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    follow_id = db.Column(db.Integer, db.ForeignKey('user.id'))


class UserRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    answer = db.Column(db.String(512))
    status = db.Column(db.Integer, default=0, index=True)  # 0 = unapproved, 1 = approved
    created_at = db.Column(db.DateTime, default=utcnow)
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    warning = db.Column(db.String(100))
    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')

    def search_similar_names(self):
        return User.query.filter(or_(func.lower(User.user_name) == self.user.user_name.lower(), User.title == self.user.title),
                                 User.id != self.user.id).order_by(desc(User.banned)).order_by(User.reputation).limit(15)


class PostVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id', ondelete='CASCADE'), index=True)
    effect = db.Column(db.Float, index=True)
    emoji = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (
        Index('ix_post_vote_user_id_id_desc', 'user_id', desc('id')),
        db.Index(
            'ix_post_vote_post_created',
            'post_id', 'created_at'
        ),
        db.Index('ix_post_vote_created', 'created_at')
    )


class PostReplyVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # who voted
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # the author of the reply voted on - who's reputation is affected
    post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id', ondelete='CASCADE'), index=True)
    effect = db.Column(db.Float)
    emoji = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (
        Index('ix_post_reply_vote_user_id_id_desc', 'user_id', desc('id')),
        db.Index(
            'ix_post_reply_vote_reply_created',
            'post_reply_id', 'created_at'
        ),
        db.Index('ix_post_reply_vote_created', 'created_at')
    )


# save every activity to a log, to aid debugging
class ActivityPubLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(3))  # 'in' or 'out'
    activity_id = db.Column(db.String(256), index=True)
    activity_type = db.Column(db.String(50))  # e.g. 'Follow', 'Accept', 'Like', etc
    activity_json = db.Column(db.Text)  # the full json of the activity
    result = db.Column(db.String(10))  # 'success' or 'failure'
    exception_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class Filter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(50))
    filter_home = db.Column(db.Boolean, default=True)
    filter_posts = db.Column(db.Boolean, default=True)
    filter_replies = db.Column(db.Boolean, default=False)
    hide_type = db.Column(db.Integer, default=0)  # 0 = hide with warning, 1 = hide completely
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    expire_after = db.Column(db.Date)
    keywords = db.Column(db.String(500))

    def keywords_string(self):
        if self.keywords is None or self.keywords == '':
            return ''
        split_keywords = [kw.strip() for kw in self.keywords.split('\n')]
        return ', '.join(split_keywords)


class Role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    weight = db.Column(db.Integer, default=0)
    permissions = db.relationship('RolePermission')


class RolePermission(db.Model):
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'), primary_key=True)
    permission = db.Column(db.String, primary_key=True, index=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150))
    url = db.Column(db.String(512))
    read = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # who the notification should go to
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))  # the person who caused the notification to happen
    created_at = db.Column(db.DateTime, default=utcnow)
    notif_type = db.Column(db.Integer, default=NOTIF_DEFAULT, index=True)  # see constants.py for possible values: NOTIF_*
    subtype = db.Column(db.String(50), index=True)
    targets = db.Column(db.JSON)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reasons = db.Column(db.String(256))
    description = db.Column(db.String(256))
    status = db.Column(db.Integer, default=0)  # 0 = new, 1 = escalated to admin, 2 = being appealed, 3 = resolved, 4 = discarded
    type = db.Column(db.Integer, default=0)  # 0 = user, 1 = post, 2 = reply, 3 = community, 4 = conversation
    reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    suspect_community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    suspect_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    suspect_post_id = db.Column(db.Integer, db.ForeignKey('post.id'))
    suspect_post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'))
    suspect_conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'))
    in_community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    source_instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'))  # the instance of the reporter. mostly used to distinguish between local (instance 1) and remote reports
    created_at = db.Column(db.DateTime, default=utcnow)
    updated = db.Column(db.DateTime, default=utcnow)
    targets = db.Column(db.JSON)

    # textual representation of self.type
    def type_text(self):
        types = ('User', 'Post', 'Comment', 'Community', 'Conversation')
        if self.type is None:
            return ''
        else:
            return types[self.type]

    def is_local(self):
        return self.source_instance_id == 1


class NotificationSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256))  # to avoid needing to look up the thing subscribed to via entity_id
    type = db.Column(db.Integer, default=0, index=True)  # see constants.py for possible values: NOTIF_*
    entity_id = db.Column(db.Integer, index=True)  # ID of the user, post, community, etc being subscribed to
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # To whom this subscription belongs
    created_at = db.Column(db.DateTime, default=utcnow)  # Perhaps very old subscriptions can be automatically deleted


class Poll(db.Model):
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), primary_key=True)
    end_poll = db.Column(db.DateTime)
    mode = db.Column(db.String(10))  # 'single' or 'multiple' determines whether people can vote for one or multiple options
    local_only = db.Column(db.Boolean)
    latest_vote = db.Column(db.DateTime)

    choices = db.relationship('PollChoice', backref='poll', cascade='all, delete-orphan', foreign_keys='PollChoice.post_id', primaryjoin='Poll.post_id==PollChoice.post_id')

    def has_voted(self, user_id):
        existing_vote = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                    PollChoiceVote.post_id == self.post_id).first()
        return existing_vote is not None

    def vote_for_choice(self, choice_id, user_id):
        existing_vote = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                    PollChoiceVote.choice_id == choice_id).first()
        if not existing_vote:
            if self.mode == 'single':  # a new vote replaces this user's earlier one
                for old_vote in self.user_votes(user_id):
                    db.session.get(PollChoice, old_vote.choice_id).num_votes -= 1
                    db.session.delete(old_vote)
            new_vote = PollChoiceVote(choice_id=choice_id, user_id=user_id, post_id=self.post_id)
            db.session.add(new_vote)
            choice = db.session.get(PollChoice, choice_id)
            choice.num_votes += 1
            self.latest_vote = utcnow()
            db.session.commit()

    def user_votes(self, user_id):
        existing_votes = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                     PollChoiceVote.post_id == self.post_id).all()

        if not existing_votes:
            existing_votes = []

        return existing_votes

    def total_votes(self):
        return db.session.execute(text('SELECT SUM(num_votes) as s FROM "poll_choice" WHERE post_id = :post_id'),
                                  {'post_id': self.post_id}).scalar() or 0


class PollChoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    choice_text = db.Column(db.String(200))
    sort_order = db.Column(db.Integer)
    num_votes = db.Column(db.Integer, default=0)

    votes = db.relationship('PollChoiceVote', backref='choice', cascade='all, delete-orphan')

    def percentage(self, poll_total_votes):
        return math.floor(self.num_votes / poll_total_votes * 100)


class PollChoiceVote(db.Model):
    choice_id = db.Column(db.Integer, db.ForeignKey('poll_choice.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Event(db.Model):
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), primary_key=True)
    start = db.Column(db.DateTime, index=True)
    end = db.Column(db.DateTime)
    timezone = db.Column(db.String(30))
    max_attendees = db.Column(db.Integer, default=0)
    participant_count = db.Column(db.Integer, default=0)
    full = db.Column(db.Boolean, default=False)
    online_link = db.Column(db.String(1024))
    more_info_url = db.Column(db.String(1024))                      # R223: the event's 'More info' link
    join_mode = db.Column(db.String(10), default='free')            # free, restricted, external, invite
    external_participation_url = db.Column(db.String(1024))         # join_made = external: the link to the place to RSVP, e.g. meetup.com
    anonymous_participation = db.Column(db.Boolean, default=False)
    online = db.Column(db.Boolean, default=False)
    buy_tickets_link = db.Column(db.String(1024))
    event_fee_currency = db.Column(db.String(4))
    event_fee_amount = db.Column(db.Float, default=0)
    location = db.Column(db.JSON)


event_user = db.Table('event_user', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                      db.Column('status', db.Integer),
                      db.Column('participation_message', db.String(200)),
                      db.PrimaryKeyConstraint('post_id', 'user_id'))


class QuoteAuthorization(db.Model):
    # R205: a FEP-044f QuoteRequest this instance Accepted. /quote_boost_auth vouches for these and nothing else.
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id', ondelete='CASCADE'), index=True)  # the quoted post,
    post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id', ondelete='CASCADE'), index=True)  # or reply
    quoting_uri = db.Column(db.String(1024), index=True)  # the peer's object that quotes it
    approved_at = db.Column(db.DateTime, default=utcnow)


class PostBookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class PostReplyBookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class ModLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=True, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=True, index=True)
    reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'), nullable=True, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    type = db.Column(db.String(10))  # 'mod' or 'admin'
    action = db.Column(db.String(30))  # 'removing post', 'banning from community', etc
    reason = db.Column(db.String(512))
    link = db.Column(db.String(512))
    link_text = db.Column(db.String(512))
    public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id], back_populates='modlog_actor')
    community = db.relationship('Community', lazy='joined', foreign_keys=[community_id], back_populates='modlog')
    target_user = db.relationship('User', lazy='joined', foreign_keys=[target_user_id], back_populates='modlog_target')
    post = db.relationship('Post', lazy='joined', foreign_keys=[post_id], back_populates='modlog')
    reply = db.relationship('PostReply', lazy='joined', foreign_keys=[reply_id], back_populates='modlog')

    action_map = {
        'add_mod': _l('Added moderator'),
        'remove_mod': _l('Removed moderator'),
        'featured_post': _l('Featured post'),
        'unfeatured_post': _l('Unfeatured post'),
        'delete_post': _l('Deleted post'),
        'restore_post': _l('Un-deleted post'),
        'delete_post_reply': _l('Deleted comment'),
        'restore_post_reply': _l('Un-deleted comment'),
        'delete_community': _l('Deleted community'),
        'delete_user': _l('Deleted account'),
        'undelete_user': _l('Restored account'),
        'ban_user': _l('Banned account'),
        'unban_user': _l('Un-banned account'),
        'lock_post': _l('Lock post'),
        'unlock_post': _l('Un-lock post'),
        'lock_post_reply': _l('Lock comment'),
        'unlock_post_reply': _l('Un-lock comment'),
        'move_post': _l('Move post'),
        'masquerade': _l('Masqueraded as account')
    }

    # Actions recorded for admins only, whatever the public_modlog setting (D942)
    admin_only_actions = {'masquerade'}

    def action_to_str(self):
        if self.action in self.action_map:
            return self.action_map[self.action]
        else:
            return self.action

    def get_correct_link(self):
        user_action_list = ["add_mod", "remove_mod", "delete_user", "undelete_user", "ban_user", "unban_user",
                            "masquerade"]

        if self.action in user_action_list and not self.link.startswith("u/"):
            return "u/" + self.link
        else:
            return self.link

class IpBan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(50), index=True)
    notes = db.Column(db.String(150))
    created_at = db.Column(db.DateTime, default=utcnow)


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256))
    description = db.Column(db.String(256))
    icon_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    sidebar = db.Column(db.Text, default='')
    sidebar_html = db.Column(db.Text, default='')
    legal_information = db.Column(db.Text, default='')
    legal_information_html = db.Column(db.Text, default='')
    tos_url = db.Column(db.String(256))
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    enable_downvotes = db.Column(db.Boolean, default=True)
    enable_gif_reply_rep_decrease = db.Column(db.Boolean, default=False)
    enable_chan_image_filter = db.Column(db.Boolean, default=False)
    enable_this_comment_filter = db.Column(db.Boolean, default=False)
    allow_local_image_posts = db.Column(db.Boolean, default=True)
    remote_image_cache_days = db.Column(db.Integer, default=30)
    enable_nsfw = db.Column(db.Boolean, default=False)
    enable_nsfl = db.Column(db.Boolean, default=False)
    community_creation_admin_only = db.Column(db.Boolean, default=False)
    reports_email_admins = db.Column(db.Boolean, default=True)
    registration_mode = db.Column(db.String(20), default='Closed')  # possible values: Open, RequireApplication, Closed
    application_question = db.Column(db.Text, default='')
    allow_or_block_list = db.Column(db.Integer, default=2)  # 1 = allow list, 2 = block list
    allowlist = db.Column(db.Text, default='')
    blocklist = db.Column(db.Text, default='')
    blocked_phrases = db.Column(db.Text, default='')  # discard incoming content with these phrases
    auto_decline_referrers = db.Column(db.Text, default='rdrama.net\nahrefs.com\nkiwifarms.sh\nkiwifarms.st')  # automatically decline registration requests if the referrer is one of these
    created_at = db.Column(db.DateTime, default=utcnow)
    updated = db.Column(db.DateTime, default=utcnow)
    last_active = db.Column(db.DateTime, default=utcnow)
    log_activitypub_json = db.Column(db.Boolean, default=False)
    default_theme = db.Column(db.String(20), default='')
    default_filter = db.Column(db.String(20), default='')
    contact_email = db.Column(db.String(255), default='')
    about = db.Column(db.Text, default='')
    about_html = db.Column(db.Text, default='')
    logo = db.Column(db.String(40), default='')
    logo_180 = db.Column(db.String(40), default='')
    logo_152 = db.Column(db.String(40), default='')
    logo_32 = db.Column(db.String(40), default='')
    logo_16 = db.Column(db.String(40), default='')
    show_inoculation_block = db.Column(db.Boolean, default=True)
    additional_css = db.Column(db.Text)
    additional_js = db.Column(db.Text)
    private_instance = db.Column(db.Boolean, default=True)
    language_id = db.Column(db.Integer)
    honeypot = db.Column(db.Boolean, default=True)
    allowlist_mode = db.Column(db.Integer, default=0)   # 0 = weak, 1 = strong, 2 = intense


    @staticmethod
    def admins() -> List[User]:
        if hasattr(g, 'admin_ids'):
            return db.session.query(User).filter(User.id.in_(tuple(g.admin_ids))).all()
        else:
            # D442: an EXISTS, not a join, so user 1 needs no user_role row to be listed.
            # D481: matched by role name, as is_admin() is.
            return db.session.query(User).filter_by(deleted=False, banned=False).filter(
                                          or_(User.roles.any(Role.name == ROLE_ADMIN_NAME), User.id == 1)).order_by(User.id).all()

    @staticmethod
    def staff() -> List[User]:
        return db.session.query(User).filter_by(deleted=False, banned=False).filter(
                                      User.roles.any(Role.name == ROLE_STAFF_NAME)).order_by(User.id).all()  # D481: by name, as is_staff()

    def active_now(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '5 minutes' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()

    def active_daily(self):
        from app.activitypub.util import active_day  # cycle: app.activitypub.util imports from this module
        return active_day()

    def active_weekly(self):
        from app.activitypub.util import active_week  # cycle: app.activitypub.util imports from this module
        return active_week()

    def active_monthly(self):
        from app.activitypub.util import active_month  # cycle: app.activitypub.util imports from this module
        return active_month()

    def active_6monthly(self):
        from app.activitypub.util import active_half_year  # cycle: app.activitypub.util imports from this module
        return active_half_year()

    def all_active_6monthly(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '6 months' AND banned is false AND deleted is false")).scalar()

    def all_active_monthly(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 month' AND banned is false AND deleted is false")).scalar()

    def all_active_weekly(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 week' AND banned is false AND deleted is false")).scalar()

    def all_active_daily(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 day' AND banned is false AND deleted is false")).scalar()


# class IngressQueue(db.Model):
#    id = db.Column(db.Integer, primary_key=True)
#    waiting_for = db.Column(db.String(255), index=True)         # The AP ID of the object we're waiting to be created before this Activity can be ingested
#    activity_pub_log_id = db.Column(db.Integer, db.ForeignKey('activity_pub_log.id')) # The original Activity that failed because some target object does not exist
#    ap_date_published = db.Column(db.DateTime, default=utcnow)  # The value of the datePublished field on the Activity
#    created_at = db.Column(db.DateTime, default=utcnow)
#    expires = db.Column(db.DateTime, default=utcnow)            # When to give up waiting and delete this row
#
#
@login.user_loader
def load_user(id):
    return db.session.get(User, int(id))


# --- Feeds Models ---

class FeedItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)


class FeedMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    is_owner = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False, index=True)
    notify_new_communities = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class Feed(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    title = db.Column(db.String(256))  # Human name
    name = db.Column(db.String(256), index=True, unique=True)  # url
    machine_name = db.Column(db.String(50), index=True)  # url also?!
    description = db.Column(db.Text)  # markdown
    description_html = db.Column(db.Text)  # html equivalent of above markdown
    nsfw = db.Column(db.Boolean, default=False)
    nsfl = db.Column(db.Boolean, default=False)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    subscriptions_count = db.Column(db.Integer, default=0)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    instance = db.relationship('Instance', lazy='joined', foreign_keys=[instance_id])

    icon_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'))

    num_communities = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)
    public = db.Column(db.Boolean, default=False, index=True)
    last_edit = db.Column(db.DateTime, default=utcnow)
    parent_feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)
    is_instance_feed = db.Column(db.Boolean, default=False, index=True)

    ap_id = db.Column(db.String(255), index=True)
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)
    ap_public_url = db.Column(db.String(255))
    ap_followers_url = db.Column(db.String(255))
    ap_following_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_discoverable = db.Column(db.Boolean, default=False)
    ap_fetched_at = db.Column(db.DateTime)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_outbox_url = db.Column(db.String(255))
    ap_moderators_url = db.Column(db.String(255))

    banned = db.Column(db.Boolean, default=False)
    searchable = db.Column(db.Boolean, default=True)

    show_posts_in_children = db.Column(db.Boolean, default=False)
    member_communities = db.relationship('FeedItem', lazy='dynamic', cascade="all, delete-orphan")

    search_vector = db.Column(TSVectorType('name', 'description', auto_index=False))

    icon = db.relationship('File', lazy='joined', foreign_keys=[icon_id], single_parent=True, backref='feed', cascade="all, delete-orphan")
    image = db.relationship('File', lazy='joined', foreign_keys=[image_id], single_parent=True, cascade="all, delete-orphan")
    parent = db.relationship('Feed', remote_side=[id], backref=db.backref('children', lazy='dynamic'))

    def __repr__(self):
        return '<Feed {}_{}>'.format(self.name, self.id)

    @cache.memoize(timeout=500)
    def icon_image(self, size='default') -> str:
        if self.icon_id is not None:
            if size == 'default':
                if self.icon.file_path is not None:
                    return served_path(self.icon.file_path)
                if self.icon.source_url is not None:
                    return served_path(self.icon.source_url)
            elif size == 'tiny':
                if self.icon.thumbnail_path is not None:
                    return served_path(self.icon.thumbnail_path)
                if self.icon.source_url is not None:
                    return served_path(self.icon.source_url)
        return '/static/images/1px.gif'

    @cache.memoize(timeout=500)
    def header_image(self) -> str:
        if self.image_id is not None:
            if self.image.file_path is not None:
                return served_path(self.image.file_path)
            if self.image.source_url is not None:
                return served_path(self.image.source_url)
        return ''

    def display_name(self) -> str:
        if self.ap_id is None:
            return self.title
        else:
            return f"{self.title}@{self.ap_domain}"

    def link(self) -> str:
        if self.ap_id is None:
            return self.name
        else:
            return self.ap_id.lower()

    def lemmy_link(self) -> str:
        if self.ap_id is None:
            return f"~{self.name}@{current_app.config['SERVER_NAME']}"
        else:
            return f"~{self.ap_id.lower()}"

    def path(self):
        return_value = [self.machine_name]
        parent_id = self.parent_feed_id
        while parent_id is not None:
            parent_feed = db.session.get(Feed, parent_id)
            if parent_feed is None:
                break
            return_value.append(parent_feed.machine_name)
            parent_id = parent_feed.parent_feed_id
        return_value = list(reversed(return_value))
        return '/'.join(return_value)

    def creator(self):
        owner = db.session.get(User, self.user_id)
        return owner.ap_id if owner.ap_id else owner.user_name

    def parent_feed_name(self):
        # D1434, as D1422 in RssFeedItem.delete_dependencies: a top-level feed has
        # `parent_feed_id` NULL, and `db.session.get(Feed, None)` answers
        # "SAWarning: fully NULL primary key identity cannot load any object. This
        # condition may raise an error in a future release." The `if parent_feed`
        # below already treats it as no parent; asking the question at all is what
        # warns, once per render of every top-level feed.
        parent_feed = db.session.get(Feed, self.parent_feed_id) if self.parent_feed_id \
            else None
        return parent_feed.title if parent_feed else ""

    def subscribed(self, user_id: int) -> int:
        if user_id is None:
            return False
        subscription: FeedMember = FeedMember.query.filter_by(user_id=user_id, feed_id=self.id).first()
        if subscription:
            if subscription.is_owner:
                return SUBSCRIPTION_OWNER
            elif subscription.is_banned:
                return SUBSCRIPTION_BANNED
            return SUBSCRIPTION_MEMBER
        else:
            join_request = FeedJoinRequest.query.filter_by(user_id=user_id, feed_id=self.id).first()
            if join_request:
                return SUBSCRIPTION_PENDING
            else:
                return SUBSCRIPTION_NONMEMBER

    def profile_id(self):
        retval = self.ap_profile_id if self.ap_profile_id else f"{current_app.config['SERVER_URL']}/f/{self.name}"
        return retval.lower()

    def public_url(self):
        result = self.ap_public_url if self.ap_public_url else f"{current_app.config['SERVER_URL']}/f/{self.name}"
        return result

    def is_local(self):
        return self.ap_id is None or self.profile_id().startswith(current_app.config['SERVER_URL'])

    def local_url(self):
        if self.is_local():
            return self.ap_profile_id
        else:
            return f"{current_app.config['SERVER_URL']}/f/{self.ap_id}"

    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_FEED).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when there is an edit in this feed's communities
    def notification_subscribers(self):
        return list(db.session.execute(
            text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :feed_id AND type = :type '),
            {'feed_id': self.id, 'type': NOTIF_FEED}).scalars())

    # instances that have users which are members of this feed. (excluding the current instance)
    def following_instances(self, include_dormant=False) -> List[Instance]:
        # D1365, the same copy-paste as `has_followers_from_domain` below: this
        # filtered `FeedMember.community_id`, which does not exist. Only
        # `Community.following_instances` has callers, so the AttributeError had
        # never been raised.
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(FeedMember,
                                                                                    FeedMember.user_id == User.id)
        instances = instances.filter(FeedMember.feed_id == self.id, FeedMember.is_banned == False)
        if not include_dormant:
            instances = instances.filter(Instance.dormant == False)
        instances = instances.filter(Instance.id != 1, Instance.gone_forever == False)
        return instances.all()

    def has_followers_from_domain(self, domain: str) -> bool:
        # D1365. This filtered `FeedMember.community_id`, which does not exist --
        # the Community method above was copied with the model swapped and the
        # column left behind, so every call was `AttributeError: type object
        # 'FeedMember' has no attribute 'community_id'`. Nothing called it.
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(FeedMember, FeedMember.user_id == User.id)
        instances = instances.filter(FeedMember.feed_id == self.id, FeedMember.is_banned == False)
        for instance in instances:
            if instance.domain == domain:
                return True
        return False


class FeedJoinRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(UUID(as_uuid=True), index=True, default=uuid.uuid4)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)


class CommunityFlair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    flair = db.Column(db.String(50), index=True)
    text_color = db.Column(db.String(50))
    background_color = db.Column(db.String(50))
    blur_images = db.Column(db.Boolean, default=False)
    ap_id = db.Column(db.String(255), index=True, unique=True)

    def get_ap_id(self):
        if self.ap_id:
            return self.ap_id

        community = db.session.get(Community, self.community_id) if self.community_id else None
        if community is None:  # D626: no community to build the id from
            return None

        self.ap_id = community.local_url() + f"/tag/{self.id}"
        db.session.commit()
        return self.ap_id


class CommunityFlairBlock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    community_flair_id = db.Column(db.Integer, db.ForeignKey('community_flair.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)


class UserFlair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    flair = db.Column(db.String(50), index=True)


class SendQueue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    destination_domain = db.Column(db.String(255), index=True)
    destination = db.Column(db.String(1024))
    actor = db.Column(db.String(255), index=True)
    private_key = db.Column(db.String(2000))
    payload = db.Column(db.Text)
    retries = db.Column(db.Integer, default=0)
    max_retries = db.Column(db.Integer, default=40)
    retry_reason = db.Column(db.String(255))
    created = db.Column(db.DateTime, default=utcnow)
    send_after = db.Column(db.DateTime, default=utcnow, index=True)


class ActivityBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)    # where the activity will be sent to
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)  # which community the activity came from
    source_type = db.Column(db.Integer, index=True)       # the type of vote that caused this. PostVote, PostReplyVote
    source_id = db.Column(db.Integer, index=True)         # the ID of the vote that caused this. When undo-ing a vote, look it up by source_id and delete it from this table (before the batch is sent). If not found, federate the undo.
    payload = db.Column(db.JSON)
    created = db.Column(db.DateTime, default=utcnow)


class BlockedImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), index=True)
    note = db.Column(db.String(255))
    hash = db.Column(BIT(256), index=True)


class CmsPage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(100), index=True)
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    last_edited_by = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow)


class InstanceChooser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(100), index=True)
    language_id = db.Column(db.Integer, index=True)
    nsfw = db.Column(db.Boolean, default=False, index=True)
    newbie_friendly = db.Column(db.Boolean, default=True, index=True)
    hide = db.Column(db.Boolean, index=True, default=False)
    data = db.Column(db.JSON)


class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    remind_at = db.Column(db.DateTime)
    reminder_type = db.Column(db.Integer)
    reminder_destination = db.Column(db.Integer)


class Emoji(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(1024))
    token = db.Column(db.String(50), index=True)
    category = db.Column(db.String(20))
    aliases = db.Column(db.String(100), index=True)
    instance_id = db.Column(db.Integer, index=True)


class ArchivedPostReply(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, index=True)
    post_reply_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime)


class CronJobLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True)
    last_run = db.Column(db.DateTime, default=utcnow)
    frequency = db.Column(db.Interval, nullable=True)

    def get_frequency(self):
        if self.frequency is not None:
            return self.frequency
        if self.name == 'send_missed_notifs':
            return timedelta(hours=7)
        elif self.name == 'process_email_bounces':
            return timedelta(hours=7)
        elif self.name == 'clean_up_old_activities':
            return timedelta(hours=7)
        elif self.name == 'remove_orphan_files':
            return timedelta(days=8)
        elif self.name == 'daily_maintenance_celery':
            return timedelta(hours=25)
        elif self.name == 'daily_maintenance':
            return timedelta(hours=25)
        elif self.name == 'send_queue':
            return timedelta(minutes=5)
        # D1334. This used to fall off the end and answer None for any name not
        # listed above, and `app/admin/routes.py` does
        # `if diff_last_run > cron_task.get_frequency():`, so the admin dashboard
        # answered `TypeError: '>' not supported between instances of
        # 'datetime.timedelta' and 'NoneType'`. `log_cron_task_to_db` writes its
        # row with `frequency` NULL, so ADDING OR RENAMING A CRON TASK broke the
        # first page an admin opens, with the failure landing nowhere near the
        # change that caused it.
        #
        # A day is the conservative answer: a task whose schedule nobody declared
        # is still watched, and an operator is told late rather than not at all.
        # `tests/test_admin_cron_overdue.py` asserts that every name actually
        # passed to `log_cron_task_to_db` is listed above, so a rename fails a
        # test rather than quietly falling back to this.
        return timedelta(days=1)


class RevokedToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(36), unique=True, index=True)  # JWT ID
    revoked_at = db.Column(db.DateTime, default=utcnow)


class BotChallenge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(50), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    sent_at = db.Column(db.DateTime, default=utcnow)
    sent_by = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    is_a_bot = db.Column(db.Boolean)        # null means waiting for response


class PostBoost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class RssFeed(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    title = db.Column(db.String(512))
    url = db.Column(db.String(1024))
    check_frequency = db.Column(db.Integer)   # How often to check, in minutes
    flair_id = db.Column(db.Integer, db.ForeignKey('community_flair.id'), nullable=True)
    next_check = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    etag = db.Column(db.String(512), nullable=True)
    last_modified = db.Column(db.String(128), nullable=True)
    last_error = db.Column(db.DateTime, nullable=True)
    error_count = db.Column(db.Integer, nullable=False, default=0)

    items = db.relationship('RssFeedItem', backref=db.backref('feed'), cascade='all,delete', lazy='dynamic')
    flair = db.relationship('CommunityFlair')

    def delete_dependencies(self):
        # delete all feed items and their posts
        for item in self.items:
            item.delete_dependencies()


class RssFeedItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('rss_feed.id'), nullable=False, index=True)
    guid = db.Column(db.String(2048), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def delete_dependencies(self):
        # D1422. `db.session.get(Post, None)` is a real state here -- the RSS importer
        # records an item with `post_id=None` when it decides not to create a post
        # (app/cli.py) -- and SQLAlchemy answers it with
        # "SAWarning: fully NULL primary key identity cannot load any object. This
        # condition may raise an error in a future release." The `if post:` below already
        # treats it as nothing to delete; asking the question at all is what warns.
        post = db.session.get(Post, self.post_id) if self.post_id else None
        if post:
            with app_pkg.redis_client.lock(f"lock:post:{post.id}", timeout=30, blocking_timeout=30):
                post.delete_dependencies()
                db.session.delete(post)
                db.session.commit()

    __table_args__ = (
        db.UniqueConstraint('feed_id', 'guid'),
    )


def _large_community_subscribers() -> float:
    # average number of subscribers in the top 15% communities

    result = cache.get('large_community_subscribers')
    if result is None:
        sql = '''   SELECT AVG(subscriptions_count) AS avg_top_25
                    FROM (
                        SELECT subscriptions_count,
                               PERCENT_RANK() OVER (ORDER BY subscriptions_count DESC) AS percentile
                        FROM "community"
                        WHERE banned IS false and subscriptions_count > 0
                    ) AS ranked
                    WHERE percentile <= 0.15;'''
        result = db.session.execute(text(sql)).scalar()
        cache.set('large_community_subscribers', result, timeout=3600)
    return result


def _store_files_in_s3():
    return current_app.config['S3_ACCESS_KEY'] != '' and current_app.config['S3_ACCESS_SECRET'] != '' and \
        current_app.config['S3_ENDPOINT'] != ''


# At the foot, not the top: app.visibility imports UserFollower from this module, so it can be
# imported only once the models above exist. Read as visibility_mod.<name> at call time.
import app.visibility as visibility_mod  # noqa: E402  cycle: app.visibility imports from this module
