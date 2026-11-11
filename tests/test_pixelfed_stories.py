"""Pixelfed's stories for its official app (plan W1.4c; spec 2026-11-10-pixelfed-app-design §7, ruling X3 and rulings
S1-S4 of 2026-11-11): a story is a microblog post with a picture from the last 24 hours, by the viewer or someone they
follow. Seen is recorded but nobody is shown who looked; expiring one's own story drops it from the carousel and
keeps the post; a comment is a chat to the author; publishing is not built."""
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_VIDEO
from app.models import ChatMessage, Conversation, Post, StoryExpiry, StoryView, post_file
from app.oauth_server import tokens
from app.utils import utcnow
from tests.factories import make_community, make_file, make_follow, make_post, make_user, make_user_block

CAROUSEL, SEEN, EXPIRE = '/api/v1.2/stories/carousel', '/api/v1.1/stories/seen', '/api/v1.1/stories/self-expire'
VIEWERS, COMMENT = '/api/v1.2/stories/viewers', '/api/v1.1/stories/comment'


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Pixelfed', ['https://app.example/cb'], 'read write', None)
    me, friend, stranger = (make_user(None, name, local=True) for name in ('stme', 'stfriend', 'ststranger'))
    for person in (me, friend, stranger):
        person.verified, person.created = True, person.created.replace(year=2020)
    make_follow(me, friend)
    microblogs, other = make_community('microblogs', kind='microblogs'), make_community('stphotos')
    db.session.commit()
    headers = {}
    for person in (me, friend, stranger):
        access, _refresh = tokens.issue_token(registered, person, 'read write')
        headers[person.user_name] = {'Authorization': f'Bearer {access}'}
    with patch('app.chat.util.publish_sse_event'):
        yield {'me': me, 'friend': friend, 'stranger': stranger, 'microblogs': microblogs, 'other': other,
               'client': app.test_client(), 'headers': headers}


def _get(world, path, who='me', **query):
    return world['client'].get(path, headers=world['headers'][world[who].user_name], query_string=query)


def _post(world, path, who='me', **body):
    return world['client'].post(path, headers=world['headers'][world[who].user_name], json=body)


def _story(world, who='friend', hours=1, kind=POST_TYPE_IMAGE, community='microblogs', microblog=True):
    picture = make_file(file_path=f'app/static/media/posts/st/{uuid.uuid4().hex[:8]}.webp')
    post = make_post(world[community], world[who], ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex}',
                     microblog=microblog)
    post.type, post.posted_at = kind, utcnow() - timedelta(hours=hours)
    if kind == POST_TYPE_GALLERY:
        db.session.execute(post_file.insert().values(post_id=post.id, file_id=picture.id, weight=0))
        post.gallery_count = 1
    else:
        post.image_id = picture.id
    db.session.commit()
    return post


def _nodes(world, who='me'):
    response = _get(world, CAROUSEL, who)
    assert response.status_code == 200, response.get_json()
    return response.get_json()['nodes']


def test_the_carousel_lists_followed_and_own_stories_own_first(world):
    theirs, mine = _story(world), _story(world, who='me', hours=2)
    nodes = _nodes(world)
    assert [node['id'] for node in nodes] == [f"self:{world['me'].id}", f"pfs:{world['friend'].id}"]
    own, friend = nodes
    assert (own['is_author'], friend['is_author']) == (True, False)
    assert (friend['username'], friend['profile_id']) == ('stfriend', str(world['friend'].id))
    assert friend['username_acct'] == 'stfriend' and friend['avatar'] and friend['url']
    story = friend['stories'][0]
    assert (story['id'], story['type'], story['overlays'], story['duration']) == (str(theirs.id), 'photo', [], 10000)
    assert story['url'].endswith(theirs.image.file_path.removeprefix('app'))
    assert story['viewed'] is False and friend['hasViewed'] is False and 'view_count' not in story
    assert own['stories'][0]['id'] == str(mine.id) and own['stories'][0]['view_count'] == 0
    assert own['stories'][0]['viewed'] is True


