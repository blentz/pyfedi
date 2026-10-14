"""Property checks for the signature and transport surface (tier T1).

Every inbound federation request is read by this code before anything about
its sender is known: the cavage Signature header and its dates, RFC 9421's
Signature-Input/Signature/Content-Digest structured fields, an RsaSignature2017
LD signature with the JSON-LD context checks in front of it, the capped reader
every outbound GET's answer goes through, and the Accept header. The contract
each check asserts is the one the function documents: only the refusal types
it names escape, what it claims to round-trip does, a signature it accepts was
genuinely made over what it covers by the key it was given, and nothing reaches
the network.

Signing happens inside the checks, with one RSA key generated at import, so a
campaign can reach the ACCEPT side of every verifier: the check signs what the
fuzzer built and then mutates or relabels it. Without that, an unsigned fuzz
input never verifies and "never accepts a forgery" would be asserted against
inputs that could not have been accepted anyway.

No network, and only the bundled JSON-LD loader. app/main/routes.py's /test
route swaps pyld's global loader for a network one; it is never imported here,
and _network_forbidden() below fails a check whose call so much as resolves a
host name, and asserts the loader in force is ld_document_loader.

Multi-argument inputs are decoded with _fields(): each field but the last is a
two-byte big-endian length and that many bytes, the last field is the rest.
encode_fields() is its inverse, for writing seeds. No atheris here: the corpus
replay (tests/test_utils_fuzz_corpus.py) runs these checks in the ordinary
suite.
"""

import base64
import hashlib
import json
import socket
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pyld import jsonld

from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.ld_contexts import (BUNDLED_LD_CONTEXTS, LDCoverageError, check_ld_coverage,
                                         ld_document_loader, strip_empty_values)
from app.activitypub.signature import (LD_MAX_NESTING_DEPTH, HttpSignature, LDSignature, parse_http_date,
                                       parse_ld_date, parse_signature_header, signature_part)
from app.activitypub.signature_errors import KeyMismatchError, VerificationError, VerificationFormatError
from app.utils import ResponseTooLarge, UnreadableEncoding, _read_capped_body, accepted_media_ranges
from tests.fuzz.targets._target import Target, hold_to_budget, machine_slowdown

# One key for every check, made once: generating RSA keys per input would cost the campaign most of its budget.
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = _PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')
KEY_ID = 'https://fuzz.example/u/signer#main-key'
# rfc9421.verify's clock, fixed so a seed's created time stays inside the window forever.
NOW = 1_760_000_000

AS_CONTEXT = 'https://www.w3.org/ns/activitystreams'
SEC_CONTEXT = 'https://w3id.org/security/v1'


# ---------------------------------------------------------------------------
# Input decoding and shared helpers
# ---------------------------------------------------------------------------

def _fields(data: bytes, count: int) -> list[bytes]:
    """`count` byte strings out of `data`: length-prefixed (two bytes, big-endian) for all but the last, which
    takes the rest. A short input yields empty trailing fields rather than failing, so every input is usable."""
    out = []
    for _ in range(count - 1):
        length = int.from_bytes(data[:2], 'big')
        out.append(data[2:2 + length])
        data = data[2 + length:]
    out.append(data)
    return out


def encode_fields(*fields: bytes) -> bytes:
    """The inverse of _fields, for writing seeds."""
    return b''.join(len(field).to_bytes(2, 'big') + field for field in fields[:-1]) + fields[-1]


def _text(data: bytes) -> str:
    return data.decode('utf-8', errors='replace')


def _json_or_text(data: bytes):
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return _text(data)


def _sign(data: bytes) -> bytes:
    return _PRIVATE_KEY.sign(data, padding.PKCS1v15(), hashes.SHA256())


