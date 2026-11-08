"""Mastodon's search, v2 (plan W1.2): accounts, statuses and hashtags, narrowed by type, paged by offset."""
from datetime import datetime, timedelta
from itertools import count

import pytest

from app import db
from app.constants import VISIBILITY_FOLLOWERS
from app.mastodon import ids
from app.models import Tag
from app.oauth_server import tokens
from tests.factories import make_community, make_post, make_post_reply, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read', None)
    me = make_user(None, 'searcher', local=True)
    access, _refresh = tokens.issue_token(registered, me, 'read')
    return {'me': me, 'community': make_community('searched'), 'token': access, 'client': app.test_client()}


def _search(world, token=True, **query):
    headers = {'Authorization': f"Bearer {world['token']}"} if token else {}
    response = world['client'].get('/api/v2/search', headers=headers, query_string=query)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _post(world, title, minutes=0, **columns):
    post = make_post(world['community'], world['me'], ap_id=f'https://test.piefed.local/post/q{next(_SERIAL)}',
                     title=title)
    post.body = title
    post.posted_at = datetime(2030, 1, 1) + timedelta(minutes=minutes)
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def test_accounts_by_name(world):
    make_user(None, 'pieeater', local=True)
    hidden = make_user(None, 'pieshy', local=True)
    hidden.searchable = False
    db.session.commit()
    found = _search(world, q='@pieea', type='accounts', token=False)
    assert [account['username'] for account in found['accounts']] == ['pieeater']
    assert found['statuses'] == [] and found['hashtags'] == []


def test_statuses_posts_and_comments_by_their_text_newest_first(world):
    older = _post(world, 'rhubarb crumble', minutes=1)
    comment = make_post_reply(older, world['me'], body='rhubarb again')
    comment.posted_at = datetime(2030, 1, 1, 0, 5)
    _post(world, 'rhubarb secret', minutes=2, visibility=VISIBILITY_FOLLOWERS)
    _post(world, 'something else', minutes=3)
    db.session.commit()
    found = _search(world, q='rhubarb', type='statuses')
    assert [status['id'] for status in found['statuses']] == [ids.status_id(comment), ids.status_id(older)]
    assert [status['id'] for status in _search(world, q='rhubarb', type='statuses', limit=1, offset=1)['statuses']] \
        == [ids.status_id(older)]


def test_hashtags_by_prefix_most_used_first(world):
    db.session.add_all([Tag(name='piecrust', post_count=1), Tag(name='pie', post_count=9),
                        Tag(name='piebanned', post_count=50, banned=True), Tag(name='cake', post_count=99)])
    db.session.commit()
    found = _search(world, q='#Pie', type='hashtags')
    assert [tag['name'] for tag in found['hashtags']] == ['pie', 'piecrust']
    assert found['hashtags'][0]['url'].endswith('/tag/pie') and found['hashtags'][0]['history'] == []


def test_no_type_searches_all_three_and_no_query_finds_nothing(world):
    _post(world, 'quince jelly')
    db.session.add(Tag(name='quince', post_count=1))
    make_user(None, 'quincefan', local=True)
    db.session.commit()
    found = _search(world, q='quince')
    assert found['accounts'] and found['statuses'] and found['hashtags']
    assert _search(world, q='  ') == {'accounts': [], 'statuses': [], 'hashtags': []}


@pytest.mark.parametrize('limit, offset', [('x', 'y'), ('0', '99999999')], ids=['letters', 'extremes'])
def test_odd_limits_and_offsets_are_held_in_range(world, limit, offset):
    assert _search(world, q='anything', limit=limit, offset=offset)['statuses'] == []
