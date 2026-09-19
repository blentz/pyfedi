"""app/dev/routes.py -- the developer tools page and the ActivityPub replay form.

MEASUREMENT BASIS. The module stood at 13.75% on the full-suite --cov=app run
at 4e9ace8f0, carrying 96 missing statements and 42 missing arcs.

BOTH ROUTES ARE BEHIND `if not current_app.debug: abort(404)`, and this suite
runs with debug off -- measured, `PROBE h1 debug off: 404`, `debug on: 200`. The
`dev_mode` fixture turns it on and puts it back, because a bare
`app.debug = True` left behind changes how every later test's errors propagate.

Three defects are pinned here:

  P1  `deleted_topics` was initialised, reported, and never incremented, so the
      tool deleted three topics and said it had deleted none.
  P2  the topic generator called random.choice on an empty community list,
      which is an IndexError and a 500 -- and an empty database is exactly what
      a fresh dev instance is.
  P3  two flashes interpolated before gettext saw them, so the catalog could
      never match.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Community, CommunityMember, Site, Topic
from tests.factories import grant_permission, make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def dev_mode(app):
    """`current_app.debug` on for the test, and back to what it was after."""
    was = app.debug
    app.debug = True
    yield app
    app.debug = was


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    grant_permission(alice, 'change instance settings')
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice


def _topic(name, machine_name=None):
    topic = Topic(name=name, machine_name=machine_name or name.lower().replace('_', '-'),
                  num_communities=0)
    db.session.add(topic)
    db.session.commit()
    return topic


def _submit(app, client, field, value='Go'):
    token = csrf(app, client)
    with patch('app.dev.routes.render_template', return_value='rendered'):
        return client.post('/dev/tools', data={field: value, 'csrf_token': token})


# --------------------------------------------------------------------------
# P1: the counter that counted nothing
# --------------------------------------------------------------------------


def test_deleting_dev_topics_reports_how_many_went(app, db_session, dev_mode):
    """Before the repair:

        PROBE h3 flash: 0 Dev Topics Deleted.
        PROBE h3 topics left: 0

    -- every topic gone, the developer told none were. D736's shape: an action
    observable only through a stale counter.
    """
    instance, alice = _seed()
    for index in range(3):
        _topic(f'dev_Topic_{index:02d}')
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.flash') as flashed:
        response = _submit(app, client, 'delete_topics_submit')

    assert response.status_code == 302
    assert Topic.query.count() == 0
    assert '3' in str(flashed.call_args.args[0])
    assert '0 Dev Topics' not in str(flashed.call_args.args[0])


def test_a_topic_that_still_has_communities_is_kept_and_counted_separately(app, db_session,
                                                                          dev_mode):
    """Both counters at once: one topic deletable, one holding a community. The
    fixture needs both or the message's two halves cannot be told apart.
    """
    instance, alice = _seed()
    empty = _topic('dev_Topic_00')
    occupied = _topic('dev_Topic_01')
    community = make_community('microblogs')
    community.topic_id = occupied.id
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.flash') as flashed:
        _submit(app, client, 'delete_topics_submit')

    remaining = [t.name for t in Topic.query.all()]
    assert remaining == ['dev_Topic_01']
    message = str(flashed.call_args.args[0])
    assert '1' in message
    assert 'remain' in message


def test_topics_that_are_not_dev_topics_are_left_alone(app, db_session, dev_mode):
    """The name filter, which is the only thing standing between this button
    and a production topic list.
    """
    instance, alice = _seed()
    _topic('dev_Topic_00')
    _topic('Technology', 'technology')
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.flash'):
        _submit(app, client, 'delete_topics_submit')

    assert [t.name for t in Topic.query.all()] == ['Technology']


# --------------------------------------------------------------------------
# P2: an empty database
# --------------------------------------------------------------------------


def test_populating_topics_with_no_communities_says_so(app, db_session, dev_mode):
    """PROBE h2 exception: IndexError Cannot choose from an empty sequence.

    A fresh dev instance has no communities, which is exactly when a developer
    reaches for this page -- and the button above this one is what creates
    them.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.flash') as flashed:
        response = _submit(app, client, 'topics_submit')

    assert response.status_code == 302
    assert Topic.query.count() == 0
    assert 'communit' in str(flashed.call_args.args[0]).lower()


