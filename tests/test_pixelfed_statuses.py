"""Pixelfed's extended status (`_pe=1`) for its own app (plan W1.4a; spec 2026-11-10-pixelfed-app-design §2, §3): the
profile grid shows only photos, albums and videos by `pf_type`, posts show `reply_count`, and `liked_by` names nobody,
since who voted is shown to moderators alone on PieFed. Without `_pe=1` a status is Mastodon's, unchanged."""
import pytest

from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE
from app.mastodon.statuses import Page, status
from app.models import post_file
from tests.factories import make_file, make_post_reply
from tests import test_mastodon_statuses as status_tests

world = status_tests.world   # the fixture
_post = status_tests._post


def _extended(app, row, query='?_pe=1'):
    with app.test_request_context(f'/api/v1/timelines/home{query}'):
        return status(row, Page(None, [row]))


def _picture(name):
    picture = make_file(file_path=f'app/static/media/posts/aa/{name}.webp')
    picture.width, picture.height = 800, 400
    db.session.commit()
    return picture


def test_a_photo_is_a_photo(app, world):
    entity = _extended(app, _post(world, type=POST_TYPE_IMAGE, image_id=_picture('one').id, reply_count=3))
    assert (entity['pf_type'], entity['reply_count'], entity['liked_by'], entity['local']) == ('photo', 3, None, True)


def test_a_gallery_is_an_album(app, world):
    post = _post(world, type=POST_TYPE_GALLERY, gallery_count=2)
    db.session.execute(post_file.insert(), [{'post_id': post.id, 'file_id': _picture('a').id, 'weight': 1},
                                            {'post_id': post.id, 'file_id': _picture('b').id, 'weight': 2}])
    db.session.commit()
    assert _extended(app, post)['pf_type'] == 'photo:album'


def test_a_video_is_a_video(app, world, monkeypatch):
    monkeypatch.setattr('app.mastodon.statuses._attachments',
                        lambda row: [{'type': 'video', 'url': 'https://x/v.mp4'}])
    assert _extended(app, _post(world))['pf_type'] == 'video'


def test_text_and_comments_are_text(app, world):
    post = _post(world)
    assert _extended(app, post)['pf_type'] == 'text'
    assert _extended(app, make_post_reply(post, world['author']))['pf_type'] == 'text'


@pytest.mark.parametrize('query', ['', '?_pe=0', '?limit=20'])
def test_without_pe_a_status_is_mastodons(app, world, query):
    entity = _extended(app, _post(world), query)
    assert not {'pf_type', 'liked_by', 'reply_count'} & set(entity)


def test_outside_a_request_a_status_is_mastodons(world):
    post = _post(world)
    assert 'pf_type' not in status(post, Page(None, [post]))
