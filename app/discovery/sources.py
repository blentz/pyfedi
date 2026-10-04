"""What every discovery fetcher shares: one polite GET, one entry shape, one failure type (interop D24)."""
import json
import re
import time
from urllib.parse import urlparse

import httpx
from flask import current_app

from app.utils import get_request_capped

POLITE_DELAY_SECONDS = 1.0
MAX_DIRECTORY_BYTES = 5 * 1024 * 1024

_HOSTNAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+')
_USERNAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')


class DiscoverySourceError(Exception):
    """The source's own index could not be read. The refresh skips the source and keeps its old entries."""


def polite_pause() -> None:
    """Spacing between two calls to one host, well under every published rate limit."""
    time.sleep(POLITE_DELAY_SECONDS)


def fetch_json(url: str, params: dict | None = None, headers: dict | None = None):
    """The decoded JSON body of a 200 answer, or None for a transport error, any other status, a body over
    MAX_DIRECTORY_BYTES, or a body that is not JSON. Goes through get_request_capped, so the SSRF guards apply,
    redirects are not followed and the body is never buffered past the cap. Logs the url only: request headers may
    carry credentials."""
    try:
        target = str(httpx.URL(url, params=params)) if params else url
        status, body = get_request_capped(target, MAX_DIRECTORY_BYTES, headers=dict(headers or {}))
    except httpx.HTTPError as error:
        current_app.logger.info(f'discovery: {url} failed: {type(error).__name__}')
        return None
    except ValueError:   # httpx.URL refuses a malformed url
        return None
    if status != 200 or body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:   # not JSON, or not decodable text
        return None


def make_entry(*, kind: str, platform: str, actor_url: str, name: str, host: str, avatar, followers: int,
               nsfw: bool, source: str) -> dict:
    return {'kind': kind, 'platform': platform, 'actor_url': actor_url, 'name': name, 'host': host,
            'avatar': avatar, 'followers': followers, 'nsfw': nsfw, 'source': source}


def as_count(value) -> int:
    """A peer's count. Anything that is not a non-negative int (bools included) is 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def is_hostname(value) -> bool:
    """A lower-case DNS name with at least one dot. Peers name hosts; we build urls from them."""
    return isinstance(value, str) and _HOSTNAME.fullmatch(value) is not None


def is_username(value) -> bool:
    return isinstance(value, str) and _USERNAME.fullmatch(value) is not None


def display_name(value, username: str) -> str:
    """The peer's display name, or the username when it has none."""
    return value if isinstance(value, str) and value.strip() else username


def avatar_of(value):
    """The peer's avatar url when it is a string, else None."""
    return value if isinstance(value, str) else None


def uri_is_on(uri, host: str) -> bool:
    """True for an https uri whose host is exactly `host`. Anything unparseable is False."""
    if not isinstance(uri, str):
        return False
    try:
        parsed = urlparse(uri)
        return parsed.scheme == 'https' and parsed.hostname == host
    except ValueError:
        return False


def actor_url_on(uri, host: str, username: str) -> str:
    """The peer's own uri when it is https and on `host`, else the canonical /users/ url on `host`."""
    return uri if uri_is_on(uri, host) else f'https://{host}/users/{username}'
