"""Episode credits from a Castopod podcast's RSS feed (interop D24, decisions 5 and 6).

Hosts are the channel's <podcast:person> tags (role host or co-host; no role means host), guests the
matching item's role="guest" tags. The feed is untrusted: refused above MAX_FEED_BYTES or when it
declares a DOCTYPE or ENTITY, and parsed by the stdlib (expat) parser, which resolves no external entities.
The DOCTYPE refusal is made by expat itself, so no encoding (UTF-16, UTF-32, BOM) can hide a declaration.
"""
import json
import re
import xml.etree.ElementTree as ElementTree
from urllib.parse import urlsplit
from xml.parsers import expat

import httpx
from flask import current_app
from sqlalchemy import func

import app.activitypub.util as ap_util   # the module, not names: app.activitypub.util imports this module
from app import cache, celery, db
from app.activitypub.signature import HttpSignature
from app.discovery.filters import ascii_host, clean_https_url, clean_name, url_is_excluded
from app.discovery.podcast import podcast_community_for
from app.models import Post, Site, User
from app.utils import get_request_capped, get_task_session, patch_db_session

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_CREDITS = 20
FEED_CACHE_SECONDS = 600
CREDIT_NAME_LIMIT = 100
PODCAST_NAMESPACE = 'https://podcastindex.org/namespace/1.0'
HOST_ROLES = ('host', 'co-host')
MAX_ACTOR_BYTES = 256 * 1024
CREDIT_ACTOR_TYPES = ('Person', 'Service')
_ACTOR_ACCEPT = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'

_PERSON = f'{{{PODCAST_NAMESPACE}}}person'


class _Declaration(Exception):
    """A DOCTYPE or ENTITY declaration was seen."""


def _refuse_declaration(*_args):
    raise _Declaration()


def _declares_a_dtd(feed_bytes: bytes) -> bool:
    """True if expat meets a DOCTYPE or entity declaration or cannot read the encoding; ParseErrors are left to the real parse."""
    scanner = expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = _refuse_declaration
    scanner.EntityDeclHandler = _refuse_declaration
    try:
        scanner.Parse(feed_bytes, True)
    # An encoding expat cannot read cannot be vetted: pyexpat raises LookupError for an unknown encoding name and a
    # plain ValueError ("multi-byte encodings are not supported") for a multi-byte one.
    except (_Declaration, LookupError, ValueError):
        return True
    except expat.ExpatError:
        return False
    return False


def _episode_key(value) -> str | None:
    return value.strip().rstrip('/') if isinstance(value, str) else None


def _credit(person, role: str) -> dict | None:
    name = clean_name(''.join(person.itertext()), CREDIT_NAME_LIMIT)
    if name is None:
        return None
    return {'name': name, 'role': role, 'image': clean_https_url(person.get('img')),
            'profile_url': clean_https_url(person.get('href')), 'user_id': None}


def _is_host(role) -> bool:
    return (role or 'host').strip().lower() in HOST_ROLES   # no role means host


def _is_guest(role) -> bool:
    return (role or '').strip().lower() == 'guest'


def _people(people, role: str, wanted) -> list[dict]:
    credits = []
    for person in people:
        if wanted(person.get('role')):
            credit = _credit(person, role)
            if credit is not None:
                credits.append(credit)
    return credits[:MAX_CREDITS]


def parse_feed(feed_bytes: bytes) -> dict:
    """The feed's credits for every episode: {'hosts': [...], 'guests': {episode link or GUID: [...]}}. An item's
    link and GUID both name it; the first item that names an episode is that episode's."""
    parsed = {'hosts': [], 'guests': {}}
    if not feed_bytes or len(feed_bytes) > MAX_FEED_BYTES or _declares_a_dtd(feed_bytes):
        return parsed
    try:
        root = ElementTree.fromstring(feed_bytes)
    except ElementTree.ParseError:   # an unreadable encoding was refused above, so only a parse error is left
        return parsed
    channel = root.find('channel')
    if channel is None:
        return parsed
    parsed['hosts'] = _people(channel.findall(_PERSON), 'host', _is_host)
    for item in channel.findall('item'):
        guests = None
        for key in (_episode_key(item.findtext('link')), _episode_key(item.findtext('guid'))):
            if key is not None and key not in parsed['guests']:
                if guests is None:
                    guests = _people(item.findall(_PERSON), 'guest', _is_guest)
                parsed['guests'][key] = guests
    return parsed


