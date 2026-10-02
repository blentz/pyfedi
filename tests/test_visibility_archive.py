"""Archived threads never store the content of a non-public reply (D18, Task 6)."""
import glob
import gzip
import os

import orjson
import pytest

from app import db
from app.post.util import convert_archived_replies_to_tree, post_replies
from app.utils import archive_post
from app.visibility import RestrictedReply
from tests.factories import bearer
from tests.test_visibility_single_object import client_as, world  # noqa: F401  (world is a fixture)
from tests.test_visibility_reply_tree import _find, csrf_on  # noqa: F401  (csrf_on is a fixture)

ARCHIVED_DIR = 'app/static/media/archived'
SECRET = 'secret reply'


@pytest.fixture
def archived(app, world):  # noqa: F811
    w = world
    written = set(glob.glob(f'{ARCHIVED_DIR}/*'))
    w.reply_id, w.child_id = w.reply.id, w.public_child.id  # the rows are deleted by archiving
    # the factory leaves path empty; a real reply gets it from PostReply.new
    w.reply.path = [0, w.reply_id]
    w.public_child.path = [0, w.reply_id, w.child_id]
    w.public_post.reply_count = 2
    db.session.commit()
    archive_post(w.public_post.id, None)
    db.session.expire_all()
    w.archive_path = f'{ARCHIVED_DIR}/post_{w.public_post.id}.json.gz'
    yield w
    for path in set(glob.glob(f'{ARCHIVED_DIR}/*')) - written:
        if os.path.isfile(path):
            os.unlink(path)


def archive_json(w):
    with gzip.open(w.archive_path, 'rb') as handle:
        return orjson.loads(handle.read())


def stored(tree, reply_id):
    for entry in tree:
        if entry['id'] == reply_id:
            return entry
        found = stored(entry['replies'], reply_id)
        if found:
            return found
    return None


def test_archive_json_holds_only_a_stub_for_the_followers_reply(archived):
    w = archived
    data = archive_json(w)
    entry = stored(data['replies'], w.reply_id)
    assert set(entry) == {'id', 'parent_id', 'depth', 'post_id', 'visibility', 'path', 'replies'}
    assert entry['visibility'] == 'followers'
    assert entry['post_id'] == w.public_post.id
    assert entry['path'][-1] == w.reply_id
    assert SECRET not in orjson.dumps(data).decode()
    assert stored(entry['replies'], w.child_id)['body'] == 'public child'


def test_public_entries_record_their_visibility(archived):
    child = stored(archive_json(archived)['replies'], archived.child_id)
    assert child['visibility'] == 'public'


def test_convert_returns_a_restricted_reply_for_the_stub(archived):
    w = archived
    tree = convert_archived_replies_to_tree(archive_json(w)['replies'], w.public_post)
    entry = next(e for e in tree if e['comment'].id == w.reply_id)
    assert isinstance(entry['comment'], RestrictedReply)
    assert entry['restricted'] is True
    assert entry['comment'].path[-1] == w.reply_id
    assert entry['comment'].post_id == w.public_post.id
    child = entry['replies'][0]['comment']
    assert child.body == 'public child' and child.visibility == 'public'


@pytest.mark.parametrize('who', ['stranger', 'follower'])
def test_archived_post_page_renders_the_placeholder(app, archived, csrf_on, who):  # noqa: F811
    """Archives store no body, so even a follower gets the placeholder."""
    w = archived
    html = client_as(app, getattr(w, who)).get(f'/post/{w.public_post.id}').get_data(as_text=True)
    assert SECRET not in html
    assert 'Visible to followers only' in html
    assert 'public child' in html


def test_archived_post_replies_marks_follower_view_restricted(app, archived):
    w = archived
    tree = post_replies(w.public_post, 'new', w.follower)
    assert isinstance(next(e for e in tree if e['comment'].id == w.reply_id)['comment'], RestrictedReply)


def test_api_replies_on_an_archived_post_return_the_nested_stub(app, archived):
    w = archived
    response = app.test_client().get('/api/alpha/post/replies', query_string={'post_id': w.public_post.id},
                                     headers={'Authorization': bearer(w.stranger)})
    assert response.status_code == 200
    stub = _find(response.json['comments'], w.reply_id)
    assert stub['visibility'] == 'followers'
    assert stub['comment']['body'] is None and stub['creator'] is None
    assert stub['replies'][0]['comment']['body'] == 'public child'
    assert SECRET not in response.get_data(as_text=True)
