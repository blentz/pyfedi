"""RFC 9421 HTTP Message Signatures and RFC 9530 Content-Digest: the subset the fediverse uses (S5).

Signing covers "@method" "@target-uri" "@authority", plus "content-digest" when there is a body, with
rsa-v1_5-sha256. Verification also accepts rsa-pss-sha512 and ed25519, any covered header field, and "@path".
Nothing here touches Flask: callers pass the method, the target URI, the headers and the body.
"""
import base64
import hashlib
import re
import time
from collections.abc import Mapping
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from app.activitypub.signature_errors import KeyMismatchError, UnusableKeyError, VerificationError, \
    VerificationFormatError

CREATED_MAX_AGE_SECONDS = 3600     # matches HttpSignature.precheck's Date window
CREATED_MAX_SKEW_SECONDS = 300
SIGN_LABEL = 'sig1'
SIGN_ALGORITHM = 'rsa-v1_5-sha256'
DIGESTS = {'sha-256': hashlib.sha256, 'sha-512': hashlib.sha512}

_KEY = re.compile(r'[a-z*][a-z0-9_\-.*]*')
_INTEGER = re.compile(r'-?[0-9]{1,15}')
_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*")


class _Parser:
    """RFC 8941 structured fields, the subset these headers use: a Dictionary whose members are Items or Inner
    Lists, with String, Byte Sequence, Integer and Token bare items. Anything else is a format error."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _peek(self) -> str:
        return self.text[self.pos:self.pos + 1]

    def _fail(self, what: str):
        raise VerificationFormatError(f'Malformed structured field: {what} at {self.pos}')

    def _match(self, pattern, what: str) -> str:
        found = pattern.match(self.text, self.pos)
        if not found:
            self._fail(what)
        self.pos = found.end()
        return found.group()

    def _skip(self, chars: str = ' '):
        while self._peek() and self._peek() in chars:
            self.pos += 1

    def dictionary(self) -> dict:
        members = {}
        self._skip()
        while self.pos < len(self.text):
            key = self._match(_KEY, 'key')
            if self._peek() != '=':
                self._fail('expected "="')
            self.pos += 1
            start = self.pos
            value, params = self._inner_list() if self._peek() == '(' else self._item()
            members[key] = (value, params, self.text[start:self.pos])
            self._skip(' \t')
            if self.pos == len(self.text):
                break
            if self._peek() != ',':
                self._fail('expected ","')
            self.pos += 1
            self._skip(' \t')
            if self.pos == len(self.text):
                self._fail('trailing ","')
        return members

    def _inner_list(self):
        self.pos += 1   # '('
        items = []
        while True:
            self._skip()
            if self._peek() == ')':
                self.pos += 1
                return items, self._params()
            items.append(self._item())
            if self._peek() not in (' ', ')'):
                self._fail('expected " " or ")"')

    def _item(self):
        return self._bare(), self._params()

    def _params(self) -> dict:
        params = {}
        while self._peek() == ';':
            self.pos += 1
            self._skip()
            key = self._match(_KEY, 'parameter key')
            params[key] = True
            if self._peek() == '=':
                self.pos += 1
                params[key] = self._bare()
        return params

    def _bare(self):
        char = self._peek()
        if char == '"':
            return self._string()
        if char == ':':
            return self._bytes()
        if char == '-' or char.isdigit():
            return int(self._match(_INTEGER, 'integer'))
        return self._match(_TOKEN, 'item')

    def _string(self) -> str:
        self.pos += 1
        out = []
        while True:
            char = self._peek()
            if not char:
                self._fail('unterminated string')
            self.pos += 1
            if char == '\\':
                escaped = self._peek()
                if escaped not in ('"', '\\'):
                    self._fail('bad escape')
                out.append(escaped)
                self.pos += 1
            elif char == '"':
                return ''.join(out)
            elif not ' ' <= char <= '~':
                self._fail('non-printable character')
            else:
                out.append(char)

    def _bytes(self) -> bytes:
        end = self.text.find(':', self.pos + 1)
        if end < 0:
            self._fail('unterminated byte sequence')
        encoded = self.text[self.pos + 1:end]
        self.pos = end + 1
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError:   # binascii.Error, or a plain ValueError for non-ASCII input
            self._fail('bad base64')


def parse_dictionary(text: str) -> dict:
    """{key: (value, params, raw)} for an RFC 8941 Dictionary. `raw` is the member's text after '=' exactly as
    received, which is what "@signature-params" must reproduce."""
    return _Parser(text).dictionary()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """The value of header `name` (lowercase), matched case-insensitively whatever mapping the caller passes."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _split(url: str):
    """(parts, port) for `url`; the target URI may come from the sender's Host header, so a malformed one is a
    format error, not a crash."""
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:
        raise VerificationFormatError('Malformed target URI')


