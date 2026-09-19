"""app/topic/routes.py -- the topic page, its feed, and the forms around it.

MEASUREMENT BASIS. The module stood at 48.197% on the full-suite --cov=app run
at c60264813, carrying 96 missing statements and 62 missing arcs.

Two defects are pinned here and repaired together:

  P1  `has_next_page = len(post_ids) > page + 1 * page_length` -- `*` binds
      tighter than `+`, so from page 1 on the reader is offered a next page
      that renders nothing. Page 0 agrees by arithmetic accident, which is why
      it survived. The same line is repaired in app/feed/routes.py.
  P2  a crafted community_id reached int() unguarded and was a 500.

Facts 292-294 apply: render_template is patched for anything that renders,
POST routes need a real CSRF token, and the site fixture supplies g.site.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Community, NotificationSubscription, Post, Site, Topic, User, utcnow
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


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
    """Two local users and an open instance.

    Site.private_instance defaults True (app/models.py:4000) and show_topic is
    decorated with login_required_if_private_instance, so an anonymous test
    would otherwise measure the decorator.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _topic(name='fediverse', parent=None, show_posts_in_children=False):
    topic = Topic(name=name.title(), machine_name=name, num_communities=0,
                  parent_id=parent.id if parent else None,
                  show_posts_in_children=show_posts_in_children)
    db.session.add(topic)
    db.session.commit()
    return topic


def _community_in(topic, name='microblogs', **kwargs):
    community = make_community(name)
    community.topic_id = topic.id
    community.total_subscriptions_count = 1
    for key, value in kwargs.items():
        setattr(community, key, value)
    db.session.commit()
    return community


def _posts(community, author, count, prefix='post'):
    made = []
    for index in range(count):
        made.append(make_post(community, author,
                              ap_id=f'https://test.piefed.local/post/{prefix}{index}',
                              title=f'{prefix} {index}'))
    return made


def _aged(user, days=30):
    """suggest_topics is gated on current_user.trustworthy(), which is false for
    an account created within 7 days whose reputation is under 100 (fact 307).
    """
    user.created = utcnow() - __import__('datetime').timedelta(days=days)
    db.session.commit()
    return user


# --------------------------------------------------------------------------
# P1: the next-page link
# --------------------------------------------------------------------------


@pytest.mark.parametrize('page, total, expects_next', [
    (0, 30, True),    # 30 posts, page 0 shows 20 -- there IS a second page
    (1, 30, False),   # page 1 shows the last 10 -- there is NOT a third
    (1, 45, True),    # page 1 shows 20 of 45 -- there IS a third
])
def test_the_next_link_appears_only_when_a_next_page_exists(app, db_session, page,
                                                            total, expects_next):
    """Before the repair, `page + 1 * page_length` read as `page +
    page_length`, so page 1 of 30 posts offered a page 2 that renders nothing:

        PROBE t1 page=1 total=30 actual_next=True intended_next=False

    Page 0 agrees under both readings, so the middle row is the one that
    pins the defect and the other two keep the repair honest.
    """
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, total)
    app.config['PAGE_LENGTH'] = 20
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/topic/{topic.machine_name}?page={page}')

    assert response.status_code == 200
    assert (render.call_args.kwargs['next_url'] is not None) is expects_next


def test_the_previous_link_appears_on_every_page_but_the_first(app, db_session):
    instance, alice, bob = _seed()
    topic = _topic()
    community = _community_in(topic)
    _posts(community, alice, 45)
    app.config['PAGE_LENGTH'] = 20
    client = app.test_client()

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        client.get(f'/topic/{topic.machine_name}?page=0')
        assert render.call_args.kwargs['prev_url'] is None
        client.get(f'/topic/{topic.machine_name}?page=1')
        assert render.call_args.kwargs['prev_url'] is not None


# --------------------------------------------------------------------------
# P2: a crafted community_id
# --------------------------------------------------------------------------


def _submitter(user):
    """topic_create_post sits behind validation_required AND approval_required
    (fact 292's neighbourhood), so its user needs verified and a private_key.
    """
    user.verified = True
    user.private_key = 'a-private-key'
    db.session.commit()
    return user


def test_a_community_id_that_is_not_a_number_does_not_crash(app, db_session):
    """PROBE t3 exception: ValueError invalid literal for int() with base 10:
    'not-a-number'. A crafted request earns a page, not a traceback -- the
    value reads as absent and the route renders the chooser it already has.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    topic = _topic()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.render_template', return_value='rendered') as render:
        response = client.post(f'/topic/{topic.machine_name}/submit',
                               data={'community_id': 'not-a-number', 'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.args[0] == 'topic/topic_create_post.html'


def test_a_real_community_id_still_redirects_to_the_join_page(app, db_session):
    """The other half of P2's inversion: a repair that dropped the branch would
    pass the test above.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    topic = _topic()
    community = _community_in(topic)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.topic.routes.render_template', return_value='rendered'):
        response = client.post(f'/topic/{topic.machine_name}/submit',
                               data={'community_id': str(community.id), 'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == f'/community/{community.link()}/join_then_add'
