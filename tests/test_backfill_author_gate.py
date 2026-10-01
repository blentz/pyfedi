"""PERM-3 residue B, owner ruling: posts and replies backfilled from a community's
outbox by retrieve_mods_and_backfill (app/community/util.py) pass the same author
gates as an inbound Create -- can_create_post for a post, can_create_post_reply for
a reply, which between them carry the instance ban and allowlist and the community
ban. A refused one is skipped and logged; the rest of the outbox is still read.

Drives the real task, doubling only the network (remote_object_to_json) and actor
resolution (find_actor_or_create), as tests/test_backfill_reply_visibility.py does.
"""
import pytest

from app import db
from app.community.util import retrieve_mods_and_backfill
from app.models import BannedInstances, Post, PostReply
from tests.factories import make_community, make_instance, make_site, make_user

OUTBOX = 'https://remote.example/c/microblogs/outbox'
REPLIES = 'https://remote.example/posts/1/replies'


def a_remote_user(instance, name):
    """make_user leaves ap_domain empty, which instance_banned refuses outright."""
    user = make_user(instance, name)
    user.ap_domain = instance.domain
    db.session.commit()
    return user


@pytest.fixture
def env(app, db_session):
    make_site()
    instance = make_instance('remote.example')
    alice = a_remote_user(instance, 'alice')  # user 1, whom make_community names as owner
    community = make_community('microblogs')
    community.ap_moderators_url = None
    community.ap_outbox_url = OUTBOX
    community.ap_featured_url = None
    community.nsfw = community.nsfl = False
    db.session.commit()
    return community, instance, alice


def an_announce(number, author, replies=None):
    note = {'id': f'https://remote.example/posts/{number}', 'type': 'Note',
            'attributedTo': author.ap_profile_id, 'content': f'<p>post {number}</p>',
            'to': ['https://www.w3.org/ns/activitystreams#Public'],
            'published': '2026-08-20T12:00:00Z'}
    if replies:
        note['replies'] = replies
    return {'id': f'https://remote.example/activities/announce/{number}', 'type': 'Announce',
            'object': {'id': f'https://remote.example/activities/create/{number}', 'type': 'Create',
                       'actor': author.ap_profile_id, 'object': note}}


def run_backfill(community, answers, authors, monkeypatch):
    monkeypatch.setattr('app.community.util.remote_object_to_json', lambda uri, *a, **k: answers.get(uri))
    monkeypatch.setattr('app.community.util.find_actor_or_create',
                        lambda uri, *a, **k: authors.get(uri))
    retrieve_mods_and_backfill(community.id, 'remote.example', 'microblogs', None)


def test_a_post_whose_author_may_not_post_is_skipped_and_the_next_one_kept(env, monkeypatch, caplog):
    community, instance, allowed = env
    gagged = a_remote_user(instance, 'mallory')
    gagged.ban_posts = True
    db.session.commit()
    outbox = {'type': 'OrderedCollection', 'totalItems': 2,
              'orderedItems': [an_announce(1, gagged), an_announce(2, allowed)]}

    run_backfill(community, {OUTBOX: outbox},
                 {allowed.ap_profile_id: allowed, gagged.ap_profile_id: gagged}, monkeypatch)

    assert [p.ap_id for p in Post.query.all()] == ['https://remote.example/posts/2']
    assert 'https://remote.example/posts/1' in caplog.text


def test_a_post_from_a_banned_instance_is_skipped(env, monkeypatch):
    community, instance, alice = env
    author = a_remote_user(make_instance('banned.example'), 'eve')
    db.session.add(BannedInstances(domain='banned.example'))
    db.session.commit()
    outbox = {'type': 'OrderedCollection', 'totalItems': 1, 'orderedItems': [an_announce(1, author)]}

    run_backfill(community, {OUTBOX: outbox}, {author.ap_profile_id: author}, monkeypatch)

    assert Post.query.count() == 0


def test_a_reply_from_a_banned_instance_is_skipped(env, monkeypatch, caplog):
    """An instance ban rather than ban_comments, which PostReply.new already refuses."""
    community, instance, author = env
    gagged = a_remote_user(make_instance('banned.example'), 'mallory')
    db.session.add(BannedInstances(domain='banned.example'))
    db.session.commit()
    replies = {'type': 'OrderedCollection', 'orderedItems': [
        {'id': f'https://remote.example/comments/{name}', 'type': 'Note',
         'attributedTo': who.ap_profile_id, 'content': f'<p>from {name}</p>',
         'inReplyTo': 'https://remote.example/posts/1',
         'to': ['https://www.w3.org/ns/activitystreams#Public']}
        for name, who in (('mallory', gagged), ('alice', author))]}
    outbox = {'type': 'OrderedCollection', 'totalItems': 1,
              'orderedItems': [an_announce(1, author, replies=REPLIES)]}

    run_backfill(community, {OUTBOX: outbox, REPLIES: replies},
                 {author.ap_profile_id: author, gagged.ap_profile_id: gagged}, monkeypatch)

    assert [r.ap_id for r in PostReply.query.all()] == ['https://remote.example/comments/alice']
    assert 'https://remote.example/comments/mallory' in caplog.text