def _authority(url: str) -> str:
    parts, port = _split(url)
    host = (parts.hostname or '').lower()
    if ':' in host:
        host = f'[{host}]'   # an IPv6 literal keeps its brackets
    if port is not None and (parts.scheme, port) not in (('https', 443), ('http', 80)):
        host = f'{host}:{port}'
    return host


def _component_value(name: str, method: str, url: str, headers: Mapping[str, str]) -> str:
    if name == '@method':
        return method   # as sent on the wire, untransformed (RFC 9421 section 2.2.1)
    if name == '@target-uri':
        return url
    if name == '@authority':
        return _authority(url)
    if name == '@path':
        return _split(url)[0].path or '/'
    if name.startswith('@'):
        raise VerificationFormatError(f'Unsupported derived component {name}')
    value = _header(headers, name)
    if value is None:
        raise VerificationError(f'Covered header {name} is missing')
    return value.strip(' \t')


def signature_base(covered: list[str], params: str, method: str, url: str, headers: Mapping[str, str]) -> str:
    """The RFC 9421 section 2.5 signature base: one line per covered component, then "@signature-params"."""
    lines = [f'"{name}": {_component_value(name, method, url, headers)}' for name in covered]
    lines.append(f'"@signature-params": {params}')
    return '\n'.join(lines)


def normalise_covered(covered) -> frozenset[str]:
    """`covered`, counting "@authority" with "@path" as "@target-uri" (the target URI is the two joined)."""
    names = set(covered)
    if {'@authority', '@path'} <= names:
        names.add('@target-uri')
    return frozenset(names)


def content_digest(body: bytes) -> str:
    return 'sha-256=:' + base64.b64encode(hashlib.sha256(body).digest()).decode('ascii') + ':'


def check_content_digest(headers: Mapping[str, str], body: bytes) -> None:
    """Raise unless every supported digest in the Content-Digest header matches `body`. An absent header passes."""
    value = _header(headers, 'content-digest')
    if value is None:
        return
    checked = False
    for algorithm, (expected, _params, _raw) in parse_dictionary(value).items():
        hasher = DIGESTS.get(algorithm)
        if hasher is None:
            continue
        if expected != hasher(body).digest():
            raise VerificationFormatError('Content-Digest is incorrect')
        checked = True
    if not checked:
        raise VerificationFormatError('Content-Digest has no supported algorithm')


def _covered(value) -> list[str]:
    if not isinstance(value, list):
        raise VerificationFormatError('Signature-Input member is not an inner list')
    names = []
    for item, params in value:
        if not isinstance(item, str):
            raise VerificationFormatError('Covered component is not a string')
        if params:
            raise VerificationFormatError(f'Component parameters are not supported: {item}')
        if item in names:
            raise VerificationFormatError(f'Duplicate component {item}')
        names.append(item)
    return names


def _check_created(params: dict, now: int) -> None:
    created = params.get('created')
    if not isinstance(created, int):
        raise VerificationFormatError('Signature-Input has no created time')
    if created < now - CREATED_MAX_AGE_SECONDS:
        raise VerificationError('Signature is too old')
    if created > now + CREATED_MAX_SKEW_SECONDS:
        raise VerificationError('Signature is from the future')


def _load_public_key(public_key_pem: str):
    try:
        return serialization.load_pem_public_key(public_key_pem.encode('ascii'))
    except (ValueError, TypeError, AttributeError, UnsupportedAlgorithm):
        raise UnusableKeyError('Public key is not a usable PEM')


def _pkcs1v15_sha256():
    return padding.PKCS1v15(), hashes.SHA256()


def _pss_sha512():
    return padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64), hashes.SHA512()


# With no alg parameter an RSA key is tried as the fediverse's usual rsa-v1_5-sha256 first, then rsa-pss-sha512
RSA_SCHEMES = {'rsa-v1_5-sha256': (_pkcs1v15_sha256,), 'rsa-pss-sha512': (_pss_sha512,),
               None: (_pkcs1v15_sha256, _pss_sha512)}


def _verify_signature(key, alg, signature: bytes, data: bytes) -> None:
    if isinstance(key, ed25519.Ed25519PublicKey):
        if alg not in (None, 'ed25519'):
            raise UnusableKeyError(f'Algorithm {alg} does not match an ed25519 key')
        try:
            key.verify(signature, data)
            return
        except InvalidSignature:
            raise KeyMismatchError('Signature mismatch')
    if not isinstance(key, rsa.RSAPublicKey):
        raise UnusableKeyError('Unsupported key type')
    if alg not in RSA_SCHEMES:
        raise VerificationFormatError(f'Unsupported algorithm {alg}')
    for scheme in RSA_SCHEMES[alg]:
        try:
            key.verify(signature, data, *scheme())
            return
        except InvalidSignature:
            continue
    raise KeyMismatchError('Signature mismatch')


