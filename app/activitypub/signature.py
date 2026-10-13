# code in this file was originally from Takahe https://github.com/jointakahe/takahe
#
# Copyright 2022 Andrew Godwin
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation and/or
# other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Literal, TypedDict, cast
from urllib.parse import urlparse

import pendulum
import httpx
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dateutil import parser
from flask import Request, current_app
from furl import furl
from pyld import jsonld
from sqlalchemy import text

import app as app_pkg
from app import celery, db
from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.ld_contexts import LDCoverageError, PYFEDI_CONTEXT_TERMS, check_ld_coverage, ld_document_loader, \
    strip_empty_values
from app.activitypub.signature_errors import KeyMismatchError, KeyRefetchable, UnusableKeyError, VerificationError, \
    VerificationFormatError  # VerificationError and VerificationFormatError are re-exported
from app.constants import AP_FETCH_ACCEPT, DATETIME_MS_FORMAT
from app.discovery.instance_answers import record_delivery_refusal
from app.relays.refusals import record_relay_refusal
from app.models import utcnow, ActivityPubLog, Community, Instance, CommunityMember, Site, User, SendQueue, public_key_pem
from app.utils import RequestNotSendable, ResponseTooLarge, _capped_get, _capped_request, get_request_capped, \
    get_task_session, is_invalid_get_request_uri
import app.activitypub.util as activitypub_util


def http_date(epoch_seconds=None):
    if epoch_seconds is None:
        epoch_seconds = pendulum.now('UTC').timestamp()
    return formatdate(epoch_seconds, usegmt=True)  # takahe uses formatdate so let's try that
    # formatted_date = pendulum.from_timestamp(epoch_seconds).format('ddd, DD MMM YYYY HH:mm:ss ZZ', locale='en_US')     # mastodon does not like this
    # return formatted_date


def format_ld_date(value: datetime) -> str:
    # We chop the timestamp to be identical to the timestamps returned by
    # Mastodon's API, because some clients like Toot! (for iOS) are especially
    # picky about timestamp parsing.
    return f"{value.strftime(DATETIME_MS_FORMAT)[:-4]}Z"


def parse_http_date(http_date_str):
    return parsedate_to_datetime(http_date_str)


def parse_ld_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    return parser.isoparse(value).replace(microsecond=0)


def send_post_request(uri: str, body: dict | None, private_key: str, key_id: str,
                      content_type: str = "application/activity+json",
                      method: Literal["get", "post"] = "post", timeout: int = 10, retries: int = 0, new_task=True):
    if current_app.debug or new_task is False:
        return post_request(uri=uri, body=body, private_key=private_key, key_id=key_id, content_type=content_type,
                            method=method, timeout=timeout, retries=retries)
    else:
        post_request.delay(uri=uri, body=body, private_key=private_key, key_id=key_id, content_type=content_type,
                           method=method, timeout=timeout, retries=retries)
        return True


