"""Interop D24, decisions 6 and 7: credits are fetched from the podcast's feed after an episode arrives,
fediverse hrefs are linked to their PieFed User, and the result lives in post.extensions['podcast']."""
import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cache, db
from app.utils import get_request_capped
from app.activitypub.util import actor_json_to_model, create_post
from app.discovery import credits
from app.discovery.credits import MAX_FEED_BYTES, credit_vouches, fetch_episode_credits_task, store_credits, \
    verified_credit_user
from app.discovery.podcast import podcast_community_for
from app.models import Community, Post, User, UserExtraField
from tests.factories import make_banned_instance, make_post, make_site, peer_actor_json, peer_instance

PEER = 'pod.example'
ACTOR = f'https://{PEER}/@mypodcast'
FEED_URL = f'https://{PEER}/@mypodcast/feed.xml'
EP1 = f'https://{PEER}/@mypodcast/episodes/ep-1'
FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_feed.xml'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def world(app, db_session):
    make_site()
    peer_instance(PEER)
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'id': ACTOR, 'type': 'Podcast', 'name': 'My Podcast',
                                                          'rssFeed': FEED_URL}), 'mypodcast', PEER)
    community = podcast_community_for(podcast)
    post = make_post(community, podcast, f'{ACTOR}/posts/1', microblog=True)
    return SimpleNamespace(podcast=podcast, community=community, post=post)


def stored(post_id):
    db.session.expire_all()
    return db.session.get(Post, post_id).extensions


VOUCH = {'alsoKnownAs': [ACTOR]}


def serve_actor(http_mock, href, name, server='social.example', actor_type='Person', fields=None):
    return http_mock.get(href).respond(json=peer_actor_json(actor_type, name=name, server=server,
                                                            fields=fields or {}))


def serve_profile(http_mock, name, fields=None):
    """A Mastodon-style profile URL whose document's id is elsewhere on the same host: both are served, since the
    id must be fetched from itself before the document is trusted. Returns (profile route, id route)."""
    return (serve_actor(http_mock, f'https://social.example/@{name}', name, fields=fields),
            serve_actor(http_mock, f'https://social.example/u/{name}', name, fields=fields))


def rows():
    return User.query.count(), Community.query.count()


def test_a_vouching_actor_id_resolves_to_a_new_user_with_one_fetch(world, http_mock):
    peer_instance('social.example')
    route = serve_actor(http_mock, 'https://social.example/u/ann', 'ann', fields=VOUCH)

    user_id = verified_credit_user('https://social.example/u/ann', world.podcast)

    assert db.session.get(User, user_id).ap_profile_id == 'https://social.example/u/ann'
    assert route.call_count == 1   # created from the document in hand, not fetched a second time


def test_a_vouching_profile_url_is_trusted_once_its_id_answers_for_itself(world, http_mock):
    peer_instance('social.example')
    profile, canonical = serve_profile(http_mock, 'ann', fields=VOUCH)

    user_id = verified_credit_user('https://social.example/@ann', world.podcast)

    assert db.session.get(User, user_id).ap_profile_id == 'https://social.example/u/ann'
    assert profile.call_count == 1 and canonical.call_count == 1


def test_a_document_claiming_a_victims_id_links_and_touches_nothing(world, http_mock):
    victim = ann_user({})   # an existing account; its real document does not vouch
    victim.title = 'The Real Ann'
    db.session.commit()
    http_mock.get('https://evil.example/@ann').respond(json=peer_actor_json(
        'Person', name='ann', server='social.example', fields={**VOUCH, 'name': 'Spoofed Ann'}))
    http_mock.get(victim.ap_profile_id).respond(json=peer_actor_json('Person', name='ann', server='social.example'))
    before = rows()

    assert verified_credit_user('https://evil.example/@ann', world.podcast) is None
    assert rows() == before
    db.session.expire_all()
    assert db.session.get(User, victim.id).title == 'The Real Ann'


