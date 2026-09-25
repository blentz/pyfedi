"""Instances in the admin, and moving a community here.

Sub-project 113 -- `app/admin/routes.py:2244-2420`: the instance list with its
six filters, the instance edit screen, the offline-instance shortcut, and
`admin_community_move`, which takes a remote community and makes it local.

The move is the interesting one. It rewrites nine columns, generates a keypair,
reassigns ownership and hands the work to a task, and two of its three steps
were broken in ways nothing announced:

* D1318: the owner's `community_member` row was written with
  `is_owner=new_owner_user.id` -- a user id into a Boolean column -- which
  SQLAlchemy refuses. A bare `except` swallowed it, so every move left the new
  owner unable to moderate what they had just been given.
* D1319: `MoveCommunityForm.validate_new_url` compared the raw value while the
  route slugifies before writing, so a name that SLUGIFIES onto an existing
  local community passed validation and hit `UniqueViolation` on
  `ix_community_ap_profile_id` -- a 500.
* D1320, next door: `admin_instance_create_offline`'s bare `except` did not roll
  back, leaving the session in `PendingRollbackError` for the rest of the
  request.

ONE LOGIN PER TEST, AND `g._login_user` POPPED WHEN A TEST CHANGES CALLER.
flask-login caches `current_user` on the app context, which a test holds open
across every request it makes, so the second client in a test is otherwise
answered as the first user (fact 666).
"""
import pytest
from flask import g
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import cache, db
from app.models import (BannedInstances, Community, CommunityMember, Instance,
                        Site, User, utcnow)
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_instance, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    remote = make_instance('remote.test')
    db.session.commit()
    return SimpleNamespace(app=app, token=token, raw=raw, remote=remote,
                           baseline=api_baseline)


def client_for(env, permission, name='anadmin'):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.verified = True
    user.private_key = 'x'
    db.session.commit()
    grant_permission(user, permission)
    client = env.app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
        session['csrf_token'] = env.raw
    g.pop('_login_user', None)
    return client


def get(client, path):
    g.pop('_login_user', None)
    return client.get(path)


def post(client, path, **data):
    g.pop('_login_user', None)
    return client.post(path, data=data)


class TestTheInstanceList:
    def test_it_lists_them(self, env):
        client = client_for(env, 'change instance settings')
        response = get(client, '/admin/instances')
        assert response.status_code == 200
        assert b'remote.test' in response.data

    def test_a_search(self, env):
        make_instance('other.test')
        db.session.commit()
        client = client_for(env, 'change instance settings')
        response = get(client, '/admin/instances?search=remote')
        assert response.status_code == 200
        assert b'remote.test' in response.data
        assert b'other.test' not in response.data

    @pytest.mark.parametrize('name,column', [('trusted', 'trusted'),
                                             ('silenced', 'silenced'),
                                             ('dormant', 'dormant'),
                                             ('gone_forever', 'gone_forever')])
    def test_each_filter_shows_only_what_it_names(self, env, name, column):
        other = make_instance('other.test')
        setattr(env.remote, column, True)
        db.session.commit()
        client = client_for(env, 'change instance settings')
        response = get(client, f'/admin/instances?filter={name}')
        assert response.status_code == 200
        assert b'remote.test' in response.data
        assert b'other.test' not in response.data

    def test_the_online_filter(self, env):
        gone = make_instance('gone.test')
        gone.gone_forever = True
        db.session.commit()
        client = client_for(env, 'change instance settings')
        response = get(client, '/admin/instances?filter=online')
        assert response.status_code == 200
        assert b'remote.test' in response.data
        assert b'gone.test' not in response.data

    def test_the_blocked_filter(self, env):
        make_instance('other.test')
        db.session.add(BannedInstances(domain='remote.test'))
        db.session.commit()
        client = client_for(env, 'change instance settings')
        response = get(client, '/admin/instances?filter=blocked')
        assert response.status_code == 200
        assert b'remote.test' in response.data
        assert b'other.test' not in response.data

    def test_a_filter_that_names_nothing(self, env):
        client = client_for(env, 'change instance settings')
        response = get(client, '/admin/instances?filter=nonsense')
        assert response.status_code == 200
        assert b'remote.test' in response.data

    def test_a_sort_column_that_does_not_exist(self, env):
        client = client_for(env, 'change instance settings')
        assert get(client, '/admin/instances?sort_by=drop').status_code == 200

    def test_a_page_beyond_the_last(self, env):
        client = client_for(env, 'change instance settings')
        assert get(client, '/admin/instances?page=99').status_code == 200

    def test_somebody_with_another_permission_may_not(self, env):
        client = client_for(env, 'edit cms pages')
        response = get(client, '/admin/instances')
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']


