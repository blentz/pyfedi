"""Deleting a File, and the path it is allowed to delete.

Sub-project 116 -- `File.delete_from_disk` and `File.local_path_for_url` in
`app/models.py`. A File row carries up to three locations: `file_path` and
`thumbnail_path`, which this instance wrote, and `source_url`, which is WHERE THE
IMAGE CAME FROM -- and for anything federated in, that is a string a peer chose.

`source_url` is set from `request_json['object']['image']['url']` and
`['icon'][-1]['url']` when a Create is processed, and from a remote actor's icon
and image when a profile is refreshed. Deleting the File turned that string into
a local path by replacement and unlinked it, so a remote instance could name any
file the application user is able to delete (D1324).
"""
import os

import pytest
from flask import current_app, g

from app import db
from app.models import File, Site


@pytest.fixture
def env(app, api_baseline, tmp_path):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, tmp_path=tmp_path,
                           server_url=current_app.config['SERVER_URL'],
                           server_name=current_app.config['SERVER_NAME'],
                           baseline=api_baseline)


def a_file(**columns):
    file = File(**columns)
    db.session.add(file)
    db.session.commit()
    return file


def a_media_file(name='probe.png', directory='app/static/media'):
    """A real file where this instance keeps its media, cleaned up by the caller."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'wb') as handle:
        handle.write(b'not really a png')
    return path


class TestWhatAUrlIsAllowedToName:
    """`local_path_for_url` is the whole of the defence, so it is asserted
    directly as well as through the delete."""

    def test_a_url_on_this_server(self, env):
        path = File().local_path_for_url(
            f'{env.server_url}/static/media/probe.png')
        assert path == os.path.realpath('app/static/media/probe.png')

    def test_a_url_on_another_server(self, env):
        assert File().local_path_for_url(
            'https://peer.test/static/media/probe.png') is None

    def test_a_host_that_merely_contains_this_one(self, env):
        """The old test was `SERVER_NAME in url`, which this passes."""
        assert File().local_path_for_url(
            f'https://evil.test/{env.server_name}/probe.png') is None

    def test_this_host_as_a_subdomain_of_another(self, env):
        assert File().local_path_for_url(
            f'https://{env.server_name}.evil.test/probe.png') is None

    def test_this_host_in_the_query_string(self, env):
        assert File().local_path_for_url(
            f'https://peer.test/probe.png?from={env.server_name}') is None

    def test_this_host_in_the_userinfo(self, env):
        """`https://user@host/` -- the host is what follows the @."""
        assert File().local_path_for_url(
            f'https://{env.server_name}@evil.test/probe.png') is None

    @pytest.mark.parametrize('path', [
        '/../../tmp/target',
        '/static/media/../../../tmp/target',
        '/static/../../etc/passwd',
        '/..%2f..%2ftmp%2ftarget',
        '/%2e%2e/%2e%2e/tmp/target',
        '/static/media/%2e%2e%2f%2e%2e%2f%2e%2e%2ftmp%2ftarget',
    ])
    def test_a_path_that_climbs_out_of_app(self, env, path):
        """Unquoted before it is resolved, so the encoded spellings are the same
        traversal."""
        assert File().local_path_for_url(f'{env.server_url}{path}') is None

    def test_a_sibling_directory_whose_name_starts_with_app(self, env):
        """`/appendix/x` is not inside `/app/`, and a containment check written
        as `startswith(root)` rather than `startswith(root + os.sep)` would take
        it -- which is the classic way this check is got wrong."""
        assert File().local_path_for_url(
            f'{env.server_url}/../appendix/probe.png') is None
        assert File().local_path_for_url(
            f'{env.server_url}/../app.evil/probe.png') is None

    def test_the_app_directory_itself_is_not_a_file(self, env):
        """The root resolves to `app` exactly, which names a directory: unlinking
        it would be an error rather than a deletion, but `local_path_for_url`
        should not offer it either."""
        assert File().local_path_for_url(f'{env.server_url}/../app') == \
            os.path.realpath('app')

    def test_a_path_that_stays_inside_app(self, env):
        assert File().local_path_for_url(
            f'{env.server_url}/static/media/ab/cd/probe.png') == \
            os.path.realpath('app/static/media/ab/cd/probe.png')

    def test_a_path_that_climbs_and_comes_back(self, env):
        """`/static/../static/media/x.png` resolves inside app, so it is allowed
        -- the test is where it LANDS, not whether it contains a `..`."""
        assert File().local_path_for_url(
            f'{env.server_url}/static/../static/media/probe.png') == \
            os.path.realpath('app/static/media/probe.png')

    def test_no_path_at_all(self, env):
        assert File().local_path_for_url(env.server_url) is None
        assert File().local_path_for_url(f'{env.server_url}/') is None

    def test_something_that_is_not_a_url(self, env):
        assert File().local_path_for_url('probe.png') is None
        assert File().local_path_for_url('') is None

    def test_the_server_name_with_its_scheme_missing(self, env):
        """`urlparse('test.piefed.local/x')` has no netloc, so this names
        nothing."""
        assert File().local_path_for_url(f'{env.server_name}/probe.png') is None


class TestDeletingTheFileItself:
    def test_a_local_file_path_is_unlinked(self, env):
        path = a_media_file('probe_local.png')
        file = a_file(file_path=path)
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)

    def test_a_thumbnail_is_unlinked_too(self, env):
        path = a_media_file('probe_thumb.png')
        file = a_file(thumbnail_path=path)
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)

    def test_both_at_once(self, env):
        first = a_media_file('probe_both_1.png')
        second = a_media_file('probe_both_2.png')
        file = a_file(file_path=first, thumbnail_path=second)
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(first)
        assert not os.path.exists(second)

    def test_a_file_that_is_already_gone(self, env):
        file = a_file(file_path='app/static/media/probe_absent.png')
        file.delete_from_disk(purge_cdn=False)

    def test_a_source_url_of_ours_is_unlinked(self, env):
        path = a_media_file('probe_source.png')
        file = a_file(source_url=f'{env.server_url}/static/media/probe_source.png')
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)

    def test_a_source_url_pointing_outside_app_deletes_nothing(self, env):
        """D1324, the defect itself. Measured before the repair: this deleted
        `/tmp/probe_delete_target`, a file no part of PieFed owns."""
        target = env.tmp_path / 'target'
        target.write_text('do not delete me')
        relative = os.path.relpath(str(target), os.path.realpath('app'))
        file = a_file(source_url=f'{env.server_url}/{relative}')
        file.delete_from_disk(purge_cdn=False)
        assert target.exists()

    def test_a_source_url_on_a_peer_deletes_nothing(self, env):
        path = a_media_file('probe_peer.png')
        try:
            file = a_file(source_url='https://peer.test/static/media/probe_peer.png')
            file.delete_from_disk(purge_cdn=False)
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_source_url_that_is_not_http_is_left_alone(self, env):
        path = a_media_file('probe_bare.png')
        try:
            file = a_file(source_url='app/static/media/probe_bare.png')
            file.delete_from_disk(purge_cdn=False)
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_file_with_nothing_set_at_all(self, env):
        a_file().delete_from_disk(purge_cdn=False)


class TestWhatIsPurgedFromTheCdn:
    def purged(self, env, monkeypatch, file):
        seen = []
        monkeypatch.setattr('app.models.flush_cdn_cache',
                            lambda urls: seen.append(urls))
        file.delete_from_disk()
        return seen[0] if seen else []

    def test_a_local_file_is_purged_by_its_url(self, env, monkeypatch):
        path = a_media_file('probe_purge.png')
        file = a_file(file_path=path)
        assert self.purged(env, monkeypatch, file) == \
            [f'{env.server_url}/static/media/probe_purge.png']

    def test_a_source_url_of_ours_is_purged(self, env, monkeypatch):
        a_media_file('probe_purge_src.png')
        url = f'{env.server_url}/static/media/probe_purge_src.png'
        assert self.purged(env, monkeypatch, a_file(source_url=url)) == [url]

    def test_a_peer_s_url_is_not_purged(self, env, monkeypatch):
        """It is not ours to purge, and asking Cloudflare to drop somebody
        else's url is a request that can only fail."""
        file = a_file(source_url='https://peer.test/media/probe.png')
        assert self.purged(env, monkeypatch, file) == []

    def test_a_traversing_url_is_not_purged_either(self, env, monkeypatch):
        file = a_file(source_url=f'{env.server_url}/../../tmp/target')
        assert self.purged(env, monkeypatch, file) == []

    def test_nothing_is_purged_when_the_caller_says_not_to(self, env, monkeypatch):
        path = a_media_file('probe_nopurge.png')
        file = a_file(file_path=path)
        seen = []
        monkeypatch.setattr('app.models.flush_cdn_cache',
                            lambda urls: seen.append(urls))
        file.delete_from_disk(purge_cdn=False)
        assert seen == []


