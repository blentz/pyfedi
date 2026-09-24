"""`name@server` wherever a caller can send one.

Sub-project 90 -- a sweep, not a module. `normalise_actor_string` turns a
handle into a pair, and seven other places did the same job themselves with
`name, domain = something.split('@')`. Every one of them is handed its string
by a query parameter:

* `/api/alpha/post/list?community_name=`  (two sites, listing and lookup)
* `/api/alpha/post/list2?community_name=` (the same two, duplicated)
* `/api/alpha/user?username=`
* `/api/alpha/resolve_object?q=`
* `community_view(str)` and `get_comm_flair_list(str)`

Two defects:

* the helper itself read `actor[0]` before checking the string had one, so
  `''` was `IndexError: string index out of range`, and it read `parts[1]`
  after splitting, so `evil@attacker.test@victim.test` answered
  `('evil', 'attacker.test')` -- a lookup against a server the caller never
  named, with the rest of the handle silently dropped (D1274);
* the seven copies raised `ValueError: too many values to unpack (expected
  2)` out of the request. The alpha API's error handler turns an exception
  into a 400 carrying its message, so the caller was answered with a Python
  internal rather than with "that is not a handle" (D1275).

All seven now go through the one helper. Five refuse with `invalid_request`;
the two in the listing blocks of `get_post_list` and `get_post_list2` need no
refusal of their own, because the lookup block above each one has already
made it, and ('', '') simply matches nothing.
"""
import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import normalise_actor_string
from app.models import Language, Site
from tests.factories import a_keypair, make_community, make_community_member

NOT_HANDLES = ['a@b@c', 'onlyname@', '@onlydomain', '@', 'a@b@', '',
               '   ', '@@', 'nomarker']


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(
        code='en').one().id
    community = make_community('probeland')
    reader = api_baseline.user2
    reader.private_key, reader.public_key = a_keypair()
    db.session.commit()
    make_community_member(reader, community)
    return SimpleNamespace(client=app.test_client(), community=community,
                           reader=reader, baseline=api_baseline)


class TestTheHelperItself:
    """D1274."""

    @pytest.mark.parametrize('handle,expected', [
        ('someone@remote.test', ('someone', 'remote.test')),
        ('@someone@remote.test', ('someone', 'remote.test')),
        ('!aplace@remote.test', ('aplace', 'remote.test')),
        ('~afeed@remote.test', ('afeed', 'remote.test')),
        ('  someone@remote.test  ', ('someone', 'remote.test')),
        ('SomeOne@Remote.Test', ('someone', 'remote.test')),
    ])
    def test_a_handle_is_split_and_lowercased(self, handle, expected):
        assert normalise_actor_string(handle) == expected

    @pytest.mark.parametrize('handle', NOT_HANDLES)
    def test_anything_else_is_a_pair_of_nothing(self, handle):
        assert normalise_actor_string(handle) == ('', '')

    def test_an_empty_string(self):
        """`actor[0]` was `IndexError: string index out of range`."""
        assert normalise_actor_string('') == ('', '')

    def test_none(self):
        assert normalise_actor_string(None) == ('', '')

    def test_a_marker_and_nothing_else(self):
        assert normalise_actor_string('@') == ('', '')

    def test_a_marker_followed_by_nothing_but_a_domain(self):
        """The marker is stripped first, so this is the one shape that
        reaches the `not parts[0]` check with an empty name."""
        assert normalise_actor_string('!@remote.test') == ('', '')

    def test_a_third_part_is_not_quietly_dropped(self):
        """`parts[1]` answered ('evil', 'attacker.test') and threw the rest
        away, so a handle naming one server was looked up against another."""
        assert normalise_actor_string(
            'evil@attacker.test@victim.test') == ('', '')

    def test_a_handle_whose_domain_holds_a_port(self):
        assert normalise_actor_string('someone@remote.test:8080') == \
            ('someone', 'remote.test:8080')


class TestTheCommunityNameOnAPostListing:
    """D1275. Two sites in `get_post_list`, and two more in the duplicated
    `get_post_list2` beside it."""

    @pytest.mark.parametrize('handle', ['a@b@c', 'onlyname@', '@onlydomain',
                                        '@', 'a@b@'])
    def test_a_name_that_is_not_one_is_refused(self, env, handle):
        response = env.client.get(
            f'/api/alpha/post/list?community_name={handle}')
        assert response.status_code == 400
        assert response.json['message'] == 'invalid_request'

    @pytest.mark.parametrize('handle', ['a@b@c', '@', 'a@b@'])
    def test_the_same_when_signed_in(self, env, handle):
        response = env.client.get(
            f'/api/alpha/post/list?community_name={handle}',
            headers=auth(env.reader))
        assert response.status_code == 400
        assert response.json['message'] == 'invalid_request'

    @pytest.mark.parametrize('handle', ['a@b@c', 'onlyname@', '@onlydomain',
                                        '@', 'a@b@'])
    def test_the_second_listing_refuses_them_too(self, env, handle):
        response = env.client.get(
            f'/api/alpha/post/list2?community_name={handle}')
        assert response.status_code == 400
        assert response.json['message'] == 'invalid_request'

    def test_no_python_internals_reach_the_caller(self, env):
        """The message used to be `too many values to unpack (expected 2)`."""
        response = env.client.get('/api/alpha/post/list?community_name=a@b@c')
        assert 'unpack' not in response.json['message']

    def test_a_bare_name_still_means_a_local_community(self, env):
        response = env.client.get(
            '/api/alpha/post/list?community_name=probeland')
        assert response.status_code == 200

    def test_a_full_handle_still_works(self, env):
        server = current_app.config['SERVER_NAME']
        response = env.client.get(
            f'/api/alpha/post/list?community_name=probeland@{server}')
        assert response.status_code == 200

    def test_a_community_nobody_hosts_is_not_an_invalid_request(self, env):
        """A well-formed handle for a community that does not exist is a
        different answer from a handle that is not one."""
        response = env.client.get(
            '/api/alpha/post/list?community_name=nosuch@nowhere.test')
        assert response.json.get('message') != 'invalid_request'