@celery.task
def post_request(uri: str, body: dict | None, private_key: str, key_id: str,
                 content_type: str = "application/activity+json",
                 method: Literal["get", "post"] = "post", timeout: int = 10, retries: int = 0):
    session = get_task_session()
    try:
        if not isinstance(body, dict) or not body.get('id'):
            # Nothing a peer could accept: log it as a failure rather than raise with no log row (D768)
            session.add(ActivityPubLog(direction='out', activity_type='', result='failure',
                                       activity_json=json.dumps(body),
                                       exception_message=f'no activity id, not sent: {uri}'))
            session.commit()
            return
        if '@context' not in body:  # add a default json-ld context if necessary
            body['@context'] = default_context()
        type = body['type'] if 'type' in body else ''
        log = ActivityPubLog(direction='out', activity_type=type, result='processing', activity_id=body['id'], exception_message='')
        log.activity_json = json.dumps(body)
        session.add(log)

        http_status_code = None
        transport_failure = False

        if uri is None or uri == '':
            log.result = 'failure'
            log.exception_message = 'empty uri'
        else:
            try:
                result = double_knock(uri, lambda signature_format: HttpSignature.signed_request(
                    uri, body, private_key, key_id, content_type, method, timeout, signature_format=signature_format))
                http_status_code = result.status_code
                if result.status_code != 200 and result.status_code != 202 and result.status_code != 204:
                    log.result = 'failure'
                    log.exception_message = f'{result.status_code}: {result.text:.100}' + ' - '
                    if 'DOCTYPE html' in result.text:
                        log.result = 'ignored'
                        log.exception_message = f'{result.status_code}: HTML instead of JSON response'
                        # log.activity_json += result.text[]
                    elif 'community_has_no_followers' in result.text:
                        fix_local_community_membership(uri, private_key, session)
                    elif result.status_code == 400 and 'person_is_banned_from_site' in result.text:
                        # D1379. The trigger is a SUBSTRING of the peer's body, so
                        # the body need not be JSON at all -- an HTML error page
                        # carrying that phrase reached `.json()` and raised. The
                        # raise was caught below, but `result.close()` is after this
                        # branch, so the response leaked each time a peer chose to
                        # answer this way.
                        try:
                            banned_json = result.json()
                        except Exception:
                            banned_json = None
                        if banned_json is not None:
                            activitypub_util.process_banned_message(banned_json, furl(uri).host, session)
                    elif result.status_code == 410 or result.status_code == 418:    # When an instance returns 410, never send to it again.
                        existing_instance = session.query(Instance).filter_by(domain=furl(uri).host).first()
                        if existing_instance:
                            existing_instance.gone_forever = True
                        session.query(SendQueue).filter_by(destination_domain=furl(uri).host).delete()
                    else:
                        if current_app.debug:
                            current_app.logger.error(f'Response code for post attempt to {uri} was ' +
                                                     str(result.status_code) + ' ' + result.text[:50])
                log.exception_message += uri
                if result.status_code == 202:
                    log.exception_message += ' 202'
                if result.status_code == 204:
                    log.exception_message += ' 204'
                result.close()
            except Exception as e:
                log.result = 'failure'
                log.exception_message = 'could not send:' + str(e)
                if current_app.debug:
                    current_app.logger.error(f'Exception while sending post to {uri}')
                http_status_code = 404
                # couldn't connect, DNS, timeout etc - signed_request re-raises all of these as httpx.HTTPError. Not a
                # request httpx refused to send (RequestNotSendable): a retry would be refused the same way.
                transport_failure = isinstance(e, httpx.HTTPError) and not isinstance(e, RequestNotSendable)
        if log.result == 'processing':
            log.result = 'success'
        session.commit()

        if log.result != 'failure':
            return
        else:
            if not transport_failure:   # transport failures report 404 internally; only a peer's answer counts
                record_delivery_refusal(session, body, http_status_code)
                record_relay_refusal(session, body, http_status_code)
            if transport_failure or (http_status_code is not None and (http_status_code == 429 or http_status_code >= 500)):
                if content_type == "application/activity+json":
                    # Calculate retry delay with exponential backoff. 1 min, 2 mins, 4 mins, 8 mins, up to 4h
                    backoff = 60 * (2 ** retries)
                    backoff = min(backoff, 15360)
                    session.add(SendQueue(destination=uri, destination_domain=furl(uri).host, actor=key_id,
                                             private_key=private_key, payload=json.dumps(body), retries=retries,
                                             retry_reason=log.exception_message,
                                             send_after=utcnow() + timedelta(seconds=backoff)))
                    session.commit()

            return
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def signed_get_request(uri: str, private_key: str, key_id: str, content_type: str = "application/activity+json",
                       method: Literal["get", "post"] = "get", timeout: int = 10, ) -> httpx.Response:
    """A GET of `uri` signed as `key_id`, in the format double_knock picks (the other once on a 401). Each attempt
    is signed only, then fetched through _capped_get like get_request: at most FETCH_MAX_BYTES of body within
    FETCH_MAX_SECONDS (else ResponseTooLarge), no redirects, `timeout` per read, no sleep and no retry. It used to
    read through HttpSignature.signed_request's own uncapped GET, so a hostile origin could pin a worker with a huge
    or slow body. `content_type` and `method` are IGNORED, kept only so the six callers' signature still holds:
    a GET has no body for content_type to describe, and the request is always signed and sent as a GET, since every
    caller asks for one and _capped_get only GETs. Raises ValueError for a URI the signer refuses, httpx.HTTPError
    on transport failure."""
    def send(signature_format: str) -> httpx.Response:
        # send_via_async: sign only; the GET itself goes through _capped_get's cap and deadline
        _uri, headers, _body = HttpSignature.signed_request(uri, None, private_key, key_id, content_type, method='get',
                                                            timeout=timeout, send_via_async=True,
                                                            signature_format=signature_format)
        return _capped_get(uri, headers=headers, timeout=timeout)

    return double_knock(uri, send)


def signed_get_capped(uri: str, private_key: str, key_id: str, max_bytes: int,
                      max_seconds: float = 15) -> tuple[int, bytes | None]:
    """A GET of `uri` signed as `key_id`, sent through get_request_capped: its SSRF guards, no redirects, at most
    max_bytes of body and a wall-clock deadline of max_seconds per attempt. Returns (status, body) as
    get_request_capped does (body None when over the cap or late; max_bytes=0 reads the status only).

    signed_get_request is capped too (_capped_get, an httpx.Response or ResponseTooLarge); this is the variant for
    callers that want (status, body) and a cap of their own, with an over-cap or late body as None rather than an
    exception. The signature format alternates as in double_knock: the format the instance is known to accept first,
    the other exactly once on a 401 (remembered on a 2xx) -- at most two signed requests, so at most
    2 * max_seconds. Raises httpx.HTTPError on transport failure."""
    domain = activitypub_util.host_of(uri)

    def send(signature_format: str) -> tuple[int, bytes | None]:
        # send_via_async: sign only; the GET itself goes through get_request_capped's cap and deadline
        _uri, headers, _body = HttpSignature.signed_request(uri, None, private_key, key_id, method='get',
                                                            send_via_async=True, signature_format=signature_format)
        return get_request_capped(uri, max_bytes, headers=headers, max_seconds=max_seconds)

    signature_format = _instance_signature_format(domain)
    status, body = send(signature_format)
    if status != 401:
        return status, body
    alternate = _other_signature_format(signature_format)
    status, body = send(alternate)
    if 200 <= status < 300:
        _remember_signature_format(domain, alternate)
    return status, body