class TestFilesKeptInS3:
    @pytest.fixture(autouse=True)
    def in_s3(self, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: True)

    def deleted_keys(self, monkeypatch, file):
        seen = []
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.delete_from_s3',
            type('_Task', (), {'delay': staticmethod(lambda keys: seen.append(keys)),
                               '__call__': staticmethod(lambda keys: seen.append(keys))})())
        monkeypatch.setattr('app.models.flush_cdn_cache', lambda urls: None)
        file.delete_from_disk()
        return seen[0] if seen else []

    def test_an_s3_file_is_handed_to_the_task(self, env, monkeypatch):
        file = a_file(file_path='https://cdn.example/media/probe.png')
        assert self.deleted_keys(monkeypatch, file) == ['media/probe.png']

    def test_the_thumbnail_and_the_source_go_too(self, env, monkeypatch):
        file = a_file(file_path='https://cdn.example/media/a.png',
                      thumbnail_path='https://cdn.example/media/b.png',
                      source_url='https://cdn.example/media/c.png')
        assert self.deleted_keys(monkeypatch, file) == \
            ['media/a.png', 'media/b.png', 'media/c.png']

    def test_a_url_on_another_host_is_not_treated_as_s3(self, env, monkeypatch):
        file = a_file(file_path='https://peer.test/media/probe.png')
        assert self.deleted_keys(monkeypatch, file) == []