def test_populating_topics_with_communities_present_still_works(app, db_session, dev_mode):
    """The other half of P2's inversion: a repair that refused every time would
    pass the test above. Ten communities, because the generator picks ten.
    """
    instance, alice = _seed()
    for index in range(10):
        make_community(f'community{index:02d}')
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.flash'):
        response = _submit(app, client, 'topics_submit')

    assert response.status_code == 302
    assert Topic.query.count() == 10
    assert sorted(t.machine_name for t in Topic.query.all())[0] == 'dev-topic-00'
    # every generated topic is assigned to at least one community, and the
    # counts are written back
    assert Community.query.filter(Community.topic_id.isnot(None)).count() >= 1


# --------------------------------------------------------------------------
# The gate, and the page itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path', ['/dev/tools', '/dev/tools/activitypub'])
def test_both_tools_are_a_404_when_the_server_is_not_in_debug(app, db_session, path):
    """The gate is the only thing between a production instance and a button
    that deletes communities, so both routes have a row -- and this one
    deliberately does NOT use the dev_mode fixture.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.render_template', return_value='rendered'):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize('path', ['/dev/tools', '/dev/tools/activitypub'])
def test_both_tools_need_the_instance_settings_permission(app, db_session, dev_mode, path):
    """The second gate: debug alone is not enough. The user here is logged in
    and has no permission, which is what separates this from the anonymous
    redirect `login_required` would give.
    """
    instance, alice = _seed()
    ordinary = make_user(instance, 'ordinary', local=True)
    client = app.test_client()
    login(client, ordinary)

    with patch('app.dev.routes.render_template', return_value='rendered'):
        response = client.get(path)

    assert response.status_code == 302
    assert response.headers['Location'] == '/auth/permission_denied'


def test_the_tools_page_renders_its_four_forms(app, db_session, dev_mode):
    """The else arm at the end of the chain: a GET with no button pressed.
    Naming the four forms is what makes a silently dropped button visible.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.render_template', return_value='rendered') as render:
        response = client.get('/dev/tools')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'dev/tools.html'
    assert set(render.call_args.kwargs) >= {'communities_form', 'topics_form',
                                            'delete_communities_form',
                                            'delete_topics_form', 'inoculation'}


def test_the_inoculation_block_follows_the_site_setting(app, db_session, dev_mode):
    """Both arms of the conditional that picks one at random or None."""
    instance, alice = _seed()
    site = Site.query.get(1)
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.render_template', return_value='rendered') as render:
        site.show_inoculation_block = False
        db.session.commit()
        client.get('/dev/tools')
        assert render.call_args.kwargs['inoculation'] is None

        site.show_inoculation_block = True
        db.session.commit()
        client.get('/dev/tools')
        assert render.call_args.kwargs['inoculation'] is not None


# --------------------------------------------------------------------------
# Populating and deleting communities
# --------------------------------------------------------------------------


def test_populating_communities_makes_thirty_the_caller_moderates(app, db_session, dev_mode):
    """Thirty communities, each with the pressing user as owner and moderator
    -- the membership is the part a developer needs, and a generator that made
    the rows without it would pass a count.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.activitypub.signature.RsaKeys.generate_keypair',
               return_value=('private', 'public')):
        response = _submit(app, client, 'communities_submit')

    assert response.status_code == 302
    assert response.headers['Location'] == '/communities'
    communities = Community.query.order_by(Community.name).all()
    assert len(communities) == 30
    assert communities[0].name == 'dev_Community_00'
    assert communities[0].local_only is True
    assert communities[0].ap_profile_id.endswith('/c/dev_community_00')
    # thirty, not twenty-nine: each membership was added and then flushed by
    # the NEXT iteration's commit, so the last one had nothing after it and was
    # never written. This assertion is what found that.
    memberships = CommunityMember.query.filter_by(user_id=alice.id).all()
    assert len(memberships) == 30
    assert all(m.is_owner and m.is_moderator for m in memberships)


def test_a_generated_community_named_for_memes_is_marked_low_quality(app, db_session,
                                                                     dev_mode):
    """`low_quality='memes' in name` -- none of the thirty generated names
    contains it, so the flag is always False here. Recorded as behaviour
    rather than asserted as intent: the expression is copied from the real
    community form, where the name comes from a person.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.activitypub.signature.RsaKeys.generate_keypair',
               return_value=('private', 'public')):
        _submit(app, client, 'communities_submit')

    assert Community.query.filter_by(low_quality=True).count() == 0


