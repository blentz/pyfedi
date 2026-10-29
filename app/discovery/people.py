"""Pixelfed seeding (docs/superpowers/specs/2026-10-29-pixelfed-seeding-design.md): the instance actor (/actor)
follows the top N opted-in Pixelfed people per server from the discovery directory, so their posts land in microblogs
as relayed Mastodon posts do. Pixelfed has no relay support (pixelfed/pixelfed#4472) and its timelines need a login,
so following is the only way in. Each account's Atom feed fills in what the Follow cannot: posts made before it, and
any a delivery lost (Pixelfed's outbox has no items)."""
import xml.etree.ElementTree as ElementTree

import httpx
from flask import current_app
from sqlalchemy import func

from app import celery, db
from app.activitypub.util import NEW_CONTENT_TYPES, create_resolved_object, find_actor_or_create, \
    find_microblogging_community, host_of, remote_object_to_json
from app.discovery import ADDS_PER_HOST_PER_RUN, ADDS_PER_RUN, KIND_PERSON, MAX_PER_HOST, POLL_SPACING_SECONDS
from app.discovery.credits import _declares_a_dtd
from app.discovery.filters import host_is_excluded
from app.discovery.instance_actor import needs_refollow, send_instance_follow, send_instance_undo
from app.models import DiscoveryEntry, DiscoveryExclusion, DiscoveryPersonSync, Post, User, utcnow
from app.storefront import acting_as_default, is_hosted_uri
from app.utils import get_request_capped, get_setting, gibberish

ATOM = '{http://www.w3.org/2005/Atom}'
ATOM_MAX_ENTRIES = 10          # what Pixelfed's feed holds (ProfileController::showAtomFeed, take(10))
ATOM_MAX_BYTES = 1024 * 1024
ERROR_LIMIT = 255              # discovery_person_sync.last_error


def people_per_host() -> int:
    """discovery_people_per_host (owner ruling S3: its own setting, default 0), or 0 for anything that is not an int
    from 0 to MAX_PER_HOST (bools included)."""
    value = get_setting('discovery_people_per_host', 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_PER_HOST)


def _unusable_person(user) -> bool:
    return user.banned or user.deleted


def _refused_urls() -> set:
    """Actor URLs never followed: accounts banned or deleted here, and those an admin removed (discovery_exclusion)."""
    banned = {url for (url,) in db.session.query(User.ap_profile_id).filter(
        (User.banned == True) | (User.deleted == True), User.ap_profile_id != None)}
    return banned | {url for (url,) in db.session.query(DiscoveryExclusion.actor_url)}


def desired_people() -> dict:
    """{host: [DiscoveryEntry, ...]}: each server's top people_per_host() opted-in Pixelfed people by followers, then
    name. Not NSFW (an Adult server's people are), on no blocked host, not banned, deleted or excluded here."""
    per_host = people_per_host()
    if per_host == 0:
        return {}
    refused = _refused_urls()
    query = db.session.query(DiscoveryEntry).filter(DiscoveryEntry.kind == KIND_PERSON,
                                                    DiscoveryEntry.platform == 'pixelfed',
                                                    DiscoveryEntry.nsfw == False) \
        .order_by(func.lower(DiscoveryEntry.host), DiscoveryEntry.followers.desc(), DiscoveryEntry.name,
                  DiscoveryEntry.id)
    desired, excluded = {}, {}
    for entry in query:
        host = entry.host.lower()
        if host not in excluded:
            excluded[host] = host_is_excluded(host, frozenset())
        if excluded[host] or entry.actor_url.lower() in refused:
            continue
        chosen = desired.setdefault(host, [])
        if len(chosen) < per_host:
            chosen.append(entry)
    return {host: entries for host, entries in desired.items() if entries}


def drop_person(row) -> None:
    """Unfollow and forget a seeded account. The account and its posts stay; the row cannot outlive the account
    (its foreign key cascades), so the account is always there to unfollow."""
    send_instance_undo(row, db.session.get(User, row.user_id))
    db.session.delete(row)
    db.session.commit()


def _add(entry, held) -> bool:
    if is_hosted_uri(entry.actor_url):   # an account of ours is never followed by ourselves
        return False
    user = find_actor_or_create(entry.actor_url)
    if not isinstance(user, User) or user.is_local() or _unusable_person(user):
        current_app.logger.info(f'discovery people: {entry.actor_url} did not resolve to a remote person')
        return False
    if db.session.get(DiscoveryPersonSync, user.id) is not None:   # another entry already resolved to this account
        return False
    row = DiscoveryPersonSync(user_id=user.id, entry_id=entry.id, follow_target=entry.actor_url)
    db.session.add(row)
    db.session.commit()
    held[row.follow_target.lower()] = row
    send_instance_follow(row, user)
    queue_person_poll(user.id)   # what the account posted before the Follow
    return True


