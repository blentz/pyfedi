"""PeerTube's read API, for NewPipe (plan W2.1a; spec 2026-11-09-peertube-read-api-design, rulings V1, V2, D4): a
video is an upload or a remote video file a signed-out reader may read; every community is a channel, every person an
account; each list is paged by start and count with a true total; each shape carries the keys NewPipe requires."""
import uuid
from datetime import datetime, timedelta
from itertools import count

import pytest
from flask import g

from app import db
from app.constants import POST_TYPE_IMAGE, POST_TYPE_VIDEO
from app.models import Language, SiteDomain, Tag
from app.peertube import entities
from app.shared.media import with_media
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import make_community, make_community_member, make_file, make_post, make_post_reply, make_user

_SERIAL = count()
START = datetime(2030, 1, 1, 12, 0, 0)   # newer than anything the shared baseline seeds


@pytest.fixture
def world(app, api_baseline):
    author = make_user(None, 'ptauthor', local=True)
    community = make_community('ptvideos')
    community.show_all = True
    db.session.commit()
    return {'author': author, 'community': community, 'client': app.test_client(), 'flask': app,
            'server': app.config['SERVER_URL']}


def _post(world, minutes=0, community=None, title='A video', **columns):
    post = make_post(community or world['community'], columns.pop('author', world['author']),
                     ap_id=f"{world['server']}/post/pt{next(_SERIAL)}", title=title)
    post.type = POST_TYPE_VIDEO
    post.posted_at = START + timedelta(minutes=minutes)
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _upload(world, minutes=0, status='ready', **columns):
    post = _post(world, minutes, **columns)
    post.url = f"{world['server']}/static/media/posts/ab/cd/abcd{next(_SERIAL):011d}.mp4"
    with_media(post, status=status, uuid=str(uuid.uuid4()), mime='video/mp4', bytes=12345, duration_s=31,
               width=1280, height=720, fps=30, codec='h264')
    db.session.commit()
    return post


def _remote_file(world, minutes=0, url='https://files.example/clip.webm', **columns):
    return _post(world, minutes, url=url, **columns)


def _get(world, path, base_url=None, **query):
    kwargs = {'base_url': base_url} if base_url else {}
    return world['client'].get(path, query_string=query, **kwargs)


def _listed(response, among=None):
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert set(body) >= {'total', 'data'}
    ids = [item['id'] for item in body['data']]
    return ids if among is None else [value for value in ids if value in {row.id for row in among}]


# /api/v1/config

def test_config_names_the_instance_and_the_api_level_served(world):
    body = _get(world, '/api/v1/config').get_json()
    assert body['instance']['name']
    assert body['serverVersion'] == entities.API_LEVEL   # D4: the PeerTube API level, not PieFed's version
    assert body['signup']['allowed'] is False


# /api/v1/videos

def test_uploads_and_remote_files_are_videos_and_nothing_else_is(world):
    upload, remote = _upload(world, 1), _remote_file(world, 2)
    youtube = _post(world, 3, url='https://www.youtube.com/watch?v=abc')
    processing = _upload(world, 4, status='processing')
    picture = _post(world, 5, url='https://files.example/x.png', type=POST_TYPE_IMAGE)
    every = [upload, remote, youtube, processing, picture]
    assert _listed(_get(world, '/api/v1/videos', sort='-publishedAt'), every) == [remote.id, upload.id]


def test_the_total_counts_every_video_and_start_and_count_page(world):
    made = [_upload(world, minutes) for minutes in range(5)]
    first = _get(world, '/api/v1/videos', sort='-publishedAt', start=0, count=2).get_json()
    second = _get(world, '/api/v1/videos', sort='-publishedAt', start=2, count=2).get_json()
    assert first['total'] == second['total'] >= 5
    assert [item['id'] for item in first['data']] == [made[4].id, made[3].id]
    assert [item['id'] for item in second['data']] == [made[2].id, made[1].id]


@pytest.mark.parametrize('start, count_', [('-1', '2'), ('x', '2'), ('0', '0'), ('0', 'x'), ('0', '101')])
def test_a_bad_page_is_a_400(world, start, count_):
    assert _get(world, '/api/v1/videos', start=start, count=count_).status_code == 400


def test_likes_sort_by_score(world):
    low, high = _upload(world, 1, score=1, up_votes=1), _upload(world, 0, score=9, up_votes=9)
    assert _listed(_get(world, '/api/v1/videos', sort='-likes'), [low, high]) == [high.id, low.id]


