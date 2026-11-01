"""A local gallery post's pictures (spec 2026-11-01 G-2, G-5, G-6): stored as an inbound gallery's are.

`set_local_album(post, images)` makes `post.gallery` the given pictures, weight 0.. in order, `gallery_count` in step.
A picture already in the gallery keeps its File (sized, and our upload's file kept on disk); one that leaves it is
deleted with its file; a new one is sized. `edit_post(..., album=...)` reads it for a gallery post: None leaves the
pictures alone, a list of 2 to max_album_images() replaces them and rebuilds the composite thumbnail, and a post that
is no longer a gallery loses them.
"""
import os

import pytest

from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_GALLERY, POST_TYPE_IMAGE, SRC_API
from app.models import File, post_file
from app.shared.post import edit_post, set_local_album
from app.utils import set_setting
from tests.test_shared_post_edit import _api_input, _seed

OURS = 'https://test.piefed.local/static/media/posts/ab/cd'


@pytest.fixture
def sized(monkeypatch):
    """make_image_sizes fetches and resizes: record the File ids it is asked for instead."""
    calls = []
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda file_id, *a, **k: calls.append(file_id))
    return calls


@pytest.fixture
def deleted(monkeypatch):
    """The source_urls whose File.delete_from_disk ran."""
    urls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **k: urls.append(self.source_url))
    return urls


@pytest.fixture
def composite(monkeypatch):
    """build_gallery_thumbnail fetches every picture: a stand-in that records the urls and makes a File."""
    asked = []

    def build(urls, post, return_file_id=True):
        asked.append(list(urls))
        thumbnail = File(source_url=f'{OURS}/composite-{len(asked)}.webp')
        db.session.add(thumbnail)
        db.session.flush()
        return thumbnail.id
    monkeypatch.setattr('app.shared.post.build_gallery_thumbnail', build)
    return asked


def _post(type=POST_TYPE_GALLERY):
    s = _seed()
    s.post.type = type
    s.post.slug = f"/post/{s.post.id}"
    db.session.commit()
    return s.post


def _pictures(post):
    return [(file.source_url, file.alt_text) for file in post.gallery_pictures()]


def _two(prefix='p'):
    return [{'url': f'{OURS}/{prefix}1.png', 'alt_text': 'one'}, {'url': f'{OURS}/{prefix}2.png', 'alt_text': 'two'}]


def test_the_pictures_are_stored_in_order_from_weight_zero(db_session, sized):
    post = _post()

    set_local_album(post, [{'url': f'{OURS}/2.png', 'alt_text': 'two'}, {'url': f'{OURS}/1.png', 'alt_text': 'one'}])
    db.session.commit()

    assert _pictures(post) == [(f'{OURS}/2.png', 'two'), (f'{OURS}/1.png', 'one')]
    assert post.gallery_count == 2
    weights = [row.weight for row in db.session.execute(post_file.select().order_by(post_file.c.weight))]
    assert weights == [0, 1]
    assert len(sized) == 2


def test_an_uploads_file_name_and_hash_are_kept(db_session, sized):
    """A web upload's File names the file it wrote (File.wrote_its_source), so deleting the post removes it."""
    post = _post()

    set_local_album(post, [{'url': f'{OURS}/up.png', 'alt_text': None, 'file_name': 'up.png', 'hash': '1' * 256}])
    db.session.commit()

    file = post.gallery_pictures()[0]
    assert (file.file_name, file.wrote_its_source()) == ('up.png', True)
    assert file.hash is not None


def test_replaced_pictures_keep_the_ones_still_there(db_session, sized, deleted):
    post = _post()
    set_local_album(post, [{'url': f'{OURS}/a.png', 'alt_text': 'a'}, {'url': f'{OURS}/b.png', 'alt_text': 'b'}])
    db.session.commit()
    kept_id = post.gallery_pictures()[1].id
    sized.clear()

    set_local_album(post, [{'url': f'{OURS}/b.png', 'alt_text': 'b, again'}, {'url': f'{OURS}/c.png', 'alt_text': 'c'}])
    db.session.commit()

    assert _pictures(post) == [(f'{OURS}/b.png', 'b, again'), (f'{OURS}/c.png', 'c')]
    assert post.gallery_pictures()[0].id == kept_id   # reordered and re-captioned, not re-made
    assert deleted == [f'{OURS}/a.png']
    assert len(sized) == 1   # only c is new
    assert db.session.query(File).filter_by(source_url=f'{OURS}/a.png').count() == 0


