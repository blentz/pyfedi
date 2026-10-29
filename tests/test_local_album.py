"""A local post's album: the pictures after its image, stored as an inbound album is (Pixelfed interop R5).

`set_local_album(post, images)` makes `post.gallery` the given pictures, weight 1.. in order, `gallery_count` in step.
A picture already in the album keeps its File (sized, and our upload's file kept on disk); one that leaves it is
deleted with its file; a new one is sized. `edit_post(..., album=...)` reads it: None leaves the album alone, a list
replaces it, and a post that is no longer an image post loses it.
"""
import pytest

from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_IMAGE, SRC_API
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


def _image_post():
    s = _seed()
    s.post.type = POST_TYPE_IMAGE
    s.post.image = File(source_url=f'{OURS}/0.png', alt_text='zero')
    db.session.commit()
    return s.post


def _album(post):
    return [(file.source_url, file.alt_text) for file in post.album_images()[1:]]


def test_an_album_is_stored_after_the_image_in_order(db_session, sized):
    post = _image_post()

    set_local_album(post, [{'url': f'{OURS}/2.png', 'alt_text': 'two'}, {'url': f'{OURS}/1.png', 'alt_text': 'one'}])
    db.session.commit()

    assert _album(post) == [(f'{OURS}/2.png', 'two'), (f'{OURS}/1.png', 'one')]
    assert post.gallery_count == 2
    weights = [row.weight for row in db.session.execute(post_file.select().order_by(post_file.c.weight))]
    assert weights == [1, 2]
    assert len(sized) == 2


def test_an_uploads_file_name_and_hash_are_kept(db_session, sized):
    """A web upload's File names the file it wrote (File.wrote_its_source), so deleting the post removes it."""
    post = _image_post()

    set_local_album(post, [{'url': f'{OURS}/up.png', 'alt_text': None, 'file_name': 'up.png', 'hash': '1' * 256}])
    db.session.commit()

    file = post.album_images()[1]
    assert (file.file_name, file.wrote_its_source()) == ('up.png', True)
    assert file.hash is not None


def test_a_replaced_album_keeps_the_pictures_it_still_has(db_session, sized, deleted):
    post = _image_post()
    set_local_album(post, [{'url': f'{OURS}/a.png', 'alt_text': 'a'}, {'url': f'{OURS}/b.png', 'alt_text': 'b'}])
    db.session.commit()
    kept_id = post.album_images()[2].id
    sized.clear()

    set_local_album(post, [{'url': f'{OURS}/b.png', 'alt_text': 'b, again'}, {'url': f'{OURS}/c.png', 'alt_text': 'c'}])
    db.session.commit()

    assert _album(post) == [(f'{OURS}/b.png', 'b, again'), (f'{OURS}/c.png', 'c')]
    assert post.album_images()[1].id == kept_id   # reordered and re-captioned, not re-made
    assert deleted == [f'{OURS}/a.png']
    assert len(sized) == 1   # only c is new
    assert db.session.query(File).filter_by(source_url=f'{OURS}/a.png').count() == 0


def test_an_empty_album_clears_it(db_session, sized, deleted):
    post = _image_post()
    set_local_album(post, [{'url': f'{OURS}/a.png', 'alt_text': 'a'}])
    db.session.commit()

    set_local_album(post, [])
    db.session.commit()

    assert (_album(post), post.gallery_count, deleted) == ([], 0, [f'{OURS}/a.png'])


# --- through edit_post ---

def test_edit_post_stores_the_album_it_is_given(db_session, sized, http_mock):
    post = _image_post()
    http_mock.head(f'{OURS}/0.png').respond(200, headers={'Content-Type': 'image/png'})

    edit_post(_api_input(url=f'{OURS}/0.png'), post, POST_TYPE_IMAGE, SRC_API, user=post.author,
              album=[{'url': f'{OURS}/1.png', 'alt_text': 'one'}])

    assert _album(post) == [(f'{OURS}/1.png', 'one')]


def test_edit_post_without_an_album_leaves_it_alone(db_session, sized, http_mock):
    post = _image_post()
    set_local_album(post, [{'url': f'{OURS}/1.png', 'alt_text': 'one'}])
    post.url = f'{OURS}/0.png'
    db.session.commit()
    http_mock.head(f'{OURS}/0.png').respond(200, headers={'Content-Type': 'image/png'})

    edit_post(_api_input(url=f'{OURS}/0.png', title='a new title'), post, POST_TYPE_IMAGE, SRC_API, user=post.author)

    assert _album(post) == [(f'{OURS}/1.png', 'one')]


def test_a_post_that_stops_being_an_image_post_loses_its_album(db_session, sized, deleted):
    post = _image_post()
    set_local_album(post, [{'url': f'{OURS}/1.png', 'alt_text': 'one'}])
    db.session.commit()

    edit_post(_api_input(url=None), post, POST_TYPE_ARTICLE, SRC_API, user=post.author)

    assert (post.gallery_count, db.session.execute(post_file.select()).all()) == (0, [])


def test_an_album_over_the_limit_is_refused(db_session, sized):
    set_setting('max_album_images', 2)
    post = _image_post()

    with pytest.raises(Exception, match='At most 2 images'):
        edit_post(_api_input(url=f'{OURS}/0.png'), post, POST_TYPE_IMAGE, SRC_API, user=post.author,
                  album=[{'url': f'{OURS}/1.png', 'alt_text': None}, {'url': f'{OURS}/2.png', 'alt_text': None}])
