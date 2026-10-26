"""Follow-up F3: a thumbnail our thumbnailer wrote is marked (`File.thumbnail_written_by_us`), and deleting the post
reclaims it; so is a `file_path` our server wrote (`File.file_written_by_us`), such as the 512px copy. Only new rows are marked: a path shaped like our thumbnailer's is no proof that we wrote it, so a legacy
row (marker false) is left where it is, as before. A thumbnail another record names is kept either way."""
import ast
import functools
import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import current_app, g
from sqlalchemy import inspect, text

from app import db
from app.models import File, Post, Site
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_file_delete_from_disk import a_file, a_media_file

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / 'migrations' / 'versions' / 'a9c4e6b2d8f1_file_thumbnail_written_by_us.py'
THUMBNAIL = 'probe_longname_thumb_f3.webp'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    path = a_media_file(THUMBNAIL)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def gone(file):
    """A post's image deleted as the post's deletion deletes it: its files, then the post and the row."""
    file.delete_from_disk(purge_cdn=False)
    db.session.query(Post).filter(Post.image_id == file.id).delete()
    db.session.delete(file)
    db.session.commit()


def a_peer_posts_image(env_path, written_by_us, **columns):
    """A peer post whose image is the File our server made from the peer's picture: its own thumbnail on our disk,
    its `source_url` the peer's, and no local account, community or upload name to prove anything."""
    peer = make_user(make_instance('peer.test'), 'peerauthor')
    post = make_post(make_community('thumbland'), peer, ap_id='https://peer.test/post/f3')
    post.image = a_file(source_url='https://peer.test/media/photo.png', thumbnail_path=env_path,
                        thumbnail_written_by_us=written_by_us, **columns)
    db.session.commit()
    return post.image


def test_a_peer_posts_thumbnail_we_generated_is_reclaimed(env):
    gone(a_peer_posts_image(env, True))
    assert not os.path.exists(env)


def test_a_legacy_row_is_left_as_before(env, caplog):
    caplog.set_level('INFO')
    gone(a_peer_posts_image(env, False))
    assert os.path.exists(env)
    assert 'left' in caplog.text and 'on disk' in caplog.text


def test_a_new_row_defaults_to_not_ours(env):
    assert a_file(thumbnail_path=env).thumbnail_written_by_us is False


def test_a_thumbnail_another_record_names_is_kept(env):
    shared = a_peer_posts_image(env, True)
    a_file(thumbnail_path=env)   # another record naming the same file
    gone(shared)
    assert os.path.exists(env)


def test_the_thumbnails_mark_does_not_reclaim_file_path(env):
    other = a_media_file('probe_longname_f3_medium.webp')
    try:
        gone(a_peer_posts_image(env, True, file_path=other))
        assert not os.path.exists(env) and os.path.exists(other)
    finally:
        os.unlink(other)


def test_the_files_mark_does_not_reclaim_the_thumbnail(env):
    other = a_media_file('probe_longname_f3_medium.webp')
    gone(a_peer_posts_image(env, False, file_path=other, file_written_by_us=True))
    assert os.path.exists(env) and not os.path.exists(other)


def test_a_peer_posts_file_we_wrote_is_reclaimed_with_its_thumbnail(env):
    """The 512px copy `url_to_thumbnail_file` writes at `file_path` goes with the thumbnail."""
    other = a_media_file('probe_longname_f3_medium.webp')
    gone(a_peer_posts_image(env, True, file_path=other, file_written_by_us=True))
    assert not os.path.exists(env) and not os.path.exists(other)


def test_a_legacy_file_path_is_left_as_before(env):
    other = a_media_file('probe_longname_f3_medium.webp')
    try:
        gone(a_peer_posts_image(env, False, file_path=other))
        assert os.path.exists(other)
    finally:
        os.unlink(other)


def test_a_file_path_another_record_names_is_kept(env):
    other = a_media_file('probe_longname_f3_medium.webp')
    try:
        shared = a_peer_posts_image(env, False, file_path=other, file_written_by_us=True)
        a_file(file_path=other)
        gone(shared)
        assert os.path.exists(other)
    finally:
        os.unlink(other)


def test_a_new_row_defaults_to_not_ours_for_both_columns(env):
    file = a_file(file_path=env)
    assert (file.thumbnail_written_by_us, file.file_written_by_us) == (False, False)


