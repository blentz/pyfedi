"""Topics in the admin, and deleting a community.

Sub-project 111 -- `app/admin/routes.py:1586-1716` (community delete, the task
behind it, and the four topic screens), plus the two helpers those screens read
the tree through: `topics_for_form` in `app/admin/util.py` and `topic_tree` in
`app/utils.py`.

Topics are a tree held in one plain Integer column. Nothing in the schema says
`Topic.parent_id` names a row that exists, or that following it terminates, and
both of those turned out to be assumptions the code made:

* D1307: the edit form offered a topic its own child as a parent, and choosing
  it wrote a cycle.
* D1308: `Topic.path()` walks parents until one has none, so a cycle never
  returned -- a hung worker for every request that asked for that path.
* D1309: refusing to delete a topic that still has communities was a 500.
* D1310: deleting a parent left its children naming a row that is gone, and
  `topic_tree` showed neither them nor anything beneath them.

The cycle is the serious one, because the three of them compound: the form
offers it, `topic_tree` then hides both topics (it roots on `parent_id is None`)
so there is nothing left to edit, and `path()` hangs on every visit.
"""
import pytest
from flask import g

from app import cache, db
from app.models import Community, CommunityMember, Site, Topic, User
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    admin = make_user(api_baseline.instance_local, 'topicadmin', local=True)
    admin.verified = True
    admin.private_key = 'x'
    db.session.commit()
    grant_permission(admin, 'administer all communities')
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


def a_topic(name, machine_name, parent_id=None, countries=None):
    topic = Topic(name=name, machine_name=machine_name, num_communities=0,
                  parent_id=parent_id, show_posts_in_children=False,
                  countries=countries if countries is not None else [])
    db.session.add(topic)
    db.session.commit()
    return topic


def machine_names(tree):
    return sorted(node['topic'].machine_name for node in tree)


def somebody_else(env, permission='edit cms pages'):
    """A logged-in admin holding a different permission."""
    user = make_user(env.baseline.instance_local, 'otheradmin', local=True)
    user.verified = True
    user.private_key = 'x'
    db.session.commit()
    grant_permission(user, permission)
    with env.client.session_transaction() as session:
        session['_user_id'] = str(user.id)
    return user


class TestTheTopicsScreen:
    def test_it_lists_them(self, env):
        a_topic('Music', 'music')
        response = env.client.get('/admin/topics')
        assert response.status_code == 200
        assert b'Music' in response.data

    def test_somebody_with_another_permission_may_not_see_it(self, env):
        somebody_else(env)
        response = env.client.get('/admin/topics')
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']


class TestAddingATopic:
    def payload(self, env, **overrides):
        data = {'csrf_token': env.token, 'name': 'Music',
                'machine_name': 'Music Genre', 'parent_id': '-1',
                'show_posts_in_children': '', 'countries': ''}
        data.update(overrides)
        return data

    def test_the_form(self, env):
        assert env.client.get('/admin/topic/add').status_code == 200

    def test_one_at_the_top_level(self, env):
        response = env.client.post('/admin/topic/add', data=self.payload(env))
        assert response.status_code == 302
        topic = Topic.query.filter_by(name='Music').one()
        assert topic.parent_id is None
        assert topic.num_communities == 0

    def test_the_slug_is_slugified(self, env):
        env.client.post('/admin/topic/add', data=self.payload(env))
        assert Topic.query.filter_by(name='Music').one().machine_name == \
            'music-genre'

    def test_one_under_a_parent(self, env):
        parent = a_topic('Parent', 'parent')
        env.client.post('/admin/topic/add',
                        data=self.payload(env, parent_id=str(parent.id)))
        assert Topic.query.filter_by(name='Music').one().parent_id == parent.id

    def test_countries_one_per_line(self, env):
        env.client.post('/admin/topic/add',
                        data=self.payload(env, countries='gb\nie\n'))
        assert Topic.query.filter_by(name='Music').one().countries == ['gb', 'ie']

    def test_blank_lines_and_stray_spaces_are_dropped(self, env):
        env.client.post('/admin/topic/add',
                        data=self.payload(env, countries=' gb \n\n\n de\n'))
        assert Topic.query.filter_by(name='Music').one().countries == ['gb', 'de']

    def test_no_countries_at_all(self, env):
        env.client.post('/admin/topic/add', data=self.payload(env))
        assert Topic.query.filter_by(name='Music').one().countries == []

    def test_a_topic_with_no_name_is_refused(self, env):
        response = env.client.post('/admin/topic/add',
                                   data=self.payload(env, name=''))
        assert response.status_code == 200
        assert Topic.query.filter_by(machine_name='music-genre').count() == 0

    def test_a_topic_with_no_slug_is_refused(self, env):
        response = env.client.post('/admin/topic/add',
                                   data=self.payload(env, machine_name=''))
        assert response.status_code == 200
        assert Topic.query.filter_by(name='Music').count() == 0

    def test_somebody_with_another_permission_may_not(self, env):
        somebody_else(env)
        response = env.client.post('/admin/topic/add', data=self.payload(env))
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        assert Topic.query.filter_by(name='Music').count() == 0


