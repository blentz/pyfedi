"""Who PieFed says it is to the apps that refuse anything but their own server software. Every other caller is told
PieFed. Each answer is the API level PieFed serves that app, not PieFed's version.

- Jerboa (plan D3; spec 2026-11-10-lemmy-v3-design §3, ruling L1) refuses a server whose nodeinfo does not say
  `lemmy`, and picks the models it decodes with from the version, so it is told Lemmy 0.19.11. It names itself (its
  Http.kt sends `User-Agent: Jerboa`), but its nodeinfo request is its API library's, as `LemmyKotlinApi`.
- Pixelfed's own app (spec 2026-11-10-pixelfed-app-design, ruling X1) sends no agent of its own and reads nodeinfo at
  a path of Pixelfed's, /api/nodeinfo/2.0.json, which only Pixelfed's clients ask: that path says Pixelfed 0.12.6."""
import re

from flask import Response, request

from app.storefront import storefront_cache_key

LEMMY_VERSION = '0.19.11'
PIXELFED_VERSION = '0.12.6'
PIXELFED_NODEINFO = '/api/nodeinfo/2.0.json'
# Jerboa names itself on its own requests; its nodeinfo request goes through its API library's client
# (it.vercruysse.lemmyapi), which names itself LemmyKotlinApi
JERBOA = re.compile(r'(?:Jerboa|LemmyKotlinApi)(?![\w-])')


def is_jerboa() -> bool:
    return JERBOA.match(request.headers.get('User-Agent', '')) is not None


def software(own: dict) -> dict:
    """nodeinfo's `software`: `own` (PieFed's), Pixelfed's on Pixelfed's own path, or Lemmy's for Jerboa."""
    if request.path == PIXELFED_NODEINFO:
        return {**own, 'name': 'pixelfed', 'version': PIXELFED_VERSION}
    return {**own, 'name': 'lemmy', 'version': LEMMY_VERSION} if is_jerboa() else own


def nodeinfo_cache_key() -> str:
    """storefront_cache_key, with Jerboa's answer kept apart from everyone else's."""
    return storefront_cache_key() + ('#lemmy' if is_jerboa() else '')


def varying(response: Response) -> Response:
    """A document that answers by agent says so, so no shared cache serves one agent's to another."""
    response.vary.add('User-Agent')
    return response