def test_a_document_claiming_an_unknown_victims_id_creates_nothing(world, http_mock):
    peer_instance('social.example')
    http_mock.get('https://evil.example/@ann').respond(json=peer_actor_json('Person', name='ann',
                                                                            server='social.example', fields=VOUCH))
    http_mock.get('https://social.example/u/ann').respond(404)
    before = rows()

    assert verified_credit_user('https://evil.example/@ann', world.podcast) is None
    assert rows() == before


def test_a_person_that_does_not_vouch_creates_nothing(world, http_mock):
    peer_instance('social.example')
    serve_profile(http_mock, 'ann')
    before = rows()

    assert verified_credit_user('https://social.example/@ann', world.podcast) is None
    assert rows() == before


@pytest.mark.parametrize('actor_type', ['Group', 'Podcast'])
def test_a_group_or_another_podcast_creates_no_rows_even_when_it_links_the_podcast(world, http_mock, actor_type):
    peer_instance('social.example')
    document = peer_actor_json(actor_type, name='club', server='social.example',
                               fields={**VOUCH, 'name': 'Club', 'inbox': 'https://social.example/club/inbox',
                                       'outbox': 'https://social.example/club/outbox'})
    http_mock.get(document['id']).respond(json=document)
    before = rows()

    assert verified_credit_user(document['id'], world.podcast) is None
    assert rows() == before


def test_an_ordinary_web_page_resolves_to_nobody(world, http_mock):
    http_mock.get('https://ben.example/about').respond(200, text='<html>Ben</html>')

    assert verified_credit_user('https://ben.example/about', world.podcast) is None


def test_an_oversized_actor_document_is_not_read(world, http_mock):
    http_mock.get('https://social.example/@ann').respond(200, content=b'{' + b' ' * (credits.MAX_ACTOR_BYTES + 1))

    assert verified_credit_user('https://social.example/@ann', world.podcast) is None


def test_plain_http_and_banned_instances_are_never_linked(world, http_mock):
    make_banned_instance('banned.example')   # no route: a banned host is never fetched (respx would refuse it)

    assert verified_credit_user('http://social.example/@ann', world.podcast) is None
    assert verified_credit_user('https://banned.example/@ann', world.podcast) is None
    assert User.query.filter_by(ap_domain='banned.example').count() == 0
    assert verified_credit_user(None, world.podcast) is None


def test_a_local_account_whose_fields_link_the_podcast_is_verified_without_a_fetch(app, world, http_mock):
    host = User(user_name='annhost', email='ann@example.invalid', verified=True, banned=False, instance_id=1)
    db.session.add(host)
    db.session.flush()
    db.session.add(UserExtraField(user_id=host.id, label='Podcast', text=ACTOR))
    db.session.commit()

    assert verified_credit_user(f"https://{app.config['SERVER_NAME']}/u/annhost", world.podcast) == host.id
    assert len(http_mock.calls) == 0


def test_a_known_remote_account_whose_fields_link_the_podcast_is_verified_without_a_fetch(world, http_mock):
    ann = ann_user({'attachment': [{'type': 'PropertyValue', 'name': 'Show', 'value': ACTOR}]})

    assert verified_credit_user(ann.ap_profile_id, world.podcast) == ann.id
    assert len(http_mock.calls) == 0