class TestEditingATopic:
    def payload(self, env, topic, **overrides):
        data = {'csrf_token': env.token, 'name': topic.name,
                'machine_name': topic.machine_name, 'parent_id': '-1',
                'show_posts_in_children': '', 'countries': ''}
        data.update(overrides)
        return data

    def test_the_form_is_filled_in(self, env):
        topic = a_topic('Music', 'music', countries=['gb', 'ie'])
        response = env.client.get(f'/admin/topic/{topic.id}/edit')
        assert response.status_code == 200
        assert b'music' in response.data
        assert b'gb\nie' in response.data or b'gb\r\nie' in response.data

    def test_a_topic_nobody_has(self, env):
        assert env.client.get('/admin/topic/999999/edit').status_code == 404

    def test_renaming_it(self, env):
        topic = a_topic('Music', 'music')
        response = env.client.post(f'/admin/topic/{topic.id}/edit',
                                   data=self.payload(env, topic, name='Sounds'))
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).name == 'Sounds'

    def test_the_new_slug_is_slugified(self, env):
        topic = a_topic('Music', 'music')
        env.client.post(f'/admin/topic/{topic.id}/edit',
                        data=self.payload(env, topic, machine_name='Loud Music'))
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).machine_name == 'loud-music'

    def test_the_community_count_is_refreshed(self, env):
        topic = a_topic('Music', 'music')
        community = make_community('probeland')
        community.topic_id = topic.id
        db.session.commit()
        env.client.post(f'/admin/topic/{topic.id}/edit',
                        data=self.payload(env, topic))
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).num_communities == 1

    def test_giving_it_a_parent(self, env):
        parent = a_topic('Parent', 'parent')
        topic = a_topic('Music', 'music')
        env.client.post(f'/admin/topic/{topic.id}/edit',
                        data=self.payload(env, topic, parent_id=str(parent.id)))
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).parent_id == parent.id

    def test_taking_the_parent_away_again(self, env):
        parent = a_topic('Parent', 'parent')
        topic = a_topic('Music', 'music', parent_id=parent.id)
        env.client.post(f'/admin/topic/{topic.id}/edit',
                        data=self.payload(env, topic, parent_id='-1'))
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).parent_id is None

    def test_countries_are_rewritten(self, env):
        topic = a_topic('Music', 'music', countries=['gb'])
        env.client.post(f'/admin/topic/{topic.id}/edit',
                        data=self.payload(env, topic, countries='de\nfr'))
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).countries == ['de', 'fr']

    def test_show_posts_in_children_can_be_switched_on(self, env):
        topic = a_topic('Music', 'music')
        env.client.post(f'/admin/topic/{topic.id}/edit',
                        data=self.payload(env, topic, show_posts_in_children='y'))
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).show_posts_in_children is True

    def test_an_edit_with_no_name_is_refused(self, env):
        topic = a_topic('Music', 'music')
        response = env.client.post(f'/admin/topic/{topic.id}/edit',
                                   data=self.payload(env, topic, name=''))
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).name == 'Music'

    def test_somebody_with_another_permission_may_not(self, env):
        topic = a_topic('Music', 'music')
        somebody_else(env)
        response = env.client.post(f'/admin/topic/{topic.id}/edit',
                                   data=self.payload(env, topic, name='Sounds'))
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).name == 'Music'