def test_a_gallery_is_a_story_of_its_first_picture(world):
    gallery = _story(world, kind=POST_TYPE_GALLERY)
    story = _nodes(world)[0]['stories'][0]
    assert story['id'] == str(gallery.id) and story['url'].endswith('.webp')


@pytest.mark.parametrize('case', ['old', 'video', 'stranger', 'community post', 'deleted', 'no picture'])
def test_what_is_not_a_story(world, case):
    if case == 'old':
        _story(world, hours=25)
    elif case == 'video':
        _story(world, kind=POST_TYPE_VIDEO)
    elif case == 'stranger':
        _story(world, who='stranger')
    elif case == 'community post':
        _story(world, community='other', microblog=False)
    elif case == 'no picture':
        _story(world).image_id = None
        db.session.commit()
    else:
        _story(world).deleted = True
        db.session.commit()
    assert _nodes(world) == []


def test_a_followed_persons_story_hidden_by_a_block_is_not_listed(world):
    _story(world)
    make_user_block(world['me'], world['friend'])
    assert _nodes(world) == []


def test_seen_greys_out_a_story_for_its_viewer_only(world):
    story = _story(world)
    response = _post(world, SEEN, id=str(story.id))
    assert (response.status_code, response.get_json()) == (200, {'code': 200})
    assert _post(world, SEEN, id=story.id).status_code == 200   # again: one row
    friend = _nodes(world)[0]
    assert friend['stories'][0]['viewed'] is True and friend['hasViewed'] is True
    assert StoryView.query.filter_by(user_id=world['me'].id, post_id=story.id).count() == 1


def test_seen_of_ones_own_story_records_nothing(world):
    mine = _story(world, who='me')
    assert _post(world, SEEN, id=mine.id).get_json() == []
    assert StoryView.query.count() == 0


@pytest.mark.parametrize('case', ['stranger', 'old', 'missing', 'garbage'])
def test_seen_of_what_is_not_the_viewers_story_is_not_found(world, case):
    if case == 'stranger':
        named = _story(world, who='stranger').id
    elif case == 'old':
        named = _story(world, hours=30).id
    elif case == 'missing':
        named = 2 ** 31 - 1
    else:
        named = 'nonsense'
    assert _post(world, SEEN, id=named).status_code == 404
    assert StoryView.query.count() == 0


def test_seen_prunes_the_viewers_rows_older_than_two_days(world):
    old_post, story = _story(world, hours=60), _story(world)
    db.session.add(StoryView(user_id=world['me'].id, post_id=old_post.id, created_at=utcnow() - timedelta(days=3)))
    db.session.commit()
    _post(world, SEEN, id=story.id)
    assert [row.post_id for row in StoryView.query.all()] == [story.id]


def test_viewers_names_nobody_and_counts_nothing(world):
    mine = _story(world, who='me')
    make_follow(world['friend'], world['me'])
    _post(world, SEEN, 'friend', id=mine.id)
    assert StoryView.query.count() == 1
    listed = _get(world, VIEWERS, sid=str(mine.id))
    assert (listed.status_code, listed.get_json()['data']) == (200, [])
    assert listed.get_json()['links'] == {'next': None, 'prev': None}
    assert _nodes(world)[0]['stories'][0]['view_count'] == 0


@pytest.mark.parametrize('case', ['someone elses', 'missing', 'garbage'])
def test_viewers_of_what_is_not_ones_own_story_is_not_found(world, case):
    named = {'someone elses': lambda: _story(world).id, 'missing': lambda: 2 ** 31 - 1,
             'garbage': lambda: 'x'}[case]()
    assert _get(world, VIEWERS, sid=named).status_code == 404


