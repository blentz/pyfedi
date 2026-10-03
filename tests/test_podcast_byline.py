"""Interop D24, decision 5: the post byline reads "Hosted by A, B · with guest C" from the credits, and
falls back to the podcast's own name when there are none."""
import copy
from types import SimpleNamespace

import pytest

from app import db
from app.activitypub.util import actor_json_to_model
from app.discovery.credits import podcast_byline
from app.discovery.podcast import podcast_community_for
from tests.factories import make_instance, make_post, make_site, make_user, peer_actor_json, peer_instance
from tests.test_visibility_single_object import client_as

PEER = 'pod.example'
ACTOR = f'https://{PEER}/@mypodcast'


def credit(name, role='host', profile_url=None, user_id=None):
    return {'name': name, 'role': role, 'image': None, 'profile_url': profile_url, 'user_id': user_id}


@pytest.fixture
def world(app, db_session):
    make_site()
    peer_instance(PEER)
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'id': ACTOR, 'type': 'Podcast', 'name': 'My Podcast'}),
                                  'mypodcast', PEER)
    ann = make_user(make_instance('social.example'), 'ann')
    community = podcast_community_for(podcast)
    return SimpleNamespace(podcast=podcast, ann=ann, community=community)


def with_credits(world, microblog=True, credits=None, number=1):
    post = make_post(world.community, world.podcast, f'{ACTOR}/posts/{number}', title='Episode 1', microblog=microblog)
    if credits is not None:
        post.extensions = {'podcast': {'credits': credits}}
        db.session.commit()
    return post


def page(app, post, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)  # the post page renders form.csrf_token
    viewer = make_user(make_instance('viewer.example'), 'viewer', local=True)
    return client_as(app, viewer).get(f'/post/{post.id}').get_data(as_text=True)


CREDITS = (credit('Ann Host', profile_url='https://social.example/@ann'),
           credit('Ben Cohost', profile_url='https://ben.example/about'),
           credit('Cara Guest', role='guest'))


@pytest.mark.parametrize('microblog', [True, False])
def test_the_byline_names_hosts_and_guests(app, world, monkeypatch, microblog):
    credits = copy.deepcopy(list(CREDITS))
    credits[0]['user_id'] = world.ann.id
    html = page(app, with_credits(world, microblog=microblog, credits=credits), monkeypatch)

    assert 'Hosted by' in html
    assert f'href="/u/{world.ann.link()}"' in html
    assert 'href="https://ben.example/about"' in html
    assert 'with guest' in html and 'Cara Guest' in html


def test_without_credits_the_byline_is_the_podcast(app, world, monkeypatch):
    html = page(app, with_credits(world), monkeypatch)

    assert 'podcast_byline' not in html and 'Hosted by' not in html
    assert f'href="/u/{world.podcast.link()}"' in html   # the ordinary username, i.e. the podcast itself


def test_a_banned_users_credit_links_to_their_profile_url_instead(world):
    world.ann.banned = True
    db.session.commit()
    post = with_credits(world, credits=[credit('Ann Host', profile_url='https://social.example/@ann',
                                               user_id=world.ann.id)])

    assert podcast_byline(post)['hosts'] == [{'name': 'Ann Host', 'href': 'https://social.example/@ann',
                                              'local': False}]


def test_guests_without_hosts_are_hosted_by_the_podcast(world):
    post = with_credits(world, credits=[credit('Cara Guest', role='guest')])

    byline = podcast_byline(post)

    assert byline['hosts'] == [{'name': world.podcast.display_name(), 'href': f'/u/{world.podcast.link()}',
                                'local': True}]
    assert byline['guests'] == [{'name': 'Cara Guest', 'href': None, 'local': False}]


def test_no_credits_means_no_byline(world):
    assert podcast_byline(with_credits(world)) is None