@contextmanager
def _network_forbidden():
    """Every way a socket gets an address or a connection fails for the duration, and the attempt is recorded.

    Recorded as well as refused because the refusal alone could be swallowed: pyld wraps a loader's exception
    in a JsonLdError, which LDSignature turns into a VerificationFormatError the check accepts. The caller
    asserts the record is empty. Also asserts pyld's global loader is still the bundled one, which is what
    app/main/routes.py's /test route would replace."""
    assert jsonld.get_document_loader() is ld_document_loader, (
        'pyld global document loader is not app.activitypub.ld_contexts.ld_document_loader')
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError('network access from a fuzz check')

    saved = (socket.getaddrinfo, socket.create_connection, socket.socket.connect, socket.socket.connect_ex)
    socket.getaddrinfo = socket.create_connection = refuse
    socket.socket.connect = socket.socket.connect_ex = refuse
    try:
        yield attempts
    finally:
        socket.getaddrinfo, socket.create_connection, socket.socket.connect, socket.socket.connect_ex = saved


# ---------------------------------------------------------------------------
# Cavage HTTP signatures
# ---------------------------------------------------------------------------

_KEYID_CHARS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/._#?&=-~@')
_TOKEN_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789()-')


def check_signature_header(data: bytes) -> None:
    """parse_signature_header, signature_part and HttpSignature.parse_signature.

    parse_signature_header never raises and gives str -> str; signature_part agrees with it for every name and
    gives '' for an absent one. parse_signature either refuses with a VerificationError or returns exactly the
    parsed parts it claims to read. With the first byte odd, the rest instead builds a details dict of
    characters PieFed itself emits, and compile_signature -> parse_signature must give it back unchanged.
    """
    if data[:1] and data[0] & 1:
        key_id, header_names, signature, algorithm = _fields(data[1:], 4)
        details = {
            'keyid': ''.join(c for c in _text(key_id) if c in _KEYID_CHARS) or KEY_ID,
            'headers': [name for name in ''.join(c if c in _TOKEN_CHARS else ' '
                                                 for c in _text(header_names).lower()).split()],
            'signature': signature,
            'algorithm': ''.join(c for c in _text(algorithm) if c in _TOKEN_CHARS) or 'rsa-sha256',
        }
        compiled = HttpSignature.compile_signature(details)
        assert HttpSignature.parse_signature(compiled) == details, (
            f'compile_signature -> parse_signature changed the details: {compiled!r}')
        return

    text = _text(data[1:])
    parts = parse_signature_header(text)
    assert isinstance(parts, dict)
    for name, value in parts.items():
        assert isinstance(name, str) and isinstance(value, str)
        assert signature_part(text, name) == value, f'signature_part disagrees about {name!r}'
    if '\x00absent' not in parts:
        assert signature_part(text, '\x00absent') == ''

    try:
        details = HttpSignature.parse_signature(text)
    except VerificationError:
        return
    assert details['keyid'] == parts['keyid']
    assert details['algorithm'] == parts['algorithm']
    assert details['headers'] == parts['headers'].split()
    assert isinstance(details['signature'], bytes)


def check_http_signature_verify(data: bytes) -> None:
    """HttpSignature.verify_signature with the module's key: only a VerificationError refuses, a signature the
    key did not make is never accepted, and (first byte odd) one it did make over the cleartext is accepted,
    while the same signature over a changed cleartext is a KeyMismatchError."""
    control, signature, cleartext_bytes = _fields(data, 3)
    cleartext = _text(cleartext_bytes)
    genuine = bool(control[:1] and control[0] & 1)
    if genuine:
        signature = _sign(cleartext.encode('utf-8'))
    try:
        HttpSignature.verify_signature(signature, cleartext, PUBLIC_PEM)
    except VerificationError:
        assert not (genuine and cleartext.isascii()), 'a genuine signature over ASCII cleartext was refused'
        return
    assert genuine, 'a signature the key never made was accepted'
    try:
        HttpSignature.verify_signature(signature, cleartext + '\n', PUBLIC_PEM)
    except KeyMismatchError:
        return
    raise AssertionError('a signature was accepted over cleartext it was not made over')