# pyld's default loader fetches every @context over the network, uncached and
# with no timeout, at a URL the sender chose; serve bundled copies instead.
jsonld.set_document_loader(ld_document_loader)


class RsaKeys:
    @classmethod
    def generate_keypair(cls) -> tuple[str, str]:
        """
        Generates a new RSA keypair
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        private_key_serialized = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        public_key_serialized = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        return private_key_serialized, public_key_serialized


def parse_signature_header(signature: str | None) -> dict[str, str]:
    """The Signature header's parts, keyed by lowercased name -- the one parser for it (D768), read by
    parse_signature for verification and by signature_part for the (created)/(expires) pseudo-headers."""
    parts = {}
    for part in (signature or '').split(','):
        # maxsplit=1, or a base64 value loses its padding and a keyId loses its
        # query string
        name, equals, value = part.partition('=')
        if not equals:
            continue
        parts[name.strip().lower()] = value.strip().strip('"')
    return parts


# Get a piece of the signature string, '' when it is absent
def signature_part(signature, key):
    return parse_signature_header(signature).get(key, '')


# The headers an inbound signature must cover (S2.1). A POST without a signed digest lets the body be swapped;
# without (request-target) or host a signature can be replayed against another endpoint or instance.
POST_REQUIRED = frozenset({'(request-target)', 'host', 'date', 'digest'})
GET_REQUIRED = frozenset({'(request-target)', 'host', 'date'})


def _check_required(signed: set[str], required: frozenset[str], key_id: str | None, enforce: bool = False) -> None:
    """Refuse (or, with SIG_REQUIRED_HEADERS_ENFORCE off and `enforce` False, log) a valid signature that leaves a
    required header uncovered."""
    missing = required - signed
    if not missing:
        return
    names = ' '.join(sorted(missing))
    if enforce or current_app.config.get('SIG_REQUIRED_HEADERS_ENFORCE'):
        raise VerificationError(f'unsigned required header: {names}')
    current_app.logger.warning(f'Signature from {activitypub_util.host_of(key_id or "")} does not sign: {names}')


# The RFC 9421 components that stand in for each cavage header a signature must cover. "date" has none: an RFC 9421
# signature's `created` parameter is checked at verification instead.
CAVAGE_TO_RFC9421 = {'(request-target)': {'@method', '@target-uri'}, 'host': {'@target-uri'}, 'date': set(),
                     'digest': {'content-digest'}}


def _rfc9421_required(required: frozenset[str]) -> frozenset[str]:
    return frozenset().union(*(CAVAGE_TO_RFC9421[name] for name in required))


def _target_uri(request) -> str:
    """The URI an RFC 9421 signer must have signed: SERVER_URL's scheme and authority (https in HTTP_PROTOCOL
    'mixed'), then the path and query. Never the Host header (R-HOSTPIN): a signature made for another instance,
    replayed here with that instance's Host, must not verify."""
    path = request.full_path if request.query_string else request.path
    server = urlparse(current_app.config['SERVER_URL'])
    return f"{server.scheme}://{server.netloc}{path}"


def _authority(value: str) -> str:
    """A Host value compared as an authority: lowercased, a default port (:443, :80) dropped."""
    value = value.strip().lower()
    for port in (':443', ':80'):
        if value.endswith(port):
            return value[:-len(port)]
    return value


def _check_signed_host(request) -> None:
    """Refuse a cavage signature whose signed Host is not this instance (R-HOSTPIN): otherwise a request signed
    for another instance verifies here when replayed with that instance's Host."""
    host = request.headers.get('Host', '')
    if _authority(host) != _authority(current_app.config['SERVER_NAME']):
        raise VerificationError(f'Signed host {host} is not this instance')


def is_rfc9421(request) -> bool:
    """True when the request carries an RFC 9421 signature (Signature-Input), which then decides verification,
    freshness and the keyId; otherwise it is a cavage request."""
    return 'signature-input' in request.headers


# A signer whose signature fails gets at most one key refetch in this many seconds (S2.3)
KEY_REFETCH_SECONDS = 300


def request_key_id(request) -> str | None:
    """The keyId the request's signature names (RFC 9421 or cavage), or None. Every keyId read goes through here."""
    if is_rfc9421(request):
        return rfc9421.parse_key_id(request.headers)
    return parse_signature_header(request.headers.get('Signature')).get('keyid') or None


# The HTTP signature formats this instance can send (S5). A peer's preference is learned by double-knocking.
SIGNATURE_FORMATS = ('cavage', 'rfc9421')


def _other_signature_format(signature_format: str) -> str:
    return 'rfc9421' if signature_format == 'cavage' else 'cavage'