def test_trending_sorts_by_hot_rank(world):
    cold, hot = _upload(world, 1, ranking=1), _upload(world, 0, ranking=50)
    assert _listed(_get(world, '/api/v1/videos', sort='-trending'), [cold, hot]) == [hot.id, cold.id]


def test_is_local_lists_only_this_servers_posts(world, api_baseline):
    local = _upload(world, 1)
    remote = _remote_file(world, 2, instance_id=api_baseline.instance_remote.id)
    assert _listed(_get(world, '/api/v1/videos', sort='-publishedAt', isLocal='true'), [local, remote]) == [local.id]


def test_a_video_in_a_private_community_is_not_listed(world):
    private = make_community('ptprivate')
    private.show_all = True
    private.private = True
    db.session.commit()
    hidden = _upload(world, community=private)
    assert _listed(_get(world, '/api/v1/videos', sort='-publishedAt'), [hidden]) == []
    assert _get(world, f'/api/v1/videos/{hidden.id}').status_code == 404


def test_on_an_onion_a_remote_file_is_not_a_video(world):
    """V1: a viewer of an onion must reach nothing but the onion."""
    db.session.add(SiteDomain(domain='ptabc.onion', scheme='http', federates=False, is_onion=True))
    db.session.commit()
    clear_domain_cache()
    remote = _remote_file(world)
    assert _get(world, f'/api/v1/videos/{remote.id}', base_url='http://ptabc.onion').status_code == 404
    assert _get(world, f'/api/v1/videos/{remote.id}').status_code == 200


def test_on_another_domain_an_uploads_file_is_served_from_that_domain(world):
    """An upload's URL names its uploader's domain; JSON gets no link rewrite, so a viewer of an onion would be sent
    to the clearnet for the file. The file is fetched from the domain asked, which serves the same media."""
    db.session.add(SiteDomain(domain='ptdef.onion', scheme='http', federates=False, is_onion=True))
    db.session.commit()
    clear_domain_cache()
    upload = _upload(world)
    path = upload.url[len(world['server']):]
    files = _get(world, f'/api/v1/videos/{upload.id}', base_url='http://ptdef.onion').get_json()['files']
    assert files[0]['fileUrl'] == files[0]['fileDownloadUrl'] == 'http://ptdef.onion' + path
    with world['flask'].test_request_context(base_url='http://ptdef.onion'):   # an image of the uploader's domain
        g.storefront = domain_for_host('ptdef.onion')
        assert entities._path(world['server'] + '/static/z.jpg') == '/static/z.jpg'


def test_a_list_item_has_what_newpipe_requires_and_never_a_channel_or_playlist_key(world):
    upload = _upload(world, title='Seeded video')
    item = next(item for item in _get(world, '/api/v1/videos', sort='-publishedAt').get_json()['data']
                if item['id'] == upload.id)
    assert item['name'] == 'Seeded video' and item['uuid'] == entities.video_uuid(upload)
    assert item['account']['name'] == 'ptauthor' and item['account']['host'] and item['account']['displayName']
    assert item['publishedAt'].endswith('Z') and item['duration'] == 31 and item['isLive'] is False
    assert 'followersCount' not in item and 'videosLength' not in item


# /api/v1/videos/{id}

def test_a_video_by_id_or_uuid_has_newpipes_required_fields(world):
    upload = _upload(world)
    for key in (str(upload.id), entities.video_uuid(upload)):
        response = _get(world, f'/api/v1/videos/{key}')
        assert response.status_code == 200, response.get_data(as_text=True)
        video = response.get_json()
        assert video['id'] == upload.id and isinstance(video['nsfw'], bool)
        assert video['category']['label'] and video['licence']['label'] and video['privacy']['id'] == 1
        assert video['channel']['url'] and video['channel']['displayName'] and video['account']['host']
        assert video['streamingPlaylists'] == []


def test_an_uploads_file_is_progressive_with_a_torrent_url_key(world):
    upload = _upload(world)
    files = _get(world, f'/api/v1/videos/{upload.id}').get_json()['files']
    assert files == [{'fileUrl': upload.url, 'fileDownloadUrl': upload.url, 'torrentUrl': '', 'magnetUri': '',
                      'resolution': {'id': 720, 'label': '720p'}, 'size': 12345, 'fps': 30}]