class TestTheUsernameOnAUserLookup:
    @pytest.mark.parametrize('handle', ['a@b@c', 'onlyname@', '@onlydomain',
                                        '@', 'a@b@'])
    def test_a_username_that_is_not_one_is_refused(self, env, handle):
        response = env.client.get(f'/api/alpha/user?username={handle}')
        assert response.status_code == 400
        assert response.json['message'] == 'invalid_request'

    def test_a_bare_name_still_means_a_local_account(self, env):
        response = env.client.get(
            f'/api/alpha/user?username={env.reader.user_name}')
        assert response.status_code == 200

    def test_a_local_account_by_its_full_handle(self, env):
        server = current_app.config['SERVER_NAME']
        response = env.client.get(
            f'/api/alpha/user?username={env.reader.user_name}@{server}')
        assert response.status_code == 200

    def test_the_handle_is_matched_without_regard_to_case(self, env):
        server = current_app.config['SERVER_NAME']
        response = env.client.get(
            f'/api/alpha/user?username={env.reader.user_name.upper()}@{server.upper()}')
        assert response.status_code == 200


class TestResolvingAnObjectByHandle:
    @pytest.mark.parametrize('query', ['!a@b@c', '@a@b@c', '~a@b@c',
                                       '!a@b@', '@@', '~'])
    def test_a_handle_that_is_not_one_finds_nothing(self, env, query):
        response = env.client.get(f'/api/alpha/resolve_object?q={query}',
                                  headers=auth(env.reader))
        assert response.status_code == 400
        assert response.json['message'] == 'No object found.'
        assert 'unpack' not in response.json['message']


class TestTheViewHelpersTakenDirectly:
    """`community_view` and `get_comm_flair_list` accept a handle as a
    string, and both split it themselves."""

    @pytest.mark.parametrize('handle', ['a@b@c', 'onlyname@', '@onlydomain',
                                        '@', 'a@b@'])
    def test_community_view_refuses_a_handle_that_is_not_one(self, env,
                                                             handle):
        from app.api.alpha.views import community_view
        with pytest.raises(Exception, match='invalid_request'):
            community_view(handle, variant=1)

    @pytest.mark.parametrize('handle', ['a@b@c', '@', 'a@b@'])
    def test_the_flair_list_refuses_them_too(self, env, handle):
        from app.shared.community import get_comm_flair_list
        with pytest.raises(Exception, match='invalid_request'):
            get_comm_flair_list(handle)

    def test_a_well_formed_handle_nobody_hosts_is_a_different_answer(self,
                                                                     env):
        from app.api.alpha.views import community_view
        with pytest.raises(Exception) as caught:
            community_view('nosuch@nowhere.test', variant=1)
        assert 'invalid_request' not in str(caught.value)

    def test_a_community_that_is_here_is_found_by_its_handle(self, env):
        from app.api.alpha.views import community_view
        server = current_app.config['SERVER_NAME']
        view = community_view(f'probeland@{server}', variant=1)
        assert view['name'] == 'probeland'

    def test_the_flair_list_of_a_community_that_is_here(self, env):
        from app.shared.community import get_comm_flair_list
        server = current_app.config['SERVER_NAME']
        assert get_comm_flair_list(f'probeland@{server}') == []


class TestASearchForAKindOfThingThatIsNotOne:
    """Not a handle defect -- the arc this covers came uncovered when the two
    redundant guards above were removed, and the floor is a ratchet."""

    def test_a_type_nobody_offers_finds_nothing(self, env):
        from app.api.alpha.utils.misc import get_search
        result = get_search(None, {'q': 'anything', 'type_': 'Sandwiches'})
        assert result['communities'] == []
        assert result['posts'] == []
        assert result['users'] == []
        assert result['comments'] == []

    def test_a_search_with_no_type_is_refused(self, env):
        from app.api.alpha.utils.misc import get_search
        with pytest.raises(Exception, match='missing parameters for search'):
            get_search(None, {'q': 'anything'})
