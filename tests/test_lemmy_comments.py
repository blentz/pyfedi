"""Comments in Lemmy's API for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2, §3): a post's thread decodes as
Lemmy 0.19.11's GetCommentsResponse with Lemmy's names, all of it to the depth asked when no page is, as Lemmy
answers; a comment the viewer may not see stays a stub in its place."""
import uuid

import pytest

from app import cache, db
from app.constants import VISIBILITY_FOLLOWERS
from app.lemmy import comments as lemmy_comments
from tests import lemmy_app_contract as contract
from tests.factories import make_community, make_post, make_post_reply, make_user
from tests.test_api_reply_list import a_child


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person, author = make_user(None, 'lemmycomments', local=True), make_user(None, 'lemmycommenter', local=True)
    community = make_community('lemmycomments')
    post = make_post(community, author, ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex[:10]}')
    db.session.commit()
    return {'person': person, 'author': author, 'post': post, 'client': app.test_client(), 'flask': app,
            'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'}}


def _top(world, body='top'):
    reply = make_post_reply(world['post'], world['author'], body=body)
    reply.path, reply.depth = [0, reply.id], 0
    db.session.commit()
    return reply


def _thread(world, **args):
    response = world['client'].get('/api/v3/comment/list', headers=world['headers'],
                                   query_string={'post_id': world['post'].id, 'type_': 'All', **args})
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert (found := contract.problems(body, 'GetCommentsResponse')) == [], found
    return body['comments']


def test_a_thread_decodes_with_lemmys_names(world):
    top = _top(world, 'Hello')
    child = a_child(top, world['person'], 'Hi back')
    views = {view['comment']['id']: view for view in _thread(world, max_depth=6, sort='Old')}
    assert views[top.id]['comment']['content'] == 'Hello' and views[top.id]['creator']['name'] == 'lemmycommenter'
    assert views[child.id]['comment']['path'] == f'0.{top.id}.{child.id}'
    assert views[child.id]['comment']['creator_id'] == world['person'].id
    assert views[child.id]['post']['id'] == world['post'].id


@pytest.mark.parametrize('sort', ['Hot', 'Top', 'New', 'Old', 'Controversial'])
def test_each_of_lemmys_comment_sorts_is_taken(world, sort):
    _top(world)
    assert len(_thread(world, sort=sort, max_depth=6)) == 1


def test_a_thread_asked_by_depth_alone_is_all_of_it_up_to_lemmys_cap(world, monkeypatch):
    monkeypatch.setitem(world['flask'].config, 'PAGE_LENGTH', 2)
    for number in range(5):
        _top(world, f'comment {number}')
    assert len(_thread(world, max_depth=6)) == 5
    monkeypatch.setattr(lemmy_comments, 'WHOLE_THREAD', 3)
    assert len(_thread(world, max_depth=6)) == 3


def test_a_page_asked_for_is_that_page(world):
    for number in range(3):
        _top(world, f'comment {number}')
    assert len(_thread(world, max_depth=6, limit=2, page=1)) == 2
    assert len(_thread(world, limit=2, page=2)) == 1


def test_a_comment_the_viewer_may_not_see_is_a_stub_that_still_decodes(world):
    hidden = _top(world, 'only for followers')
    hidden.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    stub = next(view for view in _thread(world, max_depth=6) if view['comment']['id'] == hidden.id)
    assert stub['comment']['content'] == '' and stub['creator']['name'] == ''


def test_a_sort_that_is_no_sort_is_refused(world):
    response = world['client'].get('/api/v3/comment/list', query_string={'post_id': world['post'].id, 'sort': 'Best'})
    assert response.status_code == 400 and set(response.get_json()) == {'error'}