def test_a_remote_files_one_entry_is_its_url(world):
    remote = _remote_file(world)
    files = _get(world, f'/api/v1/videos/{remote.id}').get_json()['files']
    assert [(f['fileUrl'], f['resolution']['label'], f['torrentUrl']) for f in files] == \
        [(remote.url, 'Original', '')]


@pytest.mark.parametrize('key', ['999999999', '00000000-0000-4000-8000-00003b9ac9ff', 'nonsense', '9' * 30])
def test_an_unknown_video_is_a_404_in_peertubes_shape(world, key):
    response = _get(world, f'/api/v1/videos/{key}')
    assert response.status_code == 404 and response.get_json()['status'] == 404


def test_a_long_description_is_cut_in_lists_and_whole_by_its_own_endpoint(world):
    upload = _upload(world, body='x' * 400)
    item = next(item for item in _get(world, '/api/v1/videos', sort='-publishedAt').get_json()['data']
                if item['id'] == upload.id)
    assert len(item['description']) == 250 and item['description'].endswith('...')
    assert _get(world, f'/api/v1/videos/{upload.id}').get_json()['description'] == 'x' * 400
    assert _get(world, f'/api/v1/videos/{upload.id}/description').get_json() == {'description': 'x' * 400}


def test_the_empty_extras_answer_peertubes_empty_shapes(world):
    upload = _upload(world)
    assert _get(world, f'/api/v1/videos/{upload.id}/captions').get_json() == {'total': 0, 'data': []}
    assert _get(world, f'/api/v1/videos/{upload.id}/chapters').get_json() == {'chapters': []}
    assert _get(world, f'/api/v1/videos/{upload.id}/storyboards').get_json() == {'storyboards': []}
    assert _get(world, '/api/v1/video-playlists/1').status_code == 404
    assert _get(world, '/api/v1/videos/999999999/captions').status_code == 404


def test_a_tag_is_listed_with_the_video(world):
    upload = _upload(world)
    upload.tags.append(Tag(name='cats', display_as='cats'))
    db.session.commit()
    assert _get(world, f'/api/v1/videos/{upload.id}').get_json()['tags'] == ['cats']


# comments

def test_comment_threads_are_the_top_level_replies_with_their_counts(world):
    upload = _upload(world)
    other = make_user(None, 'ptcommenter', local=True)
    top = make_post_reply(upload, other, body='first')
    child = make_post_reply(upload, world['author'], body='answer')
    child.parent_id = top.id
    db.session.commit()
    body = _get(world, f'/api/v1/videos/{upload.id}/comment-threads').get_json()
    assert body['total'] == 1
    thread = body['data'][0]
    assert thread['id'] == thread['threadId'] == top.id and thread['totalReplies'] == 1
    assert thread['totalRepliesFromVideoAuthor'] == 1 and thread['text'] and thread['createdAt'].endswith('Z')
    assert thread['account']['name'] == 'ptcommenter' and thread['isDeleted'] is False
    tree = _get(world, f'/api/v1/videos/{upload.id}/comment-threads/{top.id}').get_json()
    assert tree['comment']['id'] == top.id
    assert [entry['comment']['id'] for entry in tree['children']] == [child.id]
    assert tree['children'][0]['children'] == []
    assert _get(world, f'/api/v1/videos/{upload.id}/comment-threads/{child.id}').status_code == 404


def test_a_deleted_reply_is_left_out(world):
    upload = _upload(world)
    gone = make_post_reply(upload, world['author'])
    gone.deleted = True
    db.session.commit()
    body = _get(world, f'/api/v1/videos/{upload.id}/comment-threads').get_json()
    assert (body['total'], body['data']) == (0, [])


# accounts and channels

def test_a_person_is_an_account_listing_their_videos(world):
    upload = _upload(world)
    account = _get(world, '/api/v1/accounts/ptauthor').get_json()
    assert account['name'] == 'ptauthor' and account['displayName'] and account['host']
    assert _listed(_get(world, '/api/v1/accounts/ptauthor/videos'), [upload]) == [upload.id]
    host = account['host']
    assert _get(world, f'/api/v1/accounts/ptauthor@{host}').get_json()['id'] == world['author'].id


def test_a_numeric_account_is_still_mastodons(world):
    assert 'acct' in _get(world, f"/api/v1/accounts/{world['author'].id}").get_json()


