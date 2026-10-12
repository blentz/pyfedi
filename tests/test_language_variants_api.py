"""C3: the alpha API carries language variants."""
import pytest

from app import db
from app.api.alpha.schema import Comment, Post as PostSchema
from app.api.alpha.views import post_view, reply_view
from app.utils import utcnow
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_site, make_user)

PEER = 'remote.test'
FR = {'fr': {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}}


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


def test_post_view_has_language_variants(world):
    _, _, parent = world
    parent.language_variants = FR
    db.session.commit()
    assert post_view(parent, variant=1)['language_variants'] == FR


def test_post_view_without_variants_is_null(world):
    assert post_view(world[2], variant=1)['language_variants'] is None


def test_a_stub_of_a_hidden_post_does_not_carry_variants(world):
    _, _, parent = world
    parent.language_variants, parent.visibility = FR, 'followers'
    db.session.commit()
    assert 'language_variants' not in post_view(parent, variant=1, user_id=None)


def test_deleted_post_hides_its_variants(world):
    _, _, parent = world
    parent.language_variants, parent.deleted = FR, True
    db.session.commit()
    assert post_view(parent, variant=1)['language_variants'] is None


def test_reply_view_has_language_variants(world):
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.language_variants = FR
    db.session.commit()
    assert reply_view(reply, variant=1)['language_variants'] == FR


def test_deleted_reply_hides_its_variants(world):
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.language_variants, reply.deleted = FR, True
    db.session.commit()
    assert reply_view(reply, variant=1)['language_variants'] is None


def test_schema_declares_the_field():
    assert 'language_variants' in PostSchema().fields and 'language_variants' in Comment().fields
