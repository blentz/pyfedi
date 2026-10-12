"""C2: inbound quote posts (docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md)."""
import contextlib

import pytest

from app import db
from app.activitypub.util import (create_post_reply, quote_id_from, update_post_from_activity,
                                  update_post_reply_from_activity)
from app.models import Post, PostReply
from app.utils import utcnow
from tests.factories import make_community, make_community_member, make_instance, make_post, make_site, make_user

OWN = 'https://remote.test/notes/1'
QUOTED = 'https://other.test/notes/9'


@pytest.mark.parametrize('key', ['quote', 'quoteUrl', 'quoteUri', '_misskey_quote'])
def test_each_key_is_read(key):
    assert quote_id_from({'id': OWN, key: QUOTED}) == QUOTED


def test_quote_wins_over_the_legacy_keys():
    obj = {'id': OWN, '_misskey_quote': 'https://a.test/1', 'quoteUri': 'https://b.test/1',
           'quoteUrl': 'https://c.test/1', 'quote': QUOTED}
    assert quote_id_from(obj) == QUOTED


def test_an_invalid_first_key_falls_through():
    assert quote_id_from({'id': OWN, 'quote': 42, 'quoteUrl': QUOTED}) == QUOTED


def test_dict_form_uses_its_id():
    assert quote_id_from({'id': OWN, 'quote': {'id': QUOTED, 'type': 'Note'}}) == QUOTED


@pytest.mark.parametrize('value', [
    None, '', 42, [QUOTED], {'id': 7}, {}, 'ftp://other.test/1', 'https:///nohost', 'javascript:alert(1)',
    'https://other.test/' + 'a' * 240, 'https://ötherr.test/1', 'https://other.test/a b',
    'https://other.test/\x00', 'https://[::1/', 'other.test/1',
])
def test_hostile_values_are_refused(value):
    assert quote_id_from({'id': OWN, 'quote': value}) is None


def test_exactly_255_characters_is_accepted():
    url = 'https://other.test/' + 'a' * (255 - len('https://other.test/'))
    assert quote_id_from({'id': OWN, 'quote': url}) == url


def test_a_self_quote_is_none():
    assert quote_id_from({'id': OWN, 'quote': OWN}) is None


def test_no_quote_is_none():
    assert quote_id_from({'id': OWN}) is None


def test_columns_exist(app, db_session):
    from app.models import Post, PostReply
    assert Post.quote_ap_id.type.length == 255
    assert PostReply.quote_ap_id.type.length == 255


PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
PEER = 'remote.test'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in: only `.lock(...)`, as tests/test_ap_create_reply.py explains."""

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


@pytest.fixture
def world(app, db_session):
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'quoter')
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/quoter'
    make_community_member(author, community)
    parent = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, author, parent


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _note(n, **fields):
    obj = {'id': f'https://{PEER}/notes/{n}', 'type': 'Note', 'attributedTo': f'https://{PEER}/u/quoter',
           'to': [PUBLIC], 'cc': [], 'content': '<p>hello there friends</p>', 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def _new_reply(world, n, **fields):
    community, author, parent = world
    return create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                             request_json=_create(_note(n, inReplyTo=parent.ap_id, **fields)), user=author)


def test_post_new_stores_the_quote(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(2, quote=QUOTED)))
    assert post.quote_ap_id == QUOTED


def test_post_new_without_a_quote_stores_none(world):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(3))).quote_ap_id is None


def test_reply_stores_the_quote(world, redis_lock_only_double):
    assert _new_reply(world, 4, quoteUri=QUOTED).quote_ap_id == QUOTED


def test_reply_without_a_quote_stores_none(world, redis_lock_only_double):
    assert _new_reply(world, 7).quote_ap_id is None


@pytest.mark.parametrize('before,after', [(None, QUOTED), (QUOTED, 'https://other.test/notes/10'), (QUOTED, None)])
def test_post_update_sets_changes_and_clears(world, redis_lock_only_double, before, after):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(5, **({'quote': before} if before else {}))))
    db.session.add(post)
    db.session.commit()
    update_post_from_activity(post, {'id': 'u', 'type': 'Update', 'object': _note(5, **({'quote': after} if after else {}))})
    assert post.quote_ap_id == after


@pytest.mark.parametrize('before,after', [(None, QUOTED), (QUOTED, None)])
def test_reply_update_sets_and_clears(world, redis_lock_only_double, before, after):
    _, _, parent = world
    reply = _new_reply(world, 6, **({'quote': before} if before else {}))
    update_post_reply_from_activity(reply, {'id': 'u', 'type': 'Update',
                                            'object': _note(6, inReplyTo=parent.ap_id,
                                                            **({'quote': after} if after else {}))})
    assert reply.quote_ap_id == after


def test_a_local_reply_without_request_json_stores_no_quote(world, redis_lock_only_double):
    _, author, parent = world
    reply = PostReply.new(author, parent, None, body='hi', body_html='<p>hi</p>', notify_author=True,
                          language_id=None, distinguished=False, answer=False)
    assert reply.quote_ap_id is None
