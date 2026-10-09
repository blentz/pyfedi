"""Interop D24 / D12 workaround `castopod_person_is_podcast`: real Castopod servers (1.13.4-1.15.5) publish a
podcast's actor as type `Person`, not `Podcast`. Castopod federates only podcasts, so a Person whose own host runs
Castopod is a podcast: it gets a User and a Community twin, and its episode Notes go to the twin."""
from types import SimpleNamespace

import httpx
import pytest
from cachelib import SimpleCache

from app import cache, db
from app.activitypub.routes import process_new_content
from app.activitypub.util import actor_json_to_model, find_actor_or_create, refresh_user_profile_task
from app.discovery.podcast import podcast_community_for
import app.interop.workarounds as workarounds
from app.interop.workarounds import WORKAROUNDS, is_castopod_podcast
from app.models import Community, Instance, Post, User
from tests.factories import make_instance, make_site, seed_community_owner

CASTO = 'casto.example'
MASTO = 'masto.example'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
PEM = '-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n'


def castopod_person(name='mypodcast', server=CASTO, **fields):
    """A Castopod 1.13-1.15 podcast actor document, trimmed from https://casto.bitcoinaudible.de/@BitcoinAudibleDE:
    type Person, no rssFeed, Castopod's nodeInfo2Url."""
    actor = f'https://{server}/@{name}'
    document = {'@context': ['https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'],
                'id': actor, 'type': 'Person', 'to': [PUBLIC], 'name': 'My Podcast', 'preferredUsername': name,
                'summary': '<p>A podcast about the fediverse.</p>',
                'inbox': f'{actor}/inbox', 'outbox': f'{actor}/outbox', 'followers': f'{actor}/followers',
                'url': actor, 'nodeInfo2Url': f'https://{server}/.well-known/x-nodeinfo2',
                'publicKey': {'id': f'{actor}#main-key', 'owner': actor, 'publicKeyPem': PEM}}
    document.update(fields)
    return document


def episode(author, note_id):
    return {'id': f'{note_id}/activity', 'type': 'Create', 'actor': author.ap_profile_id, 'to': [PUBLIC], 'cc': [],
            'object': {'id': note_id, 'type': 'Note', 'content': '<p>new episode</p>',
                       'attributedTo': author.ap_profile_id, 'to': [PUBLIC], 'cc': []}}


@pytest.fixture
def world(app, db_session):
    seed_community_owner('local.example')   # instance 1 and user 1, which the microblogs community needs
    make_site()
    return SimpleNamespace(casto=make_instance(CASTO, 'castopod'), masto=make_instance(MASTO, 'mastodon'))


def test_the_workaround_is_registered_with_its_versions_and_reason():
    entry = WORKAROUNDS['castopod_person_is_podcast']
    assert entry.software == 'castopod'
    assert entry.versions == ('1.13.4', '1.15.5')
    assert entry.reason and entry.observed


def test_a_person_on_a_castopod_server_gets_a_user_and_a_twin(world):
    user = actor_json_to_model(castopod_person(), 'mypodcast', CASTO)

    assert isinstance(user, User) and user.bot is False
    twin = podcast_community_for(user)
    assert twin is not None and twin.ap_profile_id == user.ap_profile_id
    assert twin.ap_outbox_url == f'https://{CASTO}/@mypodcast/outbox'


def test_an_episode_from_a_castopod_person_lands_in_its_twin(world):
    user = actor_json_to_model(castopod_person(), 'mypodcast', CASTO)

    process_new_content(user, None, False, episode(user, f'https://{CASTO}/@mypodcast/posts/1'), False)

    post = Post.query.one()
    assert post.community_id == podcast_community_for(user).id
    assert post.user_id == user.id


def test_a_person_on_a_mastodon_server_stays_a_person(world):
    user = actor_json_to_model(castopod_person(name='alice', server=MASTO), 'alice', MASTO)

    assert Community.query.filter(Community.ap_profile_id == user.ap_profile_id).count() == 0
    process_new_content(user, None, False, episode(user, f'https://{MASTO}/@alice/posts/1'), False)
    assert Post.query.one().community.name == 'microblogs'


def test_an_actor_on_another_host_than_the_castopod_server_is_not_a_podcast(world):
    assert is_castopod_podcast(castopod_person(server='elsewhere.example'), world.casto) is False
    assert is_castopod_podcast(castopod_person(), world.casto) is True


