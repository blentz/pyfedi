"""Pixelfed's stories for its official app (plan W1.4c; spec 2026-11-10-pixelfed-app-design §7, ruling X3 and rulings
S1-S4 of 2026-11-11): a story is a microblog post with a picture from the last 24 hours, by the viewer or someone they
follow. Seen is recorded but nobody is shown who looked; expiring one's own story drops it from the carousel and
keeps the post; a comment is a chat to the author. PixelDroid (W1.4d, ruling S5) reads Pixelfed's self-carousel and
publishes a story, which posts its photo to the author's microblog."""
import io
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from PIL import Image

from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_VIDEO
from app.models import ChatMessage, Conversation, Post, StoryExpiry, StoryView, post_file
from app.oauth_server import tokens
from app.utils import utcnow
from tests.factories import make_community, make_file, make_follow, make_post, make_user, make_user_block

CAROUSEL, SEEN, EXPIRE = '/api/v1.2/stories/carousel', '/api/v1.1/stories/seen', '/api/v1.1/stories/self-expire'
VIEWERS, COMMENT = '/api/v1.2/stories/viewers', '/api/v1.1/stories/comment'
SELF_CAROUSEL, ADD, PUBLISH = '/api/pixelfed/v1/stories/self-carousel', '/api/v1.1/stories/add', \
    '/api/v1.1/stories/publish'


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Pixelfed', ['https://app.example/cb'], 'read write', None)
    me, friend, stranger = (make_user(None, name, local=True, with_keys=True)
                            for name in ('stme', 'stfriend', 'ststranger'))
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


def test_the_v1_2_publish_path_is_not_pixelfeds(world):
    assert world['client'].post('/api/v1.2/stories/publish', headers=world['headers']['stme']).status_code == 404


def _self_carousel(world, who='me'):
    response = _get(world, SELF_CAROUSEL, who)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_pixeldroids_carousel_has_ones_own_stories_apart(world):
    """PixelDroid's carousel (ruling S5): Pixelfed's self-carousel, one's own stories under `self` and each followed
    author's under `nodes`, durations in seconds."""
    theirs, mine = _story(world), _story(world, who='me', hours=2)
    body = _self_carousel(world)
    me, friend = world['me'], world['friend']
    assert body['self']['user'] == {'id': str(me.id), 'username': 'stme', 'avatar': body['self']['user']['avatar'],
                                    'local': True, 'is_author': True}
    assert body['self']['user']['avatar']
    [own] = body['self']['nodes']
    assert (own['id'], own['type'], own['duration'], own['seen']) == (str(mine.id), 'photo', 10, True)
    assert own['src'].endswith(mine.image.file_path.removeprefix('app')) and own['created_at']
    [node] = body['nodes']
    assert node['id'] == f'pfs:{friend.id}' and node['seen'] is False and node['url']
    assert node['user'] == {'id': str(friend.id), 'username': 'stfriend', 'username_acct': 'stfriend',
                            'avatar': node['user']['avatar'], 'local': True, 'is_author': False}
    [story] = node['nodes']
    assert (story['id'], story['pid'], story['type']) == (str(theirs.id), str(friend.id), 'photo')
    assert story['duration'] == 10
    assert story['seen'] is False and story['src'] and story['created_at']


def test_pixeldroids_carousel_lists_unseen_authors_first(world):
    world['second'] = make_user(None, 'stsecond', local=True)
    make_follow(world['me'], world['second'])
    seen, unseen = _story(world, hours=1), _story(world, who='second', hours=3)
    assert world['client'].post(SEEN, headers=world['headers']['stme'], query_string={'id': seen.id}).status_code == 200
    nodes = _self_carousel(world)['nodes']
    assert [node['id'] for node in nodes] == [f"pfs:{world['second'].id}", f"pfs:{world['friend'].id}"]
    assert [node['seen'] for node in nodes] == [False, True] and unseen.user_id == world['second'].id


def test_pixeldroids_carousel_with_no_stories(world):
    body = _self_carousel(world)
    assert body['self']['nodes'] == [] and body['nodes'] == []


