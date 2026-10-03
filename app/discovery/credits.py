"""Episode credits from a Castopod podcast's RSS feed (interop D24, decisions 5 and 6).

Hosts are the channel's <podcast:person> tags (role host or co-host; no role means host), guests the
matching item's role="guest" tags. The feed is untrusted: refused above MAX_FEED_BYTES or when it
declares a DOCTYPE or ENTITY, and parsed by the stdlib (expat) parser, which resolves no external entities.
The DOCTYPE refusal is made by expat itself, so no encoding (UTF-16, UTF-32, BOM) can hide a declaration.
"""
import re
import xml.etree.ElementTree as ElementTree
from urllib.parse import urlsplit
from xml.parsers import expat

import httpx
from flask import current_app

import app.activitypub.util as ap_util   # the module, not names: app.activitypub.util imports this module
from app import celery, db
from app.discovery.filters import clean_https_url, clean_name
from app.discovery.podcast import podcast_community_for
from app.models import Post, User
from app.utils import get_request, get_request_capped, get_task_session, patch_db_session

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_CREDITS = 20
CREDIT_NAME_LIMIT = 100
PODCAST_NAMESPACE = 'https://podcastindex.org/namespace/1.0'
HOST_ROLES = ('host', 'co-host')

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
    except (_Declaration, ValueError):  # ValueError: an encoding expat cannot read, so it cannot be vetted
        return True
    except expat.ExpatError:
        return False
    return False


def _same_episode(value, episode_url: str) -> bool:
    return isinstance(value, str) and value.strip().rstrip('/') == episode_url.strip().rstrip('/')


def _credit(person, role: str) -> dict | None:
    name = clean_name(''.join(person.itertext()), CREDIT_NAME_LIMIT)
    if name is None:
        return None
    return {'name': name, 'role': role, 'image': clean_https_url(person.get('img')),
            'profile_url': clean_https_url(person.get('href')), 'user_id': None}


def parse_feed_credits(feed_bytes: bytes, episode_url: str) -> list[dict]:
    if not feed_bytes or len(feed_bytes) > MAX_FEED_BYTES or _declares_a_dtd(feed_bytes):
        return []
    try:
        root = ElementTree.fromstring(feed_bytes)
    except (ElementTree.ParseError, expat.ExpatError, ValueError):
        return []
    channel = root.find('channel')
    if channel is None:
        return []
    credits = []
    for person in channel.findall(_PERSON):
        if (person.get('role') or 'host').strip().lower() in HOST_ROLES:
            credit = _credit(person, 'host')
            if credit is not None:
                credits.append(credit)
    for item in channel.findall('item'):
        if _same_episode(item.findtext('link'), episode_url) or _same_episode(item.findtext('guid'), episode_url):
            for person in item.findall(_PERSON):
                if (person.get('role') or '').strip().lower() == 'guest':
                    credit = _credit(person, 'guest')
                    if credit is not None:
                        credits.append(credit)
            break
    return credits[:MAX_CREDITS]


_FEED_ACCEPT = 'application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1'


def fetch_feed(rss_url: str) -> bytes | None:
    try:
        status, body = get_request_capped(rss_url, MAX_FEED_BYTES, headers={'Accept': _FEED_ACCEPT})
    except httpx.HTTPError:
        return None
    return body if status == 200 else None


def resolve_credit_user(profile_url) -> int | None:
    """The PieFed User a credit's href names when it is a fediverse account, else None. find_actor_or_create
    applies the usual guards (banned and non-allowlisted instances, blocked words, get_request's SSRF guard)."""
    if clean_https_url(profile_url) is None:
        return None
    try:
        actor = ap_util.find_actor_or_create(profile_url)
    except Exception as error:   # a bad actor document or a racing create must cost one credit its link, not the episode its credits
        current_app.logger.warning(f'discovery: credit profile {profile_url} not resolved: {type(error).__name__}')
        db.session.rollback()
        return None
    return actor.id if isinstance(actor, User) and not actor.banned else None


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


def credit_vouches(user_id, podcast) -> bool:
    """True when the credited account's own profile links back to the podcast. Its stored URL and profile fields
    are read first; alsoKnownAs is not stored, so a remote account's actor document is fetched once more."""
    wanted = _podcast_urls(podcast)
    user = db.session.get(User, user_id) if isinstance(user_id, int) else None
    if not wanted or user is None or user.banned or user.deleted:
        return False
    stored = _urls_in(user.ap_public_url)
    for field in user.extra_fields:
        stored |= _urls_in(field.text)
    if wanted & stored:
        return True
    if user.ap_id is None or not user.ap_profile_id:   # a local account has no remote document to read
        return False
    try:
        response = get_request(user.ap_profile_id, headers={'Accept': 'application/activity+json'})
        document = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return False
    return bool(wanted & _document_urls(document))


def store_credits(post: Post, credits: list) -> None:
    if not credits:
        return
    extensions = dict(post.extensions) if isinstance(post.extensions, dict) else {}
    extensions['podcast'] = {'credits': credits}
    post.extensions = extensions   # a new dict: db.JSON does not track in-place changes
    db.session.commit()


def fetch_episode_credits(post: Post, episode_url: str) -> None:
    if current_app.debug:
        fetch_episode_credits_task(post.id, episode_url)
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
                feed = fetch_feed(community.rss_url)
                if feed is None:
                    return
                credits = parse_feed_credits(feed, episode_url)
                for credit in credits:
                    # Only a profile that vouches back keeps its link; anyone else is a plain name, because
                    # the feed's say-so would let a podcast credit (and so link to) any account.
                    user_id = resolve_credit_user(credit['profile_url'])
                    verified = user_id is not None and credit_vouches(user_id, post.author)
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
            exposed.append({'name': user.display_name(), 'role': credit['role'], 'image': credit.get('image'),
                            'profile_url': credit.get('profile_url'), 'user_id': user.id})
    return exposed or None


def podcast_byline(post) -> dict | None:
    """The credits to show after a podcast episode's poster: hosts, then guests. None without credits.
    The poster is always shown by the template; the credits never stand in for it."""
    stored = podcast_credits(post)
    if stored is None:
        return None
    return {'hosts': [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'host'],
            'guests': [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'guest']}