def check_http_ld_dates(data: bytes) -> None:
    """parse_http_date and parse_ld_date.

    parse_http_date returns a datetime or raises what HttpSignature.check_date catches (TypeError, ValueError),
    and check_date's arithmetic on the result does not raise. parse_ld_date gives None for None, else a
    datetime without microseconds or a ValueError."""
    selector, text = data[:1], _text(data[1:])
    if not selector or selector[0] % 3 == 0:
        try:
            parsed = parse_http_date(text)
        except (TypeError, ValueError):
            return
        assert isinstance(parsed, datetime)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        abs((datetime.now(timezone.utc) - parsed).total_seconds())
    elif selector[0] % 3 == 1:
        try:
            parsed = parse_ld_date(text)
        except ValueError:
            return
        assert isinstance(parsed, datetime) and parsed.microsecond == 0
    else:
        assert parse_ld_date(None) is None


# ---------------------------------------------------------------------------
# LD signatures and the JSON-LD context checks
# ---------------------------------------------------------------------------

# LDSignature.verify_signature refuses, before any JSON-LD work, a document or signature section nested deeper
# than LD_MAX_NESTING_DEPTH: URDNA2015 costs about the cube of the depth (a 300-deep signature section took 13 s).
# The check holds it to refusing those well inside this many milliseconds of CPU time, scaled to the machine's load
# as the corpus replay scales a target's budget (hold_to_budget). It used to be wall-clock against a fixed 0.5 s,
# which a loaded machine failed with nothing slower in the code.
_DEEP_REFUSAL_MS = 500


def _depth(node) -> int:
    """How deeply `node` nests lists and objects. Iterative: the inputs of interest are deeper than the stack."""
    deepest, pending = 0, [(node, 1)]
    while pending:
        node, level = pending.pop()
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else None
        if children is not None:
            deepest = max(deepest, level)
            pending.extend((child, level + 1) for child in children)
    return deepest


def _clone(document):
    """A deep copy through the C JSON codec, which, unlike copy.deepcopy, does not recurse on the Python stack."""
    return json.loads(json.dumps(document))


def _is_empty(value) -> bool:
    return value is None or value == [] or value == {}


def _empty_inside(node) -> bool:
    """True if any value or item below `node` (not `node` itself) is null, an empty list or an empty object."""
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    return any(_is_empty(child) or _empty_inside(child) for child in children)


def _contexts(node):
    """Every @context value anywhere in a JSON document. Iterative, like _depth."""
    found, pending = [], [node]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if '@context' in node:
                found.append(node['@context'])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return found


def _ld_document(data: bytes):
    document = _json_or_text(data)
    if not isinstance(document, dict):
        document = {'@context': AS_CONTEXT, 'type': 'Note', 'id': 'https://fuzz.example/n/1', 'content': document}
    return document


def _check_deep_ld_refused(document, clock=time.thread_time, slowdown=machine_slowdown) -> None:
    """Too deep is refused quickly, before normalizing, whatever the signature section holds. `clock` and
    `slowdown` are parameters only so the budget can be tested without a slow refusal or machine."""
    with _network_forbidden() as attempts:
        started = clock()
        try:
            LDSignature.verify_signature(_clone(document), PUBLIC_PEM)
        except VerificationFormatError as refusal:
            raise AssertionError(f'a too-deep document was read as malformed, not unsigned: {refusal!r}')
        except VerificationError:
            pass
        else:
            raise AssertionError('a document nested past LD_MAX_NESTING_DEPTH was accepted')
        elapsed_ms = (clock() - started) * 1000
    assert not attempts, f'LD verification reached for the network: {attempts!r}'
    hold_to_budget('refusing a too-deep document', elapsed_ms, _DEEP_REFUSAL_MS, slowdown)


