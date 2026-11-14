"""Calls to a connected service, and what their answers mean (spec §8).

Every call goes through the shared pinned client (R162) with PieFed's user agent. An answer becomes a Transient
(429, 5xx, a network failure: worth trying again), an AuthFailed (401, 403: the member must connect again) or a
Permanent (any other 4xx). The service's own error text is kept, trimmed, for the author; a token never is, since
none of these calls puts one in a URL.
"""
import httpx

from app import httpx_client
from app.connections import AuthFailed, Permanent, Transient
from app.utils import user_agent

TIMEOUT = 30


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


def call(method: str, url: str, **kwargs) -> httpx.Response:
    headers = {'User-Agent': user_agent(), **kwargs.pop('headers', {})}
    try:
        response = httpx_client.request(method, url, headers=headers, timeout=kwargs.pop('timeout', TIMEOUT), **kwargs)
    except httpx.TransportError as failure:
        raise Transient(f'Could not reach the service ({type(failure).__name__}).') from None
    return check(response)