def episode_credits(parsed: dict, episode_url: str) -> list[dict]:
    """One episode's hosts then guests from a parse_feed result, as new dicts the caller may change."""
    guests = parsed['guests'].get(_episode_key(episode_url), [])
    return [dict(credit) for credit in parsed['hosts'] + guests][:MAX_CREDITS]


def parse_feed_credits(feed_bytes: bytes, episode_url: str) -> list[dict]:
    return episode_credits(parse_feed(feed_bytes), episode_url)


_FEED_ACCEPT = 'application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1'


def fetch_feed(rss_url: str) -> bytes | None:
    if url_is_excluded(rss_url):
        return None
    try:
        status, body = get_request_capped(rss_url, MAX_FEED_BYTES, headers={'Accept': _FEED_ACCEPT})
    except httpx.HTTPError:
        return None
    return body if status == 200 else None


def feed_credits(rss_url: str) -> dict | None:
    """parse_feed of the podcast's feed, remembered for FEED_CACHE_SECONDS per feed URL so a podcast's episodes
    share one fetch. None when the feed could not be fetched, which is not remembered."""
    key = f'discovery:feed-credits:{rss_url}'
    parsed = cache.get(key)
    if parsed is None:
        feed = fetch_feed(rss_url)
        if feed is None:
            return None
        parsed = parse_feed(feed)
        cache.set(key, parsed, timeout=FEED_CACHE_SECONDS)
    return parsed


def _normal_url(value) -> str | None:
    """scheme and host lower-cased, trailing slash ignored; None for anything that is not an http(s) URL."""
    if not isinstance(value, str):
        return None
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        return None
    query = f'?{parts.query}' if parts.query else ''
    return f'{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip("/")}{query}'


def _urls_in(text) -> set:
    """Every URL in a string, whether bare, in an HTML anchor or in a Markdown link."""
    if not isinstance(text, str):
        return set()
    return {u for u in (_normal_url(m) for m in re.findall(r'https?://[^\s"\'<>)\]]+', text)) if u}


def _document_urls(document) -> set:
    """The URLs an actor document gives in `url`, `alsoKnownAs` and its profile-field `attachment`."""
    if not isinstance(document, dict):
        return set()
    found = set()
    for key in ('url', 'alsoKnownAs'):
        values = document.get(key)
        for value in values if isinstance(values, list) else [values]:
            found |= _urls_in(value.get('href') if isinstance(value, dict) else value)
    attachment = document.get('attachment')
    for entry in attachment if isinstance(attachment, list) else []:
        if isinstance(entry, dict):
            found |= _urls_in(entry.get('value')) | _urls_in(entry.get('href'))
    return found


def _podcast_urls(podcast) -> set:
    return {u for u in (_normal_url(podcast.ap_profile_id), _normal_url(podcast.ap_public_url)) if u}