def check_ld_signature_verify(data: bytes) -> None:
    """LDSignature.verify_signature and normalized_hash, with the module's key and the bundled loader only.

    The first byte picks the mode; the rest is the JSON document (a non-object is wrapped in a Note). Mode odd:
    the check replaces the signature section with a genuine one from the key over the document, keeping any
    other keys the fuzzer put in the section (an `expires`, say) in what it signs. Properties: only a
    VerificationError refuses; nothing touches the network; an unsigned document is never accepted; a
    genuinely signed one with no extra section keys is accepted exactly when check_ld_coverage accepts it;
    an accepted document has lost its signature section and every null and empty value; normalized_hash is
    64 hex digits and deterministic. A document or signature section nested deeper than LD_MAX_NESTING_DEPTH is
    refused, as a plain VerificationError (unsigned, not malformed), within _DEEP_REFUSAL_MS of CPU time scaled to
    the machine's load."""
    genuine = bool(data[:1] and data[0] & 1)
    document = _ld_document(data[1:])
    unsigned_depth = _depth({key: value for key, value in document.items() if key != 'signature'})
    if max(unsigned_depth, _depth(document.get('signature'))) > LD_MAX_NESTING_DEPTH:
        _check_deep_ld_refused(document)
        return
    with _network_forbidden() as attempts:
        try:
            digest = LDSignature.normalized_hash(_clone(document))
        except VerificationFormatError:
            pass
        else:
            assert len(digest) == 64 and all(c in b'0123456789abcdef' for c in digest)
            assert LDSignature.normalized_hash(_clone(document)) == digest, 'normalized_hash is not deterministic'

        extras = {}
        if genuine:
            section = document.pop('signature', None)
            if isinstance(section, dict):
                extras = {key: value for key, value in section.items()
                          if key not in ('type', 'id', 'signatureValue', 'creator', 'created', '@context')}
            options = {'@context': SEC_CONTEXT, 'creator': KEY_ID, 'created': '2026-10-13T00:00:00Z', **extras}
            try:
                final_hash = LDSignature.normalized_hash(_clone(options)) + \
                    LDSignature.normalized_hash(_clone(document))
            except VerificationFormatError:
                genuine = False
                final_hash = b''
            document['signature'] = {
                **{key: value for key, value in options.items() if key != '@context'},
                'type': 'RsaSignature2017',
                'signatureValue': base64.b64encode(_sign(final_hash)).decode('ascii'),
            }
        unsigned = {key: value for key, value in document.items() if key != 'signature'}
        try:
            covered = check_ld_coverage(_clone(unsigned)) is None
        except LDCoverageError:
            covered = False

        verified = _clone(document)
        try:
            LDSignature.verify_signature(verified, PUBLIC_PEM)
        except VerificationError as refusal:
            assert not (genuine and not extras and covered), (
                f'a genuine signature over a covered document was refused: {refusal!r}')
            accepted = False
        else:
            accepted = True
    assert not attempts, f'LD verification reached for the network: {attempts!r}'
    if accepted:
        assert genuine, 'a signature the key never made was accepted'
        assert covered, 'a document check_ld_coverage refuses was accepted'
        assert 'signature' not in verified
        assert not _empty_inside(verified), 'an accepted document still holds a null or empty value'


def _stripped(node):
    """An independent, non-mutating reading of strip_empty_values's contract."""
    if isinstance(node, dict):
        kept = {key: _stripped(value) for key, value in node.items()}
        return {key: value for key, value in kept.items() if not _is_empty(value)}
    if isinstance(node, list):
        return [item for item in (_stripped(item) for item in node) if not _is_empty(item)]
    return node