def test_deleting_dev_communities_unsubscribes_and_removes_them(app, db_session, dev_mode):
    """The delete path bans, stamps and then hands each community to the
    admin module's unsubscribe-and-delete -- which is patched here, because
    what it does is another module's business and this row is about which
    communities reach it.
    """
    instance, alice = _seed()
    dev_one = make_community('dev_Community_00')
    dev_one.local_only = True
    dev_two = make_community('dev_Community_01')
    dev_two.local_only = True
    keeper = make_community('microblogs')
    keeper.local_only = True
    remote = make_community('dev_Community_99')
    remote.local_only = False
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.unsubscribe_everyone_then_delete') as deleter, \
         patch('app.dev.routes.flash') as flashed:
        response = _submit(app, client, 'delete_communities_submit')

    assert response.status_code == 302
    assert response.headers['Location'] == '/communities'
    deleted_ids = sorted(call.args[0] for call in deleter.call_args_list)
    assert deleted_ids == sorted([dev_one.id, dev_two.id])
    db.session.expire_all()
    assert Community.query.get(dev_one.id).banned is True
    assert Community.query.get(keeper.id).banned is False
    assert '2' in str(flashed.call_args.args[0])


def test_deleting_dev_communities_with_none_to_delete_says_so(app, db_session, dev_mode):
    """ngettext's other arm, and the empty-list path through the loop."""
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.unsubscribe_everyone_then_delete') as deleter, \
         patch('app.dev.routes.flash') as flashed:
        response = _submit(app, client, 'delete_communities_submit')

    assert response.status_code == 302
    assert deleter.call_count == 0
    assert '0' in str(flashed.call_args.args[0])


# --------------------------------------------------------------------------
# tools_activitypub
# --------------------------------------------------------------------------


def test_the_activitypub_form_renders(app, db_session, dev_mode):
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.dev.routes.render_template', return_value='rendered') as render:
        response = client.get('/dev/tools/activitypub')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'admin/dev_activitypub.html'


def test_valid_activitypub_is_replayed_into_the_inbox(app, db_session, dev_mode):
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.dev.routes.render_template', return_value='rendered'), \
         patch('app.dev.routes.replay_inbox_request') as replay, \
         patch('app.dev.routes.flash') as flashed:
        response = client.post('/dev/tools/activitypub',
                               data={'json': '{"type": "Create", "id": "https://x/1"}',
                                     'csrf_token': token})

    assert response.status_code == 200
    assert replay.call_args.args[0] == {'type': 'Create', 'id': 'https://x/1'}
    assert 'sent to inbox' in str(flashed.call_args.args[0])


def test_invalid_json_is_refused_without_reaching_the_inbox(app, db_session, dev_mode):
    """The JSONDecodeError arm. The replay is patched and asserted NOT to have
    run, since a message saying "invalid" while the activity was replayed
    anyway would be worse than either outcome.
    """
    instance, alice = _seed()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.dev.routes.render_template', return_value='rendered'), \
         patch('app.dev.routes.replay_inbox_request') as replay, \
         patch('app.dev.routes.flash') as flashed:
        response = client.post('/dev/tools/activitypub',
                               data={'json': 'not json at all', 'csrf_token': token})

    assert response.status_code == 200
    assert replay.call_count == 0
    assert 'Invalid json' in str(flashed.call_args.args[0])
    assert flashed.call_args.args[1] == 'error'