def _names_the_podcast(href: str, podcast, document: dict) -> bool:
    """True when a same-host web URL's path names this podcast's own account: /@ + the document's own
    preferredUsername (any case), or the actor id's path or a path under it. A query, fragment or dot segment
    disqualifies it, since one server hosts many accounts and another's page must not count as this one's."""
    if '?' in href or '#' in href:
        return False
    path = urlsplit(href).path
    if any(segment in ('.', '..') or segment.lower().startswith('%2e') for segment in path.split('/')):
        return False
    path = path.rstrip('/')
    handle = document.get('preferredUsername')
    if isinstance(handle, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', handle) and path.lower() == f'/@{handle}'.lower():
        return True
    own = urlsplit(podcast.ap_profile_id).path.rstrip('/')
    return bool(own) and (path == own or path.startswith(own + '/'))


def _podcast_web_urls(podcast) -> set:
    """The podcast's web URLs (its actor document's `url`), each only when it is https on the actor id's own host,
    names this podcast's own account there (_names_the_podcast) and is not the actor id itself: the podcast writes
    its own `url`, so it must not be able to vouch for whoever links another host or another account's page.
    Remembered for FEED_CACHE_SECONDS; a failed fetch gives none and is not remembered."""
    key = f'discovery:podcast-web-urls:{podcast.ap_profile_id}'
    cached = cache.get(key)
    if cached is not None:
        return set(cached)
    document = fetch_actor_document(podcast.ap_profile_id)
    if document is None:
        return set()
    actor_id, actor_host = _normal_url(podcast.ap_profile_id), ascii_host(podcast.ap_profile_id)
    values = document.get('url')
    found = set()
    for value in values if isinstance(values, list) else [values]:
        href = clean_https_url(value.get('href') if isinstance(value, dict) else value)
        if href is not None and actor_host is not None and ascii_host(href) == actor_host and \
                _names_the_podcast(href, podcast, document) and _normal_url(href) not in (None, actor_id):
            found.add(_normal_url(href))
    cache.set(key, sorted(found), timeout=FEED_CACHE_SECONDS)
    return found


def _stored_urls(user) -> set:
    stored = _urls_in(user.ap_public_url)
    for field in user.extra_fields:
        stored |= _urls_in(field.text)
    return stored


def _signed_headers(url) -> dict | None:
    """The headers of a GET of url signed as this instance's actor, as signed_get_request sends them; None when the
    site has no key."""
    site = db.session.get(Site, 1)
    if site is None or not site.private_key:
        return None
    # send_via_async: sign only, so the GET itself goes through get_request_capped's cap and deadline
    _uri, headers, _body = HttpSignature.signed_request(url, None, site.private_key,
                                                        f"{current_app.config['SERVER_URL']}/actor#main-key",
                                                        method='get', send_via_async=True)
    return headers


def fetch_actor_document(url) -> dict | None:
    """One capped, ActivityPub-flavoured GET of an actor document (get_request's SSRF guards, no redirects), never
    to a refused host. A server that refuses it unsigned (401 or 403, authorized fetch) is asked once more, signed,
    under the same cap."""
    if url_is_excluded(url):
        return None
    try:
        status, body = get_request_capped(url, MAX_ACTOR_BYTES, headers={'Accept': _ACTOR_ACCEPT})
        if status in (401, 403) and (signed := _signed_headers(url)) is not None:
            status, body = get_request_capped(url, MAX_ACTOR_BYTES, headers=signed)
        document = json.loads(body) if status == 200 and body else None
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError, RecursionError):   # RecursionError: deep nesting
        return None
    return document if isinstance(document, dict) else None


def credit_vouches(user_id, podcast) -> bool:
    """True when the credited account's own profile links back to the podcast. Its stored URL and profile fields
    are read first; alsoKnownAs is not stored, so a remote account's actor document is fetched once more."""
    wanted = _podcast_urls(podcast)
    user = db.session.get(User, user_id) if isinstance(user_id, int) else None
    if not wanted or user is None or user.banned or user.deleted:
        return False
    if wanted & _stored_urls(user):
        return True
    if user.ap_id is None or not user.ap_profile_id:   # a local account has no remote document to read
        return False
    return bool(wanted & _document_urls(fetch_actor_document(user.ap_profile_id)))


def _known_user(href: str):
    """The User a credit's href already names: a remote account by actor id or profile URL, a local one by /u/name.
    A profile URL is self-declared, so it names an account only on the host that serves the account's actor id."""
    parts = urlsplit(href)
    if (parts.hostname or '').lower() == current_app.config['SERVER_NAME'].lower():
        local = re.fullmatch(r'/u/([A-Za-z0-9_]+)/?', parts.path)
        if local is None:
            return None
        return db.session.query(User).filter(func.lower(User.user_name) == local.group(1).lower(),
                                             User.ap_id == None).first()
    user = db.session.query(User).filter(User.ap_profile_id == href.lower()).first()
    if user is not None:
        return user
    host = ascii_host(href)
    return next((user for user in db.session.query(User).filter(User.ap_public_url == href)
                 if host is not None and ascii_host(user.ap_profile_id) == host), None)