def test_an_account_lists_the_communities_it_moderates_as_channels(world):
    make_community_member(world['author'], world['community'], is_moderator=True)
    body = _get(world, '/api/v1/accounts/ptauthor/video-channels').get_json()
    assert [item['name'] for item in body['data']] == ['ptvideos'] and body['total'] == 1
    assert _get(world, '/api/v1/accounts/ptauthor/video-playlists').get_json() == {'total': 0, 'data': []}


def test_a_community_is_a_channel_listing_its_videos(world):
    upload = _upload(world)
    make_community_member(world['author'], world['community'], is_moderator=True)
    channel = _get(world, '/api/v1/video-channels/ptvideos').get_json()
    assert channel['name'] == 'ptvideos' and channel['displayName'] and 'followersCount' in channel
    assert channel['ownerAccount']['name'] == 'ptauthor' and channel['ownerAccount']['url']
    assert 'description' in channel
    assert _listed(_get(world, '/api/v1/video-channels/ptvideos/videos'), [upload]) == [upload.id]


@pytest.mark.parametrize('path', ['/api/v1/accounts/nobody', '/api/v1/video-channels/nothing',
                                  '/api/v1/accounts/nobody/videos', '/api/v1/video-channels/nothing/videos'])
def test_an_unknown_actor_is_a_404(world, path):
    assert _get(world, path).status_code == 404


def test_a_banned_person_is_not_an_account(world):
    world['author'].banned = True
    db.session.commit()
    assert _get(world, '/api/v1/accounts/ptauthor').status_code == 404


# search

def test_search_finds_videos_by_title_and_by_tag(world):
    found = _upload(world, title='Kittens playing')
    _upload(world, 1, title='Something else')
    assert _listed(_get(world, '/api/v1/search/videos', search='kittens')) == [found.id]
    found.tags.append(Tag(name='kittens', display_as='kittens'))
    db.session.commit()
    assert found.id in _listed(_get(world, '/api/v1/search/videos', tagsOneOf='kittens'))


def test_search_finds_channels_and_no_playlists(world):
    body = _get(world, '/api/v1/search/video-channels', search='ptvideos').get_json()
    assert [item['name'] for item in body['data']] == ['ptvideos'] and 'followersCount' in body['data'][0]
    assert _get(world, '/api/v1/search/video-playlists', search='x').get_json() == {'total': 0, 'data': []}


# web links NewPipe shares

def test_a_shared_video_link_goes_to_the_post(world):
    upload = _upload(world)
    for path in (f'/videos/watch/{entities.video_uuid(upload)}', f'/w/{upload.id}'):
        response = _get(world, path)
        assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{upload.id}')
    assert _get(world, '/w/nonsense').status_code == 404


# the edges of each shape

def test_an_image_of_this_domain_is_a_path_and_one_elsewhere_is_left_out(world):
    local, remote = _upload(world), _upload(world, 1)
    local.image = make_file(file_path='app/static/media/posts/ab/cd/thumb.jpg')
    local.image.thumbnail_path = 'app/static/media/posts/ab/cd/thumb_small.jpg'
    remote.image = make_file(source_url='https://images.example/x.jpg')
    world['author'].avatar = make_file(file_path='app/static/media/users/ab/cd/me.png')
    db.session.commit()
    mine = _get(world, f'/api/v1/videos/{local.id}').get_json()
    assert mine['thumbnailPath'] == '/static/media/posts/ab/cd/thumb_small.jpg'
    assert mine['previewPath'] == '/static/media/posts/ab/cd/thumb.jpg'
    [avatar] = mine['account']['avatars']
    assert avatar['path'] == '/static/media/users/ab/cd/me.png' and avatar['width'] == 120
    assert avatar['createdAt'] and avatar['updatedAt']   # PeerTube's own app requires both of every image
    elsewhere = _get(world, f'/api/v1/videos/{remote.id}').get_json()
    assert 'thumbnailPath' not in elsewhere and 'previewPath' not in elsewhere


@pytest.mark.parametrize('url, path', [(None, None), ('', None), ('//evil.example/x', None),
                                       ('https://elsewhere.example/x', None), ('/static/x', '/static/x')])
def test_a_path_is_only_ever_one_of_this_domain(world, url, path):
    with world['flask'].test_request_context():
        assert entities._path(url) == path
        assert entities._path(entities._base() + '/static/y') == '/static/y'


