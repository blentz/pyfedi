"""Interop D24, decisions 5-7: the API exposes a podcast episode's credits as post.extensions.podcast.credits;
only a credit whose profile vouches back is linked, and the poster stays the post's creator."""
import pytest
from flask import g

from app import db
from app.api.alpha.schema import PostExtensions
from app.api.alpha.views import _neutral_post, post_view
from tests.factories import make_community, make_instance, make_post, make_site, make_user


@pytest.fixture
def world(app, db_session):
    make_site()
    podcast = make_user(make_instance('pod.example'), 'mypodcast')
    ann = make_user(make_instance('social.example'), 'ann')
    ann.title = 'Real Ann'
    db.session.commit()
    post = make_post(make_community(), podcast, 'https://pod.example/@mypodcast/posts/1')
    g.admin_ids = []
    return post, podcast, ann


def credits_for(ann):
    return [{'name': 'Famous Name', 'role': 'host', 'image': 'https://pod.example/ann.jpg',
             'profile_url': 'https://social.example/@ann', 'user_id': ann.id, 'verified': True},
            {'name': 'Cara Guest', 'role': 'guest', 'image': 'https://pod.example/c.jpg',
             'profile_url': 'https://cara.example/me', 'user_id': ann.id}]


def store(post, credits):
    post.extensions = {'podcast': {'credits': credits}}
    db.session.commit()


def test_verified_credit_carries_user_reference_and_users_own_name(world):
    post, podcast, ann = world
    store(post, credits_for(ann))

    view = post_view(post=post, variant=1)
    assert view['extensions']['podcast']['credits'][0] == {
        'name': ann.display_name(), 'role': 'host', 'image': 'https://pod.example/ann.jpg',
        'profile_url': 'https://social.example/@ann', 'user_id': ann.id}
    assert 'Famous Name' not in str(view['extensions'])


def test_unverified_credit_is_name_and_role_only(world):
    post, podcast, ann = world
    store(post, credits_for(ann))

    assert post_view(post=post, variant=1)['extensions']['podcast']['credits'][1] == {'name': 'Cara Guest', 'role': 'guest'}


def test_verified_credit_of_banned_user_is_unlinked(world):
    post, podcast, ann = world
    store(post, credits_for(ann)[:1])
    ann.banned = True
    db.session.commit()

    assert post_view(post=post, variant=1)['extensions']['podcast']['credits'] == [{'name': 'Famous Name', 'role': 'host'}]


def test_the_poster_stays_the_creator(world):
    post, podcast, ann = world
    store(post, credits_for(ann))

    assert post_view(post=post, variant=1)['user_id'] == podcast.id


def test_no_credits_means_no_podcast_extension(world):
    assert 'extensions' not in post_view(post=world[0], variant=1)


def test_a_deleted_post_does_not_carry_them(world):
    post, podcast, ann = world
    store(post, credits_for(ann))
    post.deleted = True
    db.session.commit()

    assert 'podcast' not in post_view(post=post, variant=1).get('extensions', {})


def test_a_neutral_post_never_carries_credits(world):
    post, podcast, ann = world
    store(post, credits_for(ann))

    assert 'extensions' not in _neutral_post(post)


def test_the_schema_describes_the_credits(world):
    credits = [{'name': 'A', 'role': 'host'}, {'name': 'B', 'role': 'guest', 'image': None, 'profile_url': None, 'user_id': 3}]
    assert PostExtensions().load({'podcast': {'credits': credits}}) == {'podcast': {'credits': credits}}
