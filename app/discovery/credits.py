"""Episode credits from a Castopod podcast's RSS feed (interop D24, decisions 5 and 6).

Hosts are the channel's <podcast:person> tags (role host or co-host; no role means host), guests the
matching item's role="guest" tags. The feed is untrusted: refused above MAX_FEED_BYTES or when it
declares a DOCTYPE or ENTITY, and parsed by the stdlib (expat) parser, which resolves no external entities.
The DOCTYPE refusal is made by expat itself, so no encoding (UTF-16, UTF-32, BOM) can hide a declaration.
"""
import xml.etree.ElementTree as ElementTree
from xml.parsers import expat

from app.discovery.filters import clean_https_url, clean_name

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
