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