def test_a_thumbnail_outside_the_media_folders_is_never_unlinked(env, tmp_path):
    outside = tmp_path / 'outside_longname_f3.webp'
    outside.write_bytes(b'x')
    gone(a_peer_posts_image(str(outside), True))
    assert outside.exists()


def test_a_record_not_yet_saved_cannot_reclaim_a_path(env):
    """Without an id it cannot be told from the records that name the file: fail closed."""
    File(thumbnail_path=env, thumbnail_written_by_us=True).delete_from_disk(purge_cdn=False)
    assert os.path.exists(env)


class TestInTheBucket:
    KEY = 'posts/aa/bb/probe_longname_f3.webp'

    @pytest.fixture(autouse=True)
    def in_s3(self, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: True)
        monkeypatch.setattr('app.models.flush_cdn_cache', lambda urls: None)

    def deleted_keys(self, monkeypatch, file):
        seen = []
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.delete_from_s3',
            type('_Task', (), {'delay': staticmethod(lambda keys: seen.extend(keys)),
                               '__call__': staticmethod(lambda keys: seen.extend(keys))})())
        file.delete_from_disk()
        return seen

    def test_a_marked_key_is_deleted(self, env, monkeypatch):
        file = a_file(source_url='https://peer.test/a.png', thumbnail_path=f'https://cdn.example/{self.KEY}',
                      thumbnail_written_by_us=True)
        assert self.deleted_keys(monkeypatch, file) == [self.KEY]

    def test_an_unmarked_key_is_not(self, env, monkeypatch):
        file = a_file(source_url='https://peer.test/a.png', thumbnail_path=f'https://cdn.example/{self.KEY}')
        assert self.deleted_keys(monkeypatch, file) == []

    def test_a_marked_file_key_is_deleted_an_unmarked_one_is_not(self, env, monkeypatch):
        marked = a_file(source_url='https://peer.test/a.png', file_path=f'https://cdn.example/{self.KEY}',
                        file_written_by_us=True)
        assert self.deleted_keys(monkeypatch, marked) == [self.KEY]
        unmarked = a_file(source_url='https://peer.test/b.png', file_path='https://cdn.example/posts/aa/bb/other.webp')
        assert self.deleted_keys(monkeypatch, unmarked) == []

    def test_a_marked_key_another_record_names_is_kept(self, env, monkeypatch):
        a_file(thumbnail_path=f'https://cdn.example/{self.KEY}')
        file = a_file(source_url='https://peer.test/a.png', thumbnail_path=f'https://cdn.example/{self.KEY}',
                      thumbnail_written_by_us=True)
        assert self.deleted_keys(monkeypatch, file) == []


# ---- nothing a peer, an API caller or a form supplies can set the mark ------------------------------------------

MARKS = ('thumbnail_written_by_us', 'file_written_by_us')
PATHS = ('thumbnail_path', 'file_path')

# The exact functions that set a mark, each beside a path our own code has just written; nothing else may. No function
# that reads a peer's object sets one: make_image_sizes_async resizes a picture it fetched and writes both files
# itself, and replace_local_upload_task copies the marks of our own row onto the copy it deletes the files of.
MARK_SITES = {
    ('app/activitypub/util.py', 'make_image_sizes_async'),
    ('app/activitypub/util.py', 'replace_local_upload_task'),
    ('app/community/util.py', 'save_banner_file'),
    ('app/community/util.py', 'save_icon_file'),
    ('app/utils.py', 'url_to_thumbnail_file'),
}
# Where a path is stored (other than cleared): the mark sites, and the move of our own file to the bucket.
PATH_SITES = MARK_SITES | {('app/utils.py', 'move_file_to_s3')}


def call_sites(source):
    """(function, kind, name, value) for every place `source` sets a mark or a path: a keyword of a call, or an
    assignment to an attribute. A mark named in a string (setattr, a dict key) is reported too."""
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in MARKS + PATHS:
                    found.append((function, 'keyword', keyword.arg, keyword.value))
                if keyword.arg is None and getattr(node.func, 'id', None) == 'File':
                    found.append((function, 'keyword', '**', keyword.value))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in MARKS + PATHS:
                    found.append((function, 'assign', target.attr, node.value))
        if isinstance(node, ast.Constant) and node.value in MARKS:
            found.append((function, 'string', node.value, node))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), None)
    return found