class TestEditingAnInstance:
    def payload(self, env, **overrides):
        data = {'csrf_token': env.token, 'inbox': 'https://remote.test/inbox',
                'posting_warning': '', 'admin_note': ''}
        data.update(overrides)
        return data

    def test_the_form(self, env):
        client = client_for(env, 'administer all communities')
        response = get(client, f'/admin/instance/{env.remote.id}/edit')
        assert response.status_code == 200

    def test_an_instance_nobody_has(self, env):
        client = client_for(env, 'administer all communities')
        assert get(client, '/admin/instance/999999/edit').status_code == 404

    def test_marking_one_trusted(self, env):
        client = client_for(env, 'administer all communities')
        response = post(client, f'/admin/instance/{env.remote.id}/edit',
                        **self.payload(env, trusted='y'))
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Instance, env.remote.id).trusted is True

    def test_the_trusted_cache_is_dropped(self, env):
        from app.utils import trusted_instance_ids
        client = client_for(env, 'administer all communities')
        assert env.remote.id not in trusted_instance_ids()
        post(client, f'/admin/instance/{env.remote.id}/edit',
             **self.payload(env, trusted='y'))
        assert env.remote.id in trusted_instance_ids()

    def test_silencing_one_hides_its_communities(self, env):
        """`switch_to_silenced` clears show_all, show_popular and the topic of
        every community on that instance."""
        community = make_community('remoteland')
        community.instance_id = env.remote.id
        community.show_all = True
        community.show_popular = True
        db.session.commit()
        client = client_for(env, 'administer all communities')
        post(client, f'/admin/instance/{env.remote.id}/edit',
             **self.payload(env, silenced='y'))
        db.session.expire_all()
        fresh = db.session.get(Community, community.id)
        assert fresh.show_all is False
        assert fresh.show_popular is False
        assert db.session.get(Instance, env.remote.id).silenced is True

    def test_unsilencing_one_brings_them_back(self, env):
        community = make_community('remoteland')
        community.instance_id = env.remote.id
        community.show_all = False
        env.remote.silenced = True
        db.session.commit()
        client = client_for(env, 'administer all communities')
        post(client, f'/admin/instance/{env.remote.id}/edit', **self.payload(env))
        db.session.expire_all()
        assert db.session.get(Community, community.id).show_all is True
        assert db.session.get(Instance, env.remote.id).silenced is False

    def test_unsilencing_a_trusted_instance_shows_it_in_popular(self, env):
        community = make_community('remoteland')
        community.instance_id = env.remote.id
        community.show_popular = False
        env.remote.silenced = True
        db.session.commit()
        client = client_for(env, 'administer all communities')
        post(client, f'/admin/instance/{env.remote.id}/edit',
             **self.payload(env, trusted='y'))
        db.session.expire_all()
        assert db.session.get(Community, community.id).show_popular is True

    def test_the_other_fields_are_saved(self, env):
        client = client_for(env, 'administer all communities')
        post(client, f'/admin/instance/{env.remote.id}/edit',
             **self.payload(env, dormant='y', gone_forever='y',
                            posting_warning='careful', admin_note='a note',
                            popular='y', inbox='https://remote.test/other'))
        db.session.expire_all()
        instance = db.session.get(Instance, env.remote.id)
        assert instance.dormant is True
        assert instance.gone_forever is True
        assert instance.posting_warning == 'careful'
        assert instance.admin_note == 'a note'
        assert instance.popular is True
        assert instance.inbox == 'https://remote.test/other'

    def test_somebody_with_another_permission_may_not(self, env):
        client = client_for(env, 'edit cms pages')
        response = post(client, f'/admin/instance/{env.remote.id}/edit',
                        **self.payload(env, trusted='y'))
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        db.session.expire_all()
        assert db.session.get(Instance, env.remote.id).trusted is False