FEED_TWICE = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel><title>My Podcast</title>
<podcast:person role="host" href="https://social.example/@ann">Ann Host</podcast:person>
<item><link>{EP1}</link><guid>{EP1}</guid>
<podcast:person role="guest" href="https://social.example/@ann">Ann Again</podcast:person>
<podcast:person role="guest" href="https://social.example/@ann/">Ann Slash</podcast:person></item>
</channel></rss>""".encode()


def test_duplicate_hrefs_are_fetched_once(world, http_mock):
    peer_instance('social.example')
    http_mock.get(FEED_URL).respond(200, content=FEED_TWICE)
    profile, canonical = serve_profile(http_mock, 'ann', fields=VOUCH)

    fetch_episode_credits_task(world.post.id, EP1)

    assert profile.call_count == 1 and canonical.call_count == 1
    saved = stored(world.post.id)['podcast']['credits']
    assert len({c['user_id'] for c in saved}) == 1 and all(c.get('verified') for c in saved)


def test_the_task_links_only_vouching_credits_with_one_request_per_distinct_href(world, http_mock):
    peer_instance('social.example')
    http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes(), headers={'Content-Type': 'application/rss+xml'})
    serve_profile(http_mock, 'ann', fields=VOUCH)                                   # vouches
    http_mock.get('https://ben.example/about').respond(200, text='<html>Ben</html>')   # a web page
    serve_profile(http_mock, 'cara')                                                # does not vouch
    users_before = User.query.count()

    fetch_episode_credits_task(world.post.id, EP1)

    saved = stored(world.post.id)['podcast']['credits']
    ann = User.query.filter_by(ap_profile_id='https://social.example/u/ann').one()
    assert [(c['name'], c['role'], c['user_id']) for c in saved] == [
        ('Ann Host', 'host', ann.id), ('Ben Cohost', 'host', None), ('Cara Guest', 'guest', None)]
    assert saved[0]['verified'] is True and 'verified' not in saved[1] and 'verified' not in saved[2]
    assert saved[2]['profile_url'] is None
    assert User.query.count() == users_before + 1                      # Cara, who does not vouch, was not created
    # the feed, one fetch per distinct href, and one canonical-id fetch for each of the two profile URLs
    assert len(http_mock.calls) == 1 + 3 + 2


def ann_user(document):
    peer_instance('social.example')
    return actor_json_to_model(peer_actor_json('Person', name='ann', server='social.example', fields=document),
                               'ann', 'social.example')


@pytest.mark.parametrize('fields,expected', [
    ({'alsoKnownAs': [ACTOR + '/']}, True),
    ({'alsoKnownAs': ['https://POD.example/@mypodcast']}, True),
    ({'url': [{'type': 'Link', 'href': ACTOR}]}, True),
    ({'attachment': [{'type': 'PropertyValue', 'name': 'Show',
                      'value': f'<a href="{ACTOR}" rel="me">My Podcast</a>'}]}, True),
    ({'attachment': [{'type': 'Link', 'href': ACTOR}]}, True),
    ({'alsoKnownAs': ['https://pod.example/@other']}, False),
    ({'attachment': [{'type': 'PropertyValue', 'name': 'Show', 'value': 'https://pod.example/@mypodcastfake'}]}, False),
    ({}, False)])
def test_a_profile_vouches_when_it_links_the_podcast(world, http_mock, fields, expected):
    ann = ann_user({})
    document = peer_actor_json('Person', name='ann', server='social.example', fields=fields)
    http_mock.get(ann.ap_profile_id).respond(json=document)

    assert credit_vouches(ann.id, world.podcast) is expected


def test_a_failed_vouch_fetch_is_not_a_vouch(world, http_mock):
    ann = ann_user({})
    http_mock.get(ann.ap_profile_id).respond(500)

    assert credit_vouches(ann.id, world.podcast) is False


@pytest.mark.parametrize('answer', [dict(status_code=500), dict(status_code=200, text='<html>not a feed</html>'),
                                    dict(status_code=200, content=b'<rss>' + b' ' * MAX_FEED_BYTES + b'</rss>')])
def test_a_broken_or_oversized_feed_leaves_no_credits(world, http_mock, answer):
    http_mock.get(FEED_URL).respond(**answer)

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None


def test_a_podcast_without_a_feed_fetches_nothing(world, http_mock):
    world.community.rss_url = None
    db.session.commit()

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None


def test_storing_keeps_other_extensions(world):
    world.post.extensions = {'other': 1}
    db.session.commit()

    store_credits(world.post, [{'name': 'Ann Host', 'role': 'host', 'image': None, 'profile_url': None,
                                'user_id': None}])

    assert stored(world.post.id) == {'other': 1, 'podcast': {'credits': [
        {'name': 'Ann Host', 'role': 'host', 'image': None, 'profile_url': None, 'user_id': None}]}}


def test_an_ingested_episode_announcement_gets_its_credits(world, http_mock, monkeypatch):
    episode = http_mock.get(EP1).respond(404)   # no audio this time; the credits do not depend on it
    feed = http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes())
    monkeypatch.setattr(credits, 'verified_credit_user', lambda profile_url, podcast: None)
    note_id = f'{ACTOR}/posts/2'
    activity = {'id': f'{note_id}/activity', 'type': 'Create', 'to': [PUBLIC], 'cc': [],
                'object': {'id': note_id, 'type': 'Note', 'attributedTo': ACTOR, 'to': [PUBLIC], 'cc': [],
                           'content': f'<a href="{EP1}">Episode 1: Hello</a><br/><p>New episode is out!</p>'}}

    post = create_post(False, world.community, activity, world.podcast)

    assert episode.called and feed.called
    assert [c['name'] for c in stored(post.id)['podcast']['credits']] == ['Ann Host', 'Ben Cohost', 'Cara Guest']


def test_a_lookup_that_blows_up_costs_only_that_credit_its_link(world, http_mock, monkeypatch):
    peer_instance('social.example')
    http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes())
    serve_profile(http_mock, 'ann', fields=VOUCH)
    http_mock.get('https://ben.example/about').respond(200, text='<html>Ben</html>')
    serve_profile(http_mock, 'cara', fields=VOUCH)
    real = credits.ap_util.actor_json_to_model

    def create(document, address, server):
        if address == 'ann':
            raise KeyError('malformed actor')
        return real(document, address, server)

    monkeypatch.setattr(credits.ap_util, 'actor_json_to_model', create)

    fetch_episode_credits_task(world.post.id, EP1)

    saved = stored(world.post.id)['podcast']['credits']
    cara = User.query.filter_by(ap_profile_id='https://social.example/u/cara').one()
    assert [(c['name'], c['user_id']) for c in saved] == [('Ann Host', None), ('Ben Cohost', None),
                                                          ('Cara Guest', cara.id)]


def test_a_feed_without_a_content_length_is_not_read_past_the_cap(world, http_mock):
    chunk = b' ' * 65536
    pulled = []

    def body():
        yield b'<rss>'
        for _ in range(200):   # 12.5 MB on offer
            pulled.append(1)
            yield chunk

    http_mock.get(FEED_URL).respond(200, content=body())

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None
    assert len(pulled) * len(chunk) <= MAX_FEED_BYTES + 2 * len(chunk)


def test_a_feed_trickled_past_the_deadline_is_abandoned(app, http_mock, monkeypatch):
    now = [1000.0]
    real_time = __import__('time')
    monkeypatch.setattr('app.utils.time', SimpleNamespace(monotonic=lambda: now[0],
                                                          **{n: getattr(real_time, n) for n in dir(real_time)
                                                             if n != 'monotonic'}))
    pulled = []

    def trickle():
        for _ in range(1000):
            pulled.append(1)
            now[0] += 5   # five fake seconds per byte
            yield b'x'

    http_mock.get(FEED_URL).respond(200, content=trickle())

    assert get_request_capped(FEED_URL, MAX_FEED_BYTES, max_seconds=15) == (200, None)
    assert len(pulled) <= 5


def test_a_compressed_feed_is_refused_undecoded(app, http_mock):
    route = http_mock.get(FEED_URL).respond(200, content=gzip.compress(b'<rss></rss>'),
                                            headers={'Content-Encoding': 'gzip'})

    assert get_request_capped(FEED_URL, MAX_FEED_BYTES) == (200, None)
    assert route.calls.last.request.headers['Accept-Encoding'] == 'identity'