def _canonical_document(document, fetched_from: str) -> dict | None:
    """The actor document as its own server serves it. A document fetched from a URL other than its `id` (a
    profile page, or another server claiming someone else's account) is trusted only after `id` is fetched from
    its own host and answers with that same id; anything else is no document."""
    if not isinstance(document, dict) or clean_https_url(document.get('id')) is None:
        return None
    actor_id = document['id']
    if _normal_url(actor_id) == _normal_url(fetched_from):
        return document
    canonical = fetch_actor_document(actor_id)
    if canonical is None or _normal_url(canonical.get('id')) != _normal_url(actor_id):
        return None
    return canonical


def _creditable(document: dict, podcast) -> bool:
    """A canonical actor document a credit may link: a Person or Service, or a Podcast only when it is this podcast."""
    kind = document.get('type')
    return kind in CREDIT_ACTOR_TYPES or (kind == 'Podcast' and
                                          _normal_url(document['id']) == _normal_url(podcast.ap_profile_id))


def _link_actor(document: dict) -> int | None:
    """The User for a vouching canonical actor document: an existing row is linked by its ap_profile_id and never
    overwritten; a new one is created from the canonical document already in hand (no further fetch), behind the
    guards find_actor_or_create applies (banned and non-allowlisted instances, blocked words)."""
    actor_id = document['id']
    actors = ap_util.activitypub_actor
    try:
        if not actors.validate_remote_actor(actor_id):
            return None
        actor = actors.find_actor_by_url(actor_id)
        if actor is None:
            server, address = ap_util.extract_domain_and_actor(actor_id)
            actor = ap_util.actor_json_to_model(document, address, server)
    except Exception as error:   # a bad actor document or a racing create must cost one credit its link, not the episode its credits
        current_app.logger.warning(f'discovery: credit profile {actor_id} not linked: {type(error).__name__}')
        db.session.rollback()
        return None
    return actor.id if isinstance(actor, User) and not actor.banned and not actor.deleted else None


def verified_credit_user(href, podcast, wanted: set | None = None) -> int | None:
    """R4: the PieFed User a credit's href names, only when that profile vouches back for the podcast; verified
    before anything is created. A known account's stored fields are read first (no fetch); otherwise the href's
    actor document is fetched, capped (and its `id` fetched too when the href is not that id), and the canonical
    document must be a creditable actor that links the podcast. Only then is the account found or created. A
    credit that does not vouch creates nothing. `wanted` is the podcast URLs a profile may link (default: its actor
    URL)."""
    href = clean_https_url(href)
    wanted = _podcast_urls(podcast) if wanted is None else wanted
    if href is None or not wanted:
        return None
    known = _known_user(href)
    if known is not None:
        if known.banned or known.deleted:
            return None
        if wanted & _stored_urls(known):
            return known.id
        if known.ap_id is None:   # a local account: nothing remote to read
            return None
    elif (urlsplit(href).hostname or '').lower() == current_app.config['SERVER_NAME'].lower():
        return None
    if not ap_util.activitypub_actor.validate_remote_actor(href):   # banned or non-allowlisted hosts are not fetched
        return None
    document = _canonical_document(fetch_actor_document(href), href)
    if document is None or not _creditable(document, podcast) or not wanted & _document_urls(document):
        return None
    return _link_actor(document)


def store_credits(post: Post, credits: list) -> None:
    if not credits:
        return
    extensions = dict(post.extensions) if isinstance(post.extensions, dict) else {}
    podcast = extensions.get('podcast')
    extensions['podcast'] = {**(podcast if isinstance(podcast, dict) else {}), 'credits': credits}
    post.extensions = extensions   # new dicts: db.JSON does not track in-place changes
    db.session.commit()