def test_a_service_on_a_castopod_server_is_not_a_podcast(world):
    assert is_castopod_podcast(castopod_person(type='Service'), world.casto) is False


def test_the_podcast_type_is_still_a_podcast_anywhere(world):
    assert is_castopod_podcast(castopod_person(type='Podcast'), world.masto) is True
    assert is_castopod_podcast(castopod_person(type='Podcast'), None) is True
    assert is_castopod_podcast(castopod_person(), None) is False


def test_unknown_software_is_read_from_nodeinfo_once_and_stored(app, db_session, http_mock):
    instance = make_instance(CASTO, 'unknown')
    well_known = http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').respond(json={'links': [
        {'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0', 'href': f'https://{CASTO}/nodeinfo/2.0'}]})
    node = http_mock.get(f'https://{CASTO}/nodeinfo/2.0').respond(json={'software': {'name': 'Castopod'}})

    assert is_castopod_podcast(castopod_person(), instance) is True
    assert is_castopod_podcast(castopod_person(), instance) is True

    assert well_known.call_count == 1 and node.call_count == 1
    assert db.session.get(Instance, instance.id).software == 'castopod'


def test_unknown_software_falls_back_to_nodeinfo2_which_castopod_1_13_serves(app, db_session, http_mock):
    """casto.bitcoinaudible.de (Castopod 1.13.5) answers /.well-known/nodeinfo with 404 `""` and serves only
    /.well-known/x-nodeinfo2, the actor's nodeInfo2Url."""
    instance = make_instance(CASTO, 'unknown')
    http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').respond(404, json='')
    node2 = http_mock.get(f'https://{CASTO}/.well-known/x-nodeinfo2').respond(json={
        'version': '1.0', 'server': {'baseUrl': f'https://{CASTO}/', 'name': 'Castopod', 'software': 'Castopod',
                                     'version': '1.13.5'}, 'protocols': ['activitypub']})

    assert is_castopod_podcast(castopod_person(), instance) is True
    assert node2.call_count == 1
    assert db.session.get(Instance, instance.id).software == 'castopod'


def test_unreadable_nodeinfo_leaves_the_person_a_person(app, db_session, http_mock):
    instance = make_instance(CASTO, 'unknown')
    http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').mock(side_effect=httpx.ConnectError('down'))

    assert is_castopod_podcast(castopod_person(), instance) is False
    assert db.session.get(Instance, instance.id).software == 'unknown'


def test_a_dormant_instance_is_not_asked_for_nodeinfo(app, db_session, http_mock):
    instance = make_instance(CASTO, 'unknown')
    instance.dormant = True
    db.session.commit()

    assert is_castopod_podcast(castopod_person(), instance) is False


def known_person(world):
    """A Castopod podcast stored before this workaround: a User with no twin."""
    world.casto.software = 'unknown-at-the-time'
    db.session.commit()
    user = actor_json_to_model(castopod_person(), 'mypodcast', CASTO)
    assert podcast_community_for(user) is None
    world.casto.software = 'castopod'
    db.session.commit()
    return user


def test_a_community_lookup_repairs_a_stored_podcast_user_without_a_twin(world, http_mock):
    user = known_person(world)
    http_mock.get(f'https://{CASTO}/@mypodcast').respond(json=castopod_person())

    community = find_actor_or_create(f'https://{CASTO}/@mypodcast', community_only=True)

    assert isinstance(community, Community)
    assert community.ap_profile_id == user.ap_profile_id and community.user_id == user.id
    assert User.query.filter(User.ap_profile_id == user.ap_profile_id).count() == 1


@pytest.mark.parametrize('flag', ['banned', 'deleted'])
def test_a_banned_or_deleted_podcast_user_still_yields_no_community(world, flag):
    user = known_person(world)
    setattr(user, flag, True)
    db.session.commit()

    assert find_actor_or_create(f'https://{CASTO}/@mypodcast', community_only=True) is None
    assert Community.query.filter(Community.ap_profile_id == user.ap_profile_id).count() == 0


def test_a_refresh_of_a_stored_castopod_person_creates_its_twin(world):
    user = known_person(world)

    refresh_user_profile_task(user.id, castopod_person())

    assert podcast_community_for(db.session.get(User, user.id)) is not None


