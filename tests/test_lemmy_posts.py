"""Posts in Lemmy's API for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2-§4, ruling L3): the feeds and a post
decode as Lemmy 0.19.11's, list what alpha lists for the same person, page by Lemmy's cursor, and show PieFed's polls,
galleries and events as the nearest Lemmy post."""
import uuid

import pytest

from app import cache, db
from app.constants import POST_TYPE_POLL
from app.lemmy import shapes
from tests import lemmy_app_contract as contract
from tests.factories import make_community, make_community_member, make_poll, make_poll_choice, make_post, \
    make_post_reply, make_user, make_user_block


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person, author = make_user(None, 'lemmyposts', local=True), make_user(None, 'lemmyauthor', local=True)
    joined, other = make_community('lemmyjoined'), make_community('lemmyother')
    make_community_member(person, joined)
    db.session.commit()
    return {'person': person, 'author': author, 'joined': joined, 'other': other, 'client': app.test_client(),
            'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'}}


def _post(world, community=None, title='A post', author=None):
    return make_post(community or world['joined'], author or world['author'],
                     ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex[:10]}', title=title)


def _list(world, signed_in=True, **args):
    response = world['client'].get('/api/v3/post/list', query_string=args,
                                    headers=world['headers'] if signed_in else {})
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert (found := contract.problems(body, 'GetPostsResponse')) == [], found
    return body


def _titles(body) -> list[str]:
    return [view['post']['name'] for view in body['posts']]


def test_the_feed_decodes_as_lemmys_with_lemmys_names(world):
    post = _post(world, title='Lemmy names')
    body = _list(world, signed_in=False, type_='All', sort='New')
    view = next(view for view in body['posts'] if view['post']['id'] == post.id)
    assert view['post']['creator_id'] == world['author'].id and view['creator']['name'] == 'lemmyauthor'
    assert view['community']['name'] == 'lemmyjoined' and view['post']['featured_community'] is False


def test_the_subscribed_feed_is_the_persons_own_and_needs_a_login(world):
    _post(world, title='Joined'), _post(world, world['other'], title='Elsewhere')
    assert _titles(_list(world, type_='Subscribed', sort='New')) == ['Joined']
    response = world['client'].get('/api/v3/post/list', query_string={'type_': 'Subscribed'})
    assert response.status_code == 400 and response.get_json() == {'error': 'incorrect login'}


def test_a_blocked_authors_posts_are_left_out_as_alpha_leaves_them(world):
    blocked = make_user(None, 'lemmyblocked', local=True)
    _post(world, title='Kept'), _post(world, title='Blocked', author=blocked)
    make_user_block(world['person'], blocked)
    assert _titles(_list(world, type_='All', sort='New', community_id=world['joined'].id)) == ['Kept']


@pytest.mark.parametrize('sort', ['Hot', 'New', 'Old', 'Active', 'Scaled', 'TopDay', 'TopAll', 'TopSixHour',
                                  'MostComments', 'NewComments', 'Controversial'])
def test_each_of_lemmys_post_sorts_is_taken(world, sort):
    _post(world)
    assert len(_list(world, sort=sort, community_id=world['joined'].id)['posts']) == 1


def test_a_sort_that_is_no_sort_is_refused_in_lemmys_shape(world):
    response = world['client'].get('/api/v3/post/list', query_string={'sort': 'Sideways'})
    assert response.status_code == 400 and set(response.get_json()) == {'error'}


def test_the_next_page_is_lemmys_cursor(world):
    for title in ('One', 'Two', 'Three'):
        _post(world, title=title)
    first = _list(world, sort='New', community_id=world['joined'].id, limit=2)
    assert len(first['posts']) == 2 and first['next_page'] == '2'
    rest = _list(world, sort='New', community_id=world['joined'].id, limit=2, page_cursor=first['next_page'])
    assert len(rest['posts']) == 1 and rest['next_page'] is None