class TestATopicMayNotBeItsOwnAncestor:
    """D1307. The form offered the cycle, and the cycle hid both topics."""

    def test_its_own_child_is_not_offered_as_a_parent(self, env):
        from app.admin.util import topics_for_form
        parent = a_topic('Parent', 'parent')
        child = a_topic('Child', 'child', parent_id=parent.id)
        offered = [topic_id for topic_id, _label in topics_for_form(parent.id)]
        assert child.id not in offered
        assert parent.id not in offered

    def test_nor_is_a_grandchild(self, env):
        from app.admin.util import topics_for_form
        parent = a_topic('Parent', 'parent')
        child = a_topic('Child', 'child', parent_id=parent.id)
        grandchild = a_topic('Grandchild', 'grandchild', parent_id=child.id)
        offered = [topic_id for topic_id, _label in topics_for_form(parent.id)]
        assert grandchild.id not in offered

    def test_a_topic_outside_the_subtree_still_is(self, env):
        from app.admin.util import topics_for_form
        parent = a_topic('Parent', 'parent')
        a_topic('Child', 'child', parent_id=parent.id)
        elsewhere = a_topic('Elsewhere', 'elsewhere')
        deeper = a_topic('Deeper', 'deeper', parent_id=elsewhere.id)
        offered = [topic_id for topic_id, _label in topics_for_form(parent.id)]
        assert elsewhere.id in offered
        assert deeper.id in offered

    def test_a_child_of_the_topic_being_edited_deeper_in_the_tree(self, env):
        """The same exclusion through `topics_for_form_children`: the topic
        being edited is itself a child, so the walk reaches it in the recursive
        half rather than the top-level one, and that half had the same defect."""
        from app.admin.util import topics_for_form
        grandparent = a_topic('Grandparent', 'grandparent')
        middle = a_topic('Middle', 'middle', parent_id=grandparent.id)
        below = a_topic('Below', 'below', parent_id=middle.id)
        deeper = a_topic('Deeper', 'deeper', parent_id=below.id)
        offered = [topic_id for topic_id, _label in topics_for_form(middle.id)]
        assert below.id not in offered
        assert deeper.id not in offered
        assert grandparent.id in offered

    def test_the_post_is_refused_there_too(self, env):
        grandparent = a_topic('Grandparent', 'grandparent')
        middle = a_topic('Middle', 'middle', parent_id=grandparent.id)
        below = a_topic('Below', 'below', parent_id=middle.id)
        response = env.client.post(
            f'/admin/topic/{middle.id}/edit',
            data={'csrf_token': env.token, 'name': 'Middle',
                  'machine_name': 'middle', 'parent_id': str(below.id),
                  'show_posts_in_children': '', 'countries': ''})
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Topic, middle.id).parent_id == grandparent.id

    def test_the_form_refuses_the_post_that_would_make_a_cycle(self, env):
        """Withdrawing the offer is the refusal: a `SelectField` validates what
        it was given against its own choices."""
        parent = a_topic('Parent', 'parent')
        child = a_topic('Child', 'child', parent_id=parent.id)
        response = env.client.post(
            f'/admin/topic/{parent.id}/edit',
            data={'csrf_token': env.token, 'name': 'Parent',
                  'machine_name': 'parent', 'parent_id': str(child.id),
                  'show_posts_in_children': '', 'countries': ''})
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Topic, parent.id).parent_id is None
        assert db.session.get(Topic, child.id).parent_id == parent.id

    def test_a_topic_may_not_be_made_its_own_parent(self, env):
        topic = a_topic('Music', 'music')
        response = env.client.post(
            f'/admin/topic/{topic.id}/edit',
            data={'csrf_token': env.token, 'name': 'Music',
                  'machine_name': 'music', 'parent_id': str(topic.id),
                  'show_posts_in_children': '', 'countries': ''})
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Topic, topic.id).parent_id is None


