"""Web Push (owner ruling 2026-11-09: push notifications, a clearnet-only feature): a message encrypted for one
subscription (RFC 8291's aes128gcm, RFC 8188) and signed by this server's VAPID key (RFC 8292), sent to the push
service the app chose. Built on `cryptography` and PyJWT, which PieFed already has, and sent through PieFed's own
outbound checks, so a subscription's endpoint can never reach an address the server's own fetches may not.

The VAPID key pair is made once and kept in the settings table (`vapid_private_key`); its public half is what
/api/v2/instance and every subscription answer as `server_key`."""
import base64
import os
import struct
import time
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

import app as app_pkg   # its pinned outbound client (app/pinned_http.py)
from app.pinned_http import fetch_deadline
from app.storefront import domain_for_host
from app.utils import get_setting, is_invalid_get_request_uri, set_setting

RECORD_SIZE = 4096
SETTING = 'vapid_private_key'
TTL_SECONDS = 2 * 24 * 3600
TIMEOUT_SECONDS = 10


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869): HMAC-SHA-256 of the input keying material, keyed by the salt."""
    mac = hmac.HMAC(salt, hashes.SHA256())
    mac.update(ikm)
    return mac.finalize()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    return HKDFExpand(hashes.SHA256(), length, info).derive(prk)


def _point(key) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(plaintext: bytes, ua_public: str, auth_secret: str, as_private=None, salt: bytes | None = None) -> bytes:
    """`plaintext` encrypted for the subscription whose keys are `ua_public` (p256dh) and `auth_secret`, both
    base64url: the aes128gcm body, header and one record (RFC 8291 section 3.4; RFC 8188 section 2). `as_private`
    and `salt` are fresh each message; a caller passes them only to reproduce a known answer."""
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(ua_public))
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt if salt is not None else os.urandom(16)
    as_public = _point(as_private.public_key())
    shared = as_private.exchange(ec.ECDH(), ua_key)
    key_info = b'WebPush: info\x00' + _point(ua_key) + as_public
    ikm = _expand(_extract(unb64(auth_secret), shared), key_info, 32)
    prk = _extract(salt, ikm)
    cek = _expand(prk, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _expand(prk, b'Content-Encoding: nonce\x00', 12)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b'\x02', None)   # \x02: the last record's delimiter
    return salt + struct.pack('!IB', RECORD_SIZE, len(as_public)) + as_public + ciphertext


def vapid_key():
    """This server's VAPID private key, made and kept the first time it is asked for."""
    stored = get_setting(SETTING)
    if stored:
        return serialization.load_pem_private_key(stored.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    set_setting(SETTING, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()).decode())
    return key


def server_key() -> str:
    """The VAPID public key an app subscribes with (base64url, an uncompressed P-256 point)."""
    return b64(_point(vapid_key().public_key()))


def vapid_header(endpoint: str, subject: str) -> str:
    """The Authorization header for a message to `endpoint` (RFC 8292): a JWT for the push service's origin,
    signed by this server's key, and the key."""
    parts = urlsplit(endpoint)
    claims = {'aud': f'{parts.scheme}://{parts.netloc}', 'exp': int(time.time()) + 12 * 3600, 'sub': subject}
    token = jwt.encode(claims, vapid_key(), algorithm='ES256')
    return f'vapid t={token}, k={server_key()}'


def acceptable_endpoint(endpoint) -> bool:
    """Whether a push subscription may send to `endpoint`: https, not an onion, not one of our own domains, and a
    public address as every outbound fetch of the server's must be (is_invalid_get_request_uri)."""
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or not host or host.endswith('.onion') or domain_for_host(host) is not None:
        return False
    return not is_invalid_get_request_uri(endpoint)


def send(endpoint: str, p256dh: str, auth: str, payload: bytes, subject: str) -> int:
    """Encrypt `payload` for the subscription and POST it to its push service; the status it answered (404 and 410:
    the subscription is gone). An endpoint that is no longer acceptable (its name now resolving somewhere private) is
    not sent to, answered 410 so the caller forgets it."""
    if not acceptable_endpoint(endpoint):
        return 410
    # the pinned client (R162): it connects to the address the guard checked, so a name that answers differently the
    # second time (DNS rebinding) reaches nothing the check refused; and the whole exchange is held to a deadline
    with fetch_deadline(TIMEOUT_SECONDS):
        response = app_pkg.httpx_client.post(
            endpoint, content=encrypt(payload, p256dh, auth), timeout=TIMEOUT_SECONDS, follow_redirects=False,
            headers={'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
                     'TTL': str(TTL_SECONDS), 'Urgency': 'normal', 'Authorization': vapid_header(endpoint, subject)})
    return response.status_code
