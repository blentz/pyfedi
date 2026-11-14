"""Calls to a connected service, and what their answers mean (spec §8).

Every call goes through the shared pinned client (R162) with PieFed's user agent. An answer becomes a Transient
(429, 5xx, a network failure: worth trying again), an AuthFailed (401, 403: the member must connect again) or a
Permanent (any other 4xx). The service's own error text is kept, trimmed, for the author; a token never is, since
none of these calls puts one in a URL.
"""
import httpx

from app import httpx_client
from app.connections import AuthFailed, Permanent, Transient
from app.pinned_http import fetch_deadline
from app.utils import user_agent

TIMEOUT = 30               # seconds, per read
DEADLINE = 30              # seconds for a whole call
MAX_BYTES = 1_000_000      # an answer larger than this is refused: what PieFed reads back is small JSON


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


def fetch(method: str, url: str, max_bytes: int = MAX_BYTES, seconds: float = DEADLINE, **kwargs) -> httpx.Response:
    """The response, read whole but never past `max_bytes` (Permanent beyond) nor past `seconds` of wall-clock time
    (fetch_deadline, enforced by the pinned transport: a server that trickles is cut off). Not yet checked.

    Uncompressed only: a compressed answer is refused before its body is read, so the cap counts the bytes PieFed
    holds, and no decoder can turn a small answer into a large one (a decompression bomb)."""
    headers = {'User-Agent': user_agent(), **kwargs.pop('headers', {}), 'Accept-Encoding': 'identity'}
    try:
        with fetch_deadline(seconds), httpx_client.stream(method, url, headers=headers,
                                                          timeout=kwargs.pop('timeout', TIMEOUT), **kwargs) as response:
            if response.headers.get('Content-Encoding', 'identity').strip().lower() not in ('', 'identity'):
                raise Permanent('The answer was compressed, though PieFed asked for it plain.')
            chunks, size = [], 0
            for chunk in response.iter_raw():
                size += len(chunk)
                if size > max_bytes:
                    raise Permanent(f'The answer was larger than {max_bytes} bytes.')
                chunks.append(chunk)
    except httpx.TransportError as failure:
        raise Transient(f'Could not reach the service ({type(failure).__name__}).') from None
    headers = [(name, value) for name, value in response.headers.items()
               if name.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')]
    return httpx.Response(response.status_code, headers=headers, content=b''.join(chunks), request=response.request)


def call(method: str, url: str, **kwargs) -> httpx.Response:
    return check(fetch(method, url, **kwargs))