class TestTheOfflineInstanceShortcut:
    def test_the_form(self, env):
        client = client_for(env, 'administer all communities')
        assert get(client, '/admin/instance/create_offline').status_code == 200

    def test_one_is_created_as_gone_forever(self, env):
        client = client_for(env, 'administer all communities')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='offline.test')
        assert response.status_code == 302
        instance = Instance.query.filter_by(domain='offline.test').one()
        assert instance.gone_forever is True
        assert instance.inbox == 'https://offline.test/inbox'

    def test_a_domain_that_is_already_here(self, env):
        """The form refuses it (D1321), so the route's own handler is not
        reached -- and one row remains either way."""
        client = client_for(env, 'administer all communities')
        post(client, '/admin/instance/create_offline',
             csrf_token=env.token, domain='offline.test')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='offline.test')
        assert response.status_code == 200
        assert Instance.query.filter_by(domain='offline.test').count() == 1

    def test_an_insert_that_fails_anyway_is_rolled_back(self, env, monkeypatch):
        """D1320. The form's check cannot see a row another request commits in
        the same moment, so the route keeps its own handler -- and the failure it
        catches is a real `UniqueViolation`, which poisons the transaction. With
        no rollback the session answers `PendingRollbackError` to everything that
        follows in that request.

        The form's check is switched off here rather than raced, because the race
        is what the route's handler exists for."""
        from app.admin.forms import CreateOfflineInstanceForm
        monkeypatch.setattr(CreateOfflineInstanceForm, 'validate_domain',
                            lambda self, field: None)
        client = client_for(env, 'administer all communities')
        db.session.add(Instance(domain='offline.test',
                                inbox='https://offline.test/inbox',
                                created_at=utcnow(), gone_forever=True))
        db.session.commit()
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='offline.test')
        assert response.status_code == 302
        # No rollback here on purpose: this query IS the assertion. Without the
        # route's own rollback the session is still in the failed state and
        # raises PendingRollbackError rather than answering 1.
        assert Instance.query.filter_by(domain='offline.test').count() == 1
        assert get(client, '/admin/instance/create_offline').status_code == 200

    def test_no_domain_at_all(self, env):
        """D1321. The field had no validators at all, so this inserted an
        Instance with an empty domain and `inbox = 'https:///inbox'`. The shape
        check refuses it -- an empty string has no dot -- which is why there is
        no separate `DataRequired` to go stale beside it."""
        client = client_for(env, 'administer all communities')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='')
        assert response.status_code == 200
        assert Instance.query.filter_by(domain='').count() == 0

    @pytest.mark.parametrize('domain', ['offline.test/inbox', 'two words.test',
                                        'nodots', 'https://offline.test'])
    def test_something_that_is_not_a_domain(self, env, domain):
        """The value is interpolated into the inbox URL, so a path or a space in
        it builds an inbox pointing somewhere else."""
        client = client_for(env, 'administer all communities')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain=domain)
        assert response.status_code == 200
        assert Instance.query.filter_by(domain=domain).count() == 0

    def test_a_domain_already_here_is_refused_by_the_form(self, env):
        client = client_for(env, 'administer all communities')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='remote.test')
        assert response.status_code == 200
        assert Instance.query.filter_by(domain='remote.test').count() == 1

    def test_the_same_domain_in_another_case(self, env):
        client = client_for(env, 'administer all communities')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='REMOTE.test')
        assert response.status_code == 200
        assert Instance.query.filter(
            func.lower(Instance.domain) == 'remote.test').count() == 1

    def test_a_domain_with_stray_spaces_is_stored_trimmed(self, env):
        client = client_for(env, 'administer all communities')
        post(client, '/admin/instance/create_offline',
             csrf_token=env.token, domain='  Offline.TEST  ')
        instance = Instance.query.filter_by(domain='offline.test').one()
        assert instance.inbox == 'https://offline.test/inbox'

    def test_somebody_with_another_permission_may_not(self, env):
        client = client_for(env, 'edit cms pages')
        response = post(client, '/admin/instance/create_offline',
                        csrf_token=env.token, domain='offline.test')
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        assert Instance.query.filter_by(domain='offline.test').count() == 0