@functools.cache
def sites_in_app():
    return tuple((str(path.relative_to(ROOT)), *site) for path in sorted((ROOT / 'app').rglob('*.py'))
                 for site in call_sites(path.read_text()))


def a_mark_value_is_ours(name, value):
    """True, or the same mark read off our own row; never a value from anywhere else."""
    return (isinstance(value, ast.Constant) and value.value is True) or \
        (isinstance(value, ast.Attribute) and value.attr == name)


def test_a_mark_is_set_only_by_the_exact_functions_that_write_our_own_files():
    """Per call site, not per module: no other function in any module (the inbox's, the API's and the forms' included)
    sets a mark, so no incoming object, API field or form value reaches it. A new use must be added to MARK_SITES on
    purpose."""
    sites = sites_in_app()
    assert {(module, function) for module, function, kind, name, _ in sites if name in MARKS} == MARK_SITES
    assert [site for site in sites if site[3] == '**' or site[2] == 'string'] == []
    for module, function, kind, name, value in sites:
        if name in MARKS:
            assert a_mark_value_is_ours(name, value), (module, function, name)


def test_a_path_is_stored_only_where_our_own_code_wrote_the_file():
    """A File's thumbnail_path or file_path is set (other than cleared) only in the functions above and the move of our
    own file to the bucket, so a peer-supplied path has no way in beside a mark. Each `File(...)` call that names a
    path names its mark too."""
    stored = {(module, function) for module, function, kind, name, value in sites_in_app()
              if name in PATHS and not (isinstance(value, ast.Constant) and value.value is None)}
    assert stored == PATH_SITES
    for module, function, kind, name, value in sites_in_app():
        if kind == 'keyword' and name in PATHS:
            mark = MARKS[PATHS.index(name)]
            assert (module, function) in MARK_SITES and any(
                site[:2] == (module, function) and site[3] == mark for site in sites_in_app()), (module, name)


@pytest.mark.parametrize('source', [
    "def from_a_peer(file, doc):\n    file.thumbnail_written_by_us = True\n",
    "def from_a_peer(doc):\n    return File(thumbnail_path=doc['t'], file_written_by_us=True)\n",
    "def from_a_peer(file, doc):\n    setattr(file, 'file_written_by_us', True)\n",
    "def from_a_peer(doc):\n    return File(**doc)\n",
])
def test_the_guard_sees_a_mark_set_anywhere_else(source):
    assert {site[0] for site in call_sites(source)} == {'from_a_peer'}
    assert not {('app/inbox.py', 'from_a_peer')} <= MARK_SITES


def test_the_guard_sees_a_mark_that_is_not_ours():
    (site,) = call_sites("def f(file, doc):\n    file.file_written_by_us = doc['x']\n")
    assert not a_mark_value_is_ours('file_written_by_us', site[3])


# ---- the migration ----------------------------------------------------------------------------------------------

def load_migration():
    spec = importlib.util.spec_from_file_location('thumbnail_written_by_us_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection, name='thumbnail_written_by_us'):
    return next((c for c in inspect(connection).get_columns('file') if c['name'] == name), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('a9c4e6b2d8f1', 'f2a6c8d4b1e7')


@pytest.mark.parametrize('name', MARKS)
def test_the_column_is_a_non_null_boolean_that_defaults_to_false(app, db_session, name):
    with db.engine.connect() as connection:
        found = column(connection, name)
    assert found is not None and found['nullable'] is False and 'false' in str(found['default'])


def test_downgrade_then_upgrade_removes_and_restores_the_column_and_old_rows_get_false(app, db_session):
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert [column(connection, name) for name in MARKS] == [None, None]
                connection.execute(text("INSERT INTO \"file\" (thumbnail_path) VALUES ('app/static/media/old.webp')"))
                migration.upgrade()
            for name in MARKS:
                restored = column(connection, name)
                assert restored is not None and restored['nullable'] is False
                assert connection.execute(text(f"SELECT {name} FROM \"file\" "
                                               "WHERE thumbnail_path = 'app/static/media/old.webp'")).scalar() is False
        finally:
            transaction.rollback()
