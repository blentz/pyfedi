"""Episode credits from a Castopod podcast's RSS feed (interop D24, decisions 5 and 6).

Hosts are the channel's <podcast:person> tags (role host or co-host; no role means host), guests the
matching item's role="guest" tags. The feed is untrusted: refused above MAX_FEED_BYTES or when it
declares a DOCTYPE or ENTITY, and parsed by the stdlib (expat) parser, which resolves no external entities.
The DOCTYPE refusal is made by expat itself, so no encoding (UTF-16, UTF-32, BOM) can hide a declaration.
"""
import xml.etree.ElementTree as ElementTree
from xml.parsers import expat

import httpx
from flask import current_app

import app.activitypub.util as ap_util   # the module, not names: app.activitypub.util imports this module
from app import celery, db
from app.discovery.filters import clean_https_url, clean_name
from app.discovery.podcast import podcast_community_for
from app.models import Post, User
from app.utils import get_request_capped, get_task_session, patch_db_session

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
                    credit['user_id'] = resolve_credit_user(credit['profile_url'])
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


def _credit_link(credit: dict) -> dict:
    href = None
    user_id = credit.get('user_id')
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user = db.session.get(User, user_id)
        if user is not None and not user.banned and not user.deleted:
            href = f'/u/{user.link()}'
    local = href is not None
    if href is None:
        href = clean_https_url(credit.get('profile_url'))
    return {'name': credit.get('name') or '', 'href': href, 'local': local}


def podcast_byline(post) -> dict | None:
    """What the post byline shows for a podcast episode: hosts, then guests. None without credits."""
    stored = podcast_credits(post)
    if stored is None:
        return None
    hosts = [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'host']
    guests = [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'guest']
    if not hosts:
        hosts = [{'name': post.author.display_name(), 'href': f'/u/{post.author.link()}', 'local': True}]
    return {'hosts': hosts, 'guests': guests}
