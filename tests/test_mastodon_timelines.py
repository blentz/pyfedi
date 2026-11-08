"""Mastodon's timelines, a status and its thread (plan W1.2; rulings M2-M5). Paging is by max_id, since_id and min_id
with a Link header, which every app follows (plan W1.0 inventory)."""
import re
from datetime import datetime, timedelta
from itertools import count

import pytest

from app import db
from app.constants import POST_TYPE_IMAGE, VISIBILITY_FOLLOWERS
from app.mastodon import ids
from app.models import Tag
from app.oauth_server import tokens
from tests.factories import make_community, make_community_member, make_file, make_follow, make_post, \
    make_post_reply, make_user
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture

_SERIAL = count()
START = datetime(2030, 1, 1, 12, 0, 0)   # newer than anything the shared baseline seeds


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'tlviewer', local=True)
    friend, stranger = make_user(None, 'tlfriend', local=True), make_user(None, 'tlstranger', local=True)
    make_follow(me, friend)
    community = make_community('timelines')
    community.show_all = True
    db.session.commit()
    access, _refresh = tokens.issue_token(registered, me, 'read')
    return {'me': me, 'friend': friend, 'stranger': stranger, 'community': community, 'token': access,
            'app': registered, 'client': app.test_client()}


def _post(world, author=None, minutes=0, community=None, **columns):
    post = make_post(community or world['community'], author or world['stranger'],
                     ap_id=f'https://test.piefed.local/post/tl{next(_SERIAL)}', title=columns.pop('title', 'A post'))
    post.posted_at = START + timedelta(minutes=minutes)
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _reply(world, post, author, minutes=0, parent=None):
    reply = make_post_reply(post, author)
    reply.posted_at = START + timedelta(minutes=minutes)
    if parent is not None:
        reply.parent_id = parent.id
    db.session.commit()
    return reply


def _get(world, path, token=True, **query):
    headers = {'Authorization': f"Bearer {world['token']}"} if token else {}
    return world['client'].get(path, headers=headers, query_string=query)


def _ids(response, among=None):
    """The ids answered, in order; with `among`, only those of these rows (the shared baseline seeds posts too)."""
    assert response.status_code == 200, response.get_data(as_text=True)
    answered = [status['id'] for status in response.get_json()]
    return answered if among is None else [value for value in answered if value in _sid(*among)]


def _sid(*rows):
    return [ids.status_id(row) for row in rows]


def _links(response) -> dict:
    return {rel: url for url, rel in re.findall(r'<([^>]+)>; rel="(\w+)"', response.headers.get('Link', ''))}


def test_the_public_timeline_is_newest_first_for_anyone(world):
    old, new = _post(world, minutes=1), _post(world, minutes=2)
    assert _ids(_get(world, '/api/v1/timelines/public', token=False))[:2] == _sid(new, old)


def test_pages_follow_the_link_header_with_no_gap_or_repeat(world):
    posts = [_post(world, minutes=minute) for minute in range(5)]
    first = _get(world, '/api/v1/timelines/public', limit=2)
    assert _ids(first) == _sid(posts[4], posts[3])
    older = _links(first)['next']
    assert 'max_id=' + ids.status_id(posts[3]) in older and 'limit=2' in older
    second = world['client'].get(older, headers={'Authorization': f"Bearer {world['token']}"})
    assert _ids(second) == _sid(posts[2], posts[1])
    assert 'min_id=' + ids.status_id(posts[4]) in _links(first)['prev']


def test_posts_in_one_second_page_by_their_row(world):
    same = [_post(world) for _ in range(3)]   # one instant, as a remote server may claim
    first = _ids(_get(world, '/api/v1/timelines/public', limit=2))
    rest = _ids(_get(world, '/api/v1/timelines/public', limit=2, max_id=first[-1]))[:1]
    assert sorted(first + rest) == sorted(_sid(*same)) and len(set(first + rest)) == 3


def test_min_id_gives_the_oldest_newer_ones_and_since_id_the_newest(world):
    posts = [_post(world, minutes=minute) for minute in range(5)]
    assert _ids(_get(world, '/api/v1/timelines/public', limit=2, min_id=ids.status_id(posts[0]))) == \
        _sid(posts[2], posts[1])
    assert _ids(_get(world, '/api/v1/timelines/public', limit=2, since_id=ids.status_id(posts[0]))) == \
        _sid(posts[4], posts[3])


def test_a_bound_whose_status_is_gone_still_pages_by_its_second(world):
    posts = [_post(world, minutes=minute) for minute in range(3)]
    gone = ids.status_id(posts[1])
    db.session.delete(posts[1])
    db.session.commit()
    assert _ids(_get(world, '/api/v1/timelines/public', max_id=gone, limit=1)) == _sid(posts[0])


@pytest.mark.parametrize('limit, expected', [('0', 1), ('100', 40), ('x', 20)], ids=['zero', 'over', 'nonsense'])
def test_the_limit_is_held_between_one_and_forty(world, limit, expected):
    for minute in range(45):
        _post(world, minutes=minute)
    assert len(_ids(_get(world, '/api/v1/timelines/public', limit=limit))) == expected


