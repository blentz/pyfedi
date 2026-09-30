"""Code that arrived with the rebase onto upstream v1.7.17.

The rebase brought in upstream work that no test here had executed, and four
coverage floors fell below their ratchet because of it:

    topic export and import     `admin_topics_export`, `admin_topics_import`, and
                                the three helpers behind them in app/admin/util.py:
                                `serialize_topic_tree`/`serialize_topic_node`,
                                `create_topic_and_children`, and the
                                `process_topic_communities` task.
    per-user page length        `admin_communities` and `admin_users` now honour a
                                shorter `User.page_length` than their 500 default.
    static-file Vary            `after_request` strips every Vary member except
                                Accept-Encoding from a static response, so shared
                                caches can store one copy.
    get_event_start             the Jinja global that reads an event post's start.

`admin_topics_import`'s `flash(_('No file uploaded'))` arm is not driven: the
form's DataRequired refuses an empty upload before the view reaches it, and a
FileStorage with a filename is always truthy.
"""
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import orjson
import pytest
from flask import Response, g

from app import db
from app.admin.util import (create_topic_and_children, process_topic_communities,
                            serialize_topic_tree)
from app.constants import POST_TYPE_EVENT
from app.models import Community, CommunityMember, Event, Site, Topic
from app.utils import get_event_start, topic_tree
from tests.factories import (grant_permission, make_community, make_community_member,
                             make_post, make_user)


@pytest.fixture
def env(app, api_baseline):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    admin = make_user(api_baseline.instance_local, 'upstreamadmin', local=True)
    admin.verified = True
    admin.private_key = 'x'
    db.session.commit()
    grant_permission(admin, 'administer all communities')
    grant_permission(admin, 'administer all users')
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)
        session['_fresh'] = True
        session['csrf_token'] = raw
    return SimpleNamespace(app=app, client=client, token=token, admin=admin,
                           baseline=api_baseline)


@pytest.fixture
def debug_mode(app):
    previous = app.debug
    app.debug = True
    yield
    app.debug = previous


def a_topic(name, machine_name, parent_id=None, countries=None):
    topic = Topic(name=name, machine_name=machine_name, num_communities=0,
                  parent_id=parent_id, show_posts_in_children=False,
                  countries=countries if countries is not None else [])
    db.session.add(topic)
    db.session.commit()
    return topic


# --------------------------------------------------------------------------
# Serialising the tree
# --------------------------------------------------------------------------


class TestSerialisingTheTopicTree:
    def test_a_parent_carries_its_child(self, env):
        parent = a_topic('Music', 'music', countries=['gb'])
        a_topic('Jazz', 'jazz', parent_id=parent.id)

        data = serialize_topic_tree(topic_tree())

        assert len(data) == 1
        assert data[0]['machine_name'] == 'music'
        assert data[0]['countries'] == ['gb']
        assert [child['machine_name'] for child in data[0]['children']] == ['jazz']
        assert data[0]['children'][0]['parent_id'] == parent.id

    def test_no_countries_is_an_empty_list(self, env):
        topic = a_topic('Music', 'music')
        topic.countries = None
        db.session.commit()

        assert serialize_topic_tree(topic_tree())[0]['countries'] == []

    def test_only_public_communities_are_exported(self, env):
        topic = a_topic('Music', 'music')
        public = make_community('publicmusic')
        local_only = make_community('localmusic')
        private = make_community('privatemusic')
        local_only.local_only = True
        private.private = True
        for community in (public, local_only, private):
            community.topic_id = topic.id
        db.session.commit()

        communities = serialize_topic_tree(topic_tree())[0]['communities']

        assert communities == [public.lemmy_link()]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


class TestExport:
    def test_it_is_a_json_download(self, env):
        a_topic('Music', 'music')

        response = env.client.get('/admin/topics/export')

        assert response.status_code == 200
        assert response.mimetype == 'application/json'
        assert 'attachment' in response.headers['Content-Disposition']
        assert '_topics.json' in response.headers['Content-Disposition']
        assert orjson.loads(response.data)[0]['machine_name'] == 'music'


# --------------------------------------------------------------------------
# Creating topics from imported data
# --------------------------------------------------------------------------


