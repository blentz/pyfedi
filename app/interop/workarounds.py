"""Named, versioned workarounds for peer software (interop spec D12).

D12: kind matchers dispatch on object shape, never on `software` or host. `Instance.software`/`version` may be read
only here, in one registry where each entry records the software and versions it covers, why it exists and when it
was observed, so it can be retired once the upstream behaviour changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from flask import current_app

from app import cache, db
from app.utils import remote_instance_software

UNKNOWN_SOFTWARE = ('', 'unknown')   # find_instance_id creates a row with software='unknown' until nodeinfo is read
NODEINFO_TIMEOUT = 5                 # seconds for the whole read: it runs on the inbox path
FAILED_READ_TTL = 24 * 60 * 60       # a host whose nodeinfo failed is not asked again for a day


@dataclass(frozen=True)
class Workaround:
    name: str
    software: str               # Instance.software, lowercased
    versions: tuple[str, str]   # the oldest and newest versions observed to need it
    reason: str
    observed: str               # ISO date the behaviour was verified


WORKAROUNDS = {w.name: w for w in (
    Workaround(name='castopod_person_is_podcast', software='castopod', versions=('1.13.4', '1.15.5'),
               reason="Castopod publishes a podcast's actor as type Person, not Podcast, and federates only "
                      "podcasts: every actor on a Castopod server is a podcast (interop D24). Six servers checked.",
               observed='2026-10-04'),
)}


def _host(url) -> str:
    try:
        return (urlsplit(url).hostname or '').lower() if isinstance(url, str) else ''
    except ValueError:
        return ''


def instance_software(instance) -> str:
    """`instance.software`, lowercased. Unknown software is read once from the host's nodeinfo (capped at
    NODEINFO_TIMEOUT) and stored on the row; an offline instance, or nodeinfo that cannot be read, stays unknown ('').
    A failed or unknown read is remembered per domain for FAILED_READ_TTL, so a host whose nodeinfo is slow, broken
    or missing costs one request a day rather than one per actor (security review of a035710ec)."""
    software = (instance.software or '').strip().lower()
    if software not in UNKNOWN_SOFTWARE:
        return software
    if not instance.online() or not instance.domain:
        return ''
    failed_key = f'interop:nodeinfo-failed:{instance.domain.strip().lower()}'
    if cache.get(failed_key):
        return ''
    try:
        software = remote_instance_software(f'https://{instance.domain}', timeout=NODEINFO_TIMEOUT)
    except Exception as ex:   # transport, status or shape: the software stays unknown
        current_app.logger.info(f'nodeinfo for {instance.domain} unreadable: {ex}')
        software = ''
    if software in UNKNOWN_SOFTWARE:
        cache.set(failed_key, True, timeout=FAILED_READ_TTL)
        return ''
    instance.software = software[:50]   # Instance.software is String(50)
    db.session.commit()
    return instance.software


def is_castopod_podcast(actor_json, instance) -> bool:
    """True when an actor document is a Castopod podcast: type `Podcast`, or (workaround castopod_person_is_podcast)
    type `Person` whose id is on `instance`, the actor's own host, and that instance runs Castopod."""
    if not isinstance(actor_json, dict):
        return False
    kind = actor_json.get('type')
    if kind == 'Podcast':
        return True
    if kind != 'Person' or instance is None:
        return False
    actor_host = _host(actor_json.get('id'))
    if not actor_host or actor_host != (instance.domain or '').strip().lower():
        return False
    return instance_software(instance) == WORKAROUNDS['castopod_person_is_podcast'].software
