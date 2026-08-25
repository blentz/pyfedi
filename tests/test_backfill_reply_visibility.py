"""FINDING 1: retrieve_mods_and_backfill (app/community/util.py) builds backfilled
post replies via PostReply.new() directly, never going through create_post_reply,
so the followers/direct refusal that create_post_reply enforces was absent from
this path entirely. reply_data fetched from an arbitrary remote host's `replies`
collection landed in the database -- and was rendered to everyone -- regardless
of its addressing.

This test drives the real celery-task function (not a reimplementation of its
loop), mocking only network I/O (remote_object_to_json), actor resolution
(find_actor_or_create), and post creation (create_post, replaced with a
pre-made local Post row) so the reply-backfill loop runs unmodified against a
realistic Mastodon-shaped `replies` OrderedCollection.
"""
from app import db
from tests.factories import make_community, make_instance, make_post, make_site, make_user


def test_followers_only_backfilled_reply_is_refused(db_session, monkeypatch):
    from app.community.util import retrieve_mods_and_backfill
    from app.models import PostReply

    make_site()
    instance = make_instance('remote.example')
    author = make_user(instance, 'alice')
    community = make_community('microblogs')
    community.ap_moderators_url = None
    community.ap_outbox_url = 'https://remote.example/c/microblogs/outbox'
    community.ap_featured_url = None
    community.nsfw = False
    community.nsfl = False
    db.session.commit()

    post = make_post(community, author, 'https://remote.example/posts/1', title='', microblog=True)

    outbox_data = {
        'type': 'OrderedCollection',
        'totalItems': 1,
        'orderedItems': [
            {
                'id': 'https://remote.example/activities/announce/1',
                'type': 'Announce',
                'object': {
                    'id': 'https://remote.example/activities/create/1',
                    'object': {
                        'id': post.ap_id,
                        'type': 'Note',
                        'attributedTo': author.ap_profile_id,
                        'content': '<p>hello</p>',
                        'published': '2026-08-20T12:00:00Z',
                        'replies': 'https://remote.example/posts/1/replies',
                    },
                },
            }
        ],
    }

    replies_data = {
        'type': 'OrderedCollection',
        'orderedItems': [
            {
                'id': 'https://remote.example/comments/1',
                'type': 'Note',
                'attributedTo': author.ap_profile_id,
                'content': '<p>a followers-only reply</p>',
                'inReplyTo': post.ap_id,
                # Mastodon followers-only addressing: exactly one 'to', the actor's
                # /followers collection, no Public anywhere.
                'to': ['https://remote.example/users/alice/followers'],
            }
        ],
    }

    def fake_fetch(uri):
        if uri == community.ap_outbox_url:
            return outbox_data
        if uri == 'https://remote.example/posts/1/replies':
            return replies_data
        return None

    def fake_create_post(is_backfill, comm, request_json, user, announce_id):
        return post

    monkeypatch.setattr('app.community.util.remote_object_to_json', fake_fetch)
    monkeypatch.setattr('app.community.util.find_actor_or_create', lambda uri: author)
    monkeypatch.setattr('app.community.util.create_post', fake_create_post)

    retrieve_mods_and_backfill(community.id, 'remote.example', 'microblogs', None)

    assert PostReply.query.filter_by(ap_id='https://remote.example/comments/1').count() == 0, \
        'a followers-only reply from the replies collection must not be stored'
