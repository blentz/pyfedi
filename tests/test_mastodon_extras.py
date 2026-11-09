"""The small Mastodon reads apps make at startup and from their menus: lists, custom emojis, preferences, filters,
followed tags, markers (plan W1.2)."""
import pytest

from app import db
from app.models import Emoji
from app.oauth_server import tokens
from tests.factories import make_community, make_community_member, make_user


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'extrasme', local=True)
    access, _refresh = tokens.issue_token(registered, me, 'read')
    return {'me': me, 'app': registered, 'token': access, 'client': app.test_client()}


def _get(world, path, token=True):
    headers = {'Authorization': f"Bearer {token if isinstance(token, str) else world['token']}"} if token else {}
    return world['client'].get(path, headers=headers)


def test_the_communities_joined_are_the_lists(world):
    joined, banned_from, _other = make_community('listed'), make_community('banned'), make_community('unjoined')
    joined.title = 'Listed Community'
    make_community_member(world['me'], joined)
    member = make_community_member(world['me'], banned_from)
    member.is_banned = True
    db.session.commit()
    response = _get(world, '/api/v1/lists')
    assert response.status_code == 200
    assert response.get_json() == [{'id': str(joined.id), 'title': 'Listed Community', 'replies_policy': 'list',
                                    'exclusive': False}]
    assert _get(world, f'/api/v1/lists/{joined.id}').get_json()['title'] == 'Listed Community'
    assert _get(world, f'/api/v1/lists/{banned_from.id}').status_code == 404
    assert _get(world, '/api/v1/lists/99999999999').status_code == 404
    assert _get(world, '/api/v1/lists', token=False).status_code == 401


def test_custom_emojis_are_this_instances_own(world):
    db.session.add_all([Emoji(token=':pie:', url='/static/media/emoji/pie.png', category='food', instance_id=1),
                        Emoji(token=':far:', url='https://far.example/far.png', instance_id=None),
                        Emoji(token=':blank:', url=None, instance_id=1)])
    db.session.commit()
    emojis = _get(world, '/api/v1/custom_emojis', token=False).get_json()
    assert [emoji['shortcode'] for emoji in emojis] == ['pie']
    assert emojis[0]['url'].startswith('http') and emojis[0]['url'].endswith('/static/media/emoji/pie.png')
    assert emojis[0]['category'] == 'food' and emojis[0]['visible_in_picker'] is True
    app_token, _none = tokens.issue_token(world['app'], None, 'read', with_refresh=False)
    assert _get(world, '/api/v1/custom_emojis', token=app_token).status_code == 200


@pytest.mark.parametrize('path, expected', [('/api/v1/filters', []), ('/api/v2/filters', []),
                                            ('/api/v1/followed_tags', []), ('/api/v1/markers', {})],
                         ids=['filters-v1', 'filters-v2', 'followed-tags', 'markers'])
def test_what_piefed_keeps_no_copy_of_is_empty(world, path, expected):
    response = _get(world, path)
    assert response.status_code == 200 and response.get_json() == expected
    assert _get(world, path, token=False).status_code == 401


def test_preferences_are_mastodons_defaults(world):
    preferences = _get(world, '/api/v1/preferences').get_json()
    assert preferences['posting:default:visibility'] == 'public'
    assert preferences['reading:expand:spoilers'] is False


@pytest.mark.parametrize('path', ['/api/v1/trends', '/api/v1/trends/tags', '/api/v1/trends/statuses',
                                  '/api/v1/trends/links', '/api/v2/suggestions'])
def test_nothing_trends_and_nobody_is_suggested(world, path):
    response = _get(world, path, token=False)
    assert response.status_code == 200 and response.get_json() == []
