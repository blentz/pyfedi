"""Interop D24, decision 5: the post byline reads "Hosted by A, B · with guest C" from the credits, and
falls back to the podcast's own name when there are none."""
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


HOSTILE = '"><script>x</script>'


def verified(name, user, role='host'):
    return {**credit(name, role=role, profile_url='https://social.example/@ann', user_id=user.id), 'verified': True}


@pytest.mark.parametrize('microblog', [True, False])
def test_the_poster_stays_and_the_credits_follow_it(app, world, monkeypatch, microblog):
    credits = [verified('Ann Host', world.ann), credit('Ben Cohost'), credit('Cara Guest', role='guest')]
    html = page(app, with_credits(world, microblog=microblog, credits=credits), monkeypatch)

    poster = html.index(f'href="/u/{world.podcast.link()}"')   # the podcast account, via render_username
    assert poster < html.index('podcast_byline')
    assert 'hosted by' in html and 'with guest' in html and 'Cara Guest' in html
    assert f'href="/u/{world.ann.link()}"' in html


def test_an_unverified_credit_is_plain_text_even_for_a_real_user(app, world, monkeypatch):
    credits = [credit('Ann Host', profile_url='https://social.example/@ann', user_id=world.ann.id),
               credit('Ben Cohost', profile_url='https://ben.example/about')]
    html = page(app, with_credits(world, credits=credits), monkeypatch)
    byline = html[html.index('podcast_byline'):]
    byline = byline[:byline.index('</span>')]

    assert 'Ann Host' in byline and 'Ben Cohost' in byline
    assert '<a ' not in byline and 'ben.example' not in html and f'/u/{world.ann.link()}' not in byline


def test_a_hostile_credit_name_is_escaped(app, world, monkeypatch):
    html = page(app, with_credits(world, credits=[credit(HOSTILE)]), monkeypatch)

    assert '<script>x</script>' not in html and '&lt;script&gt;x&lt;/script&gt;' in html


def test_without_credits_there_is_no_byline_and_the_poster_remains(app, world, monkeypatch):
    html = page(app, with_credits(world), monkeypatch)

    assert 'podcast_byline' not in html
    assert f'href="/u/{world.podcast.link()}"' in html


def test_a_verified_credit_links_to_its_profile_but_a_banned_one_does_not(world):
    post = with_credits(world, credits=[verified('Ann Host', world.ann)])
    assert podcast_byline(post)['hosts'] == [{'name': world.ann.display_name(), 'href': f'/u/{world.ann.link()}', 'local': True}]

    world.ann.banned = True
    db.session.commit()
    assert podcast_byline(post)['hosts'] == [{'name': 'Ann Host', 'href': None, 'local': False}]


def test_a_verified_credit_shows_the_users_own_name_not_the_feeds(world):
    post = with_credits(world, credits=[verified('Famous Name', world.ann)])

    assert [h['name'] for h in podcast_byline(post)['hosts']] == [world.ann.display_name()]


def test_a_stored_user_id_without_the_verified_flag_does_not_link(world):
    post = with_credits(world, credits=[credit('Ann Host', user_id=world.ann.id)])

    assert podcast_byline(post)['hosts'] == [{'name': 'Ann Host', 'href': None, 'local': False}]


def test_guests_without_hosts_have_no_hosts_line(world):
    byline = podcast_byline(with_credits(world, credits=[credit('Cara Guest', role='guest')]))

    assert byline == {'hosts': [], 'guests': [{'name': 'Cara Guest', 'href': None, 'local': False}]}


def test_no_credits_means_no_byline(world):
    assert podcast_byline(with_credits(world)) is None