def _instance_signature_format(domain: str) -> str:
    session = get_task_session()
    try:
        stored = session.query(Instance.signature_format).filter_by(domain=domain).scalar()
    finally:
        session.close()
    return stored if stored in SIGNATURE_FORMATS else 'cavage'


def _remember_signature_format(domain: str, signature_format: str) -> None:
    session = get_task_session()
    try:
        session.query(Instance).filter_by(domain=domain).update({'signature_format': signature_format})
        session.commit()
    finally:
        session.close()


def double_knock(uri: str, send) -> httpx.Response:
    """Send in the signature format `uri`'s instance is known to accept; on a 401 try the other format exactly
    once, remembering it when that succeeds. `send(signature_format)` makes one signed request. The format is read
    and written in short sessions of their own, so no database connection is held across the HTTP request."""
    domain = activitypub_util.host_of(uri)
    signature_format = _instance_signature_format(domain)
    response = send(signature_format)
    if response.status_code != 401:
        return response
    response.close()
    alternate = _other_signature_format(signature_format)
    response = send(alternate)
    if 200 <= response.status_code < 300:
        _remember_signature_format(domain, alternate)
    return response


def _fetch_actor_key(actor_url: str) -> str | None:
    """The PEM `actor_url` publishes now, fetched with a signed GET as the instance actor; None on any failure.
    Only the key is taken: the daily profile refresh does the rest."""
    site = db.session.get(Site, 1)
    if site is None or not site.private_key:
        return None
    try:
        response = signed_get_request(actor_url, site.private_key,
                                      f"{current_app.config['SERVER_URL']}/actor#main-key")
    except (httpx.HTTPError, ValueError):
        return None
    try:
        if response.status_code != 200:
            return None
        return public_key_pem(response.json())
    except ValueError:   # JSONDecodeError
        return None
    finally:
        response.close()


def verify_refreshing_key(request, stored_key, required: frozenset[str] = frozenset(), *, owner_url,
                          rate_key: str, fetch_key, store_key) -> None:
    """Verify `request` against `stored_key`; on failure, refetch the signer's key once and verify again.

    The refetch happens only when the keyId names a key on `owner_url`'s host (an unauthenticated request must not
    make this instance fetch from a host of its choosing), `owner_url` is set (None for a local actor), and
    `rate_key` was not used in the last KEY_REFETCH_SECONDS, and the failure is one a new key could cure (no stored
    key, an unusable one, or a signature that does not verify under it: KeyRefetchable). `fetch_key()` returns the
    published PEM or None; `store_key(pem)` persists a new one. Raises VerificationError when the request does not
    verify."""
    try:
        if not stored_key:
            raise KeyMismatchError('No public key is stored for the signer')
        HttpSignature.verify_request(request, stored_key, required)
        return
    except (ValueError, TypeError, UnsupportedAlgorithm):
        first_error = UnusableKeyError('The stored public key is not a usable PEM')
    except VerificationError as e:
        first_error = e
    if not isinstance(first_error, KeyRefetchable):
        raise first_error
    key_host = activitypub_util.host_of(request_key_id(request) or '')
    if not owner_url or not key_host or key_host != activitypub_util.host_of(owner_url):
        raise first_error
    if not app_pkg.redis_client.set(rate_key, 1, nx=True, ex=KEY_REFETCH_SECONDS):
        raise first_error
    fetched = fetch_key()
    if not fetched or fetched == stored_key:
        raise first_error
    store_key(fetched)
    try:
        HttpSignature.verify_request(request, fetched, required)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise VerificationError('The refetched public key is not a usable PEM')


def verify_with_key_refresh(request, actor, required: frozenset[str] = frozenset()) -> None:
    """verify_refreshing_key for a User, Community or Feed: its stored key, a signed refetch of its actor
    document, and the key stored back on the row. A local actor, and one whose instance is gone for good or not
    online, is never refetched."""
    def store(pem):
        actor.public_key = pem
        db.session.commit()

    instance = actor.instance if actor.instance_id else None
    refetchable = not actor.is_local() and not (instance is not None and not instance.online())
    verify_refreshing_key(request, actor.public_key, required,
                          owner_url=actor.ap_profile_id if refetchable else None,
                          rate_key=f'sig-refetch:{actor.ap_profile_id}',
                          fetch_key=lambda: _fetch_actor_key(actor.ap_profile_id), store_key=store)