def check_ld_contexts(data: bytes) -> None:
    """check_ld_coverage, strip_empty_values and ld_document_loader.

    check_ld_coverage refuses only with LDCoverageError, touches no network, and never accepts a document
    with a string @context that is not bundled. ld_document_loader, given the input's text as a URL, serves
    the bundled document for a bundled URL and refuses any other with a JsonLdError, without fetching.

    strip_empty_values is held to its contract on what it is documented to run on, a document
    check_ld_coverage accepted: it leaves no null or empty value, removes nothing else, and is idempotent.
    Its sole caller, LDSignature.verify_signature, never hands it anything else."""
    document = _ld_document(data)
    with _network_forbidden() as attempts:
        try:
            check_ld_coverage(_clone(document))
        except LDCoverageError:
            covered = False
        else:
            covered = True

        url = _text(data)
        try:
            loaded = ld_document_loader(url)
        except jsonld.JsonLdError:
            assert url not in BUNDLED_LD_CONTEXTS, f'the bundled context {url!r} was refused'
        else:
            assert url in BUNDLED_LD_CONTEXTS, f'an unbundled context {url!r} was served'
            assert loaded['document'] is BUNDLED_LD_CONTEXTS[url]
    assert not attempts, f'JSON-LD context handling reached for the network: {attempts!r}'
    if not covered:
        return

    for context in _contexts(document):
        for entry in context if isinstance(context, list) else [context]:
            assert not isinstance(entry, str) or entry in BUNDLED_LD_CONTEXTS, (
                f'check_ld_coverage accepted the unbundled context {entry!r}')

    if _depth(document) > 400:
        return   # the recursive reference readings below need two stack frames a level; production needs one
    stripped = _clone(document)
    strip_empty_values(stripped)
    assert stripped == _stripped(document), 'strip_empty_values kept or lost the wrong values'
    assert not _empty_inside(stripped), 'strip_empty_values left a null or empty value'
    again = _clone(stripped)
    strip_empty_values(again)
    assert again == stripped, 'strip_empty_values is not idempotent'


# ---------------------------------------------------------------------------
# RFC 9421 and RFC 9530
# ---------------------------------------------------------------------------

def _well_formed_raw(key: str, member) -> None:
    value, params, raw = member
    assert isinstance(raw, str)
    assert isinstance(params, dict)
    assert rfc9421.parse_dictionary(f'{key}={raw}') == {key: member}, (
        f'the raw text of member {key!r} does not parse back to the member')


def check_sf_dictionary(data: bytes) -> None:
    """signature_rfc9421.parse_dictionary: only VerificationFormatError refuses, and each member's `raw` is
    the received text after its '=' exactly -- it occurs in the input as `key=raw` and parses back to the very
    member, which is what "@signature-params" relies on."""
    text = _text(data)
    try:
        members = rfc9421.parse_dictionary(text)
    except VerificationFormatError:
        return
    assert isinstance(members, dict)
    for key, member in members.items():
        assert f'{key}={member[2]}' in text, f'raw for {key!r} is not text the input holds'
        _well_formed_raw(key, member)


def _covered_names(value):
    """The covered component names of a Signature-Input member, or None when they are not plain strings."""
    if not isinstance(value, list) or any(not isinstance(item, str) or params for item, params in value):
        return None
    return [item for item, _params in value]


def _independent_digest_ok(header: str, body: bytes) -> bool:
    members = rfc9421.parse_dictionary(header)
    supported = {name: member[0] for name, member in members.items() if name in ('sha-256', 'sha-512')}
    hashers = {'sha-256': hashlib.sha256, 'sha-512': hashlib.sha512}
    return bool(supported) and all(value == hashers[name](body).digest() for name, value in supported.items())


