"""What discovery refuses to store or show (interop D24): banned or non-allowlisted instances, banned
domains, PeerTube isolation-list hosts; and how third-party names and urls are cleaned."""
import re
from urllib.parse import urlparse

from flask import current_app

from app import cache, db
from app.models import Domain
from app.utils import get_setting, instance_allowed, instance_banned, retrieve_peertube_block_list, url_is_storable

NAME_LIMIT = 256
URL_LIMIT = 1024

_TAG = re.compile(r'<[^>]*>')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]+')
_SPACE = re.compile(r'\s+')
_NSFW = re.compile(r'\bnsfw\b|\b18\+|\bporn', re.IGNORECASE)


@cache.memoize(timeout=86400)
def peertube_isolated_hosts() -> frozenset:
    """Hosts on the PeerTube isolation list (https://peertube_isolation.frama.io/). Empty when the list
    cannot be fetched: `flask init-db` already copied it into banned_instances, which is checked anyway."""
    try:
        listing = retrieve_peertube_block_list()
    except Exception:  # the upstream helper only guards its HTTP call, not malformed JSON
        current_app.logger.exception('discovery: PeerTube isolation list unreadable, treated as empty')
        listing = None
    return frozenset(line.strip().lower() for line in (listing or '').split('\n') if line.strip())


def host_is_excluded(host: str, isolated: frozenset) -> bool:
    if not host:
        return True
    host = host.strip().lower()
    if host in isolated or instance_banned(host):
        return True
    if get_setting('use_allowlist', False) and not instance_allowed(host):
        return True
    return db.session.query(Domain.id).filter(Domain.name == host, Domain.banned == True).first() is not None


def clean_name(value, limit: int = NAME_LIMIT) -> str | None:
    """Plain text: tags and control characters removed, whitespace collapsed, capped. None if empty."""
    if not isinstance(value, str):
        return None
    text = _SPACE.sub(' ', _CONTROL.sub(' ', _TAG.sub('', value))).strip()
    return text[:limit].strip() or None


def clean_https_url(value, limit: int = URL_LIMIT) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != 'https' or not hostname or not url_is_storable(value):
        return None
    return value


def looks_nsfw(name: str) -> bool:
    return isinstance(name, str) and _NSFW.search(name) is not None