class TestCreatingImportedTopics:
    def test_a_topic_and_its_children(self, env):
        data = {'machine_name': 'music', 'name': 'Music', 'countries': ['gb'],
                'show_posts_in_children': True,
                'children': [{'machine_name': 'jazz', 'name': 'Jazz'}]}

        with patch('app.admin.util.process_topic_communities') as task:
            create_topic_and_children(data, None)

        music = Topic.query.filter_by(machine_name='music').one()
        jazz = Topic.query.filter_by(machine_name='jazz').one()
        assert music.parent_id is None
        assert music.countries == ['gb']
        assert music.show_posts_in_children is True
        assert jazz.parent_id == music.id
        assert jazz.countries == []
        assert task.delay.call_count == 2

    def test_debug_runs_the_community_work_inline(self, env, debug_mode):
        data = {'machine_name': 'music', 'name': 'Music'}

        with patch('app.admin.util.process_topic_communities') as task:
            create_topic_and_children(data, None)

        task.assert_called_once()
        task.delay.assert_not_called()


class TestAddingImportedCommunitiesToATopic:
    def test_no_communities_is_nothing_to_do(self, env):
        topic = a_topic('Music', 'music')

        with patch('app.admin.util.search_for_community') as search:
            process_topic_communities({'communities': []}, topic.id)

        search.assert_not_called()

    def test_a_known_community_is_joined_and_assigned(self, env):
        topic = a_topic('Music', 'music')
        community = make_community('jazz')
        db.session.commit()

        # search_for_community runs inside patch_db_session, so the row it
        # answers belongs to the task's session -- which is the one committed.
        with patch('app.admin.util.search_for_community',
                   side_effect=lambda link: db.session.get(Community, community.id)), \
                patch('app.admin.util.do_subscribe') as subscribe:
            process_topic_communities({'communities': ['jazz@elsewhere.example']}, topic.id)

        subscribe.delay.assert_called_once_with(community.ap_id, 1, admin_preload=True)
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).num_communities == 1
        assert db.session.get(Community, community.id).topic_id == topic.id

    def test_debug_subscribes_inline(self, env, debug_mode):
        topic = a_topic('Music', 'music')
        community = make_community('jazz')
        db.session.commit()

        with patch('app.admin.util.search_for_community', return_value=community), \
                patch('app.admin.util.do_subscribe') as subscribe:
            process_topic_communities({'communities': ['jazz@elsewhere.example']}, topic.id)

        subscribe.assert_called_once_with(community.ap_id, 1, admin_preload=True)
        subscribe.delay.assert_not_called()

    def test_a_community_user_1_already_joined_is_not_joined_again(self, env):
        topic = a_topic('Music', 'music')
        community = make_community('jazz')
        db.session.commit()
        make_community_member(env.baseline.user1, community)
        assert CommunityMember.query.filter_by(community_id=community.id, user_id=1).first()

        with patch('app.admin.util.search_for_community', return_value=community), \
                patch('app.admin.util.do_subscribe') as subscribe:
            process_topic_communities({'communities': ['jazz@elsewhere.example']}, topic.id)

        subscribe.assert_not_called()
        subscribe.delay.assert_not_called()
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).num_communities == 1

    def test_a_community_that_cannot_be_found_is_skipped(self, env):
        topic = a_topic('Music', 'music')

        with patch('app.admin.util.search_for_community', return_value=None), \
                patch('app.admin.util.do_subscribe') as subscribe:
            process_topic_communities({'communities': ['gone@elsewhere.example']}, topic.id)

        subscribe.assert_not_called()
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).num_communities == 0

    def test_a_lookup_that_raises_is_treated_as_not_found(self, env):
        topic = a_topic('Music', 'music')

        with patch('app.admin.util.search_for_community', side_effect=RuntimeError('down')), \
                patch('app.admin.util.do_subscribe') as subscribe:
            process_topic_communities({'communities': ['down@elsewhere.example']}, topic.id)

        subscribe.assert_not_called()

    def test_a_failure_after_the_lookup_is_rolled_back_and_raised(self, env):
        topic = a_topic('Music', 'music')
        community = make_community('jazz')
        db.session.commit()

        with patch('app.admin.util.search_for_community', return_value=community), \
                patch('app.admin.util.do_subscribe', MagicMock(delay=MagicMock(side_effect=RuntimeError('queue down')))):
            with pytest.raises(RuntimeError, match='queue down'):
                process_topic_communities({'communities': ['jazz@elsewhere.example']}, topic.id)


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