def check_rfc9421_verify(data: bytes) -> None:
    """parse_key_id, check_content_digest, signature_base, normalise_covered and verify, over one request.

    Fields: control, method, url, Signature-Input, Signature, Content-Digest, an extra header's name and value,
    body. Control bit 0 makes the check sign EVERY label's signature base with the module's key -- including
    labels whose keyid names some other key, so the keyid gate is tested against genuine signatures. Bit 1
    sends Content-Digest, bit 2 the extra header.

    Properties: only VerificationError subclasses escape any of them; parse_key_id never raises and agrees
    with the parsed first label; check_content_digest passes only when a supported digest is present and every
    supported one matches the body; normalise_covered adds "@target-uri" exactly for "@authority" + "@path";
    verify(key_id=KEY_ID) accepts only a label whose keyid is KEY_ID, whose signature the key made, and
    returns that label's covered components."""
    control, method, url, sig_input, signature, digest, extra_name, extra_value, body = _fields(data, 9)
    flags = control[0] if control else 0
    headers = {'Signature-Input': _text(sig_input)}
    if flags & 2:
        headers['Content-Digest'] = _text(digest)
    if flags & 4 and _text(extra_name).strip():
        headers[_text(extra_name).strip()] = _text(extra_value)
    method, url = _text(method) or 'POST', _text(url) or 'https://fuzz.example/inbox'

    try:
        inputs = rfc9421.parse_dictionary(headers['Signature-Input'])
    except VerificationFormatError:
        inputs = None

    key_id = rfc9421.parse_key_id(headers)
    assert key_id is None or isinstance(key_id, str)
    if inputs:
        first_keyid = next(iter(inputs.values()))[1].get('keyid')
        assert key_id == (first_keyid if isinstance(first_keyid, str) else None)
    else:
        assert key_id is None

    if 'Content-Digest' in headers:
        try:
            rfc9421.check_content_digest(headers, body)
        except VerificationError:
            pass
        else:
            assert _independent_digest_ok(headers['Content-Digest'], body), (
                'check_content_digest passed a digest that does not match the body')

    signed = {}
    for label, (value, params, raw) in (inputs or {}).items():
        names = _covered_names(value)
        if names is None:
            continue
        normalised = rfc9421.normalise_covered(names)
        assert set(names) <= normalised
        assert ('@target-uri' in normalised) == ('@target-uri' in names or {'@authority', '@path'} <= set(names))
        try:
            base = rfc9421.signature_base(names, raw, method, url, headers)
        except VerificationError:
            continue
        assert base.endswith(f'"@signature-params": {raw}')
        if flags & 1:
            signed[label] = base64.b64encode(_sign(base.encode('utf-8'))).decode('ascii')
    if flags & 1:
        headers['Signature'] = ', '.join(f'{label}=:{value}:' for label, value in signed.items())
    else:
        headers['Signature'] = _text(signature)

    try:
        covered = rfc9421.verify(method, url, headers, body, PUBLIC_PEM, key_id=KEY_ID, now=NOW)
    except VerificationError:
        return
    assert flags & 1, 'a signature the key never made was accepted'
    matching = [label for label, (value, params, _raw) in inputs.items()
                if params.get('keyid') == KEY_ID and label in signed and _covered_names(value) is not None
                and frozenset(_covered_names(value)) == covered]
    assert matching, f'verify accepted, but no label naming {KEY_ID!r} covers {sorted(covered)}'


# ---------------------------------------------------------------------------
# The capped body reader and the Accept header
# ---------------------------------------------------------------------------

_ENCODINGS = ['', 'identity', 'gzip', 'x-gzip', 'deflate', ' GZIP ', 'Deflate', 'br', 'gzip, gzip', 'compress']


class _ChunkedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        yield from self._chunks


def _wire_header(value: bytes) -> bytes:
    """A header value as h11 would hand it over: no CR, LF or NUL."""
    return bytes(b for b in value if b not in b'\r\n\x00')


def _reference_inflate(encoding: str, wire: bytes) -> bytes:
    """What a whole-body zlib read gives, the way _Inflater reads it: gzip, or zlib-wrapped deflate falling back
    to raw deflate."""
    if encoding in ('gzip', 'x-gzip'):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        return decoder.decompress(wire) + decoder.flush()
    try:
        decoder = zlib.decompressobj(zlib.MAX_WBITS)
        return decoder.decompress(wire) + decoder.flush()
    except zlib.error:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        return decoder.decompress(wire) + decoder.flush()


