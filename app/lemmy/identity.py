"""Who PieFed says it is to Jerboa (plan D3; spec 2026-11-10-lemmy-v3-design §3, ruling L1). Jerboa refuses a server
whose nodeinfo does not say `lemmy`, and picks the models it decodes with from the version, so it is told Lemmy
0.19.11: the API level /api/v3 serves, not PieFed's version. It names itself (its Http.kt sends `User-Agent: Jerboa`),
but its nodeinfo request is its API library's, as `LemmyKotlinApi`; every other agent is told PieFed."""
import re

from flask import Response, request

from app.storefront import storefront_cache_key

LEMMY_VERSION = '0.19.11'
# Jerboa names itself on its own requests; its nodeinfo request goes through its API library's client
# (it.vercruysse.lemmyapi), which names itself LemmyKotlinApi
JERBOA = re.compile(r'(?:Jerboa|LemmyKotlinApi)(?![\w-])')


def is_jerboa() -> bool:
    return JERBOA.match(request.headers.get('User-Agent', '')) is not None


def software(own: dict) -> dict:
    """nodeinfo's `software`: `own` (PieFed's), or Lemmy's name and version for Jerboa."""
    return {**own, 'name': 'lemmy', 'version': LEMMY_VERSION} if is_jerboa() else own


def nodeinfo_cache_key() -> str:
    """storefront_cache_key, with Jerboa's answer kept apart from everyone else's."""
    return storefront_cache_key() + ('#lemmy' if is_jerboa() else '')


def varying(response: Response) -> Response:
    """A document that answers by agent says so, so no shared cache serves one agent's to another."""
    response.vary.add('User-Agent')
    return response
