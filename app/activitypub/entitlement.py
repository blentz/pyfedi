"""Who signed this ActivityPub GET, and may that signer see a given audience.

Security track (docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Shared helpers"). One answer
used by /activities, /private_message, the post and comment routes and secure mode, so a fix to who is entitled
lands everywhere at once.

Identifying a signer never fetches: only actors already stored here are considered, so an unauthenticated
request cannot make this instance issue a GET.
"""
from urllib.parse import urlparse

from flask import request
from sqlalchemy import or_

from app import db
from app.activitypub.actor import find_actor_by_url
from app.activitypub.signature import GET_REQUIRED, HttpSignature, VerificationError, is_rfc9421, request_key_id
from app.activitypub.util import AS_PUBLIC, host_of
from app.models import Community, CommunityMember, User, UserFollower

ADDRESS_FIELDS = ('to', 'cc', 'audience', 'bto', 'bcc')

# A response whose content depends on who signed the request must never be stored by a shared cache.
PRIVATE_HEADERS = {'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}

# How many trailing path segments a keyId may carry past its actor id ('<actor>/main-key' is GoToSocial's).
_KEY_PATH_TRIMS = 2


def _owner_candidates(key_id: str) -> list[str]:
    """Actor ids a keyId may belong to, most specific first, all on the keyId's own host.

    The fragment is dropped (Mastodon '#main-key'), the host is lowercased and a default port removed
    (Review Focus 3), then up to _KEY_PATH_TRIMS trailing path segments are trimmed (GoToSocial '/main-key').
    A wrong candidate is harmless: the signature still has to verify against that actor's own key."""
    owner = key_id.split('#', 1)[0].strip()
    host = host_of(owner)
    if not host:
        return []
    parsed = urlparse(owner)
    try:
        port = parsed.port
    except ValueError:
        return []
    base = f'{parsed.scheme.lower()}://{host}'
    if port and port not in (80, 443):
        base += f':{port}'
    path = parsed.path.rstrip('/')
    candidates = [base + path]
    for _ in range(_KEY_PATH_TRIMS):
        path = path.rsplit('/', 1)[0]
        if not path:
            break
        candidates.append(base + path)
    return candidates


def signed_requester_actor():
    """The stored User or Community whose key validly signed the current GET, else None.

    GET_REQUIRED is enforced whatever SIG_REQUIRED_HEADERS_ENFORCE says: authorized fetch is a new privilege, and
    with `date` unsigned the Date check below would mean nothing. An RFC 9421 GET needs no Date: verify_request
    checks its signed `created` time instead, and GET_REQUIRED maps onto "@method" and "@target-uri"."""
    key_id = request_key_id(request)
    if not key_id:
        return None
    if not is_rfc9421(request):
        try:
            HttpSignature.check_date(request)
        except VerificationError:
            return None
    for candidate in _owner_candidates(key_id):
        actor = find_actor_by_url(candidate)
        if not actor or not getattr(actor, 'public_key', None):
            continue
        try:
            HttpSignature.verify_request(request, actor.public_key, GET_REQUIRED, enforce=True)
        except (VerificationError, ValueError):
            return None
        return actor
    return None


def addressees_of(doc) -> list[str]:
    """Every addressee of an object or activity: to, cc, audience, bto and bcc, in that order."""
    if not isinstance(doc, dict):
        return []
    result = []
    for field in ADDRESS_FIELDS:
        value = doc.get(field)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                item = item.get('id')
            if isinstance(item, str) and item:
                result.append(item)
    return result


def _actor_id(actor) -> str:
    return (actor.ap_profile_id or actor.public_url()).lower()


def _followers_url(local_actor) -> str:
    return (local_actor.ap_followers_url or f'{local_actor.public_url()}/followers').lower()


def _host_prefixes(host: str):
    return f'https://{host}/', f'http://{host}/'


def _hosts_a_follower(local_actor, requester, host: str) -> bool:
    """True when `requester` follows `local_actor`, or another account on `host` does (accepted, not banned)."""
    https_prefix, http_prefix = _host_prefixes(host)
    on_host = or_(User.ap_profile_id.startswith(https_prefix, autoescape=True),
                  User.ap_profile_id.startswith(http_prefix, autoescape=True),
                  User.id == (requester.id if isinstance(requester, User) else None))
    if isinstance(local_actor, User):
        query = db.session.query(UserFollower).join(User, User.id == UserFollower.remote_user_id).filter(
            UserFollower.local_user_id == local_actor.id,
            UserFollower.is_inward.is_(True),
            UserFollower.is_accepted.is_(True),
            on_host)
    elif isinstance(local_actor, Community):
        query = db.session.query(CommunityMember).join(User, User.id == CommunityMember.user_id).filter(
            CommunityMember.community_id == local_actor.id,
            CommunityMember.is_banned.is_(False),
            on_host)
    else:
        return False
    return db.session.query(query.exists()).scalar()


def audience_entitles(addressees: list[str], requester, local_actor=None) -> bool:
    """May `requester` (signed_requester_actor()'s answer, possibly None) see something addressed to `addressees`?

    1. a public addressee; 2. the requester itself is addressed; 3. the requester's host hosts an addressed actor
    (followers collections excluded); 4. local_actor's followers collection is addressed and the requester, or
    another account on its host, is an accepted follower (member, for a community). Rules 3 and 4 hold because
    bulk delivery already went to that host's shared inbox."""
    if any(addressee in AS_PUBLIC for addressee in addressees):
        return True
    if requester is None:
        return False
    lowered = [addressee.lower() for addressee in addressees]
    requester_id = _actor_id(requester)
    if requester_id in lowered:
        return True
    requester_host = host_of(requester_id)
    if not requester_host:
        return False
    if any(not addressee.endswith('/followers') and host_of(addressee) == requester_host for addressee in lowered):
        return True
    if local_actor is not None and _followers_url(local_actor) in lowered:
        return _hosts_a_follower(local_actor, requester, requester_host)
    return False
