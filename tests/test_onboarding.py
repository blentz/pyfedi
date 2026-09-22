"""The three screens a new account sees: the instance chooser, the content
filters, and the topics.

Sub-project 83, slice E -- `app/auth/onboarding.py`. Five defects, all
measured:

* the "Trump & Musk" filter was created only for somebody who ALREADY had
  one -- never for the new account the screen exists to set up, and twice
  over for anyone who came back to it (D1152);
* `int(topic_id_str)` on `request.form.getlist`, which is whatever was
  posted: `ValueError: invalid literal for int() with base 10: 'nonsense'`
  (D1153);
* onboarding put the account straight into every PRIVATE community carrying
  a chosen topic, with no join request and nobody's approval (D1154);
* `mark_onboarding_as_finished()` was the route's first line, so merely
  looking at the page finished onboarding (D1155);
* and "You have joined some communities relating to those interests." was
  said whether or not anything had been joined (D1156).
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import SUBSCRIPTION_NONMEMBER
from app.models import (Community, CommunityJoinRequest, CommunityMember,
                        Filter, Instance, InstanceChooser, Language, Site,
                        Topic, User)
from tests.factories import (make_community, make_community_ban,
                             make_community_member, make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.enable_nsfw = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    person = make_user(local, 'person', local=True)
    person.verified = True
    person.finished_onboarding = False
    db.session.commit()
    return as_user(app, person), person


def a_topic(name='news', **columns):
    topic = Topic(name=name, machine_name=name, num_communities=0)
    for column, value in columns.items():
        setattr(topic, column, value)
    db.session.add(topic)
    db.session.commit()
    return topic


def in_topic(topic, name='general', **columns):
    community = make_community(name)
    community.topic_id = topic.id
    for column, value in columns.items():
        setattr(community, column, value)
    db.session.commit()
    return community


def remote_community(topic=None, name='faraway', host='remote.example'):
    """`is_local()` is `ap_id is None or ap_profile_id.startswith(SERVER_URL)`,
    and `make_community` leaves `ap_id` unset and `instance_id` at 1, so a
    community is local until both are moved."""
    community = make_community(name, host=host)
    community.ap_id = f'{name}@{host}'
    community.instance_id = make_instance(host).id
    if topic is not None:
        community.topic_id = topic.id
    db.session.commit()
    return community


def filter_form(**overrides):
    data = {'trump_musk_level': 1, 'ignore_bots': 1, 'hide_nsfw': 1,
            'hide_nsfl': 1, 'hide_gen_ai': 1, 'submit': 'Next'}
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# The instance chooser
# --------------------------------------------------------------------------


def test_the_instance_chooser_is_shown_when_it_is_enabled(app, env):
    client, person = env
    language = Language(code='en', name='English')
    db.session.add(language)
    db.session.commit()
    db.session.add(InstanceChooser(domain='other.example', language_id=language.id))
    db.session.commit()

    with patch('app.auth.onboarding.get_setting', return_value=True):
        response = app.test_client().get('/auth/instance_chooser')

    assert response.status_code == 200
    assert b'other.example' in response.data


def test_registration_is_where_the_chooser_sends_you_when_it_is_off(app, env):
    client, person = env

    with patch('app.auth.onboarding.get_setting', return_value=False):
        response = app.test_client().get('/auth/instance_chooser')

    assert response.headers['Location'] == '/auth/register'


# --------------------------------------------------------------------------
# D1152 -- the filter the filter screen did not create
# --------------------------------------------------------------------------


def test_the_filter_screen_needs_an_account(app, env):
    client, person = env

    response = app.test_client().get('/auth/filter_selection')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


def test_choosing_to_be_spared_creates_the_filter(app, env):
    """D1152. The test was `if existing_filters is not None`, so the filter
    was created only for somebody who already had one. Measured: `PROBE ay1
    status: 302 | filters now: []`."""
    client, person = env

    response = client.post('/auth/filter_selection', data=filter_form())

    assert response.headers['Location'] == '/auth/choose_topics'
    made = Filter.query.filter_by(user_id=person.id).all()
    assert [f.title for f in made] == ['Trump & Musk']
    assert made[0].keywords == 'trump\nmusk'
    assert made[0].hide_type == 1


def test_the_filter_is_not_created_twice(app, env):
    """D1152's other half. Measured: `PROBE ay2 filters now: 2 | titles:
    ['Trump & Musk', 'Trump & Musk']`."""
    client, person = env
    db.session.add(Filter(title='Trump & Musk', user_id=person.id,
                          filter_home=True, filter_posts=True,
                          filter_replies=False, hide_type=1,
                          keywords='trump\nmusk'))
    db.session.commit()

    client.post('/auth/filter_selection', data=filter_form())

    assert Filter.query.filter_by(user_id=person.id).count() == 1


def test_bring_it_on_creates_no_filter(app, env):
    """`-1` is "Bring it on", and `>= 0` is what keeps it from being
    filtered."""
    client, person = env

    client.post('/auth/filter_selection', data=filter_form(trump_musk_level=-1))

    assert Filter.query.filter_by(user_id=person.id).count() == 0


def test_the_other_answers_are_written_to_the_account(app, env):
    client, person = env

    client.post('/auth/filter_selection',
                data=filter_form(ignore_bots=1, hide_nsfw=2, hide_nsfl=3,
                                 hide_gen_ai=1))

    assert person.ignore_bots == 1
    assert person.hide_nsfw == 2
    assert person.hide_nsfl == 3
    assert person.hide_gen_ai == 1


def test_the_filter_screen_offers_defaults(app, env):
    """A GET sets the form's own defaults rather than writing anything."""
    client, person = env

    with patch('app.auth.onboarding.render_template',
               return_value='rendered') as rendered:
        response = client.get('/auth/filter_selection')

    assert response.status_code == 200
    form = rendered.call_args.kwargs['form']
    assert form.ignore_bots.data == 1
    assert form.hide_nsfw.data == 1
    assert Filter.query.filter_by(user_id=person.id).count() == 0


def test_an_instance_that_warns_rather_than_hides_starts_you_seeing_it(app,
                                                                      env):
    """`CONTENT_WARNING` or `enable_nsfw` means the default is 0 -- show --
    because the instance labels rather than withholds."""
    client, person = env

    with patch.dict(app.config, {'CONTENT_WARNING': True}):
        with patch('app.auth.onboarding.render_template',
                   return_value='rendered') as rendered:
            client.get('/auth/filter_selection')

    assert rendered.call_args.kwargs['form'].hide_nsfw.data == 0


def test_the_filter_screen_can_be_turned_off(app, env):
    client, person = env

    with patch('app.auth.onboarding.get_setting', return_value=False):
        response = client.post('/auth/filter_selection', data=filter_form())

    assert response.headers['Location'] == '/auth/choose_topics'
    assert Filter.query.filter_by(user_id=person.id).count() == 0


# --------------------------------------------------------------------------
# D1153, D1154, D1155, D1156 -- the topics screen
# --------------------------------------------------------------------------


def test_choosing_a_topic_joins_its_communities(app, env):
    """The feature every guard below must leave working."""
    client, person = env
    topic = a_topic()
    community = in_topic(topic)

    with patch('app.auth.onboarding.flash') as flashed:
        response = client.post('/auth/choose_topics',
                               data={'chosen_topics': str(topic.id)})

    assert response.headers['Location'] == '/home'
    assert CommunityMember.query.filter_by(user_id=person.id,
                                           community_id=community.id).first() \
        is not None
    assert 'joined some communities' in str(flashed.call_args.args[0])


def test_a_private_community_is_not_joined_by_choosing_a_topic(app, env):
    """D1154. `Community.private` is invite-only real access control
    (app/models.py:594) -- a join request and somebody's approval. This put
    the account straight into the membership row. Measured: `PROBE ay4
    status: 302 | member of the private community: True`."""
    client, person = env
    topic = a_topic()
    secret = in_topic(topic, 'secret', private=True)

    with patch('app.auth.onboarding.flash'):
        client.post('/auth/choose_topics', data={'chosen_topics': str(topic.id)})

    assert CommunityMember.query.filter_by(user_id=person.id,
                                           community_id=secret.id).first() is None
    assert CommunityJoinRequest.query.filter_by(user_id=person.id).count() == 0


def test_a_banned_community_is_not_joined_either(app, env):
    client, person = env
    topic = a_topic()
    banned = in_topic(topic, 'banned_one', banned=True)

    with patch('app.auth.onboarding.flash'):
        client.post('/auth/choose_topics', data={'chosen_topics': str(topic.id)})

    assert CommunityMember.query.filter_by(user_id=person.id,
                                           community_id=banned.id).first() is None


def test_a_community_you_are_banned_from_is_not_joined(app, env):
    client, person = env
    topic = a_topic()
    community = in_topic(topic)
    make_community_ban(person, community)
    db.session.commit()

    with patch('app.auth.onboarding.flash'):
        client.post('/auth/choose_topics', data={'chosen_topics': str(topic.id)})

    assert CommunityMember.query.filter_by(user_id=person.id,
                                           community_id=community.id).first() \
        is None


def test_a_community_you_are_already_in_is_not_joined_twice(app, env):
    client, person = env
    topic = a_topic()
    community = in_topic(topic)
    make_community_member(person, community)
    db.session.commit()

    with patch('app.auth.onboarding.flash'):
        client.post('/auth/choose_topics', data={'chosen_topics': str(topic.id)})

    assert CommunityMember.query.filter_by(user_id=person.id,
                                           community_id=community.id).count() == 1


def test_a_remote_community_is_followed_over_the_wire(app, env):
    """A community on another instance needs a Follow sending to it, and a
    join request to carry its id."""
    client, person = env
    topic = a_topic()
    remote = remote_community(topic)

    with patch('app.auth.onboarding.send_community_follow') as followed:
        with patch('app.auth.onboarding.flash'):
            client.post('/auth/choose_topics',
                        data={'chosen_topics': str(topic.id)})

    assert CommunityJoinRequest.query.filter_by(user_id=person.id,
                                                community_id=remote.id).first() \
        is not None
    followed.assert_called_once()


def test_a_topic_id_that_is_not_a_number_is_dropped(app, env):
    """D1153. Measured: `PROBE ay3 outcome: ValueError: invalid literal for
    int() with base 10: 'nonsense'`."""
    client, person = env
    a_topic()

    with patch('app.auth.onboarding.flash'):
        response = client.post('/auth/choose_topics',
                               data={'chosen_topics': 'nonsense'})

    assert response.status_code == 302


def test_joining_nothing_is_not_reported_as_joining_something(app, env):
    """D1156. A topic with no communities in it, or with only communities
    this account may not join, still said "You have joined some
    communities". Measured: `PROBE ay6 said: You have joined some communities
    relating to those interests.`"""
    client, person = env
    topic = a_topic()

    with patch('app.auth.onboarding.flash') as flashed:
        response = client.post('/auth/choose_topics',
                               data={'chosen_topics': str(topic.id)})

    assert response.headers['Location'] == '/communities'
    assert 'did not choose any topics' in str(flashed.call_args.args[0])


def test_choosing_nothing_at_all_offers_the_community_list(app, env):
    client, person = env
    a_topic()

    with patch('app.auth.onboarding.flash') as flashed:
        response = client.post('/auth/choose_topics', data={})

    assert response.headers['Location'] == '/communities'
    assert 'did not choose any topics' in str(flashed.call_args.args[0])


def test_looking_at_the_topics_does_not_finish_onboarding(app, env):
    """D1155. `mark_onboarding_as_finished()` was the route's first line, so
    somebody who opened the page and went elsewhere was never brought back to
    it. Measured: `PROBE ay5 finished_onboarding after a GET: True`."""
    client, person = env
    a_topic()

    response = client.get('/auth/choose_topics')

    assert response.status_code == 200
    assert person.finished_onboarding is False


def test_answering_the_topics_finishes_onboarding(app, env):
    client, person = env
    topic = a_topic()
    in_topic(topic)

    with patch('app.auth.onboarding.flash'):
        client.post('/auth/choose_topics', data={'chosen_topics': str(topic.id)})

    assert person.finished_onboarding is True


def test_an_instance_with_no_topics_sends_you_to_the_communities(app, env):
    """And finishes onboarding, or every login would come back here to be
    asked a question this instance cannot ask."""
    client, person = env

    with patch('app.auth.onboarding.flash') as flashed:
        response = client.get('/auth/choose_topics')

    assert response.headers['Location'] == '/communities'
    assert person.finished_onboarding is True
    assert 'Please join some communities' in str(flashed.call_args.args[0])


def test_the_topics_screen_can_be_turned_off(app, env):
    client, person = env
    a_topic()

    with patch('app.auth.onboarding.get_setting', return_value=False):
        with patch('app.auth.onboarding.flash'):
            response = client.get('/auth/choose_topics')

    assert response.headers['Location'] == '/communities'


# --------------------------------------------------------------------------
# The topic tree
# --------------------------------------------------------------------------


def test_the_tree_carries_three_levels_and_no_more(app, env):
    """"Max depth is 2 (0=root, 1=child, 2=grandchild)" -- a fourth level is
    dropped from the tree itself. The template renders three, so a row that
    reads the page cannot tell the cap from the template."""
    from app.auth.onboarding import topics_for_form

    client, person = env
    root = a_topic('root')
    child = a_topic('child', parent_id=root.id)
    grandchild = a_topic('grandchild', parent_id=child.id)
    a_topic('greatgrandchild', parent_id=grandchild.id)

    with app.test_request_context('/'):
        topic_tree, selections = topics_for_form()

    assert topic_tree[0]['name'] == 'root'
    assert topic_tree[0]['children'][0]['children'][0]['name'] == 'grandchild'
    assert topic_tree[0]['children'][0]['children'][0]['children'] == []


def test_a_topic_for_your_country_is_ticked_to_begin_with(app, env):
    """The selections the form opens with, not merely which topics exist."""
    from app.auth.onboarding import topics_for_form

    client, person = env
    mine = a_topic('local_news', countries=['GB'])
    a_topic('elsewhere', countries=['FR'])
    a_topic('everywhere')

    with app.test_request_context('/'):
        with patch('app.auth.onboarding.get_country', return_value='GB'):
            topic_tree, selections = topics_for_form()

    assert selections == [mine.id]


# --------------------------------------------------------------------------
# send_community_follow
# --------------------------------------------------------------------------


def test_a_follow_is_sent_to_a_remote_community(app, env):
    from app.auth.onboarding import send_community_follow

    client, person = env
    remote = remote_community()
    person.private_key = 'a-key'
    db.session.commit()

    with patch('app.auth.onboarding.send_post_request') as sent:
        send_community_follow(remote.id, 'a-join-request-uuid', person.id)

    assert sent.call_args.args[1]['type'] == 'Follow'
    assert sent.call_args.args[1]['object'] == remote.public_url()


def test_no_follow_is_sent_to_an_instance_that_is_gone(app, env):
    """A dead instance is not worth the request."""
    from app.auth.onboarding import send_community_follow

    client, person = env
    remote = remote_community()
    remote.instance.gone_forever = True
    db.session.commit()

    with patch('app.auth.onboarding.send_post_request') as sent:
        send_community_follow(remote.id, 'a-join-request-uuid', person.id)

    sent.assert_not_called()


def test_a_stale_membership_cache_does_not_write_a_second_row(app, env):
    """`community_membership` is memoized, so it can say NONMEMBER for an
    account that already has the row. The second check is what keeps that
    from becoming a duplicate."""
    client, person = env
    topic = a_topic()
    community = in_topic(topic)
    make_community_member(person, community)
    db.session.commit()

    with patch('app.auth.onboarding.community_membership',
               return_value=SUBSCRIPTION_NONMEMBER):
        with patch('app.auth.onboarding.flash'):
            client.post('/auth/choose_topics',
                        data={'chosen_topics': str(topic.id)})

    assert CommunityMember.query.filter_by(user_id=person.id,
                                           community_id=community.id).count() == 1


def test_a_remote_community_you_are_already_in_is_not_followed_again(app, env):
    """The membership test is what keeps a second Follow -- and a second join
    request -- off the wire for a community this account already belongs
    to."""
    client, person = env
    topic = a_topic()
    remote = remote_community(topic)
    make_community_member(person, remote)
    db.session.commit()

    with patch('app.auth.onboarding.send_community_follow') as followed:
        with patch('app.auth.onboarding.flash'):
            client.post('/auth/choose_topics',
                        data={'chosen_topics': str(topic.id)})

    followed.assert_not_called()
    assert CommunityJoinRequest.query.filter_by(user_id=person.id).count() == 0