def check_capped_body(data: bytes) -> None:
    """app.utils._read_capped_body (and _Inflater) over a fake byte stream.

    Fields: control (cap high byte, cap low byte, encoding index, chunk size), a raw Content-Encoding used when
    the index is past the list, a raw Content-Length (sent when non-empty), the wire body. Only
    ResponseTooLarge (UnreadableEncoding included) and httpx.DecodingError escape; an encoding outside
    gzip/deflate/identity is always UnreadableEncoding; the body returned is at most the cap; and it is the
    WHOLE decoded body -- a body that would inflate past the cap is refused, never returned cut short."""
    control, raw_encoding, raw_length, wire = _fields(data, 4)
    control = control.ljust(4, b'\x00')
    max_bytes = int.from_bytes(control[:2], 'big')
    if control[2] < len(_ENCODINGS):
        encoding_header = _ENCODINGS[control[2]].encode('ascii')
    else:
        encoding_header = _wire_header(raw_encoding)
    chunk_size = control[3] or 1
    chunks = [wire[i:i + chunk_size] for i in range(0, len(wire), chunk_size)]
    headers = []
    if encoding_header:
        headers.append((b'Content-Encoding', encoding_header))
    if raw_length:
        headers.append((b'Content-Length', _wire_header(raw_length)))
    response = httpx.Response(200, headers=headers, stream=_ChunkedStream(chunks),
                              request=httpx.Request('GET', 'https://fuzz.example/'))
    encoding = response.headers.get('Content-Encoding', '').strip().lower()

    try:
        body = _read_capped_body(response, 'https://fuzz.example/', max_bytes, time.monotonic() + 3600, 3600)
    except UnreadableEncoding:
        return
    except (ResponseTooLarge, httpx.DecodingError):
        assert encoding in ('', 'identity', 'gzip', 'x-gzip', 'deflate')
        return
    assert encoding in ('', 'identity', 'gzip', 'x-gzip', 'deflate'), (
        f'a body in Content-Encoding {encoding!r} was read rather than refused')
    assert isinstance(body, bytes)
    assert len(body) <= max_bytes, f'{len(body)} bytes returned under a cap of {max_bytes}'
    if encoding in ('', 'identity'):
        assert body == wire
    else:
        try:
            expected = _reference_inflate(encoding, wire)
        except zlib.error as error:
            raise AssertionError(f'a corrupt {encoding} body was returned: {error}') from None
        assert body == expected, 'the body returned is not the whole decoded body'


def check_accept_header(data: bytes) -> None:
    """accepted_media_ranges: never raises; every range is a non-empty lowercased media type, a dict of
    lowercased parameter names, and a float q inside [0, 1] (so never NaN)."""
    for media_type, params, quality in accepted_media_ranges(_text(data)):
        assert isinstance(media_type, str) and media_type and media_type == media_type.strip()
        assert isinstance(params, dict)
        for name, value in params.items():
            assert isinstance(name, str) and isinstance(value, str) and name == name.strip()
        assert isinstance(quality, float) and 0.0 <= quality <= 1.0, f'q of {quality!r} is outside [0, 1]'


# None needs an app context: every function here is plain parsing and crypto, and utcnow() reads the clock, not
# the config. None is budgeted: no regex here has a backtracking shape (the RFC 8941 patterns are anchored
# single classes), and URDNA2015's cost is bounded by verify_signature refusing documents nested past
# LD_MAX_NESTING_DEPTH before normalizing (_check_deep_ld_refused holds it to that).
TARGETS = [
    Target(name='signature_header', check=check_signature_header, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='http_signature_verify', check=check_http_signature_verify, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='http_ld_dates', check=check_http_ld_dates, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='ld_signature_verify', check=check_ld_signature_verify, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='ld_contexts', check=check_ld_contexts, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='sf_dictionary', check=check_sf_dictionary, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='rfc9421_verify', check=check_rfc9421_verify, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='capped_body', check=check_capped_body, surface='signatures',
           budget_ms=None, needs_app=False),
    Target(name='accept_header', check=check_accept_header, surface='signatures',
           budget_ms=None, needs_app=False),
]