def test_self_expire_drops_ones_story_from_every_carousel_and_keeps_the_post(world):
    mine = _story(world, who='me')
    make_follow(world['friend'], world['me'])
    response = _post(world, f'{EXPIRE}/{mine.id}')
    assert (response.status_code, response.get_json()) == (200, {'code': 200, 'msg': 'Successfully deleted'})
    assert _nodes(world) == [] and _nodes(world, 'friend') == []
    kept = db.session.get(Post, mine.id)
    assert kept is not None and kept.deleted is False


def test_self_expire_of_someone_elses_story_is_not_found(world):
    theirs = _story(world)
    assert _post(world, f'{EXPIRE}/{theirs.id}').status_code == 404
    assert StoryExpiry.query.count() == 0 and len(_nodes(world)) == 1


def test_self_expire_prunes_rows_older_than_two_days(world):
    old_post, mine = _story(world, who='me', hours=60), _story(world, who='me')
    db.session.add(StoryExpiry(post_id=old_post.id, created_at=utcnow() - timedelta(days=3)))
    db.session.commit()
    _post(world, f'{EXPIRE}/{mine.id}')
    assert [row.post_id for row in StoryExpiry.query.all()] == [mine.id]


def test_deleting_the_post_deletes_its_story_rows(world):
    story = _story(world)
    _post(world, SEEN, id=story.id)
    db.session.add(StoryExpiry(post_id=story.id))
    db.session.commit()
    db.session.execute(Post.__table__.delete().where(Post.id == story.id))
    db.session.commit()
    assert (StoryView.query.count(), StoryExpiry.query.count()) == (0, 0)


def test_a_comment_is_a_chat_to_the_author_linking_the_post(world):
    story = _story(world)
    response = _post(world, COMMENT, sid=str(story.id), caption='lovely light')
    assert (response.status_code, response.get_json()) == (200, {'code': 200, 'msg': 'Sent!'})
    message = ChatMessage.query.one()
    assert (message.sender_id, message.recipient_id) == (world['me'].id, world['friend'].id)
    assert 'lovely light' in message.body and story.public_url() in message.body
    assert _post(world, COMMENT, sid=story.id, caption='again').status_code == 200
    assert Conversation.query.count() == 1 and ChatMessage.query.count() == 2


def test_a_comment_to_an_author_who_blocked_the_commenter_is_refused(world):
    story = _story(world)
    make_user_block(world['friend'], world['me'])
    assert _post(world, COMMENT, sid=story.id, caption='hi').status_code == 403
    assert ChatMessage.query.count() == 0


@pytest.mark.parametrize('case', ['own', 'stranger', 'missing', 'no caption', 'blank caption', 'long caption',
                                  'not json'])
def test_what_cannot_be_commented(world, case):
    story = _story(world, who='me' if case == 'own' else 'stranger' if case == 'stranger' else 'friend')
    body = {'sid': str(story.id), 'caption': 'hello'}
    if case == 'missing':
        body['sid'] = str(2 ** 31 - 1)
    elif case == 'no caption':
        del body['caption']
    elif case == 'blank caption':
        body['caption'] = '   '
    elif case == 'long caption':
        body['caption'] = 'x' * 5001
    if case == 'not json':
        response = world['client'].post(COMMENT, headers=world['headers']['stme'], data='sid=1')
    else:
        response = _post(world, COMMENT, **body)
    assert response.status_code in (404, 422), response.get_json()
    assert ChatMessage.query.count() == 0


@pytest.mark.parametrize('path', ['/api/v1.1/stories/add', '/api/v1.1/stories/publish', '/api/v1.2/stories/publish'])
def test_publishing_a_story_is_not_built(world, path):
    assert world['client'].post(path, headers=world['headers']['stme'], data={}).status_code == 404


@pytest.mark.parametrize('method,path', [('get', CAROUSEL), ('post', SEEN), ('post', f'{EXPIRE}/1'),
                                         ('get', VIEWERS), ('post', COMMENT)])
def test_stories_need_a_token(world, method, path):
    assert getattr(world['client'], method)(path).status_code == 401
