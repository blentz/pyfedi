"""Calls to a connected service, and what their answers mean (spec §8).

Every call goes through the shared pinned client (R162) with PieFed's user agent. An answer becomes a Transient
(429, 5xx, a network failure: worth trying again), an AuthFailed (401, 403: the member must connect again) or a
Permanent (any other 4xx). The service's own error text is kept, trimmed, for the author; a token never is, since
none of these calls puts one in a URL.

An answer may come compressed (gzip or deflate, the two PieFed offers), and is inflated in steps whose output is
capped, so a small answer that would inflate to a huge one (a decompression bomb) is refused once its inflated
bytes reach the cap, with no more than the cap held. Raw bytes are capped too.
"""
import zlib
from zlib import decompressobj

import httpx

from app import httpx_client
from app.connections import AuthFailed, Permanent, Transient
from app.pinned_http import fetch_deadline
from app.utils import user_agent

TIMEOUT = 30               # seconds, per read
DEADLINE = 30              # seconds for a whole call
MAX_BYTES = 1_000_000      # an answer larger than this is refused: what PieFed reads back is small JSON
ACCEPT_ENCODING = 'gzip, deflate'   # what zlib inflates with a bounded output; not br, whose binding cannot bound it


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get('Retry-After', '')
    return int(value) if value.isdigit() else None


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f'HTTP {response.status_code}'
    if isinstance(body, dict):
        for key in ('message', 'error_description', 'error'):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get('message')
            if isinstance(value, str) and value:
                return f'HTTP {response.status_code}: {value}'
    return f'HTTP {response.status_code}'


def check(response: httpx.Response) -> httpx.Response:
    """The response, when it succeeded; otherwise the failure it means."""
    status = response.status_code
    if status < 400:
        return response
    if status == 429 or status >= 500:
        raise Transient(_message(response), _retry_after(response))
    if status in (401, 403):
        raise AuthFailed(_message(response))
    raise Permanent(_message(response))


def _too_large(max_bytes: int) -> Permanent:
    return Permanent(f'The answer was larger than {max_bytes} bytes.')


class _BoundedInflater:
    """One gzip or deflate stream, inflated into `out` but never past `max_bytes`: each step may make at most the
    room left plus one byte, and the input zlib holds back for lack of room is fed again, so memory holds at most
    the cap however far the stream would inflate."""

    def __init__(self, encoding: str, max_bytes: int):
        self.encoding, self.max_bytes = encoding, max_bytes
        self.decoder = decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
        self.out = bytearray()
        self.fed = False

    def _deflate_decoder(self, first: bytes):
        """`deflate` is meant to be zlib-wrapped, but some servers send it raw: the wrapper's two header bytes say."""
        wrapped = len(first) >= 2 and first[0] & 0x0F == 8 and (first[0] << 8 | first[1]) % 31 == 0
        return decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.fed = True
        if self.decoder is None:
            self.decoder = self._deflate_decoder(data)
        try:
            while data:
                self.out += self.decoder.decompress(data, self.max_bytes - len(self.out) + 1)
                if len(self.out) > self.max_bytes:
                    raise _too_large(self.max_bytes)
                data = self.decoder.unconsumed_tail
        except zlib.error:
            raise Permanent('The answer could not be decoded.') from None

    def finish(self) -> bytes:
        if not self.fed:   # no body at all, as a 204 has, whatever its header says
            return b''
        if not self.decoder.eof:
            raise Permanent('The answer ended part way through.')
        return bytes(self.out)


def fetch(method: str, url: str, max_bytes: int = MAX_BYTES, seconds: float = DEADLINE, **kwargs) -> httpx.Response:
    """The response, read whole but never past `max_bytes` (Permanent beyond) nor past `seconds` of wall-clock time
    (fetch_deadline, enforced by the pinned transport: a server that trickles is cut off). Not yet checked.

    `max_bytes` caps both what arrives and what it inflates to (gzip or deflate, inflated by _BoundedInflater). Any
    other encoding, or more than one (`gzip, gzip` multiplies a bomb), is refused before the body is read."""
    headers = {'User-Agent': user_agent(), **kwargs.pop('headers', {}), 'Accept-Encoding': ACCEPT_ENCODING}
    try:
        with fetch_deadline(seconds), httpx_client.stream(method, url, headers=headers,
                                                          timeout=kwargs.pop('timeout', TIMEOUT), **kwargs) as response:
            encoding = response.headers.get('Content-Encoding', '').strip().lower()
            if encoding in ('', 'identity'):
                inflater = None
            elif encoding in ('gzip', 'x-gzip', 'deflate'):
                inflater = _BoundedInflater('deflate' if encoding == 'deflate' else 'gzip', max_bytes)
            else:
                raise Permanent(f'The answer came encoded as {encoding[:40]}, which PieFed did not ask for.')
            raw, received = bytearray(), 0
            for chunk in response.iter_raw():
                received += len(chunk)
                if received > max_bytes:
                    raise _too_large(max_bytes)
                if inflater is None:
                    raw += chunk
                else:
                    inflater.feed(chunk)
    except httpx.TransportError as failure:
        raise Transient(f'Could not reach the service ({type(failure).__name__}).') from None
    content = bytes(raw) if inflater is None else inflater.finish()
    headers = [(name, value) for name, value in response.headers.items()
               if name.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')]
    return httpx.Response(response.status_code, headers=headers, content=content, request=response.request)


def call(method: str, url: str, **kwargs) -> httpx.Response:
    return check(fetch(method, url, **kwargs))
