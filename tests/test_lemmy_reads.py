"""A community, a person and search in Lemmy's API for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2): each
answers Lemmy 0.19.11's model from alpha's handler, under Lemmy's names."""
import uuid

import pytest

from app import cache, db
from tests import lemmy_app_contract as contract
from tests.factories import make_community, make_community_member, make_post, make_post_reply, make_user


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person, author = make_user(None, 'lemmyreader', local=True), make_user(None, 'lemmywriter', local=True)
    community = make_community('lemmyreads')
    community.title = 'Lemmy reads'
    make_community_member(author, community, is_moderator=True)
    make_community_member(person, community)
    post = make_post(community, author, ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex[:10]}',
                     title='Findable penguin')
    reply = make_post_reply(post, author, body='a penguin reply')
    reply.path, reply.depth = [0, reply.id], 0
    db.session.commit()
    return {'person': person, 'author': author, 'community': community, 'post': post, 'client': app.test_client(),
            'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'}}


def _get(world, path, model, **args):
    response = world['client'].get(path, query_string=args, headers=world['headers'])
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert (found := contract.problems(body, model)) == [], found
    return body


@pytest.mark.parametrize('named', ['id', 'name'])
def test_a_community_with_its_moderators(world, named):
    args = {'id': world['community'].id} if named == 'id' else {'name': 'lemmyreads'}
    body = _get(world, '/api/v3/community', 'GetCommunityResponse', **args)
    assert body['community_view']['community']['title'] == 'Lemmy reads'
    assert body['community_view']['subscribed'] == 'Subscribed'
    assert [row['moderator']['name'] for row in body['moderators']] == ['lemmywriter']


def test_a_person_with_their_posts_and_comments(world):
    body = _get(world, '/api/v3/user', 'GetPersonDetailsResponse', person_id=world['author'].id, sort='New')
    assert body['person_view']['person']['name'] == 'lemmywriter'
    assert [view['post']['name'] for view in body['posts']] == ['Findable penguin']
    assert [view['comment']['content'] for view in body['comments']] == ['a penguin reply']
    assert [row['community']['name'] for row in body['moderates']] == ['lemmyreads']
    by_name = _get(world, '/api/v3/user', 'GetPersonDetailsResponse', username='lemmywriter')
    assert by_name['person_view']['person']['id'] == world['author'].id


@pytest.mark.parametrize('kind,listed', [('Posts', 'posts'), ('Communities', 'communities'), ('Users', 'users'),
                                         ('Comments', 'comments'), ('Url', 'posts')])
def test_a_search_of_one_kind_lists_that_kind(world, kind, listed):
    q = {'Communities': 'lemmyreads', 'Users': 'lemmywriter', 'Url': 'test.piefed.local'}.get(kind, 'penguin')
    body = _get(world, '/api/v3/search', 'SearchResponse', q=q, type_=kind, sort='TopAll', listing_type='All')
    assert body['type_'] == kind
    assert all(not body[other] for other in ('posts', 'communities', 'users', 'comments') if other != listed)


def test_a_search_of_everything_lists_each_kind(world):
    body = _get(world, '/api/v3/search', 'SearchResponse', q='penguin', listing_type='All', sort='New')
    assert body['type_'] == 'All'
    assert [view['post']['name'] for view in body['posts']] == ['Findable penguin']
    assert [view['comment']['content'] for view in body['comments']] == ['a penguin reply']


def test_a_search_with_nothing_to_find_is_refused(world):
    response = world['client'].get('/api/v3/search', query_string={'type_': 'Posts'})
    assert response.status_code == 400 and set(response.get_json()) == {'error'}