class TestThePathOfATopic:
    def test_a_topic_at_the_top(self, env):
        assert a_topic('Music', 'music').path() == 'music'

    def test_a_child(self, env):
        parent = a_topic('Parent', 'parent')
        assert a_topic('Child', 'child', parent_id=parent.id).path() == \
            'parent/child'

    def test_a_grandchild(self, env):
        parent = a_topic('Parent', 'parent')
        child = a_topic('Child', 'child', parent_id=parent.id)
        assert a_topic('Deep', 'deep', parent_id=child.id).path() == \
            'parent/child/deep'

    def test_a_parent_that_is_gone(self, env):
        topic = a_topic('Music', 'music')
        topic.parent_id = 999999
        db.session.commit()
        assert topic.path() == 'music'

    def test_a_cycle_ends_the_walk(self, env):
        """D1308. `while parent_id is not None` never came true, so the request
        that asked for the path was never answered -- one hung worker per visit.
        Written as a cycle in the database rather than through the form, which no
        longer allows one (D1307), because a database can already hold one.

        The path is each ancestor the walk saw before it came back round, once:
        'first' names 'second' as its parent and 'second' names 'first'."""
        first = a_topic('First', 'first')
        second = a_topic('Second', 'second', parent_id=first.id)
        first.parent_id = second.id
        db.session.commit()
        assert first.path() == 'second/first'

    def test_a_cycle_of_one(self, env):
        topic = a_topic('Music', 'music')
        topic.parent_id = topic.id
        db.session.commit()
        assert topic.path() == 'music'

    def test_a_longer_cycle(self, env):
        first = a_topic('First', 'first')
        second = a_topic('Second', 'second', parent_id=first.id)
        third = a_topic('Third', 'third', parent_id=second.id)
        first.parent_id = third.id
        db.session.commit()
        assert first.path() == 'second/third/first'

    def test_every_topic_in_a_cycle_gets_an_answer(self, env):
        """Each of them, not only the one the walk starts at -- the loop is
        bounded by what this walk has seen and not by any single topic."""
        first = a_topic('First', 'first')
        second = a_topic('Second', 'second', parent_id=first.id)
        first.parent_id = second.id
        db.session.commit()
        assert second.path() == 'first/second'


class TestTheTreeTheScreensRead:
    def test_a_parent_and_its_child(self, env):
        parent = a_topic('Parent', 'parent')
        a_topic('Child', 'child', parent_id=parent.id)
        from app.utils import topic_tree
        tree = topic_tree()
        assert machine_names(tree) == ['parent']
        assert machine_names(tree[0]['children']) == ['child']

    def test_a_topic_whose_parent_is_gone_is_shown_at_the_top(self, env):
        """D1310. It used to be shown nowhere: the tree rooted on `parent_id is
        None` alone, and nothing in the schema stops the column naming a row
        that has been deleted."""
        a_topic('Orphan', 'orphan', parent_id=999999)
        from app.utils import topic_tree
        assert machine_names(topic_tree()) == ['orphan']

    def test_and_its_own_children_come_with_it(self, env):
        orphan = a_topic('Orphan', 'orphan', parent_id=999999)
        a_topic('Below', 'below', parent_id=orphan.id)
        from app.utils import topic_tree
        tree = topic_tree()
        assert machine_names(tree) == ['orphan']
        assert machine_names(tree[0]['children']) == ['below']