def test_no_statuses_no_link(world):
    response = _get(world, '/api/v1/timelines/tag/nothingtagged')
    assert response.get_json() == [] and not {'next', 'prev'} & set(_links(response))


def test_only_media_local_and_remote(world):
    picture = make_file(source_url='https://cdn.example/p.png')
    image = _post(world, minutes=1, type=POST_TYPE_IMAGE, image_id=picture.id)
    text = _post(world, minutes=2)
    assert _ids(_get(world, '/api/v1/timelines/public', only_media='true'), (image, text)) == _sid(image)
    assert _ids(_get(world, '/api/v1/timelines/public', local='true'))[:2] == _sid(text, image)
    assert _ids(_get(world, '/api/v1/timelines/public', remote='true'), (image, text)) == []


def test_a_tag_timeline_is_the_posts_carrying_it(world):
    tagged, plain = _post(world, minutes=1), _post(world, minutes=2)
    tagged.tags.append(Tag(name='pie', display_as='Pie'))
    db.session.commit()
    assert _ids(_get(world, '/api/v1/timelines/tag/Pie')) == _sid(tagged)
    assert _ids(_get(world, '/api/v1/timelines/tag/nothing')) == []


def test_home_is_the_people_followed_and_oneself_posts_and_comments(world):
    """M2: people only; a community joined is a list."""
    theirs = _post(world, author=world['friend'], minutes=1)
    mine = _post(world, author=world['me'], minutes=2)
    _post(world, author=world['stranger'], minutes=3)
    comment = _reply(world, theirs, world['friend'], minutes=4)
    _reply(world, theirs, world['stranger'], minutes=5)
    assert _ids(_get(world, '/api/v1/timelines/home')) == _sid(comment, mine, theirs)
    assert _get(world, '/api/v1/timelines/home', token=False).status_code == 401


def test_home_pages_across_posts_and_comments(world):
    post = _post(world, author=world['friend'], minutes=0)
    rows = [post]
    for minute in range(1, 5):
        rows.append(_reply(world, post, world['friend'], minutes=minute) if minute % 2 else
                    _post(world, author=world['friend'], minutes=minute))
    first = _get(world, '/api/v1/timelines/home', limit=3)
    assert _ids(first) == _sid(*reversed(rows[2:]))
    second = world['client'].get(_links(first)['next'], headers={'Authorization': f"Bearer {world['token']}"})
    assert _ids(second) == _sid(rows[1], rows[0])


def test_home_pages_by_a_comment_or_a_post_as_the_bound(world):
    """A bound from either table cuts both: a post and a comment posted the same instant sort post first."""
    post = _post(world, author=world['friend'], minutes=0)
    comment = _reply(world, post, world['friend'], minutes=0)
    later = _post(world, author=world['friend'], minutes=1)
    by_comment = _get(world, '/api/v1/timelines/home', max_id=ids.status_id(comment))
    assert _ids(by_comment) == _sid(post)
    assert _ids(_get(world, '/api/v1/timelines/home', min_id=ids.status_id(post))) == _sid(later, comment)
    assert _ids(_get(world, '/api/v1/timelines/home', since_id=ids.status_id(comment))) == _sid(later)
    assert _ids(_get(world, '/api/v1/timelines/home', max_id=ids.status_id(later))) == _sid(comment, post)


def test_a_followers_only_post_reaches_a_followers_home_only(world):
    hidden = _post(world, author=world['friend'], visibility=VISIBILITY_FOLLOWERS)
    assert _ids(_get(world, '/api/v1/timelines/home')) == _sid(hidden)
    assert _ids(_get(world, '/api/v1/timelines/public'), (hidden,)) == []


def test_a_joined_community_is_a_list_with_its_posts(world):
    other = make_community('notjoined')
    inside, _outside = _post(world, minutes=1), _post(world, community=other, minutes=2)
    make_community_member(world['me'], world['community'])
    assert _ids(_get(world, f"/api/v1/timelines/list/{world['community'].id}")) == _sid(inside)
    assert _get(world, f'/api/v1/timelines/list/{other.id}').status_code == 404
    assert _get(world, '/api/v1/timelines/list/99999999999').status_code == 404


def test_an_accounts_statuses_are_its_posts_and_comments(world):
    post = _post(world, author=world['friend'], minutes=1)
    comment = _reply(world, post, world['friend'], minutes=2)
    path = f"/api/v1/accounts/{world['friend'].id}/statuses"
    assert _ids(_get(world, path, token=False)) == _sid(comment, post)
    assert _ids(_get(world, path, exclude_replies='true')) == _sid(post)
    assert _ids(_get(world, path, only_media='true')) == []
    assert _ids(_get(world, path, pinned='true')) == []
    assert _get(world, '/api/v1/accounts/99999999/statuses').status_code == 404
    assert _get(world, '/api/v1/accounts/99999999999/statuses').status_code == 404