def verify(method: str, url: str, headers: Mapping[str, str], body: bytes, public_key_pem: str,
           required: frozenset[str] = frozenset(), *, now: int | None = None,
           key_id: str | None = None) -> frozenset[str]:
    """Verify the request's RFC 9421 signature against `public_key_pem`; return the components it covers.

    With several labels the first that verifies wins; if none does, the last label's error is raised. Every
    component in `required` must be covered (after normalise_covered). Content-Digest, when present, must match
    `body` whether or not it is covered. With `key_id` (the keyId the caller resolved `public_key_pem` from) only
    a label whose keyid parameter is exactly that can verify, so a label naming another key is never accepted."""
    inputs = parse_dictionary(_header(headers, 'signature-input') or '')
    if not inputs:
        raise VerificationFormatError('No Signature-Input header present')
    signatures = parse_dictionary(_header(headers, 'signature') or '')
    check_content_digest(headers, body)
    key = _load_public_key(public_key_pem)
    now = int(time.time()) if now is None else now
    error = None
    for label, (value, params, raw) in inputs.items():
        try:
            if key_id is not None and params.get('keyid') != key_id:
                raise VerificationError(f'Label {label} names another key')
            covered = _covered(value)
            signature = signatures.get(label, (None,))[0]
            if not isinstance(signature, bytes):
                raise VerificationFormatError(f'No signature for label {label}')
            _check_created(params, now)
            base = signature_base(covered, raw, method, url, headers)
            _verify_signature(key, params.get('alg'), signature, base.encode('utf-8'))
            missing = required - normalise_covered(covered)
            if missing:
                raise VerificationError('unsigned required component: ' + ' '.join(sorted(missing)))
            return frozenset(covered)
        except VerificationError as e:
            error = e
    raise error


def check_fresh(headers: Mapping[str, str], *, now: int | None = None) -> None:
    """Raise VerificationFormatError unless every Signature-Input label has a created time inside the window: the
    RFC 9421 counterpart of HttpSignature.precheck's Date check, made before any key is looked up."""
    inputs = parse_dictionary(_header(headers, 'signature-input') or '')
    if not inputs:
        raise VerificationFormatError('No Signature-Input header present')
    now = int(time.time()) if now is None else now
    for _value, params, _raw in inputs.values():
        try:
            _check_created(params, now)
        except VerificationError as e:
            raise VerificationFormatError(str(e)) from None


def parse_key_id(headers: Mapping[str, str]) -> str | None:
    """The keyid of the first Signature-Input label, or None when there is none to read."""
    try:
        inputs = parse_dictionary(_header(headers, 'signature-input') or '')
    except VerificationFormatError:
        return None
    first = next(iter(inputs.values()), None)
    key_id = first[1].get('keyid') if first else None
    return key_id if isinstance(key_id, str) else None


def _sf_string(value: str) -> str:
    if not all(' ' <= char <= '~' for char in value):
        raise ValueError('A structured-field string must be printable ASCII')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


@lru_cache(maxsize=256)
def _private_key(private_key_pem: str):
    return serialization.load_pem_private_key(private_key_pem.encode('ascii'), password=None)


def sign(method: str, url: str, body: bytes | None, private_key_pem: str, key_id: str,
         created: int | None = None) -> dict[str, str]:
    """The Signature-Input, Signature and (with a body) Content-Digest headers for a request. The method is
    signed uppercased, as httpx sends it."""
    method = method.upper()
    covered = ['@method', '@target-uri', '@authority']
    headers = {}
    if body is not None:
        headers['Content-Digest'] = content_digest(body)
        covered.append('content-digest')
    created = int(time.time()) if created is None else created
    params = ('(' + ' '.join(f'"{name}"' for name in covered) + ')'
              f';created={created};keyid={_sf_string(key_id)};alg="{SIGN_ALGORITHM}"')
    base = signature_base(covered, params, method, url, headers)
    signature = _private_key(private_key_pem).sign(base.encode('utf-8'), *_pkcs1v15_sha256())
    headers['Signature-Input'] = f'{SIGN_LABEL}={params}'
    headers['Signature'] = f'{SIGN_LABEL}=:{base64.b64encode(signature).decode("ascii")}:'
    return headers