def test_an_empty_list_clears_them(db_session, sized, deleted):
    post = _post()
    set_local_album(post, [{'url': f'{OURS}/a.png', 'alt_text': 'a'}])
    db.session.commit()

    set_local_album(post, [])
    db.session.commit()

    assert (_pictures(post), post.gallery_count, deleted) == ([], 0, [f'{OURS}/a.png'])


# --- through edit_post ---

def test_edit_post_makes_a_gallery_of_the_pictures_it_is_given(db_session, sized, composite):
    post = _post(POST_TYPE_ARTICLE)

    edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, album=_two())

    assert post.type == POST_TYPE_GALLERY
    assert _pictures(post) == [(f'{OURS}/p1.png', 'one'), (f'{OURS}/p2.png', 'two')]
    assert composite == [[f'{OURS}/p1.png', f'{OURS}/p2.png']]
    assert post.image.source_url == f'{OURS}/composite-1.webp'   # the thumbnail, never a gallery row (G-2)
    assert post.url.endswith(post.slug)   # its own page (G-3)


def test_new_pictures_rebuild_the_thumbnail_and_delete_the_old_one(db_session, sized, composite, deleted):
    post = _post(POST_TYPE_ARTICLE)
    edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, album=_two())

    edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, album=_two('q'))

    assert post.image.source_url == f'{OURS}/composite-2.webp'
    assert f'{OURS}/composite-1.webp' in deleted
    assert db.session.query(File).filter_by(source_url=f'{OURS}/composite-1.webp').count() == 0


def test_edit_post_without_pictures_leaves_them_alone(db_session, sized, composite):
    post = _post(POST_TYPE_ARTICLE)
    edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, album=_two())

    edit_post(_api_input(url=None, title='a new title'), post, POST_TYPE_GALLERY, SRC_API, user=post.author)

    assert _pictures(post) == [(f'{OURS}/p1.png', 'one'), (f'{OURS}/p2.png', 'two')]
    assert len(composite) == 1


def test_a_post_that_stops_being_a_gallery_loses_its_pictures(db_session, sized, composite, deleted):
    post = _post(POST_TYPE_ARTICLE)
    edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, album=_two())

    edit_post(_api_input(url=None), post, POST_TYPE_ARTICLE, SRC_API, user=post.author)

    assert (post.gallery_count, db.session.execute(post_file.select()).all()) == (0, [])
    assert post.url is None   # its own page was the gallery's url, not the article's


@pytest.mark.parametrize('count, message', [(1, 'A gallery has 2 to 3 pictures'), (4, 'At most 3 images')])
def test_a_gallery_of_too_few_or_too_many_pictures_is_refused(db_session, sized, composite, count, message):
    set_setting('max_album_images', 3)
    post = _post(POST_TYPE_ARTICLE)

    with pytest.raises(Exception, match=message):
        edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author,
                  album=[{'url': f'{OURS}/{n}.png', 'alt_text': None} for n in range(count)])


def test_pictures_for_a_post_that_is_not_a_gallery_are_refused(db_session, sized):
    post = _post(POST_TYPE_IMAGE)

    with pytest.raises(Exception, match='Only a gallery post has a gallery'):
        edit_post(_api_input(url=None), post, POST_TYPE_ARTICLE, SRC_API, user=post.author, album=_two())


def test_a_new_gallery_needs_its_pictures(db_session, sized):
    post = _post(POST_TYPE_ARTICLE)

    with pytest.raises(Exception, match='A gallery needs its pictures'):
        edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, from_scratch=True)


@pytest.mark.usefixtures('private_static_tree')
def test_the_composite_reads_our_own_pictures_from_disk(db_session, sized, composite):
    """A picture of ours on disk is handed over by its path, so building the thumbnail never has this server fetch
    its own uploads; one it cannot find there goes by its url."""
    os.makedirs('app/static/media/posts/ab/cd', exist_ok=True)
    with open('app/static/media/posts/ab/cd/p1.png', 'wb') as handle:
        handle.write(b'a picture')
    post = _post(POST_TYPE_ARTICLE)

    edit_post(_api_input(url=None), post, POST_TYPE_GALLERY, SRC_API, user=post.author, album=_two())

    assert composite == [[os.path.realpath('app/static/media/posts/ab/cd/p1.png'), f'{OURS}/p2.png']]