class TestImport:
    def upload(self, env, content):
        return env.client.post('/admin/topics/import', data={
            'csrf_token': env.token,
            'import_file': (BytesIO(content), 'topics.json'),
            'import_submit': 'Import'}, content_type='multipart/form-data')

    def test_the_form(self, env):
        assert env.client.get('/admin/topics/import').status_code == 200

    def test_an_export_is_imported(self, env):
        content = orjson.dumps([{'machine_name': 'music', 'name': 'Music',
                                 'children': [{'machine_name': 'jazz', 'name': 'Jazz'}]}])

        with patch('app.admin.util.process_topic_communities'):
            response = self.upload(env, content)

        assert response.status_code == 302
        assert response.headers['Location'].endswith('/admin/topics')
        assert Topic.query.filter_by(machine_name='jazz').one().parent_id == \
            Topic.query.filter_by(machine_name='music').one().id

    def test_a_file_that_is_not_json_is_reported(self, env):
        response = self.upload(env, b'not json')

        assert response.status_code == 200
        assert Topic.query.count() == 0

    def test_debug_lets_the_error_through(self, env, debug_mode):
        with pytest.raises(orjson.JSONDecodeError):
            self.upload(env, b'not json')


# --------------------------------------------------------------------------
# Page length on the admin listings
# --------------------------------------------------------------------------


class TestAShorterPageLength:
    def test_the_community_list_uses_it(self, env):
        env.admin.page_length = 1
        for name in ('first', 'second'):
            make_community(name)
        db.session.commit()

        response = env.client.get('/admin/communities')

        assert response.status_code == 200
        assert b'page=2' in response.data

    def test_the_user_list_uses_it(self, env):
        env.admin.page_length = 1
        db.session.commit()

        response = env.client.get('/admin/users')

        assert response.status_code == 200
        assert b'page=2' in response.data


# --------------------------------------------------------------------------
# Vary on static files
# --------------------------------------------------------------------------


def after_request_for(app, path, vary):
    """Run app/request_hooks.py's after_request alone, on a non-HTML response.

    Alone, so the Vary it leaves is the Vary the test sees: no other extension's
    hook runs after it. Non-HTML, because the hook's text/html branch merges
    Accept-Language and Cookie back in after the strip -- a static stylesheet or
    image never takes that branch.
    """
    hook = next(func for func in app.after_request_funcs[None]
                if func.__module__ == 'app.request_hooks' and func.__name__ == 'after_request')
    response = Response('x', mimetype='text/css')
    response.headers['Vary'] = vary
    with app.test_request_context(path):
        return hook(response)


class TestStaticVary:
    def test_only_accept_encoding_is_kept(self, app, db_session):
        response = after_request_for(app, '/static/styles.css', 'Cookie, Accept-Encoding, Accept-Language')
        assert response.headers['Vary'] == 'Accept-Encoding'

    def test_vary_without_accept_encoding_is_removed(self, app, db_session):
        response = after_request_for(app, '/static/styles.css', 'Cookie')
        assert 'Vary' not in response.headers

    def test_the_manifest_keeps_its_own_headers(self, app, db_session):
        response = after_request_for(app, '/static/manifest.json', 'User-Agent')
        assert response.headers['Vary'] == 'User-Agent'
        assert 'immutable' not in response.headers.get('Cache-Control', '')


# --------------------------------------------------------------------------
# get_event_start
# --------------------------------------------------------------------------


class TestGetEventStart:
    def event_post(self, env, start):
        community = make_community('events')
        post = make_post(community, env.baseline.user1, 'https://test.piefed.local/post/event')
        post.type = POST_TYPE_EVENT
        db.session.add(Event(post_id=post.id, start=start))
        db.session.commit()
        return post

    def test_an_event_answers_its_start(self, env):
        start = datetime(2026, 10, 1, 18, 0)
        assert get_event_start(self.event_post(env, start).id) == start

    def test_an_event_with_no_start(self, env):
        assert get_event_start(self.event_post(env, None).id) is None

    def test_a_post_that_is_not_an_event(self, env):
        community = make_community('notevents')
        post = make_post(community, env.baseline.user1, 'https://test.piefed.local/post/plain')
        assert get_event_start(post.id) is None

    def test_a_post_that_does_not_exist(self, env):
        assert get_event_start(999999) is None