class HttpSignature:
    """
    Allows for calculation and verification of HTTP signatures
    """

    # Class-level cache for deserialized private keys
    _private_key_cache = {}  # key: hash of private_key, value: RSAPrivateKey instance
    _public_key_cache = {}  # key: hash of public_key, value: RSAPublicKey instance
    _cache_max_size = 1000  # Limit to prevent unbounded growth

    @classmethod
    def _get_private_key_instance(cls, private_key: str) -> rsa.RSAPrivateKey:
        """
        Get a deserialized private key instance from cache, or load and cache it.
        Uses SHA-256 hash of the private key as the cache key to avoid storing large strings.
        """
        # Use hash of private key as cache key (shorter than full PEM string)
        key_hash = hashlib.sha256(private_key.encode()).hexdigest()

        if key_hash not in cls._private_key_cache:
            # Simple size limit: clear oldest half when full
            if len(cls._private_key_cache) >= cls._cache_max_size:
                # Remove first half
                keys_to_remove = list(cls._private_key_cache.keys())[:cls._cache_max_size // 2]
                for k in keys_to_remove:
                    del cls._private_key_cache[k]

            # Deserialize and cache
            cls._private_key_cache[key_hash] = cast(
                rsa.RSAPrivateKey,
                serialization.load_pem_private_key(
                    private_key.encode("ascii"),
                    password=None,
                ),
            )

        return cls._private_key_cache[key_hash]

    @classmethod
    def _get_public_key_instance(cls, public_key: str) -> rsa.RSAPublicKey:
        """
        Get a deserialized public key instance from cache, or load and cache it.
        Uses SHA-256 hash of the public key as the cache key to avoid storing large strings.
        """
        # Use hash of public key as cache key (shorter than full PEM string)
        key_hash = hashlib.sha256(public_key.encode()).hexdigest()

        if key_hash not in cls._public_key_cache:
            # Simple size limit: clear oldest half when full
            if len(cls._public_key_cache) >= cls._cache_max_size:
                # Remove first half
                keys_to_remove = list(cls._public_key_cache.keys())[:cls._cache_max_size // 2]
                for k in keys_to_remove:
                    del cls._public_key_cache[k]

            # Deserialize and cache
            cls._public_key_cache[key_hash] = cast(
                rsa.RSAPublicKey,
                serialization.load_pem_public_key(public_key.encode("ascii")),
            )

        return cls._public_key_cache[key_hash]

    @classmethod
    def calculate_digest(cls, data, algorithm="sha-256") -> str:
        """
        Calculates the digest header value for a given HTTP body
        """
        if algorithm == "sha-256":
            digest = hashes.Hash(hashes.SHA256())
            digest.update(data)
            return "SHA-256=" + base64.b64encode(digest.finalize()).decode("ascii")
        else:
            raise ValueError(f"Unknown digest algorithm {algorithm}")

    @classmethod
    def headers_from_request(cls, request: Request, header_names: list[str], with_query: bool = True) -> str:
        """
        Creates the to-be-signed header payload from a Flask request. (request-target) is '<method> <path>?<query>'
        when the request has a query (draft-cavage section 2.3); `with_query=False` builds the legacy path-only form.
        """
        headers = {}
        for header_name in header_names:
            if header_name == "(request-target)":
                target = request.full_path if with_query and request.query_string else request.path
                value = f"{request.method.lower()} {target}"
            elif header_name == '(created)':
                value = signature_part(request.headers.get('Signature'), 'created')
            elif header_name == '(expires)':
                value = signature_part(request.headers.get('Signature'), 'expires')
            elif header_name == "content-type":
                value = request.headers.get("Content-Type", "")
            elif header_name == "content-length":
                value = request.headers.get("Content-Length", "")
            else:
                value = request.headers.get(header_name.replace("-", "_").upper(), "")
            headers[header_name] = value
        return "\n".join(f"{name.lower()}: {value}" for name, value in headers.items())

    @classmethod
    def parse_signature(cls, signature: str) -> "HttpSignatureDetails":
        bits = parse_signature_header(signature)
        try:
            signature_details: HttpSignatureDetails = {
                "headers": bits["headers"].split(),
                "signature": base64.b64decode(bits["signature"]),
                "algorithm": bits["algorithm"],
                "keyid": bits["keyid"],
            }
        except KeyError as e:
            key_names = " ".join(bits.keys())
            raise VerificationError(
                f"Missing item from details (have: {key_names}, error: {e})"
            )
        return signature_details

    @classmethod
    def compile_signature(cls, details: "HttpSignatureDetails") -> str:
        value = f'keyId="{details["keyid"]}",headers="'
        value += " ".join(h.lower() for h in details["headers"])
        value += '",signature="'
        value += base64.b64encode(details["signature"]).decode("ascii")
        value += f'",algorithm="{details["algorithm"]}"'
        return value

    @classmethod
    def verify_signature(
            cls,
            signature: bytes,
            cleartext: str,
            public_key: str,
    ):
        public_key_instance = cls._get_public_key_instance(public_key)
        try:
            public_key_instance.verify(
                signature,
                cleartext.encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            raise KeyMismatchError("Signature mismatch")

    @classmethod
    def precheck(cls, request: Request):
        """
        Checks digest and freshness before signature verification.
        Digest or Content-Digest is required, and each present must match the body. A cavage request's Date must
        be within 1 hour; an RFC 9421 request needs no Date, its created time must be in rfc9421's window instead.
        """
        has_digest = "digest" in request.headers
        has_content_digest = "content-digest" in request.headers
        if not has_digest and not has_content_digest:
            raise VerificationFormatError("No digest header present")
        if has_digest and request.headers["digest"] != HttpSignature.calculate_digest(request.data):
            raise VerificationFormatError("Digest is incorrect")
        if has_content_digest:
            rfc9421.check_content_digest(request.headers, request.data)

        if is_rfc9421(request):
            rfc9421.check_fresh(request.headers)
        else:
            cls.check_date(request)

    @classmethod
    def check_date(cls, request: Request):
        """
        The Date header must be present, a valid HTTP date and within 1 hour of now. Without this a captured
        signed request could be replayed forever.
        """
        if "date" not in request.headers:
            raise VerificationFormatError("No date header present")
        try:
            header_date = parse_http_date(request.headers["date"])
        except (TypeError, ValueError):
            # a peer's malformed date is the caller's 400, not a traceback
            raise VerificationFormatError("Date is not a valid HTTP date")
        if header_date.tzinfo is None:
            # RFC 7231 requires the timezone; a peer that omits it means UTC
            header_date = header_date.replace(tzinfo=timezone.utc)
        if abs((datetime.now(timezone.utc) - header_date).total_seconds()) > 3600:
            raise VerificationFormatError("Date is too far away")

    @classmethod
    def verify_request(cls, request: Request, public_key, required: frozenset[str] = frozenset(), *,
                       enforce: bool = False):
        """
        Verifies that the request has a valid signature for its body, and that the signature covers every
        header in `required` (see _check_required; `enforce` refuses a gap whatever SIG_REQUIRED_HEADERS_ENFORCE
        says). An RFC 9421 request (see is_rfc9421) is verified as one, its created time checked, and only a
        label naming the keyId request_key_id reads (the one the caller resolved `public_key` from) can verify.
        """
        if is_rfc9421(request):
            key_id = request_key_id(request)
            if key_id is None:
                raise VerificationFormatError("Signature-Input names no keyid")
            covered = rfc9421.verify(request.method, _target_uri(request), request.headers, request.get_data(),
                                     public_key, key_id=key_id)
            _check_required(set(rfc9421.normalise_covered(covered)), _rfc9421_required(required), key_id, enforce)
            return True

        # Get the signature details
        if "signature" not in request.headers:
            raise VerificationFormatError("No signature header present")
        signature_details = cls.parse_signature(request.headers["signature"])

        # Reject unknown algorithms
        # hs2019 is used by some libraries to obfuscate the real algorithm per the spec
        # https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-12
        if (
                signature_details["algorithm"] != "rsa-sha256"
                and signature_details["algorithm"] != "hs2019"
        ):
            raise VerificationFormatError("Unknown signature algorithm")
        # Create the signature payload
        headers_string = cls.headers_from_request(request, signature_details["headers"])
        signed = {name.lower() for name in signature_details["headers"]}
        try:
            cls.verify_signature(signature_details["signature"], headers_string, public_key)
        except VerificationError:
            # Earlier PieFed and Mastodon signed (request-target) without the query; Mastodon still accepts that
            # form, and so does this, so such a signature covers no more than it ever did
            if not request.query_string or "(request-target)" not in signed:
                raise
            cls.verify_signature(signature_details["signature"],
                                 cls.headers_from_request(request, signature_details["headers"], with_query=False),
                                 public_key)
        if "host" in signed:
            _check_signed_host(request)
        _check_required(signed, required, signature_details["keyid"], enforce)
        return True

    @classmethod
    def signed_request(
            cls,
            uri: str,
            body: dict | None,
            private_key: str,
            key_id: str,
            content_type: str = "application/activity+json",
            method: Literal["get", "post"] = "post",
            timeout: int = 5,
            send_via_async=False,
            signature_format: str = 'cavage'
    ):
        """
        Performs a request to the given path, with a document, signed
        as an identity.
        """
        if "://" not in uri:
            raise ValueError("URI does not contain a scheme")
        if is_invalid_get_request_uri(uri):
            raise ValueError("URI is invalid")
        if signature_format not in SIGNATURE_FORMATS:
            raise ValueError(f"Unknown signature format {signature_format}")
        # Create the core header field set
        uri_parts = urlparse(uri)
        date_string = http_date()
        target = f"{uri_parts.path}?{uri_parts.query}" if uri_parts.query else uri_parts.path   # draft-cavage 2.3
        headers = {
            "(request-target)": f"{method} {target}",
            "Host": uri_parts.hostname,
            "Date": date_string,
        }
        # If we have a body, add a digest and content type
        if body is not None:
            if '@context' not in body:  # add a default json-ld context if necessary
                body['@context'] = default_context()
            body_bytes = json.dumps(body).encode("utf8")
            if signature_format == 'cavage':  # RFC 9421 sends Content-Digest instead (P5)
                headers["Digest"] = cls.calculate_digest(body_bytes)
            headers["Content-Type"] = content_type
        else:
            body_bytes = b""
        # GET requests get implicit accept headers added. A cavage signature covers every header set here, so the
        # Accept sent is the Accept signed.
        if method == "get":
            headers["Accept"] = AP_FETCH_ACCEPT
        if signature_format == 'rfc9421':
            del headers["(request-target)"]
            # Sign the target URI exactly as it goes on the wire: httpx never sends a fragment
            headers.update(rfc9421.sign(method, uri_parts._replace(fragment='').geturl(),
                                        body_bytes if body is not None else None, private_key, key_id))
        else:
            # Sign the headers
            signed_string = "\n".join(
                f"{name.lower()}: {value}" for name, value in headers.items()
            )

            private_key_instance = cls._get_private_key_instance(private_key)

            # RSA signature generation
            signature = private_key_instance.sign(
                signed_string.encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            headers["Signature"] = cls.compile_signature(
                {
                    "keyid": key_id,
                    "headers": list(headers.keys()),
                    "signature": signature,
                    "algorithm": "rsa-sha256",
                }
            )
            # Send the request with all those headers except the pseudo one
            del headers["(request-target)"]

        headers["User-Agent"] = f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'
        if send_via_async:  # 'async' sending involves passing the data through to the piefed_notifs service. See announce_activity_to_followers().
            return uri, headers, body_bytes
        else:
            try:
                # Through _capped_request: the answer is read under the 1 MiB cap and the fetch deadline. For a
                # POST the status is kept and an answer over either is dropped rather than raised -- the activity
                # was delivered, and raising would have post_request send it again. A GET gets ResponseTooLarge
                # instead, not an empty document to parse. It never follows a redirect, which
                # is what `follow_redirects=method == "GET"` here always meant: every caller passes "post" (or
                # "get") lowercase, and no caller sends a GET through this path any more -- signed GETs only sign
                # (send_via_async) and fetch through _capped_get.
                response = _capped_request(
                    method,
                    uri,
                    headers=headers,
                    # `content=`, not `data=`. httpx takes raw bytes through
                    # `content=` and has deprecated passing them as `data=`,
                    # which it keeps only for form encoding -- so every
                    # outbound federation request raised
                    # `DeprecationWarning: Use 'content=<...>' to upload raw
                    # bytes/text content.` and will stop working when httpx
                    # drops it. Measured identical on the wire: same body,
                    # same Content-Type, same Content-Length, which is what
                    # the HTTP signature over the digest requires.
                    content=body_bytes,
                    timeout=timeout,
                    keep_status=method.lower() != 'get',
                )
            except (RequestNotSendable, ResponseTooLarge) as ex:
                # Kept as their own type, as neither is worth a retry: httpx refused the request itself, which it
                # will do again (post_request logs it and does not queue it to be sent again), or a GET's answer was
                # over the cap or the deadline. Neither carries a request, so the url is the one asked for.
                raise type(ex)(f"HTTP Exception for {uri} - {ex}") from None
            except httpx.HTTPError as ex:
                # Convert to a more generic error we handle
                raise httpx.HTTPError(f"HTTP Exception for {ex.request.url} - {ex}") from None

            # D1322. There was a branch here raising ValueError for a 4xx POST.
            # It had never run: `method` is `Literal["get", "post"]` and every
            # caller passes it lowercase, so `method == "POST"` is always False.
            #
            # DO NOT "FIX" IT TO `method.lower() == "post"`. `post_request`
            # above is the caller, and it reads 4xx RESPONSES rather than
            # catching an exception: `community_has_no_followers` calls
            # `fix_local_community_membership`, `person_is_banned_from_site`
            # calls `process_banned_message`, and 410/418 marks the instance
            # gone forever and empties its SendQueue. A raise here lands in that
            # function's `except Exception`, which records `http_status_code =
            # 404` and does none of them -- so waking this branch up would stop
            # this instance ever noticing that a peer is gone.
            return response


class HttpSignatureDetails(TypedDict):
    algorithm: str
    headers: list[str]
    signature: bytes
    keyid: str


LD_SIGNATURE_EXPIRY_SKEW = timedelta(minutes=5)   # clock difference allowed past a signature's `expires`
SEC_EXPIRATION = "https://w3id.org/security#expiration"


class LDSignature:
    """
    Creates and verifies signatures of JSON-LD documents
    """

    @classmethod
    def verify_signature(cls, document: dict, public_key: str) -> None:
        """
        Verifies a document. Raises VerificationError (VerificationFormatError for a malformed signature section
        or a document the signature does not cover) unless the signature is genuine and covers every key and
        value of the JSON pyfedi will read (see check_ld_coverage). On success the document is stripped, in place,
        of null values and empty lists, which the signature cannot cover. An LD signature authenticates only who
        sent what kind of activity about which object: the inbox hands an LD-accepted activity to
        process_inbox_request with ld_signed=True, which takes content from the origin instead.
        """
        # Strip out the signature from the incoming document
        signature = document.pop("signature", None)
        if not isinstance(signature, dict) or not all(
                isinstance(signature.get(key), str) for key in ("type", "creator", "created", "signatureValue")):
            raise VerificationFormatError("Invalid signature section")
        if signature["type"].lower() != "rsasignature2017":
            raise VerificationFormatError("Unknown signature type")
        # Create the options document: like Mastodon, every key of the signature section except these three,
        # so an `expires` the signer included is hashed too
        options = {key: value for key, value in signature.items() if key not in ("type", "id", "signatureValue")}
        options["@context"] = "https://w3id.org/security/v1"
        # An expiry the signer set is honoured, so a relay cannot replay an old signed body; none is still accepted.
        # Read from the expanded options, not the `expires` key: every alias (`expiration`, ...) hashes the same
        try:
            expanded = jsonld.expand(options)
        except jsonld.JsonLdError:
            raise VerificationFormatError("Invalid signature section")
        for node in expanded:
            for value in node.get(SEC_EXPIRATION, []):
                try:
                    expires = parser.isoparse(value["@value"])
                    if expires.tzinfo is not None:
                        expires = expires.astimezone(timezone.utc).replace(tzinfo=None)
                    expired = expires + LD_SIGNATURE_EXPIRY_SKEW < utcnow()
                except (KeyError, TypeError, ValueError, OverflowError):
                    # OverflowError: a time at the edge of the datetime range (hour 24 of 9999-12-31, year 1
                    # or 9999 moved to UTC, the skew added past 9999)
                    raise VerificationFormatError("Invalid signature expiry")
                if expired:
                    raise VerificationError("Signature expired")
        try:
            check_ld_coverage(document)
        except LDCoverageError as e:
            raise VerificationFormatError(f"Signature does not cover the document: {e}") from e
        try:
            signature_value = base64.b64decode(signature["signatureValue"], validate=True)
        except binascii.Error as e:
            raise VerificationFormatError("Invalid signatureValue") from e
        # Get the normalised hash of each document
        final_hash = cls.normalized_hash(options) + cls.normalized_hash(document)
        # Verify the signature
        try:
            public_key_instance = serialization.load_pem_public_key(public_key.encode("ascii"))
        except (AttributeError, ValueError, UnsupportedAlgorithm) as e:
            raise VerificationError("Unusable public key") from e
        if not isinstance(public_key_instance, rsa.RSAPublicKey):
            raise VerificationError("Unusable public key")
        try:
            public_key_instance.verify(
                signature_value,
                final_hash,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            raise VerificationError("Signature mismatch")
        # What produced no triple was not signed: pyfedi must not read it (R-LDDOWNGRADE)
        strip_empty_values(document)

    @classmethod
    def create_signature(
            cls, document: dict, private_key: str, key_id: str
    ) -> dict[str, str]:
        """
        Creates the signature for a document
        """
        # Create the options document
        options: dict[str, str] = {
            "@context": "https://w3id.org/security/v1",
            "creator": key_id,
            "created": format_ld_date(utcnow()),
        }
        # Get the normalised hash of each document
        final_hash = cls.normalized_hash(options) + cls.normalized_hash(document)
        # Create the signature
        private_key_instance: rsa.RSAPrivateKey = cast(
            rsa.RSAPrivateKey,
            serialization.load_pem_private_key(
                private_key.encode("ascii"),
                password=None,
            ),
        )
        signature = base64.b64encode(
            private_key_instance.sign(
                final_hash,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        )
        # Add it to the options document along with other bits
        options["signatureValue"] = signature.decode("ascii")
        options["type"] = "RsaSignature2017"
        return options

    @classmethod
    def normalized_hash(cls, document) -> bytes:
        """
        Takes a JSON-LD document and create a hash of its URDNA2015 form,
        in the same way that Mastodon does internally.

        Reference: https://socialhub.activitypub.rocks/t/making-sense-of-rsasignature2017/347
        """
        try:
            norm_form = jsonld.normalize(
                document,
                {"algorithm": "URDNA2015", "format": "application/n-quads"},
            )
        except jsonld.JsonLdError as e:
            # pyld wraps the real reason several levels down
            cause = e
            while isinstance(cause.__cause__, jsonld.JsonLdError):
                cause = cause.__cause__
            raise VerificationFormatError(f"Could not normalize document: {cause.args[0]}") from e
        digest = hashes.Hash(hashes.SHA256())
        digest.update(norm_form.encode("utf8"))
        return digest.finalize().hex().encode("ascii")


def default_context():
    context = [
        "https://www.w3.org/ns/activitystreams",
        "https://w3id.org/security/v1",
    ]
    if current_app.config['FULL_AP_CONTEXT']:
        context.append(copy.deepcopy(PYFEDI_CONTEXT_TERMS))
    return context


def fix_local_community_membership(uri: str, private_key: str, session):
    community = session.query(Community).filter_by(private_key=private_key).first()
    parsed_url = urlparse(uri)
    instance_domain = parsed_url.netloc
    instance = session.query(Instance).filter_by(domain=instance_domain).first()

    if community and instance:
        followers = session.query(CommunityMember).filter_by(community_id=community.id). \
            join(User, User.id == CommunityMember.user_id). \
            filter(User.instance_id == instance.id)
        for f in followers:
            session.execute(
                text('DELETE FROM "community_member" WHERE user_id = :user_id AND community_id = :community_id'),
                {'user_id': f.user_id, 'community_id': community.id})