def test_a_community_is_named_by_its_name_too(world):
    _post(world, title='Here'), _post(world, world['other'], title='There')
    assert _titles(_list(world, sort='New', community_name='lemmyother')) == ['There']


def test_a_post_decodes_with_its_community_and_is_found_by_a_comment(world):
    post = _post(world, title='The post')
    reply = make_post_reply(post, world['author'])
    for args in ({'id': post.id}, {'comment_id': reply.id}):
        response = world['client'].get('/api/v3/post', query_string=args, headers=world['headers'])
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        assert (found := contract.problems(body, 'GetPostResponse')) == [], found
        assert body['post_view']['post']['name'] == 'The post' and body['community_view']['subscribed'] == 'Subscribed'


@pytest.mark.parametrize('args', [{}, {'id': 999999}, {'comment_id': 999999}, {'id': 'x'}])
def test_no_such_post_is_not_found(world, args):
    response = world['client'].get('/api/v3/post', query_string=args)
    assert response.status_code == 404 and response.get_json() == {'error': 'not_found'}


def test_a_poll_shows_its_choices_and_where_to_vote(world):
    post = _post(world, title='A poll')
    post.body, post.type = 'Pick one.', POST_TYPE_POLL
    make_poll(post)
    make_poll_choice(post, 'Tea', sort_order=0), make_poll_choice(post, 'Coffee', sort_order=1)
    body = world['client'].get('/api/v3/post', query_string={'id': post.id}).get_json()['post_view']['post']['body']
    assert body == f'Pick one.\n\n- Tea (0)\n- Coffee (0)\n\n[Vote on the web]({post.profile_id()})'


ALPHA_POST = {'id': 1, 'title': 't', 'user_id': 2, 'community_id': 3, 'removed': False, 'locked': False,
              'published': '2026-01-01T00:00:00.000000Z', 'deleted': False, 'nsfw': False, 'ap_id': 'https://x/post/1',
              'local': True, 'language_id': 0, 'sticky': False, 'instance_sticky': False, 'post_type': 'Discussion'}


def test_a_gallery_is_an_image_post_of_its_first_picture_with_the_rest_in_its_body():
    gallery = [{'url': 'https://x/1.png', 'alt_text': 'one'}, {'url': 'https://x/2.png', 'alt_text': None},
               {'url': 'https://x/3.png', 'alt_text': 'three'}]
    shaped = shapes.post({**ALPHA_POST, 'post_type': 'Gallery', 'url': 'https://x/post/1', 'body': 'Look.',
                          'extensions': {'gallery': gallery}})
    assert shaped['url'] == 'https://x/1.png'
    assert shaped['body'] == 'Look.\n\n![](https://x/2.png)\n\n![three](https://x/3.png)'
    assert shapes.post({**ALPHA_POST, 'post_type': 'Gallery', 'extensions': {'gallery': gallery[:1]}})['body'] is None


@pytest.mark.parametrize('event,first_lines', [
    ({'start': '2026-12-01T18:00:00Z', 'timezone': 'UTC',
      'location': {'address': '1 Main St', 'city': 'Springfield', 'country': ''}},
     '**When:** 2026-12-01T18:00:00Z (UTC)\n\n**Where:** 1 Main St, Springfield'),
    ({'start': '2026-12-01T18:00:00Z', 'end': '2026-12-01T20:00:00Z', 'timezone': 'UTC',
      'online_link': 'https://meet.example/1'},
     '**When:** 2026-12-01T18:00:00Z – 2026-12-01T20:00:00Z (UTC)\n\n**Where:** https://meet.example/1'),
    ({'start': '2026-12-01T18:00:00Z', 'timezone': 'UTC'}, '**When:** 2026-12-01T18:00:00Z (UTC)')])
def test_an_events_body_begins_with_when_and_where(event, first_lines):
    assert shapes.post({**ALPHA_POST, 'event': event, 'body': 'Come.'})['body'] == f'{first_lines}\n\nCome.'
    assert shapes.post({**ALPHA_POST, 'event': event})['body'] == first_lines
