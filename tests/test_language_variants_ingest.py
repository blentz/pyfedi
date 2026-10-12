"""C3: Create and Update store language variants and the default language."""
import contextlib

import pytest

from app import db
from app.activitypub.util import create_post_reply, update_post_from_activity, update_post_reply_from_activity
from app.models import Language, Post
from app.utils import site_language_id, utcnow
from tests.factories import make_community, make_community_member, make_instance, make_post, make_site, make_user

PEER = 'remote.test'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
MAPS = {'content': '<p>hello there</p>',
        'contentMap': {'fr': '<p>bonjour</p>', 'en': '<p>hello there</p>'},
        'nameMap': {'fr': 'Bonjour'}}


class _RedisLockOnlyDouble:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


@pytest.fixture
def world(app, db_session):
    make_site()
    for code, name in (('en', 'English'), ('fr', 'Français'), ('de', 'Deutsch')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'polyglot')
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/polyglot'
    make_community_member(author, community)
    parent = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, author, parent


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _note(n, **fields):
    obj = {'id': f'https://{PEER}/notes/{n}', 'type': 'Note', 'attributedTo': f'https://{PEER}/u/polyglot',
           'to': [PUBLIC], 'cc': [], 'content': '<p>hello there friends</p>', 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def _new_reply(world, n, **fields):
    community, author, parent = world
    return create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                             request_json=_create(_note(n, inReplyTo=parent.ap_id, **fields)), user=author)


def test_post_new_stores_variants_and_the_matching_language(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(2, **MAPS)))
    assert post.language.code == 'en'
    assert post.language_variants == {'fr': {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}}


def test_post_new_with_bare_text_content_matches_its_map_key_and_leaves_the_callers_dict(world):
    community, author, _ = world
    activity = _create(_note(30, content='Hello', contentMap={'de': 'Hallo', 'en': 'Hello'}))
    post = Post.new(author, community, activity)
    assert post.language.code == 'en'
    assert post.language_variants == {'de': {'title': None, 'body_html': '<p>Hallo</p>', 'cw': None}}
    assert post.body_html == '<p>Hello</p>'
    assert activity['object']['content'] == 'Hello'


def test_mastodon_single_key_map_is_unchanged(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(3, contentMap={'fr': '<p>hello there friends</p>'})))
    assert post.language.code == 'fr' and post.language_variants is None


def test_post_new_unknown_map_language_has_no_language(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(4, contentMap={'xx': '<p>hello there friends</p>'})))
    assert post.language_id is None


def test_post_new_without_maps_uses_the_site_language(world):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(5))).language_id == site_language_id()


def test_reply_stores_variants_and_the_matching_language(world, redis_lock_only_double):
    reply = _new_reply(world, 6, **MAPS)
    assert reply.language.code == 'en' and set(reply.language_variants) == {'fr'}
    assert reply.language_variants['fr']['title'] is None


def test_reply_unknown_map_language_has_no_language(world, redis_lock_only_double):
    assert _new_reply(world, 7, contentMap={'xx': '<p>hello there friends</p>'}).language_id is None


def test_reply_with_a_new_language_property_gets_its_id(world, redis_lock_only_double):
    reply = _new_reply(world, 8, language={'identifier': 'eo', 'name': 'Esperanto'})
    assert reply.language_id is not None and reply.language_variants is None


def test_post_update_sets_changes_and_clears(world, redis_lock_only_double):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(9)))
    db.session.add(post)
    db.session.commit()
    update_post_from_activity(post, {'id': 'u1', 'type': 'Update', 'object': _note(9, **MAPS)})
    assert set(post.language_variants) == {'fr'} and post.language.code == 'en'
    update_post_from_activity(post, {'id': 'u2', 'type': 'Update', 'object': _note(9)})
    assert post.language_variants is None


def test_reply_update_sets_and_clears(world, redis_lock_only_double):
    _, _, parent = world
    reply = _new_reply(world, 10)
    update_post_reply_from_activity(reply, {'id': 'u1', 'type': 'Update',
                                            'object': _note(10, inReplyTo=parent.ap_id, **MAPS)})
    assert set(reply.language_variants) == {'fr'} and reply.language.code == 'en'
    update_post_reply_from_activity(reply, {'id': 'u2', 'type': 'Update', 'object': _note(10, inReplyTo=parent.ap_id)})
    assert reply.language_variants is None


@pytest.fixture
def blocked(monkeypatch):
    monkeypatch.setattr('app.utils.blocked_phrases', lambda: ['forbidden'])


def test_post_with_a_blocked_phrase_only_in_a_variant_body_is_discarded(world, blocked):
    community, author, _ = world
    maps = dict(MAPS, contentMap={'fr': '<p>this is forbidden</p>', 'en': '<p>hello there</p>'})
    assert Post.new(author, community, _create(_note(20, **maps))) is None


def test_post_with_a_blocked_phrase_only_in_a_variant_title_is_discarded(world, blocked):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(21, **dict(MAPS, nameMap={'fr': 'forbidden'})))) is None


def test_post_with_clean_variants_is_kept_under_a_blocklist(world, blocked):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(22, **MAPS))) is not None


def test_reply_with_a_blocked_phrase_only_in_a_variant_body_is_rejected(world, blocked, redis_lock_only_double):
    maps = dict(MAPS, contentMap={'fr': '<p>this is forbidden</p>', 'en': '<p>hello there</p>'})
    assert _new_reply(world, 23, **maps) is None  # create_post_reply turns PostReplyValidationError into None


def test_reply_with_clean_variants_is_kept_under_a_blocklist(world, blocked, redis_lock_only_double):
    assert _new_reply(world, 24, **MAPS) is not None