def test_seen_and_comment_take_their_fields_from_the_query_as_pixeldroid_sends_them(world):
    story = _story(world)
    headers = world['headers']['stme']
    assert world['client'].post(SEEN, headers=headers, query_string={'id': story.id}).status_code == 200
    assert db.session.get(StoryView, (world['me'].id, story.id)) is not None
    response = world['client'].post(COMMENT, headers=headers, query_string={'sid': story.id, 'caption': 'Nice'})
    assert response.status_code == 200, response.get_json()
    assert ChatMessage.query.one().body.startswith('Nice')


def _add(world, who='me', content=None, name='story.png'):
    return world['client'].post(ADD, headers=world['headers'][world[who].user_name],
                                content_type='multipart/form-data', data={'file': (content or _png(), name)})


def _png() -> io.BytesIO:
    data = io.BytesIO()
    Image.new('RGB', (30, 40), 'teal').save(data, format='PNG')
    data.seek(0)
    return data


def _mine(world) -> list[Post]:
    return Post.query.filter(Post.user_id == world['me'].id).all()


def _our_picture_is_an_image(http_mock):
    http_mock.head(url__startswith='https://test.piefed.local/static/media/').respond(
        200, headers={'Content-Type': 'image/png'})


@pytest.mark.usefixtures('private_static_tree')
def test_publishing_a_story_posts_its_photo_to_ones_microblog(world, http_mock):
    """Ruling S5: PixelDroid's Story uploads the photo (stories/add, the upload Mastodon's media takes) and publishes
    it, which posts it to the author's microblog with no words: a story for a day, as any photo posted there."""
    _our_picture_is_an_image(http_mock)
    added = _add(world)
    assert added.status_code == 200, added.get_json()
    body = added.get_json()
    assert (body['code'], body['msg'], body['media_type']) == (200, 'Successfully added', 'photo')
    assert body['media_url'].startswith('http') and body['media_id'].isdigit()
    assert _mine(world) == []   # added, not yet published
    published = world['client'].post(PUBLISH, headers=world['headers']['stme'], query_string={
        'media_id': body['media_id'], 'duration': 10, 'can_reply': '1', 'can_react': '0'})
    assert published.status_code == 200, published.get_json()
    assert published.get_json() == {'code': 200, 'msg': 'Successfully published'}
    [post] = _mine(world)
    assert (post.user_id, post.community_id, post.type) == (world['me'].id, world['microblogs'].id, POST_TYPE_IMAGE)
    assert _self_carousel(world)['self']['nodes'][0]['id'] == str(post.id)
    assert _nodes(world, 'friend') == [] and _nodes(world)[0]['stories'][0]['id'] == str(post.id)


@pytest.mark.usefixtures('private_static_tree')
@pytest.mark.parametrize('case', ['not an image', 'no file'])
def test_a_story_upload_that_is_no_photo_is_refused(world, case):
    if case == 'no file':
        response = world['client'].post(ADD, headers=world['headers']['stme'], data={})
    else:
        response = _add(world, content=io.BytesIO(b'not an image'), name='clip.mp4')
    assert response.status_code == 422, response.get_json()


@pytest.mark.usefixtures('private_static_tree')
@pytest.mark.parametrize('case', ['someone elses', 'nothing', 'missing'])
def test_publishing_what_is_not_ones_own_upload_is_refused(world, case):
    media_id = {'someone elses': lambda: _add(world, who='friend').get_json()['media_id'], 'nothing': lambda: '999999',
                'missing': lambda: None}[case]()
    query = {'duration': 10, 'can_reply': '1', 'can_react': '1', **({'media_id': media_id} if media_id else {})}
    response = world['client'].post(PUBLISH, headers=world['headers']['stme'], query_string=query)
    assert response.status_code == 422, response.get_json()
    assert _mine(world) == []


@pytest.mark.parametrize('method,path', [('get', CAROUSEL), ('post', SEEN), ('post', f'{EXPIRE}/1'),
                                         ('get', VIEWERS), ('post', COMMENT), ('get', SELF_CAROUSEL), ('post', ADD),
                                         ('post', PUBLISH)])
def test_stories_need_a_token(world, method, path):
    assert getattr(world['client'], method)(path).status_code == 401