class TestDeletingATopic:
    def test_one_with_nothing_in_it(self, env):
        topic = a_topic('Music', 'music')
        response = env.client.post(f'/admin/topic/{topic.id}/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 302
        assert db.session.get(Topic, topic.id) is None

    def test_one_with_communities_is_refused(self, env):
        """D1309. This was a 500, and the refusal is load-bearing:
        `Topic.communities` cascades "all, delete-orphan", so deleting the topic
        would delete the communities in it."""
        topic = a_topic('Music', 'music')
        community = make_community('probeland')
        community.topic_id = topic.id
        db.session.commit()
        response = env.client.post(f'/admin/topic/{topic.id}/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Topic, topic.id) is not None
        assert db.session.get(Community, community.id) is not None

    def test_the_refusal_says_so(self, env):
        topic = a_topic('Music', 'music')
        community = make_community('probeland')
        community.topic_id = topic.id
        db.session.commit()
        response = env.client.post(f'/admin/topic/{topic.id}/delete',
                                   data={'csrf_token': env.token},
                                   follow_redirects=True)
        assert b'Cannot delete topic' in response.data

    def test_the_count_it_refuses_on_is_recounted_first(self, env):
        """`num_communities` is a stored count and the guard reads a fresh one,
        so a stale zero does not open the delete."""
        topic = a_topic('Music', 'music')
        community = make_community('probeland')
        community.topic_id = topic.id
        topic.num_communities = 0
        db.session.commit()
        env.client.post(f'/admin/topic/{topic.id}/delete',
                        data={'csrf_token': env.token})
        db.session.expire_all()
        assert db.session.get(Topic, topic.id) is not None
        assert db.session.get(Topic, topic.id).num_communities == 1

    def test_its_children_move_up_to_where_it_was(self, env):
        """D1310. They used to keep naming the deleted row, which hid them and
        everything beneath them from the topics screen and the menu."""
        grandparent = a_topic('Grandparent', 'grandparent')
        parent = a_topic('Parent', 'parent', parent_id=grandparent.id)
        child = a_topic('Child', 'child', parent_id=parent.id)
        env.client.post(f'/admin/topic/{parent.id}/delete',
                        data={'csrf_token': env.token})
        db.session.expire_all()
        assert db.session.get(Topic, child.id).parent_id == grandparent.id

    def test_children_of_a_top_level_topic_become_top_level(self, env):
        parent = a_topic('Parent', 'parent')
        child = a_topic('Child', 'child', parent_id=parent.id)
        env.client.post(f'/admin/topic/{parent.id}/delete',
                        data={'csrf_token': env.token})
        db.session.expire_all()
        assert db.session.get(Topic, child.id).parent_id is None
        from app.utils import topic_tree
        assert machine_names(topic_tree()) == ['child']

    def test_every_child_moves_not_only_the_first(self, env):
        parent = a_topic('Parent', 'parent')
        first = a_topic('First', 'first', parent_id=parent.id)
        second = a_topic('Second', 'second', parent_id=parent.id)
        env.client.post(f'/admin/topic/{parent.id}/delete',
                        data={'csrf_token': env.token})
        db.session.expire_all()
        assert db.session.get(Topic, first.id).parent_id is None
        assert db.session.get(Topic, second.id).parent_id is None

    def test_a_grandchild_stays_where_it_was(self, env):
        parent = a_topic('Parent', 'parent')
        child = a_topic('Child', 'child', parent_id=parent.id)
        grandchild = a_topic('Deep', 'deep', parent_id=child.id)
        env.client.post(f'/admin/topic/{parent.id}/delete',
                        data={'csrf_token': env.token})
        db.session.expire_all()
        assert db.session.get(Topic, grandchild.id).parent_id == child.id

    def test_a_topic_nobody_has(self, env):
        response = env.client.post('/admin/topic/999999/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 404

    def test_somebody_with_another_permission_may_not(self, env):
        topic = a_topic('Music', 'music')
        somebody_else(env)
        response = env.client.post(f'/admin/topic/{topic.id}/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        assert db.session.get(Topic, topic.id) is not None

    def test_the_menu_is_rebuilt(self, env):
        """The topics menu is memoized, and a deleted topic must leave it."""
        from app.utils import menu_topics
        topic = a_topic('Music', 'music')
        assert 'music' in [t.machine_name for t in menu_topics()]
        env.client.post(f'/admin/topic/{topic.id}/delete',
                        data={'csrf_token': env.token})
        assert 'music' not in [t.machine_name for t in menu_topics()]


class TestDeletingACommunity:
    def test_it_is_banned_first_and_then_gone(self, env):
        """The route bans the community before handing it to the task, because
        unsubscribing everyone can take a while and a banned community is
        hidden from the UI meanwhile."""
        community = make_community('probeland')
        db.session.commit()
        community_id = community.id
        response = env.client.post(f'/admin/community/{community_id}/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 302
        db.session.expunge_all()
        assert Community.query.filter_by(id=community_id).count() == 0

    def test_a_remote_community_with_members(self, env):
        community = make_community('remoteland')
        community.ap_id = 'remoteland@remote.test'
        community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        make_community_member(env.baseline.user2, community)
        community_id = community.id
        env.client.post(f'/admin/community/{community_id}/delete',
                        data={'csrf_token': env.token})
        db.session.expunge_all()
        assert Community.query.filter_by(id=community_id).count() == 0
        assert CommunityMember.query.filter_by(
            community_id=community_id).count() == 0

    def test_its_posts_go_with_it(self, env):
        from app.models import Post
        from tests.factories import make_post
        community = make_community('probeland')
        db.session.commit()
        make_community_member(env.baseline.user2, community)
        post = make_post(community, env.baseline.user2,
                         ap_id='https://test.piefed.local/p/77')
        post_id = post.id
        env.client.post(f'/admin/community/{community.id}/delete',
                        data={'csrf_token': env.token})
        db.session.expunge_all()
        assert Post.query.filter_by(id=post_id).count() == 0

    def test_a_community_nobody_has(self, env):
        response = env.client.post('/admin/community/999999/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 404

    def test_somebody_with_another_permission_may_not(self, env):
        community = make_community('probeland')
        db.session.commit()
        somebody_else(env)
        community_id = community.id
        response = env.client.post(f'/admin/community/{community_id}/delete',
                                   data={'csrf_token': env.token})
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        db.session.expunge_all()
        assert Community.query.filter_by(id=community_id).count() == 1