@pytest.fixture
def incoming(env):
    """A remote community, and somebody to give it to."""
    from types import SimpleNamespace
    community = make_community('incoming')
    community.ap_id = 'incoming@remote.test'
    community.instance_id = env.remote.id
    db.session.commit()
    owner = make_user(env.baseline.instance_local, 'newowner', local=True)
    db.session.commit()
    return SimpleNamespace(community=community, owner=owner)


class TestMovingACommunityHere:
    def move(self, env, client, incoming, **overrides):
        data = {'csrf_token': env.token, 'new_url': 'somewhere_else',
                'new_owner': 'y'}
        data.update(overrides)
        return post(client, f'/admin/community/{incoming.community.id}'
                            f'/move/{incoming.owner.id}', **data)

    def test_the_form(self, env, incoming):
        client = client_for(env, 'change instance settings')
        response = get(client, f'/admin/community/{incoming.community.id}'
                               f'/move/{incoming.owner.id}')
        assert response.status_code == 200

    def test_a_community_nobody_has(self, env, incoming):
        client = client_for(env, 'change instance settings')
        assert get(client, f'/admin/community/999999/move/'
                           f'{incoming.owner.id}').status_code == 404

    def test_a_new_owner_nobody_has(self, env, incoming):
        client = client_for(env, 'change instance settings')
        assert get(client, f'/admin/community/{incoming.community.id}'
                           f'/move/999999').status_code == 404

    def test_the_community_becomes_local(self, env, incoming):
        client = client_for(env, 'change instance settings')
        assert self.move(env, client, incoming).status_code == 302
        db.session.expire_all()
        community = db.session.get(Community, incoming.community.id)
        assert community.ap_id is None
        assert community.instance_id == 1
        assert community.name == 'somewhere_else'
        assert community.ap_domain == 'test.piefed.local'
        assert community.ap_profile_id == \
            'https://test.piefed.local/c/somewhere_else'
        assert community.ap_followers_url == \
            'https://test.piefed.local/c/somewhere_else/followers'

    def test_it_gets_its_own_keypair(self, env, incoming):
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming)
        db.session.expire_all()
        community = db.session.get(Community, incoming.community.id)
        assert community.private_key is not None
        assert 'BEGIN' in community.public_key

    def test_the_url_is_slugified(self, env, incoming):
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming, new_url='Some Where Else')
        db.session.expire_all()
        assert db.session.get(Community, incoming.community.id).name == \
            'some_where_else'

    def test_the_new_owner_owns_it(self, env, incoming):
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming)
        db.session.expire_all()
        assert db.session.get(Community, incoming.community.id).user_id == \
            incoming.owner.id

    def test_the_new_owner_can_moderate_it(self, env, incoming):
        """D1318. `is_owner=new_owner_user.id` is a user id in a Boolean column,
        which SQLAlchemy refuses -- and the bare `except` hid it, so the
        `community_member` row was never written and the new owner appeared in
        neither `Community.moderators()` nor `moderating_communities`."""
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming)
        db.session.expire_all()
        membership = CommunityMember.query.filter_by(
            user_id=incoming.owner.id,
            community_id=incoming.community.id).one()
        assert membership.is_owner is True
        assert membership.is_moderator is True

    def test_and_shows_up_as_a_moderator(self, env, incoming):
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming)
        db.session.expire_all()
        community = db.session.get(Community, incoming.community.id)
        assert incoming.owner.id in [m.user_id for m in community.moderators()]

    def test_a_new_owner_who_was_already_a_member_is_promoted(self, env, incoming):
        """One row, not two: the table has no unique constraint to stop a
        second."""
        make_community_member(incoming.owner, incoming.community)
        db.session.commit()
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming)
        db.session.expire_all()
        membership = CommunityMember.query.filter_by(
            user_id=incoming.owner.id,
            community_id=incoming.community.id).one()
        assert membership.is_owner is True
        assert membership.is_moderator is True

    def test_a_new_owner_who_was_banned_from_it_is_unbanned(self, env, incoming):
        member = make_community_member(incoming.owner, incoming.community)
        member.is_banned = True
        db.session.commit()
        client = client_for(env, 'change instance settings')
        self.move(env, client, incoming)
        db.session.expire_all()
        assert CommunityMember.query.filter_by(
            user_id=incoming.owner.id,
            community_id=incoming.community.id).one().is_banned is False

    def test_without_the_box_ticked_the_owner_is_left_alone(self, env, incoming):
        client = client_for(env, 'change instance settings')
        before = incoming.community.user_id
        self.move(env, client, incoming, new_owner='')
        db.session.expire_all()
        community = db.session.get(Community, incoming.community.id)
        assert community.user_id == before
        assert community.ap_id is None
        assert CommunityMember.query.filter_by(
            user_id=incoming.owner.id,
            community_id=incoming.community.id).count() == 0

    def test_a_url_that_is_taken_is_refused(self, env, incoming):
        make_community('taken')
        db.session.commit()
        client = client_for(env, 'change instance settings')
        response = self.move(env, client, incoming, new_url='taken')
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Community, incoming.community.id).ap_id == \
            'incoming@remote.test'

    def test_a_url_that_slugifies_onto_one_that_is_taken_is_refused_too(
            self, env, incoming):
        """D1319. `validate_new_url` compared the raw value, so 'My Community'
        passed and then collided at the database: `UniqueViolation` on
        `ix_community_ap_profile_id`, a 500 for the admin and a community left
        remote."""
        make_community('my_community')
        db.session.commit()
        client = client_for(env, 'change instance settings')
        response = self.move(env, client, incoming, new_url='My Community')
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Community, incoming.community.id).ap_id == \
            'incoming@remote.test'
        assert Community.query.filter(Community.ap_id == None,
                                      Community.name == 'my_community').count() == 1

    def test_a_remote_community_at_that_name_does_not_block_the_move(
            self, env, incoming):
        """The check is for a LOCAL community: a remote one's name is not in
        this instance's namespace."""
        elsewhere = make_community('somewhere_else')
        elsewhere.ap_id = 'somewhere_else@other.test'
        elsewhere.ap_profile_id = 'https://other.test/c/somewhere_else'
        elsewhere.ap_public_url = 'https://other.test/c/somewhere_else'
        db.session.commit()
        client = client_for(env, 'change instance settings')
        assert self.move(env, client, incoming).status_code == 302

    def test_no_url_at_all_is_refused(self, env, incoming):
        client = client_for(env, 'change instance settings')
        response = self.move(env, client, incoming, new_url='')
        assert response.status_code == 200
        db.session.expire_all()
        assert db.session.get(Community, incoming.community.id).ap_id == \
            'incoming@remote.test'

    def test_somebody_with_another_permission_may_not(self, env, incoming):
        client = client_for(env, 'edit cms pages')
        response = self.move(env, client, incoming)
        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        db.session.expire_all()
        assert db.session.get(Community, incoming.community.id).ap_id == \
            'incoming@remote.test'