def reconcile_people() -> dict:
    """Keep exactly each server's desired people followed, as reconcile_sync does channels."""
    desired = desired_people()
    wanted = {entry.actor_url.lower() for entries in desired.values() for entry in entries}
    summary = {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    held = {}
    for row in db.session.query(DiscoveryPersonSync).all():
        if _unusable_person(db.session.get(User, row.user_id)) or row.follow_target.lower() not in wanted:
            try:
                drop_person(row)
                summary['dropped'] += 1
            except Exception:
                current_app.logger.exception(f'discovery people: dropping user {row.user_id} failed')
                db.session.rollback()
        else:
            held[row.follow_target.lower()] = row
    now = utcnow()
    run_added = 0
    for host in sorted(desired):
        try:
            added = 0
            for entry in desired[host]:
                row = held.get(entry.actor_url.lower())
                if row is not None:
                    if needs_refollow(row, now) and send_instance_follow(row, db.session.get(User, row.user_id)):
                        summary['refollowed'] += 1
                elif added < ADDS_PER_HOST_PER_RUN and run_added < ADDS_PER_RUN and _add(entry, held):
                    added += 1
                    run_added += 1
            summary['added'] += added
        except Exception:
            current_app.logger.exception(f'discovery people: reconcile of {host} failed')
            db.session.rollback()
            summary['failed_hosts'].append(host)
    return summary


def _known_post(url: str) -> bool:
    return Post.get_by_ap_id(url) is not None


def fetch_post(url: str, host: str) -> None:
    """Fetch a post the feed lists and file it in microblogs, as the inbox files a Note that names no community
    (routes.py) and the API's resolve does (get_resolve_object): create_resolved_object checks the author's host and
    the posting gates. A reply or a non-content object is not a seeded post."""
    post_data = remote_object_to_json(url, retry_delay=0)
    if not isinstance(post_data, dict) or post_data.get('inReplyTo') or post_data.get('type') not in NEW_CONTENT_TYPES:
        return
    create_resolved_object(url, post_data, host, find_microblogging_community(),
                           f'https://{host}/activities/announce/{gibberish(15)}', False)


def _feed_post_urls(feed: bytes, host: str) -> list[str] | None:
    """The https post URLs on `host` that an Atom feed's entries name, in feed order, at most ATOM_MAX_ENTRIES; None
    when the feed cannot be read (a DTD or entity declaration is refused before parsing)."""
    if _declares_a_dtd(feed):
        return None
    try:
        root = ElementTree.fromstring(feed)
    except ElementTree.ParseError:
        return None
    urls = []
    for entry in root.findall(f'{ATOM}entry'):
        url = (entry.findtext(f'{ATOM}id') or '').strip()
        if url.startswith('https://') and host_of(url) == host and url not in urls:
            urls.append(url)
    return urls[:ATOM_MAX_ENTRIES]


def _record(row, error) -> None:
    row.last_polled_at = utcnow()
    row.last_error = error[:ERROR_LIMIT] if error else None
    db.session.commit()


@celery.task
@acting_as_default()   # instance-level: the default domain's, whoever queued it (plan C3)
def poll_synced_person(user_id: int) -> None:
    """Fetch the posts a seeded account's Atom feed lists that are not here yet, as any post URL is fetched, so each
    lands in microblogs as a pushed one does."""
    row = db.session.get(DiscoveryPersonSync, user_id)
    if row is None:
        return
    host = host_of(row.follow_target)
    try:
        status, body = get_request_capped(f'{row.follow_target}.atom', ATOM_MAX_BYTES,
                                          headers={'Accept': 'application/atom+xml'}, max_seconds=15)
    except httpx.HTTPError as error:
        _record(row, f'Atom feed: {type(error).__name__}')
        return
    if status != 200:
        _record(row, f'Atom feed: HTTP {status}')
        return
    if body is None:
        _record(row, 'Atom feed: over the size cap')
        return
    urls = _feed_post_urls(body, host)
    if urls is None:
        _record(row, 'Atom feed: unreadable')
        return
    for url in urls:
        if _known_post(url):
            continue
        try:
            fetch_post(url, host)
        except Exception:   # one post's failure must not stop the rest
            current_app.logger.exception(f'discovery people: {url} could not be fetched')
            db.session.rollback()
    _record(row, None)


def queue_person_poll(user_id: int) -> None:
    if current_app.debug:
        poll_synced_person(user_id)
    else:
        poll_synced_person.apply_async(args=[user_id], countdown=0)


def enqueue_person_polls() -> int:
    """One poll per seeded account; one host sees at most one poll every POLL_SPACING_SECONDS."""
    per_host = {}
    queued = 0
    for row in db.session.query(DiscoveryPersonSync).order_by(DiscoveryPersonSync.user_id).all():
        host = host_of(row.follow_target)
        slot = per_host.get(host, 0)
        per_host[host] = slot + 1
        poll_synced_person.apply_async(args=[row.user_id], countdown=slot * POLL_SPACING_SECONDS)
        queued += 1
    return queued
