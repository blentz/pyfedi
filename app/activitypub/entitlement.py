"""Who signed this ActivityPub GET, and may that signer see a given audience.

Security track (docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Shared helpers"). One answer
used by /activities, /private_message, the post and comment routes and secure mode, so a fix to who is entitled
lands everywhere at once.

Identifying a signer never fetches: only actors already stored here are considered, so an unauthenticated
request cannot make this instance issue a GET.
"""
from urllib.parse import urlparse

from flask import g, make_response, request
from sqlalchemy import or_

from app import db
from app.activitypub.actor import find_actor_by_url
from app.activitypub.signature import GET_REQUIRED, HttpSignature, VerificationError, is_rfc9421, request_key_id
from app.activitypub.util import AS_PUBLIC, host_of
from app.constants import AP_MEDIA_TYPES
from app.models import Community, CommunityMember, User, UserFollower
from app.utils import is_activitypub_request

ADDRESS_FIELDS = ('to', 'cc', 'audience', 'bto', 'bcc')

# A response whose content depends on who signed the request must never be stored by a shared cache.
PRIVATE_HEADERS = {'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}

# How many trailing path segments a keyId may carry past its actor id ('<actor>/main-key' is GoToSocial's).
_KEY_PATH_TRIMS = 2


def _owner_candidates(key_id: str) -> list[str]:
    """Actor ids a keyId may belong to, most specific first, all on the keyId's own host.

    The fragment is dropped (Mastodon '#main-key'), the host is lowercased and a default port removed
    (Review Focus 3). The first candidate keeps the path exactly, root slash and query included (R-SECUREACTORS:
    Lemmy's site actor is 'https://host/', WordPress authors are 'https://host/?author=1'); then the path without
    its trailing slash and query, and up to _KEY_PATH_TRIMS trailing path segments trimmed (GoToSocial '/main-key').
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
    candidates = [base + parsed.path + (f'?{parsed.query}' if parsed.query else '')]
    path = parsed.path.rstrip('/')
    if base + path != candidates[0]:
        candidates.append(base + path)
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


# Routes secure mode never refuses: the instance actor (whose key a peer needs before it can sign anything at all),
# WebFinger and NodeInfo. A prefix ending in '/' matches by prefix; one without matches exactly or as a whole
# path segment, so '/actor' covers '/actor/outbox' and '/actor/inbox' but not '/actors'.
SIGNED_FETCH_EXEMPT_PREFIXES = ('/actor', '/.well-known/', '/nodeinfo/')

# Routes that answer ActivityPub JSON whatever the Accept header says, so secure mode covers them even when the
# request does not ask for ActivityPub. Matched as SIGNED_FETCH_EXEMPT_PREFIXES are. /activity_result/ is left out
# on purpose: it answers only 'Ok' or an error for an activity id the asking peer sent itself, which is no content.
SIGNED_FETCH_AP_ONLY_PREFIXES = ('/activities/', '/quote_boost_auth')

# R-SECUREACTORS: actor documents secure mode answers in minimal form instead of refusing. A peer that signs its key
# fetches with an instance actor this instance has never stored could otherwise never learn a key at all. Matched on
# the routed endpoint, so an actor's collections (/u/<name>/outbox, /c/<name>/followers...) stay refused.
MINIMAL_ACTOR_ENDPOINTS = ('activitypub.user_profile', 'activitypub.community_profile', 'activitypub.feed_profile')

# What the minimal actor document keeps: enough to verify a signature and to deliver, nothing about the actor.
MINIMAL_ACTOR_FIELDS = ('@context', 'id', 'type', 'preferredUsername', 'inbox', 'publicKey')

# Set in the WSGI environ, which is per request (g is not, while a test keeps an app context pushed).
_MINIMAL_ACTOR_FLAG = 'pyfedi.minimal_actor_document'


def _path_matches(path: str, prefixes) -> bool:
    for prefix in prefixes:
        if prefix.endswith('/'):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + '/'):
            return True
    return False


def signed_fetch_exempt(path: str) -> bool:
    return _path_matches(path, SIGNED_FETCH_EXEMPT_PREFIXES)


def _secure_mode_covers(ap_response: bool = False) -> bool:
    """Whether secure mode governs the current request: it is on, and this is a GET or HEAD for ActivityPub (by its
    Accept header, by an ActivityPub-only route, or, after the fact, by an ActivityPub answer) on no exempt route."""
    if request.method not in ('GET', 'HEAD'):
        return False
    if not (ap_response or is_activitypub_request() or _path_matches(request.path, SIGNED_FETCH_AP_ONLY_PREFIXES)):
        return False
    site = getattr(g, 'site', None)
    if site is None or not site.require_signed_fetch:
        return False
    return not signed_fetch_exempt(request.path)


def signed_fetch_refusal():
    """The 401 secure mode answers an ActivityPub GET or HEAD that no actor stored here validly signed, else None.

    Never fetches: a signer this instance has not seen is refused until it is learned of some other way. A user,
    community or feed actor document (MINIMAL_ACTOR_ENDPOINTS) is not refused but flagged, and its route serves the
    minimal form (actor_document)."""
    if not _secure_mode_covers() or signed_requester_actor() is not None:
        return None
    if request.endpoint in MINIMAL_ACTOR_ENDPOINTS:
        request.environ[_MINIMAL_ACTOR_FLAG] = True   # answered by the route, in minimal form (actor_document)
        return None
    response = make_response('', 401)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['WWW-Authenticate'] = 'Signature'
    return response


def actor_document(actor_data: dict) -> dict:
    """The actor document to serve: `actor_data` itself, or, when secure mode let an unsigned (or unknown-signer)
    fetch through to an actor route, only its MINIMAL_ACTOR_FIELDS and its shared inbox."""
    if not request.environ.get(_MINIMAL_ACTOR_FLAG):
        return actor_data
    minimal = {field: actor_data[field] for field in MINIMAL_ACTOR_FIELDS if field in actor_data}
    shared_inbox = (actor_data.get('endpoints') or {}).get('sharedInbox')
    if shared_inbox:
        minimal['endpoints'] = {'sharedInbox': shared_inbox}
    return minimal


def secure_mode_cache_headers(response):
    """Under secure mode an ActivityPub answer was earned by a signature, so no shared cache may hand it on: a proxy
    cache answers before before_request runs, and an unsigned requester would get the signer's copy. The answer is
    made private (the signer's own max-age stays) and varies on the signature. A no-store answer is left alone.

    A minimal actor document (actor_document) is the same for every unsigned requester and holds only public key
    material, so it keeps the route's public cache policy: varying on the signature keeps it from ever answering a
    signed request, and the signer's full document is private."""
    if response.cache_control.no_store or not _secure_mode_covers(response.mimetype in AP_MEDIA_TYPES):
        return response
    if request.environ.get(_MINIMAL_ACTOR_FLAG):
        response.vary.add('Signature')
        return response
    response.cache_control.public = False
    response.cache_control.s_maxage = None
    response.cache_control.private = True
    response.vary.add('Signature')
    return response