def test_a_status_by_its_id(world):
    post = _post(world)
    response = _get(world, f'/api/v1/statuses/{ids.status_id(post)}', token=False)
    assert response.status_code == 200 and response.get_json()['id'] == ids.status_id(post)
    assert 'favourited' in _get(world, f'/api/v1/statuses/{ids.status_id(post)}').get_json()


@pytest.mark.parametrize('what', ['deleted', 'followers-only', 'private-community', 'nonsense'])
def test_a_status_the_viewer_may_not_read_is_not_found(world, what):
    post = _post(world)
    raw = ids.status_id(post)
    if what == 'deleted':
        post.deleted = True
    elif what == 'followers-only':
        post.visibility = VISIBILITY_FOLLOWERS
    elif what == 'private-community':
        post.community.private = True
    else:
        raw = 'not-an-id'
    db.session.commit()
    response = _get(world, f'/api/v1/statuses/{raw}')
    assert response.status_code == 404 and response.get_json() == {'error': 'Record not found'}


def test_a_comment_in_a_followers_only_or_unreadable_spot_is_not_found(world):
    post = _post(world)
    hidden = _reply(world, post, world['stranger'])
    hidden.visibility = VISIBILITY_FOLLOWERS
    open_reply = _reply(world, post, world['stranger'])
    db.session.commit()
    assert _get(world, f'/api/v1/statuses/{ids.status_id(hidden)}').status_code == 404
    assert _get(world, f'/api/v1/statuses/{ids.status_id(open_reply)}').status_code == 200
    post.community.private = True
    db.session.commit()
    assert _get(world, f'/api/v1/statuses/{ids.status_id(open_reply)}').status_code == 404
    post.community.private = False
    post.deleted = True
    db.session.commit()
    assert _get(world, f'/api/v1/statuses/{ids.status_id(open_reply)}').status_code == 404


def test_a_posts_context_is_its_readable_comments_in_thread_order(world):
    post = _post(world)
    first = _reply(world, post, world['friend'], minutes=1)
    second = _reply(world, post, world['stranger'], minutes=2)
    under_first = _reply(world, post, world['stranger'], minutes=3, parent=first)
    hidden = _reply(world, post, world['stranger'], minutes=4, parent=second)
    hidden.visibility = VISIBILITY_FOLLOWERS
    _reply(world, post, world['friend'], minutes=5, parent=hidden)   # hangs from one the viewer cannot see
    db.session.commit()
    context = _get(world, f'/api/v1/statuses/{ids.status_id(post)}/context').get_json()
    assert context['ancestors'] == []
    assert [status['id'] for status in context['descendants']] == _sid(first, under_first, second)


def test_a_comments_context_is_its_chain_up_and_its_replies_down(world):
    post = _post(world)
    top = _reply(world, post, world['friend'], minutes=1)
    middle = _reply(world, post, world['stranger'], minutes=2, parent=top)
    below = _reply(world, post, world['friend'], minutes=3, parent=middle)
    _reply(world, post, world['friend'], minutes=4)   # another branch
    context = _get(world, f'/api/v1/statuses/{ids.status_id(middle)}/context').get_json()
    assert [status['id'] for status in context['ancestors']] == _sid(post, top)
    assert [status['id'] for status in context['descendants']] == _sid(below)


def test_a_comment_under_a_vanished_parent_has_the_post_as_its_only_ancestor(world):
    post = _post(world)
    orphan = _reply(world, post, world['friend'])
    orphan.parent_id = 2 ** 31 - 1
    db.session.commit()
    context = _get(world, f'/api/v1/statuses/{ids.status_id(orphan)}/context').get_json()
    assert [status['id'] for status in context['ancestors']] == _sid(post)
    assert context['descendants'] == []


def test_the_context_of_nothing_is_not_found(world):
    assert _get(world, '/api/v1/statuses/12345/context').status_code == 404


def test_on_a_private_instance_timelines_need_a_persons_token(app, private):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read', None)
    member = make_user(None, 'tlmember', local=True)
    access, _refresh = tokens.issue_token(registered, member, 'read')
    client = app.test_client()
    assert client.get('/api/v1/timelines/public').status_code == 401
    assert client.get('/api/v1/timelines/public', headers={'Authorization': f'Bearer {access}'}).status_code == 200


def test_the_paging_links_and_the_licence_share_one_link_header(world, monkeypatch):
    """OkHttp's header('Link') answers only the last Link header (Mastodon Android, plan W1.0): the RSL licence link
    added after the view joins the timeline's, so the next page is never lost behind it."""
    monkeypatch.setitem(world['client'].application.config, 'ALLOW_AI_CRAWLERS', False)
    _post(world)
    response = _get(world, '/api/v1/timelines/public')
    values = response.headers.getlist('Link')
    assert len(values) == 1
    assert 'rel="next"' in values[0] and 'rel="prev"' in values[0] and 'rel="license"' in values[0]