def fetch_episode_credits(post: Post, episode_url: str, background: bool = False) -> None:
    if current_app.debug:
        fetch_episode_credits_task(post.id, episode_url)
    elif background:   # a backfilled episode: never ahead of the inbox on the default queue
        fetch_episode_credits_task.apply_async(args=(post.id, episode_url), queue='background')
    else:
        fetch_episode_credits_task.delay(post.id, episode_url)


@celery.task
def fetch_episode_credits_task(post_id, episode_url):
    """Any failure leaves the post without credits; the byline then falls back to the podcast's name."""
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.get(Post, post_id)
                if post is None or post.deleted:
                    return
                community = podcast_community_for(post.author)
                if community is None or not community.rss_url:
                    return
                parsed = feed_credits(community.rss_url)
                if parsed is None:
                    return
                credits = episode_credits(parsed, episode_url)
                linked = {}   # one lookup (and at most one fetch) per distinct href
                wanted = None   # the podcast's actor and web URLs, read once and only when a credit has an href
                for credit in credits:
                    # Only a profile that vouches back keeps its link; anyone else is a plain name, because
                    # the feed's say-so would let a podcast credit (and so link to) any account.
                    href = _normal_url(credit['profile_url'])
                    if href is not None and href not in linked:
                        if wanted is None:
                            wanted = _podcast_urls(post.author) | _podcast_web_urls(post.author)
                        linked[href] = verified_credit_user(credit['profile_url'], post.author, wanted)
                    user_id = linked.get(href) if href is not None else None
                    verified = user_id is not None
                    credit['user_id'] = user_id if verified else None
                    credit['profile_url'] = credit['profile_url'] if verified else None
                    if verified:
                        credit['verified'] = True
                store_credits(post, credits)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def podcast_credits(post) -> list | None:
    extensions = post.extensions if isinstance(post.extensions, dict) else {}
    podcast = extensions.get('podcast')
    stored = podcast.get('credits') if isinstance(podcast, dict) else None
    return stored if isinstance(stored, list) and stored else None


def _verified_user(credit: dict):
    """The PieFed user a credit links to: only when it was verified at resolve time (the profile vouched for the
    podcast) and the account is still neither banned nor deleted."""
    user_id = credit.get('user_id')
    if credit.get('verified') is True and isinstance(user_id, int) and not isinstance(user_id, bool):
        user = db.session.get(User, user_id)
        if user is not None and not user.banned and not user.deleted:
            return user
    return None


def _credit_link(credit: dict) -> dict:
    """A linked credit shows the verified user's own display name, never the feed's (a sock-puppet could otherwise
    be labelled as a famous name); an unlinked one shows the feed's name as plain text."""
    user = _verified_user(credit)
    if user is None:
        return {'name': credit.get('name') or '', 'href': None, 'local': False}
    return {'name': user.display_name(), 'href': f'/u/{user.link()}', 'local': True}


def podcast_api_credits(post) -> list | None:
    """The credits for the API: a verified credit carries its user reference, an unverified one only name and role."""
    stored = podcast_credits(post)
    if stored is None:
        return None
    exposed = []
    for credit in stored:
        if not isinstance(credit, dict) or credit.get('role') not in ('host', 'guest'):
            continue
        user = _verified_user(credit)
        if user is None:
            exposed.append({'name': credit.get('name') or '', 'role': credit['role']})
        else:
            # Every identifying field comes from the verified user, never the feed (a feed could otherwise
            # put any picture or address beside a genuine identity).
            avatar = user.avatar.medium_url() if user.avatar_id else None
            exposed.append({'name': user.display_name(), 'role': credit['role'], 'image': avatar or None,
                            'profile_url': user.public_url(), 'user_id': user.id})
    return exposed or None


def podcast_byline(post) -> dict | None:
    """The credits to show after a podcast episode's poster: hosts, then guests. None without credits.
    The poster is always shown by the template; the credits never stand in for it."""
    stored = podcast_credits(post)
    if stored is None:
        return None
    return {'hosts': [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'host'],
            'guests': [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'guest']}