def test_a_known_language_is_named(world):
    upload = _upload(world)
    language = Language(code='fr', name='Français')
    db.session.add(language)
    db.session.commit()
    upload.language_id = language.id
    db.session.commit()
    assert _get(world, f'/api/v1/videos/{upload.id}').get_json()['language'] == {'id': 'fr', 'label': 'Français'}


def test_an_upload_without_a_height_or_a_size_is_still_one_file(world):
    upload = _upload(world)
    with_media(upload, height='tall', bytes=None, fps=None)
    db.session.commit()
    [entry] = _get(world, f'/api/v1/videos/{upload.id}').get_json()['files']
    assert entry['resolution'] == {'id': 0, 'label': 'Original'} and entry['size'] == 0 and 'fps' not in entry


def test_a_banned_tag_is_not_listed(world):
    upload = _upload(world)
    upload.tags.append(Tag(name='hidden', display_as='hidden', banned=True))
    db.session.commit()
    assert _get(world, f'/api/v1/videos/{upload.id}').get_json()['tags'] == []


def test_a_failed_upload_with_a_video_url_is_not_a_remote_file(world):
    failed = _upload(world, status='failed')
    assert _get(world, f'/api/v1/videos/{failed.id}').status_code == 404


@pytest.mark.parametrize('key', ['0', '9999999999'])
def test_an_id_no_row_can_hold_is_a_404(world, key):
    assert _get(world, f'/api/v1/videos/{key}').status_code == 404


def _without_creator(world):
    world['community'].user_id = None
    db.session.commit()


def test_a_channel_with_no_moderator_is_owned_by_its_creator_or_by_itself(world):
    _without_creator(world)
    channel = _get(world, '/api/v1/video-channels/ptvideos').get_json()
    assert channel['ownerAccount']['name'] == 'ptvideos'
    world['community'].user_id = world['author'].id
    db.session.commit()
    assert _get(world, '/api/v1/video-channels/ptvideos').get_json()['ownerAccount']['name'] == 'ptauthor'
    world['author'].deleted = True
    db.session.commit()
    assert _get(world, '/api/v1/video-channels/ptvideos').get_json()['ownerAccount']['name'] == 'ptvideos'


def test_a_deleted_moderator_does_not_own_the_channel(world):
    _without_creator(world)
    make_community_member(world['author'], world['community'], is_moderator=True)
    world['author'].deleted = True
    db.session.commit()
    assert _get(world, '/api/v1/video-channels/ptvideos').get_json()['ownerAccount']['name'] == 'ptvideos'


@pytest.mark.parametrize('path', ['/api/v1/accounts/nobody/video-channels', '/api/v1/accounts/nobody/video-playlists',
                                  '/api/v1/video-channels/nothing/video-playlists', '/api/v1/accounts/' + 'x' * 300,
                                  '/api/v1/video-channels/' + 'x' * 300])
def test_an_unknown_or_impossible_actors_tabs_are_404s(world, path):
    assert _get(world, path).status_code == 404


def test_a_known_channel_has_no_playlists_and_the_playlist_list_is_empty(world):
    assert _get(world, '/api/v1/video-channels/ptvideos/video-playlists').get_json() == {'total': 0, 'data': []}
    assert _get(world, '/api/v1/video-playlists').get_json() == {'total': 0, 'data': []}


def test_a_private_community_is_not_a_channel(world):
    world['community'].private = True
    db.session.commit()
    assert _get(world, '/api/v1/video-channels/ptvideos').status_code == 404


def test_an_empty_channel_search_finds_nothing(world):
    assert _get(world, '/api/v1/search/video-channels').get_json() == {'total': 0, 'data': []}


def test_tags_may_be_sent_as_an_array_or_comma_separated(world):
    found = _upload(world)
    found.tags.append(Tag(name='dogs', display_as='dogs'))
    db.session.commit()
    assert found.id in _listed(_get(world, '/api/v1/search/videos', **{'tagsOneOf[]': 'cats, dogs'}))
    assert found.id not in _listed(_get(world, '/api/v1/search/videos', tagsOneOf='cats'))


def test_a_comment_thread_of_an_unknown_video_is_a_404(world):
    assert _get(world, '/api/v1/videos/999999999/comment-threads').status_code == 404
    assert _get(world, '/api/v1/videos/999999999/comment-threads/1').status_code == 404
    assert _get(world, '/api/v1/videos/999999999/description').status_code == 404