# Security review of a035710ec: a host whose nodeinfo fails must not cost a blocking request per actor.

@pytest.fixture
def real_cache(app, monkeypatch):
    """conftest's NullCache forgets every write; a real cache for the remembered failure."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    return cache


def three_people(server=CASTO):
    for name in ('one', 'two', 'three'):
        actor_json_to_model(castopod_person(name=name, server=server), name, server)


def test_a_failing_nodeinfo_is_read_once_across_three_actors(app, db_session, http_mock, real_cache):
    instance = make_instance(CASTO, 'unknown')
    well_known = http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').mock(side_effect=httpx.ConnectError('down'))

    three_people()

    assert well_known.call_count == 1
    assert Community.query.count() == 0
    assert db.session.get(Instance, instance.id).software == 'unknown'


def test_a_remembered_failure_expires_and_nodeinfo_is_read_again(app, db_session, http_mock, real_cache):
    make_instance(CASTO, 'unknown')
    well_known = http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').mock(side_effect=httpx.ConnectError('down'))
    three_people()

    real_cache.delete(f'interop:nodeinfo-failed:{CASTO}')   # the 24-hour window has passed
    actor_json_to_model(castopod_person(name='four'), 'four', CASTO)

    assert well_known.call_count == 2


def test_the_failure_is_remembered_for_a_day(app, db_session, http_mock, monkeypatch):
    make_instance(CASTO, 'unknown')
    http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').mock(side_effect=httpx.ConnectError('down'))
    stored = {}
    monkeypatch.setattr(workarounds.cache, 'set', lambda key, value, timeout=None: stored.update({key: timeout}))

    actor_json_to_model(castopod_person(), 'mypodcast', CASTO)

    # flask-caching 2.5 sends memoized lookups through the same cache.set: keep this test to its key
    assert {k: v for k, v in stored.items() if k.startswith('interop:')} == {f'interop:nodeinfo-failed:{CASTO}': 24 * 60 * 60}


def test_a_successful_read_is_not_repeated_across_three_actors(app, db_session, http_mock, real_cache):
    make_instance(CASTO, 'unknown')
    well_known = http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').respond(json={'links': [
        {'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0', 'href': f'https://{CASTO}/nodeinfo/2.0'}]})
    http_mock.get(f'https://{CASTO}/nodeinfo/2.0').respond(json={'software': {'name': 'castopod'}})

    three_people()

    assert well_known.call_count == 1
    assert Community.query.count() == 3


def test_the_read_is_capped_at_five_seconds(app, db_session, monkeypatch):
    instance = make_instance(CASTO, 'unknown')
    calls = []
    monkeypatch.setattr(workarounds, 'remote_instance_software',
                        lambda url, **kwargs: calls.append(kwargs) or 'castopod')

    is_castopod_podcast(castopod_person(), instance)

    assert calls == [{'timeout': 5}]


def test_a_capped_nodeinfo_read_gives_each_request_only_the_time_left(app, monkeypatch):
    import app.utils as app_utils
    seen = []

    def capped(uri, max_bytes, headers=None, max_seconds=15):
        seen.append(max_seconds)
        return 200, (b'{"links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0", "href": "https://x/n"}]}'
                     if len(seen) == 1 else b'{"software": {"name": "Castopod"}}')

    monkeypatch.setattr(app_utils, 'get_request_capped', capped)
    monkeypatch.setattr(app_utils, 'get_request', lambda *a, **k: pytest.fail('uncapped get_request'))

    assert app_utils.remote_instance_software('https://x', timeout=5) == 'castopod'
    assert len(seen) == 2 and all(0 < seconds <= 5 for seconds in seen)


# Owner ruling: a podcast posting into its own twin is exempt from the new-account cap ("3 posts in the first 24h").

def a_fresh_podcast_with_five_posts():
    user = actor_json_to_model(castopod_person(), 'mypodcast', CASTO)
    user.post_count = 5
    db.session.commit()
    assert user.created_very_recently()
    return user


def test_a_fresh_podcast_past_the_new_account_cap_may_post_into_its_twin(world):
    from app.utils import can_create_post
    user = a_fresh_podcast_with_five_posts()

    assert can_create_post(user, podcast_community_for(user)) is True


def test_a_fresh_podcast_past_the_cap_may_not_post_into_another_community(world):
    from app.utils import can_create_post
    from tests.factories import make_community
    user = a_fresh_podcast_with_five_posts()

    assert can_create_post(user, make_community('elsewhere')) is False


def test_an_ordinary_new_remote_user_stays_capped(world):
    from app.utils import can_create_post
    from tests.factories import make_community
    alice = actor_json_to_model(castopod_person(name='alice', server=MASTO), 'alice', MASTO)
    alice.post_count = 5
    db.session.commit()

    assert can_create_post(alice, make_community('elsewhere')) is False
    alice.post_count = 2
    db.session.commit()
    assert can_create_post(alice, make_community('another')) is True


def test_a_banned_instance_still_refuses_the_podcast_in_its_twin(world, monkeypatch):
    import app.utils as app_utils
    user = a_fresh_podcast_with_five_posts()
    monkeypatch.setattr(app_utils, 'instance_banned', lambda domain: True)

    assert app_utils.can_create_post(user, podcast_community_for(user)) is False


# Owner ruling: a podcast's own episode Note in its twin is titled with the episode link's text, not the text up to
# its first period. Trimmed from https://casto.bitcoinaudible.de/@BitcoinAudibleDE/posts/34e64381-... (2026-10-04).

BA = 'casto.bitcoinaudible.de'
BA_ACTOR = f'https://{BA}/@BitcoinAudibleDE'
BA_EPISODE = f'{BA_ACTOR}/episodes/208-martin-connor-bitcoin-das-ultimative-kollateral'
BA_NOTE = {'@context': 'https://www.w3.org/ns/activitystreams',
           'id': f'{BA_ACTOR}/posts/34e64381-eebb-4b68-b5af-7f97691c389c', 'type': 'Note',
           'content': f'<a href="{BA_EPISODE}">208. Martin Connor - Bitcoin, das ultimative Kollateral</a><br/>',
           'published': '2026-02-04T10:40:24+00:00', 'to': [PUBLIC], 'cc': [f'{BA_ACTOR}/followers'],
           'attributedTo': BA_ACTOR}


@pytest.fixture
def bitcoin_audible(world):
    make_instance(BA, 'castopod')
    return actor_json_to_model(castopod_person(name='BitcoinAudibleDE', server=BA), 'BitcoinAudibleDE', BA)


def deliver(author, note):
    note = dict(note, attributedTo=author.ap_profile_id)
    process_new_content(author, None, False, {'id': f"{note['id']}/activity", 'type': 'Create',
                                              'actor': author.ap_profile_id, 'to': [PUBLIC], 'cc': [],
                                              'object': note}, False)
    return Post.query.filter_by(ap_id=note['id']).one()


def test_a_podcast_episode_is_titled_with_its_episode_link_text(bitcoin_audible, http_mock):
    http_mock.get(BA_EPISODE).respond(404)   # C1's audio fetch

    post = deliver(bitcoin_audible, BA_NOTE)

    assert post.community_id == podcast_community_for(bitcoin_audible).id
    assert post.title == '208. Martin Connor - Bitcoin, das ultimative Kollateral'


def test_the_episode_link_may_be_the_notes_url_and_its_text_is_collapsed_and_capped(bitcoin_audible):
    page = f'https://{BA}/somewhere/else'
    note = dict(BA_NOTE, id=f'{BA_ACTOR}/posts/2', url=page,
                content=f'<p><a href="https://{BA}/other">Not. This</a> <a href="{page}">  Folge\n 209.  '
                        + 'x' * 300 + '</a></p>')

    post = deliver(bitcoin_audible, note)

    from app.utils import shorten_string
    assert post.title == shorten_string('Folge 209. ' + 'x' * 300, 255)   # capped as Post.new caps a warning title


def test_a_podcast_note_without_an_episode_link_keeps_todays_title(bitcoin_audible):
    note = dict(BA_NOTE, id=f'{BA_ACTOR}/posts/3',
                content=f'<p><a href="https://{BA}/@BitcoinAudibleDE/about">208. About us</a> and more</p>')

    assert deliver(bitcoin_audible, note).title == '208'


def test_a_persons_note_with_the_same_shape_keeps_todays_title(world, http_mock):
    alice = actor_json_to_model(castopod_person(name='alice', server=MASTO), 'alice', MASTO)
    episode_page = f'https://{MASTO}/@alice/episodes/208-x'
    http_mock.get(episode_page).respond(404)   # C1's audio fetch looks at any author's own-host episode link
    note = dict(BA_NOTE, id=f'https://{MASTO}/@alice/posts/1',
                content=f'<a href="{episode_page}">208. Martin Connor - Bitcoin</a><br/>')

    post = deliver(alice, note)

    assert post.community.name == 'microblogs' and post.title == '208'


def test_an_update_of_a_podcast_episode_keeps_the_episode_title(bitcoin_audible, http_mock):
    from app.activitypub.util import update_post_from_activity
    http_mock.get(BA_EPISODE).respond(404)
    post = deliver(bitcoin_audible, BA_NOTE)

    update_post_from_activity(post, {'id': f"{BA_NOTE['id']}/update", 'type': 'Update',
                                     'object': dict(BA_NOTE, attributedTo=bitcoin_audible.ap_profile_id)})

    assert post.title == '208. Martin Connor - Bitcoin, das ultimative Kollateral'


def _listed(host, platform='castopod'):
    from tests.discovery_fixtures import add_entry
    add_entry('Listed', platform=platform, host=host, url=f'https://{host}/@listed')


def test_unreadable_nodeinfo_on_a_host_the_castopod_index_lists_is_castopod(app, db_session, http_mock):
    """Many Castopod servers answer neither nodeinfo nor NodeInfo2 (hell.cloud: 126 podcasts in one sync never got
    a community). index.castopod.org lists only Castopod servers, so a host it lists is one, and that is stored."""
    instance = make_instance(CASTO, 'unknown')
    http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').mock(side_effect=httpx.ConnectError('down'))
    _listed(CASTO)

    assert is_castopod_podcast(castopod_person(), instance) is True
    # third-party directory data is never stored: nodeinfo is asked again once the failure expires, and wins
    assert db.session.get(Instance, instance.id).software == 'unknown'


def test_a_remembered_failure_still_consults_the_directory(app, db_session, http_mock, real_cache):
    instance = make_instance(CASTO, 'unknown')
    real_cache.set(f'interop:nodeinfo-failed:{CASTO}', True)
    _listed(CASTO)

    assert workarounds.instance_software(instance) == 'castopod'


def test_a_directory_entry_for_another_platform_or_host_does_not_count(app, db_session, http_mock, real_cache):
    instance = make_instance(CASTO, 'unknown')
    real_cache.set(f'interop:nodeinfo-failed:{CASTO}', True)
    _listed(CASTO, platform='mastodon')
    _listed('elsewhere.example')

    assert workarounds.instance_software(instance) == ''


def test_a_readable_nodeinfo_wins_over_the_directory(app, db_session, http_mock):
    instance = make_instance(CASTO, 'unknown')
    http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').respond(json={'links': [
        {'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0', 'href': f'https://{CASTO}/nodeinfo/2.0'}]})
    http_mock.get(f'https://{CASTO}/nodeinfo/2.0').respond(json={'software': {'name': 'mastodon'}})
    _listed(CASTO)

    assert workarounds.instance_software(instance) == 'mastodon'


def test_a_directory_answer_is_not_stored_so_a_later_readable_nodeinfo_corrects_it(app, db_session, http_mock,
                                                                                    real_cache):
    """Security review of fc632a22c: a wrong or hostile directory listing must not misclassify a host for good."""
    instance = make_instance(CASTO, 'unknown')
    real_cache.set(f'interop:nodeinfo-failed:{CASTO}', True)
    _listed(CASTO)
    assert workarounds.instance_software(instance) == 'castopod'

    real_cache.delete(f'interop:nodeinfo-failed:{CASTO}')
    http_mock.get(f'https://{CASTO}/.well-known/nodeinfo').respond(json={'links': [
        {'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0', 'href': f'https://{CASTO}/nodeinfo/2.0'}]})
    http_mock.get(f'https://{CASTO}/nodeinfo/2.0').respond(json={'software': {'name': 'mastodon'}})

    assert workarounds.instance_software(instance) == 'mastodon'
    assert db.session.get(Instance, instance.id).software == 'mastodon'
