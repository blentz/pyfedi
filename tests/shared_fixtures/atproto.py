"""Fixtures for the ATProto tests (tests/test_atproto_hosted*.py), registered in tests/conftest.py: a member on a
domain whose Bluesky accounts are made on tests/atproto_fake.py's PDS. Each test gets its own Redis keys for the
budget, the describeServer cache and the creation spacing, since the test Redis is shared between workers."""
import itertools
import uuid
from types import SimpleNamespace

import pytest
import respx

from app import db, storefront
from app.atproto import budget
from app.connections import setting_name
from app.connections import bluesky_hosted as hosted
from app.connections.tokens import seal_tokens
from app.constants import COMMUNITY_KIND_MICROBLOGS
from app.models import SocialConnection, UserFollower
from app.sealing import generate_key
from app.utils import set_setting
from tests.atproto_fake import ADMIN_PASSWORD, PDS, PLC, FakePDS
from tests.factories import make_community, make_post, make_user
from tests.test_community_add_post import instance

_numbers = itertools.count(1)


@pytest.fixture
def pds_router():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def hosted_world(app, community_add_post_poster, pds_router, monkeypatch):
    client, token, community, author, english = community_add_post_poster
    home = storefront.domain_by_id(author.site_domain_id).domain
    for name, value in (('ATPROTO_PDS_URL', PDS), ('ATPROTO_PDS_ADMIN_PASSWORD', ADMIN_PASSWORD),
                        ('CONNECTIONS_SECRETS_KEY', generate_key()), ('BLUESKY_OAUTH_KEY', ''),
                        ('ATPROTO_MAX_ACCOUNTS', 2), ('ATPROTO_HOSTED_LIMIT', 3), ('ATPROTO_SLOT_POLICY', 'sticky'),
                        ('ATPROTO_BUDGET_SECOND', 1000), ('ATPROTO_BUDGET_HOUR', 10000),
                        ('ATPROTO_BUDGET_DAY', 100000)):
        monkeypatch.setitem(app.config, name, value)
    tag = uuid.uuid4().hex
    monkeypatch.setattr(budget, 'PREFIX', f'test:{tag}:budget')
    monkeypatch.setattr(hosted, 'DESCRIBE_KEY', f'test:{tag}:describe')
    monkeypatch.setattr(hosted, 'CREATE_KEY', f'test:{tag}:create')
    monkeypatch.setattr(hosted, 'CREATE_SPACING_MS', 1)
    set_setting(setting_name('bluesky'), True)
    set_setting(setting_name(hosted.HOSTING), True)
    fake = FakePDS(pds_router, [home])
    microblogs = make_community('microblogs', kind=COMMUNITY_KIND_MICROBLOGS)
    return SimpleNamespace(client=client, token=token, community=community, author=author, home=home, pds=fake,
                           microblogs=microblogs, app=app)


def a_member(name: str):
    user = make_user(instance(), name, local=True)
    db.session.commit()
    return user


def a_hosted(world, user, state=hosted.ACTIVE, **columns) -> SocialConnection:
    """A hosted row as bluesky_hosted leaves one: with an account on the fake PDS unless it is waiting for its first."""
    if state == hosted.WAITING and not columns.pop('with_account', False):
        row = SocialConnection(user_id=user.id, service='bluesky', variant=hosted.HOSTED, slot_state=state,
                               remote_id=f'waiting:{user.id}', remote_name=f'{user.user_name}.{world.home}',
                               sealed_tokens=seal_tokens({'label': user.user_name}), default_on=False, **columns)
    else:
        did = world.pds.account(f'{user.user_name}.{world.home}', active=state == hosted.ACTIVE)
        row = SocialConnection(user_id=user.id, service='bluesky', variant=hosted.HOSTED, slot_state=state,
                               remote_id=did, remote_name=f'{user.user_name}.{world.home}',
                               sealed_tokens=seal_tokens(world.pds.tokens(did)), default_on=False, **columns)
    db.session.add(row)
    db.session.commit()
    return row


def a_microblog(world, user=None, **columns):
    post = make_post(world.microblogs, user or world.author, f'https://{world.home}/post/m{next(_numbers)}',
                     title='')
    post.body = f'note {post.id}'
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def follow(user, count: int):
    for number in range(count):
        follower = a_member(f'fan{user.id}x{number}')
        db.session.add(UserFollower(local_user_id=user.id, remote_user_id=follower.id, is_accepted=True,
                                    is_inward=True))
    db.session.commit()


@pytest.fixture
def inbound(hosted_world, monkeypatch):
    """hosted_world with Bluesky's directory and resolver on the fake PDS, and bob, an account there someone could
    follow (a handle answering no well-known: it resolves through the resolver)."""
    monkeypatch.setitem(hosted_world.app.config, 'BLUESKY_PLC_URL', PLC)
    monkeypatch.setitem(hosted_world.app.config, 'BLUESKY_RESOLVER', PDS)
    hosted_world.pds.router.route(path='/.well-known/atproto-did').respond(404)
    hosted_world.bob = hosted_world.pds.account('bob.bsky.example')
    hosted_world.pds.put(hosted_world.bob, 'app.bsky.actor.profile', 'self', {
        '$type': 'app.bsky.actor.profile', 'displayName': 'Bob', 'description': 'I *post* here',
        'avatar': {'$type': 'blob', 'ref': {'$link': 'bafkavatar'}, 'mimeType': 'image/png', 'size': 9}})
    return hosted_world
